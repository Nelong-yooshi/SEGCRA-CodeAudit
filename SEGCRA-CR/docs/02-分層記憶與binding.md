# 02-分層記憶與binding

## 在幹嘛(一段白話)

團隊知識(規範、慣例、觀察到的習慣)不整包倒進 prompt,而是存成一顆分層、可檢索的知識庫(`memory/knowledge/*.md`,每檔一條,YAML frontmatter + 本文):**binding** 恆常規範永遠注入且程式強制;**guideline** 大量參考知識依 scope + 相關度檢索少量注入;**observed** 從程式碼觀察學得的習慣權威最低。同一主題衝突時按四級來源權威解析,高者勝。binding 還能宣告 `suppress`,把已經講定不適用的預掃檢核點在**預掃層直接刪掉**——「講過的規定不再重複報」是確定性保證,不是模型的自律。風格學習(`style_learn.py`)則示範 observed 層的閉環:從已合併範例學出「前置逗號」慣例,附帶確定性檢查碼 S-COMMA,學到才檢查、被更高權威覆蓋就停用。

## 防的是什麼

- **遺忘**:把慣例塞對話 context 的做法,聊久了規範會被擠出視窗,模型「回到舊行為」。binding 層每次審查都重新注入(scope 內全數、不看相關度分數),**不受對話長度/時間影響**。
- **誤報重複(講過的還一直問)**:使用者已明確說「資料已淨額,H001 不適用」,若只把這句話放 prompt,模型仍可能照報。`suppress` 在預掃層確定性濾除,檢核點根本進不了 prompt,也進不了 enforce 補報鏈。
- **知識互相打架**:程式碼觀察到的習慣可能和使用者明講的規定衝突。`conflict_key` + 權威分級保證 user-explicit 永遠壓過 code-mined,不會出現「觀察學來的雜訊蓋掉明確指示」。
- **context 爆量**:正式環境多系統、大量知識,不可能每次全塞。scope 過濾 + top_k 檢索控制注入量。
- **風格提醒靠不住**:「團隊愛用前置逗號」寫在 prompt 裡模型不一定照做也不一定報;學成確定性檢查(S 碼)才有牙齒。

## 怎麼建的

### 三層與四級權威 — `toolbox/knowledge_store.py`

資料模型 `knowledge_store.py:Item`:`id / text / tier / source / scope / code / suppress / conflict_key / tags / evidence / ts`。

| tier | 行為 |
|---|---|
| `binding` | scope 內**全數**注入 prompt(不經相關度打分);`suppress` 生效;render 時標「【恆常規範·必守】」 |
| `guideline` | 進檢索池,依相關度取 top_k |
| `observed` | 同 guideline 進檢索池,但通常帶 `code`(確定性檢查碼),權威最低 |

| source(`AUTHORITY`) | 分數 | 語意 |
|---|---|---|
| `user-explicit` | 40 | 使用者明確指示(最高) |
| `user-authored` | 30 | 人工撰寫的慣例/skill |
| `auto-consolidated` | 20 | 學習迴路自動草擬、經人工核准 |
| `code-mined` | 10 | 從多份正式程式碼觀察到的習慣(最低) |

### conflict_key 衝突解析 — `knowledge_store.py:resolve_conflicts`

同 `conflict_key` 的知識項互斥:按 `(authority, ts)` 排序取最高者(權威平手取較新),其餘記進 suppressed 清單(`{id, beaten_by, key, loser_authority, winner_authority}`,會隨檢索結果回傳供稽核)。沒有 `conflict_key` 的項不參與互斥。這是權威分級的落地:使用者明講「改用行尾逗號」(user-explicit, 40)會直接壓掉 code-mined(10)的 `style-leading-comma`,且因為敗者的 `code` 不再進 `active_style_codes`,對應的確定性檢查同時停用。

### scope 過濾與檢索打分 — `knowledge_store.py:retrieve`

- **scope 過濾**(`_in_scope`):知識項 scope 含 `"*"`、或查詢 scope 為空、或兩者集合有交集即通過。查詢 scope 由 `pipeline.py:_derive_scope` 從變更檔路徑推出(`_SCOPE_MAP`:`rules/|anomaly|alert`→anomaly-rules、`settle|clearing|nostro`→settlement、`crm|…`→crm、`etl|…`→etl、`ledger|…`→core-banking;無命中預設 anomaly-rules),並固定附加 `sql-style`。
- **打分**(`_score`):query 與「item.text + tags」各取字元 bigram 集合,分數 = 交集 / query bigram 數。query 由 pipeline 取每檔 SQL 前 800 字串接(`kb_query`)。
- **`retrieve(query, scope, top_k=4)`**:scope 過濾 → `resolve_conflicts` → binding 全取,guideline/observed 取分數 > 0 的前 top_k。回傳 `{binding, retrieved, suppressed, stats}`。
- **render**(`render_for_prompt`):binding 段標題明寫「必守 — 不論對話多長/多久前講定,一律遵守」;每條標 tier 標籤與來源(如「使用者明確指示」),讓模型知道權威層級。

工具封裝在 `toolbox/memory.py:retrieve_knowledge(query, scope, top_k)`,回傳 JSON:`rendered`(直接進 system prompt 的「團隊慣例」段)、`binding_ids`/`retrieved_ids`、`suppress_codes`、`style_codes`、`suppressed_conflicts`、`stats`。

### binding 的 suppress:「講過的不再提」全機制

端到端鏈路(以附帶示例 `bind-reversal-netted.md` 為例):

1. 使用者明確指示「交易資料已由前置系統淨額,無需再處理沖正/退匯」→ 落成知識項:`tier: binding`、`source: user-explicit`、`scope: [anomaly-rules]`、`suppress: [H001]`、`conflict_key: reversal-handling`。
2. 審查時 `pipeline.py:review_mr` 呼叫 `memory__retrieve_knowledge`;其中 `knowledge_store.py:binding_suppress_codes(scope)` 先跑 `resolve_conflicts`(**先解衝突再算抑制**——若這條 binding 被更高權威項壓掉,它的 suppress 就不生效),再收集存活的、scope 內 binding 項的 `suppress` 聯集 → `{"H001"}`。
3. `review_mr` 拿到 `suppressed = set(know.get("suppress_codes"))` 後,直接改寫預掃結果:`entry["rules"] = [h for h in entry["rules"] if h.get("rule") not in suppressed]`。
4. 效果是三重的:H001 **不進 prompt**(模型不會看到而報)、**不進 `enforce_hints`**(比對基準就是同一份被濾過的 `pre`,不會補報「待人工確認」)、也就**不影響 `apply_policy`** 的 `pending_hints` 訊號。同時 binding 本文仍注入 prompt(「【恆常規範·必守】…H001 檢核點對本組織資料一律不適用」),模型也知道原因。

整條鏈沒有一步依賴模型自律——濾除發生在字典操作層。

### 風格學習 S 碼 — `toolbox/style_learn.py`

「從範例學風格 → 確定性檢查」的閉環,示範標的是前置逗號:

- **學**:`style_learn.py:mine_leading_comma(examples, min_examples=3, min_ratio=0.6)`——對每份已合併範例跑 `comma_style`(抓 `SELECT…FROM` 之間的欄位清單,數行首/行尾逗號的行數,判 leading/trailing/mixed/unknown),leading 佔比達門檻才學(避免誤學雜訊)。學到即產出 `Item(id="style-leading-comma", tier="observed", source="code-mined", scope=["sql-style"], code="S-COMMA", conflict_key="sql-comma-style", evidence=<範例數>)`,由 `memory.py:learn_style_from_examples` 經 `knowledge_store.save_item` 寫成 `memory/knowledge/style-leading-comma.md`——**機器學到的與人寫的完全同格式**,可 diff、可人工刪改。
- **查**:`knowledge_store.py:active_style_codes(scope)`——衝突解析後存活、scope 內、帶 `code` 的項的代碼集合(「學到才檢查;被更高權威覆蓋就停用」)。
- **掃**:`style_learn.py:check_style(sql, active_codes)` 只跑 active 的檢查;`_check_leading_comma` 在行尾逗號 ≥2 處時回 `{"rule": "S-COMMA", "severity": "info", "message": …}`。pipeline 在 `style_codes` 非空時對每檔呼叫 `memory__check_style`,命中附加進預掃結果的 `rules`。
- **牙齒**:模型沒報的 S 碼命中由 `pipeline.py:enforce_style` 確定性補成 info finding。

### 知識項 frontmatter 格式與手動新增

格式說明見 `memory/knowledge/TEMPLATE.md`(該檔不以 `---` 開頭,故不會被 `knowledge_store.py:_parse_md` 載入)。手動加一條:在 `memory/knowledge/` 建 `<id>.md`:

```markdown
---
id: bind-no-full-scan          # 檔名同 id
tier: binding                  # binding | guideline | observed
source: user-explicit          # user-explicit | user-authored | auto-consolidated | code-mined
scope:                         # 生效範圍;"*" = 全部
- anomaly-rules
suppress:                      # (選填)binding 專用:預掃層要確定性濾除的檢核點代碼
- H002
code: S-COMMA                  # (選填)確定性風格檢查碼,有填才啟用該掃描
conflict_key: report-grain     # (選填)同 key 互斥,權威高者勝
tags: [粒度, 聯名]              # 檢索用關鍵詞(參與 bigram 打分)
evidence: 1
ts: 1783612400                 # 權威平手時較新者勝
---
(本文)用完整、明確的書面語寫這條規範;審查 prompt 會直接注入這段文字。
```

存檔即生效(`load_items` 每次檢索都重讀 `KB_DIR` glob;`MEMORY_KB` 環境變數可改目錄)。real 模式下知識庫治理走 GitLab MR 人閘(`scripts/autopin.py` 的 seed/merge/sync,見 08 篇)。

## 對決策的影響

- binding 的 `rendered` 進 system prompt「團隊慣例」段,引導 findings 內容;且 system prompt 規定引用只允許 spec 與團隊慣例——知識項 id 是 `pipeline.py:validate_citations` 白名單的一部分(`kb_ids`)。
- `suppress` 濾除 → 該檢核點不產生 finding、不產生「待人工確認」→ 不觸發 `apply_policy` 的 `forbid_pending_hints`,等於**替自動放行清障**(這正是誤報收斂的價值:少一條噪音就少一次 needs_human)。
- S 碼命中(模型報或 `enforce_style` 補)是 info 級:不擋 auto_approve(`allowed_severities: [info]`),但會出現在回寫 GitLab 的行內留言。
- 衝突解析結果(`suppressed_conflicts`)只是稽核資訊,不進 findings。

## 怎麼驗證它在動

```bash
cd .../SEGCRA-CR

# 1) 檢索與分層:看 binding 全注入 + suppress/style 碼
.venv/bin/python -c "
from toolbox.memory import retrieve_knowledge
print(retrieve_knowledge('ATM 提領 累計 通報', scope='anomaly-rules', top_k=4))"
# 預期:rendered 內含 bind-reversal-netted 與 bind-no-sysdate(binding 全注入),
#       suppress_codes=["H001"],style_codes=[](尚未學風格時)

# 2) suppress 確定性濾除:同一份 SQL,預掃層有 H001、進管線後消失
.venv/bin/python -c "
import json; from toolbox.sqltools import run_rules
sql = json.load(open('fixtures/mr_001.json'))['files'][0]['full_content']
print([h['rule'] for h in json.loads(run_rules(sql))])"
# 預期:含 'H001'(聚合 transactions 且無沖正字樣)
.venv/bin/python demo.py --mr 001 --dry-run
# 預期:findings 中沒有 [H001](被 bind-reversal-netted 的 suppress 濾除)

# 3) 風格學習閉環:學 → 存檔 → 檢查
.venv/bin/python -c "
from toolbox.memory import learn_style_from_examples, check_style
ex = ['SELECT a\n  , b\n  , c\nFROM t']*3
print(learn_style_from_examples(ex))            # learned: true, code: S-COMMA
print(check_style('SELECT a,\n  b,\n  c\nFROM t', scope='sql-style'))"
# 預期:第二行印出 S-COMMA info 命中;memory/knowledge/style-leading-comma.md 已生成
# (驗完可刪 style-leading-comma.md 還原)
```

## 擴充與注意

- **加一層知識**:通常不用改程式——建 `.md` 檔即可。要新的 scope 名,對應規則在 `pipeline.py:_SCOPE_MAP` 加一行(路徑 regex → scope 名)。
- **加 S 碼**:`style_learn.py` 寫檢查函式並登記進 `_CHECKS`,再寫對應的挖掘函式(參考 `mine_leading_comma`);pipeline 端 `enforce_style` 以 `S-` 前綴通用比對,不用改。
- **調權威**:`knowledge_store.py:AUTHORITY` 字典;調 top_k / memory 預算在 `pipeline.py` 呼叫處與 `config/models.yaml` 的 `budget.memory`。
- 已知限制:檢索打分是字元 bigram 重疊(無 embedding),對同義改寫召回有限——binding 不受影響(不走打分),guideline 命名與 tags 要放關鍵詞;`suppress` 只在 `tier: binding` 生效;`_score` 只看 text+tags,不看 id;`conflict_key` 需人工命名一致,兩條實質同主題但 key 不同的項不會互斥。
