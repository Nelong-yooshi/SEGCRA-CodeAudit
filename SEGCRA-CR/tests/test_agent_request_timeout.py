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
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import orchestrator.agent as agent
from orchestrator.config import Config, ModelProfile
from tests.test_review_truncation import CLEAN_REPORT, STOP, review  # noqa: F401 — review 是 fixture


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


FLAG = "SEGCRA_TEST_FLAG"


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("0", False), ("false", False), ("no", False), (" OFF ", False),
])
def test_開關讀得懂的值_不印警告(monkeypatch, capsys, raw, expected):
    """直接測解讀的函式,不 reload 模組:reload 會換掉 agent 裡的函式物件,別的測試比對
    「是不是同一個函式」時就會失敗。"""
    monkeypatch.setenv(FLAG, raw)
    assert agent._env_flag(FLAG, "") is expected
    assert "警告" not in capsys.readouterr().out


def test_開關沒設就是關_不印警告(monkeypatch, capsys):
    monkeypatch.delenv(FLAG, raising=False)
    assert agent._env_flag(FLAG, "") is False
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("raw", ["ture", "enable", "2", "亂寫", "", "   "])
def test_開關讀不懂的值_印警告並當成沒開(monkeypatch, capsys, raw):
    """review 實測:打錯一個字(ture、enable、2)就跟沒設一樣、又沒有任何輸出,而症狀(長請求
    被切斷)看起來跟這個設定無關。設了但是空的也說:多半是部署腳本沒把值帶進來。"""
    monkeypatch.setenv(FLAG, raw)
    assert agent._env_flag(FLAG, "") is False
    out = capsys.readouterr().out
    assert FLAG in out and "讀不懂" in out and repr(raw) in out


def test_模組讀的是_LLM_SEND_TIMEOUT_與_LLM_TIMEOUT():
    """讀哪個變數:看原始碼,不 reload(理由同上)。"""
    src = inspect.getsource(agent)
    assert 'SEND_TIMEOUT = _env_flag("LLM_SEND_TIMEOUT", "")' in src
    assert 'REQUEST_TIMEOUT = _env_timeout("LLM_TIMEOUT", 3600)' in src


# ---- LLM_TIMEOUT 要是有限的正數(它會放進請求內容)

@pytest.mark.parametrize("raw,expected", [("1800", 1800.0), ("90.5", 90.5), (" 60 ", 60.0)])
def test_請求逾時讀得懂的值(monkeypatch, capsys, raw, expected):
    monkeypatch.setenv(FLAG, raw)
    assert agent._env_timeout(FLAG, 3600.0) == expected
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "0", "-5", "abc", "1e400"])
def test_請求逾時不是有限的正數_退回預設並警告(monkeypatch, capsys, raw):
    """review 實測:LLM_TIMEOUT=inf 加 LLM_SEND_TIMEOUT=1 時,每次 run_agent 都在送出前丟
    `ValueError: Out of range float values are not JSON compliant`,所有模型呼叫都失敗。"""
    monkeypatch.setenv(FLAG, raw)
    assert agent._env_timeout(FLAG, 3600.0) == 3600.0
    out = capsys.readouterr().out
    assert FLAG in out and "有限的正數" in out


def test_請求逾時沒設或空的_用預設不警告(monkeypatch, capsys):
    monkeypatch.delenv(FLAG, raising=False)
    assert agent._env_timeout(FLAG, 3600.0) == 3600.0
    monkeypatch.setenv(FLAG, " ")
    assert agent._env_timeout(FLAG, 3600.0) == 3600.0
    assert capsys.readouterr().out == ""


# ---- 送出去的值 = 這次請求實際會等的時間

def test_沒有截止時間時_用戶端與閘道都用_LLM_TIMEOUT(monkeypatch, captured):
    monkeypatch.setattr(agent, "SEND_TIMEOUT", True)
    monkeypatch.setattr(agent, "REQUEST_TIMEOUT", 1234.0)
    _run(_cfg())
    assert captured[0]["timeout"] == 1234.0 and captured[0]["extra_body"] == {"timeout": 1234.0}


def test_這一環節剩的時間比較短時_送剩下的(monkeypatch, captured):
    monkeypatch.setattr(agent, "SEND_TIMEOUT", True)
    monkeypatch.setattr(agent, "REQUEST_TIMEOUT", 3600.0)
    with agent.llm_deadline(100):
        _run(_cfg())
    sent = captured[0]["extra_body"]["timeout"]
    assert 95 <= sent <= 100
    assert captured[0]["timeout"] == sent, "用戶端與閘道要用同一個值,兩邊同時到"


def test_沒開送閘道時_用戶端照樣用剩下的時間(monkeypatch, captured):
    monkeypatch.setattr(agent, "SEND_TIMEOUT", False)
    with agent.llm_deadline(100):
        _run(_cfg())
    assert "extra_body" not in captured[0] and 95 <= captured[0]["timeout"] <= 100


def test_剩的時間比_LLM_TIMEOUT_長時_用_LLM_TIMEOUT(monkeypatch):
    monkeypatch.setattr(agent, "REQUEST_TIMEOUT", 50.0)
    with agent.llm_deadline(100):
        assert agent.request_timeout() == 50.0


def test_巢狀時取較早的截止時間_離開後還原():
    with agent.llm_deadline(1000):
        with agent.llm_deadline(100):
            assert agent.request_timeout() <= 100
        with agent.llm_deadline(5000):
            assert 995 <= agent.request_timeout() <= 1000, "裡面那層比較晚,不能把截止時間延後"
        assert agent.request_timeout() > 100
    assert agent.request_timeout() == agent.REQUEST_TIMEOUT


def test_時間已經到了也至少送一秒():
    with agent.llm_deadline(-10):
        assert agent.request_timeout() == 1.0


def test_執行驗證只剩_100_秒_裡面的模型呼叫各自用那一刻剩的(monkeypatch, review):
    """整條 review_mr 走一次(review 這個 fixture 來自 test_review_truncation):執行驗證的上限
    100 秒,裡面的測資生成看到的約 100 秒;同一環節後面的呼叫剩的更少(sleep 代替前一次
    模型呼叫花掉的時間)。測資生成、重試、仲裁都是這樣各自算。"""
    from orchestrator import pipeline, spec_exec

    seen = []
    real_generate = spec_exec.generate_cases          # fixture 已經換成假的,這裡包一層記錄

    async def recording_generate(*a, **kw):
        seen.append(agent.request_timeout())
        await asyncio.sleep(1.2)
        seen.append(agent.request_timeout())
        return await real_generate(*a, **kw)

    monkeypatch.setattr(spec_exec, "generate_cases", recording_generate)
    monkeypatch.setattr(pipeline, "SPEC_EXEC_TIMEOUT_SECONDS", 100)
    report, _, _ = review(CLEAN_REPORT, STOP)
    assert report.get("_spec_exec"), "執行驗證要真的跑到"
    assert len(seen) == 2 and 95 <= seen[0] <= 100, seen
    assert seen[1] <= seen[0] - 1, seen


def test_主審查的每次請求_等的時間跟主審查的上限一致(monkeypatch):
    from orchestrator import pipeline
    seen = []

    async def fake_run_agent(*a, **kw):
        seen.append(agent.request_timeout())
        return "{}"

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline, "MAIN_REVIEW_TIMEOUT_SECONDS", 150)
    deadline = time.monotonic() + 10_000
    asyncio.run(pipeline._run_main_review(None, None, "s", "u", None, None, {}, "1", deadline))
    assert len(seen) == 1 and 145 <= seen[0] <= 150, seen


def test_整份審查剩的比主審查的上限少時_用整份剩的(monkeypatch):
    from orchestrator import pipeline
    seen = []

    async def fake_run_agent(*a, **kw):
        seen.append(agent.request_timeout())
        return "{}"

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline, "MAIN_REVIEW_TIMEOUT_SECONDS", 1800)
    deadline = time.monotonic() + 200
    asyncio.run(pipeline._run_main_review(None, None, "s", "u", None, None, {}, "1", deadline))
    assert 195 <= seen[0] <= 200, seen


def test_冷讀的每次請求_等的時間跟冷讀的上限一致(monkeypatch):
    from orchestrator import pipeline
    seen = []

    async def fake_stage(cfg, mr, dbt_ctx):
        seen.append(agent.request_timeout())
        return None, None

    monkeypatch.setattr(pipeline, "_cold_read_stage", fake_stage)
    monkeypatch.setattr(pipeline, "COLD_READ_TIMEOUT_SECONDS", 120)
    asyncio.run(pipeline._run_cold_read(None, {}, None, time.monotonic() + 10_000))
    assert len(seen) == 1 and 115 <= seen[0] <= 120, seen



# ---- 假閘道 + 真的 openai 套件

class _Gateway(BaseHTTPRequestHandler):
    """像 LiteLLM:請求內容沒有 timeout 就回它逾時時的 400;有的話正常回答。"""
    bodies = []
    codes = []       # 回了什麼狀態碼(驗失敗原因用,不靠 openai 套件的錯誤訊息格式)

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
        _Gateway.codes.append(code)
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
    _Gateway.codes = []
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()
    srv.server_close()


def test_經過閘道時開啟就成功(monkeypatch, gateway):
    monkeypatch.setattr(agent, "SEND_TIMEOUT", True)
    out = _run(_cfg(gateway))
    assert out == '{"findings": []}'
    assert _Gateway.bodies[0]["timeout"] == agent.REQUEST_TIMEOUT


def test_經過閘道時_送到閘道的是這一環節剩的時間(monkeypatch, gateway):
    """真的 openai 套件 + 假閘道:extra_body 的值真的是剩下的時間,而不是 LLM_TIMEOUT。"""
    monkeypatch.setattr(agent, "SEND_TIMEOUT", True)
    monkeypatch.setattr(agent, "REQUEST_TIMEOUT", 3600.0)
    with agent.llm_deadline(100):
        _run(_cfg(gateway))
    assert 95 <= _Gateway.bodies[0]["timeout"] <= 100


def test_經過閘道時沒開就被切斷(monkeypatch, gateway):
    """對照組:同一個閘道、沒開時要失敗,才證明上面那條是因為帶了 timeout 才過。

    失敗原因看閘道這邊的紀錄:收到的請求都沒有 timeout、回的都是逾時的 400(套件重試的話不只一次)。
    不比對例外的文字:openai 套件的錯誤訊息帶不帶回應內容,各版本不同。"""
    monkeypatch.setattr(agent, "SEND_TIMEOUT", False)
    with pytest.raises(Exception):
        _run(_cfg(gateway))
    assert _Gateway.codes and set(_Gateway.codes) == {400}
    assert all("timeout" not in body for body in _Gateway.bodies)
