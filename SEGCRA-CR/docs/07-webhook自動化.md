# 07-webhook 自動化

## 在幹嘛(一段白話)

`scripts/webhook_server.py` 是一個純 Python 標準庫寫成的 HTTP 服務,跑在能連到 GitLab 的內網主機上。GitLab 專案一有 MR 動作(開新 MR、重開、推新 commit)就打一個 webhook 過來,server 驗完密鑰後在背景執行整條審查管線(`orchestrator/pipeline.py:review_mr`),審完把行內留言、總評分數、`ai-review::<decision>` label 與 commit status 回寫到 GitLab。開發者留言 `/review` 也能手動觸發重審。搭配 GitLab 專案設定「Pipelines must succeed」+ protected branch,commit status(`segcra/review`)就成了 CE 版的 merge 閘門——不必等人手動跑指令,MR 進來自動被審。

## 防的是什麼

- **防「忘了審」**:審查若靠人手動觸發,漏跑一次就等於該 MR 完全沒有 AI 防線(漏報的最上游形態)。webhook 把觸發變成事件驅動,MR 一動就審。
- **防外部亂打**:server 監聽 `0.0.0.0`,任何能連到這台機器的人都打得到 endpoint。沒有 token 驗證的話,攻擊者可以偽造 payload 對任意 MR 觸發審查(浪費 GPU 資源)、甚至藉由灌事件干擾正常審查節奏。這裡用密鑰比對確定性擋掉,不是靠「應該沒人知道這個 URL」。
- **防自我觸發迴圈**:審查完成會回寫留言,留言又會發 note webhook——若 note 事件無條件重審,就是無限迴圈燒 GPU。所以 note 事件只認明確的 `/review` 指令,且 bot 帳號(username 以 `ai-` 或 `root` 開頭)的留言一律忽略。這是機制擋,不是期待模型或人「小心一點」。
- **防重複觸發**:GitLab 對同一 MR 短時間可能連發多個 update 事件。只有**真的推了新 commit**(事件帶 `oldrev`)才會重審,其餘 update(改 label、標題、描述、指派人 —— 包括審查自己最後加 label 那一下)一律忽略。推了新 commit 時取消進行中的那次、改審新的:舊的那次審的是舊程式,結論貼到新 commit 上是錯的。

## 怎麼建的

核心程式全在 `scripts/webhook_server.py`,單檔、無第三方依賴。

**Token 驗證(`Handler.do_POST`)**:GitLab webhook 會把專案設定裡填的 Secret token 原文放在 `X-Gitlab-Token` header。server 讀環境變數 `GITLAB_WEBHOOK_SECRET`,用 `hmac.compare_digest(token, SECRET)` 做常數時間比對(防 timing attack;注意這是明文密鑰比對,不是 HMAC 簽章——GitLab CE 的 webhook 就是送原文)。`SECRET` 未設或比對失敗一律回 401。啟動時若沒 export 密鑰,`main()` 會印警告「所有 webhook 請求都會被拒絕」。

**事件路由(`Handler.do_POST`)**:只收 `POST /gitlab-webhook`(其他路徑 404),依 payload 的 `object_kind` 分流:

1. `merge_request`:取 `object_attributes.action` 與 `iid`,只有 `action ∈ TRIGGER_ACTIONS = {"open", "reopen", "update"}` 才觸發(approve、close、merge 等動作回 `ignored (action=...)`)。
2. `note`:必須同時滿足四個條件才重審——`noteable_type == "MergeRequest"`、payload 帶 `merge_request.iid`、留言文字含 `/review`、留言者 username 不以 `ai-`/`root` 開頭(`author_bot` 判斷)。最後一條就是防迴圈:審查器自己回寫的留言不會再觸發自己。
3. 其他 kind 回 `ignored (kind=...)`,一律 200(GitLab 收到非 2xx 會標記 webhook 失敗並重試/停用)。

**取消舊審查(`_trigger` / `_cancel`)**:模組層一個 `_inflight: dict`({mr_iid: (event_loop, task)})配 `threading.Lock`。`_trigger(mr_iid, head_sha)` 先試著取消同一個 MR 進行中的那次(跨執行緒要用 `loop.call_soon_threadsafe(task.cancel)`,task 屬於那個執行緒的迴圈),再開一條新的 thread。取消只會在下一個真正的等待點生效,所以舊的那次有可能一路走到回寫 —— `review_mr` 在回寫前會比對 MR 目前的 head sha,發現已被取代就不貼(見 `orchestrator/pipeline.py:_superseded`)。

**背景執行(`_trigger` → `_run_review`)**:審查耗時數十秒到數分鐘,而 GitLab webhook 有逾時限制,所以 `_trigger` 起一個 `threading.Thread(daemon=True)` 跑 `_run_review` 後**立刻**回 200。`_run_review` 內用 `asyncio.run(review_mr(_cfg, str(mr_iid), profile_name=PROFILE))` 跑完整管線(profile 由 `REVIEW_PROFILE` 環境變數決定,預設 `review`)。HTTP 服務本身用 `ThreadingHTTPServer`,審查進行中仍能收後續事件。

**Graceful 失敗(`_run_review`)**:整段包在 try/except——模型連不上、fixture 不存在、管線任何 exception 都只印 `[webhook] MR !N 審查失敗:...` 到 stdout,server 不死;`finally` 保證把 iid 從 `_inflight` 移除,失敗的 MR 之後還能再觸發。payload 不是合法 JSON 回 400。`log_message` 覆寫為靜音,stdout 只留自己的結構化 print。

**資料流**:GitLab 事件 → `POST /gitlab-webhook`(驗 token)→ 路由抽出 `mr_iid`(update 事件另外看 `oldrev`)→ 取消同 MR 進行中的審查 → 背景 thread 跑 `review_mr` → 管線經 `toolbox/gitlab.py` 回寫 GitLab:逐條 finding 行內留言、總評留言(決策文字)、`ai-review::<decision>` label、commit status `segcra/review`(`auto_approved` → success,`needs_human`/`blocked` → failed,description 帶原因;見 `orchestrator/pipeline.py` 決策回寫段)。

**GitLab 端對應設定**(README 第 6 節有實測步驟):

1. Admin Area → Settings → Network → Outbound requests → 勾 **Allow requests to the local network from webhooks and integrations**(GitLab 容器要打回同機內網位址,不開會被 GitLab 自己擋)。
2. 專案 → Settings → Webhooks → Add new webhook:URL 填 `http://172.17.0.1:8000/gitlab-webhook`(容器打回宿主機走 docker bridge gateway;容器內打 `localhost` 是打容器自己)、Secret token 填與 `GITLAB_WEBHOOK_SECRET` 相同的值、勾 **Merge request events** 與 **Comments**(後者供 `/review` 重審)。
3. 建好後用該 webhook 的 Test → Merge request events 驗證,server stdout 應出現觸發紀錄。

## 對決策的影響

webhook server 本身不產生 finding、不動 severity、不參與三態決策的任何條件——它是觸發器與回寫通道。決策(`auto_approved` / `needs_human` / `blocked`)完全由管線的 `apply_policy`(`orchestrator/pipeline.py`)確定性計算。webhook 的貢獻在**決策的落地**:把決策轉成 commit status(只有 `auto_approved` 是 success),配合「Pipelines must succeed」讓 `needs_human` 與 `blocked` 的 MR 在 GitLab 上實際按不下 merge。換句話說,它讓三態決策從「一段報告文字」變成「merge 按鈕的狀態」。

## 怎麼驗證它在動

不需要 GitLab、不需要模型就能驗證路由/token 驗證/取消重審三件事(mock 模式,`GITLAB_URL` 不設):

```bash
cd SEGCRA-CR
export GITLAB_WEBHOOK_SECRET=test123
.venv/bin/python scripts/webhook_server.py --port 8000
# 另開 shell:

# 1. 健康檢查
curl -s http://127.0.0.1:8000/health          # → healthy

# 2. token 錯 → 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/gitlab-webhook \
  -H 'X-Gitlab-Token: wrong' -d '{}'          # → 401,server 印「token 驗證失敗,拒絕」

# 3. MR open 事件觸發
curl -s -X POST http://127.0.0.1:8000/gitlab-webhook \
  -H 'X-Gitlab-Token: test123' -H 'Content-Type: application/json' \
  -d '{"object_kind":"merge_request","object_attributes":{"action":"open","iid":1}}'
# → "review triggered for MR !1";server 印「→ 觸發審查 MR !1」

# 4. 立刻重發同一事件(帶 oldrev)→ 取消舊的、改審新的
#(重跑上一條 curl)→ server 印「MR !1 取消進行中的舊審查,改審新的」

# 5. 不觸發的 action
curl -s -X POST http://127.0.0.1:8000/gitlab-webhook \
  -H 'X-Gitlab-Token: test123' -H 'Content-Type: application/json' \
  -d '{"object_kind":"merge_request","object_attributes":{"action":"approved","iid":1}}'
# → "ignored (action=approved)"

# 6. note 事件:含 /review 且非 bot → 觸發;bot 留言 → ignored
curl -s -X POST http://127.0.0.1:8000/gitlab-webhook \
  -H 'X-Gitlab-Token: test123' -H 'Content-Type: application/json' \
  -d '{"object_kind":"note","user":{"username":"ai-reviewer"},"merge_request":{"iid":1},"object_attributes":{"noteable_type":"MergeRequest","note":"/review"}}'
# → "ignored (note)"
```

步驟 3 的背景審查在 mock 模式會去讀 `fixtures/mr_1.json`——該檔不存在,所以 server 會印「MR !1 審查失敗:...」後把 iid 從 `_inflight` 移除,這同時驗證了 graceful 失敗與 `_inflight` 的清理(再發一次事件又能觸發)。要跑完整審查:`cp fixtures/mr_001.json fixtures/mr_1.json` 且模型 profile 可用,審完 stdout 會印「MR !1 審完:決策=... 分數=... findings=...」,結果落在 `review_output/mr_1_review.json`。接真 GitLab 則用 webhook 設定頁的 Test 按鈕。

## 時間上限與未完成的處理(#45 / #53 review)

預設值與單位見 **[設定參數一覽](10-設定參數.md)**,這裡只寫各管什麼、什麼時候要調。

| 環境變數 | 管什麼 | 什麼時候要調 |
|---|---|---|
| `REVIEW_TIMEOUT_SECONDS` | **一份審查的總上限**(`_run_review` 的 `asyncio.wait_for`) | 要比執行驗證那一段的上限大。模型變慢、或規格變大而生成時間拉長時往上調 |
| `LINT_TIMEOUT_SECONDS` | 單一份內容的 lint 上限(預掃每個 `.sql` 檔各一次,模型的 `sqltools__lint` 工具呼叫每次一次),見 `toolbox/sqltools.py` | 一般的 SQL 在預設值內跑得完;逾時的檔案在報告記「審查未完整」並交人工。正常的大檔也逾時時往上調 |
| `SPEC_EXEC_TIMEOUT_SECONDS` | 執行驗證整段(測資生成 + 沙盒執行 + 仲裁)的上限,見 `orchestrator/pipeline.py` | 照 `_spec_exec.stage_seconds` 量到的各階段時間訂。大規格的測資生成在現在的機器上幾乎一定會逾時 |
| `MAIN_REVIEW_TIMEOUT_SECONDS` | 主審查(模型呼叫)的上限,預設整份 − 600 秒(見 `docs/10`) | 單次請求的 `LLM_TIMEOUT`(3600)比整份的上限長:端點收了連線卻不回時,沒有這個上限就會等到整份被取消、確定性結果跟著消失。逾時或連不上時,之後的冷讀與執行驗證不再呼叫模型,記成環節未完整 |
| `COLD_READ_TIMEOUT_SECONDS` | 冷讀在主審查之後依序跑的時間上限,從冷讀開始算(見 `docs/03`) | 逾時記成環節未完整、交人工。端點很慢、冷讀常逾時就往上調 |

**整份審查的截止時間**(`pipeline.REVIEW_BUDGET_SECONDS`):主審查、冷讀、執行驗證各自的上限加起來
(1800 + 600 + 1200)比整份的 2400 秒長,端點沒掛、只是慢時,外層會先把整份取消,確定性的結果跟著消失。
所以 `review_mr` 一開始就算出截止時間 = `REVIEW_TIMEOUT_SECONDS` 減掉收尾的時間(評分、決策、把每條
finding 貼回 MR、設 label 與 commit status;收尾保留 `FINISH_RESERVE_SECONDS` 預設 300 秒,最多整份的四分之一)。
這三步的上限都取「自己的上限」與「離截止還剩多少」的較小值;剩不到 `MIN_STAGE_SECONDS`(預設 30 秒)就不開始,
記成環節未完整、寫出原因;上限被截止時間縮短而逾時時,說明會寫明「被整份審查的截止時間縮短了(原本是 N 秒)」。執行驗證交給沙盒的截止時間也跟著縮。主審查因為時間不夠而沒有呼叫時,說明寫
「整份審查剩下的時間不夠」,不寫成端點的問題。

每份報告記下各階段花了幾秒(`_stage_seconds`:`prepare` 取 MR、預掃、找規格,`main_review`、`cold_read`、
`postprocess` 後處理,`spec_exec`,`finish` 收尾含回寫,`total`)與這輪的截止時間(`_review_deadline_seconds`)。
各步的上限與收尾保留照這些數字調。

### 收尾保留怎麼訂(量過)

收尾 = 執行驗證結束之後到回傳:校準、評分、決策、比對 MR 的 head(重新取一次 MR 與每個檔案)、
逐條貼回(每條 2 個請求:取 MR 的 `diff_refs`、開行內討論;沒有行號的貼一般留言)、總結、label、commit status。
跟模型無關,時間幾乎都花在 GitLab 的請求上。

量法:`eval/measure_finish.py` 跑真的 `review_mr` 與真的 `toolbox/gitlab.py`(real 模式),GitLab 換成本機的
假伺服器、每個請求固定延遲;模型的三步換成立刻回傳的替身。finding 數用檔案數控制(每個檔案一句沒有 WHERE 的
DELETE,另外主審查回 20 條)。

```bash
python eval/measure_finish.py --files 10 50 100 200 --latency 0 0.1 0.3
```

開發機(Windows)10/25 量到的收尾秒數:

| finding 數 | 收尾的 GitLab 請求數 | 延遲 0 | 每個請求 0.1 秒 | 每個請求 0.3 秒 |
|---|---|---|---|---|
| 29 | 73 | 1.0 | 8.5 | 23.1 |
| 69 | 193 | 2.8 | 22.2 | 61.0 |
| 119 | 343 | 4.4 | 39.5 | 108.4 |
| 219 | 643 | 9.5 | 74.1 | 202.8 |

請求數是延遲 0.1、0.3 秒那兩輪的;延遲 0 那一輪有幾條行內討論在用戶端失敗、退成一般留言,多出 1–12 個請求。
收尾 ≈ 請求數 × 單次延遲 + 每條 finding 約 0.04 秒;請求數 ≈ 2 × finding 數 + 檔案數 + 5。
**預設 300 秒**:單次延遲 0.3 秒時夠 300 條上下的 finding,0.1 秒時夠 800 條上下;一般的 MR 遠低於這個數。
部署端先量一次 GitLab API 的延遲(例如 `GET /projects/:id/merge_requests/:iid` 的回應時間),用
`--latency` 帶進去重跑,或直接照上面的式子算;超過就調大 `FINISH_RESERVE_SECONDS`。

另外量到的一件事:**收尾目前打不斷**。回寫是同步的工具呼叫(`ToolHub.call` 直接呼叫同步函式),中間沒有
讓出事件迴圈,外層 `asyncio.wait_for` 的時限在回寫途中到了也插不進去,回寫會做完、結果照常回傳,只是整份
超過 `REVIEW_TIMEOUT_SECONDS` 才結束(Python 3.11、3.13 實測:外層 1 秒、回寫 10 次各 0.2 秒,10 次都貼了,
2.5 秒後正常回傳)。所以收尾超過保留時不會讓結果消失。**數字不靠這個性質訂**:回寫日後改成非同步(例如
丟到執行緒)時就打得斷了,保留要照真的會花的時間留。

### 每一步的上限與最少秒數

`MIN_STAGE_SECONDS`(30 秒)是「剩這麼少就不開始」:冷讀關掉思考後,10/24 量到的中位數是 golden 12 秒、
公開題 29 秒,剩不到 30 秒開始多半跑不完。主審查 1800、冷讀 600、執行驗證 1200 與這個值都跟模型主機的速度
有關,開發環境量到的只能當參考,**部署端用正式的上限跑一輪評測題、看 `stage_time_summary.py` 的各階段
P90 與最大值再調**(見下一節)。

### 評測的截止時間

**評測平常把整份的上限設大,量審查品質;時間另外統計。** 黑盒也照這個做:

```bash
REVIEW_TIMEOUT_SECONDS=86400 python eval/run_eval.py --dump-reports out/
python eval/stage_time_summary.py out/          # 各階段耗時,與照正式截止時間會被縮短的份數
```

為什麼不照正式的截止時間跑:

- **開發與黑盒的模型主機比正式環境慢,而且是共用的**(排隊、別人也在跑)。照正式的上限跑,慢的題目後面的
  步驟被縮短或不開始、變成「未完整」:量到的是那天主機忙不忙,同一份程式兩次評測的分數會因為負載不同而不同,
  品質的比較就失去意義。
- **跟之前的成績可比**:10/23 以前的評測沒有整份的截止時間;冷讀 600、執行驗證 1200 這兩個各自的上限照舊有。
  設成 86400 時主審查的上限跟著變成 85800 秒,等於跟以前一樣不限(單次請求仍有 `LLM_TIMEOUT`)。

時間的問題不會因此看不到:`stage_time_summary.py` 讀每份報告的 `_stage_seconds`,列出各階段的中位數、P90、
最大值,以及「照正式的截止時間(`--budget`,預設 2100;主審查 `--main-limit`,預設 1800)跑會被縮短或不開始」
的份數與先碰到的是哪一步。評測主機較慢,這個份數是正式環境的上限,拿來看趨勢(某次改動讓時間明顯變長);
要拿來調上限的數字,在部署端用正式的上限量。

預設值寫在指令上、不寫進 `run_eval.py`:`run_eval.py` 在 import 時設環境變數,測試也會 import 它,
把整份的上限放在那裡會讓同一輪測試裡別的測試讀到的上限依 import 順序而不同。忘了帶時,
`stage_time_summary.py` 會提醒(報告記的截止時間不比正式的長,品質的數字裡混了時間的影響)。

都是「讀不懂就退回預設值並印一行警告」(`orchestrator/config.py:env_seconds`),
**不是**直接丟例外:讀它們的是 webhook 伺服器在 import 的時候,填錯一個字就起不來 →
所有 MR 都收不到審查,那比用預設值跑糟得多。0 或負數也退回預設(上限設成 0 等於每次
都立刻逾時,不會是誰的本意)。

審查沒跑完時,webhook 會做兩件事(`_notify_incomplete`):在 MR 上留言,**並把
`segcra/review` 這個 commit status 設成失敗**。留言只是給人看的,擋合併的是 commit
status(專案設定「Pipelines must succeed」);只留言不設狀態,等於審查沒跑完卻沒有
東西擋著合併。

審查模型的端點連不上、逾時或回錯誤時,管線**不會**丟例外到這裡:它照常產出報告(程式確定性檢查的
結果加上一條「審查模型沒有完成」的 major),照常回寫,決策是交人工,commit status 一樣是失敗
(見 04 篇 enforce_truncation)。走到這裡的「審查沒跑完」是總時間上限、或管線本身的錯誤。

## 擴充與注意

- **加/減觸發動作**:改 `TRIGGER_ACTIONS`(`scripts/webhook_server.py` 模組層)。
  注意 `update` 另有一道判斷:**只有 `object_attributes.oldrev` 有值(= 真的推了新
  commit)才會重審**。GitLab 改 label、標題、描述、指派人都送 `action=update`,包括
  審查自己最後 `set_label` 那一下——不分辨的話每個 MR 至少多跑一次完整審查,而且兩次
  決策不同時 label 又變、再觸發一次。
- **改 bot 判斷**:目前用 username 前綴 `("ai-", "root")` 硬編碼在 note 分支，換 bot 帳號命名時要同步改,否則可能迴圈或誤擋真人。
- **改審查 profile / 密鑰**:環境變數 `REVIEW_PROFILE`、`GITLAB_WEBHOOK_SECRET`，不用改碼。
- **已知限制(單機、無佇列)**:事件只存在 `_inflight` 記憶體集合裡，server 重啟即遺失;審查中收到同 MR 的**新 commit** 事件會取消舊的那次、改審新的(舊的那次審的是舊程式,結論貼到新 commit 上是錯的),而舊的那次若較晚走到回寫,`review_mr` 會比對 MR 目前的 head sha、發現已被取代就不回寫;多 MR 同時觸發就同時起多條管線，沒有併發上限，單機 GPU 可能被打滿;server 掛掉期間的事件不會重放(GitLab 端有重試但次數有限)。要上量得換成佇列(如一個簡單的 job queue + worker)並把 in-flight 狀態持久化。
- **安全邊界**:token 驗證只擋「誰能觸發」，MR 內容本身的提示注入是管線內 `scan_mr` + `enforce_injection` 的事(見 `orchestrator/pipeline.py`)，兩層互不取代。
