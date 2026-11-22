"""每一次請求只拿這一環節剩下的時間:剩不到 MIN_STAGE_SECONDS 就不送;環節裡單一次請求逾時(含重試
用完),記成那個環節沒跑完(帶 stage,pipeline.review_incomplete 認得到)。"""
import asyncio
import json
import types

import httpx
import openai
import pytest

from orchestrator import agent, pipeline, spec_exec
from orchestrator.config import Config, ModelProfile, load_config
from toolbox import gitlab

_REQ = httpx.Request("POST", "http://model.invalid/v1/chat/completions")
_PROFILE = ModelProfile(model="m", num_ctx=1000, temperature=0.0, max_output_tokens=100)


class _Client:
    def __init__(self):
        self.calls = 0

        async def create(**kw):
            self.calls += 1
            msg = types.SimpleNamespace(content='{"ok": true}', tool_calls=None)
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=msg, finish_reason="stop")],
                usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5))
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=create))


def _cfg():
    return Config(endpoint="http://x/v1", api_key="ollama", default_profile="review",
                  profiles={"review": vars(_PROFILE)}, roles={}, budget={}, policy={})


def test_剩下的時間不夠就不送請求():
    client = _Client()

    async def go():
        with agent.llm_deadline(5, min_left=30):
            await agent._run_agent(client, _cfg(), _PROFILE, "s", "u", None,
                                   verbose=False, use_tools=False)
    with pytest.raises(agent.LlmNoTimeLeft):
        asyncio.run(go())
    assert client.calls == 0


class _Sent(Exception):
    pass


def test_時間夠就照送():
    sent = []

    async def create(**kw):
        sent.append(kw["timeout"])
        raise _Sent()
    client = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(create=create)))

    async def go():
        with agent.llm_deadline(600, min_left=30):
            await agent._run_agent(client, _cfg(), _PROFILE, "s", "u", None,
                                   verbose=False, use_tools=False)
    with pytest.raises(_Sent):
        asyncio.run(go())
    assert len(sent) == 1 and 500 < sent[0] <= 600


def test_巢狀時門檻取較大的_截止時間取較早的():
    with agent.llm_deadline(600, min_left=10):
        with agent.llm_deadline(900, min_left=30):
            left, need = agent._time_left()
    assert need == 30 and left <= 600


def test_不夠時間與逾時都算時間到了():
    assert agent.is_timeout(agent.LlmNoTimeLeft("x"))
    assert agent.is_timeout(openai.APITimeoutError(request=_REQ))
    assert not agent.is_timeout(openai.APIConnectionError(request=_REQ))


# ─────────── 主審查 ───────────

@pytest.fixture
def review(monkeypatch, tmp_path):
    fixtures, output = tmp_path / "fixtures", tmp_path / "out"
    fixtures.mkdir()
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    def _run(error):
        (fixtures / "mr_981.json").write_text(json.dumps({
            "title": "t", "description": "d", "sha": "e1b0" + "0" * 36,
            "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+SELECT id FROM t WHERE id = 1",
                       "full_content": "SELECT id FROM t WHERE id = 1"}],
        }), encoding="utf-8")

        async def failing(*a, **k):
            raise error
        monkeypatch.setattr(pipeline, "run_agent", failing)
        return asyncio.run(pipeline.review_mr(load_config(), "981"))
    return _run


@pytest.mark.parametrize("error", [openai.APITimeoutError(request=_REQ), agent.LlmNoTimeLeft("x")])
def test_主審查時間到了_記成主審查沒跑完(review, error):
    report = review(error)
    [f] = [f for f in report["findings"] if f["title"] == pipeline._FAILED_TITLE]
    assert f["stage"] == "主審查"
    assert pipeline._FAILED_TITLE in pipeline.review_incomplete(report)
    assert report["decision"] != "auto_approved"


def test_主審查連不上_不算沒跑完(review):
    report = review(openai.APIConnectionError(request=_REQ))
    [f] = [f for f in report["findings"] if f["title"] == pipeline._FAILED_TITLE]
    assert "stage" not in f


# ─────────── 測資生成、仲裁 ───────────

def test_測資生成每次都逾時_記成測資生成沒跑完(monkeypatch):
    calls = []

    async def timing_out(*a, **k):
        calls.append(1)
        raise openai.APITimeoutError(request=_REQ)
    monkeypatch.setattr(spec_exec, "run_agent", timing_out)
    cfg = load_config()
    mr = {"files": [{"path": "sql/rules/r201.sql", "full_content": "SELECT 1;"}]}
    r = asyncio.run(spec_exec.run_spec_exec(cfg, None, mr, "R-201", "規格內容"))
    [f] = [f for f in r["findings"] if f.get("stage")]
    assert f["stage"] == spec_exec.STAGE_GENERATE_NAME and r["passed"] is False
    assert len(calls) > 1          # 時間還夠時照樣重試


def test_測資生成剩的時間不夠時不再重試(monkeypatch):
    calls = []

    async def no_time(*a, **k):
        calls.append(1)
        raise agent.LlmNoTimeLeft("x")
    monkeypatch.setattr(spec_exec, "run_agent", no_time)
    cfg = load_config()
    mr = {"files": [{"path": "sql/rules/r201.sql", "full_content": "SELECT 1;"}]}
    r = asyncio.run(spec_exec.run_spec_exec(cfg, None, mr, "R-201", "規格內容"))
    assert len(calls) == 1
    assert [f["stage"] for f in r["findings"] if f.get("stage")] == [spec_exec.STAGE_GENERATE_NAME]


def _plan():
    case = lambda cid, d, fl: {"case_id": cid, "condition_id": "C1", "direction": d,  # noqa: E731
                               "rows": [["transactions", 1, "2026-06-01 10:00:00"]],
                               "expect_flagged": fl, "note": cid}
    return {"schema_ddl": ["CREATE TABLE transactions (id INT, tx_time DATETIME);"],
            "conditions": [{"id": "C1", "desc": "條件 C1"}],
            "cases": [case("C1-T", "true", True), case("C1-F", "false", False)]}


@pytest.mark.parametrize("error", [openai.APITimeoutError(request=_REQ), agent.LlmNoTimeLeft("x")])
def test_仲裁時間到了_記成仲裁沒有結論_不混進行為不符(monkeypatch, error):
    async def fake_generate(cfg, code, text, profile_name=None):
        return _plan(), [], []

    def fake_execute(sql, plan_):
        return {"engine": "stub", "sandbox_error": None, "testdata_error": None, "sql_error": None,
                "case_results": [{"case_id": "C1-T"}, {"case_id": "C1-F"}],
                "mismatches": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
                                "note": "邊界", "rows": [["transactions", 1]],
                                "expect_flagged": True, "actual_flagged": False, "actual_rows": 0}]}

    async def timing_out(*a, **k):
        raise error
    monkeypatch.setattr(spec_exec, "generate_cases", fake_generate)
    monkeypatch.setattr(spec_exec, "execute_cases", fake_execute)
    monkeypatch.setattr(spec_exec, "run_agent", timing_out)
    mr = {"files": [{"path": "sql/rules/r201.sql", "full_content": "SELECT 1;"}]}
    r = asyncio.run(spec_exec.run_spec_exec(load_config(), None, mr, "R-201", "規格內容"))
    assert r["passed"] is False
    [f] = [f for f in r["findings"] if f.get("stage")]
    assert f["stage"] == spec_exec.STAGE_ARBITRATE_NAME and "仲裁沒有結論" in f["title"]
    assert not [f for f in r["findings"] if f["title"] == "執行驗證失敗:實作與規格行為不符"]
