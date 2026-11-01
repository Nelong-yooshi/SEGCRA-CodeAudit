"""`run_agent` 回填的 token 用量(#84)——不呼叫真的模型。

要回答的是「每份 MR 的審查讀進多少、生成多少」。只記最後一次呼叫的話,有工具
呼叫的審查一輪會跑好幾次、每次都把整串對話重讀一遍,總量遠大於最後一次 —— 工具用
得多的 MR 反而看起來很便宜,跟模型端點那邊記的總量對不起來。

`_output_meta_record`(pipeline)已經在等這三個欄位,沒有回填的話報告裡永遠是 None。
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from orchestrator import agent
from orchestrator.config import Config, ModelProfile
from orchestrator.tool_hub import ToolHub


def _usage(prompt, completion):
    if prompt is None and completion is None:
        return None
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


class _Msg(SimpleNamespace):
    def model_dump(self, exclude_none=False):
        out = {"role": "assistant", "content": self.content}
        if getattr(self, "tool_calls", None):
            out["tool_calls"] = [{"id": t.id, "type": "function",
                                  "function": {"name": t.function.name,
                                               "arguments": t.function.arguments}}
                                 for t in self.tool_calls]
        return out


def _tool_call(i):
    return SimpleNamespace(id=f"c{i}", type="function",
                           function=SimpleNamespace(name="load_skill",
                                                    arguments=json.dumps({"name": "x"})))


def _client_with(rounds):
    """rounds:[(prompt_tokens, completion_tokens, 要不要叫工具), …] 逐輪回應。"""
    state = {"i": 0}

    class _FakeClient:
        def __init__(self, **_):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create))

        async def _create(self, **_kw):
            prompt, completion, wants_tool = rounds[state["i"]]
            state["i"] += 1
            msg = _Msg(content=None if wants_tool else '{"findings": []}',
                       reasoning_content=None,
                       tool_calls=[_tool_call(state["i"])] if wants_tool else None)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg,
                                         finish_reason="tool_calls" if wants_tool else "stop")],
                usage=_usage(prompt, completion))

    return _FakeClient


def _hub():
    hub = ToolHub()
    hub.register_local("load_skill", lambda name: "skill", "載入 skill",
                       {"type": "object", "properties": {"name": {"type": "string"}}})
    return hub


def _run(monkeypatch, rounds, use_tools=True):
    monkeypatch.setattr(agent, "AsyncOpenAI", _client_with(rounds))
    cfg = Config(endpoint="http://x/v1", api_key="k", default_profile="review",
                 profiles={}, roles={}, budget={"diff": 8000}, policy={})
    profile = ModelProfile(model="m", num_ctx=32768, temperature=0, max_output_tokens=8192)
    meta: dict = {}
    asyncio.run(agent.run_agent(cfg, profile, "sys", "user", _hub() if use_tools else None,
                                meta=meta, verbose=False, use_tools=use_tools))
    return meta


def test_單輪_最後一次與總量相同(monkeypatch):
    meta = _run(monkeypatch, [(1200, 80, False)], use_tools=False)
    assert meta["prompt_tokens"] == 1200
    assert meta["completion_tokens"] == 80
    assert meta["prompt_tokens_total"] == 1200
    assert meta["completion_tokens_total"] == 80


def test_有工具呼叫時總量是每一輪的加總(monkeypatch):
    """這就是 #84 要的數字:每一輪都把整串對話重讀一遍,總量遠大於最後一次。"""
    meta = _run(monkeypatch, [(1000, 20, True), (2000, 30, True), (3000, 40, False)])
    assert meta["prompt_tokens"] == 3000, "最後一次的語意不變"
    assert meta["completion_tokens"] == 40
    assert meta["prompt_tokens_total"] == 6000
    assert meta["completion_tokens_total"] == 90


def test_某一輪沒回報用量_那一輪不加(monkeypatch):
    """不要把 None 當 0 加進去之後還記成有值。"""
    meta = _run(monkeypatch, [(1000, 20, True), (None, None, True), (3000, 40, False)])
    assert meta["prompt_tokens_total"] == 4000
    assert meta["completion_tokens_total"] == 60
    assert meta["prompt_tokens"] == 3000


def test_沒回報用量的輪數記下來(monkeypatch):
    """沒回報的那幾輪不加進總量,所以總量是低估的——低估多少,光看總量看不出來。
    0 代表每一輪都有回報,不是「沒在記」。"""
    meta = _run(monkeypatch, [(1000, 20, True), (None, None, True), (3000, 40, False)])
    assert meta["rounds_without_usage"] == 1, "三輪裡只有中間那一輪沒回報"
    assert _run(monkeypatch, [(1000, 20, False)], use_tools=False)[
        "rounds_without_usage"] == 0


def test_只回報一邊也算沒回報(monkeypatch):
    """少的那一側一樣會讓總量偏低。"""
    meta = _run(monkeypatch, [(1000, None, False)], use_tools=False)
    assert meta["rounds_without_usage"] == 1


def test_整份都沒回報用量_總量是None(monkeypatch):
    """跟 completion_tokens 沒回報時同一個意思:鍵不存在,呼叫端讀到 None。"""
    meta = _run(monkeypatch, [(None, None, False)], use_tools=False)
    assert meta.get("prompt_tokens_total") is None
    assert meta.get("completion_tokens_total") is None
    assert meta.get("prompt_tokens") is None
    assert meta.get("completion_tokens") is None


def test_最後一輪沒回報_總量仍留著前面幾輪(monkeypatch):
    """最後一次是 None 不代表整份都沒讀到東西 —— 總量不可以跟著被蓋掉。"""
    meta = _run(monkeypatch, [(1000, 20, True), (None, None, False)])
    assert meta["prompt_tokens"] is None
    assert meta["prompt_tokens_total"] == 1000
    assert meta["completion_tokens_total"] == 20


def test_三個欄位會進到報告的_output_meta():
    """pipeline 那半已經合併(#88),這裡釘住兩邊對得上:欄位名改了就會紅。"""
    from orchestrator.pipeline import _output_meta_record
    meta = {"finish_reason": "stop", "completion_tokens": 40, "prompt_tokens": 3000,
            "prompt_tokens_total": 6000, "completion_tokens_total": 90}
    rec = _output_meta_record(meta, max_output_tokens=8192)
    assert rec["prompt_tokens"] == 3000
    assert rec["prompt_tokens_total"] == 6000
    assert rec["completion_tokens_total"] == 90
