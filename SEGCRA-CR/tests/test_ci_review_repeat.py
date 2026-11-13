"""CI 審查要不要每次都審(CI_REVIEW_REPEAT)——不呼叫 LLM、不連 GitLab。

部署端可能只想讓 MR 第一次自動審,之後的改動交給人。CI 的審查是合併閘門,「不審」時 job 回什麼
會改變閘門的意思,所以 first 模式有幾道保護:

  1. 模式:預設 every;不認得的值當成 every(設錯時往多審的方向走)
  2. 只有「服務自己記得這個 MR(同一個目標分支)上一次跑完、結果是放行或交人工」才跳過:
     上一次擋下、沒跑完整、還在跑、服務重啟後忘記、換了目標分支,都照樣審
  3. 有人要求重審:上一次處理的(審過或跳過的)同一個 commit 又送來(retry、重跑 pipeline)就真的審。
     MR 的 pipeline 帶不了手動變數,請求裡也沒有「強制重審」的欄位
  4. 跳過時 job 回 3(交人工),MR 上留言寫明沒有經過 AI 審查、標籤改成交人工;寫回失敗不影響 job
"""
from __future__ import annotations

import json

import pytest

from orchestrator import ci_gate
from tests.test_ci_gate import (SHA, _fake_review, _job, _mr, _pipeline, _result, _start,  # noqa: F401
                                _wait, ws)

SHA2 = "d" * 40
SHA3 = "e" * 40


# ─────────────────── 1. 模式 ───────────────────

@pytest.mark.parametrize("value, mode", [
    (None, "every"), ("every", "every"), ("first", "first"), (" FIRST ", "first"),
    ("once", "every"), ("", "every"),
])
def test_模式_不認得的當成每次都審(monkeypatch, value, mode):
    if value is None:
        monkeypatch.delenv("CI_REVIEW_REPEAT", raising=False)
    else:
        monkeypatch.setenv("CI_REVIEW_REPEAT", value)
    assert ci_gate.review_repeat() == mode


# ─────────────────── 2. 什麼時候跳過 ───────────────────

@pytest.mark.parametrize("mode, previous, requested, skip", [
    ("first", 0, False, True),
    ("first", 3, False, True),
    ("first", 4, False, False),            # 上一次擋下:照樣審
    ("first", 5, False, False),            # 上一次沒跑完整:照樣審
    ("first", None, False, False),         # 不記得:照樣審
    ("first", 0, True, False),             # 有人要求重審(同一個 commit 又送來)
    ("every", 0, False, False),
])
def test_只有記得上一次放行或交人工才跳過(mode, previous, requested, skip):
    assert ci_gate.should_skip(mode, previous, requested) is skip


def test_跳過時的留言寫明沒有經過_AI_審查與怎麼重審():
    note = ci_gate.skipped_note(0)
    assert "沒有經過 AI 審查" in note and "自動放行" in note and "retry" in note
    assert "交人工確認" in ci_gate.skipped_note(3)


# ─────────────────── 3. /ci/review ───────────────────

@pytest.fixture()
def writes(ws, monkeypatch):
    from toolbox import gitlab
    got = []
    monkeypatch.setattr(gitlab, "post_note", lambda iid, body: got.append(("note", iid, body)) or
                        json.dumps({"ok": True}))
    monkeypatch.setattr(gitlab, "set_label", lambda iid, label: got.append(("label", iid, label)) or
                        json.dumps({"ok": True}))
    return got


def _push(monkeypatch, sha, target_branch=None):
    """MR 推了新的 commit:job、pipeline、MR 的 head 都換成這個 sha。"""
    from toolbox import gitlab
    pipe = {"id": 77, "project_id": 42, "sha": sha, "ref": "refs/merge-requests/5/head"}
    head = _mr(sha=sha) if target_branch is None else _mr(sha=sha, target_branch=target_branch)
    monkeypatch.setattr(gitlab, "job_from_token",
                        lambda t: _job(pipeline=pipe) if t == "tok-900" else None)
    monkeypatch.setattr(gitlab, "pipeline_info", lambda pid: _pipeline(sha=sha) if pid == 77 else None)
    monkeypatch.setattr(gitlab, "mr_head", lambda iid: head if iid == "5" else None)


def _send(ws, sha, **extra):
    status, ticket = _start(ws, body={"mr_iid": 5, "sha": sha, **extra})
    assert status == 202
    return _result(ws, ticket)


def _twice(ws, monkeypatch, first_report, *, mode="first", **extra):
    """同一個 MR 審一次之後推了新的 commit,回 (審查呼叫次數, 第二次的結果)。"""
    monkeypatch.setenv("CI_REVIEW_REPEAT", mode)
    calls = _fake_review(ws, monkeypatch, first_report)
    _send(ws, SHA)
    _push(monkeypatch, SHA2)
    return calls, _send(ws, SHA2, **extra)


APPROVED = {"decision": "auto_approved", "findings": [], "score": 90}


def test_first_第二次不審_交人工_並寫回(ws, monkeypatch, writes):
    calls, second = _twice(ws, monkeypatch, APPROVED)
    assert len(calls) == 1 and second == (200, "done 3")
    assert _wait(lambda: len(writes) == 2)
    kinds = {w[0]: w for w in writes}
    assert "沒有經過 AI 審查" in kinds["note"][2] and kinds["label"][2] == "ai-review::needs_human"


def test_every_每次都審(ws, monkeypatch, writes):
    calls, second = _twice(ws, monkeypatch, APPROVED, mode="every")
    assert len(calls) == 2 and second == (200, "done 0") and writes == []


def test_預設每次都審(ws, monkeypatch, writes):
    monkeypatch.delenv("CI_REVIEW_REPEAT", raising=False)
    calls = _fake_review(ws, monkeypatch, APPROVED)
    _send(ws, SHA)
    _push(monkeypatch, SHA2)
    _send(ws, SHA2)
    assert len(calls) == 2


def test_上一次擋下時照樣審(ws, monkeypatch, writes):
    calls, second = _twice(ws, monkeypatch, {"decision": "blocked", "findings": [], "score": 10})
    assert len(calls) == 2 and second == (200, "done 4")


def test_上一次沒跑完整時照樣審(ws, monkeypatch, writes):
    monkeypatch.setattr(ws, "_notify_incomplete", lambda *a, **k: None)
    monkeypatch.setenv("CI_REVIEW_REPEAT", "first")
    _fake_review(ws, monkeypatch, boom=RuntimeError("x"))
    assert _send(ws, SHA) == (200, "done 5")
    calls = _fake_review(ws, monkeypatch, APPROVED)
    _push(monkeypatch, SHA2)
    assert _send(ws, SHA2) == (200, "done 0") and len(calls) == 1


def test_跳過之後對同一個_commit_retry_就真的審(ws, monkeypatch, writes):
    calls, second = _twice(ws, monkeypatch, APPROVED)
    assert second == (200, "done 3") and len(calls) == 1
    assert _send(ws, SHA2) == (200, "done 0") and len(calls) == 2


def test_審過之後對同一個_commit_retry_也真的審(ws, monkeypatch, writes):
    """retry 是人要求重審:第一次 retry 就要審,不能先被跳過一次。"""
    monkeypatch.setenv("CI_REVIEW_REPEAT", "first")
    calls = _fake_review(ws, monkeypatch, APPROVED)
    _send(ws, SHA)
    assert _send(ws, SHA) == (200, "done 0") and len(calls) == 2 and writes == []


def test_retry_審完之後再推新的_commit_又跳過(ws, monkeypatch, writes):
    calls, _ = _twice(ws, monkeypatch, APPROVED)
    _send(ws, SHA2)                                         # retry:真的審
    _push(monkeypatch, SHA3)
    assert _send(ws, SHA3) == (200, "done 3") and len(calls) == 2


@pytest.mark.parametrize("force", [True, "true", 1])
def test_請求裡帶_force_沒有作用(ws, monkeypatch, writes, force):
    """沒有「強制重審」的欄位:重審只看是不是同一個 commit 又送來。"""
    calls, second = _twice(ws, monkeypatch, APPROVED, force=force)
    assert len(calls) == 1 and second == (200, "done 3")


def test_換了目標分支就重審(ws, monkeypatch, writes):
    monkeypatch.setenv("CI_REVIEW_REPEAT", "first")
    calls = _fake_review(ws, monkeypatch, APPROVED)
    _push(monkeypatch, SHA, target_branch="main")
    _send(ws, SHA)
    _push(monkeypatch, SHA2, target_branch="release")
    assert _send(ws, SHA2) == (200, "done 0") and len(calls) == 2


def test_服務重新啟動後忘記_照樣審(ws, monkeypatch, writes):
    monkeypatch.setenv("CI_REVIEW_REPEAT", "first")
    calls = _fake_review(ws, monkeypatch, APPROVED)
    _send(ws, SHA)
    ws._ci_history.clear()                      # 等同重新啟動:紀錄只在記憶體
    _push(monkeypatch, SHA2)
    assert _send(ws, SHA2) == (200, "done 0") and len(calls) == 2


def test_紀錄超過上限時丟掉最舊的_之後照樣審(ws, monkeypatch, writes):
    monkeypatch.setenv("CI_REVIEW_REPEAT", "first")
    monkeypatch.setattr(ws, "MAX_CI_HISTORY", 2)
    calls = _fake_review(ws, monkeypatch, APPROVED)
    _send(ws, SHA)
    with ws._lock:
        ws._remember_ci_result((6, ""), 0)
        ws._remember_ci_result((7, ""), 0)      # MR !5 的紀錄被擠掉
    _push(monkeypatch, SHA2)
    assert _send(ws, SHA2) == (200, "done 0") and len(calls) == 2


def test_第一次還在跑時第二次照樣審(ws, monkeypatch, writes):
    """還沒跑完就沒有結論:不能拿「正在審」當成「審過了」。"""
    monkeypatch.setenv("CI_REVIEW_REPEAT", "first")
    calls = _fake_review(ws, monkeypatch, APPROVED, sleep=0.5)
    first = _start(ws)[1]
    _push(monkeypatch, SHA2)
    second = _start(ws, body={"mr_iid": 5, "sha": SHA2})[1]
    assert _result(ws, second) == (200, "done 0")              # 真的審了,不是跳過的 3
    _result(ws, first)
    assert len(calls) == 2


def test_寫回失敗不影響_job_的結果(ws, monkeypatch):
    from toolbox import gitlab

    def boom(*a, **k):
        raise RuntimeError("連不上")
    monkeypatch.setattr(gitlab, "post_note", boom)
    monkeypatch.setattr(gitlab, "set_label", boom)
    _, second = _twice(ws, monkeypatch, APPROVED)
    assert second == (200, "done 3")


def test_沒有目標分支的紀錄不記():
    import webhook_server as mod
    before = dict(mod._ci_history)
    with mod._lock:
        mod._remember_ci_result(None, 0)
    assert mod._ci_history == before


# ─────────────────── 4. gitlab.mr_head、gitlab.post_note ───────────────────

def test_mr_head_帶目標分支(monkeypatch):
    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "_api", lambda method, path, **kw: {
        "sha": SHA, "state": "opened", "source_project_id": 42, "target_branch": "release"})
    assert gitlab.mr_head("5")["target_branch"] == "release"


def test_post_note_寫回失敗時回種類_不重送(monkeypatch):
    from toolbox import gitlab
    sent = []

    def fail(method, path, **kw):
        sent.append(path)
        raise TimeoutError("slow")
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "_api", fail)
    got = json.loads(gitlab.post_note("5", "x"))
    assert got["ok"] is False and len(sent) == 1


def test_post_note_mr_編號要是數字(monkeypatch):
    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    with pytest.raises(Exception):
        gitlab.post_note("5/../../x", "x")
