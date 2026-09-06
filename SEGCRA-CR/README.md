# SEGCRA-CR — SQL Merge Request 自動審查

## 1. 這是什麼

對 GitLab CE 上的 SQL merge request(異常交易規則等)做自動 code review 的地端系統:模型跑在本地 Ollama(OpenAI-compatible endpoint),審查結果以行內留言、總評、label 與 commit status 回寫 GitLab,並依確定性政策給出三態決策(自動放行 / 需人工 / 擋下)。

```
MR(webhook 或手動觸發)
 │
 ├─ 前處理(確定性,不經 LLM)
 │    ├ 注入掃描(MR 標題/描述/diff/註解中的提示注入 → 命中即 blocker)
 │    ├ rule-base 預掃(sqltools:AST 規則 R 系列 + 檢核點 H 系列 + lint)
 │    ├ 分層記憶(binding 恆常規範全注入;guideline 依相關度檢索;
 │    │           binding 的 suppress 在預掃層確定性濾除「講過的不再提」)
 │    └ 已學會的風格檢查(S 系列,確定性掃描)
 │
 ├─ LLM 審查(中間層,視為不可信)
 │    skill(審查方法論)+ 慣例 + 預掃結果 + 核定規格(spec)→ 報告 JSON
 │
 ├─ 後處理(確定性)
 │    ├ 引用白名單(只允許 spec 檔 / 團隊慣例,防捏造)
 │    ├ enforce 鏈(rule/hint/style/injection:預掃命中不因模型省略而消失)
 │    ├ **執行驗證 spec_exec(必跑)**:
 │    │    角色一 測資生成 agent(LLM,讀 spec 產測資)
 │    │    → 角色二 沙盒執行(確定性程式,DuckDB in-memory)
 │    │    → 角色三 仲裁 agent(LLM,只對不符案例判定測資錯還是 SQL 錯)
 │    ├ rubric 評分(分數由 severity 確定性計算,不信模型自評)
 │    └ 決策三態:auto_approved / needs_human / blocked
 │
 └─ 回寫 GitLab:行內留言、總評、label、commit status(merge 閘門)
```

> **三明治理念**:中間的 LLM 不可信——它會漏報、會被提示注入壓制、自評分數會漂移。所以前後都是確定性程式:前處理把該查的資料備妥、把已講定的規範濾掉;後處理保證規則命中不消失、注入必擋、分數可重現、決策有據。個別防線可依環境增減,但「確定性前處理 → LLM → 確定性後處理」這個三明治骨架是底線。



目錄結構:

```
SEGCRA-CR/
├── demo.py                  # 單一 MR 審查入口(mock/real 皆可)
├── config/models.yaml       # 模型 endpoint / profiles / roles / 決策政策
├── orchestrator/            # 管線:pipeline.py、spec_exec.py(執行驗證)、tool_hub.py、agent.py …
├── toolbox/                 # 行程內工具模組:gitlab / sqltools / memory(純 Python 函式)
├── tools/                   # prescan.py:確定性預掃 CLI(IDE agent / 桌邊檢查用)
├── scripts/                 # webhook_server.py、autopin.py、consolidate_feedback.py
├── skills/                  # 審查方法論(sql-review、objective-reviewer …)
├── specs/                   # 核定規格 R-xxx.md(執行驗證的真值來源)
├── fixtures/                # 三個 mock MR(離線 demo 用)
└── memory/knowledge/        # 分層知識庫(binding/guideline/observed)
```

## 機制細節文件(docs/)

本 README 只講**這一包怎麼啟動與接上 GitLab**;每一道檢測/關卡「在幹嘛、防什麼失敗、程式怎麼建的、對決策的影響、怎麼驗證它在動」更詳細的細節，請看 **`docs/`**(讀碼前先看對應篇):

| 篇 | 管什麼 |
|---|---|
| `docs/00-總覽.md` | 一次審查的端到端資料流 + 各篇索引 |
| `docs/01-前處理.md` | 注入掃描、R/H 規則逐條、lint，怎麼進 prompt |
| `docs/02-分層記憶與binding.md` | 三層知識庫、來源分級、「講過的不再提」全機制、風格自學 |
| `docs/03-LLM審查層.md` | agent loop、JSON 容錯、工具註冊表、skill、prompt 組裝 |
| `docs/04-後處理防線.md` | enforce 鏈逐道:防什麼、演算法、對 findings 的動作 |
| `docs/05-執行驗證spec_exec.md` | 三角色最細一篇:測資契約、覆蓋檢查、DuckDB 執行、仲裁 |
| `docs/06-決策與merge閘門.md` | 評分公式、三態全部條件、CE 閘門三件套 |
| `docs/07-webhook自動化.md` | token 驗證、事件路由、debounce、已知限制 |
| `docs/08-學習迴路autopin.md` | 判例入庫 → 分群起草 → binding MR 人閘全鏈 |

## ADR
### ADR-00（棄用RAG）
經檢視，此專案檢視之程式碼難度不高，基本上就是 if-else 的 rule base 規則判斷；另外，有可能最後會使用雲端的模型，這些 context window 都應該很夠，直接把這些資訊都塞進去也可行。因此不做 RAG 改以 skill 去指定要看參考哪些檔案。

### ADR-01（必須使用沙盒）
取消 RAG 之後，若只剩 system prompt 在調教 LLM，等於沒有任何機制性保證;因此把沙盒執行驗證從選配改為必要。

執行驗證必須要有三角色，SQL 不只要「看起來對」,還要「跑起來對」。測資由 LLM 依 spec 生成(角色一)，但測資生成本身也可能幻覺(造錯資料、預期標錯)；執行(角色二)是純程式，不可有幻覺；當實際結果與測資預期不符時，不能直接斷定 SQL 錯——由仲裁 agent(角色三)以 spec 為唯一真值逐案判定:**測資錯**就剔除該案並記錄,**SQL 錯**才成為 major finding。全程記錄在報告的 `_spec_exec` 供稽核。找不到規格檔時不是跳過,而是 major「無規格可驗,無法執行驗證」→ 一律 needs_human,不得自動放行。

### ADR-02（棄用 MCP）
目前的設計架構，MCP 套用之功能皆能以 skill 取代；另外，將來導入後人員維護需要多學習一套 MCP 協定。

## 2. 環境需求

- Python 3.12+
- Ollama(本機或遠端 endpoint，見 §5 模型端點)
- 模型:`gemma4:31b`(審查主力，對應 `review` profile)+ 一個小模型供開發迭代(預設 `gpt-oss:20b`,對應 `fast` profile;可換更小的，改 `config/models.yaml` 即可)

## 3. 快速開始(mock 模式，不用 GitLab)

```bash
cd SEGCRA-CR
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1) 管線煙霧測試(不呼叫模型):預掃 + 記憶 + spec 尋找是否正常
.venv/bin/python demo.py --mr 001 --dry-run

# 2) 完整審查(需要模型;預設 profile=review)
.venv/bin/python demo.py --mr 001
.venv/bin/python demo.py --mr 001 --profile fast   # 開發時換小模型

# 3) 裸模型 A/B 對照:無 skills/記憶/預掃/執行驗證，拿來對照管線的價值
.venv/bin/python demo.py --mr 001 --baseline

# 4) 單檔確定性預掃(不經 LLM;給桌邊 IDE agent 照 instructions 跑的檢查入口)
.venv/bin/python tools/prescan.py path/to/change.sql   # 有 blocker/major 時 exit 1
```

三個內附 fixture(審查結果寫到 `review_output/mr_<id>_review.json`):

| MR | 內容 | 預期結果 |
|---|---|---|
| `001` | 新規則 R-201，SQL 與 `specs/R-201.md` 完全相符 | 執行驗證通過;新規則檔依政策仍 needs_human |
| `002` | R-305 維護，埋雷:`>` 被改成 `>=`(規格為「超過=不含」) | 執行驗證在邊界案例(恰 200,000)抓到 → major |
| `003` | 帶提示注入 payload + 閾值數量級 typo(20 → 2) | 注入掃描命中 → blocker → blocked;執行驗證另抓到 typo |

## 4. spec 怎麼寫

執行驗證以 `specs/<規則碼>.md` 為唯一真值，格式(參考 `specs/R-201.md`):

```markdown
# R-xxx 規則名稱

## 說明
一段白話:這條規則偵測什麼、何時通報。

## 資料表定義(釘死 schema;執行驗證以此建表)
CREATE TABLE transactions(account_id INT, tx_time TIMESTAMP, ...);

## 需求項目
1. **交易範圍**:tx_type='WITHDRAW' AND channel='ATM'
2. **閾值**:100,000 元;「達(含)」= `>= 100000`
3. **時間窗**:半開區間(`>= :start_date AND < :end_date`)
...(每一條具體到可直接對照實作:含運算子、欄位、條件)

## 補充
- 來源與特殊註記(閾值含等由誰核定等)。
```

要點:

- **schema 必須釘死**(資料表定義段)。測資生成 agent 依此建表;沒有固定 schema 就無法執行驗證。
- **用語與含等一致**:「達/以上」= 含 = `>=`;「超過」= 不含 = `>`。測資會對每個數值門檻生成邊界案例(恰好等於門檻、差最小單位)，含等寫錯一定被抓。
- 測資生成怎麼用它:agent 把需求項目拆成**原子條件**(過濾、閾值、時段、幣別、豁免、時間窗各自成條)，每條生成 true/false 兩向案例;之後由確定性程式逐案在 DuckDB 執行比對。
- **找不到 spec 會怎樣**:管線從 MR 的 SQL/標題/描述抓規則碼(`R-\d+`)，對應 `specs/<code>.md`(real 模式從 GitLab repo 的 `specs/` 目錄抓;mock 模式讀本地 `specs/`)。找不到 → major finding「無規格可驗，無法執行驗證」→ 決策至多 needs_human，**不會自動放行**。

### 範例:表格式規格 → md(導入時的第一件整理工作)

既有規則的規格常常都在**試算表**裡(基本資料、代碼清單、條件 1/2/3 各細項…)。導入這套系統前，要把每條規則的表格規格**逐條整理成上面這種 md 格式**——完整到不用去讀程式碼就知道規則是什麼。

**示範用的合成範例**放在 **`examples/sample/`**(表名、欄位、代碼、金額門檻皆為虛構,只用來展示格式與程式長相):

- `examples/sample/RETAIL_M1_code` — 典型的規則程式長相:dbt 模型(Jinja macro、`{{ ref() }}`、`var("target_date")`)、SQL Server 方言、排程器傳入日期。
- `examples/sample/RETAIL_M1_spec.md` — **就是照通用格式整理出來的規格 md**，整理其他規則時照這個骨架填:基本資料 → 說明 → 排除資料 → 代碼分類 → 篩選條件(逐條、可對測資判 true/false)→ **待補**(核定規格未載明的實作細節)→ 補充。

整理原則:**規格未載明的實作細節一律列入「待補」,不從程式碼腦補**。程式怎麼寫是實作,不等於規則的核定內容;把實作反寫成規格,等於讓錯誤的實作自我認證。跨來源不一致時,以核定規格為準。

> 注意:這類程式是 dbt + Jinja + SQL Server 方言，本包的執行驗證吃**純 SQL**(postgres→duckdb 轉譯)。接實際環境時，規則 SQL 先 `dbt compile` 展開 macro/ref、把日期變數帶入，再送進管線——這是整合工作的一部分,範例的 macro(`is_inward_large` 等)展開後就是規格 md 裡的代碼清單 + 排除規則。

## 5. 接上 GitLab(CE)

> 需要一個自己的 GitLab CE 實例;沒有現成環境的話,§8 有用 docker 起一個測試用 GitLab CE 的作法。以下的 URL、專案 id 請換成自己的。

1. **建 Personal Access Token**:登入 GitLab → 右上頭像 → *Edit profile* → *Access tokens* → *Add new token*，Scopes 勾 `api`，建立後複製 token(只顯示一次)。
2. **設環境變數**(或複製 `config/gitlab.env.example` 為 `config/gitlab.env` 填入，autopin 會讀;管線本身讀環境變數):

   ```bash
   export GITLAB_URL=http://localhost:8929        # GitLab 位址(遠端時可經 SSH tunnel,見 §8)
   export GITLAB_TOKEN=<your-token>               # 請自行設定,勿提交至版本控制
   export GITLAB_PROJECT=<專案 id>                # 專案首頁 Settings→General 可查
   ```

   > `config/gitlab.env` 與任何含 token / 密碼的檔案都已列入 `.gitignore`,**請自行設定,勿提交至版本控制**。

   三個都設齊即切到 real 模式;缺任一則走 mock(fixtures/)。
3. **手動審一條真 MR**:

   ```bash
   .venv/bin/python demo.py --mr <MR 的 iid>
   ```

   審完到 GitLab 該 MR 頁面看:行內留言、總評(分數/verdict/決策)、`ai-review::<決策>` label，以及 commit status `segcra/review`。
4. **模型端點哪來**:管線讀 `OLLAMA_URL`(預設 `http://localhost:11434/v1`)。Ollama 跑在別台機器時,常見作法是以 SSH(或 autossh)反向 tunnel 把 11434 掛到執行管線那台機器的 loopback;掛好後驗證:

   ```bash
   curl http://127.0.0.1:11434/api/tags     # 有回模型清單即通
   ```

   endpoint 在別處時:`export OLLAMA_URL=http://<host>:11434/v1`。

## 6. Webhook 自動觸發

webhook server 建議跑在**與 GitLab 同一台主機**(放在哪個目錄都可以):

```bash
export GITLAB_URL=http://127.0.0.1:8929 GITLAB_TOKEN=<your-token> GITLAB_PROJECT=<專案 id>
export GITLAB_WEBHOOK_SECRET=<自訂一串密鑰>
export REVIEW_PROFILE=review                  # 用哪個 profile 審
.venv/bin/python scripts/webhook_server.py --port 8000
# 健康檢查:curl http://127.0.0.1:8000/health  → healthy
```

GitLab 設定(逐步):

1. **Admin 區必要設定**:Admin Area → Settings → Network → *Outbound requests* → 勾 **Allow requests to the local network from webhooks and integrations** → Save。
   (GitLab 容器要打回同機的 host，目標是內網位址，不開這個 webhook 會被 GitLab 自己擋掉。)
2. 專案 → Settings → Webhooks → *Add new webhook*:
   - URL:`http://172.17.0.1:8000/gitlab-webhook`(`172.17.0.1` 是 docker bridge gateway，容器打回主機用它，已實測可達)
   - Secret token:填與 `GITLAB_WEBHOOK_SECRET` 相同的值
   - Trigger:勾 **Merge request events** 與 **Comments**
   - Add webhook 後按該 webhook 的 **Test → Merge request events** 驗證，webhook server 端應印出觸發紀錄(HTTP 200)。
3. 之後 MR 開啟/更新即自動觸發審查;在 MR 討論串留言 **`/review`** 可隨時重審(bot 帳號與 root 的留言不觸發，避免迴圈)。
4. **Debounce**:同一 MR 審查進行中再被觸發會直接略過(log 顯示 `審查中，略過`)，審完才接受下一次觸發。

## 7. Merge 閘門(CE 的作法)

GitLab CE 沒有 approval rules，可行的擋法是:**commit status(external pipeline)+ 專案設定「Pipelines must succeed」+ protected branch**。

1. 審查管線每次審完會對 MR 的 head commit 回寫 commit status(name=`segcra/review`):
   - `auto_approved` → **success**
   - `needs_human` / `blocked` → **failed**(description 帶原因)
2. 專案 → Settings → Merge requests → Merge checks → 勾 **Pipelines must succeed** → Save。
   從此 status 為 failed 的 MR，Merge 按鈕會被鎖住。
3. 專案 → Settings → Repository → Protected branches:把 `main` 設為 protected，*Allowed to push* 設 No one、*Allowed to merge* 設 Maintainers——確保所有變更都走 MR、都經過審查。

三態決策的實際效果:

| 決策 | commit status | 效果 |
|---|---|---|
| `auto_approved` | success | 小幅變更且無實質問題;Merge 按鈕可按(仍可要求人再看) |
| `needs_human` | failed | Merge 被鎖;人工確認後，人可留言 `/review` 重審，或由有權限者依 GitLab 流程處置 |
| `blocked` | failed | 有 blocker(注入攻擊、hardcode 憑證、DELETE 無 WHERE 等);修正前不得合入 |

補充:`needs_human` 走 failed 是刻意保守——CE 的 merge check 只認 success/failed 兩種結果，凡是需要人看過的都先鎖住，由人做最後放行。

## 8. 自架一個測試用 GitLab CE(docker)

手邊沒有 GitLab 可接時,用 docker 起一個 GitLab CE 來測整條流程。以下把 web 綁在
`127.0.0.1:8929`、git-ssh 綁在 `127.0.0.1:2224`,只聽本機不對外開 port;
資料持久化到主機上的 `<資料目錄>`(自行決定路徑,例如 `/srv/gitlab`)。

```bash
sudo docker run -d \
  --name segcra-gitlab \
  --hostname localhost \
  --shm-size 256m \
  -p 127.0.0.1:8929:80 \
  -p 127.0.0.1:2224:22 \
  -v <資料目錄>/config:/etc/gitlab \
  -v <資料目錄>/logs:/var/log/gitlab \
  -v <資料目錄>/data:/var/opt/gitlab \
  -e GITLAB_OMNIBUS_CONFIG="external_url 'http://localhost:8929'; gitlab_rails['gitlab_shell_ssh_port'] = 2224; prometheus_monitoring['enable'] = false; puma['worker_processes'] = 2; sidekiq['max_concurrency'] = 10;" \
  gitlab/gitlab-ce:latest
```

### 日常開關

```bash
docker start segcra-gitlab     # 啟動
docker stop  segcra-gitlab     # 關閉
docker ps -a --filter name=segcra-gitlab   # 看容器狀態
docker logs -f segcra-gitlab   # 跟看容器 log
```

啟動後要等 **1~4 分鐘**才可用。ready 判準:

```bash
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8929/users/sign_in   # 回 200 = ready
```

(不要用 `/-/readiness` 判斷——它有監控白名單,外部打會 404。)

**GitLab 很吃資源(建議保留 4GB+ RAM 給它),不用時務必 `docker stop segcra-gitlab`。**

### 初始 root 密碼

全新安裝時的初始 root 密碼在容器內 `/etc/gitlab/initial_root_password`(**24 小時後自動失效**):

```bash
docker exec segcra-gitlab cat /etc/gitlab/initial_root_password
# 或直接重設:
docker exec -it segcra-gitlab gitlab-rails runner \
  "u = User.find_by_username('root'); u.password = u.password_confirmation = '<你的密碼>'; u.save!"
```

密碼與 token 請自行設定並妥善保管,**勿寫進文件或提交至版本控制**。

### 跑在遠端主機時:用 SSH tunnel 看網頁

GitLab 只綁在該主機的 127.0.0.1,不對外開 port——所以從自己的電腦要透過 SSH tunnel 進去:

```bash
ssh -L 8929:localhost:8929 <your-user>@<your-server>
```

保持這條連線,瀏覽器開 `http://localhost:8929`。

## 9. binding 與 auto-pin(講過的不再提)

**分層記憶**(`memory/knowledge/*.md`，格式見 `memory/knowledge/TEMPLATE.md`):

- `binding` 恆常規範:scope 內**每次審查都注入**，且其 `suppress` 欄位列的檢核點會在**預掃層被確定性濾除**。例:內附的 `bind-reversal-netted.md`(使用者明確指示「交易資料已前置淨額」)抑制 H001 沖正檢核點——所以審查 R-201 這類聚合規則時**不會**再被問「沖正處理了嗎」。這是機制保證，不是靠模型讀了慣例後自律。
- `guideline` / `observed`:大量參考知識，依變更內容的 scope + 相關度檢索少量注入。
- 衝突時高權威者勝:使用者明確指示(40)> 人工撰寫(30)> 學習迴路草擬且經核准(20)> 程式碼觀察(10)。

**auto-pin**(規範自動定住;GitLab MR 就是人閘，merge 才生效):

```bash
# 審查人員講了一句常設規定 → AI 起草 binding → 開 MR 到知識庫 repo
.venv/bin/python scripts/autopin.py from-comment "沖正退匯前置系統都處理掉了，不用再問" --profile fast

# 同型 finding 被人工駁回 ≥2 次(record_feedback 累積)→ 自動起草抑制型 binding → 開 MR
.venv/bin/python scripts/autopin.py from-history

# 人在 GitLab 檢視該 MR、按 merge(或用指令模擬)，再同步回本地生效
.venv/bin/python scripts/autopin.py merge <iid>
.venv/bin/python scripts/autopin.py sync
```

另有離線版學習迴路 `scripts/consolidate_feedback.py`:從審查歷史提煉提案寫進 `memory/proposals/`，人工核准後移入 `memory/knowledge/`。兩者都**不做全自動合併**——一條規範釘錯就是永久錯，停在「AI 起草、人一鍵 merge」。

## 10. 疑難排解

| 症狀 | 檢查 / 處置 |
|---|---|
| 模型連不上(connection refused / timeout) | `curl $OLLAMA_URL` 的主機端(如 `curl http://127.0.0.1:11434/api/tags`)。經反向 tunnel 掛進來的 endpoint，確認 tunnel 活著;模型名不存在時先 `ollama pull` |
| 審查極慢或 OOM | 換小模型:`--profile fast`，或把 `config/models.yaml` 的 fast 換成更小的模型 |
| LLM 請求逾時(`APITimeoutError`) | 單次 LLM 請求上限預設 3600 秒、不自動重試;硬體更慢時以環境變數 `LLM_TIMEOUT`(秒)調高。測資生成/仲裁的 LLM 失敗不會中斷管線——降級為 major「執行驗證無法完成」轉 needs_human |
| webhook 回 401 | webhook 的 Secret token 與 `GITLAB_WEBHOOK_SECRET` 不一致，或啟動時忘了 export |
| webhook 完全收不到(GitLab 端顯示錯誤) | ① Admin Area → Settings → Network → Outbound requests 沒勾 **Allow requests to the local network from webhooks**;② URL 要用 `http://172.17.0.1:8000/...`(容器內打 `localhost` 是打容器自己);③ webhook server 沒起來(`curl http://127.0.0.1:8000/health`) |
| MR 觸發了但沒審 | webhook server 的 stdout:同 MR debounce 中、或 action 不在 open/reopen/update |
| 執行驗證報「無規格可驗」 | MR 的 SQL/標題/描述抓不到 `R-xxx` 規則碼，或 `specs/<code>.md` 不存在(real 模式看 GitLab repo 的 `specs/`;mock 模式看本地 `specs/`) |
| DuckDB transpile 失敗 / SQL 無法在測資上執行 | 報告會帶 major「SQL 無法在測資上執行」與錯誤訊息。常見:方言特有語法(sqlglot 轉不動)、用了 spec 資料表定義沒有的欄位。SQL 應以 postgres 方言撰寫，時間參數用 `:start_date`/`:end_date` |
| 測資生成一直失敗(測資生成失敗 finding) | 小模型產不出合法 JSON;把 `config/models.yaml` 的 `roles.testgen` 指到較大的 profile(預設 review) |
| GitLab 網頁打不開 | 先在該主機上 `docker ps -a --filter name=segcra-gitlab` 看容器起了沒;剛啟動要等 1~4 分鐘(ready 判準見 §8);GitLab 在遠端時,自己電腦要掛著 `ssh -L 8929:localhost:8929 <your-user>@<your-server>` |
| commit status 沒鎖住 Merge 按鈕 | 專案 Settings → Merge requests 沒勾 **Pipelines must succeed**;或審的是舊 commit(push 新 commit 後要重審) |

## 附錄:如何把工具層包回 MCP

本管線的工具是**行程內函式**(`toolbox/`)，不需要 MCP。什麼時候才需要包回去:

- 要把 sqltools / memory 等工具**掛進 MCP host**——VS Code Copilot agent mode、Claude Code、其他支援 MCP 的 IDE/agent——讓桌邊 agent 直接呼叫;
- 工具要**搬到遠端機器**跑(如 sqltools 搬去存放既有 rule-base 資料庫的主機)，用 MCP 當跨機協定。

因為領域邏輯都在 `toolbox/` 純模組，包回去只是薄薄一層轉發，一個檔案的事:

```python
#!/usr/bin/env python3
"""sqltools_mcp.py — 把 toolbox.sqltools 包成 stdio MCP server(薄轉接層)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 專案根目錄

from mcp.server.fastmcp import FastMCP
from toolbox.sqltools import lint, parse_ast, run_rules

mcp = FastMCP("sqltools", log_level="WARNING")

# 既有函式直接註冊轉發——docstring 就是工具描述，一行都不用改
mcp.tool()(parse_ast)
mcp.tool()(lint)
mcp.tool()(run_rules)

if __name__ == "__main__":
    mcp.run()   # stdio transport
```

requirements 要加回:

```
mcp>=1.2,<2        # 2.x 移除了 FastMCP,務必釘在 1.x
```

host 端設定範例(VS Code:`.vscode/mcp.json`;Copilot agent mode 會列出這些工具):

```jsonc
{
  "servers": {
    "sqltools": {
      "type": "stdio",
      "command": "${workspaceFolder}/.venv/bin/python",
      "args": ["${workspaceFolder}/mcp_adapters/sqltools_mcp.py"]
    }
  }
}
```

gitlab / memory 同法:import `toolbox/` 的既有函式 → `mcp.tool()` 逐一轉發 → `mcp.run()`。管線本身(`orchestrator/tool_hub.py`)維持行程內呼叫,不受影響——協定層永遠是可選件。
