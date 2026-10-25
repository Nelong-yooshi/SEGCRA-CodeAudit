"""量收尾要多久:評分、決策、比對 head、逐條貼回 MR、設 label 與 commit status。

整份審查的截止時間 = REVIEW_TIMEOUT_SECONDS − 收尾保留(FINISH_RESERVE_SECONDS),這支量的就是
收尾保留該留多少。用真的 review_mr 與真的 toolbox/gitlab.py(real 模式),GitLab 換成本機的
假伺服器,每個請求固定延遲 --latency 秒(部署端的 GitLab 量到多少就填多少)。模型的三步
(主審查、冷讀、執行驗證)換成立刻回傳的替身:它們的時間跟模型主機有關,這裡不量。

finding 的數量用 MR 的檔案數控制:每個檔案一句沒有 WHERE 的 DELETE(R001,程式確定性補報),
另外主審查替身回 --model-findings 條。

    python eval/measure_finish.py --files 10 50 100 --latency 0 0.2

輸出每一組的 finding 數、各階段秒數(報告的 `_stage_seconds`)與收尾時打了幾次 GitLab API。
不連任何外部服務;假伺服器只聽 127.0.0.1。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))

HEAD = "e1f0" + "0" * 36
PROJECT = "1"


class _FakeGitLab(BaseHTTPRequestHandler):
    """只回這支用得到的端點;每個請求先睡 latency 秒,並記下是哪一種請求。"""
    latency = 0.0
    files: dict[str, str] = {}
    counts: Counter = Counter()
    lock = threading.Lock()

    def log_message(self, *args):          # 不印每一個請求
        pass

    def _reply(self, status, body, headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self, method):
        time.sleep(self.latency)
        path = self.path.split("?", 1)[0]
        prefix = f"/api/v4/projects/{PROJECT}"
        rest = path[len(prefix):] if path.startswith(prefix) else path
        if method == "GET" and rest.endswith("/diffs"):
            kind = "GET diffs"
            items = [{"new_path": p, "old_path": p, "diff": "@@ -0,0 +1 @@\n+" + t}
                     for p, t in self.files.items()]
            body, headers = items, {"X-Next-Page": ""}
        elif method == "GET" and rest.startswith("/merge_requests/"):
            kind = "GET MR"
            body = {"title": "量測用", "description": "", "sha": HEAD,
                    "diff_refs": {"base_sha": "b" * 40, "head_sha": HEAD, "start_sha": "b" * 40}}
            headers = {}
        elif method == "GET" and "/repository/files/" in rest:
            kind = "GET 檔案"
            from urllib.parse import unquote
            name = unquote(rest.split("/repository/files/", 1)[1].rsplit("/raw", 1)[0])
            if name not in self.files:
                with self.lock:
                    self.counts[kind] += 1
                return self._reply(404, {"message": "404 File Not Found"})
            body, headers = self.files[name].encode("utf-8"), {}
        elif method == "POST" and rest.endswith("/discussions"):
            kind, body, headers = "POST 行內討論", {"id": "d"}, {}
        elif method == "POST" and rest.endswith("/notes"):
            kind, body, headers = "POST 一般留言", {"id": 1}, {}
        elif method == "PUT":
            kind, body, headers = "PUT label", {"iid": 1}, {}
        elif method == "POST" and "/statuses/" in rest:
            kind, body, headers = "POST commit status", {"id": 1}, {}
        else:
            kind, body, headers = f"其他 {method}", {"message": "404 Not Found"}, {}
            with self.lock:
                self.counts[kind] += 1
            return self._reply(404, body)
        with self.lock:
            self.counts[kind] += 1
        return self._reply(200, body, headers)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")


def _model_report(n: int) -> str:
    return json.dumps({"summary": "量測用", "findings": [
        {"file": "sql/q0.sql", "line": 1, "severity": "minor", "title": f"量測用第 {i} 條",
         "detail": "量測用", "suggestion": "量測用", "citations": []} for i in range(n)]},
        ensure_ascii=False)


def measure(files: int, model_findings: int, latency: float) -> dict:
    from orchestrator import pipeline
    from orchestrator.cold_read import ColdRead
    from orchestrator.config import load_config
    from toolbox import gitlab

    _FakeGitLab.latency = latency
    _FakeGitLab.files = {f"sql/q{i}.sql": f"DELETE FROM t{i}" for i in range(files)}
    _FakeGitLab.counts = Counter()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGitLab)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    saved = {k: getattr(gitlab, k) for k in
             ("REAL_MODE", "GITLAB_URL", "GITLAB_TOKEN", "GITLAB_PROJECT", "GITLAB_READ_TOKEN")}
    gitlab.REAL_MODE = True
    gitlab.GITLAB_URL = f"http://127.0.0.1:{server.server_port}"
    gitlab.GITLAB_TOKEN = "test-write-token"
    gitlab.GITLAB_READ_TOKEN = "test-read-token"
    gitlab.GITLAB_PROJECT = PROJECT

    async def run_agent(cfg, profile, system, user, hub, meta=None, **kw):
        if meta is not None:
            meta.update({"finish_reason": "stop", "completion_tokens": 100})
        return _model_report(model_findings)

    async def cold(cfg, mr, head_texts=None):
        return ColdRead(flagged=False)

    async def spec_exec(cfg, hub, mr, *a, **kw):
        return {"passed": True, "findings": []}

    superseded = pipeline._superseded
    before_finish: Counter = Counter()

    async def mark_then_check(*a, **kw):     # 收尾的第一個 GitLab 請求在這裡:先記下之前打了幾次
        before_finish.update(_FakeGitLab.counts)
        return await superseded(*a, **kw)

    originals = (pipeline.run_agent, pipeline._cold_read, pipeline.run_spec_exec)
    pipeline.run_agent, pipeline._cold_read, pipeline.run_spec_exec = run_agent, cold, spec_exec
    pipeline._superseded = mark_then_check
    try:
        cfg = load_config()
        cfg.dbt["enabled"] = False          # 量的是收尾,不展開 dbt
        report = asyncio.run(pipeline.review_mr(cfg, "1"))
    finally:
        pipeline.run_agent, pipeline._cold_read, pipeline.run_spec_exec = originals
        pipeline._superseded = superseded
        for k, v in saved.items():
            setattr(gitlab, k, v)
        server.shutdown()
    return {"files": files, "latency": latency, "findings": len(report["findings"]),
            "stage_seconds": report.get("_stage_seconds"),
            "finish_requests": dict(_FakeGitLab.counts - before_finish)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--files", type=int, nargs="+", default=[10, 50, 100])
    ap.add_argument("--model-findings", type=int, default=20)
    ap.add_argument("--latency", type=float, nargs="+", default=[0.0, 0.2])
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory() as out:
        os.environ["REVIEW_OUTPUT"] = out       # 主審查原始輸出寫到暫存目錄
        for lat in args.latency:
            for n in args.files:
                r = measure(n, args.model_findings, lat)
                print(json.dumps(r, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
