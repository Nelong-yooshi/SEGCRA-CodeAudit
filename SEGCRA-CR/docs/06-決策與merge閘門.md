# 06-決策與 merge 閘門

## 在幹嘛(一段白話)

LLM 審完、執行驗證跑完之後,管線用兩個**確定性函式**把「這個 MR 接下來怎麼走」算出來:`apply_rubric` 依 finding 的 severity 組成重算分數與 verdict(模型自評分數只留檔不採用),`apply_policy` 依客觀訊號(diff 行數、是否新規則檔、rubric 分數、severity 組成、未回應檢核點、執行驗證結果)判成三態之一——`auto_approved` / `needs_human` / `blocked`。決策再以 commit status 回寫 GitLab,配合 CE 的「Pipelines must succeed」與 protected branch,讓 `needs_human` 與 `blocked` 的 MR **實際按不下 Merge 鈕**,而不是只留一句留言。

## 防的是什麼

- **分數漂移**:實測同一個 MR,模型自評分數逐輪不一致(同一 MR 曾出現 50 vs 10)。裁決若掛在模型自評上,今天 needs_human、明天 auto_approved。所以分數由 rubric 從 severity 確定性重算(`apply_rubric` docstring 即記載此起因)。
- **模型被壓制後「假通過」**:提示注入若讓模型輸出 score 100 / findings 空,決策層還有 enforce 鏈補回的確定性 blocker(注入掃描命中必進 findings)→ `min_blockers` 條件把它擋成 `blocked`,與模型嘴上說什麼無關。
- **「小改」偽裝**:自動放行只給小變更,但 diff 行數本身可被佔位 diff(`(generated)`)騙過;`apply_policy` 對不可靠 diff 退回 `full_content` 行數計算,新規則檔另有一條獨立的 `new_rule` 硬條件。
- **只看靜態審查就放行**:SQL「看起來對」不代表「跑起來對」。`_spec_exec.passed` 是寫死在管線的硬條件(config 關不掉),沒通過執行驗證的 MR 永遠不會自動放行。
- **決策做了卻擋不住人**:三態若只寫在留言裡,誰都能照樣按 Merge。所以決策落到 commit status,靠 GitLab 自身的 merge check 生效——這一段是機制,不是模型自律,也不是審查者自律。

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
   - `score ≥ policy.auto_approve.min_score`(models.yaml 設 95;沒設時預設 101,等於不放行)
   - `severities ⊆ policy.auto_approve.allowed_severities`(models.yaml 只允許 `[info]`)
   - `not (forbid_pending_hints and pending_hints)`(models.yaml 設 true:有待確認檢核點一律不放行)
3. 其餘 → `needs_human`。
4. 訊號寫進 `report["_policy_signals"]`(change_lines / new_rule / severities / pending_hints / spec_exec_passed)供稽核,決策寫 `report["decision"]`。`policy` 段整個缺席時直接 `needs_human`(fail-safe)。

與 `config/models.yaml` 的對應:`policy.auto_approve.{max_diff_lines, min_score, allowed_severities, forbid_pending_hints}` 與 `policy.block.min_blockers` 一一對應上述條件;唯一不在 yaml 的是 `spec_exec_ok`,因為它不是可調參數而是不變量。

### 3. commit status 回寫(`orchestrator/pipeline.py:review_mr` 尾段 → `toolbox/gitlab.py:set_commit_status`)

`review_mr` 在決策後依序回寫四樣東西:

1. 逐 finding `gitlab__post_inline_comment`(行內留言,`_format_comment` 排版:標題/說明/建議/引用)。
2. `gitlab__post_summary`:總評 + 三態決策文案(✅ 自動放行 / 👤 請人工確認 / ⛔ 修正前不得合入)+ rubric 分數 + verdict。
3. `gitlab__set_label`:`ai-review::<decision>`。
4. `gitlab__set_commit_status`:對 `mr["sha"]`(MR head commit)回寫 status,`name="segcra/review"`,**state 的映射:`auto_approved → success`,其餘(`needs_human`、`blocked`)→ `failed`**,description 帶 `decision:summary 前 180 字`。

`toolbox/gitlab.py:set_commit_status` 兩路:real 模式(`GITLAB_URL`+`GITLAB_TOKEN`+`GITLAB_PROJECT` 三者齊備)打 GitLab REST `POST /projects/:id/statuses/:sha`;mock 模式寫進 `review_output/mr_<id>_review.json` 的 `commit_status` 欄。state 僅接受 success/failed/pending;real 模式缺 sha 會回錯誤而不是默默略過。

### 4. GitLab CE 的 merge 閘門三件套(README §7)

CE 沒有 EE 的 approval rules,可行的擋法是三件事組起來:

1. **commit status**:上面第 3 步,每次審完對 head commit 回寫 `segcra/review` 的 success/failed。
2. **Pipelines must succeed**:專案 → Settings → Merge requests → Merge checks 勾選。從此 status=failed 的 MR,Merge 按鈕被 GitLab 鎖住。
3. **Protected branch**:`main` 設 protected,Allowed to push = No one、Allowed to merge = Maintainers——確保所有變更都走 MR、都經過審查,沒有側門直推。

三態的末端效果:

| 決策 | commit status | 末端效果 |
|---|---|---|
| `auto_approved` | success | 小幅變更且無實質問題;Merge 按鈕可按(仍可要求人再看) |
| `needs_human` | failed | Merge 被鎖;人工確認後留言 `/review` 重審,或由有權限者依 GitLab 流程處置 |
| `blocked` | failed | 有 blocker(注入、hardcode 憑證、DELETE 無 WHERE 等);修正前不得合入 |

`needs_human` 走 failed 是刻意保守:CE 的 merge check 只認 success/failed 兩種結果,凡是需要人看過的都先鎖住,由人做最後放行。注意 status 綁 commit——push 新 commit 後舊 status 不跟過去,要重審才會解鎖(這也是對的:新 commit 是沒審過的東西)。

## 對決策的影響

本篇兩個函式**就是**決策本體:`apply_rubric` 產出 `score`/`verdict`(並把模型自評挪去 `_model_score`),`apply_policy` 產出 `decision` 與 `_policy_signals`。上游各防線的產物在這裡收口——enforce 鏈補的 blocker 走 `min_blockers` 進 `blocked`;`enforce_hints` 補的「檢核點待人工確認」觸發 `pending_hints`;spec_exec 的 major 既拉低 rubric 分數(−15)又帶入非 info severity,而 `_spec_exec.passed` 另走硬條件。決策再經 commit status 變成 GitLab 上實際可執行的閘門。

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

**(c) GitLab 端**:real 模式審完後,到 MR 頁面確認 status `segcra/review` 出現在 pipeline 區;`needs_human`/`blocked` 時 Merge 按鈕呈鎖定。若沒鎖住,先查專案是否勾了 Pipelines must succeed、審的是否為最新 commit(README §10 疑難排解表列有此症狀)。

## 擴充與注意

- **調閾值**:`config/models.yaml` 的 `policy:` 段——`max_diff_lines`、`min_score`、`allowed_severities`、`forbid_pending_hints`、`min_blockers`。改完即生效,不用動程式。
- **改評分權重**:`orchestrator/pipeline.py:apply_rubric` 的公式(40/15/5 與 59 封頂)。動之前想清楚連動:`min_score: 95` 的意思是「至多一個 minor(100−5)」,權重一改,yaml 的 min_score 語意跟著變。
- **加新的放行條件**:加在 `apply_policy` 的 `ok = (...)` 鏈上,並把新訊號寫進 `_policy_signals`,決策才可稽核。與 `spec_exec_ok` 同級的「不變量」請寫死在程式,不要進 yaml。
- **已知限制**:
  - 三態映射到 commit status 只有兩值(success/failed),GitLab 上看不出 `needs_human` 與 `blocked` 的差別——差別要看 label(`ai-review::<decision>`)與總評留言。
  - `new_rule` 的判定靠路徑(`rules/`)與 diff 形態(`@@ -0,0` / `generated`),repo 目錄慣例改了要同步改 `apply_policy`。
  - commit status 需要 `mr["sha"]`;real 模式由 `gitlab__get_mr_diff` 回傳,若 API 拿不到 sha,status 回寫會失敗(有錯誤訊息,不會默默成功)。
  - `apply_rubric` 只看 severity 個數,不看 finding 內容真偽——內容品質由上游(引用白名單、enforce 鏈、仲裁)把關,rubric 不重複做。
