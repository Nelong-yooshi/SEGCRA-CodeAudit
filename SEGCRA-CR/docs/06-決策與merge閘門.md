# 06-決策與 merge 閘門

## 在幹嘛(一段白話)

LLM 審完、執行驗證跑完之後,管線用兩個**確定性函式**把「這個 MR 接下來怎麼走」算出來:`apply_rubric` 依 finding 的 severity 組成重算分數與 verdict(模型自評分數只留檔不採用),`apply_policy` 依客觀訊號(diff 行數、是否新規則檔、rubric 分數、severity 組成、未回應檢核點、執行驗證結果)判成三態之一——`auto_approved` / `needs_human` / `blocked`。審查是部署端 MR pipeline 最後一個 stage 的 job,決策變成 job 的結束碼,配合 GitLab 的「Pipelines must succeed」與 protected branch,讓 `blocked` 與沒跑完的審查**實際按不下 Merge 鈕**、`needs_human` 走既有的人工合併流程,而不是只留一句留言(見第 4 節;不接 CI 時改用 commit status,第 5 節)。

## 防的是什麼

- **分數漂移**:實測同一個 MR,模型自評分數逐輪不一致(同一 MR 曾出現 50 vs 10)。裁決若掛在模型自評上,今天 needs_human、明天 auto_approved。所以分數由 rubric 從 severity 確定性重算(`apply_rubric` docstring 即記載此起因)。
- **模型被壓制後「假通過」**:提示注入若讓模型輸出 score 100 / findings 空,決策層還有 enforce 鏈補回的確定性 blocker(注入掃描命中必進 findings)→ `min_blockers` 條件把它擋成 `blocked`,與模型嘴上說什麼無關。
- **「小改」偽裝**:自動放行只給小變更,但 diff 行數本身可被佔位 diff(`(generated)`)騙過;`apply_policy` 對不可靠 diff 退回 `full_content` 行數計算,新規則檔另有一條獨立的 `new_rule` 硬條件。
- **只看靜態審查就放行**:SQL「看起來對」不代表「跑起來對」。`_spec_exec.passed` 是寫死在管線的硬條件(config 關不掉),沒通過執行驗證的 MR 永遠不會自動放行。
- **決策做了卻擋不住人**:三態若只寫在留言裡,誰都能照樣按 Merge。所以決策落到 pipeline 裡那個 job 的結果,靠 GitLab 自身的 merge check 生效——這一段是機制,不是模型自律,也不是審查者自律。
- **MR 把審查 job 換掉**:CI 設定留在待審 repo 時,MR 改設定檔就能把審查 job 換成 `exit 0`;用 `include` 把 repo 的設定帶進來,同名 job 會被逐欄蓋掉。所以設定放在 MR 改不到的專案、不 include(第 4 節)。
- **前面的 job 改後面 job 的執行環境**:同一台 runner、同一個帳號時,前面的 job 留一個假的 `curl`,審查 job 不連服務就回報成功;別的專案的 job 帶上同一個 tag 也能跑到那台。所以審查 job 跑在只屬於待審專案的專用 runner(第 4 節)。
- **runner 上的秘密被前面的 job 拿走**:審查不在 runner 上跑,GitLab token 與模型通行碼只在審查服務那台(第 4 節)。
- **別的 job 拿自己的 token 呼叫 `/ci/review`**:服務向 GitLab 確認呼叫者是這個專案、這個 MR 的 MR pipeline 正在跑的 job,不符回 403、查不到回 502(第 4 節、07 篇)。

## 怎麼建的

### 1. `apply_rubric`(`orchestrator/pipeline.py:apply_rubric`)

輸入:後處理完(sanitize、enforce 鏈、spec_exec findings 已併入)的 report。步驟:

1. 統計 findings 的 severity:`counts = {blocker, major, minor, info}`。
2. 公式:`score = max(0, 100 − 40×blocker − 15×major − 5×minor)`;info 不扣分。
3. **blocker 封頂**:只要有任何 blocker,`score = min(score, 59)`——不管其他組成多乾淨,有 blocker 就不可能及格到看起來能過。
4. verdict 確定性判:`score ≥ 80 且無 blocker 且無 major → approve`;`score < 20 → reject`;其餘 `needs_changes`。
5. 模型自己填的分數搬到 `report["_model_score"]` 留檔稽核,`report["score"]` / `report["verdict"]` 一律覆寫為 rubric 計算值。

為什麼不信模型自評:同一輸入自評會漂(見上),而 severity 是逐條 finding 的離散標記,錯了可以逐條追溯、逐條吵,分數則是黑箱。rubric 讓「分數 = severity 組成的函數」,可重現、可解釋。

### 2. `apply_policy`(`orchestrator/pipeline.py:apply_policy`)

輸入:report(rubric 已算完)、mr、`cfg.policy`(來自 `config/models.yaml` 的 `policy:` 段)。訊號計算:

- `severities`:findings 的 severity 集合。
- `pending_hints`:任一 finding 標題含「檢核點待人工確認」(這是 `enforce_hints` 補報未回應 hint 時用的固定標題)。
- `diff_lines`:逐檔 `_change_size` 加總——先數 diff 的 `+` 新增行;若新增行為 0 或 diff 含 `generated` 佔位(規則生成器產的 fixture 就是這樣),退回 `full_content` 行數。否則一整條新規則會被佔位 diff 誤判成 0 行小改而自動放行。
- `new_rule`:任一變更檔路徑含 `rules/` 且(diff 以 `@@ -0,0` 開頭 = 新建檔,或 diff 含 `generated`)。新業務規則影響通報結果,再小也至少人工過目;`sql/maintenance/` 下的小維護腳本不受此限。
- `spec_exec_ok`:`report["_spec_exec"]["passed"] is True`(嚴格比對 True;沒跑、沒 spec、任何失敗都不算)。

判定順序(短路):

1. **blocked**:blocker 數 ≥ `policy.block.min_blockers`(models.yaml 設 1)→ 直接 `blocked`,不再看其他條件。
2. **auto_approved**:以下**全部**成立——
   - `diff_lines ≤ policy.auto_approve.max_diff_lines`(models.yaml 設 30;policy 沒設時預設 0,等於不放行)
   - `not new_rule`
   - `spec_exec_ok`(**硬條件,寫死在程式,models.yaml 的註解也明講不可設定關閉**)
   - 下面幾項也是**寫死在程式的硬條件**,不可由設定關閉:
     - 沒有 `_needs_human`(無規格可對照、規格未載明等「意圖無法確認」的理由)
     - `not spec_changed`(MR 改到規格檔)
     - `not incomplete`(有環節沒完成,見 `review_incomplete`)
     - `not rule_hits`:規則層(R 系列)沒有任何命中,資安與環境相依的檢核點(`_HARD_HINT_CODES`:
       H004 撈憑證/密碼類欄位、H006 檢查物件存不存在、H007 讀語系設定、H008 dbt 編譯期的環境資訊、
       H009 現在時間與寫死的日期比較)也沒有。`report["_rule_hits"]` 由程式從預掃結果算
       (`_rule_hit_codes`,呼叫到的 macro 的命中也算),模型輸出的同名欄位會被蓋掉;不看 finding 的標題或
       嚴重度(模型可以自己寫一條同標題的 finding,或把命中寫成 info)。彙整這一步出錯時記成環節未完整
       - 這幾個檢核點不看模型有沒有回應:「有沒有回應」是同一個檔的 finding 有沒有含關鍵詞
         (見 04 篇的已知限制),一條 info 寫「密碼欄位存的是雜湊值,沒有問題」就算回應過 H004。
         H006–H009 會讓沙盒與正式環境走不同分支,執行驗證驗不到。其他檢核點(H001–H003、H005)照
         `pending_hints` 與 `forbid_pending_hints`
     - `not cold_read`:冷讀沒有命中、也看完了(`report["_cold_read"]` 的 `flagged`、`truncated`)
   - `score ≥ policy.auto_approve.min_score`(models.yaml 設 95;沒設時預設 101,等於不放行)
   - `severities ⊆ policy.auto_approve.allowed_severities`(models.yaml 只允許 `[info]`)
   - `not (forbid_pending_hints and pending_hints)`(models.yaml 設 true:有待確認檢核點一律不放行)
3. 其餘 → `needs_human`。
4. 訊號寫進 `report["_policy_signals"]`(change_lines / new_rule / severities / pending_hints / spec_exec_passed /
   needs_human / spec_changed / incomplete / rule_hits / cold_read)供稽核,決策寫 `report["decision"]`。`policy` 段整個缺席時直接 `needs_human`(fail-safe)。

與 `config/models.yaml` 的對應:`policy.auto_approve.{max_diff_lines, min_score, allowed_severities, forbid_pending_hints}` 與 `policy.block.min_blockers` 一一對應上述條件;寫死在程式的那幾項(`spec_exec_ok`、`_needs_human`、`spec_changed`、`incomplete`、`rule_hits`、`cold_read`)不在 yaml,因為它們不是可調參數而是不變量。規則層命中與冷讀命中在預設設定下也擋得住(`allowed_severities` 只許 info),寫死是為了設定放寬之後一樣擋得住;`rule_hits` 裡的檢核點在預設設定下也要靠它擋(模型一條提到關鍵詞的 info 就讓 `pending_hints` 不成立)。

### 3. 回寫(`orchestrator/pipeline.py:review_mr` 尾段)

`review_mr` 在決策後依序回寫四樣東西,**每一步寫入之前都先取一次 MR**(`gitlab__get_mr_refs`,只取 head 與 `diff_refs`、不取檔案內容):head 已經換了就停在那裡,後面的步驟不再寫(`_writeback_target`,見 07 篇);沒換就用剛取回的 `diff_refs` 開行內討論。

1. 逐 finding `gitlab__post_inline_comment`(行內留言,`_format_comment` 排版:標題/說明/建議/引用;位置用剛取回的 `diff_refs`,不再每條重取)。行內討論**被 GitLab 拒絕(4xx,例如行號對不上 diff)**時退成一般留言;**連線中斷、逾時、5xx 時不退**:GitLab 可能已經建立了討論、只是回應沒送到,改貼的話同一條會出現兩次。這種記成「沒有確定貼上」(`report["_writeback"]["unposted"]`),在總結**每一條都列出**(前 20 條連標題,其餘只列檔名與行號:看 MR 的人拿不到伺服器上的報告檔)。
2. `gitlab__set_commit_status`:**只在不接 CI 時**(`review_mr(commit_status=True)`,webhook 那條路;部署端的 CI 那條路不寫,job 的結果就是閘門,見第 4 節)。對 `mr["sha"]`(MR head commit)回寫 status,`name="segcra/review"`,**state 的映射:`auto_approved → success`,其餘(`needs_human`、`blocked`)→ `failed`**,description 帶 `decision:summary 前 180 字`。
3. `gitlab__set_label`:`ai-review::<decision>`。
4. `gitlab__post_summary`:總評 + 三態決策文案(✅ 自動放行 / 👤 請人工確認 / ⛔ 修正前不得合入)+ rubric 分數 + verdict;有沒有確定貼上的 finding、或 commit status / 標籤沒寫上時附在最後。**總結排在最後**,前兩步沒寫上時才寫得進總結。
   **同一個 MR 只有一則總結**:留言裡放一個 HTML 註解當記號(`_SUMMARY_MARKERS`,在 GitLab 的 markdown 上看不見),重審時找回上一則、原地改寫。沒有記號的留言不動(那是人留的)。改寫被明確拒絕(4xx,上一則被刪了)就改貼新的一則;逾時或 5xx 不補貼(可能已經改好了,只是回應沒送到)。問不出留言清單時就貼新的一則:多一則是看得見的雜訊,總結完全沒貼上去是無聲失敗。
   webhook 的「審查未完成」通知用**另一個**記號:它會覆蓋自己那一則(連續失敗不洗版),但不會蓋掉一份完整的審查報告。

**每一步都看結果**(`gitlab.write_failure`):寫回的函式不丟例外,失敗時回 `{"ok": false, "failure": 種類}`——`rejected`(GitLab 拒絕,4xx)、`uncertain`(連線中斷、逾時、5xx,不確定有沒有寫上)、`no_sha`、`failed`(寫入工具本身出錯)。沒寫上的步驟記進 `report["_writeback"]`(`commit_status`、`label`、`summary`),印一行 log(只寫步驟與種類,不寫例外訊息)。

| 步驟 | 失敗時 |
|---|---|
| commit status | 重送(同一個 sha 與 name 只會更新同一個 status),最多送 3 次;最後還是失敗時總結寫明「merge 閘門沒有寫上」 |
| label | 重送一次;還是失敗時總結寫明 |
| 總結 | 不重送(逾時、5xx 時可能已經建立) |

被拒絕(4xx)的不重送:再送一樣被拒絕。

`toolbox/gitlab.py:set_commit_status` 兩路:real 模式(`GITLAB_URL`+`GITLAB_TOKEN`+`GITLAB_PROJECT` 三者齊備)打 GitLab REST `POST /projects/:id/statuses/:sha`;mock 模式寫進 `review_output/mr_<id>_review.json` 的 `commit_status` 欄。state 僅接受 success/failed/pending;real 模式缺 sha 時回 `failure: no_sha`,不會默默略過。

webhook 那條路在審查失敗或逾時時(`_notify_incomplete`)留言並把 commit status 設成失敗(CI 那條路只留言),兩步各自看結果:沒寫上時 log 寫「沒有設上」,不會印成「已設為失敗」;沒有 head sha 時用 `get_mr_refs` 取,不取檔案內容。

### 4. CI 那條路:審查是 MR pipeline 的最後一個 stage(部署端用這一條)

審查做成要掛審查的 repo 的 MR pipeline 最後一個 stage 的 job:pipeline 會等它跑完,結果就在 MR 看的同一條 pipeline 上,配合「Pipelines must succeed」讓決策變成能不能合併。接上去的步驟、job 的 YAML 與 GitLab 專案設定在 **[README §7](../README.md)**;這一節講機制。

**結束碼怎麼對**(`orchestrator/ci_gate.py:exit_code`,這張表是唯一的一份,其他地方連到這裡):

| 審查結果 | job 結束碼 | job | pipeline | 效果 |
|---|---|---|---|---|
| `auto_approved` | 0 | 成功 | 綠燈 | 可以合併 |
| `needs_human`(審完了,交給人判斷) | 3 | 失敗,但 `allow_failure: exit_codes: [3]` 允許 | passed with warnings | 有權限的人審完合併 |
| `blocked` | 4 | 失敗 | 紅燈 | 鎖住 |
| 審查沒跑完整(報告裡有環節未完整、整份失敗或逾時、被新的 commit 取代、審查服務連不上或拒絕) | 5 | 失敗 | 紅燈 | 鎖住;重審就是在 GitLab 上 retry 那個 job |

沒跑完整時就算決策是 `needs_human` 也給 5:沒跑完就是沒有結論(可能是 MR 本身就有問題,例如 dbt 展開不了,後面執行驗證也跑不了),不能走「審完了、交給人判斷」那一條。`needs_human` 用 3:1 與 2 是程式異常結束時常見的結束碼(Python 沒接住的例外是 1、參數錯誤是 2),允許失敗的碼不能跟它們重疊,否則 job 自己出錯也會被當成「審完了、交給人判斷」。

**job 呼叫審查服務,不在 runner 上直接跑審查。** CI 設定檔放在 MR 改不到的專案(下面),MR 改不了 job 的定義;但部署端既有的 job 會執行 repo 裡的東西(lint 的設定、腳本等),這些 MR 改得到,而前面的 job 改得到後面 job 的執行環境:shell executor 的 job 用同一個系統帳號、同一個家目錄,runner 用 `bash --login` 執行,家目錄的 `.profile`、`.bashrc` 都會載入;runner 自己的設定檔(含 runner 的 token)要是讀得到,也能冒充 runner 回報 job 的結果。審查需要的 GitLab token 與模型端點的通行碼若放在 runner 上,前面的 job 就拿得到。所以秘密只放在審查服務那台機器(`scripts/webhook_server.py`),job 只帶 GitLab 自動給的 `CI_JOB_TOKEN`;審查 job 也要跑在其他 job 碰不到的專用 runner 上(設定見 README §7),不然前面的 job 在 `PATH` 前面放一個假的 `curl`,審查 job 不連審查服務就會印出 `done 0`、結束碼 0。

job 跟服務之間:

1. job 送 `POST /ci/review`(header `JOB-TOKEN`,body `{"mr_iid": …, "sha": …}`)。body 不是這個形狀回 400。
2. 服務拿這把 token 向 GitLab 查它是哪一個 job(`GET /api/v4/job`),再用自己的 token 查那條 pipeline 與 MR,確認(`ci_gate.check_request`):job 在執行中、屬於 `GITLAB_PROJECT` 這個專案;pipeline 是 `merge_request_event`、ref 是 `refs/merge-requests/<iid>/head`、sha 跟送來的一樣;MR 是開啟中、head 就是這個 sha。任一項不符回 403;向 GitLab 查 job、pipeline、MR 取不到(連不上、逾時、非 2xx、內容不對)回 502,原因寫成「向 GitLab 查<哪一個>失敗:<原因>」,不說成不符;服務驗不了呼叫者(mock 模式、`GITLAB_PROJECT` 不是數字)回 503。三種都不開始審查,job 印出「審查服務沒有開始審查(HTTP <碼>)」與原因、以 5 結束。只有 GitLab 拒絕這把 job token(`GET /job` 回 401)時才說「job token 無效或 job 已經結束」:在測試 GitLab 上量過,亂打的 token 與 job 結束後的 token 都回 401,執行中回 200;其他狀態碼算查不到。查完 job 確定屬於這個專案才查 pipeline,pipeline 確定是這個 MR 的才查 MR:前一步不符時不會變成下一步「查不到」。
3. 通過時開始審查(同一個 MR 還有進行中的就取消、改審這次的),回一個查詢用的編號;job 每 30 秒 `GET /ci/review/<編號>`(同一把 `JOB-TOKEN`,只有開始那份審查、還在執行中的 job 查得到),拿到 `done <結束碼>` 就照那個碼結束。
4. 這條路不寫 commit status、不看 Draft、不等安靜期;留言、行內意見、label 照常。

**CI 設定檔放在 MR 改不到的專案,而且不 include 待審 repo 裡的任何設定:**

- 設定檔留在同一個 repo:MR pipeline 用的是 MR 來源分支上的設定檔,MR 把審查 job 換成 `exit 0` 或拿掉,閘門就沒了。
- 用 `include`(`local` 或 `project` 指到待審 repo)把 repo 裡的設定帶進來也不行:include 進來的內容跟主設定檔**合併**,同名 job 逐欄合併,主設定檔沒寫的欄位都補得進來,一欄一欄擋擋不完。例如 `image` 換成照樣印出 `done 0` 的映像、`services` 用別名把 `SEGCRA_URL` 的主機名指到自己起的服務、`variables` 設 `http_proxy` 讓 curl 走自己的代理。
- README §7 的 job 仍明寫 `before_script`、`after_script` 為空、`inherit` 不吃 `default` 與全域變數:設定檔裡給既有 job 用的 `default:` 不會套到它。
- 專案的 *CI/CD configuration file* 填成 `<檔案>@<群組>/<專案>` 時,GitLab 把它當成 `include: project` 讀進來,權限照觸發 pipeline 的人算:讀不到設定專案的人推的 commit,開出的 pipeline 直接失敗、沒有任何 job。所以開發者在設定專案要有讀取權(README §7 第 4 步)。

**專用 runner 為什麼要鎖在待審專案:** runner 只看 job 有沒有帶它的 tag,而 tag 是 job 自己寫的。這台 runner 若是 instance runner、group runner,或在其他專案也啟用,別的專案的 CI 設定還在它自己的 repo 裡、開發者改得到,加一行 `tags: [segcra-review]` 就跑在這台、用同一個帳號,一樣能留下假的 `curl`。docker executor 開了 `privileged` 時,容器碰得到主機,效果跟掛 `docker.sock` 相同。

**同一個 MR 只審第一次(`CI_REVIEW_REPEAT=first`)時,什麼時候跳過**(`ci_gate.should_skip`,設法見
README §7「使用時」):以下都成立才跳過,否則照樣審。

- 同一個 MR 沒有還在跑的 CI 審查:有的話照樣審這次、取消舊的(「還在審就被取消」照樣成立)。
- 審查服務自己記得這個 MR 上一次**真的審完**的結果,而且是放行或交人工。上一次擋下或沒跑完整時照樣審,
  不然被擋的 MR 推一個新 commit 就變成可以合併。
- 這次不是有人要求重審:上一次處理的(審過或跳過的)同一個 commit 又送來,就是有人按了 retry 或重跑
  pipeline。MR 的 pipeline 帶不了手動變數,「重審」只能用這個方式表達;請求裡沒有「強制重審」的欄位。
- 目標分支跟上一次真的審時相同,而且兩邊都取得到:換過目標分支(包括換回原本的)、或取不到,都照樣審。

只看服務自己的紀錄,不看 GitLab 的 label 或留言:那些開發者改得到。紀錄只有這個 MR 最後一份 CI 審查的
結果才記,比較晚結束的舊審查不會把紀錄蓋回舊的 commit。跳過時的留言寫出上一次真的審的 commit,連續
跳過好幾次也是同一個,合併的人知道要從哪個版本開始看。

### 5. 不接 CI 時:webhook + commit status(README §6、§7)

測試環境或沒有 CI 的 repo 用這一條,部署端不用。MR 事件觸發審查(webhook),審完對 head commit 回寫 `segcra/review` 的 success/failed(第 3 節第 4 點),再配上 **Pipelines must succeed** 與 **protected branch**(設定見 README §7 第 6 步):

| 決策 | commit status | 末端效果 |
|---|---|---|
| `auto_approved` | success | Merge 按鈕可按 |
| `needs_human` | failed | Merge 被鎖;人工確認後留言 `/review` 重審,或由有權限者依 GitLab 流程處置 |
| `blocked` | failed | 修正前不得合入 |
| 審查沒完成 | failed | 留言說明「審查未完成」 |

限制:審查在 pipeline 外面跑,pipeline 不會等它;補寫的 status 也不一定掛在 MR 看的那一條 pipeline 上,要靠「Pipelines must succeed」把外部 status 一起算進去。status 綁 commit,push 新 commit 後要重審才會解鎖。

## 對決策的影響

本篇兩個函式**就是**決策本體:`apply_rubric` 產出 `score`/`verdict`(並把模型自評挪去 `_model_score`),`apply_policy` 產出 `decision` 與 `_policy_signals`。上游各防線的產物在這裡收口——enforce 鏈補的 blocker 走 `min_blockers` 進 `blocked`;`enforce_hints` 補的「檢核點待人工確認」觸發 `pending_hints`;spec_exec 的 major 既拉低 rubric 分數(−15)又帶入非 info severity,而 `_spec_exec.passed` 另走硬條件。決策再經 CI job 的結束碼變成 GitLab 上實際可執行的閘門。

## 怎麼驗證它在動

**(a) 不經 LLM,直接打函式**(全部確定性,秒回;以下輸出為本機實測):

```bash
cd SEGCRA-CR && .venv/bin/python - <<'EOF'
from orchestrator.pipeline import apply_rubric, apply_policy
from orchestrator.config import load_config
cfg = load_config()
mr = {"files": [{"path": "sql/maintenance/fix.sql", "diff": "@@ -1 +1 @@\n-a\n+b"}]}

r = apply_rubric({"score": 92, "findings": [{"severity": "major"}]})
print(r["score"], r["verdict"], r["_model_score"])   # → 85 needs_changes 92(模型自評 92 被覆寫)

r = apply_rubric({"findings": [{"severity": "blocker"}, {"severity": "minor"}]})
print(r["score"], r["verdict"])                      # → 55 needs_changes(100−40−5)

rep = apply_policy({"score": 100, "findings": [{"severity": "info"}],
                    "_spec_exec": {"passed": True}}, mr, cfg.policy)
print(rep["decision"])                               # → auto_approved

rep = apply_policy({"score": 100, "findings": [{"severity": "info"}],
                    "_spec_exec": {"passed": False}}, mr, cfg.policy)
print(rep["decision"])                               # → needs_human(spec_exec 硬條件)

mr2 = {"files": [{"path": "sql/rules/r999.sql", "diff": "@@ -0,0 +1,3 @@\n+a\n+b\n+c"}]}
rep = apply_policy({"score": 100, "findings": [{"severity": "info"}],
                    "_spec_exec": {"passed": True}}, mr2, cfg.policy)
print(rep["decision"])                               # → needs_human(new_rule)

rep = apply_policy({"score": 0, "findings": [{"severity": "blocker"}],
                    "_spec_exec": {"passed": True}}, mr, cfg.policy)
print(rep["decision"])                               # → blocked
EOF
```

**(b) 端到端(需模型)**:`.venv/bin/python demo.py --mr 003`。mr_003 帶提示注入 payload——注入掃描確定性命中 → blocker → 本機實測:rubric 算出 `score: 45`(blocker −40 + major −15,封頂 59 內)、模型自評 20 只留在 `_model_score`、決策 `blocked`;`review_output/mr_003_review.json` 的 `commit_status` 應為 `{"state": "failed", "name": "segcra/review", "description": "blocked:…"}`,`labels` 含 `ai-review::blocked`。`--mr 001`(SQL 與規格全符,執行驗證 `passed: true`)本機實測 `_policy_signals` 為 `{change_lines: 12, new_rule: true, spec_exec_passed: true}` → 決策 `needs_human`——執行驗證通過不等於自動放行,新規則檔一律至少人工過目。

**(c) GitLab 端**:`tests/test_ci_gate.py` 驗四種結果的結束碼、`/ci/review` 只認這個 MR 的 job、這條路不寫 commit status。實際的燈號與能不能合併要在 GitLab 上試:用 README §8 的測試 GitLab,照 README §7 接上(測試用的 runner 也要帶 `segcra-review` 這個 tag,不然審查 job 一直 pending),把審查 job 接在一個會成功的 job 後面,四種結果各試一次,看 pipeline 的燈號與 Merge 按鈕。

接上部署端之後,在部署端再試一次下面幾項:

- MR 改 repo 裡的 CI 設定(包括在 repo 放一份 `.gitlab-ci.yml`),審查 job 不會變綠。
- 前面的 job 在 runner 上留下假的 `curl`,審查 job 不會變綠。
- 用只在待審專案是 Developer 的帳號推 commit:在設定專案是 Reporter 時 pipeline 建得起來、審查 job 照常跑;設定專案是 Private 又不是成員時 pipeline 直接失敗、沒有任何 job(Internal 的設定專案,External 以外的登入使用者都讀得到)。
- 另一個專案的 job 帶 `tags: [segcra-review]`,專用 runner 不會接(job 一直 pending)。

不接 CI 時(第 5 節)看 status `segcra/review` 出現在 pipeline 區、`needs_human`/`blocked` 時 Merge 按鈕呈鎖定。

## 擴充與注意

- **調閾值**:使用中的模型設定檔(`SEGCRA_MODELS_CONFIG` 指的那份,沒設時是 `config/models.yaml`;見 docs/10)的 `policy:` 段——`max_diff_lines`、`min_score`、`allowed_severities`、`forbid_pending_hints`、`min_blockers`。改完即生效,不用動程式。
- **改評分權重**:`orchestrator/pipeline.py:apply_rubric` 的公式(40/15/5 與 59 封頂)。動之前想清楚連動:`min_score: 95` 的意思是「至多一個 minor(100−5)」,權重一改,yaml 的 min_score 語意跟著變。
- **加新的放行條件**:加在 `apply_policy` 的 `ok = (...)` 鏈上,並把新訊號寫進 `_policy_signals`,決策才可稽核。與 `spec_exec_ok` 同級的「不變量」請寫死在程式,不要進 yaml。
- **選 `CI_REVIEW_REPEAT=first` 的代價**:第一次之後的改動整份不經過審查(規則檢查、注入掃描、執行驗證、
  AI 都沒跑),只靠有合併權限的人把關。「先送一個無害的版本取得放行、再改成惡意的改動」這種情況不會被看到。
  預設的 `every` 沒有這個問題。
- **`first` 的紀錄只在記憶體**:審查服務重新啟動後忘記,下一次就照樣審(往多審的方向走);紀錄有上限,
  超過時丟掉最舊的,一樣是多審。
- **已知限制**:
  - 不接 CI 時(第 5 節)三態映射到 commit status 只有兩值(success/failed),GitLab 上看不出 `needs_human` 與 `blocked` 的差別——差別要看 label(`ai-review::<decision>`)與總評留言。CI 那條路(第 4 節)分得出來:`needs_human` 是 passed with warnings。
  - `new_rule` 的判定靠路徑(`rules/`)與 diff 形態(`@@ -0,0` / `generated`),repo 目錄慣例改了要同步改 `apply_policy`。
  - 不接 CI 時 commit status 需要 `mr["sha"]`;real 模式由 `gitlab__get_mr_diff` 回傳,若 API 拿不到 sha,status 回寫會失敗(有錯誤訊息,不會默默成功)。
  - `apply_rubric` 只看 severity 個數,不看 finding 內容真偽——內容品質由上游(引用白名單、enforce 鏈、仲裁)把關,rubric 不重複做。
  - CI 那條路(第 4 節、README §7):
    - **Draft 也審**:Draft 標成 Ready 不會重跑 pipeline,Draft 時跳過的話,標成 Ready 之後那條 pipeline 裡就沒有審查。
    - **審查服務重新啟動**:進行中的審查記在記憶體,重新啟動就沒了;job 查不到狀態,連續 10 次(約 5 分鐘)後以 5 結束,retry job 重審。
    - **job 在 GitLab 上被取消時,服務那邊的審查不會停**:服務只在同一個 MR 有新的請求進來時取消舊的。被取消的 job 空出 runner 的位置、下一份審查開始,這段時間同時審查的份數會超過 runner 的 `limit`,直到舊的審完或逾時。
    - **同時審查的上限是專用 runner 的 `limit`**:審查 job 在整份審查期間(最長 `REVIEW_TIMEOUT_SECONDS`)都佔著一個位置,大部分時間在等。
