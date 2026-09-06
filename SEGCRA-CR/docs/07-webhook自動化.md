# 07-webhook 自動化

## 在幹嘛(一段白話)

`scripts/webhook_server.py` 是一個純 Python 標準庫寫成的 HTTP 服務,跑在能連到 GitLab 的內網主機上。GitLab 專案一有 MR 動作(開新 MR、重開、推新 commit)就打一個 webhook 過來,server 驗完密鑰後在背景執行整條審查管線(`orchestrator/pipeline.py:review_mr`),審完把行內留言、總評分數、`ai-review::<decision>` label 與 commit status 回寫到 GitLab。開發者留言 `/review` 也能手動觸發重審。搭配 GitLab 專案設定「Pipelines must succeed」+ protected branch,commit status(`segcra/review`)就成了 CE 版的 merge 閘門——不必等人手動跑指令,MR 進來自動被審。

## 防的是什麼

- **防「忘了審」**:審查若靠人手動觸發,漏跑一次就等於該 MR 完全沒有 AI 防線(漏報的最上游形態)。webhook 把觸發變成事件驅動,MR 一動就審。
- **防外部亂打**:server 監聽 `0.0.0.0`,任何能連到這台機器的人都打得到 endpoint。沒有 token 驗證的話,攻擊者可以偽造 payload 對任意 MR 觸發審查(浪費 GPU 資源)、甚至藉由灌事件干擾正常審查節奏。這裡用密鑰比對確定性擋掉,不是靠「應該沒人知道這個 URL」。
- **防自我觸發迴圈**:審查完成會回寫留言,留言又會發 note webhook——若 note 事件無條件重審,就是無限迴圈燒 GPU。所以 note 事件只認明確的 `/review` 指令,且 bot 帳號(username 以 `ai-` 或 `root` 開頭)的留言一律忽略。這是機制擋,不是期待模型或人「小心一點」。
- **防重複觸發**:GitLab 對同一 MR 短時間可能連發多個 update 事件(推多個 commit、改標題),每發都起一輪審查會把單機資源打爆。`_inflight` 集合做 debounce,同一 MR 審查中就略過。

## 怎麼建的

核心程式全在 `scripts/webhook_server.py`,單檔、無第三方依賴。

**Token 驗證(`Handler.do_POST`)**:GitLab webhook 會把專案設定裡填的 Secret token 原文放在 `X-Gitlab-Token` header。server 讀環境變數 `GITLAB_WEBHOOK_SECRET`,用 `hmac.compare_digest(token, SECRET)` 做常數時間比對(防 timing attack;注意這是明文密鑰比對,不是 HMAC 簽章——GitLab CE 的 webhook 就是送原文)。`SECRET` 未設或比對失敗一律回 401。啟動時若沒 export 密鑰,`main()` 會印警告「所有 webhook 請求都會被拒絕」。

**事件路由(`Handler.do_POST`)**:只收 `POST /gitlab-webhook`(其他路徑 404),依 payload 的 `object_kind` 分流:

1. `merge_request`:取 `object_attributes.action` 與 `iid`,只有 `action ∈ TRIGGER_ACTIONS = {"open", "reopen", "update"}` 才觸發(approve、close、merge 等動作回 `ignored (action=...)`)。
2. `note`:必須同時滿足四個條件才重審——`noteable_type == "MergeRequest"`、payload 帶 `merge_request.iid`、留言文字含 `/review`、留言者 username 不以 `ai-`/`root` 開頭(`author_bot` 判斷)。最後一條就是防迴圈:審查器自己回寫的留言不會再觸發自己。
3. 其他 kind 回 `ignored (kind=...)`,一律 200(GitLab 收到非 2xx 會標記 webhook 失敗並重試/停用)。

**Debounce(`_trigger`)**:模組層一個 `_inflight: set` 配 `threading.Lock`。`_trigger(mr_iid)` 先在鎖內檢查 iid 是否在集合中,在就回 `already running` 不重複起審;不在就加入集合再開 thread。

**背景執行(`_trigger` → `_run_review`)**:審查耗時數十秒到數分鐘,而 GitLab webhook 有逾時限制,所以 `_trigger` 起一個 `threading.Thread(daemon=True)` 跑 `_run_review` 後**立刻**回 200。`_run_review` 內用 `asyncio.run(review_mr(_cfg, str(mr_iid), profile_name=PROFILE))` 跑完整管線(profile 由 `REVIEW_PROFILE` 環境變數決定,預設 `review`)。HTTP 服務本身用 `ThreadingHTTPServer`,審查進行中仍能收後續事件。

**Graceful 失敗(`_run_review`)**:整段包在 try/except——模型連不上、fixture 不存在、管線任何 exception 都只印 `[webhook] MR !N 審查失敗:...` 到 stdout,server 不死;`finally` 保證把 iid 從 `_inflight` 移除,失敗的 MR 之後還能再觸發。payload 不是合法 JSON 回 400。`log_message` 覆寫為靜音,stdout 只留自己的結構化 print。

**資料流**:GitLab 事件 → `POST /gitlab-webhook`(驗 token)→ 路由抽出 `mr_iid` → debounce → 背景 thread 跑 `review_mr` → 管線經 `toolbox/gitlab.py` 回寫 GitLab:逐條 finding 行內留言、總評留言(決策文字)、`ai-review::<decision>` label、commit status `segcra/review`(`auto_approved` → success,`needs_human`/`blocked` → failed,description 帶原因;見 `orchestrator/pipeline.py` 決策回寫段)。

**GitLab 端對應設定**(README 第 6 節有實測步驟):

1. Admin Area → Settings → Network → Outbound requests → 勾 **Allow requests to the local network from webhooks and integrations**(GitLab 容器要打回同機內網位址,不開會被 GitLab 自己擋)。
2. 專案 → Settings → Webhooks → Add new webhook:URL 填 `http://172.17.0.1:8000/gitlab-webhook`(容器打回宿主機走 docker bridge gateway;容器內打 `localhost` 是打容器自己)、Secret token 填與 `GITLAB_WEBHOOK_SECRET` 相同的值、勾 **Merge request events** 與 **Comments**(後者供 `/review` 重審)。
3. 建好後用該 webhook 的 Test → Merge request events 驗證,server stdout 應出現觸發紀錄。

## 對決策的影響

webhook server 本身不產生 finding、不動 severity、不參與三態決策的任何條件——它是觸發器與回寫通道。決策(`auto_approved` / `needs_human` / `blocked`)完全由管線的 `apply_policy`(`orchestrator/pipeline.py`)確定性計算。webhook 的貢獻在**決策的落地**:把決策轉成 commit status(只有 `auto_approved` 是 success),配合「Pipelines must succeed」讓 `needs_human` 與 `blocked` 的 MR 在 GitLab 上實際按不下 merge。換句話說,它讓三態決策從「一段報告文字」變成「merge 按鈕的狀態」。

## 怎麼驗證它在動

不需要 GitLab、不需要模型就能驗證路由/驗證/debounce 三件事(mock 模式,`GITLAB_URL` 不設):

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

# 4. 立刻重發同一事件 → debounce
#(重跑上一條 curl)→ "already running";server 印「MR !1 審查中,略過(debounce)」

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

步驟 3 的背景審查在 mock 模式會去讀 `fixtures/mr_1.json`——該檔不存在,所以 server 會印「MR !1 審查失敗:...」後把 iid 從 `_inflight` 移除,這同時驗證了 graceful 失敗與 debounce 解除(再發一次事件又能觸發)。要跑完整審查:`cp fixtures/mr_001.json fixtures/mr_1.json` 且模型 profile 可用,審完 stdout 會印「MR !1 審完:決策=... 分數=... findings=...」,結果落在 `review_output/mr_1_review.json`。接真 GitLab 則用 webhook 設定頁的 Test 按鈕。

## 擴充與注意

- **加/減觸發動作**:改 `TRIGGER_ACTIONS`(`scripts/webhook_server.py` 模組層)。
- **改 bot 判斷**:目前用 username 前綴 `("ai-", "root")` 硬編碼在 note 分支，換 bot 帳號命名時要同步改,否則可能迴圈或誤擋真人。
- **改審查 profile / 密鑰**:環境變數 `REVIEW_PROFILE`、`GITLAB_WEBHOOK_SECRET`，不用改碼。
- **已知限制(單機、無佇列)**:事件只存在 `_inflight` 記憶體集合裡，server 重啟即遺失;審查中收到同 MR 的新 commit 事件會被 debounce **丟棄而不是排隊**，審完不會補審最新版(需要再推一次 commit 或留言 `/review`);多 MR 同時觸發就同時起多條管線，沒有併發上限，單機 GPU 可能被打滿;server 掛掉期間的事件不會重放(GitLab 端有重試但次數有限)。要上量得換成佇列(如一個簡單的 job queue + worker)並把 in-flight 狀態持久化。
- **安全邊界**:token 驗證只擋「誰能觸發」，MR 內容本身的提示注入是管線內 `scan_mr` + `enforce_injection` 的事(見 `orchestrator/pipeline.py`)，兩層互不取代。
