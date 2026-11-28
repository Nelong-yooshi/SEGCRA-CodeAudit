"""/ci/review 向 GitLab 查 job、pipeline、MR 時,「取不到」跟「確定不符」分開說。

用真的 toolbox/gitlab.py 打一個本機的假 GitLab(不開始審查):GitLab 掛了、GITLAB_PROJECT 填錯、
查 MR 失敗,都不能說成「job token 無效」「job 不屬於這個專案」「MR 不是開啟中」。
取不到回 502、確定不符回 403、服務驗不了呼叫者(mock 模式、GITLAB_PROJECT 不是數字)回 503。
"""
import importlib
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from orchestrator import ci_gate
from toolbox import gitlab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

SHA = "c" * 40
PROJECT = "42"
TOKEN = "job-tok"
JOB = {"id": 900, "status": "running",
       "pipeline": {"id": 77, "project_id": 42, "sha": SHA, "ref": "refs/merge-requests/5/head"}}
PIPELINE = {"id": 77, "source": "merge_request_event", "sha": SHA, "ref": "refs/merge-requests/5/head"}
MR = {"iid": 5, "title": "t", "sha": SHA, "state": "opened", "source_project_id": 42}
JOB_PATH = "/api/v4/job"
PIPELINE_PATH = f"/api/v4/projects/{PROJECT}/pipelines/77"
MR_PATH = f"/api/v4/projects/{PROJECT}/merge_requests/5"


class _GitLab(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _GitLab.seen.append(self.path)
        code, body = _GitLab.routes.get(self.path, (404, {"message": "404 Not Found"}))
        if self.path == JOB_PATH and self.headers.get("JOB-TOKEN") != TOKEN and code == 200:
            code, body = 401, {"message": "401 Unauthorized"}
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def fake(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _GitLab)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _GitLab.routes = {JOB_PATH: (200, JOB), PIPELINE_PATH: (200, PIPELINE), MR_PATH: (200, MR)}
    _GitLab.seen = []
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", "api-tok")
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", PROJECT)
    yield _GitLab
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def ws(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "x")
    import webhook_server as mod
    importlib.reload(mod)
    started = []
    monkeypatch.setattr(mod, "_run_review", lambda *a, **k: started.append(a))
    mod._started = started
    return mod


def _start(ws, token=TOKEN, body=None):
    return ws._ci_start(token, body or {"mr_iid": 5, "sha": SHA})


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_三種不開始的情況用不同的_HTTP_碼():
    """job 只印得出 HTTP 碼:拒絕、查不到、服務驗不了,要分得出來。"""
    assert (ci_gate.STATUS_REJECTED, ci_gate.STATUS_LOOKUP_FAILED, ci_gate.STATUS_UNAVAILABLE) == (403, 502, 503)


def test_都對的時候開始審查(fake, ws):
    status, ticket = _start(ws)
    assert status == 202 and ticket
    assert fake.seen == [JOB_PATH, PIPELINE_PATH, MR_PATH]


# ─────────────────── 查 job(GET /job,帶 job token) ───────────────────

def test_GitLab_連不上時說查_job_失敗_不說_token_無效(fake, ws, monkeypatch):
    monkeypatch.setattr(gitlab, "GITLAB_URL", f"http://127.0.0.1:{_free_port()}")
    status, reason = _start(ws)
    assert status == ci_gate.STATUS_LOOKUP_FAILED
    assert reason.startswith("向 GitLab 查job(用 job token)失敗:") and "埠沒有服務" in reason
    assert ci_gate.JOB_TOKEN_REJECTED not in reason and ws._started == []


def test_GET_job_回_500_時說查_job_失敗(fake, ws):
    fake.routes[JOB_PATH] = (500, {"message": "500"})
    status, reason = _start(ws)
    assert status == ci_gate.STATUS_LOOKUP_FAILED and "查job(用 job token)失敗" in reason
    assert "伺服器錯誤(500)" in reason


def test_GitLab_拒絕這把_job_token_才說_token_無效(fake, ws):
    """測試 GitLab 上量過:亂打的 token、job 結束後的 token,GET /job 都回 401。"""
    fake.routes[JOB_PATH] = (401, {"message": "401 Unauthorized"})
    assert _start(ws) == (ci_gate.STATUS_REJECTED, ci_gate.JOB_TOKEN_REJECTED)
    assert fake.seen == [JOB_PATH]


@pytest.mark.parametrize("code", [403, 404, 429])
def test_GET_job_其他狀態碼算查不到_不說成_token_無效(fake, ws, code):
    fake.routes[JOB_PATH] = (code, {"message": str(code)})
    status, reason = _start(ws)
    assert status == ci_gate.STATUS_LOOKUP_FAILED and f"{code}" in reason
    assert ci_gate.JOB_TOKEN_REJECTED not in reason


def test_亂打的_token_被拒絕(fake, ws):
    assert _start(ws, token="亂打的") == (ci_gate.STATUS_REJECTED, ci_gate.JOB_TOKEN_REJECTED)


def test_GET_job_回的不是物件時說查不到(fake, ws):
    fake.routes[JOB_PATH] = (200, [1, 2])
    status, reason = _start(ws)
    assert status == ci_gate.STATUS_LOOKUP_FAILED and "內容不是預期的資料" in reason


def test_GET_job_回網頁時說不是_JSON(fake, ws):
    fake.routes[JOB_PATH] = (200, "<html>login</html>")
    status, reason = _start(ws)
    assert status == ci_gate.STATUS_LOOKUP_FAILED and "不是 JSON" in reason


def test_job_不屬於這個專案時不去查這個專案的_pipeline(fake, ws):
    fake.routes[JOB_PATH] = (200, {**JOB, "pipeline": {**JOB["pipeline"], "project_id": 41}})
    assert _start(ws) == (ci_gate.STATUS_REJECTED, "job 不屬於這個專案")
    assert fake.seen == [JOB_PATH]


# ─────────────────── GITLAB_PROJECT、mock 模式 ───────────────────

@pytest.mark.parametrize("project", ["grp%2Fproj", "grp/proj", "", " 42", "４２"])
def test_GITLAB_PROJECT_不是數字時說是_GITLAB_PROJECT_也不查_GitLab(fake, ws, monkeypatch, project):
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", project)
    status, reason = _start(ws)
    assert (status, reason) == (ci_gate.STATUS_UNAVAILABLE, ci_gate.PROJECT_NOT_NUMBER_TEXT)
    assert "GITLAB_PROJECT" in reason and fake.seen == []


def test_mock_模式寫明驗不了呼叫者(ws, monkeypatch):
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    status, reason = _start(ws)
    assert (status, reason) == (ci_gate.STATUS_UNAVAILABLE, ci_gate.MOCK_MODE_TEXT)
    assert "mock 模式" in reason and ws._started == []


def test_mock_模式的啟動說明標出_CI_那條路停用(ws, monkeypatch):
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    text = "\n".join(ws._startup_lines("0.0.0.0", 8000))
    assert "/ci/review(mock 模式,停用)" in text and ci_gate.MOCK_MODE_TEXT in text


def test_真實模式的啟動說明沒有_mock_字樣(ws, monkeypatch):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(ws, "SECRET", "s")
    text = "\n".join(ws._startup_lines("0.0.0.0", 8000))
    assert "mock" not in text.replace("real_mode=", "") or "off(mock)" in text
    assert "/ci/review(CI 的審查 job)" in text


# ─────────────────── 查 pipeline ───────────────────

@pytest.mark.parametrize("code", [403, 500, 404])
def test_查_pipeline_失敗時說查_pipeline_失敗(fake, ws, code):
    fake.routes[PIPELINE_PATH] = (code, {"message": str(code)})
    status, reason = _start(ws)
    assert status == ci_gate.STATUS_LOOKUP_FAILED
    assert reason.startswith("向 GitLab 查job 所在的 pipeline失敗:") and f"{code}" in reason
    assert MR_PATH not in fake.seen


def test_pipeline_查到了但不是_MR_pipeline_才是拒絕(fake, ws):
    fake.routes[PIPELINE_PATH] = (200, {**PIPELINE, "source": "push"})
    assert _start(ws) == (ci_gate.STATUS_REJECTED, "job 所在的 pipeline 不是 MR pipeline")


# ─────────────────── 查 MR ───────────────────

@pytest.mark.parametrize("code", [500, 403, 404])
def test_查_MR_失敗時不說成_MR_不是開啟中(fake, ws, code):
    fake.routes[MR_PATH] = (code, {"message": str(code)})
    status, reason = _start(ws)
    assert status == ci_gate.STATUS_LOOKUP_FAILED and reason.startswith("向 GitLab 查MR失敗:")
    assert "不是開啟中" not in reason and ws._started == []


def test_MR_查到了而且已經合併才是不是開啟中(fake, ws):
    fake.routes[MR_PATH] = (200, {**MR, "state": "merged"})
    assert _start(ws) == (ci_gate.STATUS_REJECTED, "MR 不是開啟中")


# ─────────────────── 審查進行中查狀態 ───────────────────

def _ticket(ws, fake):
    status, ticket = _start(ws)
    assert status == 202
    return ticket


def test_查狀態時_GitLab_連不上_說查不到而不是只有開始的_job_查得到(fake, ws, monkeypatch):
    ticket = _ticket(ws, fake)
    monkeypatch.setattr(gitlab, "GITLAB_URL", f"http://127.0.0.1:{_free_port()}")
    status, reason = ws._ci_status(TOKEN, ticket)
    assert status == ci_gate.STATUS_LOOKUP_FAILED and "查job(用 job token)失敗" in reason


def test_查狀態時_GET_job_回_500(fake, ws):
    ticket = _ticket(ws, fake)
    fake.routes[JOB_PATH] = (500, {"message": "500"})
    status, reason = ws._ci_status(TOKEN, ticket)
    assert status == ci_gate.STATUS_LOOKUP_FAILED and "伺服器錯誤(500)" in reason


def test_查狀態時_token_被拒絕(fake, ws):
    ticket = _ticket(ws, fake)
    fake.routes[JOB_PATH] = (401, {"message": "401"})
    assert ws._ci_status(TOKEN, ticket) == (ci_gate.STATUS_REJECTED, ci_gate.JOB_TOKEN_REJECTED)


def test_查狀態時_還在跑(fake, ws):
    ticket = _ticket(ws, fake)
    assert ws._ci_status(TOKEN, ticket) == (200, "running")


# ─────────────────── 原因只有固定文字 ───────────────────

@pytest.mark.parametrize("path", [JOB_PATH, PIPELINE_PATH, MR_PATH])
def test_原因不帶例外原文與主機位址(fake, ws, path):
    fake.routes[path] = (500, {"message": "內部 host-a.internal 錯誤"})
    status, reason = _start(ws)
    assert status == ci_gate.STATUS_LOOKUP_FAILED
    assert "127.0.0.1" not in reason and "host-a" not in reason


def test_mr_iid_是全形或上標數字時當成參數不對(ws):
    assert _start(ws, body={"mr_iid": "²", "sha": SHA})[0] == 400
