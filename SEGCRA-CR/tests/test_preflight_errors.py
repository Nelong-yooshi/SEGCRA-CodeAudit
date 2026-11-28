"""eval/preflight.py 的探針遇到端點錯誤時,印出的結果與指紋不能帶錯誤原文。

端點回 401 時,openai 套件把錯誤內容放進例外訊息,裡面可能有收到的金鑰;preflight 的結果會印出來,
指紋會寫進 baseline 目錄(跟著進公開 repo)。只留型別與狀態碼。
"""
import asyncio
import json
import sys
import types
from pathlib import Path

import httpx
import openai

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import preflight  # noqa: E402

CANARY = "sk-canary-preflight-0000"
_REQ = httpx.Request("POST", "http://model.invalid/v1/chat/completions")


def _auth_error():
    return openai.AuthenticationError(
        f"Error code: 401 - Invalid API key provided: Bearer {CANARY}",
        response=httpx.Response(401, request=_REQ), body=None)


class _Client:
    def __init__(self):
        async def create(**kw):
            raise _auth_error()
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))


def test_探針遇到帶金鑰的_401_結果與指紋都不含金鑰(monkeypatch):
    import orchestrator.agent as agent

    async def boom(*a, **k):
        raise _auth_error()
    monkeypatch.setattr(agent, "run_agent", boom)
    client = _Client()
    cfg = preflight.load_config()

    async def go():
        return [await preflight.check_seed(client, "m"),
                await preflight.check_num_ctx(client, "m", 1000),
                await preflight.check_latency(client, "m"),
                await preflight.check_truncation_signal(cfg)]
    checks = asyncio.run(go())
    for c in checks:
        assert c.level == "fail", c
        assert CANARY not in c.detail and "Invalid API key" not in c.detail, c.detail
        assert "AuthenticationError" in c.detail and "HTTP 401" in c.detail, c.detail
    fp = preflight.build_fingerprint(checks, cfg, [cfg.default_profile])
    assert CANARY not in json.dumps(fp, ensure_ascii=False, default=str)
