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

審查完成後管線會回寫 commit status(name=segcra/review):
auto_approved → success;needs_human / blocked → failed(description 帶原因)。
搭配專案設定「Pipelines must succeed」+ protected branch,即為 CE 的 merge 閘門。
"""
import argparse
import asyncio
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))
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

# 每個 MR 一份進行中的紀錄:{mr_iid: (event_loop, task)}。
# 用它做兩件事:同一 MR 推新 commit 時取消舊的那次、以及知道誰還在跑。
_inflight: dict = {}
_lock = threading.Lock()


def _notify_incomplete(mr_iid: int, reason: str, head_sha: str | None = None) -> None:
    """審查沒完成時:在 MR 上留言,**並把 commit status 設成失敗**。

    留言只是給人看的;**合併閘門靠的是 commit status**(GitLab CE 的
    「Pipelines must succeed」)。只留言不設狀態,等於審查沒跑完卻沒有東西擋著合併
    ——那正是 #45 要消滅的「沒擋、沒放、也沒人知道」。

    兩個動作各自 try:留言失敗不該讓狀態也設不上,反之亦然。
    """
    body = (f"審查未完成({reason}),請人工處理。\n\n"
            f"這不是「沒問題」——是這次審查沒有跑完,沒有結論。")
    from toolbox import gitlab
    try:
        gitlab.post_summary(str(mr_iid), body, 0, "審查未完成")
        print(f"[webhook] MR !{mr_iid} 已留言告知審查未完成({reason})", flush=True)
    except Exception as e:
        print(f"[webhook] MR !{mr_iid} 連留言都失敗了:{type(e).__name__}: {e}", flush=True)
    try:
        sha = head_sha or ""
        if not sha:
            info = json.loads(gitlab.get_mr_diff(str(mr_iid)))
            sha = info.get("head_sha") or info.get("sha") or ""
        gitlab.set_commit_status(sha, "failed", "segcra/review",
                                 f"審查未完成:{reason}"[:200], str(mr_iid))
        print(f"[webhook] MR !{mr_iid} commit status 設為失敗(審查未完成)", flush=True)
    except Exception as e:
        print(f"[webhook] MR !{mr_iid} commit status 設定失敗:{type(e).__name__}: {e}",
              flush=True)


def _run_review(mr_iid: int, head_sha: str | None = None):
    """在自己的執行緒與事件迴圈裡跑一份審查。

    每個 MR 各一個執行緒 → 一個 MR 卡住或炸掉不影響其他 MR(#45)。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = None
    try:
        task = loop.create_task(
            asyncio.wait_for(review_mr(_cfg, str(mr_iid), profile_name=PROFILE),
                             timeout=REVIEW_TIMEOUT_SECONDS))
        with _lock:
            _inflight[mr_iid] = (loop, task)
        report = loop.run_until_complete(task)
        print(f"[webhook] MR !{mr_iid} 審完:決策={report.get('decision')} "
              f"分數={report.get('score')} findings={len(report.get('findings', []))} "
              f"執行驗證={'通過' if report.get('_spec_exec', {}).get('passed') else '未通過'}",
              flush=True)
    except asyncio.TimeoutError:
        print(f"[webhook] MR !{mr_iid} 審查逾時({REVIEW_TIMEOUT_SECONDS}s),中止", flush=True)
        _notify_incomplete(mr_iid, f"超過 {REVIEW_TIMEOUT_SECONDS} 秒的時間上限", head_sha)
    except asyncio.CancelledError:
        # 同一個 MR 推了新 commit → 舊的這次被取消。新的那次會給出結論,
        # 所以這裡不留言(免得同一個 MR 洗版)。
        print(f"[webhook] MR !{mr_iid} 舊審查被新的 commit 取代", flush=True)
    except Exception as e:
        print(f"[webhook] MR !{mr_iid} 審查失敗:{type(e).__name__}: {e}", flush=True)
        _notify_incomplete(mr_iid, f"審查程式發生 {type(e).__name__}", head_sha)
    finally:
        with _lock:
            if _inflight.get(mr_iid, (None, None))[1] is task:
                _inflight.pop(mr_iid, None)
        try:
            loop.close()
        except Exception:
            pass


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


def _trigger(mr_iid: int, head_sha: str | None = None) -> str:
    # 同一個 MR 推了新 commit:取消舊的那次,改審新的(#45)。
    # 原本是 debounce(略過新的那次),但那會讓**新的 commit 不被審查**——
    # 舊的那次審的是舊程式,結論貼到新的 commit 上是錯的。
    if _cancel(mr_iid):
        print(f"[webhook] MR !{mr_iid} 取消進行中的舊審查,改審新的", flush=True)
    print(f"[webhook] → 觸發審查 MR !{mr_iid}(profile={PROFILE})", flush=True)
    threading.Thread(target=_run_review, args=(mr_iid, head_sha), daemon=True).start()
    return f"review triggered for MR !{mr_iid}"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, msg: str = "ok"):
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, "healthy") if self.path == "/health" else self._send(404, "not found")

    def do_POST(self):
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

        kind = payload.get("object_kind")
        if kind == "merge_request":
            attrs = payload.get("object_attributes", {})
            action, iid = attrs.get("action"), attrs.get("iid")
            if action in TRIGGER_ACTIONS and iid is not None:
                if action == "update" and not attrs.get("oldrev"):
                    # GitLab 的 action=update 不只代表推了新 commit:改 label、標題、
                    # 描述、指派人都是 update——**包括審查自己最後 set_label 那一下**。
                    # 只有推了新 commit 時才會帶 oldrev,所以用它分辨。
                    # 不分辨的後果:每個 MR 至少多跑一次完整審查(自己的 set_label 觸發
                    # 的),而且兩次決策不同時 label 又變、再觸發一次。
                    return self._send(200, f"ignored (update 沒有新 commit)")
                return self._send(200, _trigger(int(iid), head_sha=attrs.get("last_commit", {}).get("id")))
            return self._send(200, f"ignored (action={action})")
        if kind == "note":                      # 討論串留言:只有明確 /review 才重審(避免迴圈)
            attrs = payload.get("object_attributes", {})
            mr = payload.get("merge_request", {})
            author_bot = payload.get("user", {}).get("username", "").startswith(("ai-", "root"))
            if (attrs.get("noteable_type") == "MergeRequest" and mr.get("iid")
                    and "/review" in (attrs.get("note") or "") and not author_bot):
                return self._send(200, _trigger(int(mr["iid"]),
                                                 head_sha=mr.get("last_commit", {}).get("id")))
            return self._send(200, "ignored (note)")
        return self._send(200, f"ignored (kind={kind})")

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
