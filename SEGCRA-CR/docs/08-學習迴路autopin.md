# 08-學習迴路與 autopin

## 在幹嘛(一段白話)

審查系統用久了會累積兩種寶貴訊號:審查人員對每條 finding 的處置(採納/駁回),以及審查人員在討論中講定的常設規定(「沖正前置系統都處理掉了,不用再問」)。這條學習迴路把這些訊號變成系統長期記憶:每筆處置由 `record_feedback` 記成判例;審查時模型用 `lookup_similar_reviews` 查判例,曾被駁回的同型問題降級或不報;判例累積到門檻後,`consolidate_feedback.py` 或 `autopin.py` 自動起草成「抑制型 binding 知識項」的提案;審查人員講定的規定則由 `draft_binding` 用 LLM 結構化成 binding。所有草案都走 GitLab MR——**人按 merge 才生效**,merge 後 `sync` 拉回本地 `memory/knowledge/`,下一次審查起確定性遵守。整個設計是「AI 起草、人一鍵定住」,GitLab 就是 UI,merge 就是鎖。

## 防的是什麼

- **防同型誤報無限重複(遺忘)**:LLM 沒有跨審查的記憶,審查人員這次駁回「H001 沖正未處理」,下個 MR 模型照報。靠「把慣例全部塞進 prompt 期待模型記得」不可靠——context 變長就被稀釋,而且模型自律本來就不可信。所以誤報收斂最終落在**確定性抑制**:binding 的 `suppress` 代碼在預掃層直接濾掉,模型根本看不到那個檢核點。
- **防講過的規定被遺忘**:審查人員口頭講定的規範若只活在對話裡,人一換、對話一長就消失。autopin 把它固化成版本控制下的 .md 檔。
- **防學壞(這是留人閘的核心理由)**:抑制是永久靜音一個檢核點——釘錯一條,就是**系統性漏報**從此常態化,而且因為「不報」是無聲的,沒人會發現。攻擊面也存在:若判例入庫→自動生效無人把關,灌幾筆假駁回就能把資安檢核點(如 H004)靜音,等於用回饋通道做注入。因此門檻(rejected≥2)只決定「起草」,生效必須經過 GitLab MR 的人工 merge——權限、稽核軌跡、可回溯(revert 即解除)全用現成機制。
- **防模型亂填內部代碼**:審查人員講白話,不知道 H001/H002 這些內部檢核點代碼;讓 LLM 猜代碼實測會亂填,填錯 suppress 等於靜音錯誤的檢核點。所以代碼對映是確定性關鍵詞表,不進 LLM。

## 怎麼建的

整條迴路五站,對應五個程式位置:

**(1)判例入庫 — `toolbox/memory.py:record_feedback`**
輸入 `title / disposition(accepted|rejected)/ detail / sql / note / tag`(tag 填來源規則或檢核點代碼,如 `H001`,供後續精準分群),驗證 disposition 合法後,append 一行 JSON 到 `memory/review_history/history.jsonl`(路徑可用環境變數 `MEMORY_HISTORY` 覆蓋)。此函式經 `orchestrator/tool_hub.py` 的 `TOOL_GROUPS["memory"]` 以 `memory__record_feedback` 曝露,審查人員處置 finding 時記錄。

**(2)審查時查判例 — `toolbox/memory.py:lookup_similar_reviews`**
用 bigram 集合相似度:`_bigrams` 把文字去空白、轉小寫、切成所有相鄰兩字元的集合;對 history.jsonl 每筆記錄以 `title+detail+sql` 建集合,分數 = 交集大小 / 查詢集合大小,取 top 5 回傳(含 disposition 與 similarity)。審查 system prompt(`orchestrator/pipeline.py` SYSTEM_TEMPLATE)明定:「每個 finding 先用 memory__lookup_similar_reviews 查判例,曾被 rejected 的同型問題降級或不報」。注意這一步是**模型參考判例後自行降權**,屬勸導性;確定性的抑制在第(5)站的 binding suppress。

**(3)離線分群起草提案 — `scripts/consolidate_feedback.py`**
讀 history.jsonl 全量記錄,`_cluster` 分群:**有 tag 的按 tag 精準分群**(規則/檢核點代碼最可靠);無 tag 的退回標題+note 的 bigram Jaccard 相似度(`len(交集)/len(聯集) ≥ 0.3` 併入既有群,否則自成一群)。每群統計:

- `rejected ≥ 2`(`REJECT_THRESHOLD`)→ 草擬「抑制型 binding」——完整的 .md front-matter 區塊(`id: auto-suppress-<tag>`、`tier: binding`、`source: auto-consolidated`、`suppress: [<tag>]`、`conflict_key: <tag>-applicability`)加上駁回 note 彙整成的規範文字。
- `accepted ≥ 3`(`ACCEPT_THRESHOLD`)→ 草擬「規則升級提案」:反覆出現且皆被採納的問題,建議寫成確定性規則(R/H 系列)進 sqltools rule-base,零 LLM 成本攔截。

輸出到 `memory/proposals/PROPOSALS.md`。**刻意不放** `memory/knowledge/`——那個目錄整包會被審查管線讀取,未核准的 `suppress` 會直接生效;提案放隔離目錄,人工核准後才移入。

**(4)線上版:自動開 MR — `scripts/autopin.py` + `orchestrator/knowledge_governance.py`**

- `autopin.py from-history`:`_cluster_reject` 按 tag(無 tag 退回 title)聚 rejected 記錄,≥2 次的群直接組出 `Item(tier="binding", source="auto-consolidated", suppress=[tag], conflict_key=f"{tag}-applicability")`,交 `propose_binding` 開 MR。
- `autopin.py from-comment "<審查人員的一句話>"`:走 `knowledge_governance.py:draft_binding`。關於**審查人員指示的確定性偵測**:`knowledge_governance.py` 定義了 `_DIRECTIVE_MARKERS`(regex 字樣表:「不要報|不用管|不得|一律|統一|固定用|屬核定用途|以後都|本來就…」等),作為從討論串自動篩出「硬指示」候選的確定性前濾;現版程式中此表尚未接上自動掃留言的流程,`from-comment` 為手動觸發、把整句直接交給 `draft_binding`,由其 LLM 判準把關非規範的句子(回 `is_rule:false` 就不起草,避免亂 pin)。
- `draft_binding`(`knowledge_governance.py`):用 `DRAFT_SYSTEM` prompt 讓 LLM 只做兩件事——判斷是否為**常設**團隊硬規定(一次性說明/閒聊回 `{"is_rule": false}`),是則把口語重述成完整書面規範並輸出嚴格 JSON(text/scope/conflict_key/tags)。prompt 明講「不用管內部檢核點代碼——系統會自動對應」。**suppress 代碼完全不讓模型填**:`_infer_suppress` 用確定性關鍵詞表 `_CHECK_KEYWORDS`(H001←沖正/退匯/淨額、H002←通報粒度/聯名戶/…、H003、H004、H005←遮罩)對「原始指示+模型重述」做包含比對推出代碼。理由寫在程式註解裡:讓模型猜內部代碼它會亂填,而填錯 suppress 等於靜音錯的檢核點;關鍵詞對映保證「不用管通報粒度」這種口語真的對到 H002。產出 `Item(source="user-explicit")`——權威最高(`toolbox/knowledge_store.py` AUTHORITY:user-explicit 40 > user-authored 30 > auto-consolidated 20 > code-mined 10),衝突時以 `conflict_key` 互斥、高權威者勝。
- `propose_binding`(`knowledge_governance.py`):在知識庫 repo(`root/segcra-knowledge`,`KnowledgeRepo.ensure_project` 不存在會自動建)開分支 `autopin/<item.id>-<ts>`、commit `knowledge/<item.id>.md`(`knowledge_store.dumps` 序列化)、開 MR——標題 `[binding] <規範前40字>`,描述附**來源證據**(誰、哪句話,或「同型 finding 被人工駁回 N 次」)與抑制的檢核點清單,並明說「merge 此 MR = 規範被定住;若不成立請直接關閉」。

**(5)人閘與生效 — merge → sync → 確定性抑制**
人在 GitLab 檢視證據後按 merge(demo 可用 `autopin.py merge <iid>`,`KnowledgeRepo.merge_mr` 會先輪詢 mergeability 再合併)。`autopin.py sync` 呼叫 `sync_to_local` 把 repo main 的 `knowledge/*.md` 拉回本地 `memory/knowledge/`。之後每次審查,`pipeline.py:review_mr` 呼叫 `memory__retrieve_knowledge`,其中 `knowledge_store.binding_suppress_codes` 先做衝突解析、再收集 scope 內存活 binding 的 suppress 代碼,pipeline 直接把預掃結果中 `rule` 命中這些代碼的項**確定性濾除**——這就是「講過的規定不再重複報」的機制保證,不靠模型讀了慣例後自律。

## 對決策的影響

- **binding suppress(確定性)**:在預掃層移除被抑制的檢核點,該代碼從此不會成為 finding、也不會成為「檢核點待人工確認」的 pending hint。連鎖效應:findings 變少 → `apply_rubric` 分數不再被扣 → `apply_policy` 三態決策裡 `severities ⊆ allowed_severities` 與 `forbid_pending_hints` 兩個 auto_approve 條件更容易成立。也就是說,一條 pin 對的 binding 能把原本卡在 `needs_human` 的同型小改 MR 放行到 `auto_approved`;反過來,pin 錯就是讓漏報直通 `auto_approved`——這正是留人閘的原因。
- **lookup_similar_reviews(勸導性)**:影響的是模型產出 finding 前的判斷(降 severity 或不報),不直接動三態條件;severity 一降,經 `apply_rubric` 的確定性計分間接影響 score 與 verdict。
- **accepted≥3 的規則升級提案**:被採納後進 sqltools rule-base,命中經 `enforce_rules` 保證進 findings(模型省略也會補回),影響方向與抑制相反——把反覆出現的真問題變成零成本必報。
- 學習迴路本身(consolidate/autopin)是離線流程,不在審查管線的請求路徑上,提案未核准前對決策零影響(PROPOSALS.md 不被管線讀取)。

## 怎麼驗證它在動

離線部分不需要 GitLab 與模型即可完整驗證(注意 `consolidate_feedback.py` 讀的是固定路徑 `memory/review_history/history.jsonl`,測試前先備份):

```bash
cd SEGCRA-CR
cp memory/review_history/history.jsonl /tmp/history.bak 2>/dev/null || true

# 1. 判例入庫:同型 finding 駁回兩次(tag=H002)
.venv/bin/python - <<'PY'
import sys; sys.path.insert(0, ".")
from toolbox import memory
for note in ("聯名戶已於前置系統拆分", "通報粒度規格已核定為一戶一筆"):
    print(memory.record_feedback(
        title="通報粒度疑似一對多重複通報", disposition="rejected",
        note=note, tag="H002"))
PY
# 每筆回 {"ok": true},history.jsonl 各多一行 JSON

# 2. 判例檢索:bigram 相似度查得到、帶 disposition
.venv/bin/python -c "import sys; sys.path.insert(0,'.'); \
from toolbox import memory; print(memory.lookup_similar_reviews('通報粒度 重複通報'))"
# → {"results":[{... "disposition":"rejected", "similarity":0.xx}, ...]}

# 3. 分群起草:rejected≥2 達門檻
.venv/bin/python scripts/consolidate_feedback.py
# → 「產出 1 條提案 → .../memory/proposals/PROPOSALS.md」
# PROPOSALS.md 內含「抑制型 binding 提案(rejected ×2)」與
# auto-suppress-h002 的完整 front-matter 區塊(suppress: [H002])
```

線上部分(需 GitLab 可達;`from-comment` 另需 fast profile 模型):

```bash
# 4. 審查人員指示 → LLM 結構化 → 開 MR(觀察 suppress 由關鍵詞確定性補上 H001)
.venv/bin/python scripts/autopin.py from-comment "沖正退匯前置系統都處理掉了,不用再問" --profile fast
# → 「✓ 起草 binding「explicit-...」→ MR !N <url>」;閒聊句則印「判定為非常設規範,不起草」

# 5. 判例達門檻 → auto-consolidated binding → 開 MR
.venv/bin/python scripts/autopin.py from-history
# → 「達門檻(rejected≥2)的誤報群:1」「✓ auto-consolidated「auto-suppress-h002」→ MR !N」

# 6. 人閘 → 生效
.venv/bin/python scripts/autopin.py merge <iid>   # state: merged
.venv/bin/python scripts/autopin.py sync          # 已從 repo main 同步 N 個知識項
# 之後對含 H002 命中的 MR 跑審查,該檢核點不再出現於 findings / pending hints
```

抑制生效的既有實例可直接看:`memory/knowledge/bind-reversal-netted.md`(`suppress: [H001]`)存在時,審查 fixtures 裡的聚合規則 MR 不會再被問「沖正處理了嗎」。驗完還原:`cp /tmp/history.bak memory/review_history/history.jsonl`。

## 擴充與注意

- **加白話→代碼對映**:改 `knowledge_governance.py` 的 `_CHECK_KEYWORDS`(新增檢核點代碼時必須同步加,否則審查人員口語推不出 suppress,binding 只剩文字勸導、沒有確定性抑制)。
- **改門檻**:離線版在 `consolidate_feedback.py` 的 `REJECT_THRESHOLD`/`ACCEPT_THRESHOLD`;線上版 `autopin.py:_cluster_reject` 的 `threshold` 參數(兩處各自獨立,調整要同步)。
- **改起草 prompt**:`knowledge_governance.py` 的 `DRAFT_SYSTEM`(判準、scope 選單、輸出 JSON 欄位)。
- **改硬指示字樣表**:`_DIRECTIVE_MARKERS`;若要把「從討論串自動偵測指示」接上 webhook 的 note 事件,這個 regex 就是設計好的前濾器,接線時記得沿用 note 事件的 bot 過濾避免迴圈。
- **已知限制**:bigram 相似度對短標題與同義改寫不敏感,分群主要靠 tag 撐精度——record_feedback 時**務必填 tag**;`lookup_similar_reviews` 的降權靠模型遵守 prompt,屬軟約束,誤報要真正歸零得走到 binding suppress;`_cluster` 的相似度分群只跟每群第一筆比對,群代表選取有順序敏感性;autopin `from-history` 每次重跑會對同一群再開新 MR(無去重),重複的提案 MR 需人工關閉。
- **鐵則不要破**:提案類產物永遠不得直接落在 `memory/knowledge/`(那裡即刻生效);不做無人自動 merge——一條規範釘錯就是永久錯,系統停在「AI 起草、人一鍵 merge」是設計,不是未完成。
