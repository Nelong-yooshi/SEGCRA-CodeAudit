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
from orchestrator.config import load_config      # noqa: E402
from orchestrator.pipeline import review_mr       # noqa: E402

SECRET = os.environ.get("GITLAB_WEBHOOK_SECRET", "")
PROFILE = os.environ.get("REVIEW_PROFILE", "review")
TRIGGER_ACTIONS = {"open", "reopen", "update"}     # MR 這些動作觸發審查
_cfg = load_config()
_inflight: set = set()          # 進行中的 MR,避免同一 MR 短時間重複觸發(debounce)
_lock = threading.Lock()


def _run_review(mr_iid: int):
    try:
        report = asyncio.run(review_mr(_cfg, str(mr_iid), profile_name=PROFILE))
        print(f"[webhook] MR !{mr_iid} 審完:決策={report.get('decision')} "
              f"分數={report.get('score')} findings={len(report.get('findings', []))} "
              f"執行驗證={'通過' if report.get('_spec_exec', {}).get('passed') else '未通過'}",
              flush=True)
    except Exception as e:
        print(f"[webhook] MR !{mr_iid} 審查失敗:{e}", flush=True)
    finally:
        with _lock:
            _inflight.discard(mr_iid)


def _trigger(mr_iid: int) -> str:
    with _lock:
        if mr_iid in _inflight:
            print(f"[webhook] MR !{mr_iid} 審查中,略過(debounce)", flush=True)
            return "already running"
        _inflight.add(mr_iid)
    print(f"[webhook] → 觸發審查 MR !{mr_iid}(profile={PROFILE})", flush=True)
    threading.Thread(target=_run_review, args=(mr_iid,), daemon=True).start()
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
                return self._send(200, _trigger(int(iid)))
            return self._send(200, f"ignored (action={action})")
        if kind == "note":                      # 討論串留言:只有明確 /review 才重審(避免迴圈)
            attrs = payload.get("object_attributes", {})
            mr = payload.get("merge_request", {})
            author_bot = payload.get("user", {}).get("username", "").startswith(("ai-", "root"))
            if (attrs.get("noteable_type") == "MergeRequest" and mr.get("iid")
                    and "/review" in (attrs.get("note") or "") and not author_bot):
                return self._send(200, _trigger(int(mr["iid"])))
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
