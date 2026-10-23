"""主審查的模型呼叫失敗時,確定性的結果不跟著消失。

連不上、逾時、端點錯誤時,用空報告走完後處理:規則命中等程式確定性檢查照常報出,補一條 major
「審查模型沒有完成」、交人工。對外只寫問題類型與處理方式,不寫例外訊息(主機位址、端點回應)。
取消(webhook 的總時間上限、新 commit 取消舊審查)照樣往外傳。
"""
import asyncio
import json

import httpx
import openai
import pytest

from orchestrator import pipeline
from orchestrator.config import load_config
from toolbox import gitlab

SECRET = "ZZ_HOST_192.0.2.10:443_ZZ"     # 例外訊息裡的內部細節(RFC 5737 的範例位址),不可以出現在報告與 log
_REQ = httpx.Request("POST", "http://model.invalid/v1/chat/completions")


def _status(cls, code):
    return cls(SECRET, response=httpx.Response(code, request=_REQ), body=None)


ERRORS = [
    (openai.APITimeoutError(request=_REQ), "timeout"),
    (asyncio.TimeoutError(), "timeout"),
    (TimeoutError(SECRET), "timeout"),
    (openai.APIConnectionError(message=SECRET, request=_REQ), "connection"),
    (_status(openai.RateLimitError, 429), "rate_limited"),
    (_status(openai.BadRequestError, 400), "rejected"),
    (_status(openai.AuthenticationError, 401), "rejected"),
    (_status(openai.InternalServerError, 500), "server_error"),
    (_status(openai.APIStatusError, 503), "server_error"),
    (RuntimeError(SECRET), "other"),
    (ValueError(SECRET), "other"),
]


@pytest.mark.parametrize("error,kind", ERRORS)
def test_問題類型(error, kind):
    assert pipeline._model_failure_kind(error) == kind


def test_狀態碼拿不到時當成拒絕():
    err = _status(openai.APIStatusError, 500)
    err.status_code = None
    assert pipeline._model_failure_kind(err) == "rejected"


@pytest.fixture
def review(monkeypatch, tmp_path):
    fixtures, output = tmp_path / "fixtures", tmp_path / "out"
    fixtures.mkdir()
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    def _run(sql, error, *, partial=None, baseline=False):
        (fixtures / "mr_962.json").write_text(json.dumps({
            "title": "調整", "description": "例行維護", "sha": "e1b0" + "0" * 36,
            "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+" + sql, "full_content": sql}],
        }, ensure_ascii=False), encoding="utf-8")

        async def failing_run_agent(cfg, profile, system, user, hub, meta=None, **kw):
            if meta is not None and partial:
                meta.update(partial)             # 失敗前已經跑了幾輪
            raise error

        monkeypatch.setattr(pipeline, "run_agent", failing_run_agent)
        return asyncio.run(pipeline.review_mr(load_config(), "962", baseline=baseline)), output
    return _run


def _failed(report):
    return [f for f in report["findings"] if f["title"] == pipeline._FAILED_TITLE]


@pytest.mark.parametrize("error,kind", ERRORS)
def test_確定性結果照常報出_補major_不得放行(review, error, kind):
    report, _ = review("DELETE FROM t", error)
    assert any(f["title"].startswith("[R001]") for f in report["findings"])
    [f] = _failed(report)
    assert f["severity"] == "major" and pipeline._MODEL_FAILURE_TEXT[kind] in f["detail"]
    assert report["decision"] != "auto_approved"
    assert report["_output_meta"]["failed"] == kind


def test_沒有任何確定性命中時也交人工(review):
    report, _ = review("SELECT id FROM t WHERE id = 1", openai.APIConnectionError(request=_REQ))
    assert len(_failed(report)) == 1 and report["decision"] == "needs_human"


@pytest.mark.parametrize("error,kind", ERRORS)
def test_報告與log不帶例外訊息與類別名稱(review, capsys, error, kind):
    report, output = review("DELETE FROM t", error)
    text = json.dumps(report, ensure_ascii=False)
    posted = "".join(p.read_text(encoding="utf-8") for p in output.rglob("*.json"))
    logged = capsys.readouterr().out
    for where in (text, posted, logged):
        assert SECRET not in where and type(error).__name__ not in where
    assert f"主審查模型呼叫失敗({kind})" in logged


def test_失敗前已記下的用量照實記(review):
    partial = {"prompt_tokens_total": 1200, "completion_tokens_total": 80}
    report, _ = review("DELETE FROM t", openai.APITimeoutError(request=_REQ), partial=partial)
    meta = report["_output_meta"]
    assert (meta["prompt_tokens_total"], meta["completion_tokens_total"], meta["failed"]) == \
        (1200, 80, "timeout")


def test_前幾輪的用量達到上限也不算截斷_只補沒有完成那條(review):
    """模型在第二輪以後才失敗時,meta 裡的用量是前一輪的;這次沒有最終輸出,不能說它被截斷。"""
    partial = {"finish_reason": "length", "completion_tokens": 10 ** 9}
    report, _ = review("DELETE FROM t", openai.APITimeoutError(request=_REQ), partial=partial)
    titles = [f["title"] for f in report["findings"]]
    assert pipeline._FAILED_TITLE in titles and pipeline._TRUNCATED_TITLE not in titles
    assert report["_output_meta"]["truncated"] is False


def test_沒有失敗時截斷照常判斷():
    rec = pipeline._output_meta_record({"finish_reason": "length"}, 8192)
    assert rec["truncated"] is True and rec["failed"] is None


def test_失敗時不寫原始輸出檔(review):
    _, output = review("DELETE FROM t", RuntimeError(SECRET))
    assert not list(output.rglob("mr_962_raw.txt"))


def test_取消照樣往外傳(review):
    with pytest.raises(asyncio.CancelledError):
        review("DELETE FROM t", asyncio.CancelledError())


def test_baseline模式照舊丟例外(review):
    """評測用的對照組:失敗就該讓評測看到。"""
    with pytest.raises(RuntimeError):
        review("DELETE FROM t", RuntimeError("x"), baseline=True)


# ─────────────────── 後處理的單元 ───────────────────

def test_模型呼叫失敗時_解析回空報告():
    out = pipeline._parse_model_output(None, {"failed": "timeout"}, 8192)
    assert out == {"summary": pipeline._FAILED_SUMMARY, "findings": []}


def test_沒有失敗時照常解析():
    out = pipeline._parse_model_output('{"summary": "x", "findings": []}', {"failed": None}, 8192)
    assert out == {"summary": "x", "findings": []}


def test_不認得的類型記成_other():
    assert pipeline._output_meta_record({"failed": "ZZ"}, 8192)["failed"] == "other"
    report = pipeline.enforce_truncation({"findings": []}, {"failed": "ZZ"}, 8192)
    [f] = _failed(report)
    assert pipeline._MODEL_FAILURE_TEXT["other"] in f["detail"] and "ZZ" not in f["detail"]


def test_沒有失敗時不補(review):
    report = pipeline.enforce_truncation({"findings": []}, {"finish_reason": "stop"}, 8192)
    assert _failed(report) == [] and report["_output_meta"]["failed"] is None


def test_模型自己輸出同標題的info會被取代():
    fake = {"file": "(MR 內容)", "line": 0, "severity": "info", "title": pipeline._FAILED_TITLE,
            "detail": "沒事", "suggestion": "", "citations": []}
    report = pipeline.enforce_truncation({"findings": [fake]}, {"failed": "connection"}, 8192)
    [f] = _failed(report)
    assert f["severity"] == "major" and f["detail"] != "沒事"
