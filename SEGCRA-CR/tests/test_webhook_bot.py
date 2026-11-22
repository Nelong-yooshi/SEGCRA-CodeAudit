"""webhook 認得出機器人留的言:審查自己(GITLAB_TOKEN 的帳號)與機器人帳號的 `/review` 不觸發審查。

project / group access token 的帳號名稱是 GitLab 產生的(project_<id>_bot_<亂數>),不是我們取的
ai- 開頭;只看名稱前綴的話,審查自己的留言會被當成開發者的。所以服務啟動時向 GitLab 查
GITLAB_TOKEN 是哪一個帳號,之後用帳號 id 認;查不到時背景重查,這段期間退回只看名稱。

不起 webhook 的 HTTP 伺服器:直接呼叫 `_handle`。查帳號的部分起一個假的 GitLab。
"""
import importlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from toolbox import gitlab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

HEAD = "b" * 40
OWN_ID = 42


@pytest.fixture(autouse=True)
def _no_own_user(monkeypatch):
    monkeypatch.setattr(gitlab, "_own_user", None)


def _own(monkeypatch, uid=OWN_ID, name="segcra-review"):
    monkeypatch.setattr(gitlab, "_own_user", {"id": uid, "username": name})


@pytest.fixture()
def ws(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "x")
    import webhook_server as mod
    importlib.reload(mod)
    return mod


@pytest.fixture()
def triggered(ws, monkeypatch):
    calls = []
    monkeypatch.setattr(ws, "_trigger", lambda iid, head_sha=None: calls.append((iid, head_sha)) or "ok")
    return calls


def _note(user):
    return {"object_kind": "note", "user": user,
            "object_attributes": {"noteable_type": "MergeRequest", "note": "/review"},
            "merge_request": {"iid": 7, "draft": False, "last_commit": {"id": HEAD}}}


class _NoThread:
    """換掉 threading.Thread:記下要開的背景執行緒,不真的開。"""
    started: list = []

    def __init__(self, target, args=(), daemon=None):
        _NoThread.started.append((target, args, daemon))

    def start(self):
        pass


@pytest.fixture()
def no_thread(ws, monkeypatch):
    _NoThread.started = []
    monkeypatch.setattr(ws.threading, "Thread", _NoThread)
    return _NoThread.started


# ---- 名稱:GitLab 產生的 access token 帳號一律是機器人

@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("username", ["project_57_bot_a1b2c3", "project_123_bot", "group_7_bot_x"])
def test_access_token_帳號的名稱不觸發(ws, triggered, monkeypatch, username, known):
    if known:
        _own(monkeypatch)
    ws._handle(_note({"id": 999, "username": username}))
    assert triggered == []


@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("username", ["dev", "project_manager", "projectx_57_bot", "my_project_57_bot",
                                      "project__bot"])
def test_開發者的帳號照常觸發(ws, triggered, monkeypatch, username, known):
    """只認 GitLab 產生的格式:名稱裡剛好有 project、bot 的開發者不能被擋掉。"""
    if known:
        _own(monkeypatch)
    ws._handle(_note({"id": 999, "username": username}))
    assert triggered == [(7, HEAD)]


# ---- 認得自己的帳號時,ai-、root 不算機器人

@pytest.mark.parametrize("username", ["ai-ling", "ai-chen", "rootlee", "root", "ai-reviewer"])
def test_認得自己的帳號時_ai_與_root_的名稱照常觸發(ws, triggered, monkeypatch, username):
    _own(monkeypatch)
    ws._handle(_note({"id": 999, "username": username}))
    assert triggered == [(7, HEAD)]


@pytest.mark.parametrize("username", ["ai-reviewer", "ai-ling", "root"])
def test_查不到自己的帳號時退回名稱判斷(ws, triggered, username):
    ws._handle(_note({"id": 999, "username": username}))
    assert triggered == []


@pytest.mark.parametrize("username", ["rootlee", "root2", "myroot", "xai-ling"])
def test_退回名稱判斷時_root_要整個相等_ai_要在開頭(ws, triggered, username):
    ws._handle(_note({"id": 999, "username": username}))
    assert triggered == [(7, HEAD)]


# ---- 帳號 id

def test_審查自己的帳號不觸發_不管名稱是什麼(ws, triggered, monkeypatch):
    _own(monkeypatch)
    ws._handle(_note({"id": OWN_ID, "username": "segcra-review"}))
    assert triggered == []


def test_別的帳號照常觸發(ws, triggered, monkeypatch):
    _own(monkeypatch)
    ws._handle(_note({"id": OWN_ID + 1, "username": "dev"}))
    assert triggered == [(7, HEAD)]


def test_查不到自己的帳號時只看名稱(ws, triggered):
    ws._handle(_note({"id": OWN_ID, "username": "dev"}))
    assert triggered == [(7, HEAD)]


@pytest.mark.parametrize("user", [None, "dev", {}, {"id": str(OWN_ID), "username": "dev"},
                                  {"id": True, "username": "dev"}])
def test_帳號欄位格式不對時當成不是審查自己(ws, triggered, monkeypatch, user):
    """id 是字串或布林、沒有 user:不跟自己的 id 相等(True == 1 這種巧合也不算)。"""
    _own(monkeypatch, uid=1 if user == {"id": True, "username": "dev"} else OWN_ID)
    ws._handle(_note(user))
    assert triggered == [(7, HEAD)]


def test_名稱不是字串時照樣用帳號_id_認(ws, triggered, monkeypatch):
    _own(monkeypatch)
    ws._handle(_note({"id": OWN_ID, "username": None}))
    assert triggered == []


def test_處理_webhook_時不向_GitLab_查帳號(ws, triggered, monkeypatch):
    """GitLab 等 webhook 回應只等幾秒:帳號在啟動時(或背景)查好,處理事件時不再打 API。"""
    def boom():
        raise AssertionError("處理 webhook 時不該查帳號")
    monkeypatch.setattr(gitlab, "learn_own_user", boom)
    ws._handle(_note({"id": OWN_ID, "username": "dev"}))
    assert triggered == [(7, HEAD)]


# ---- 啟動時查、查不到就背景重查

# 背景重查的保險:查了這麼多次還沒停,就是迴圈停不下來(查到了卻還在查),讓測試失敗而不是卡住
MAX_RETRIES = 20


def _learn_returns(monkeypatch, *results):
    """learn_own_user 依序回這些結果(用完就一直回最後一個);None 表示查到(順便記下帳號)。
    回 (呼叫次數的清單, 保險用的 stop):查超過 MAX_RETRIES 次時 stop 會被設起來。"""
    seq, calls, guard = list(results), [], threading.Event()

    def learn():
        calls.append(1)
        if len(calls) >= MAX_RETRIES:
            guard.set()
        r = seq.pop(0) if len(seq) > 1 else seq[0]
        if r is None:
            gitlab._own_user = {"id": OWN_ID, "username": "segcra-reviewer"}
        return r
    monkeypatch.setattr(gitlab, "learn_own_user", learn)
    return calls, guard


def test_啟動時記下自己的帳號_並寫出名稱與_id(ws, monkeypatch, no_thread):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    _learn_returns(monkeypatch, None)
    line = ws._learn_own_account()
    assert gitlab.own_user_id() == OWN_ID
    assert "segcra-reviewer" in line and f"id {OWN_ID}" in line and "/review" in line
    assert no_thread == [], "查到了就不用重查"


def test_mock_模式不查也不重查(ws, monkeypatch, no_thread):
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    _learn_returns(monkeypatch, "不該被呼叫")
    assert ws._learn_own_account() is None and no_thread == []


def test_啟動時查不到就寫出原因並開背景重查(ws, monkeypatch, no_thread):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    reason = "GET /user 連不上 GitLab(ConnectError)"
    _learn_returns(monkeypatch, reason)
    line = ws._learn_own_account()
    assert reason in line and "重查" in line
    assert no_thread == [(ws._retry_own_account, (reason,), True)]


def test_第一次查不到_之後查得到_之後審查帳號的_review_不觸發(ws, triggered, monkeypatch, capsys, no_thread):
    """啟動時 GitLab 剛好連不上:背景重查到之後,審查帳號(名稱不是機器人格式)的 /review 就不觸發。"""
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(ws, "OWN_ACCOUNT_RETRY_SECONDS", 0.01)
    calls, guard = _learn_returns(monkeypatch, "原因 A", "原因 A", None)
    ws._learn_own_account()
    reviewer = {"id": OWN_ID, "username": "segcra-reviewer"}
    ws._handle(_note(reviewer))                 # 還沒查到:只看名稱,會觸發
    assert triggered == [(7, HEAD)]
    target, args, _ = no_thread[0]
    target(*args, stop=guard)                   # 背景那條執行緒做的事
    assert len(calls) == 3, "查到了就要停"
    assert gitlab.own_user_id() == OWN_ID
    assert "segcra-reviewer" in capsys.readouterr().out
    triggered.clear()
    ws._handle(_note(reviewer))
    assert triggered == []


def test_背景重查_同一個原因不重複印_原因變了才印(ws, monkeypatch, capsys):
    monkeypatch.setattr(ws, "OWN_ACCOUNT_RETRY_SECONDS", 0.01)
    calls, guard = _learn_returns(monkeypatch, "原因 A", "原因 A", "原因 B", "原因 B", None)
    ws._retry_own_account("原因 A", guard)
    assert len(calls) == 5, "查到了就要停"
    out = capsys.readouterr().out
    assert out.count("原因 A") == 0 and out.count("原因 B") == 1
    assert out.strip().splitlines()[-1].startswith("[webhook] GITLAB_TOKEN 的帳號:")


def test_背景重查可以停下來(ws, monkeypatch):
    monkeypatch.setattr(ws, "OWN_ACCOUNT_RETRY_SECONDS", 0.01)
    stop = threading.Event()
    calls = []

    def learn():
        calls.append(1)
        if len(calls) == 3:
            stop.set()
        return "原因 A"
    monkeypatch.setattr(gitlab, "learn_own_user", learn)
    ws._retry_own_account("原因 A", stop)
    assert len(calls) == 3 and gitlab.own_user_id() is None


# ---- gitlab.learn_own_user:向假的 GitLab 查

class _GitLab(BaseHTTPRequestHandler):
    reply = (200, {"id": OWN_ID, "username": "project_57_bot_x"})
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _GitLab.seen.append((self.path, self.headers.get("PRIVATE-TOKEN")))
        code, body = _GitLab.reply
        html = isinstance(body, str)
        data = body.encode() if html else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html" if html else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def fake_gitlab(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _GitLab)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _GitLab.seen = []
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", "tok-test")
    yield _GitLab
    srv.shutdown()
    srv.server_close()


def test_查到_token_的帳號(fake_gitlab, monkeypatch):
    monkeypatch.setattr(fake_gitlab, "reply", (200, {"id": OWN_ID, "username": "segcra-reviewer"}))
    assert gitlab.learn_own_user() is None
    assert (gitlab.own_user_id(), gitlab.own_username()) == (OWN_ID, "segcra-reviewer")
    assert fake_gitlab.seen == [("/api/v4/user", "tok-test")]


@pytest.mark.parametrize("code", [401, 403])
def test_token_被拒絕時說是_GITLAB_TOKEN(fake_gitlab, monkeypatch, code):
    monkeypatch.setattr(fake_gitlab, "reply", (code, {"id": OWN_ID, "message": f"{code} Unauthorized"}))
    reason = gitlab.learn_own_user()
    assert reason.startswith(f"GET /user 回 {code}") and "GITLAB_TOKEN 被拒絕" in reason
    assert gitlab.own_user_id() is None


def test_其他狀態碼寫出狀態碼(fake_gitlab, monkeypatch):
    monkeypatch.setattr(fake_gitlab, "reply", (502, {"message": "bad gateway"}))
    assert gitlab.learn_own_user() == "GET /user 回 502"
    assert gitlab.own_user_id() is None


@pytest.mark.parametrize("body", ["<html>請登入</html>", {"id": "42"}, {"id": True}, {}, [1], None])
def test_回的不是帳號資料時說出來(fake_gitlab, monkeypatch, body):
    """代理的登入頁(HTML)、沒有數字的 id:不是 token 的問題,是 GITLAB_URL 指錯。"""
    monkeypatch.setattr(fake_gitlab, "reply", (200, body))
    reason = gitlab.learn_own_user()
    assert "不是帳號資料" in reason and "GITLAB_URL" in reason
    assert gitlab.own_user_id() is None


def test_連不上時寫出例外型別_不帶原文(monkeypatch):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", "tok-test")
    reason = gitlab.learn_own_user()
    assert reason.startswith("GET /user 連不上 GitLab(ConnectError)")
    assert "127.0.0.1" not in reason, "例外原文帶網址,不印"


def test_GITLAB_URL_沒寫_http_時寫出例外型別(monkeypatch):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", "gitlab.internal:8929")
    reason = gitlab.learn_own_user()
    assert reason.startswith("GET /user 連不上 GitLab(") and "http://" in reason
    assert "gitlab.internal" not in reason


def test_mock_模式不查(monkeypatch):
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    assert gitlab.learn_own_user() == "mock 模式,沒有 GitLab"
    assert gitlab.own_user_id() is None


@pytest.mark.parametrize("name,kept", [("segcra-reviewer", "segcra-reviewer"), ("壞\x1b[31m名", ""),
                                       (None, ""), (123, "")])
def test_帳號名稱只留印得出來的字(fake_gitlab, monkeypatch, name, kept):
    """名稱會印進 log:有控制字元(終端機跳脫碼)的不印。"""
    monkeypatch.setattr(fake_gitlab, "reply", (200, {"id": OWN_ID, "username": name}))
    assert gitlab.learn_own_user() is None and gitlab.own_username() == kept


def test_服務啟動時先查帳號再開始收_webhook_並印出帳號(ws, monkeypatch, capsys):
    order = []

    class FakeServer:
        def __init__(self, *a):
            order.append("listen")

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            pass

    monkeypatch.setattr(ws, "_learn_own_account", lambda: order.append("learn") or "[webhook] 帳號那一行")
    monkeypatch.setattr(ws, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(sys, "argv", ["webhook_server.py", "--port", "0"])
    ws.main()
    assert order == ["learn", "listen"]
    assert "[webhook] 帳號那一行" in capsys.readouterr().out
