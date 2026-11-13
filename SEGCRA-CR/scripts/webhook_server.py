#!/usr/bin/env python3
"""GitLab webhook 接收端 — MR 事件自動觸發 code review(回寫行內留言/分數/label/
commit status)。

地端跑在能連到 GitLab 的內網主機;無第三方依賴(Python 標準庫)。

啟動:
  export GITLAB_URL=http://localhost:8929 GITLAB_TOKEN=<token> GITLAB_PROJECT=<id>
  export GITLAB_WEBHOOK_SECRET=<自訂密鑰>   # 與 GitLab webhook 的 Secret token 一致
  export REVIEW_PROFILE=review               # 用哪個模型 profile 審
  .venv/bin/python scripts/webhook_server.py --port 8000

GitLab 設定:專案 → Settings → Webhooks → URL 指到 http://<本機>:8000/gitlab-webhook、
Secret token 填 GITLAB_WEBHOOK_SECRET、勾 Merge request events 與 Comments
(留言 /review 可重審)。健康檢查:GET /health。

什麼時候審:MR 開啟、重開、推了新 commit、從 Draft 標成 Ready。Draft 的 MR 不審(開發中反覆推
commit,每推一次就整份審一次);Draft 期間要看,留言 /review。/review 要寫在留言開頭(一行只寫
`/review`,或後面空一格再寫別的),內文或網址裡出現 /review 不算。同一個 MR 短時間內連續觸發時,
等 WEBHOOK_DEBOUNCE_SECONDS 秒沒有新事件才開始,只審最後一次。

審查完成後管線會回寫 commit status(name=segcra/review):
auto_approved → success;needs_human / blocked → failed(description 帶原因)。
搭配專案設定「Pipelines must succeed」+ protected branch,即為 CE 的 merge 閘門。

**部署端用的是另一條:CI 的 job 呼叫 /ci/review**(見 orchestrator/ci_gate.py、docs/06)。
審查做成 MR pipeline 最後一個 stage 的 job:job 帶自己的 CI_JOB_TOKEN 呼叫
`POST /ci/review`(body:{"mr_iid": ..., "sha": ...}),服務向 GitLab 確認它是這個專案、這個 MR
的 MR pipeline 裡正在跑的 job,開始審查並回一個查詢用的編號;job 每隔一段時間
`GET /ci/review/<編號>`,拿到 `done <結束碼>` 就照那個碼結束。這條路不寫 commit status、
不看 Draft、不等安靜期(每個 job 都要自己的結果);留言與行內意見照常。部署端不設上面的
webhook,只用這一條。
"""
import argparse
import asyncio
import hmac
import json
import os
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))
from orchestrator import ci_gate                  # noqa: E402
from orchestrator.config import env_seconds, load_config      # noqa: E402
from orchestrator.pipeline import review_mr       # noqa: E402

SECRET = os.environ.get("GITLAB_WEBHOOK_SECRET", "")
PROFILE = os.environ.get("REVIEW_PROFILE", "review")
TRIGGER_ACTIONS = {"open", "reopen", "update"}     # MR 這些動作觸發審查
_cfg = load_config()
# 一份審查的總時間上限(#45)。超過就中止,並在 MR 上留言——**不能無聲無息**:
# 正式環境裡「沒擋、沒放、也沒人知道」是最糟的失敗型態。
# 執行驗證那一段自己另有上限(pipeline.SPEC_EXEC_TIMEOUT_SECONDS,預設 1200 秒),
# 這裡是整份的天花板,要比它大。
REVIEW_TIMEOUT_SECONDS = env_seconds("REVIEW_TIMEOUT_SECONDS", 2400)
# 同一個 MR 短時間內連續觸發(連推幾個 commit、連續留言)時,等這麼多秒沒有新事件才開始審:
# 每份審查都是完整的模型呼叫,每來一個事件就開一份再取消,只是白白佔住模型
DEBOUNCE_SECONDS = env_seconds("WEBHOOK_DEBOUNCE_SECONDS", 10)
# 留言開頭的 /review 指令(後面是空白或結尾)。寫在內文、網址裡、或 /reviews 都不算
_REVIEW_COMMAND = re.compile(r"\A\s*/review(?:\s|\Z)")

# 每個 MR 一份進行中的紀錄:{mr_iid: (event_loop, task)}。
# 用它做兩件事:同一 MR 推新 commit 時取消舊的那次、以及知道誰還在跑。
_inflight: dict = {}
_lock = threading.Lock()


def _notify_incomplete(mr_iid: int, reason: str, head_sha: str | None = None,
                       commit_status: bool = True) -> None:
    """審查沒完成時:在 MR 上留言,**並把 commit status 設成失敗**。

    commit_status=False(CI 的 job 呼叫的那條路):只留言。閘門是 job 的結束碼,不寫 commit status。

    留言只是給人看的;**合併閘門靠的是 commit status**(GitLab CE 的
    「Pipelines must succeed」)。只留言不設狀態,等於審查沒跑完卻沒有東西擋著合併
    ——那正是 #45 要消滅的「沒擋、沒放、也沒人知道」。

    兩個動作各自 try:留言失敗不該讓狀態也設不上,反之亦然。兩個都看回傳的結果
    (`gitlab.write_failure`):沒寫上時不能印成已經寫上。log 只寫哪一步與問題的種類,
    不印例外訊息(可能帶主機位址)。
    """
    body = (f"審查未完成({reason}),請人工處理。\n\n"
            f"這不是「沒問題」——是這次審查沒有跑完,沒有結論。")
    from toolbox import gitlab
    try:
        failure = gitlab.write_failure(gitlab.post_summary(str(mr_iid), body, 0, "審查未完成"))
    except Exception as e:                   # noqa: BLE001 — 只記種類
        failure = type(e).__name__
    if failure is None:
        print(f"[webhook] MR !{mr_iid} 已留言告知審查未完成({reason})", flush=True)
    else:
        print(f"[webhook] MR !{mr_iid} 審查未完成的留言沒有貼上({failure})", flush=True)
    if not commit_status:
        return
    try:
        sha = head_sha or ""
        if not sha:
            # 只要 head 的 sha:取 MR 的位置就好,不用 get_mr_diff 把每個檔案都取一遍
            refs = json.loads(gitlab.get_mr_refs(str(mr_iid)))
            sha = refs.get("head_sha") or refs.get("sha") or ""
        failure = gitlab.write_failure(gitlab.set_commit_status(
            sha, "failed", "segcra/review", f"審查未完成:{reason}"[:200], str(mr_iid)))
    except Exception as e:                   # noqa: BLE001 — 只記種類
        failure = type(e).__name__
    if failure is None:
        print(f"[webhook] MR !{mr_iid} commit status 設為失敗(審查未完成)", flush=True)
    else:
        print(f"[webhook] MR !{mr_iid} commit status 沒有設上({failure}):merge 閘門上沒有這次的結果",
              flush=True)


# 關掉事件迴圈前,等還沒結束的工作收尾最多幾秒。正常的工作被取消後立刻結束(模型請求的
# 連線跟著斷掉);等不完的是吞掉取消、繼續跑的工作,為它卡住整條執行緒不值得,照關並記下來。
_LOOP_CLEANUP_SECONDS = 30


def _close_loop(loop: asyncio.AbstractEventLoop, mr_iid: int) -> None:
    """跟 `asyncio.run` 收尾的做法一樣:取消還沒結束的工作、等它們收完,收掉 async generator
    與預設的執行緒池,最後才關迴圈。

    直接 `loop.close()` 的話,審查裡另外開的工作(背景的模型呼叫等)不會被取消:外層逾時、
    或新 commit 取消舊審查時,打到模型的請求沒有人收尾,端點照樣把它生成完,Python 也只會
    在之後某個時間點印「Task was destroyed but it is pending!」。

    跟 `asyncio.run` 不同的是等待有上限(`_LOOP_CLEANUP_SECONDS`)。每一步各自 try,收尾出錯
    不往外丟;log 只寫例外的類型,不寫訊息(訊息可能帶主機位址或模型輸出)。
    """
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            _, still = loop.run_until_complete(asyncio.wait(pending, timeout=_LOOP_CLEANUP_SECONDS))
            if still:
                print(f"[webhook] MR !{mr_iid} 有 {len(still)} 個工作取消後 "
                      f"{_LOOP_CLEANUP_SECONDS} 秒仍沒結束,照樣關掉事件迴圈", flush=True)
        for t in pending:
            # 取出例外,免得之後又印一次「Task exception was never retrieved」
            if t.done() and not t.cancelled() and t.exception() is not None:
                print(f"[webhook] MR !{mr_iid} 收尾時有工作丟出 {type(t.exception()).__name__}",
                      flush=True)
    except Exception as e:
        print(f"[webhook] MR !{mr_iid} 取消剩下的工作時出錯:{type(e).__name__}", flush=True)
    for step in ("shutdown_asyncgens", "shutdown_default_executor"):
        try:
            loop.run_until_complete(asyncio.wait_for(getattr(loop, step)(), _LOOP_CLEANUP_SECONDS))
        except Exception as e:
            print(f"[webhook] MR !{mr_iid} 收尾({step})出錯:{type(e).__name__}", flush=True)
    try:
        loop.close()
    except Exception as e:
        print(f"[webhook] MR !{mr_iid} 關閉事件迴圈出錯:{type(e).__name__}", flush=True)
    asyncio.set_event_loop(None)


def _run_review(mr_iid: int, head_sha: str | None = None, ci_ticket: str | None = None):
    """在自己的執行緒與事件迴圈裡跑一份審查。

    每個 MR 各一個執行緒 → 一個 MR 卡住或炸掉不影響其他 MR(#45)。

    ci_ticket 給定時是 CI 的 job 呼叫的那一份(見 _ci_start):不寫 commit status,結束時把
    結束碼記在 _ci_reviews[ci_ticket](沒跑完、被取消、出錯都是 EXIT_INCOMPLETE)。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = None
    in_ci = ci_ticket is not None
    code = ci_gate.EXIT_INCOMPLETE
    try:
        task = loop.create_task(
            asyncio.wait_for(review_mr(_cfg, str(mr_iid), profile_name=PROFILE,
                                       **({"commit_status": False} if in_ci else {})),
                             timeout=REVIEW_TIMEOUT_SECONDS))
        with _lock:
            _inflight[mr_iid] = (loop, task)
        report = loop.run_until_complete(task)
        code = ci_gate.exit_code(report)
        print(f"[webhook] MR !{mr_iid} 審完:決策={report.get('decision')} "
              f"分數={report.get('score')} findings={len(report.get('findings', []))} "
              f"執行驗證={'通過' if report.get('_spec_exec', {}).get('passed') else '未通過'}"
              + (f" 結束碼={code}" if in_ci else ""),
              flush=True)
    except asyncio.TimeoutError:
        print(f"[webhook] MR !{mr_iid} 審查逾時({REVIEW_TIMEOUT_SECONDS}s),中止", flush=True)
        _notify_incomplete(mr_iid, f"超過 {REVIEW_TIMEOUT_SECONDS} 秒的時間上限", head_sha,
                           **({"commit_status": False} if in_ci else {}))
    except asyncio.CancelledError:
        # 同一個 MR 推了新 commit → 舊的這次被取消。新的那次會給出結論,
        # 所以這裡不留言(免得同一個 MR 洗版)。CI 那條路:舊的 job 拿到 EXIT_INCOMPLETE
        print(f"[webhook] MR !{mr_iid} 舊審查被新的 commit 取代", flush=True)
    except Exception as e:
        print(f"[webhook] MR !{mr_iid} 審查失敗:{type(e).__name__}: {e}", flush=True)
        _notify_incomplete(mr_iid, f"審查程式發生 {type(e).__name__}", head_sha,
                           **({"commit_status": False} if in_ci else {}))
    finally:
        with _lock:
            if _inflight.get(mr_iid, (None, None))[1] is task:
                _inflight.pop(mr_iid, None)
            if in_ci and ci_ticket in _ci_reviews:
                _ci_reviews[ci_ticket]["code"] = code
                _remember_ci_result(_ci_reviews[ci_ticket].get("key"), code)
        _close_loop(loop, mr_iid)


def _cancel(mr_iid: int) -> bool:
    """取消某個 MR 進行中的審查。回傳有沒有真的取消到。

    跨執行緒取消要透過 `call_soon_threadsafe`:task 屬於那個執行緒的迴圈,
    直接呼叫 cancel() 不會被該迴圈看見。
    """
    with _lock:
        entry = _inflight.get(mr_iid)
    if not entry:
        return False
    loop, task = entry
    try:
        loop.call_soon_threadsafe(task.cancel)
        return True
    except Exception as e:
        print(f"[webhook] MR !{mr_iid} 取消舊審查失敗:{type(e).__name__}: {e}", flush=True)
        return False


# 還在等的審查:{mr_iid: 計時器}。計時器到了才真的開始(見 _trigger)
_pending: dict = {}


def _trigger(mr_iid: int, head_sha: str | None = None) -> str:
    """排一份審查:DEBOUNCE_SECONDS 秒內同一個 MR 沒有新事件才開始;有的話重新計時,改審最新的。

    開始時,同一個 MR 還有進行中的審查就取消它、改審新的:舊的那次審的是舊程式,
    結論貼到新的 commit 上是錯的。"""
    timer = threading.Timer(DEBOUNCE_SECONDS, _start, args=(mr_iid, head_sha))
    timer.daemon = True
    with _lock:
        old = _pending.get(mr_iid)
        _pending[mr_iid] = timer
    if old is not None:
        old.cancel()
    timer.start()
    return f"review scheduled for MR !{mr_iid}"


def _start(mr_iid: int, head_sha: str | None) -> None:
    """計時器到了:這個計時器還是這個 MR 最新的那一個,才開始審查。

    計時器取消得太晚(已經在跑這個函式)時,新的事件已經換上新的計時器,這裡就不開始,
    不會出現兩份。"""
    with _lock:
        if _pending.get(mr_iid) is not threading.current_thread():
            return
        _pending.pop(mr_iid)
    if _cancel(mr_iid):
        print(f"[webhook] MR !{mr_iid} 取消進行中的舊審查,改審新的", flush=True)
    print(f"[webhook] → 觸發審查 MR !{mr_iid}(profile={PROFILE})", flush=True)
    threading.Thread(target=_run_review, args=(mr_iid, head_sha), daemon=True).start()


# CI 的 job 呼叫的審查:{編號: {"job": job id, "mr": MR iid, "code": 結束碼或 None(還在跑)}}
_ci_reviews: dict = {}
# 最多記幾份;超過時丟掉最舊的、已經結束的(查不到的編號,job 會以 EXIT_INCOMPLETE 結束)
MAX_CI_REVIEWS = 500
# 每個 MR(同一個目標分支)上一次 CI 審查**跑完**的結束碼,給 CI_REVIEW_REPEAT=first 判斷要不要跳過
# (見 ci_gate.should_skip)。只在記憶體:服務重新啟動後忘記,下一次就照樣審 —— 出錯時往「多審」
# 的方向走。有上限,超過時丟掉最舊的(一樣是多審)
_ci_history: dict = {}
MAX_CI_HISTORY = 2000


def _remember_ci_result(key, code) -> None:
    """記下這個 MR 上一次 CI 審查的結束碼(呼叫端持有 _lock)。"""
    if key is None:
        return
    _ci_history.pop(key, None)
    while len(_ci_history) >= MAX_CI_HISTORY:
        _ci_history.pop(next(iter(_ci_history)))
    _ci_history[key] = code


def _announce_skipped(mr_iid: int, previous: int) -> None:
    """跳過時寫回 MR:留言說明這次的改動沒有經過 AI 審查,標籤改成交人工(不然 MR 上還掛著
    上一次的「自動放行」)。兩個動作各自做,失敗只記種類。"""
    from toolbox import gitlab
    for what, send in (("留言", lambda: gitlab.post_note(str(mr_iid), ci_gate.skipped_note(previous))),
                       ("標籤", lambda: gitlab.set_label(str(mr_iid), "ai-review::needs_human"))):
        try:
            failure = gitlab.write_failure(send())
        except Exception as e:               # noqa: BLE001 — 只記種類
            failure = type(e).__name__
        if failure is not None:
            print(f"[webhook] MR !{mr_iid} 跳過審查時{what}沒寫上({failure})", flush=True)


def _ci_job(job_token):
    """job token → GitLab 上的 job;查不到回 None。"""
    from toolbox import gitlab
    return gitlab.job_from_token(job_token)


def _ci_start(job_token, body) -> tuple[int, str]:
    """POST /ci/review:確認呼叫者後開始一份審查,回 (HTTP 狀態碼, 查詢用的編號或拒絕原因)。

    同一個 MR 還有進行中的審查(webhook 那條路或前一個 job)就取消它、改審這次的:舊的那次
    審的可能是舊的 commit;同一個 commit 的 job 被 retry 也是重審。"""
    from toolbox import gitlab
    if not isinstance(body, dict):
        return 400, "body 要是 JSON 物件"
    mr_iid, sha = body.get("mr_iid"), body.get("sha")
    # 手動要求重審(pipeline 帶 SEGCRA_REVIEW=1):只認 JSON 的 true,其他一律當成沒有要求
    force = body.get("force") is True
    if isinstance(mr_iid, str) and mr_iid.isdigit():
        mr_iid = int(mr_iid)
    if not isinstance(mr_iid, int) or isinstance(mr_iid, bool) or mr_iid <= 0 \
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        return 400, "mr_iid 要是正整數、sha 要是 40 碼的 commit"
    job = _ci_job(job_token)
    jp = job.get("pipeline") if isinstance(job, dict) else None
    pipeline = gitlab.pipeline_info(jp.get("id")) if isinstance(jp, dict) else None
    mr = gitlab.mr_head(str(mr_iid))
    reason = ci_gate.check_request(job, pipeline, mr, project_id=gitlab.GITLAB_PROJECT,
                                   mr_iid=mr_iid, sha=sha)
    if reason:
        print(f"[webhook] /ci/review 拒絕 MR !{mr_iid}:{reason}", flush=True)
        return 403, reason
    ticket = secrets.token_urlsafe(16)
    key = (mr_iid, (mr or {}).get("target_branch") or "")
    mode = ci_gate.review_repeat()
    with _lock:
        done = [t for t, e in _ci_reviews.items() if e["code"] is not None]
        while len(_ci_reviews) >= MAX_CI_REVIEWS and done:
            _ci_reviews.pop(done.pop(0), None)
        previous = _ci_history.get(key)
        skip = ci_gate.should_skip(mode, previous, force)
        _ci_reviews[ticket] = {"job": job["id"], "mr": mr_iid, "key": key,
                               "code": ci_gate.EXIT_NEEDS_HUMAN if skip else None}
        timer = None if skip else _pending.pop(mr_iid, None)
    if skip:
        print(f"[webhook] MR !{mr_iid} 已審過(CI_REVIEW_REPEAT=first,上一次結束碼 {previous}),"
              f"這次不審、交人工(job {job['id']})", flush=True)
        threading.Thread(target=_announce_skipped, args=(mr_iid, previous), daemon=True).start()
        return 202, ticket
    if timer is not None:
        # webhook 那條路還在等安靜期的,不用再審一次:上面已經從 _pending 拿掉,計時器到了也不會開始
        # (見 _start 的身分比對);這裡取消只是省掉一次喚醒
        timer.cancel()
    if _cancel(mr_iid):
        print(f"[webhook] MR !{mr_iid} 取消進行中的舊審查,改審 CI 這次的", flush=True)
    print(f"[webhook] → CI 觸發審查 MR !{mr_iid}(job {job['id']},profile={PROFILE})", flush=True)
    threading.Thread(target=_run_review, args=(mr_iid, sha),
                     kwargs={"ci_ticket": ticket}, daemon=True).start()
    return 202, ticket


def _ci_status(job_token, ticket) -> tuple[int, str]:
    """GET /ci/review/<編號>:只有開始那份審查的 job 查得到。回 `running` 或 `done <結束碼>`。"""
    with _lock:
        entry = dict(_ci_reviews.get(ticket) or {})
    if not entry:
        return 404, "查不到這份審查(編號不對,或審查服務重新啟動過)"
    job = _ci_job(job_token)
    if not isinstance(job, dict) or job.get("id") != entry["job"] or job.get("status") != "running":
        return 403, "只有開始這份審查、而且還在執行中的 job 查得到"
    return 200, "running" if entry["code"] is None else f"done {entry['code']}"


def _is_draft(attrs: dict) -> bool:
    """MR 是不是 Draft。GitLab 較早的版本用 work_in_progress 這個欄位,兩個都認。"""
    return attrs.get("draft") is True or attrs.get("work_in_progress") is True


def _became_ready(changes) -> bool:
    """這次 update 是不是把 Draft 標成 Ready(changes 裡 draft 從 true 變成 false)。"""
    if not isinstance(changes, dict):
        return False
    for key in ("draft", "work_in_progress"):
        change = changes.get(key)
        if isinstance(change, dict) and change.get("previous") is True and change.get("current") is False:
            return True
    return False


def _review_command(note) -> bool:
    return isinstance(note, str) and _REVIEW_COMMAND.match(note) is not None


def _handle(payload: dict) -> str:
    """一個 webhook 事件要不要觸發審查;回傳回給 GitLab 的文字。觸發時排一份審查(_trigger)。"""
    kind = payload.get("object_kind")
    if kind == "merge_request":
        attrs = payload.get("object_attributes", {})
        action, iid = attrs.get("action"), attrs.get("iid")
        if action in TRIGGER_ACTIONS and iid is not None:
            if _is_draft(attrs):
                # Draft 不審:開發中反覆推 commit,每推一次就整份審一次。標成 Ready 時再審
                return "ignored (draft)"
            if action == "update" and not attrs.get("oldrev") and not _became_ready(
                    payload.get("changes")):
                # GitLab 的 action=update 不只代表推了新 commit:改 label、標題、
                # 描述、指派人都是 update——**包括審查自己最後 set_label 那一下**。
                # 只有推了新 commit 時才會帶 oldrev,所以用它分辨。Draft 標成 Ready 也是
                # 沒有 oldrev 的 update,由 changes 認出來另外審。
                # 不分辨的後果:每個 MR 至少多跑一次完整審查(自己的 set_label 觸發
                # 的),而且兩次決策不同時 label 又變、再觸發一次。
                return f"ignored (update 沒有新 commit)"
            return _trigger(int(iid), head_sha=attrs.get("last_commit", {}).get("id"))
        return f"ignored (action={action})"
    if kind == "note":                      # 討論串留言:只有開頭的 /review 才重審(Draft 也審)
        attrs = payload.get("object_attributes", {})
        mr = payload.get("merge_request", {})
        author_bot = payload.get("user", {}).get("username", "").startswith(("ai-", "root"))
        if (attrs.get("noteable_type") == "MergeRequest" and mr.get("iid")
                and _review_command(attrs.get("note")) and not author_bot):
            return _trigger(int(mr["iid"]), head_sha=mr.get("last_commit", {}).get("id"))
        return "ignored (note)"
    return f"ignored (kind={kind})"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, msg: str = "ok"):
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, "healthy")
        if self.path.startswith("/ci/review/"):
            return self._send(*_ci_status(self.headers.get("JOB-TOKEN", ""),
                                          self.path[len("/ci/review/"):]))
        self._send(404, "not found")

    def do_POST(self):
        if self.path == "/ci/review":
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > 4096:
                return self._send(413, "too large")
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._send(400, "bad json")
            return self._send(*_ci_start(self.headers.get("JOB-TOKEN", ""), body))
        if self.path != "/gitlab-webhook":
            return self._send(404, "not found")
        # 驗 secret(擋外部亂打)
        token = self.headers.get("X-Gitlab-Token", "")
        if not SECRET or not hmac.compare_digest(token, SECRET):
            print("[webhook] token 驗證失敗,拒絕", flush=True)
            return self._send(401, "unauthorized")
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, "bad json")

        return self._send(200, _handle(payload))

    def log_message(self, *a):    # 靜音預設 access log,只留自己的 print
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    if not SECRET:
        print("⚠️ 未設 GITLAB_WEBHOOK_SECRET,所有 webhook 請求都會被拒絕(請先 export)。",
              flush=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[webhook] 監聽 http://{args.host}:{args.port}/gitlab-webhook  profile={PROFILE}  "
          f"real_mode={'on' if os.environ.get('GITLAB_URL') else 'off(mock)'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
