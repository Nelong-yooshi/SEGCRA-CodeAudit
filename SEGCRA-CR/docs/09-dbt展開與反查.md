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
   - `ref()` / `source()` 展開成 `"<資料庫>"."<綱要>"."<表>"`(資料庫名由呼叫端傳入,不猜);
     沒有專案資訊時綱要固定為 `dbo`,有專案資訊時見下方「`ref()` / `source()` 的精確度」
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
   以 `{{-` / `{%-` / `{#-` 開頭的行會吃掉前面的換行,標記不能插在它前面(會擋住
   空白控制),改把左修剪拆成一個只輸出標記的 `{{- '標記' }}`,後面接去掉 `-` 的原
   標記:輸出只多了標記,這行(例如 `{{- 巨集() }}` 展開出的多行)仍有自己的行號。
   空白控制把幾行接成一個輸出行時,這行歸給內容開頭所在的原始行。
4. **`ref()` / `source()` 的精確度(`dbt_relations`)**:呼叫端從專案檔案整理出
   `DbtProjectInfo`(`project_info_from_files()`),交給 `render_model(project=...)`。

   | 情況 | 沒給專案資訊 | 有給專案資訊 |
   |---|---|---|
   | 找得到的 `ref()` | 以 model 名稱為表名,帶提醒 | 照 dbt 規則展開,不帶提醒 |
   | 找得到的 `source()` | 綱要固定 `dbo`,帶提醒 | 用 `sources.yml` 的 schema / identifier(schema 預設 = 來源名),不帶提醒 |
   | 確定不存在 | 照常展開,帶提醒 | 展開失敗(dbt compile 一樣會失敗) |
   | 無法確定 | — | 以慣例展開,帶「表名無法確定」提醒 |

   - **「確定不存在」的條件**(缺一就只提醒、不判失敗,寧可保留提醒也不冤枉開發者):
     沒用 dbt 套件(套件的 model / source 不在 repo 裡)、呼叫端提供了 seed / snapshot /
     Python model 的名單(打包只收 `.sql` / `.yml` 的內容,看不到 `.csv` 與 `.py`;名單要從
     檔名清單取得,snapshot 的名稱寫在 `{% snapshot %}` 區塊上)、所有屬性檔都解析得了、呼叫端
     以 `root_files_checked=True` 明確確認已檢查專案根目錄的 `packages.yml` /
     `dependencies.yml`(沒傳進來不等於沒有)
   - **只偵測、不模擬**:alias、自訂 schema、snapshot 的 `target_schema` / `target_database`
     (dbt 用它們覆蓋 schema / database;只看屬性檔與 `config()` 的參數。`dbt_project.yml` 的
     `+target_schema` 不讓整個專案無法確定:snapshot 本來就一律無法確定,見下一點)、
     `generate_*_name`、樣板值、ephemeral
     (被引用時展開成 CTE,不是表名)、停用(`enabled` 不是 true,被引用時 dbt 編譯失敗)、
     model 版本、同名的 model(dbt 編譯失敗;model 與 seed / snapshot 同名也算)、
     Python model(內容不讀)、macro 裡呼叫的 `config()`(作用在呼叫它的 model 上,
     看不出是哪些,整個專案都無法確定);屬性檔的 `seeds:` / `snapshots:` 區段同樣檢查。
     有這類設定就保留提醒;規則層檢查語法結構、不看表名,模擬的成本高、價值低
   - **呼叫端提供的名稱(seed / snapshot / Python model)只代表存在,表名一律無法確定**
     (#19 review):只有名稱、看不到內容,而 snapshot 的 `target_schema`、Python model 的
     `dbt.config()` 都寫在內容裡。`ref()` 得到名單上沒有的名稱時照樣判成不存在
   - **model 檔內 `config()` 的偵測用白名單**:每個參數都必須是「名稱 = 常值」(字串、數字、
     布林、字串清單),名稱不是 alias / schema / database,`materialized` 也不是 ephemeral,
     才算確定。字典寫法、`**` 展開、變數、`var()`、樣板運算、沒有右括號,一律算可能改名——
     看不懂的寫法會自動往「無法確定」靠,不會漏判。屬性檔與 `dbt_project.yml` 的
     `materialized` 為 ephemeral 或樣板值時同樣無法確定
   - **`config.set()` 一律算可能改名**(#19 review):dbt 的 `config.set(name, value)` 等同
     `config({name: value})`,寫在 model 或 macro 裡都一樣。連同 `config['set']`、
     `config|attr(...)` 一起認,不看參數。**只認這幾種寫法,不是看到 config 這個字就算**:
     註解、欄位名、名為 config 的區域變數(範例專案的註冊表 macro 就這樣寫)都很常見,
     全部算進去會讓整個專案都帶提醒。已知偵測不到:先把 `config` 指給別的變數再呼叫
     它的 `set`(`{% set c = config %}` 之後 `c.set(...)`),會被當成確定
   - **資料庫名一律用呼叫端給的約定假名**,不採用 `sources.yml` 宣告的 database:
     展開結果會出現在審查報告裡,不放正式資料庫名;規則也不看資料庫名
   - **macro 檔最外層的 `ref()` 不判定**:dbt 不會執行那段程式碼,在那裡失敗會冤枉開發者
   - 專案檔案來自待審的 commit:YAML 一律用安全的載入器(有 libyaml 時用 C 版,行為相同、
     約快 4 倍);輸入形狀不對就回傳「全部無法確定」;`sources.yml` 宣告的名稱拼進 SQL 前過
     識別字白名單;YAML 別名可以把「來源 × 表」放大成平方級,所有屬性檔合計最多處理
     `MAX_PROPERTY_ENTRIES`(50,000)筆,超過即無法確定;`config()` 的掃描是線性時間
   - 接線時:屬性檔的解析放進子行程並設逾時(純 Python 解析 50 MB 最壞約一分鐘),
     屬性檔設單檔大小上限

   以 `tests/dbt_reference` 驗證:有專案資訊時,與 `dbt compile` 逐字一致且不帶提醒。

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
| 預掃前展開 | 已接上 | 含樣板的檔案先以 `render_model_isolated()` 展開再交給規則層;展開失敗則原樣退回,由 `enforce_dbt_render_failure()` 一律補 major(見下方)。lint 刻意仍吃原始文字(帶行號,基準要與 diff 一致) |
| 依檔名對應規格 | 已接上 | 完全沒有 R 編號時,先查本機 `specs/`、再逐一探測 GitLab 的候選路徑 |
| `ref()`/`source()` 精確度提醒 | 已接上 | 預掃結果帶 `dbt_render_notice`,後處理由 `enforce_dbt_notice()` 確定性補成 info finding(正式路徑與 dry-run 皆同) |
| 執行驗證(沙盒) | **不支援** | SQL 是樣板時不送進沙盒,回報「尚未支援」major、交人工——原樣執行只會得到語法錯誤,會錯誤地指控程式有誤。改到 dbt 而執行驗證卻通過時,`enforce_dbt_unexecuted()` 補 major |
| 專案的 macro 與專案資訊 | 已接上(真實 GitLab 模式) | 依 MR 的 head commit 取回專案;取不回來一律交人工。mock 模式要在 fixture 目錄放專案才有(見「接進審查管線」),沒放時呼叫專案 macro 的 model 展開失敗 |
| macro 反查 | 已接上(真實 GitLab 模式) | MR 改到 macro、屬性檔或專案設定檔時反查受影響的 model,比較變更前後的規則命中,由 `enforce_dbt_impact()` 補成一條 finding |

**展開失敗一律補 major**(#16 review):展開失敗時規則層掃的是未展開的原文。以前只靠
「原文會讓 sqlglot 解析失敗 → `enforce_parse`」間接揭露,但 model 主體就是一句 macro
呼叫(`{{ purge_staging() }}`)時,原文解析得過、0 條規則命中,展開後卻是一條 DELETE
無 WHERE 的 blocker。拿不到 macro 目錄時這是常態。同一個檔案若也解析失敗,只留展開
失敗這一條。

**不做「標記只在註解/字串裡就不報」的豁免**:SQL 註解擋不住 Jinja 執行(dbt 本身的
已知陷阱),而 macro 的輸出可以帶換行結束行註解、帶 `*/` 結束區塊註解、帶引號結束字串
或引號識別字,之後就是任意 SQL。四種位置都實測過能變成 R001 blocker
(`test_macro_output_escapes_comments_and_strings`)。展開失敗 = 不知道輸出是什麼,
無法證明它跳不出去,所以不看標記的位置。

資料庫名稱以環境變數提供,**固定使用假名** `SEGCRA_DBT_DATABASE=DBT_PLACEHOLDER`
(#14 review 同意):它只用來把 `ref()`/`source()` 展開成完整表名給規則層掃描,
不會連線;`toolbox/sqltools.py` 目前沒有任何規則看資料庫名,用假名不影響判斷。
展開後的 SQL 片段會出現在審查結果裡,所以不放正式資料庫名。假名必須固定不變,
golden set 的標準答案才不會跟著變;約定值寫在 `orchestrator/config.py` 的
`DBT_DATABASE_PLACEHOLDER`,改名時要同步更新 `config/models.yaml` 的註解與本文件
(有測試確保三處一致)。

**名稱正確性檢查**(#14 review):漏設或拼錯時,展開不會報錯,只會產出錯的表名,
報告看起來卻完全正常。所以 `dbt.enabled` 開啟時,資料庫名必須**完全等於**約定值:

- 載入設定時檢查,不符(含未設定、大小寫不同)就報錯,審查不會帶著錯的設定啟動
- 預掃展開前再檢查一次(呼叫端可能不經 `load_config()` 直接傳入設定),不符就不展開,
  以展開失敗處理(補 major、交人工)
- 兩處的錯誤訊息都不回顯實際的值(填進來的可能正是不該外流的正式資料庫名)

日後若改用正式資料庫名(例如要在持久化的測試資料庫上實際執行展開結果),只改環境
變數、仍不寫進進版控的檔案;約定值的檢查要改成另一種正確性驗證(例如確認資料庫
確實存在),不能直接拿掉。

`ref()`/`source()` 精確度提醒由 `enforce_dbt_notice()` 在後處理鏈上**確定性**補成
一條 info finding(#14 review):不靠模型轉述;info 不影響決策,但一定會出現在報告裡。
沒有取回專案時(mock 模式而 fixture 沒有放專案、專案外的檔案)沒有 macro 可用,呼叫到專案自訂 macro 的 model 會展開失敗(fail closed)。

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

## 取回 dbt 專案(#15)

審查機要能展開呼叫自訂 macro 的 model、做 macro 反查,必須拿到整個專案的 models / macros。
以 GitLab `/repository/archive` 依 MR 的 commit 一次取回,分四個模組:

| 模組 | 做什麼 | 主要防線 |
|---|---|---|
| `toolbox/gitlab.py` 的 `download_archive()` | 下載 tar.gz | 只用唯讀的 `GITLAB_READ_TOKEN`(不退回、不可與 `GITLAB_TOKEN` 相同);只接受完整 commit 編號;本機以外必須 https;不讀代理設定;不跟隨轉址;不做傳輸層解壓;大小與時間上限;**不在模型可呼叫的工具清單內** |
| `orchestrator/archive.py` 的 `extract_archive()` | 解開成 {路徑: 內容};另可只列指定目錄的檔名(不解出) | 邊解壓邊計數(壓縮炸彈);**所有成員**檢查單一頂層目錄、絕對路徑、`..`、`.` 或空段落、重複(要收內容的目錄內另含大小寫與 Unicode 正規化),指定目錄的上層不能是連結或檔案(否則目錄看起來是空的);**要收內容的目錄內**不能有連結或特殊檔;**會解出的檔案**另外檢查控制字元、反斜線、冒號、Windows 保留名稱——不解出的成員(目錄外,或 `.md`、`.csv`、`.py` 等)不讓整包失敗;只收 `.sql` / `.yml` / `.yaml`;`filter="data"`;權限 700 的暫存目錄、讀完即刪 |
| `orchestrator/dbt_project.py` 的 `load_dbt_project()` | 讀 `dbt_project.yml` 的目錄設定(model / macro / analysis / test / seed / snapshot),取回這些目錄的內容、model / seed / snapshot 目錄的檔名清單,以及專案根目錄的 `packages.yml` / `dependencies.yml`(`root_files`,判斷有沒有用 dbt 套件);`extra_ref_names()` 從同一包算出 seed / Python model / snapshot 的名稱(看不懂就回 `None`) | 同一包讀設定與檔案;目錄設定當不可信內容檢查(`read_layout()` 與取回專案共用同一套檢查);記憶體快取(以 commit 為鍵、有筆數與總量上限、只收成功結果、進出都複製) |
| `orchestrator/dbt_mr_impact.py` 的 `analyze_mr_macro_impact()` | 把 MR(變更清單 + head / base commit)轉成 macro 反查的輸入並執行 | 路徑換成相對專案根目錄;新增檔以 head / base 兩個專案比對判斷(不依賴 GitLab 的旗標),改名與刪除的舊路徑一併列入;反查看得到 `dbt_project.yml` 的 hook;變更清單被截斷、路徑看不懂、取回失敗 → 全部 model 可能受影響;改到專案設定檔或兩邊目錄設定不同 → 全部 model 可能受影響;不在 dbt 目錄裡的檔案(CI 設定、一般的 SQL)不列入;seed / snapshot / analysis / test 目錄依專案的設定判斷(改名的目錄跟預設的 `seeds/` 等一樣處理,改到裡面的檔案交人工);snapshot 也交給反查(含 snapshot 目錄裡的屬性檔),呼叫到被改的 macro(直接或間接)、或看不懂時交人工;反查一律在子行程 |
| `toolbox/gitlab.py` 的 `read_file_at()` | 讀某個 commit 的單一檔案(取回整個專案之前,先讀 `dbt_project.yml` 判斷哪些檔案屬於 dbt) | 與 `download_archive()` 同一套請求程式:只用唯讀 token、不跟隨轉址、不讀代理設定、大小與時間上限;指定 commit,不讀分支;**不在模型可呼叫的工具清單內** |

任何一步失敗都是整包失敗(`ok=False`),**不會當成「專案沒有 macro」**。

**真實 GitLab 實測**(2026-10-01,測試用 GitLab、只含假資料的專案;project access token,
角色 Reporter、只勾 `read_api`):

| 項目 | 結果 |
|---|---|
| 依 commit 打包下載整個 repo | 成功;單一頂層目錄 `<專案>-<sha>-<sha>/` |
| 指定 `path=dbt_sub` | 成功;頂層目錄變成 `<專案>-<sha>-<sha>-dbt_sub/`,**底下保留 `dbt_sub/` 這一層** |
| `load_dbt_project()`,專案在 repo 根目錄 | 成功;只取回 models / macros 的 `.sql` / `.yml`(`README.md`、`seeds/*.csv` 排除) |
| `load_dbt_project()`,專案在 `dbt_sub/` | 成功;路徑換算成相對專案根目錄 |
| 不存在的 commit | HTTP 404,乾淨失敗並附處理提示 |
| 同一個 token 只勾 `read_repository`(先前實測) | 讀單檔 200,打包與列目錄 403 |

#20 review 修正後重測(2026-10-04,同一個測試專案新增一個 commit;token 條件同上,
另建新的一把):

| 項目 | 結果 |
|---|---|
| repo 裡有 `docs/aux.md`、`notes/con.txt`(review 實測會讓整包失敗的檔名) | 取回根目錄的專案**成功**;兩個檔案在 dbt 目錄外,不解出 |
| `models/props.yaml` | 有收進來 |
| seed、Python model、snapshot(`.sql` 區塊與 yml) | snapshot 的 `.sql` 與 yml 收內容;`.csv`、`.py` 只列檔名(清單含 `seeds/sample.csv`、`models/py_score.py`);`extra_ref_names()` 得到 `py_score`、`sample`、`snap_dim`、`snap_sample` |
| `load_dbt_project()`,專案在 `dbt_sub/` | 成功 |
| 不存在的 commit | HTTP 404,乾淨失敗 |

正式環境的條件(#15 review 回覆):正式的 dbt 專案**沒有用 dbt 套件**(共用邏輯都是自己寫的
macro);正式環境的主機**不連外網**,審查流程裡不能有任何執行時才從網路下載的步驟(包括
`dbt deps`);正式環境用 dbt-sqlserver,預設 schema 是 `dbo`。含 dbt 樣板的 MR 這一階段**不需要
自動放行**:接線完成就是終點,最後由人決定(dbt 的執行驗證另排,前提是測資生成在真實規模下
穩定、測試資料的持久化方式定案)。

已知限制:

- **dbt 套件的 macro 拿不到**:`packages.yml` 宣告的套件(例如 `dbt_utils`)由 `dbt deps`
  下載到 `dbt_packages/`,不在 repo 裡。呼叫套件 macro 的 model 仍會展開失敗(以展開失敗
  揭露,不會靜默放行)。正式專案沒有用套件(見上)
- 反查不展開 snapshot、不比較前後:snapshot 呼叫到被改的 macro(直接或間接)、或看不懂時交人工;沒有呼叫的不受影響
- `ref()` / `source()` 的專案資訊(#19)只讀 model 目錄的屬性檔;seed / snapshot 目錄的屬性檔
  (可能設 alias)與 snapshot 區塊的 config 沒有讀,所以接線時 seed / snapshot / Python model
  一律標成無法確定。`extra_ref_names()` 與屬性檔的解析都在有逾時的子行程裡做(見「接進審查管線」)
- 權限:唯讀 token 需要 `read_api`(仍然只能讀;見 README),`read_repository` 不夠(見上表)
- 解壓與讀 `dbt_project.yml` 在審查行程內執行(不在子行程),由各項大小上限約束記憶體

## 接進審查管線(#15)

`dbt.enabled` 開啟、MR 改到 dbt 的檔案、而且拿得到專案時才會動作。dbt 的檔案是 `dbt_project.yml` 設定的
model / macro / analysis / test / seed / snapshot 目錄裡的檔案,與專案根目錄的設定檔(`dbt_project.yml`、
`packages.yml`、`dependencies.yml`、`selectors.yml`);專案目錄(`dbt.project_dir`,預設 repo 根目錄)
裡的其他檔案(CI 設定、一般的 SQL)不算。取回整個專案之前,先讀 head commit 的 `dbt_project.yml`
判斷:只改到其他檔案的 MR 不下載專案。讀不到時退回「專案目錄裡的 `.sql` / `.yml` / `.yaml`」
(接下來取回專案也會失敗、交人工);head 沒有 `dbt_project.yml` 而 MR 改到這些檔案時算取回失敗。專案的來源:真實 GitLab 模式是打包下載;mock 模式是 fixture 目錄裡
放的專案(見下方「開啟前的條件」後的說明),沒有放就不適用。沒開 dbt 時行為與接線前相同;開了 dbt 而
不適用(mock 模式沒有放專案、MR 沒改到專案裡的這些檔案)時,只有最後一步 `enforce_dbt_unexecuted` 仍會檢查。

```
review_mr
 ├─ get_mr_diff            sha、base_sha / head_sha、變更清單、truncated
 ├─ prepare_dbt_context    先讀 head 的 dbt_project.yml 判斷哪些是 dbt 的檔案;依 head commit 取回專案;
 │                         整理專案資訊(子行程);
 │                         MR 改到 model 以外的專案檔 → macro 反查 + 取回 base commit 的專案
 ├─ prescan                專案裡的樣板:用 head 的完整內容 + 專案的 macro + 專案資訊展開
 │                         專案的 macro 檔:不當成 model 展開,規則跑在改到的內容上
 ├─ 注入掃描               另外掃取回的完整內容與展開後的 SQL
 ├─ find_spec              MR 自己對不到規格、而受影響的 model 都對到同一份 → 用那一份
 ├─ check_affected_models  受影響、但不在變更清單裡的 model:變更前後各展開一次,比較規則命中
 ├─ …模型審查…
 ├─ enforce_dbt_impact     影響範圍與檢查結果 → 一條 finding
 ├─ enforce_dbt_config_change 只改設定(config()、dbt_project.yml、屬性檔)→ 每個檔案一條(見 04 篇)
 ├─ run_spec_exec          樣板檔帶上完整內容再交給它(如實回報「尚未支援」)
 └─ enforce_dbt_unexecuted 改到 dbt 而執行驗證卻通過 → major
```

| 模組 | 做什麼 |
|---|---|
| `orchestrator/dbt_context.py` | 取回專案、判斷變更檔在不在專案裡、專案資訊、macro 反查結果、base commit 的專案 |
| `orchestrator/dbt_affected.py` | 檢查受影響的 model、對應規格、整理成 finding |
| `orchestrator/dbt_render.py` 的 `macro_files` | 展開器直接吃取回的檔案(不落地),載入邏輯與順序和從磁碟讀相同 |

**取不回來就是取不回來。** token 未設、權限不足、GitLab 連不上、專案設定看不懂、屬性檔解析逾時:
專案裡被改到的 `.sql`(與其他確定是樣板的檔案)一律記成展開失敗 → major;macro 反查當成
「全部 model 可能受影響」→ major。不退回「沒有 macro」繼續展開。

**展開用的內容。** 真實 GitLab 模式的 diff 只有新增的行。專案裡的檔案只要 head 的完整內容是樣板,
就用完整內容展開(改到的那幾行沒有樣板標記,不代表整個檔案沒有)。不是樣板的 SQL 檔維持原本的
來源(完整內容屬於 #22 的範圍)。專案裡有同名的 macro 時算展開失敗:dbt 會編譯失敗,擇一展開的
結果不可信。

**專案資訊。** `ref()` / `source()` 依取回的專案驗證(見「一、展開」第 4 點)。seed、snapshot、
Python model 只知道存在:snapshot 的 `target_schema` / `alias` 寫在區塊的 config 裡、seed 的
alias 寫在 seed 目錄的屬性檔裡,都沒有讀,所以一律標成無法確定(帶提醒),不用預設慣例當成確定的
表名。屬性檔是待審 repo 的內容,解析放在有逾時與記憶體上限的子行程裡。

**受影響的 model(MR 改到 macro、屬性檔、專案設定檔時)。**

- 不放進預掃、也不放進模型的 prompt:它們不在 diff 裡,放進預掃會讓每個檢核點各補一條
  「模型未回應」,放進 prompt 會佔掉預算並多一個注入的入口
- 以變更前、變更後的專案各展開一次,**只回報變更後多出來的規則命中**。比較的是**每一句的規則命中數**
  有沒有增加(依語句在檔案裡的順序對應),不比語句的文字:macro 只把一個值從 1 改成 2,同一句原本就有
  的命中不能因為文字變了就當成新的。兩邊切出來的語句數量不同時,退回比較整個 model 同一條規則的
  命中數;文字層的規則與對不到語句的命中只比個數。原本就有的命中不重報(否則每個改到 macro 的 MR 都會被既有的寫法擋下)。
  命中寫出在展開後的第幾句
- 整理成一條 finding(`enforce_dbt_impact`),嚴重度:

  | 嚴重度 | 條件 |
  |---|---|
  | major | 範圍不確定(反查標為不確定、全部 model 可能受影響);有 model 沒檢查到或沒查規格;變更後展開失敗或無法解析;多出 blocker 或 major 級的命中;無法與變更前比較而有命中 |
  | minor | 只多出 minor 級的命中 |
  | info | 有受影響的 model 或沒人呼叫的 macro,檢查不出問題(清單給人看) |

  由前後比較推論出來的結果最多給 major,不給 blocker:比較本身可能出錯(語句對不上、展開方式不同),
  不拿它直接擋下 MR,交人工確認。多出的命中、展開失敗、沒有檢查、沒查規格的清單不截斷

- 上限:每個 MR 最多檢查 50 個受影響的 model、整段最多 120 秒、規格最多查 20 個。超過的列為
  「沒有檢查」或「沒有查規格」→ major。剩下的時間傳進每一次展開(逾時取 30 秒與剩下時間的較小者),
  展開被剩下的時間截斷時記成沒有檢查(不是展開失敗);查規格(真實 GitLab 模式會發請求)放在背景
  執行緒裡、只等剩下的時間。反查判定全部 model 可能受影響時不逐一展開。時間上限與機器負載有關,同一個
  MR 在忙碌時可能多出「沒有檢查」的項目;方向是保守的(major)
- 規格:每個受影響的 model 依檔名對應規格並列在 finding 裡。MR 自己對不到規格(只改了 macro)、
  而受影響的 model **全部檢查過、全部對到同一份**時,用那一份給模型審查

**dbt 的變更不會被當成「執行驗證通過」。** dbt 樣板不送沙盒,所以這種 MR 的執行驗證應該是
「尚未支援」。但 `run_spec_exec` 是看檔案內容有沒有樣板標記來決定的,真實 GitLab 模式下只改了
macro 或 model 裡幾行純 SQL 時,片段沒有標記,會被當成一般 SQL 拿去執行。兩層處理:

1. 交給 `run_spec_exec` 的 MR 副本裡,專案的樣板檔帶上 head 的完整內容 → 既有邏輯如實回報
   「尚未支援」,也不會白花一次測資生成
2. `enforce_dbt_unexecuted`:MR 改到 dbt 的東西(樣板、macro、屬性檔、專案設定檔、或專案取不回來),
   而執行驗證的結果是通過 → 補 major(被執行的是同一個 MR 裡別的內容)。這一層在 mock 模式
   也生效:沒有取回專案時,改看手上的內容有沒有樣板標記(樣板檔與純 SQL 檔混在同一個 MR、
   通過的是純 SQL 檔)。沒開 dbt 時不介入

檢查受影響的 model 時若發生非預期的錯誤,審查不中斷:結果收斂成「全部 model 可能受影響」→ major,
只留錯誤的類型、不回顯內容。

**開啟 `dbt.enabled` 之前的條件**(#14 review 列的四項)與目前的狀態:

| 條件 | 狀態 |
|---|---|
| `relation_notice` 改成確定性揭露 | 完成(#14):`enforce_dbt_notice` |
| macro 目錄的取檔方式決定並實作 | 完成:依 commit 打包下載(#20),接進管線(本節) |
| 進入點有 `if __name__ == "__main__":` 保護 | `demo.py` 與 `scripts/webhook_server.py` 都有,由 `tests/test_entrypoint_guard.py` 鎖住。展開、macro 反查、專案資訊都開子行程(spawn);進入點少了保護時,子行程會把整支程式從頭重跑一次。實際踩到時的結果是專案資訊整理失敗 → 當成取不回專案 → 交人工 |
| 資料庫假名確定並寫進文件,設錯要擋下 | 完成(#16):固定為 `DBT_PLACEHOLDER`,載入設定與展開前各檢查一次 |

開啟與回復的步驟、以及不接 GitLab 就能跑的範例,見 README「開啟 dbt 接線」與 `examples/dbt_wiring/`。
mock 模式下,fixture 目錄若放了 MR 那個 commit 的專案(`<FIXTURES_DIR>/dbt_archives/<commit 編號>/`),
接線會從那裡取得專案——打包成 GitLab 的形狀後交給同一個 `load_dbt_project`,解壓與檢查走同一條路;
沒有放就不適用。預設的 `fixtures/` 與 golden set 都沒有放,行為不變。

**真實 GitLab 實測**(2026-10-12,測試用 GitLab、只含假資料的專案)。只讀取:不呼叫模型、不寫回
GitLab;執行的是 `review_mr` 在呼叫模型之前的部分(取 MR → 取回專案 → 預掃 → 反查 → 檢查受影響的
model)與確定性補報。讀 MR 與打包下載各用一把 project access token,都是角色 Reporter、只勾
`read_api`。測試專案的 dbt 專案在 repo 根目錄,有 1 個 macro、3 個 model(含 1 個 Python model)、
1 個 seed、2 個 snapshot;三個 MR 各改一個檔案,目標分支相同。

| 情況 | 結果 |
|---|---|
| MR 改 model(diff 只有一行純 SQL、沒有樣板標記) | 下載 1 次(head)。送去跑規則的是**用完整內容與專案的 macro 展開後**的整句 SQL,不是 diff 的那一行;`ref()` 依專案資訊展開;不反查、沒有補報;交給執行驗證的內容帶上完整檔案 |
| MR 改 macro(讓呼叫它的 model 多出 `NOT IN` 子查詢) | 下載 2 次(head、base)。macro 檔不當成 model 展開;反查到 1 個確定受影響的 model;變更前後各展開一次,多出 `R003 major`;補一條 major,列出該 model、沒有對應到規格、以及專案有 snapshot 未分析 |
| MR 改 `dbt_project.yml` | 下載 2 次。反查判定全部 model 可能受影響;不逐一展開;補 major |
| 沒有設定 `GITLAB_READ_TOKEN` | 取回失敗;被改到的 macro 檔記成展開失敗;反查當成全部 model 可能受影響;兩條 major |
| `GITLAB_READ_TOKEN` 與 `GITLAB_TOKEN` 是同一把 | 拒絕下載(不退回用另一把);三個 MR 都交人工 |

以上是依 review 修正**之前**的版本(2026-10-12)。

**修正後重跑**(2026-10-17,同一個測試專案,另加兩個 MR;同樣只讀取)。每次都先讀 head 的
`dbt_project.yml` 一次,再決定要不要下載。

| 情況 | 讀設定檔 / 下載 | 結果 |
|---|---|---|
| MR 改 model | 1 / 1(head) | 與修正前相同:展開後的整句 SQL 送去跑規則,沒有補報 |
| MR 改 macro | 1 / 2(head、base) | 多出 `R003 major`,補報寫出「展開後第 1 句」;**不再**因為專案有 snapshot 就標不確定 |
| MR 改 `dbt_project.yml` | 1 / 2 | 與修正前相同:全部 model 可能受影響,補 major |
| MR 只改 `README.md` | 1 / 0 | 不下載、不算改到 dbt;沒有 dbt 相關的補報 |
| dbt 專案在子目錄(`dbt.project_dir: dbt_sub`),MR 改其中的 model | 1(`dbt_sub/dbt_project.yml`)/ 1(只下載 `dbt_sub`) | 取回成功,展開後的 SQL 送去跑規則 |
| `GITLAB_READ_TOKEN` 與 `GITLAB_TOKEN` 是同一把 | 1 / 1(拒絕) | 取回失敗,兩條 major;變更檔也不取回完整內容 |
| 沒有設定 `GITLAB_READ_TOKEN` | 1 / 1(拒絕) | 同上 |

重跑用的是接線那一版:只改 `README.md`、只改 `dbt_project.yml` 時,預掃把它當成 SQL 解析失敗而補一條
major。加上非 SQL 檔的處理之後,改由非 SQL 檔的分類與設定比對處理,不再補解析失敗的 major。

另外確認:三個 MR 的 `base_sha` 都是目標分支的最新 commit;seed、snapshot、Python model 都列在
`ref()` 找得到的名稱裡,並標成表名無法確定;專案資訊在子行程裡整理,正常完成;錯誤訊息不含 token
或網址。實測沒有涵蓋:呼叫模型之後的步驟(模型審查、執行驗證、寫回 GitLab)、超過上限的大量受影響 model。

**已知限制**

- mock 模式而 fixture 沒有放專案時(預設的 `fixtures/`、golden set):macro 檔仍會被當成 model 展開
  (接線前的行為)。要在 mock 模式看到接線的效果,得照 `examples/dbt_wiring/` 的方式放專案
- 注入掃描涵蓋 MR、取回的完整內容與預掃展開後的 SQL(會以規則命中的語句、解析錯誤的片段進 prompt);
  受影響 model 的展開結果不進 prompt(只有路徑、規則代號、語句序號),不另外掃。取回的完整內容與展開後的
  SQL 合計最多掃 500 萬字元(`MAX_EXTRA_SCAN_CHARS`,放得下一份展開結果的上限):展開可以把短樣板放大成
  幾百萬字元,掃描是線性時間,這個上限讓掃描時間有界。超過的部分不掃,補 major「部分內容超過注入掃描的上限,沒有掃描」交人工
  跟 MR 檔案本身的完整內容逐字相同(同一個路徑)的不重複掃,也不算進這個上限
- 審查途中 MR 被推了新 commit 偵測不到:取回的是 `sha` 那一版,審查結果也寫在那一版上;新的
  commit 沒有 commit status,要等它自己的審查
- 被刪除的 model 檔沿用接線前的行為(拿到的是被刪掉的內容,展開失敗 → major)
- 前後比較看的是每一句的規則命中數;命中數沒變、但邏輯變了的情況(例如同一句改刪另一張表),
  靠人工與之後的執行驗證
- 每個要展開的檔案開一次子行程;受影響的 model 每個開兩次
- 改到 seed / snapshot / analysis / test 目錄裡的檔案(含 seed 的 `.csv` 資料檔)時,反查不分析它們的影響,一律標不確定(major,交人工)。seed 的資料會被 model 以 `ref()` 讀到,不能說沒有影響;analysis / test 不影響 model,但沿用反查原本對這些目錄的保守處理,沒有放寬

## 後續

- **展開結果尚未在 MS SQL 沙盒實際執行過**(目前只驗證了展開字串與 `dbt compile`
  逐字一致)。要支援樣板的執行驗證,需把 `ref()`/`source()` 對應到沙盒每次建立的
  資料庫,且 model 引用的上游表與規格建立的測資表名稱不同,需要處理
- `{% if is_incremental() %}` 內的 SQL 不會被掃到(比照 compile 固定為 False),需另以 True
  再展開一次
- 真實 GitLab 模式下,dbt 專案以外的 SQL 檔(與專案裡不是樣板的 SQL 檔)仍只有 diff 新增的行,
  完整內容由 #22 以 Files API 依 head sha 取得
- 反查以檔案為單位;不分析第三方套件內的 macro;目錄由呼叫端指定
- `invocation_id` 為固定值,樣板若輸出它,結果會與 dbt compile 不同
- 待確認:規格檔名慣例(目前只去除 `mrt_` 前綴);earlyjob model 應歸屬主規則的規格,
  確認前會交人工
