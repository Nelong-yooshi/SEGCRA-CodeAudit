# 05-執行驗證 spec_exec

## 在幹嘛(一段白話)

靜態審查只能看 SQL「像不像對的」;執行驗證讓它「真的跑一遍」。`orchestrator/spec_exec.py` 對每個 MR 做四步:(0)確定性找出這條 SQL 對應哪份核定規格(spec);(1)請一個 LLM(角色一,測資生成)讀 spec,把需求拆成原子條件,替每個條件造出「應命中」與「不應命中」兩向、外加數值門檻的邊界值測資;(2)由確定性程式(角色二,非 LLM)在 **MS SQL 沙盒**裡把每個測資案例灌進獨立的交易,原樣執行 MR 的 T-SQL,比對有沒有命中;(3)實際結果與測資預期不符時,再請另一個 LLM(角色三,仲裁)以 spec 為唯一真值判定是**測資造錯了**還是 **SQL 寫錯了**——前者剔除該案並記錄覆蓋缺口,後者才成為 major finding。整包結果(通過與否、條件清單、覆蓋缺口、逐案結果、被剔案例)寫進報告的 `_spec_exec`,供稽核;`passed` 是後續自動放行的**硬條件**。

## 防的是什麼

- **靜態審查的漏報**:比較運算子含不含等(`>` vs `>=`)、半開區間、豁免名單這類錯,肉眼與 LLM 都可能看漏,但邊界測資一跑必現形。mr_002 的埋雷(`>` 被改成 `>=`)就是靠「恰好 200,000」這個邊界案例抓到的。
- **測資生成自己的幻覺**:測資也是 LLM 生的——它會造錯資料、把預期標反、發明 spec 沒有的欄位。所以生成後有**確定性形狀/覆蓋檢查**(不合法直接剔除、缺向列入 coverage_gaps),執行層是純程式(執行本身不可有幻覺),mismatch 再交仲裁分辨責任方,而不是一律怪 SQL(避免假 major)、也不是一律信測資(避免漏抓真 bug)。實測案例見下文「為什麼要仲裁」。
- **「沒 spec 就跳過」的遺忘性放水**:必跑的意思不是「有 spec 才跑」,而是**找不到 spec = major「無規格可驗」→ 不得自動放行**。驗證缺席這件事本身就是 finding,不會靜默消失。
- **注入與旁路**:測資的資料表名要過識別字白名單(`_IDENT` regex)、DDL 只收 `CREATE TABLE` 開頭(`_DDL_OK`)、資料列一律走參數化 INSERT——LLM 生成的測資不可能藉表名或 DDL 夾帶任意 SQL。
- **基礎設施故障變成假結論**:LLM 傳輸層失敗(連線/逾時)不會把管線炸掉,也不會被當成「驗證通過」——一律降級成保守結果(詳見下文)。

## 怎麼建的

管線掛載點:`orchestrator/pipeline.py:review_mr` 在 enforce 鏈之後、`apply_rubric` 之前呼叫 `run_spec_exec(cfg, hub, mr, spec_code, spec_text)`(spec 在前處理已由 `find_spec` 找好傳入,避免重找),把回傳的 `findings` 併入報告、其餘欄位存成 `report["_spec_exec"]`。

### 第 0 步:找 spec(確定性;`spec_exec.py:extract_rule_codes` / `find_spec`)

1. `extract_rule_codes`:把 MR 的標題、描述、每個檔的路徑與內容(`full_content`,沒有就用 diff)串起來,用 regex `\bR-\d{2,4}\b` 抓規則碼,去重排序。
2. `find_spec` 兩路:
   - **mock/本地路**:逐碼查本包 `specs/<code>.md`,存在即讀入回傳。
   - **real 路**:**不讀本包的 `specs/`**,一律經 ToolHub 呼叫 `gitlab__get_file` 抓 GitLab repo 內的 `specs/<code>.md`(`toolbox/gitlab.py:get_file`,REST `repository/files/.../raw`);內容須以 `#` 開頭才算拿到(擋 404 佔位訊息)。不指定 `ref` → 讀**這次 MR 的版本**(pipeline 在取完 MR 之後釘住 head sha)。

     > 本包的 `specs/` 是評測與離線開發用的樣本,不是正式環境的規格。real 模式若先讀它,MR 改了 `specs/R-093.md` 時執行驗證拿到的還是樣本那份,而報告上看不出用的是哪一份。
     >
     > 規格讀 MR 的版本是刻意的:規格與程式會在同一個 MR 裡一起修正,鎖在目標分支會讓「規格寫錯、跟程式一起修」的 MR 被拿舊規格判成不符。代價是一個 MR 可以同時改規格與程式,這一點由「讓審查的人看見規格被改了」處理(見 #38 第 3 點),不是靠鎖版本。
   - 都找不到 → 回 `(第一個規則碼|None, None)`。
3. `run_spec_exec` 收到 `spec_text=None` 時,直接產出 **major「無規格可驗,無法執行驗證」**(`no_spec: True`、`passed: False`)並要求先補規格再送審;MR 完全抓不到規則碼也是同一條 major(措辭不同)。另一個早退:MR 還原不出 SQL(`_sql_from_diff` 只收 diff 的 `+` 行)→ major「MR 無可執行的 SQL」。

### 第 1 步:角色一 測資生成(LLM;`generate_cases` + `_shape_check`)

模型由 `config/models.yaml` 的 `roles.testgen` 指定 profile(預設 `review` = gemma4:31b;`orchestrator/config.py:role_profile`)。

**prompt 契約**(`TESTGEN_SYSTEM`):「spec 是唯一真值,不得自行增減條件」,要求輸出嚴格 JSON(無圍欄、無多餘文字),形狀:

```json
{"schema_ddl": ["CREATE TABLE ...(照 spec 的資料表定義)"],
 "conditions": [{"id": "C1", "desc": "<一個原子條件,如 tx_type='WITHDRAW'>"}],
 "cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
            "rows": [["<表名>", 值1, 值2, ...]],
            "expect_flagged": true, "note": "<在測什麼>"}]}
```

硬性要求(prompt 內明列):每個需求項目拆成**可獨立為真/假的原子條件**(過濾、閾值、時段、幣別、豁免、時間窗各自成條);每條件必有 `direction="true"`(條件成立→應命中)與 `"false"`(僅此條件不成立→不應命中)兩向,數值門檻另加**邊界案例**(恰等於門檻、差最小單位,依 spec 的含/不含決定 expect_flagged);rows 的值順序與 DDL 欄位順序一致;時間窗固定代入 `WIN_START/WIN_END = 2026-06-01/2026-06-02`(半開區間,窗內交易一律落在 6/1);案例之間互相獨立、各自只放自己需要的資料列;只准用 spec 資料表定義裡的表與欄位。

**確定性形狀/覆蓋檢查**(`_shape_check`)——LLM 輸出一律先過這關:

- `schema_ddl` 只留符合 `^\s*CREATE\s+TABLE\s`(`_DDL_OK`)的字串;
- `conditions` 須有 `id` + `desc`;
- 每個 case 逐項驗:有 `case_id`、`condition_id` 對得上條件、`direction ∈ {true,false}`、`expect_flagged` 是 bool、`rows` 非空且每列是「表名 + 至少一個值」的 list(長度 ≥ 2)、首元素為合法識別字表名(`_IDENT`,擋注入)——不合法者進 `dropped`(標 `shape-check`,原因「形狀不合法(缺欄位/表名非法/方向錯)」),不進執行;
- 覆蓋檢查:每個條件逐向檢查,缺 `true` 或 `false` 向 → `coverage_gaps` 記「條件 Cx 缺 x 向案例」;無合法 DDL、未拆出條件也各記一條。
- **資料要先對過資料表定義**:`schema_ddl` 解析出每欄的型別與限制(`_ddl_columns`),逐列檢查放不放得進去(字串長度、整數範圍、`DECIMAL` 的精度與小數位、`NOT NULL` 給 `NULL`、日期時間的寫法)。放不進去的案例當形狀不合法剔除、把原因回饋給模型重生 —— 這些值會通過其他檢查、到**灌資料時才失敗**,而灌資料失敗會讓整輪執行驗證停下來(後面的案例全部沒跑),不是只丟掉那一個案例。型別或限制認不出來就不檢查(誤判的代價是把好案例丟掉)。
  - 日期時間的小數秒上限照型別:`DATETIME`、`SMALLDATETIME` 收 3 位,其餘收 7 位(`DATETIME2` 預設就是 7 位)。`yyyymmdd`、`yyyymmdd HH:MM:SS` 都收得下;只給時間(`22:00:00`)擋下來,因為 SQL Server 會把日期當成 1900-01-01,時間窗一定對不上。
- **標籤一致性檢查**:案例宣稱測哪一條、哪一向,要用資料證明。基準案例要「自己這一支加上全域條件全部成立、別支不得整個成立」;變體要「跟同一支的基準相比真的弄壞了東西,而且改的**每一欄**都出現在目標條件的判斷式裡」(欄名用 sqlglot 解析取出,不是比對字串 —— 否則 `amount` 會被當成出現在 `fee_amount` 裡)。對不上的不動 `ok`(那是「行為符不符合預期」),另外列入 `coverage_gaps`:那一向沒有被有效驗證,不可據以自動放行。判斷式算不出來時整個案例的標籤算「未經驗證」,並把原因寫進通過的 finding —— 不然沒驗到看起來跟驗過一模一樣。
- **缺口與 `passed` 只有一份算法**:`gaps_and_passed()` 由執行結果算出覆蓋缺口與 `passed`,`run_spec_exec` 與 `eval/check_frozen_testdata.py` 共用。各算一份的時候,正式流程多一種缺口(例如沒跑到的案例、沙盒逾時),腳本印出來的「執行驗證的結論」就會跟真的不一樣 —— 而那正是用來判斷凍結檔能不能用的依據。

`generate_cases` 跑**至多三次**(`max_attempts`,預設 3):輸出解析不出 JSON、零合法案例、**或 `coverage_gaps` 非空**都算這次不夠好,帶提示重試;有合法案例但覆蓋不完整時,重試提示會**列出上一次缺了哪些條件、哪個方向**(評測固定 temperature 0 + seed 42,同一個 prompt 會得到逐位元組相同的輸出,不帶缺口的重試等於白跑)。LLM 傳輸層例外(連線/逾時)被 catch 住記為 `llm_error` 續跑。**抽到 0 缺口、且拆出的條件數不少於之前各次嘗試的最大值才提前結束**;沒抽到的話,多次嘗試中留**已覆蓋「條件 × 方向」最多**的一版,不是直接判失敗、也不是只認最後一次。比覆蓋數而不是缺口數,是因為缺口是對照模型**自己拆出的**條件算的:拆 12 個條件全覆蓋的那次是 0 缺口,不該勝過拆齊 23 個、只缺幾個案例的那次。已知限制:第一次就抽到「拆得少、0 缺口」時沒有東西可比,仍會直接收下——要擋這個得拿規格本身的條件數比對。全部嘗試都沒有任何合法案例(從未進到「留最佳版」這步)才真的判失敗 → 回空 plan + gap「測資生成失敗(LLM 呼叫失敗:…/N 次嘗試皆無合法輸出)」,`run_spec_exec` 轉成 **major「執行驗證無法完成(測資生成失敗)」**、`passed: False`——降級,不炸管線。

（改這個之前,舊版只要解析出任何案例就直接收下、完全不看覆蓋度——同一份規格單次取樣抽到不完整覆蓋,就把那次不完整的結果直接當生成完成。這正是 `eval/BASELINE.md`「R-140 測資生成二選一」那個現象的根因:兩種觀測到的結果都是非空案例,依舊版條件都會在第一次呼叫就直接回傳,重試機制從未介入過。)

> ⚠️ **這道重試只在小規模有效**。實測:`R-140`(7 個原子條件)修法後連跑 5 次皆 0 缺口;但真實程式形狀的 `RETAIL_M1_EVAL`(**23 個原子條件**)只產出 9 個甚至 0 個案例、近 40 個覆蓋缺口,**重試 3 次結果一樣**。原因是它解決的是「這次取樣抽到不完整」,而大規則是**每次都一樣不完整**:直接量測時 `max_output_tokens` 8,192 確實被截斷,但放寬到 32,768 模型也只生 12 個案例(需 46 個)就自己停下,調高上限救不回覆蓋度,下一步是分批生成。**在那個規模上執行驗證這道防線等於沒有生效**,只剩 LLM 靜態審查接住。詳見 `eval/golden/mr_701.json` / `mr_702.json` 的 `gap_note` 與 `eval/BASELINE.md`。
>
> **審查時間會變長**:決策行為不變,但真實規模的規則每次嘗試都會生成到輸出上限(被截斷後重試),最多 3 次——測資生成這段的耗時最多約為原本的 3 倍。

**截斷偵測**:`generate_cases` 在解析輸出**之前**先看 `run_agent` 回填的 `meta["finish_reason"]`,是 `length`(碰到 `max_output_tokens` 被硬切)就整次不收、帶「大幅精簡、兩向不可省略」的提示重試;全部嘗試都被截斷時,失敗原因明說是長度問題(調上限或改分批生成)。這一關不能靠 `_shape_check` 代勞:截斷的 JSON 會被 `extract_json` 的 json-repair 修補成合法物件,形狀檢查只看得到「案例比較少」,甚至可能剛好 0 缺口。

這道防線**整個依賴端點回報 `finish_reason`**,單元測試的 `run_agent` 是假的、證明不了這件事。2026-10-02 對 `gemma4:31b`(Ollama OpenAI 相容端點)實測:

| 檢查 | 結果 |
|---|---|
| 輸出上限 64、要求長輸出 | `meta` = `finish_reason: length`、`completion_tokens: 64` |
| 正常短回答 | `finish_reason: stop` |
| `generate_cases` 整條路徑,上限 256 | 0 案例,失敗原因「2/2 次因超過輸出長度上限被截斷…」,沒有把殘骸當結果 |

換端點或模型時(例如部署到客戶環境)端點可能不回報這個欄位,防線就會**靜默失效**。所以 `eval/preflight.py` 的「輸出截斷回報」探針會在每次跑批前重驗一次(走真正的 `run_agent`):碰到上限卻沒回 `length` 就中止跑批。

#### 關掉思考(`reasoning_effort`)救不了大規則——它只是讓失敗變得看不見

`gemma4` 回答前會先思考,而**思考的 token 也算在 `max_output_tokens` 裡**,
實測佔掉將近一半(R-140:思考 5,054 字 vs JSON 本體 5,647 字)。
既然測資生成的瓶頸是輸出空間,直覺是把思考關掉、把預算讓給真正要的 JSON。
#25 實作了 `reasoning_effort` 之後實際量了一輪(2026-10-12,`temperature=0`、`seed=42`):

| 規格 | 設定 | 耗時 | 模型拆出的條件 | 案例 | 回報的缺口 |
|---|---|---|---|---|---|
| R-140 | 端點預設(會思考) | 636.6s | 7 | 18 | 0 |
| R-140 | `none` | 1079.5s | 8 | 18 | 0 |
| RETAIL_M1_EVAL | 端點預設 | 3448.4s | **0** | **0** | 1(3/3 次截斷) |
| RETAIL_M1_EVAL | `none` | 2581.7s | **7** | **14** | **1** |

表面上最後一列是大勝:從「57 分鐘、3 次全截斷、0 個案例」變成「14 個案例、只缺 1 個」。
**但那個「只缺 1 個」是假的。**

`RETAIL_M1_EVAL` 的規格實際有 **23 個原子條件**(排除條件 3 條 +
條件 1 的 7 項 + 條件 2 的 7 項 + 條件 3 的 6 項),需要 46 個案例。
關掉思考之後模型拆出來的是:

```
C_ACC_VALID / C_DATE_FILTER / C_EARLYJOB_EXCL / C_INBOUND_MIN   ← 4 個,拆對了
C_COND1: kiosk_outbound_cnt [2,3] AND kiosk_outbound_amt [...] AND outbound_amt [...]
C_COND2: ...
C_COND3: ...
```

**它把條件 1 的七項子條件壓成「一個」條件**,條件 2、3 也一樣。
於是 23 個原子條件變成 7 個、14 個案例就「覆蓋完」了,覆蓋檢查回報「只缺 1 個」。

這正是 #23 第 3 點講的陷阱:**覆蓋檢查完全相信模型自己貼的條件編號**。
模型少拆,分母就跟著變小,覆蓋率自然好看。而一個複合條件的反向案例
**分不出是七項裡的哪一項不成立**,所以就算那 14 個案例全過,
也不代表那七項子條件各自驗過。

> **安全性上這是退步,不是進步。**
>
> | | 思考開著(現行) | 關掉思考 |
> |---|---|---|
> | 結果 | 3/3 截斷 → **誠實失敗** | 14 案例 / 1 缺口 |
> | 執行驗證 | 不做 | 做了,而且會過 |
> | 決策 | 必定交人工 | **可能自動放行** |
>
> 現行行為雖然難看,但它**知道自己沒做到**並且擋下來。關掉思考之後,
> 一份只驗了 7 個粗條件的測資計畫看起來像驗完了 23 個。
> 寧可誤差、不要錯放——這條原則在這裡指向保留現行行為。

R-140 各再跑一輪確認上面兩欄不是單次噪音,**兩個差異都穩定重現**:

| 輪 | 設定 | 耗時 | 條件 | 案例 | 缺口 |
|---|---|---|---|---|---|
| 1 | 預設 | 636.6s | 7 | 18 | 0 |
| 2 | 預設 | 683.7s | 7 | 18 | 0 |
| 1 | `none` | 1079.5s | 8 | 18 | 0 |
| 2 | `none` | 965.1s | 8 | 18 | 0 |

**第 8 個條件是個假條件。** 逐條對照:

| 預設(7 條) | `none`(8 條) |
|---|---|
| C6 `tx_time >= '2026-06-01' AND tx_time < '2026-06-02'` | C6 **`SUM(amount) per account per day`** |
| C7 `SUM(amount) >= 500000` | C7 `total_amount >= 500000` |
| | C8 `tx_time >= '2026-06-01' AND tx_time < '2026-06-02'` |

`none` 把「SUM(amount) >= 500000」拆成兩筆,其中一筆是**衍生指標的定義**
(「每帳戶每日加總」)而不是可驗證的條件——沒辦法替它寫一個反向案例。
但覆蓋檢查照樣回報 0 缺口,因為模型自己宣稱它正反兩向都有。

所以兩份規格的退化方向是一致的,只是嚴重程度不同:

| 規格 | 關掉思考後的拆解 |
|---|---|
| R-140(7 個原子條件) | 多一個不可驗證的假條件 |
| RETAIL_M1_EVAL(23 個) | 把 7 項子條件壓成 1 個,分母從 23 變 7 |

**兩種退化覆蓋檢查都看不出來**,因為它信的是模型自己貼的條件清單。

另外:**關掉思考沒有變快**,R-140 兩輪平均慢了 55%(660s → 1022s)。
RETAIL_M1 那組快了 15 分鐘,但那是因為它提早放棄拆解,不是效率變好。

**結論:`reasoning_effort` 這個旋鈕留著(有端點或模型換了之後會用上的可能),
但預設維持不送,測資生成也不要關思考。** #23 第 2 點的分批補生成仍然要做——
真正的問題是「模型一次拆不完也生不完 23 個條件」,而那不是輸出空間省下來就能解決的。

### 第 2 步:角色二 沙盒執行(確定性程式,非 LLM;`prepare_sql` / `execute_cases`)

**為什麼沙盒必須是 MS SQL**:規則程式跑在 MS SQL 上。不同引擎在整數除法(`7/2` 在 SQL Server 是 3,在 DuckDB 是 3.5)、`DATETIME` 精度、型別轉換、日期函式上的語意不同,同一段 SQL 在別的引擎上「通過」,不代表在正式環境通過。所以沙盒是 `scripts/sandbox.sh` 起的 SQL Server 容器(Developer 版,只綁 127.0.0.1),**SQL 本體原樣執行、不做方言轉譯**。

**SQL 前處理**(`prepare_sql`),只做三件事:
1. 整句是 `INSERT INTO … SELECT`(含前置 `WITH`)時,用 sqlglot(tsql 方言)取出 SELECT 部分——驗的是篩選邏輯,通報寫入表不在沙盒 schema 裡;其餘 SQL 不經 sqlglot,原字原句;
2. 系統日期函式(`GETDATE()`、`SYSDATETIME()`、`CURRENT_TIMESTAMP` 等)換成窗尾,確保可重現;
3. 用到 `@start_date` / `@end_date` 時,前面補 `DECLARE @start_date DATE = '2026-06-01', @end_date DATE = '2026-06-02';`,並固定 `SET NOCOUNT ON; SET DATEFORMAT ymd;`。

**逐案執行**(`execute_cases`):每次執行在沙盒建一個專用資料庫(`segcra_sbx_<隨機碼>`,同時進行的審查互不干擾),結束後刪除;超過一小時的殘留資料庫會在下次執行時清掉。每個案例在**獨立交易**裡執行,結束一律 rollback,下一個案例從乾淨狀態開始:

1. 跑 plan 的所有 `schema_ddl` 建表;
2. 逐列 `INSERT INTO <表名> VALUES (%s,%s,…)` 參數化灌入該案例的 rows;
3. **這兩步任何例外 = 測資側的錯**(schema/資料造不起來)→ 整包回 `testdata_error`,`run_spec_exec` 轉 major「執行驗證無法完成(測資無法建置)」→ 需人工;
4. 執行 prepared SQL;**這步例外 = SQL 側的錯**(語法/欄位/非 T-SQL 寫法)→ 回 `sql_error`,轉 major「SQL 無法在測資上執行」;

連不上沙盒(沒啟動、密碼錯)→ 回 `sandbox_error`,轉 major「執行驗證無法完成(沙盒不可用)」,並註明是環境問題而非程式問題——照樣 `passed: False`,**沙盒不可用絕不等於驗證通過**。報告的 `_spec_exec.engine` 記錄實際執行的引擎版本,供稽核。

> **第 3、4 步是在 per-case 迴圈裡 `return`,也就是第一個失敗的案例會中止整輪。**
> 中止本身合理(SQL 真的壞掉時,跑完 46 個案例只會得到 46 個相同錯誤),但
> **沒跑到的案例一律列入 `coverage_gaps`**(`_unrun_gaps()`)——這與仲裁剔除案例時
> 的處理是同一個道理:**沒驗到就是沒驗到**。少了它,報告會顯示「跑了 3 個案例、
> 零缺口」,讀起來像全覆蓋,實際上計畫裡多數案例根本沒執行,而 `coverage_gaps`
> 正是人用來判斷「這次到底驗了多少」的欄位。決策層本來就會因 major 擋下自動放行,
> 所以這不是放行漏洞,修的是**報告的誠實度**。
5. 比對邏輯:`actual_flagged = 輸出列數 > 0`,與 `expect_flagged` 相等即 `ok`;不等進 `mismatches`(帶原始 rows 供仲裁)。每案記錄 `case_id/condition_id/direction/note/expect_flagged/actual_flagged/actual_rows(前5列)/ok`。

### 第 3 步:角色三 仲裁(LLM;`arbitrate`)

**何時出動**:只對 `mismatches` 逐案呼叫——全符時零 LLM 呼叫。模型由 `roles.arbiter` 指定(預設同 review)。

**prompt**(`ARBITER_SYSTEM` + user):給它核定規格全文(標明「唯一真值」)、MR 的 SQL、該案例的條件/方向/測資說明/資料列/預期 vs 實際(含 SQL 實際輸出列)。原則:只以 spec 條文判斷、不腦補;逐步核對「這筆資料在 spec 之下應該命中嗎」再看 SQL 為何相反;**測資與 SQL 都可能錯,不預設任何一方;無法確定時傾向判測資錯**——理由是代價不對稱:誤殺一個測資案例只是少驗一個角(且會記成覆蓋缺口,照樣擋自動放行),誤指控 SQL 會生出假 major。輸出嚴格 JSON:`{"who_is_wrong": "testdata"|"sql", "reason": "<依 spec 條文的推理>"}`。

**兩種結局**:

- 判 `testdata` → 該案進 `dropped_cases`(`by: "arbiter"` + 理由),對應 `case_results` 標 `dropped: True`,**並在 `coverage_gaps` 補一條**「案例 X(條件 C,某向)遭仲裁剔除(測資錯),該向未驗證」——被剔除 ≠ 通過,該條件該向就是沒驗到,不可據以自動放行。
- 判 `sql` → 收進 `sql_faults`,最後彙整成**一條 major「執行驗證失敗:實作與規格行為不符」**,detail 逐案列出預期/實際與仲裁理由,建議對照規格修正(含/不含、時段、過濾、粒度)。

**無法解析時的保守路徑**:仲裁 LLM 呼叫失敗(傳輸層例外)或輸出解析不出合法 `who_is_wrong` → 回 `{"who_is_wrong": "sql", "reason": "(仲裁…失敗,保守列為 SQL 側待人工確認)", "arbiter_unparseable": True}`——寧可多一條待人工確認的 major(→ needs_human),不可把 mismatch 靜默吞掉。

### LLM 傳輸失敗的優雅降級(彙總)

單次 LLM 請求上限 3600 秒、**不自動重試**(`orchestrator/agent.py:REQUEST_TIMEOUT`,環境變數 `LLM_TIMEOUT` 可調;openai client 預設的 600s×3 次重試對本地慢推理只是把同一個慢請求連跑三遍)。在 spec_exec 內:

| 失敗點 | 行為 |
|---|---|
| 測資生成呼叫失敗(兩次) | major「執行驗證無法完成(測資生成失敗:LLM 呼叫失敗)」→ `passed: False` → needs_human |
| 仲裁呼叫失敗 / 輸出無法解析 | 該 mismatch 保守判 SQL 側(帶 `arbiter_unparseable`)→ major → needs_human |

兩者都不會中斷整條審查管線,也絕不會產生「假通過」。

### `passed` 的定義(`run_spec_exec` 末段)

```
effective = 未被剔除的 case_results
passed = bool(effective) and not sql_faults and not coverage_gaps
```

三個條件缺一不可:**至少有一個有效案例**(全被剔光不算通過)、**零 SQL 側不符**、**零覆蓋缺口**(含生成階段缺向與仲裁剔除造成的缺口)。通過時補一條 info「執行驗證通過(N 案例全數相符)」;有缺口時補 info「執行驗證覆蓋缺口」逐條列出。

### 時間上限與各階段耗時(#45 / #53 review)

整段有一個上限 `SPEC_EXEC_TIMEOUT_SECONDS`(由 `orchestrator/pipeline.py` 讀;讀不懂
退回預設並印警告。預設值見 [設定參數一覽](10-設定參數.md))。三個階段各自計時,結果放在 `_spec_exec.stage_seconds`
(`{"測資生成": 秒, "沙盒執行": 秒, "仲裁": 秒}`),**逾時那次也會留下**
(`report["_spec_exec_stage_seconds"]`)—— 上限要照這些數字訂,而逾時那一次正是最需要
知道時間花在哪裡的一次。

失敗或逾時的 finding 會標出是哪個階段(`[執行驗證/沙盒執行] 審查未完整(逾時)`,
並在 finding 上附 `stage` 欄位):三件事的修法完全不同,測資生成要調 prompt 或換做法、
沙盒要看環境、仲裁是另一次模型呼叫。

**沙盒階段是同步呼叫,所以不能只靠 `asyncio.wait_for`**:`execute_cases` 用 pymssql,
它在跑的時候事件迴圈整個卡住,`wait_for` 插不進去(實測:上限 1 秒、沙盒卡 6 秒 →
跑了 6 秒、沒有逾時、結果照常回傳)。現在的做法是兩道:

1. `execute_cases` 收一個截止時間,**過了就不再開始新的案例**(手上那個照跑完 —— 單一
   查詢本來就有 120 秒上限,所以最多超過一個查詢的時間);沒跑到的案例由 `_unrun_gaps`
   列成覆蓋缺口,不會被當成「驗過而且沒問題」。
2. 它被丟到一條 **daemon 背景執行緒**(`spec_exec._in_thread`),外層的 await 才能準時
   回來。不用 `asyncio.to_thread` 的預設執行緒池:`asyncio.run` 結束時會等池子裡的工作
   做完,等於逾時又被拖回去(跟 #33 的 `_wait_in_thread` 同一個理由)。`contextvars`
   會帶進去 —— 審查用的 ref 釘在 ContextVar 裡(#31),不帶的話背景那邊讀到預設值。

### 為什麼要仲裁——測資生成也會幻覺(mr_003 實測)

如果把每個 mismatch 都直接當 SQL 錯,測資的幻覺就會變成假 major。以下是本機對 `fixtures/mr_003.json`(R-093,埋雷:`HAVING COUNT(*) >= 20` 被改成 `>= 2`)完整跑一次的實際輸出(`demo.py --mr 003`,--profile fast / gpt-oss:20b):

角色一把 R-093 拆成 3 個條件(C1 `tx_type='TRANSFER'`、C2 時間窗、C3 `COUNT(*) >= 20`),生成 6 個案例。其中 **C3-F** 這個案例,note 寫的是「threshold false (**19 transfers**)」、`expect_flagged: false`——但它實際造出來的資料是帳戶 1006 **整整 20 筆**轉帳。這就是測資幻覺:嘴上說 19 筆、手上造 20 筆。角色二執行後該案 `actual_flagged: true`(SQL 輸出 `["1006", "20"]`)→ mismatch。角色三仲裁的實際判語(節錄):

> Spec requires a trigger when an account has 20 or more TRANSFER transactions in a day (HAVING COUNT(*) >= 20). The provided test data contains exactly 20 rows for account 1006 …, so it meets the threshold and should be reported. The test expectation states "threshold false (19 transfers)", which contradicts the actual row count; therefore **the testdata expectation is incorrect**. (Note: the SQL's HAVING COUNT(*) >= 2 is also non-compliant with the spec, but this does not affect the mismatch in this particular case.)

仲裁判 **testdata** → C3-F 進 `dropped_cases`(`by: "arbiter"`),同時 `coverage_gaps` 記入「案例 C3-F(條件 C3 false 向)遭仲裁剔除(測資錯),該向未驗證」→ `passed: false`。若當初直接判 SQL 錯,這條 major 的敘述會是錯的(拿一筆「本來就該命中」的資料指控 SQL 誤報);若默默把案例丟掉不記缺口,C3 的 false 向等於沒驗過卻可能被當成全過。兩頭的錯這個設計都擋住了。

同場加映這次運轉的分工:被剔除的 C3-F 沒驗到「19 筆不通報」,所以 `>= 2` 的埋雷這一輪沒有被執行驗證直接抓到——但它逃不掉:(1)覆蓋缺口讓 `passed: false`,自動放行永遠不成立;(2)靜態審查層(規格逐項核對)另外報了 major「實作與核定規格不符:閾值調低」;(3)mr_003 的提示注入被確定性掃描命中 → blocker → 最終決策 `blocked`。單一防線可以失手,疊起來的閘門不會。

## 對決策的影響

- `run_spec_exec` 的 `findings` 直接併入報告(`pipeline.py:review_mr`),**在 `apply_rubric` 之前**——所以執行驗證的 major 會照 rubric 扣 15 分、進 verdict 計算;不是旁路註記。
- 其餘結果存 `report["_spec_exec"]`,其中 `passed` 被 `apply_policy` 當**硬條件**讀取(`spec_exec_ok = _spec_exec.passed is True`):沒 spec、測資生成失敗、測資建不起來、SQL 跑不動、任何 SQL 側不符、任何覆蓋缺口——全都 `passed: False` → 自動放行條件永遠不成立,至多 `needs_human`。此條件寫死在管線,`config/models.yaml` 關不掉(詳見 docs/06)。
- spec_exec 自己不產 blocker、不改既有 finding 的 severity;它走 `blocked` 的唯一途徑是別的防線(如注入掃描)已給了 blocker。

## 怎麼驗證它在動

**(a) 端到端——mr_002(埋雷:`>` 改 `>=`,規格 R-305 核定「超過=不含」)**:

```bash
cd SEGCRA-CR && .venv/bin/python demo.py --mr 002
```

預期(以下為本機實測輸出,--profile fast):角色一把 R-305 拆成 5 個條件(C1 交易範圍、C2 夜間時段、C3 幣別、C4 時間窗、C5 閾值),生成 11 個案例,其中含邊界案例 `C5-boundary-200k`(夜間跨行累計**恰 200,000**,note「Exact threshold, should not flag.」,`expect_flagged: false`);角色二執行時 SQL 的 `>=` 讓它命中(`actual_rows: [["12345", "200000.00"]]`)→ 唯一的 mismatch;角色三引規格判 **SQL 錯**(判語節錄:「Spec states the threshold is "超過(不含)" meaning strictly greater than 200,000 (`> 200000`). The SQL uses `HAVING SUM(t.amount) >= 200000`…」)→ 報告出現 major「執行驗證失敗:實作與規格行為不符」,`_spec_exec.passed: false`、`coverage_gaps: []`、`dropped_cases: []`(其餘 10 案全 ok),`_policy_signals.spec_exec_passed: false`,決策 `needs_human`。

**(b) 只跑 spec_exec 三角色(不跑整條審查管線,快很多)**:

```bash
cd SEGCRA-CR && .venv/bin/python - <<'EOF'
import asyncio, json
from orchestrator.config import load_config
from orchestrator.spec_exec import run_spec_exec
mr = json.load(open("fixtures/mr_002.json"))
res = asyncio.run(run_spec_exec(load_config(), None, mr))   # hub=None → 走本地 specs/
print(json.dumps({k: res[k] for k in
      ("passed", "spec_code", "coverage_gaps", "dropped_cases")}, ensure_ascii=False, indent=1))
for r in res["case_results"]:
    print(("OK " if r["ok"] else "XX "), r["case_id"], r["direction"],
          "expect", r["expect_flagged"], "actual", r["actual_flagged"])
EOF
```

**(c) 「無規格可驗」路徑(不需 LLM,秒回)**:把上段的 fixture 換成一個沒有規則碼的假 MR:

```python
mr = {"title": "調整報表", "files": [{"path": "sql/x.sql", "full_content": "SELECT 1"}]}
```

預期輸出:`passed: False`、`no_spec: True`、findings 只有一條 major「無規格可驗,無法執行驗證」。

**(d) 找 spec 是否正常,用 dry-run 看**:`demo.py --mr 001 --dry-run` 的 `_plumbing.spec_found / spec_code` 應為 `true / R-201`(dry-run 不跑三角色,只驗 spec 找得到)。

## 擴充與注意

- **加一條新規則**:寫 `specs/R-xxx.md` 即可(格式見 README §4 與下方要點),spec_exec 不用改碼——規則碼靠 regex 自動對應。real 模式規格放 GitLab repo 的 `specs/` 目錄。
- **怎麼寫 spec 才驗得動**(整理自 README §4 與三份內附 spec):
  1. **「資料表定義」段必須釘死 schema**(可直接執行的 `CREATE TABLE`)——角色一照抄建表,沒有固定 schema 整個執行驗證做不起來;
  2. **需求項目逐條原子化、含運算子**:每條具體到可直接對照實作(交易範圍、聚合、閾值、時間窗、幣別、豁免、通報粒度、輸出欄位各自成條),用語與含等一致——「達/以上」=含=`>=`、「超過」=不含=`>`,並在補充段記下核定依據;含等寫錯一定被邊界案例抓到,寫「模糊」則測資會亂猜;
  3. 時間窗一律寫半開區間(`>= @start_date AND < @end_date`),參數名固定 `@start_date`/`@end_date`;程式與規格的 SQL 一律寫 T-SQL(`BIT` 用 `= 1`、`DATEPART(HOUR, x)`、字串型別寫長度、日期時間用 `DATETIME2` 而非 `TIMESTAMP`);
  4. 表格式規格 → md 的整理範例在 `examples/sample/RETAIL_M1_spec.md`(原則:只寫核定規格有的、實作細節列「待補」不腦補、跨源不一致以核定規格為準);dbt + Jinja 的規則程式要先展開樣板才能執行——目前尚未內建,含樣板的檔案會被擋下需人工(不會誤判通過)。
- **換模型**:`config/models.yaml` 的 `roles.testgen` / `roles.arbiter` 各自指到任一 profile;小模型常產不出合法測資 JSON(症狀:「測資生成失敗」finding),把 testgen 指回大 profile 即可(README §10)。
- **改 prompt**:`orchestrator/spec_exec.py` 的 `TESTGEN_SYSTEM`(測資契約)與 `ARBITER_SYSTEM`(仲裁原則);執行窗常數 `WIN_START/WIN_END` 也在檔頭。改 TESTGEN 的輸出形狀時,`_shape_check` 必須同步改——形狀檢查是契約的執行者。
- **已知限制**:
  - 一個 MR 只驗**一份 spec、一段 SQL**:`find_spec` 回第一個找得到規格檔的規則碼;SQL 取自最後一個有內容的變更檔。一個 MR 改多條規則時,現版只驗到其一——拆 MR 是目前的正解。
  - 命中判定是「有無輸出列」(`len(rows) > 0`),**通報粒度**(每帳戶每日至多 1 筆)與輸出欄位語意不在執行驗證範圍,靠靜態審查的 H002/規格核對把關。
  - 覆蓋是「LLM 拆出的條件」的覆蓋:若角色一漏拆某條件,形狀檢查看不出「本來該有第 N 條」——上限由 spec 條列品質決定,這也是需求項目要逐條原子化的原因。
  - 沙盒需要先啟動(`scripts/sandbox.sh start`,約 2GB 記憶體);沙盒未啟動時執行驗證一律判不通過(major、needs_human)。
  - dbt 樣板(`{{ ref() }}`、`{{ var() }}`、macro)尚未展開:含樣板的程式會落在 `sql_error`,不會誤判通過。

## 結構化生成:規格的形狀分類

結構化生成是:模型只讀規格、抽成結構,資料與預期值由程式照結構產生,不再由模型逐筆寫。**放不進結構的規格,執行驗證記成「未完成(結構不支援:原因)」交人工**,不退回模型逐筆生成。

所以放不放得進,看的是規則的**形狀**(用到哪幾種計算),不是條件多寡或難度。下表逐份對照結構目前打算支援的形狀,用途是**決定結構先擴充哪一種形狀**:同一種原因出現,就把那種形狀加進結構,之後同形狀的規則都能用。

結構目前打算支援的形狀(結構還在實作,最後以 `orchestrator/spec_exec.py` 實作的為準):

- **篩選**:清單(`IN`)、等於、門檻、NULL 照算(只排除明確標記者)、不可空白、多段區間。每條篩選要標明套用範圍:是整張表,還是只算進某一個指標(例如「小額入帳不列計」只影響入帳的指標)。
- **指標**:每組(帳戶、交易日)的筆數、總額、最後一筆的欄位值、兩個總額的比例(分母為 0 時的取值、四捨五入位數)。
- **門檻與分支**:分支之間 OR、分支內 AND,門檻含不含端點逐條寫明。
- **時間窗**:單一交易日(半開區間,或日期等於)。
- **排除名單**:另一張表裡有的帳戶不輸出(例如前一日已出過的帳戶)。

放不進的形狀:join 其他資料表取欄位、由多個欄位運算出的衍生欄位、逐筆輸出(命中判定是有無輸出列)、跨多日累計、排名。

### 手上 11 份規格的分類(2026-10-26)

| 規格 | 用到的形狀 | 放得進 | 放不進的原因 / 要擴充的形狀 |
|---|---|---|---|
| `R-093` | 等於;筆數 ≥ 門檻;單日 | 是 | — |
| `R-140` | 清單、等於、門檻、NULL 照算 ×2;總額 ≥ 門檻;單日 | 是 | — |
| `R-201` | 等於 ×3;總額 ≥ 門檻;單日;**另一張表的豁免名單(不分日期)** | 擴充後可以 | 排除名單目前只有「前一日已出過」的形狀;要加**不分日期的名單**(`NOT EXISTS`,只比對帳號) |
| `R-305` | 等於 ×3;總額 > 門檻(不含);單日;**時段:交易時間的小時 < 6** | 擴充後可以 | 篩選要加**時段**(取交易時間的小時比大小)。只在單日內、不跨日,不是衍生欄位 |
| `R-410` | 清單、門檻、NULL 照算 ×2;筆數與總額 ×4;兩個分支(OR),分支內 AND;介於含兩端;單日 | 是 | 「小額入帳不列計」只套在入帳的指標上,結構要能標篩選的套用範圍 |
| `R-520` | 清單 ×2、NULL 照算;兩個總額的比例(分母 0 取 0、四捨五入至小數 2 位);介於含兩端;單日 | 是 | 產生器要內建分母為 0 的案例,以及四捨五入的兩側(1.004、0.996) |
| `R-863` | 清單;總額 > 門檻(不含);單日 | 是 | — |
| `RETAIL_M1_EVAL` | 清單、等於、門檻、不可空白;筆數、總額、比例、**最後一筆的餘額**;三個分支(OR);單日(日期等於);前一日排除名單 | 是 | 最後一筆依 `posted_date DESC, txn_time DESC` 排序,兩個排序鍵都要能產生同值與不同值的案例 |
| `SHARED_A_EVAL` | 清單;總額 ≥ 門檻;單日 | 是 | 門檻寫在共用門檻表,結構用規格寫的數字 |
| `SHARED_B_EVAL` | 同上 | 是 | 同上 |
| `examples/sample/RETAIL_M1_spec.md` | — | **否** | 不是形狀的問題:這份是整理格式的範例,**沒有「資料表定義」段**,「沖銷交易」也沒寫判定欄位(列在待補)。補上資料表定義後的版本就是 `RETAIL_M1_EVAL` |

結論:

- 11 份裡 **8 份照結構的現有形狀就放得進**。2 份各要擴充一種形狀:**不分日期的排除名單**(`R-201`)、**時段**(`R-305`)。兩種都只多一個篩選或名單的種類,不影響產生器組合案例的方式。
- 沒有一份用到 join、衍生欄位、逐筆輸出、跨多日累計或排名。
- 1 份(範例規格)不是形狀的問題,是規格沒寫資料表定義。這種規格現在也驗不了,要先補規格。

產生器要注意的兩件事,不影響放不放得進:

- **待補事項不產生依賴它的案例**。例如 `R-410` 的「沒有自助設備出帳的帳戶要不要參與判斷」、`R-520` 的「負數金額」,規格沒寫答案,照某一種解讀算出的預期值,會把照另一種解讀寫的程式判成不符。
- **篩選的套用範圍要照規格**。`R-410` 的「行銷活動撥入不列計」寫在排除資料段,沒寫是否也影響出帳。這種情況照規格字面(整張表)產生,並在報告附上的結構裡寫出來,讓看報告的人分得出是測資的解讀還是程式的問題。

### 覆蓋檢查(`eval/coverage_check.py`)

測資夠不夠,不靠人讀測資判斷。「測到」的定義是:**有一個案例,在這個要素一變的時候結果就會不同。** 工具把正確的 SQL 只改一個要素,用凍結的案例在沙盒跑;有案例結果跟預期不同,這個要素就算測到。

```bash
python eval/coverage_check.py R-140
```

- **覆蓋目標分七類,各自統計**:
  - 條件兩向:每條篩選與每個子條件拿掉一次、反過來一次,分支之間的 OR 寫成 AND;
  - 排除條件:每條排除條件拿掉一次、反過來一次;
  - 清單代碼:每個代碼各拿掉一次,清單外的代碼加進去一次;
  - 門檻邊界:每個門檻的兩端各改成不含端點、各往外挪一格;
  - NULL 語意:拿掉 NULL 照算;
  - 時間窗:拿掉時間窗,起點與終點的含不含各改一次,排除名單的比對日;
  - 指標計算:最後一筆怎麼排序、比率的除零與進位。
- 目前列了 `R-140`(24 個)與 `RETAIL_M1_EVAL`(142 個)。`RETAIL_M1_EVAL` 的子條件由規格的條件清單產生;同一段在幾個分支重複出現時,只改它所屬的那一個分支。
- 報告開頭印出凍結檔記的**生成方式**(`generation`:模型逐筆寫,或程式照結構產生)。加這個欄位之前凍結的檔案標成「未記錄」,不猜。新凍結的檔案由 `eval/freeze_testdata.py` 寫入;程式照結構產生的測資會在 plan 裡帶 `generation`。
- **跟測資怎麼生成的無關**:只讀凍結檔的案例(資料列與預期),模型逐筆生成的、程式照結構產生的都能檢查,所以能拿現在的凍結檔當對照基準。
- **等價的改法不列**:改了對任何資料結果都一樣的,不是測資的問題(例如金額兩位小數時,`>= 500000.01` 跟 `> 500000` 等價,門檻下緣改用 `>= 499999.99`)。
- 正確的 SQL 取自正向對照的 golden case(`check_frozen_testdata.REFERENCE`)。正確的 SQL 自己跑不起來、或已經有案例不符時,不下覆蓋結論。改過的 SQL 跑不起來的要素記成「無法判斷」,不算測到。
- 新規則要用時,在 `TARGETS` 加一組。之後結構化生成接上,目標清單可以直接從結構產生。
