# 05-執行驗證 spec_exec

## 在幹嘛(一段白話)

靜態審查只能看 SQL「像不像對的」;執行驗證讓它「真的跑一遍」。`orchestrator/spec_exec.py` 對每個 MR 做四步:(0)確定性找出這條 SQL 對應哪份核定規格(spec);(1)請一個 LLM(角色一,測資生成)讀 spec,把需求拆成原子條件,替每個條件造出「應命中」與「不應命中」兩向、外加數值門檻的邊界值測資;(2)由確定性程式(角色二,非 LLM)把每個測資案例灌進一個乾淨的 in-memory DuckDB,跑 MR 的 SQL,比對有沒有命中;(3)實際結果與測資預期不符時,再請另一個 LLM(角色三,仲裁)以 spec 為唯一真值判定是**測資造錯了**還是 **SQL 寫錯了**——前者剔除該案並記錄覆蓋缺口,後者才成為 major finding。整包結果(通過與否、條件清單、覆蓋缺口、逐案結果、被剔案例)寫進報告的 `_spec_exec`,供稽核;`passed` 是後續自動放行的**硬條件**。

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
   - **real 路**:本地沒有時,經 ToolHub 呼叫 `gitlab__get_file` 抓 GitLab repo 內的 `specs/<code>.md`(`toolbox/gitlab.py:get_file`,REST `repository/files/.../raw`);內容須以 `#` 開頭才算拿到(擋 404 佔位訊息)。
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

`generate_cases` 跑**至多兩次**:輸出解析不出 JSON 或零合法案例時,帶「上一次輸出無法解析」重試一次;LLM 傳輸層例外(連線/逾時)被 catch 住記為 `llm_error` 續跑。兩次皆敗 → 回空 plan + gap「測資生成失敗(LLM 呼叫失敗:…/兩次皆無合法輸出)」,`run_spec_exec` 轉成 **major「執行驗證無法完成(測資生成失敗)」**、`passed: False`——降級,不炸管線。

### 第 2 步:角色二 沙盒執行(確定性程式,非 LLM;`prepare_sql` / `execute_cases`)

**SQL 前處理**(`prepare_sql`):先做參數代入(`:start_date`/`:end_date` → `DATE '2026-06-01'`/`DATE '2026-06-02'` 字面值;`CURRENT_DATE` → 窗結束日)——**必須在 sqlglot 之前**,否則 `:name` 會被改寫;再用 sqlglot 以 postgres 方言 parse,若整句是 `INSERT INTO ... SELECT` 取其 SELECT 部分(通報寫入表不在沙盒 schema 裡);最後 `transpile(postgres → duckdb)`。parse/transpile 失敗都 fallback 用原句(讓 DuckDB 自己報錯,錯誤會被歸到 SQL 側)。

**逐案執行**(`execute_cases`):每個案例開一個全新的 `duckdb.connect(":memory:")`(案例之間零污染):

1. 跑 plan 的所有 `schema_ddl` 建表;
2. 逐列 `INSERT INTO <表名> VALUES (?,?,…)` 參數化灌入該案例的 rows;
3. **這兩步任何例外 = 測資側的錯**(schema/資料造不起來)→ 整包回 `testdata_error`,`run_spec_exec` 轉 major「執行驗證無法完成(測資無法建置)」→ 需人工;
4. 執行 prepared SQL;**這步例外 = SQL 側的錯**(語法/欄位)→ 回 `sql_error`,轉 major「SQL 無法在測資上執行」;
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
  3. 時間窗一律寫半開區間(`>= :start_date AND < :end_date`),參數名固定 `:start_date`/`:end_date`;
  4. 表格式規格 → md 的整理範例在 `examples/sample/RETAIL_M1_spec.md`(原則:只寫核定規格有的、實作細節列「待補」不腦補、跨源不一致以核定規格為準);dbt + Jinja + SQL Server 的規則程式要先 `dbt compile` 展開成純 SQL 再進管線——本包執行層吃 postgres 方言(sqlglot 轉 duckdb)。
- **換模型**:`config/models.yaml` 的 `roles.testgen` / `roles.arbiter` 各自指到任一 profile;小模型常產不出合法測資 JSON(症狀:「測資生成失敗」finding),把 testgen 指回大 profile 即可(README §10)。
- **改 prompt**:`orchestrator/spec_exec.py` 的 `TESTGEN_SYSTEM`(測資契約)與 `ARBITER_SYSTEM`(仲裁原則);執行窗常數 `WIN_START/WIN_END` 也在檔頭。改 TESTGEN 的輸出形狀時,`_shape_check` 必須同步改——形狀檢查是契約的執行者。
- **已知限制**:
  - 一個 MR 只驗**一份 spec、一段 SQL**:`find_spec` 回第一個找得到規格檔的規則碼;SQL 取自最後一個有內容的變更檔。一個 MR 改多條規則時,現版只驗到其一——拆 MR 是目前的正解。
  - 命中判定是「有無輸出列」(`len(rows) > 0`),**通報粒度**(每帳戶每日至多 1 筆)與輸出欄位語意不在執行驗證範圍,靠靜態審查的 H002/規格核對把關。
  - 覆蓋是「LLM 拆出的條件」的覆蓋:若角色一漏拆某條件,形狀檢查看不出「本來該有第 N 條」——上限由 spec 條列品質決定,這也是需求項目要逐條原子化的原因。
  - 方言:postgres → duckdb 轉譯,DuckDB 不支援的方言特性會落在 `sql_error`(major、needs_human),不會誤判通過。
