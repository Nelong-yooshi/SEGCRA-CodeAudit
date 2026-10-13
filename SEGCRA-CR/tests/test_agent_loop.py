"""run_agent 的迴圈邊界——用假的 AsyncOpenAI,不連端點。

重點是「工具輪次用光」那條路:`run_agent` 回傳的是 `messages[-1]`,也就是
**最後一則工具結果**,不是模型的報告。那則結果是 JSON 物件時 `extract_json`
會把它當成報告收下,而最後一次模型呼叫的 `finish_reason` 是 `"stop"`——
呼叫端從 meta 完全看不出發生了這件事(同事在 #26 第 1 點實測到的)。
所以由 run_agent 自己標記 `meta["exhausted"]`:只有它知道迴圈是跑完的,
還是中途 return 出去的。
"""
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import agent as agent_mod  # noqa: E402


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, exclude_none=False):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [{"id": t.id, "function": {
                "name": t.function.name, "arguments": t.function.arguments}}
                for t in self.tool_calls]
        return d


class _TC:
    def __init__(self, name, args, tid="call_1"):
        self.id = tid
        self.function = types.SimpleNamespace(name=name, arguments=args)


def _resp(msg, finish="stop", completion=10):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=msg, finish_reason=finish)],
        usage=types.SimpleNamespace(prompt_tokens=100, completion_tokens=completion))


def _fake_client(monkeypatch, responses):
    """responses 依序回傳;用完就一直回最後一個(模擬模型一直要求呼叫工具)。"""
    seq = list(responses)
    calls = []

    async def create(**kw):
        calls.append(kw)
        return seq[len(calls) - 1] if len(calls) <= len(seq) else seq[-1]

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    monkeypatch.setattr(agent_mod, "AsyncOpenAI", lambda **kw: client)
    return calls


class _Hub:
    openai_tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]

    # #29 之後模型只能走白名單的 call_from_model(),不是 call()
    async def call_from_model(self, name, args):
        # 工具結果刻意是一個合法的 JSON 物件:這正是危險的情況——
        # extract_json 會把它當成報告,而且看起來完全正常。
        return json.dumps({"findings": [], "summary": "我是工具結果,不是報告"},
                          ensure_ascii=False)


class _Cfg:
    endpoint = "http://x/v1"
    api_key = "k"


_PROFILE = types.SimpleNamespace(model="m", temperature=0.0, max_output_tokens=4096,
                                 seed=None, reasoning_effort="")


def _run(monkeypatch, responses, use_tools=True):
    _fake_client(monkeypatch, responses)
    meta: dict = {}
    out = asyncio.run(agent_mod.run_agent(
        _Cfg(), _PROFILE, "sys", "user", _Hub(), verbose=False,
        use_tools=use_tools, meta=meta))
    return out, meta


def test_工具輪次用光時要標記exhausted(monkeypatch):
    """模型每一輪都要求呼叫工具 → 12 輪用完,回傳的是最後一則工具結果。
    沒有這個標記,呼叫端會把工具結果當成審查報告,而且 finish_reason 是 "stop",
    看起來一切正常。
    """
    always_tool = _resp(_Msg(content=None, tool_calls=[_TC("t", "{}")]))
    out, meta = _run(monkeypatch, [always_tool])
    assert meta["exhausted"] is True
    assert "我是工具結果" in out, "回傳的本來就是工具結果——這正是要讓呼叫端知道的事"
    assert meta["finish_reason"] == "stop", "端點回 stop,所以只靠 finish_reason 偵測不到"


def test_正常跑完時exhausted是False(monkeypatch):
    out, meta = _run(monkeypatch, [_resp(_Msg(content='{"findings": []}'))])
    assert meta["exhausted"] is False
    assert out == '{"findings": []}'


def test_exhausted一開始就寫進meta(monkeypatch):
    """呼叫端要分得出「舊版程式沒有這個 key」與「跑完沒用光輪次」,
    所以不能只在用光時才寫。"""
    _fake_client(monkeypatch, [_resp(_Msg(content="x"))])
    meta: dict = {}
    asyncio.run(agent_mod.run_agent(_Cfg(), _PROFILE, "s", "u", None,
                                    verbose=False, use_tools=False, meta=meta))
    assert "exhausted" in meta


def test_沒給meta也不能炸(monkeypatch):
    """meta 是選用的:正式審查以外的呼叫端(測資生成、仲裁)不一定會傳。"""
    _fake_client(monkeypatch, [_resp(_Msg(content=None, tool_calls=[_TC("t", "{}")]))])
    out = asyncio.run(agent_mod.run_agent(_Cfg(), _PROFILE, "s", "u", _Hub(),
                                          verbose=False, use_tools=True))
    assert out
