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


# ─────────────────── 主審查的時間上限、端點無法使用時後面不再呼叫模型 ───────────────────

@pytest.fixture
def staged(monkeypatch, tmp_path):
    """review_mr 跑一次,記下冷讀與執行驗證有沒有被呼叫。main:主審查的模型呼叫(async 函式)。"""
    fixtures, output = tmp_path / "fixtures", tmp_path / "out"
    fixtures.mkdir()
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))
    calls = []

    async def cold(cfg, mr, head_texts=None):
        calls.append("cold")
        from orchestrator.cold_read import ColdRead
        return ColdRead(flagged=False)

    async def spec_exec(cfg, hub, mr, *a, **kw):
        calls.append("spec_exec")
        return {"passed": True, "findings": []}

    monkeypatch.setattr(pipeline, "_cold_read", cold)
    monkeypatch.setattr(pipeline, "run_spec_exec", spec_exec)

    def _run(main, sql="SELECT id FROM t WHERE id = 1", outer=30):
        (fixtures / "mr_963.json").write_text(json.dumps({
            "title": "調整", "description": "例行維護", "sha": "e1b1" + "0" * 36,
            "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+" + sql, "full_content": sql}],
        }, ensure_ascii=False), encoding="utf-8")

        async def run_agent(cfg, profile, system, user, hub, meta=None, **kw):
            calls.append("main")
            return await main()

        monkeypatch.setattr(pipeline, "run_agent", run_agent)
        report = asyncio.run(asyncio.wait_for(pipeline.review_mr(load_config(), "963"), outer))
        return report, calls
    return _run


def _stages(report):
    return {f.get("stage"): f for f in report["findings"] if f.get("stage")}


def test_端點不回應時_整份審查在上限內結束_確定性結果照常_後面不再呼叫模型(staged, monkeypatch):
    """端點收了連線卻一直不回:主審查到自己的上限就停,冷讀與測資生成不再呼叫模型,
    整份審查遠在外層的上限之前結束,確定性的結果照常報出,決策交人工。"""
    import time
    monkeypatch.setattr(pipeline, "MAIN_REVIEW_TIMEOUT_SECONDS", 0.3)

    async def hang():
        await asyncio.Event().wait()

    t0 = time.monotonic()
    report, calls = staged(hang, sql="DELETE FROM t", outer=20)
    assert time.monotonic() - t0 < 15
    assert calls == ["main"]                               # 冷讀、執行驗證都沒有呼叫
    assert any(f["title"].startswith("[R001]") for f in report["findings"])   # 確定性的結果
    [f] = _failed(report)
    assert pipeline._MODEL_FAILURE_TEXT["timeout"] in f["detail"]
    stages = _stages(report)
    for stage in ("冷讀", "執行驗證"):
        assert pipeline._ENDPOINT_DOWN_REASON in stages[stage]["detail"]
        assert stages[stage]["severity"] == "major"
    assert report["decision"] != "auto_approved"


def test_端點不回應而且沒有確定性命中時_決策交人工(staged, monkeypatch):
    monkeypatch.setattr(pipeline, "MAIN_REVIEW_TIMEOUT_SECONDS", 0.3)

    async def hang():
        await asyncio.Event().wait()

    report, calls = staged(hang)
    assert calls == ["main"] and report["decision"] == "needs_human"


def test_連不上時後面也不再呼叫模型(staged):
    async def refuse():
        raise openai.APIConnectionError(message=SECRET, request=_REQ)

    report, calls = staged(refuse)
    assert calls == ["main"]
    assert {"冷讀", "執行驗證"} <= set(_stages(report))


@pytest.mark.parametrize("error", [
    _status(openai.InternalServerError, 500), _status(openai.BadRequestError, 400),
    _status(openai.RateLimitError, 429), RuntimeError(SECRET),
])
def test_端點還在時_冷讀與執行驗證照常跑(staged, error):
    """端點錯誤、拒絕(例如主審查的輸入超過上限)、忙碌:不代表端點不能用,後面的呼叫照常試。"""
    async def fail():
        raise error

    report, calls = staged(fail)
    assert calls == ["main", "cold", "spec_exec"]
    assert "冷讀" not in _stages(report) and "執行驗證" not in _stages(report)


def test_主審查沒有失敗時_照常跑完(staged):
    async def ok():
        return json.dumps({"summary": "沒問題", "score": 100, "findings": []}, ensure_ascii=False)

    report, calls = staged(ok)
    assert calls == ["main", "cold", "spec_exec"] and _failed(report) == []


def test_主審查用自己的時間上限():
    seen = {}

    async def run(cfg, profile, system, user, hub, meta=None, **kw):
        await asyncio.sleep(5)

    async def go():
        import unittest.mock as m
        with m.patch.object(pipeline, "run_agent", run), \
                m.patch.object(pipeline, "MAIN_REVIEW_TIMEOUT_SECONDS", 0.05):
            return await pipeline._run_main_review(None, None, "", "", None, None, seen, "1")

    assert asyncio.run(go()) is None and seen["failed"] == "timeout"


def test_等主審查時外層被取消_模型呼叫一起取消():
    """webhook 的總時間上限、新 commit 取消舊審查:取消要傳進模型呼叫,不留下沒人收尾的請求。"""
    cancelled = []

    async def run(cfg, profile, system, user, hub, meta=None, **kw):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def go():
        import unittest.mock as m
        with m.patch.object(pipeline, "run_agent", run):
            task = asyncio.ensure_future(
                pipeline._run_main_review(None, None, "", "", None, None, {}, "1"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(go())
    assert cancelled == [True]


@pytest.mark.parametrize("env,expected,warned", [
    ({}, 1800, False),                                                  # 預設:整份 2400 留 600
    ({"MAIN_REVIEW_TIMEOUT_SECONDS": "900"}, 900, False),
    ({"MAIN_REVIEW_TIMEOUT_SECONDS": "2300"}, 1800, True),              # 太長:改回上限並警告
    ({"REVIEW_TIMEOUT_SECONDS": "3600"}, 3000, False),
    ({"REVIEW_TIMEOUT_SECONDS": "600"}, 300, False),                    # 整份很短:留一半
    ({"REVIEW_TIMEOUT_SECONDS": "600", "MAIN_REVIEW_TIMEOUT_SECONDS": "500"}, 300, True),
    ({"MAIN_REVIEW_TIMEOUT_SECONDS": "壞掉"}, 1800, False),             # 讀不懂:退回預設
])
def test_主審查的時間上限比整份短(monkeypatch, capsys, env, expected, warned):
    monkeypatch.delenv("REVIEW_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MAIN_REVIEW_TIMEOUT_SECONDS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert pipeline._main_review_timeout() == expected
    assert ("不比整份審查的上限" in capsys.readouterr().out) is warned


def test_沒有執行的環節_寫出原因():
    f = pipeline._incomplete_finding("冷讀", reason="端點不能用")
    assert f["stage"] == "冷讀" and f["severity"] == "major"
    assert "冷讀 沒有執行:端點不能用" in f["detail"] and "沒有執行" in f["title"]


def test_只有連不上與逾時算端點無法使用():
    assert pipeline._endpoint_unavailable({"failed": "timeout"})
    assert pipeline._endpoint_unavailable({"failed": "connection"})
    for kind in ("rate_limited", "rejected", "server_error", "other", None):
        assert not pipeline._endpoint_unavailable({"failed": kind})
