"""`run_agent` **實際送給端點的參數** — 不連網路,用假 client 攔下來檢查。

為什麼需要這一層:這個專案已經在「宣告了但沒送出去」上吃過兩次虧。

  * `num_ctx` 在 `models.yaml` 宣告了 32768,但 `agent.py` 從來沒把它送出去,
    一直到有人去量 context 才發現(而且就算送了,OpenAI 相容路徑也不吃)
  * `seed` 一度被認為「送了但被端點忽略」,查下去才發現**根本沒帶**

兩次的共同點是:**設定檔看起來是對的,沒有任何測試走到真正的呼叫**,
所以「有沒有真的送出去」沒有人守。現有測試全部把 `run_agent` 整個換掉,
驗的是它的回傳值,不是它送出去什麼。

這個檔案補的就是那一段:攔住 `chat.completions.create` 的 kwargs 直接斷言。
"""
import asyncio

import pytest

from orchestrator.agent import run_agent
from orchestrator.config import Config, ModelProfile


class _Msg:
    tool_calls = None
    content = '{"findings": []}'

    def model_dump(self, exclude_none=False):
        return {"role": "assistant", "content": self.content}


class _Choice:
    message = _Msg()
    finish_reason = "stop"


class _Usage:
    prompt_tokens = 999999      # 大一點,免得觸發「context 被截斷」的警告路徑
    completion_tokens = 10


class _Resp:
    choices = [_Choice()]
    usage = _Usage()


@pytest.fixture
def captured(monkeypatch):
    """把 AsyncOpenAI 換成假的,記下每次 create() 收到的 kwargs。"""
    seen = []

    class _Completions:
        async def create(self, **kw):
            seen.append(kw)
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _FakeClient:
        def __init__(self, **_):
            self.chat = _Chat()

    import orchestrator.agent as agent
    monkeypatch.setattr(agent, "AsyncOpenAI", _FakeClient)
    return seen


def _cfg() -> Config:
    return Config(endpoint="http://x/v1", api_key="k", default_profile="review",
                  profiles={}, roles={}, budget={"diff": 8000}, policy={})


def _profile(**over) -> ModelProfile:
    base = dict(model="m", num_ctx=32768, temperature=0.1, max_output_tokens=8192)
    return ModelProfile(**{**base, **over})


def _run(cfg, profile):
    """與專案其他 async 測試一致:用 asyncio.run,不引入 pytest-asyncio
    (多一個 CI 相依,為了幾個測試不值得)。"""
    return asyncio.run(run_agent(cfg, profile, "sys", "user", hub=None,
                                 verbose=False, use_tools=False))


# ─────────────────── reasoning_effort ───────────────────

def test_有設定就要真的送出去(captured):
    """這是整個 #25 的關鍵:設定了卻沒送,等於這個功能不存在,而且沒有徵兆。"""
    _run(_cfg(), _profile(reasoning_effort="none"))
    assert captured[0].get("reasoning_effort") == "none"


def test_沒設定就完全不帶這個欄位(captured):
    """不是送 None,是**整個 key 都不出現**。

    送 `reasoning_effort=None` 跟不送是兩回事:前者仍然是一個端點可能不認得的欄位,
    有被整個請求退回的風險。
    """
    _run(_cfg(), _profile())
    assert "reasoning_effort" not in captured[0]


@pytest.mark.parametrize("level", ["none", "minimal", "low", "medium", "high"])
def test_各個檔位都原樣送出(captured, level):
    _run(_cfg(), _profile(reasoning_effort=level))
    assert captured[0]["reasoning_effort"] == level


# ─────────────────── 迴歸:既有參數不能因此掉了 ───────────────────

def test_既有參數仍然照送(captured):
    """加新參數最容易出的錯是把旁邊的參數弄掉,而那一樣不會有徵兆。"""
    _run(_cfg(), _profile(seed=42, temperature=0.0, max_output_tokens=4096))
    kw = captured[0]
    assert kw["model"] == "m"
    assert kw["temperature"] == 0.0
    assert kw["max_tokens"] == 4096
    assert kw["seed"] == 42


def test_沒設定_seed_時一樣不帶那個欄位(captured):
    """seed 的既有語意不能被這次改動影響(能力報告要靠不帶 seed 恢復隨機)。"""
    _run(_cfg(), _profile(reasoning_effort="none"))
    assert "seed" not in captured[0]
    assert captured[0]["reasoning_effort"] == "none"
