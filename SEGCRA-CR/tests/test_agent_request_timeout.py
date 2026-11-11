"""模型閘道的請求上限:LLM_SEND_TIMEOUT=1 時,請求內容要帶 timeout。

模型前面若有 LiteLLM 這類閘道,閘道對每次請求有自己的上限(可能只有幾十秒),超過就切斷。
用戶端的 LLM_TIMEOUT 再長也沒用;要把上限放進請求內容,閘道才會照著改。

這裡除了攔 kwargs,也起一個假的閘道、用真的 openai 套件打過去:確認 extra_body
真的被併進送出去的 JSON,而不只是「我們有傳這個參數」。
"""
import asyncio
import inspect
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import orchestrator.agent as agent
from orchestrator.config import Config, ModelProfile


def _cfg(endpoint="http://x/v1") -> Config:
    return Config(endpoint=endpoint, api_key="k", default_profile="review",
                  profiles={}, roles={}, budget={"diff": 8000}, policy={})


def _profile(**over) -> ModelProfile:
    base = dict(model="m", num_ctx=32768, temperature=0.1, max_output_tokens=8192)
    return ModelProfile(**{**base, **over})


def _run(cfg, profile=None):
    return asyncio.run(agent.run_agent(cfg, profile or _profile(), "sys", "user", None,
                                       verbose=False, use_tools=False))


# ---- 攔 kwargs

@pytest.fixture
def captured(monkeypatch):
    seen = []

    class _Msg:
        tool_calls = None
        content = '{"findings": []}'

        def model_dump(self, exclude_none=False):
            return {"role": "assistant", "content": self.content}

    class _Choice:
        message = _Msg()
        finish_reason = "stop"

    class _Usage:
        prompt_tokens = 999999
        completion_tokens = 10

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    class _Completions:
        async def create(self, **kw):
            seen.append(kw)
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _FakeClient:
        def __init__(self, **_):
            self.chat = _Chat()

        async def close(self):
            pass

    monkeypatch.setattr(agent, "AsyncOpenAI", _FakeClient)
    return seen


def test_預設不送timeout(monkeypatch, captured):
    monkeypatch.setattr(agent, "SEND_TIMEOUT", False)
    _run(_cfg())
    assert "extra_body" not in captured[0]


def test_開啟時請求內容帶timeout(monkeypatch, captured):
    monkeypatch.setattr(agent, "SEND_TIMEOUT", True)
    monkeypatch.setattr(agent, "REQUEST_TIMEOUT", 1234.0)
    _run(_cfg())
    assert captured[0]["extra_body"] == {"timeout": 1234.0}


def test_開啟時其他參數照送(monkeypatch, captured):
    monkeypatch.setattr(agent, "SEND_TIMEOUT", True)
    _run(_cfg(), _profile(seed=42, reasoning_effort="low"))
    kw = captured[0]
    assert kw["seed"] == 42 and kw["reasoning_effort"] == "low"
    assert kw["extra_body"] == {"timeout": agent.REQUEST_TIMEOUT}


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("", False), ("0", False), ("false", False), ("no", False), ("off", False), ("亂寫", False),
])
def test_環境變數的解讀(raw, expected):
    """只有明確開啟才送;讀不懂的值當成不送(跟預設一樣,不會多送端點不認得的欄位)。

    直接測解讀的函式,不 reload 模組:reload 會換掉 agent 裡的函式物件,別的測試比對
    「是不是同一個函式」時就會失敗。
    """
    assert agent._flag_on(raw) is expected


def test_模組讀的是_LLM_SEND_TIMEOUT_且預設不送():
    """讀哪個變數、沒設時的預設值:看原始碼,不 reload(理由同上)。"""
    src = inspect.getsource(agent)
    assert 'SEND_TIMEOUT = _flag_on(os.environ.get("LLM_SEND_TIMEOUT", ""))' in src
    assert agent._flag_on("") is False


# ---- 假閘道 + 真的 openai 套件

class _Gateway(BaseHTTPRequestHandler):
    """像 LiteLLM:請求內容沒有 timeout 就回它逾時時的 400;有的話正常回答。"""
    bodies = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Gateway.bodies.append(body)
        if "timeout" not in body:
            out = {"status": "error", "error_message": {
                "error_code": "PROXY_ERR",
                "description": "litellm.Timeout: Connection timed out. Timeout passed=30.0"}}
            code = 400
        else:
            out = {"id": "x", "object": "chat.completion", "model": body["model"],
                   "choices": [{"index": 0, "finish_reason": "stop",
                                "message": {"role": "assistant", "content": '{"findings": []}'}}],
                   "usage": {"prompt_tokens": 999999, "completion_tokens": 5, "total_tokens": 1000004}}
            code = 200
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def gateway():
    srv = HTTPServer(("127.0.0.1", 0), _Gateway)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _Gateway.bodies = []
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()
    srv.server_close()


def test_經過閘道時開啟就成功(monkeypatch, gateway):
    monkeypatch.setattr(agent, "SEND_TIMEOUT", True)
    out = _run(_cfg(gateway))
    assert out == '{"findings": []}'
    assert _Gateway.bodies[0]["timeout"] == agent.REQUEST_TIMEOUT


def test_經過閘道時沒開就被切斷(monkeypatch, gateway):
    """對照組:同一個閘道、沒開時要失敗,才證明上面那條是因為帶了 timeout 才過。"""
    monkeypatch.setattr(agent, "SEND_TIMEOUT", False)
    with pytest.raises(Exception) as info:
        _run(_cfg(gateway))
    assert "Timeout passed=30.0" in str(info.value)
    assert "timeout" not in _Gateway.bodies[0]
