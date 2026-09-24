# 09-dbt 展開與 macro 反查

## 在幹嘛(一段白話)

規則程式的實際形狀是 dbt 模型:SQL 裡夾著 Jinja 樣板(`{{ ref() }}`、`{{ var() }}`、macro),
關鍵條件(門檻、代碼清單)寫在 macro 與 config 註冊表裡,model 只負責呼叫。

這兩個模組處理由此而來的兩件事:

- **`orchestrator/dbt_render.py`**:把 model 展開成純 T-SQL,讓既有的 rule-base 預掃與
  執行驗證拿得到可解析、可執行的 SQL;同時建立「展開後行號 → 原始檔行號」的對應,
  行內留言才會落在原始檔的正確位置。
- **`orchestrator/dbt_impact.py`**:MR 只改 macro 或 config 註冊表時,反查出呼叫它的
  model(diff 裡看不到那些檔案),並依 model 檔名對應到核定規格。

兩者共用 **`orchestrator/isolation.py`**(子行程隔離:逾時、記憶體上限)。

對應 Issue #7 的四點。接進審查管線的部分以 `config/models.yaml` 的 `dbt.enabled`
控制,**預設關閉**:目前已接上「預掃前展開」與「依檔名對應規格」;macro 反查尚未
接上(見下方「接線狀態」)。

## 防的是什麼

| 失敗模式 | 沒有這個模組時會怎樣 | 對應機制 |
|---|---|---|
| 樣板檔完全沒被檢查 | 預掃在 `{% set %}` 解析失敗 → 整份檔案 0 條規則命中(靠 `enforce_parse` 擋下自動放行,但等於全部要人工看) | 展開成純 SQL 再送進預掃 |
| 改 macro 的 MR 看起來「沒動到 model」 | diff 只有 macro 檔,受影響的 model 不會被審查、也不會做執行驗證 | macro 反查 |
| 留言落在錯的行 | 展開後行數與原始檔不同 | 哨兵標記行號對應 |
| 找不到核定規格 | 真實規格沒有 R 編號,只能靠 MR 文字提到編號 | 依 model 檔名對應規格 |
| **審查機被待審內容攻擊** | 樣板本質是可執行程式:可取得任意程式執行、讀檔、吃光資源 | 沙箱 + 不掛載入器 + 資源上限 + 子行程隔離 |
| **刻意隱藏 macro 呼叫躲過反查** | 反查回報「沒有影響」,MR 逃過審查 | 一律 fail closed(見下方) |

## 怎麼建的

### 一、展開(dbt_render)

自己用 Jinja2 展開,不呼叫 dbt。兩個理由:dbt 的 SQL Server 轉接器即使只編譯也要實際
連線;dbt 執行樣板時**不在沙箱內**,不適合拿來處理待審的 MR 內容。

代價是「會不會跟 dbt 不一樣」,所以驗收方式是**以 dbt 官方編譯結果為標準答案逐字比對**
(`tests/dbt_reference/` → `tests/fixtures/dbt_compiled/`),不拿自己寫的預期值比。

1. **`build_env()`**:建立含 dbt 樁的 `SandboxedEnvironment`。
   - `ref()` / `source()` 展開成 `"<資料庫>"."dbo"."<表>"`(資料庫名由呼叫端傳入,不猜)
   - `config()` 回傳空字串、`var()` 取呼叫端給的值、`execute` 為 True、
     `is_incremental()` 為 False(比照 compile 期)
   - `this`、`env_var` **刻意不定義**:給錯值會靜靜產出錯的 SQL,或把審查機的環境變數
     寫進 SQL;未定義則由 `StrictUndefined` 大聲失敗。`target.database` 在未提供
     database 時同理
   - **所有 macro 檔共用同一個命名空間**:dbt 解析 macro 與檔案順序無關,逐檔各自展開
     會讓「呼叫排在後面檔案的 macro」找不到名稱(跨檔互相呼叫在真實專案很常見)
   - **`adapter.dispatch('x')`**:比照 dbt,先找 `<轉接器>__x`,再退回 `default__x`;
     兩者都沒有就報錯,不猜。轉接器名稱由 `adapter` 參數決定(預設 `sqlserver`,
     與正式環境一致),名稱同樣過識別字白名單。`adapter` 只提供 dispatch,
     其餘(`get_relation`、`execute`…)要連資料庫,展開期不該有,未定義即失敗
   - **macro 目錄可設定**(對應 dbt 的 macro-paths,預設 `macros/`);目錄設定同樣
     不接受絕對路徑、磁碟代號、`..`(跨平台判斷:Windows 上 `專案 / "/etc"` 會跳到
     磁碟根目錄,不能只靠 `is_absolute()`)
2. **展開**:比照 dbt 先去掉原始檔頭尾空白再渲染。
3. **行號對應(哨兵標記)**:在每行插入帶隨機權杖的 SQL 註解標記,再展開一次,從輸出
   讀回標記。**自我驗證**:去掉標記後必須與正常展開逐字相同;不相同就退回 difflib
   近似對應,再不行就標在檔案層(`line: 0`)。原則是**寧可不指行,也不要指錯行**。
   權杖每次隨機產生,MR 內容無法偽造。

### 二、反查與規格對應(dbt_impact)

**只解析,不執行**:用 Jinja 解析器取得語法樹,記錄「誰引用了哪些名字」,再反向走訪。

```
model  mrt_ALPHA      引用 → flag_large …
macro  flag_large     引用 → get_rules
macro  get_rules      引用 → (無)

MR 改了 get_rules 所在的檔案 → 反向走訪 → mrt_ALPHA 受影響
回報呼叫鏈:get_rules(macros/registry/rules.sql) → flag_large → models/mrt_ALPHA.sql
```

納入的引用寫法:直接呼叫、命名空間呼叫、**當成值傳遞**、`adapter.dispatch` 的各轉接器
實作、`config(post_hook="{{ … }}")` 字串中的樣板、`dbt_project.yml` 與 model 目錄 yml
中的 hook。被刪除或改名的 macro 透過**變更前內容**找回原本的呼叫者。

**規格對應**:依 model 檔名找 `specs/<名稱>.md`(預設只去除 `mrt_` 前綴),找不到才用
MR 中的 R 編號當備援。只回傳呼叫端提供的「實際存在的規格清單」裡的路徑,不自行拼路徑
讀檔;多個候選時不自行擇一。

命名慣例(避免與 repo 內的範例混淆):

| 檔案 | 角色 | 會被 `resolve_spec()` 對應嗎 |
|---|---|---|
| `specs/<名稱>.md` | **核定規格**,規格對應與執行驗證實際使用 | 會 |
| `examples/sample/RETAIL_M1_spec.md` | **格式骨架範例**,給人整理規格時照抄結構用 | 不會——不在 `specs/` 底下,且多了 `_spec` 後綴 |

所以 `mrt_RETAIL_M1.sql` 在本 repo 目前對應不到規格,會標為待人工確認。這是預期行為,
不是漏判:範例檔刻意不放進 `specs/`,以免被當成核定規格送進執行驗證。實際專案的規格
檔名慣例確認後,再調整去除前綴的規則。

### 三、fail closed:無法確定時一律交人工

**絕不因為判斷不出來就當成沒有影響。** 下列情況標為 `needs_human`,並保守列出可能受
影響的 model:

| 情況 | 例子 |
|---|---|
| 動態呼叫 | `context[變數]()`、`adapter.dispatch(變數)` |
| hook 在執行期才組出來 | `config(post_hook="{" ~ "{ x() }}")`、`var(...)`、`**kwargs`。**已用 dbt 實測:這類寫法會被還原成 macro 呼叫並執行** |
| 物件被取別名或傳出去 | `{% set c = config %}`、`{% set c = context %}`、`helper(self)` |
| 經由物件內省取用 macro | 私有屬性(`__globals__`)、macro 物件的 `.context`、`attr()` / `map(attribute=)` 等 filter(含位置參數寫法) |
| yml 中的樣板 | 解析失敗、含動態呼叫,或含可能藏住名稱的 YAML 跳脫(`\x`、`\u`、行尾接行) |
| 覆寫 dbt 內建 | `generate_schema_name`、materialization:影響全部 model |
| **找不到任何 model** | 目錄設定錯、呼叫端沒傳 model:不是「沒有影響」,是無法判斷 |
| **變更前內容不齊** | 有給 `base_files` 卻缺了被改的檔案,且未宣告為新增檔 |
| 其他 | 專案 hook、檔案解析失敗、路徑不合法、超過資源上限 |

同時確認**常見的正常寫法不會誤報**(否則每個 macro MR 都得交人工):與範例相同的
`{% set config = {...} %}` 註冊表、寫死的 hook、`config.get`、`set_sql_header(config)`、
`join('_')`、`sort(attribute='name')` 等 22 種,且都確認過 dbt 接受。

## 資安

**這兩個模組處理的是待審的 MR 內容,不是我們自己的程式碼。** 每一項都經過實際攻擊驗證,
並有測試防止被改回去。

### 展開器

| 風險 | 實測(修正前) | 防護 |
|---|---|---|
| 樣板注入取得任意程式執行 | 惡意樣板成功執行 `os.getcwd()` | `SandboxedEnvironment`;要求 **jinja2 ≥ 3.1.6**(更早版本有沙箱逃逸 CVE-2024-56326、CVE-2025-27516),版本不符拒絕展開 |
| 讀取審查機檔案 | `{% include %}` 讀得到 `config/` 下的檔案(可能含 token) | 不掛任何檔案載入器;macro 由程式自行讀入;略過符號連結 |
| 讀取環境變數 | — | 不提供 `env_var()` |
| 覆寫內建繞過防護 | 自訂 `ref` macro 產出 `x"; DELETE FROM t; --`;自訂 `var` 蓋掉呼叫端的值 | macro 不得與 dbt / Jinja 內建同名;macro 檔的模組層 `{% set %}` 不進共用命名空間 |
| 識別字注入 | 原始碼看似無害的 `ref('...')`,展開後藏了另一段 SQL | 表名只接受英數與底線 |
| 指向錯誤的關聯 | `ref('t', version=2)` 在 dbt 是另一張表,我們忽略參數 | 拒絕展開(沒有 manifest 可解析版本) |
| 偽造行號標記 | MR 放入仿冒標記,行號對應降級 | 標記使用每次隨機產生的權杖 |
| 資訊洩漏 | 錯誤訊息帶出審查機完整路徑;`{{ ref }}` 輸出記憶體位址 | 錯誤訊息不含路徑、不含控制字元、有長度上限;不輸出函式物件 |
| 樣板改到呼叫端資料 | `{% do var('x').append(...) %}` 改到呼叫端的 list | 變數每次取用都給複本;`target` 唯讀 |
| ReDoS | 內部正規表達式是平方時間,40 萬字元約 40 分鐘 | 改為線性時間掃描 |
| 資源耗盡 | `((9**256)**256)**256`、超大字串、無窮迴圈 | 原始碼 / macro / 輸出 / 整數運算上限;子行程隔離 |

### 反查

| 風險 | 防護 |
|---|---|
| 分析時執行到惡意樣板 | 只解析不渲染;解析器同樣用沙箱、不掛載入器;測試以 AST 檢查模組內沒有讀檔、渲染或執行的呼叫 |
| 惡意路徑 | 路徑白名單:拒絕絕對路徑、`..`、反斜線、冒號、控制字元與雙向文字控制字元 |
| 刻意隱藏 macro 呼叫 | 見上方 fail closed;以 48 種規避寫法與 8 種 yml 規避寫法測試 |
| 故意不寫結束標籤讓解析卡住 | 自訂標籤解析器在檔案結尾必定停止 |
| 資源耗盡 | 檔案數、單檔與總字元數(含變更前內容)、語法樹節點數、字串內樣板巢狀深度上限,超過即 fail closed;走訪不用遞迴,名稱掃描線性時間 |
| 輸出被灌爆 / 帶出控制字元 | 說明文字數量與長度有上限,只含已過白名單的路徑與識別字 |

### 共用與供應鏈

- **子行程隔離**(`isolation.py`):逾時強制終止;Linux 上限制記憶體;子行程異常結束、
  被系統終止、拋例外、**啟動失敗**一律收斂成失敗結果,訊息不帶輸入內容
- dbt 內建 macro 名稱清單由 `dbt parse` 產生,**只收錄名稱,不含任何程式碼**;來源套件
  版本與授權取自套件中繼資料(dbt-core、dbt-adapters 為 Apache-2.0,dbt-sqlserver 為 MIT)
- 產生標準答案的腳本只傳白名單內的環境變數給 dbt,並關閉 dbt 的匿名使用統計
- 對照專案的連線設定預設**驗證伺服器憑證**,不信任自簽憑證

### 已知限制

**本模組是審查輔助,不是資安邊界。** dbt 執行樣板時不在沙箱內,刻意以 Python 物件內省
在多層資料流中隱藏呼叫(例如把 macro 當參數傳給另一個 macro,再以執行期組出的名稱取
屬性),靜態分析無法完全判定。常見與低成本的手法都已擋下;真正的防線是執行沙盒與資安
審查。

## 對決策的影響

`dbt.enabled` 關閉時(預設):含樣板的檔案維持現行行為——預掃解析失敗 →
`enforce_parse` 補 major → 不得自動放行。

### 接線狀態(`dbt.enabled` 開啟時)

| 項目 | 狀態 | 行為 |
|---|---|---|
| 預掃前展開 | 已接上 | 含樣板的檔案先以 `render_model_isolated()` 展開再交給規則層;展開失敗則原樣退回,走既有 `parse_error` → `enforce_parse` 路徑。lint 刻意仍吃原始文字(帶行號,基準要與 diff 一致) |
| 依檔名對應規格 | 已接上 | 完全沒有 R 編號時,先查本機 `specs/`、再逐一探測 GitLab 的候選路徑 |
| `ref()`/`source()` 精確度提醒 | 已接上 | 預掃結果帶 `dbt_render_notice` |
| 執行驗證(沙盒) | **不支援** | SQL 是樣板時不送進沙盒,回報「尚未支援」major、交人工——原樣執行只會得到語法錯誤,會錯誤地指控程式有誤 |
| macro 反查 | **未接上** | 需要整個專案的檔案,目前沒有列 GitLab 目錄的工具 |

資料庫名稱以環境變數 `SEGCRA_DBT_DATABASE` 提供,**不寫進任何進版控的檔案**
(repo 是公開的);未設定時,用到 `ref()`/`source()` 的 model 一律展開失敗。
目前沒有 macro 目錄可餵,呼叫到專案自訂 macro 的 model 也會展開失敗(fail closed)。

呼叫端約定(接線時必須遵守):

1. 待審的 MR 內容一律使用 `render_model_isolated()` 與 `analyze_macro_impact_isolated()`
2. `ok=False` 或 `needs_human=True` 一律交人工;反查失敗時 `affected_models` 為空,但
   `all_models_possibly_affected=True`
3. 必須傳入 `base_files`(所有被修改檔案的變更前內容)與 `added_paths`(本次新增的檔案)
4. 檔案內容以字典傳入,不要傳由 MR 資料拼出來的路徑
5. 呼叫端要有 `if __name__ == "__main__":` 保護(multiprocessing spawn 的要求)

## 怎麼驗證它在動

確定性測試,不呼叫 LLM、不連資料庫;Windows、Linux 與全新 clone 皆通過,CI 在 Linux 上跑。

| 方法 | 內容 |
|---|---|
| 與 dbt 逐字對照 | 展開結果比 `dbt compile`;反查結果比 `dbt parse` 的依賴 |
| 隨機專案差異測試 | 固定種子產生的 dbt 專案,每個都讓 dbt 實際解析過(`tests/fixtures/dbt_manifest/random_projects.json`);`test_random_project_matches_dbt` 逐一改動每個 macro 檔,dbt 判定受影響的 model 必須全部判為確定受影響(不需安裝 dbt,CI 直接跑) |
| 攻擊與規避測試 | 沙箱逃逸(含兩個 CVE 手法)、讀檔、注入、覆寫內建、偽造標記、資訊洩漏、資源耗盡、各種隱藏 macro 呼叫的寫法 |
| 不誤報測試 | 常見正常寫法不可被判為不確定 |
| 隨機輸入測試 | 固定種子產生大量怪異樣板,驗證不丟例外、行號不越界、輸出有上限、結果可重現 |
| **突變測試** | `tests/tools/mutate.py`:把每道防線逐一改壞,確認測試會失敗。測試全過不代表測試有效,這一步驗證的是測試本身 |
| 第三方掃描 | bandit(產品程式碼)、pip-audit(依賴) |

標準答案平常不需要重新產生,只有對照專案變動或升級 dbt 時才執行(`--dbt` 要給絕對路徑):

```bash
python tests/dbt_reference/generate.py --dbt <dbt 執行檔> --target sqlserver
python tests/dbt_impact_reference/generate.py --dbt <dbt 執行檔>
python tests/dbt_impact_reference/random_projects.py --dbt <dbt 執行檔>
```

## 後續

- **macro 反查接進管線**:需要列 GitLab 目錄(或以 `/repository/archive` 依 commit
  一次取回 `models/`、`macros/`)的工具,並先定好下載量上限
- **展開結果尚未在 MS SQL 沙盒實際執行過**(目前只驗證了展開字串與 `dbt compile`
  逐字一致)。要支援樣板的執行驗證,需把 `ref()`/`source()` 對應到沙盒每次建立的
  資料庫,且 model 引用的上游表與規格建立的測資表名稱不同,需要處理
- `{% if is_incremental() %}` 內的 SQL 不會被掃到(比照 compile 固定為 False),接線時
  需另以 True 再展開一次
- `source()` 尚未讀取 `sources.yml`;`ref()` 尚未套用 alias 與自訂 `generate_schema_name`;
  也不檢查 model 是否存在(接 manifest 時一起處理)
- 反查以檔案為單位;不分析第三方套件內的 macro;目錄由呼叫端指定
- `invocation_id` 為固定值,樣板若輸出它,結果會與 dbt compile 不同
- 左側修剪標籤(`{{-`)那行輸出多行時,行號會對到上一行
- 待確認:規格檔名慣例(目前只去除 `mrt_` 前綴);earlyjob model 應歸屬主規則的規格,
  確認前會交人工
