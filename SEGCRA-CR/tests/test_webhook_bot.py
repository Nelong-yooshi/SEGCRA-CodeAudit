"""webhook 認得出機器人留的言:審查自己(GITLAB_TOKEN 的帳號)與機器人帳號的 `/review` 不觸發審查。

project / group access token 的帳號名稱是 GitLab 產生的(project_<id>_bot_<亂數>),不是我們取的
ai- 開頭;只看名稱前綴的話,審查自己的留言會被當成開發者的。所以服務啟動時向 GitLab 查一次
GITLAB_TOKEN 是哪一個帳號,之後用帳號 id 認;查不到時退回只看名稱。

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


# ---- 名稱

@pytest.mark.parametrize("username", ["project_57_bot_a1b2c3", "project_123_bot", "group_7_bot_x",
                                      "ai-reviewer", "root"])
def test_機器人帳號的名稱不觸發(ws, triggered, username):
    ws._handle(_note({"id": 999, "username": username}))
    assert triggered == []


@pytest.mark.parametrize("username", ["dev", "project_manager", "projectx_57_bot", "my_project_57_bot",
                                      "project__bot"])
def test_開發者的帳號照常觸發(ws, triggered, username):
    """只認 GitLab 產生的格式:名稱裡剛好有 project、bot 的開發者不能被擋掉。"""
    ws._handle(_note({"id": 999, "username": username}))
    assert triggered == [(7, HEAD)]


# ---- 帳號 id

def test_審查自己的帳號不觸發_不管名稱是什麼(ws, triggered, monkeypatch):
    monkeypatch.setattr(ws, "_own_user_id", OWN_ID)
    ws._handle(_note({"id": OWN_ID, "username": "segcra-review"}))
    assert triggered == []


def test_別的帳號照常觸發(ws, triggered, monkeypatch):
    monkeypatch.setattr(ws, "_own_user_id", OWN_ID)
    ws._handle(_note({"id": OWN_ID + 1, "username": "dev"}))
    assert triggered == [(7, HEAD)]


def test_查不到自己的帳號時只看名稱(ws, triggered, monkeypatch):
    monkeypatch.setattr(ws, "_own_user_id", None)
    ws._handle(_note({"id": OWN_ID, "username": "dev"}))
    assert triggered == [(7, HEAD)]


@pytest.mark.parametrize("user", [None, "dev", {}, {"id": str(OWN_ID), "username": "dev"},
                                  {"id": True, "username": "dev"}])
def test_帳號欄位格式不對時當成不是審查自己(ws, triggered, monkeypatch, user):
    """id 是字串或布林、沒有 user:不跟自己的 id 相等(True == 1 這種巧合也不算)。"""
    monkeypatch.setattr(ws, "_own_user_id", 1 if user == {"id": True, "username": "dev"} else OWN_ID)
    ws._handle(_note(user))
    assert triggered == [(7, HEAD)]


def test_處理_webhook_時不向_GitLab_查帳號(ws, triggered, monkeypatch):
    """GitLab 等 webhook 回應只等幾秒:帳號在啟動時查好,處理事件時不再打 API。"""
    def boom():
        raise AssertionError("處理 webhook 時不該查帳號")
    monkeypatch.setattr(gitlab, "token_user_id", boom)
    ws._handle(_note({"id": OWN_ID, "username": "dev"}))
    assert triggered == [(7, HEAD)]


def test_啟動時記下自己的帳號(ws, monkeypatch):
    monkeypatch.setattr(gitlab, "token_user_id", lambda: OWN_ID)
    ws._learn_own_account()
    assert ws._own_user_id == OWN_ID


def test_啟動時查不到就印一行說明(ws, monkeypatch, capsys):
    monkeypatch.setattr(gitlab, "token_user_id", lambda: None)
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    ws._learn_own_account()
    assert ws._own_user_id is None
    assert "只用名稱判斷" in capsys.readouterr().out


# ---- gitlab.token_user_id:向假的 GitLab 查

class _GitLab(BaseHTTPRequestHandler):
    reply = (200, {"id": OWN_ID, "username": "project_57_bot_x"})
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        _GitLab.seen.append((self.path, self.headers.get("PRIVATE-TOKEN")))
        code, body = _GitLab.reply
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
    monkeypatch.setattr(fake_gitlab, "reply", (200, {"id": OWN_ID}))
    assert gitlab.token_user_id() == OWN_ID
    assert fake_gitlab.seen == [("/api/v4/user", "tok-test")]


@pytest.mark.parametrize("reply", [(401, {"id": OWN_ID, "message": "401 Unauthorized"}), (200, {"id": "42"}),
                                   (200, {"id": True}), (200, {}), (200, [1])])
def test_查不到或格式不對時回_None(fake_gitlab, monkeypatch, reply):
    monkeypatch.setattr(fake_gitlab, "reply", reply)
    assert gitlab.token_user_id() is None


def test_連不上時回_None(monkeypatch):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", "http://127.0.0.1:9")
    assert gitlab.token_user_id() is None


def test_mock_模式不查(monkeypatch):
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    assert gitlab.token_user_id() is None


def test_服務啟動時先查帳號再開始收_webhook(ws, monkeypatch):
    order = []

    class FakeServer:
        def __init__(self, *a):
            order.append("listen")

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            pass

    monkeypatch.setattr(ws, "_learn_own_account", lambda: order.append("learn"))
    monkeypatch.setattr(ws, "ThreadingHTTPServer", FakeServer)
    monkeypatch.setattr(sys, "argv", ["webhook_server.py", "--port", "0"])
    ws.main()
    assert order == ["learn", "listen"]
