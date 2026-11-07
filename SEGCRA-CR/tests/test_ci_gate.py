"""審查做成部署端 CI 的最後一個 stage:審查結果 → job 的結束碼,以及 /ci/review 只認這個 MR 的 job。

| 審查結果 | 結束碼 | pipeline |
|---|---|---|
| auto_approved | 0 | 綠燈 |
| needs_human | 3(`allow_failure: exit_codes: [3]`) | passed with warnings |
| blocked | 4 | 紅燈 |
| 審查沒跑完整 | 5 | 紅燈 |

不起 HTTP 伺服器、不連 GitLab、不呼叫 LLM:GitLab 的查詢與 review_mr 換成假的。
"""
import asyncio
import importlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from orchestrator import ci_gate

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

SHA = "c" * 40
PROJECT = "42"


# ─────────────────── 結束碼 ───────────────────

@pytest.mark.parametrize("decision, code", [
    ("auto_approved", 0), ("needs_human", 3), ("blocked", 4)])
def test_審完的三種決策各有自己的結束碼(decision, code):
    assert ci_gate.exit_code({"decision": decision, "findings": []}) == code


@pytest.mark.parametrize("decision", ["auto_approved", "needs_human", "blocked"])
def test_審查沒跑完整一律是失敗的結束碼_不管決策是什麼(decision):
    report = {"decision": decision, "findings": [
        {"file": "(MR 內容)", "severity": "major", "title": "[lint] 審查未完整", "stage": "lint"}]}
    assert ci_gate.exit_code(report) == ci_gate.EXIT_INCOMPLETE == 5


@pytest.mark.parametrize("report", [
    None, "x", {}, {"decision": "x"}, {"decision": None},
    {"decision": "auto_approved", "writeback_skipped": "已有更新的 commit"},   # 被新的 commit 取代
    {"decision": "auto_approved", "findings": "壞掉的形狀"},
])
def test_看不懂的報告當成沒跑完整(report):
    assert ci_gate.exit_code(report) == ci_gate.EXIT_INCOMPLETE


def test_允許失敗的結束碼不跟程式異常的結束碼重疊():
    """job 自己出錯(Python 沒接住的例外是 1、參數錯誤是 2)不能被當成「審完了、交給人判斷」。"""
    assert ci_gate.EXIT_NEEDS_HUMAN not in (0, 1, 2)
    assert len(ci_gate.EXIT_CODES) == 4 and ci_gate.EXIT_APPROVED == 0


# ─────────────────── 確認呼叫者 ───────────────────

def _job(**kw):
    return {"id": 900, "status": "running",
            "pipeline": {"id": 77, "project_id": 42, "sha": SHA, "ref": "refs/merge-requests/5/head"},
            **kw}


def _pipeline(**kw):
    return {"id": 77, "source": "merge_request_event", "sha": SHA,
            "ref": "refs/merge-requests/5/head", **kw}


def _mr(**kw):
    return {"sha": SHA, "state": "opened", **kw}


def _check(job=None, pipeline=None, mr=None, **kw):
    args = {"project_id": PROJECT, "mr_iid": 5, "sha": SHA, **kw}
    return ci_gate.check_request(_job() if job is None else job,
                                 _pipeline() if pipeline is None else pipeline,
                                 _mr() if mr is None else mr, **args)


def test_這個_MR_的_MR_pipeline_裡正在跑的_job_才算():
    assert _check() is None


@pytest.mark.parametrize("change, reason", [
    (dict(job="不是物件"), "job token 無效"),
    (dict(job=_job(status="success")), "不是執行中"),
    (dict(job=_job(pipeline=None)), "pipeline"),
    (dict(job=_job(pipeline={"id": 77, "project_id": 41, "sha": SHA})), "不屬於這個專案"),
    (dict(pipeline=_pipeline(id=78)), "pipeline"),
    (dict(pipeline=_pipeline(source="push")), "不是 MR pipeline"),
    (dict(pipeline=_pipeline(source="web")), "不是 MR pipeline"),
    (dict(sha="d" * 40), "不是這條 pipeline 的 commit"),
    (dict(pipeline=_pipeline(ref="refs/merge-requests/6/head")), "不是這個 MR 的"),
    (dict(pipeline=_pipeline(ref="main")), "不是這個 MR 的"),
    (dict(mr=_mr(state="merged")), "不是開啟中"),
    (dict(mr=_mr(sha="d" * 40)), "MR 目前的 head"),
])
def test_其他呼叫一律拒絕(change, reason):
    got = _check(**change)
    assert got is not None and reason in got, got


def test_拒絕的原因不帶呼叫端送來的內容():
    got = _check(sha="<script>" + "x" * 40)
    assert got is not None and "<script>" not in got


# ─────────────────── /ci/review ───────────────────

@pytest.fixture()
def ws(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "x")
    import webhook_server as mod
    importlib.reload(mod)
    from toolbox import gitlab
    jobs = {"tok-900": _job(), "tok-901": _job(id=901)}
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", PROJECT)
    monkeypatch.setattr(gitlab, "job_from_token", lambda t: jobs.get(t))
    monkeypatch.setattr(gitlab, "pipeline_info", lambda pid: _pipeline() if pid == 77 else None)
    monkeypatch.setattr(gitlab, "mr_head", lambda iid: _mr() if iid == "5" else None)
    mod._test_jobs = jobs
    yield mod
    # 開始了的審查要等它結束:背景執行緒在替身還原之後才跑的話,會用到真的程式
    assert _wait(lambda: all(e["code"] is not None for e in mod._ci_reviews.values())),         "測試結束時還有審查沒跑完"


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _fake_review(ws, monkeypatch, report=None, *, sleep=0.0, boom=None):
    calls = []

    async def fake(cfg, mr_id, profile_name=None, **kw):
        calls.append((mr_id, kw))
        if sleep:
            await asyncio.sleep(sleep)
        if boom:
            raise boom
        return report
    monkeypatch.setattr(ws, "review_mr", fake)
    return calls


def _start(ws, token="tok-900", body=None):
    return ws._ci_start(token, {"mr_iid": 5, "sha": SHA} if body is None else body)


def _result(ws, ticket, token="tok-900"):
    assert _wait(lambda: ws._ci_status(token, ticket)[1] != "running"), "審查一直沒結束"
    return ws._ci_status(token, ticket)


@pytest.mark.parametrize("decision, code", [
    ("auto_approved", 0), ("needs_human", 3), ("blocked", 4)])
def test_job_拿到的結束碼照審查結果(ws, monkeypatch, decision, code):
    calls = _fake_review(ws, monkeypatch, {"decision": decision, "findings": [], "score": 90})
    status, ticket = _start(ws)
    assert status == 202
    assert _result(ws, ticket) == (200, f"done {code}")
    # 這條路不寫 commit status:job 的結果就是閘門
    assert calls == [("5", {"commit_status": False})]


def test_審查沒跑完整時_job_拿到失敗的結束碼(ws, monkeypatch):
    _fake_review(ws, monkeypatch, {"decision": "needs_human", "findings": [
        {"file": "(MR 內容)", "severity": "major", "title": "[沙盒執行] 審查未完整(逾時)",
         "stage": "沙盒執行"}]})
    status, ticket = _start(ws)
    assert _result(ws, ticket) == (200, "done 5")


def test_逾時時_job_拿到失敗的結束碼_只留言不寫_commit_status(ws, monkeypatch):
    _fake_review(ws, monkeypatch, sleep=30)
    monkeypatch.setattr(ws, "REVIEW_TIMEOUT_SECONDS", 0.5)
    notified = []
    monkeypatch.setattr(ws, "_notify_incomplete",
                        lambda iid, why, sha=None, **kw: notified.append((iid, why, kw)))
    status, ticket = _start(ws)
    assert _result(ws, ticket) == (200, "done 5")
    assert notified and notified[0][0] == 5 and notified[0][2] == {"commit_status": False}


def test_審查程式出錯時_job_拿到失敗的結束碼(ws, monkeypatch):
    _fake_review(ws, monkeypatch, boom=RuntimeError("壞了"))
    monkeypatch.setattr(ws, "_notify_incomplete", lambda iid, why, sha=None, **kw: None)
    status, ticket = _start(ws)
    assert _result(ws, ticket) == (200, "done 5")


def test_還沒審完時查到的是_running(ws, monkeypatch):
    _fake_review(ws, monkeypatch, {"decision": "auto_approved", "findings": []}, sleep=0.5)
    status, ticket = _start(ws)
    assert ws._ci_status("tok-900", ticket) == (200, "running")
    assert _result(ws, ticket) == (200, "done 0")


def test_只寫_commit_status_的那個參數不影響_webhook_那條路(ws, monkeypatch):
    calls = _fake_review(ws, monkeypatch, {"decision": "auto_approved", "findings": []})
    ws._run_review(5)
    assert calls == [("5", {})]


def test_別的_job_查不到這份審查(ws, monkeypatch):
    _fake_review(ws, monkeypatch, {"decision": "auto_approved", "findings": []})
    status, ticket = _start(ws)
    assert ws._ci_status("tok-901", ticket)[0] == 403
    assert ws._ci_status("沒有這個", ticket)[0] == 403


def test_開始這份審查的_job_結束後就查不到(ws, monkeypatch):
    _fake_review(ws, monkeypatch, {"decision": "auto_approved", "findings": []})
    status, ticket = _start(ws)
    ws._test_jobs["tok-900"] = _job(status="canceled")
    assert ws._ci_status("tok-900", ticket)[0] == 403


def test_不認得的編號(ws):
    assert ws._ci_status("tok-900", "nope")[0] == 404


@pytest.mark.parametrize("body", [
    None, [], {"mr_iid": 5}, {"sha": SHA}, {"mr_iid": "x", "sha": SHA}, {"mr_iid": 0, "sha": SHA},
    {"mr_iid": True, "sha": SHA}, {"mr_iid": 5, "sha": "abc"}, {"mr_iid": 5, "sha": SHA.upper()},
])
def test_參數不對時不開始審查(ws, monkeypatch, body):
    calls = _fake_review(ws, monkeypatch, {"decision": "auto_approved", "findings": []})
    status, _ = ws._ci_start("tok-900", body if body is not None else "不是物件")
    assert status == 400 and calls == []


def test_mr_iid_寫成字串的數字也收(ws, monkeypatch):
    _fake_review(ws, monkeypatch, {"decision": "blocked", "findings": []})
    status, ticket = _start(ws, body={"mr_iid": "5", "sha": SHA})
    assert status == 202 and _result(ws, ticket) == (200, "done 4")


@pytest.mark.parametrize("token, body", [
    ("沒有這個", None),                                   # token 無效
    ("tok-900", {"mr_iid": 6, "sha": SHA}),               # 別的 MR
    ("tok-900", {"mr_iid": 5, "sha": "d" * 40}),          # 別的 commit
])
def test_確認不過時拒絕_也不開始審查(ws, monkeypatch, token, body):
    calls = _fake_review(ws, monkeypatch, {"decision": "auto_approved", "findings": []})
    status, reason = _start(ws, token=token, body=body)
    assert status == 403 and reason and calls == []


def test_CI_開始時_webhook_那條路還在等的不再審一次(ws, monkeypatch):
    calls = _fake_review(ws, monkeypatch, {"decision": "auto_approved", "findings": []})
    monkeypatch.setattr(ws, "DEBOUNCE_SECONDS", 0.3)
    ws._trigger(5, SHA)
    status, ticket = _start(ws)
    assert _result(ws, ticket) == (200, "done 0")
    time.sleep(0.6)
    assert calls == [("5", {"commit_status": False})]


def test_記的份數有上限_只丟已經結束的(ws, monkeypatch):
    _fake_review(ws, monkeypatch, {"decision": "auto_approved", "findings": []})
    monkeypatch.setattr(ws, "MAX_CI_REVIEWS", 2)
    ws._ci_reviews.update({"old-done": {"job": 1, "mr": 1, "code": 0},
                           "old-running": {"job": 2, "mr": 2, "code": None}})
    status, ticket = _start(ws)
    assert "old-done" not in ws._ci_reviews and "old-running" in ws._ci_reviews
    assert ticket in ws._ci_reviews
    ws._ci_reviews.pop("old-running")      # 手動放的那份不會有人跑完


def test_記的份數超過上限但都還在跑時一份也不丟(ws, monkeypatch):
    _fake_review(ws, monkeypatch, {"decision": "auto_approved", "findings": []})
    monkeypatch.setattr(ws, "MAX_CI_REVIEWS", 1)
    ws._ci_reviews["old-running"] = {"job": 2, "mr": 2, "code": None}
    status, ticket = _start(ws)
    assert "old-running" in ws._ci_reviews and ticket in ws._ci_reviews
    ws._ci_reviews.pop("old-running")


# ─────────────────── 走 HTTP handler ───────────────────

class _Fake:
    def __init__(self, ws, path, headers, body=b""):
        import io
        self.handler = ws.Handler.__new__(ws.Handler)
        self.handler.path = path
        self.handler.headers = {"Content-Length": str(len(body)), **headers}
        self.handler.rfile = io.BytesIO(body)
        self.sent = None
        self.handler._send = lambda code, msg="ok": setattr(self, "sent", (code, msg))


def test_HTTP_的進出口(ws, monkeypatch):
    _fake_review(ws, monkeypatch, {"decision": "needs_human", "findings": []})
    body = json.dumps({"mr_iid": 5, "sha": SHA}).encode()
    f = _Fake(ws, "/ci/review", {"JOB-TOKEN": "tok-900"}, body)
    f.handler.do_POST()
    code, ticket = f.sent
    assert code == 202
    assert _wait(lambda: ws._ci_reviews[ticket]["code"] is not None)
    g = _Fake(ws, f"/ci/review/{ticket}", {"JOB-TOKEN": "tok-900"})
    g.handler.do_GET()
    assert g.sent == (200, "done 3")
    big = _Fake(ws, "/ci/review", {"JOB-TOKEN": "tok-900"}, b"{" + b" " * 5000 + b"}")
    big.handler.do_POST()
    assert big.sent[0] == 413


# ─────────────────── review_mr 不寫 commit status ───────────────────

def test_review_mr_關掉_commit_status_時其他回寫照常(monkeypatch, tmp_path):
    from orchestrator import pipeline
    from orchestrator.config import load_config
    from toolbox import gitlab

    fixtures, output = tmp_path / "fixtures", tmp_path / "out"
    fixtures.mkdir()
    output.mkdir()
    (fixtures / "mr_966.json").write_text(json.dumps({
        "title": "調整", "description": "例行維護", "sha": "e1ac" + "0" * 36,
        "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+SELECT a FROM t WHERE id = 1",
                   "full_content": "SELECT a FROM t WHERE id = 1"}]}, ensure_ascii=False),
        encoding="utf-8")
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    async def fake_run_agent(cfg, profile, system, user, hub, **kw):
        return json.dumps({"summary": "x", "score": 100, "findings": []}, ensure_ascii=False)
    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    cfg = load_config()
    out = {}
    for flag in (True, False):
        asyncio.run(pipeline.review_mr(cfg, "966", commit_status=flag))
        out[flag] = json.loads((output / "mr_966_review.json").read_text(encoding="utf-8"))
        (output / "mr_966_review.json").unlink()
    assert out[True]["commit_status"] is not None
    assert out[False]["commit_status"] is None
    assert out[False]["summary"] is not None and out[False]["labels"]


def test_同一個_MR_的新_job_取消進行中的那份(ws, monkeypatch):
    """job 被 retry、或推了新 commit:舊的那份取消,舊的 job 拿到失敗的結束碼。"""
    _fake_review(ws, monkeypatch, {"decision": "auto_approved", "findings": []}, sleep=1.5)
    status, first = _start(ws)
    assert _wait(lambda: 5 in ws._inflight)
    status, second = _start(ws, token="tok-901")
    assert _result(ws, first) == (200, "done 5")
    assert _result(ws, second, token="tok-901") == (200, "done 0")


def test_沒完成時只留言_不寫_commit_status(ws, monkeypatch):
    from toolbox import gitlab
    posted, status = [], []
    monkeypatch.setattr(gitlab, "post_summary", lambda *a, **k: posted.append(a) or "ok")
    monkeypatch.setattr(gitlab, "set_commit_status", lambda *a, **k: status.append(a) or "ok")
    ws._notify_incomplete(5, "逾時", SHA, commit_status=False)
    assert posted and status == []
    ws._notify_incomplete(5, "逾時", SHA)
    assert len(status) == 1                 # webhook 那條路照樣寫
