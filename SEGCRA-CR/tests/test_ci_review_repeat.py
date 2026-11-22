"""CI 審查要不要每次都審(CI_REVIEW_REPEAT)——不呼叫 LLM、不連 GitLab。

部署端可能只想讓 MR 第一次自動審,之後的改動交給人。CI 的審查是合併閘門,「不審」時 job 回什麼
會改變閘門的意思,所以 first 模式有幾道保護:

  1. 模式:服務啟動時讀一次,預設 every;設了但是空的、或不認得的值,服務不啟動
  2. 只有「服務自己記得這個 MR 上一次真的審完、結果是放行或交人工,目標分支相同」才跳過:
     上一次擋下、沒跑完整、還有審查在跑、服務重啟後忘記、換過目標分支、目標分支取不到,都照樣審
  3. 有人要求重審:上一次處理的(審過或跳過的)同一個 commit 又送來(retry、重跑 pipeline)就真的審。
     MR 的 pipeline 帶不了手動變數,請求裡也沒有「強制重審」的欄位
  4. 跳過時 job 回 3(交人工),MR 上留言寫明從哪個 commit 之後沒有經過審查(規則檢查與 AI 都沒跑)、
     標籤改成交人工;寫回失敗不影響 job
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
    (None, "every"), ("every", "every"), ("first", "first"), (" FIRST ", "first"), ("Every", "every"),
])
def test_模式(value, mode):
    env = {} if value is None else {"CI_REVIEW_REPEAT": value}
    assert ci_gate.review_repeat(env) == mode


@pytest.mark.parametrize("value", ["", "   ", "frist", "First-only", "1", "once"])
def test_模式_設了但是空的或不認得_服務不啟動(value):
    with pytest.raises(ci_gate.ReviewRepeatError, match="CI_REVIEW_REPEAT"):
        ci_gate.review_repeat({"CI_REVIEW_REPEAT": value})


def test_服務啟動時讀一次_設錯就不啟動(ws, monkeypatch):
    import sys
    monkeypatch.setenv("CI_REVIEW_REPEAT", "frist")
    monkeypatch.setattr(sys, "argv", ["webhook_server.py", "--port", "0"])
    monkeypatch.setattr(ws, "ThreadingHTTPServer", lambda *a: pytest.fail("不該開始收請求"))
    with pytest.raises(SystemExit) as e:
        ws.main()
    assert "CI_REVIEW_REPEAT" in str(e.value)


@pytest.mark.parametrize("mode", ["every", "first"])
def test_啟動說明印出用的是哪一種(ws, monkeypatch, mode):
    monkeypatch.setattr(ws, "REVIEW_REPEAT", mode)
    assert f"CI_REVIEW_REPEAT={mode}" in "\n".join(ws._startup_lines("0.0.0.0", 8000))


# ─────────────────── 2. 什麼時候跳過 ───────────────────

def _rec(code=0, branch="main", last_sha=SHA):
    return {"code": code, "branch": branch, "reviewed_sha": SHA, "last_sha": last_sha}


@pytest.mark.parametrize("mode, record, sha, branch, running, skip", [
    ("first", _rec(0), SHA2, "main", False, True),
    ("first", _rec(3), SHA2, "main", False, True),
    ("first", _rec(4), SHA2, "main", False, False),         # 上一次擋下:照樣審
    ("first", _rec(5), SHA2, "main", False, False),         # 上一次沒跑完整
    ("first", None, SHA2, "main", False, False),            # 不記得
    ("first", _rec(0), SHA, "main", False, False),          # 有人要求重審(同一個 commit 又送來)
    ("first", _rec(0), SHA2, "dev", False, False),          # 換了目標分支
    ("first", _rec(0, branch=""), SHA2, "", False, False),  # 目標分支取不到
    ("first", _rec(0), SHA2, "", False, False),
    ("first", _rec(0), SHA2, "main", True, False),          # 還有審查在跑
    ("every", _rec(0), SHA2, "main", False, False),
])
def test_什麼時候跳過(mode, record, sha, branch, running, skip):
    assert ci_gate.should_skip(mode, record, sha=sha, target_branch=branch, running=running) is skip


def test_跳過時的留言寫明從哪個_commit_起沒審_規則檢查與_AI_都沒跑():
    note = ci_gate.skipped_note(0, SHA)
    assert f"`{SHA[:8]}` 之後的改動都**沒有經過審查**" in note
    assert "規則檢查與 AI 都沒有跑" in note and "自動放行" in note and "retry" in note
    assert "交人工確認" in ci_gate.skipped_note(3, SHA)


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


def _push(monkeypatch, sha, target_branch="main"):
    """MR 推了新的 commit(或改了目標分支):job、pipeline、MR 的 head 都換成這個 sha。"""
    from toolbox import gitlab
    pipe = {"id": 77, "project_id": 42, "sha": sha, "ref": "refs/merge-requests/5/head"}
    head = _mr(sha=sha, target_branch=target_branch)
    monkeypatch.setattr(gitlab, "job_from_token",
                        lambda t: _job(pipeline=pipe) if t == "tok-900" else None)
    monkeypatch.setattr(gitlab, "pipeline_info", lambda pid: _pipeline(sha=sha) if pid == 77 else {"id": pid})
    monkeypatch.setattr(gitlab, "mr_head", lambda iid: head if iid == "5" else {"state": "merged"})


def _send(ws, sha, **extra):
    status, ticket = _start(ws, body={"mr_iid": 5, "sha": sha, **extra})
    assert status == 202
    return _result(ws, ticket)


def _twice(ws, monkeypatch, first_report, *, mode="first", **extra):
    """同一個 MR 審一次之後推了新的 commit,回 (審查呼叫次數, 第二次的結果)。"""
    monkeypatch.setattr(ws, "REVIEW_REPEAT", mode)
    calls = _fake_review(ws, monkeypatch, first_report)
    _push(monkeypatch, SHA)
    _send(ws, SHA)
    _push(monkeypatch, SHA2)
    return calls, _send(ws, SHA2, **extra)


APPROVED = {"decision": "auto_approved", "findings": [], "score": 90}
BLOCKED = {"decision": "blocked", "findings": [], "score": 10}


def test_first_第二次不審_交人工_並寫回(ws, monkeypatch, writes):
    calls, second = _twice(ws, monkeypatch, APPROVED)
    assert len(calls) == 1 and second == (200, "done 3")
    assert _wait(lambda: len(writes) == 2)
    kinds = {w[0]: w for w in writes}
    assert f"`{SHA[:8]}` 之後的改動" in kinds["note"][2] and kinds["label"][2] == "ai-review::needs_human"


def test_連續跳過兩次_留言的_sha_不變(ws, monkeypatch, writes):
    _twice(ws, monkeypatch, APPROVED)
    _push(monkeypatch, SHA3)
    assert _send(ws, SHA3) == (200, "done 3")
    assert _wait(lambda: len(writes) == 4)
    notes = [w[2] for w in writes if w[0] == "note"]
    assert all(f"`{SHA[:8]}` 之後" in n for n in notes) and len(notes) == 2


def test_every_每次都審(ws, monkeypatch, writes):
    calls, second = _twice(ws, monkeypatch, APPROVED, mode="every")
    assert len(calls) == 2 and second == (200, "done 0") and writes == []


def test_上一次擋下時照樣審(ws, monkeypatch, writes):
    calls, second = _twice(ws, monkeypatch, BLOCKED)
    assert len(calls) == 2 and second == (200, "done 4")


def test_上一次沒跑完整時照樣審(ws, monkeypatch, writes):
    monkeypatch.setattr(ws, "_notify_incomplete", lambda *a, **k: None)
    monkeypatch.setattr(ws, "REVIEW_REPEAT", "first")
    _fake_review(ws, monkeypatch, boom=RuntimeError("x"))
    _push(monkeypatch, SHA)
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
    monkeypatch.setattr(ws, "REVIEW_REPEAT", "first")
    calls = _fake_review(ws, monkeypatch, APPROVED)
    _push(monkeypatch, SHA)
    _send(ws, SHA)
    assert _send(ws, SHA) == (200, "done 0") and len(calls) == 2 and writes == []


def test_retry_審完之後再推新的_commit_又跳過(ws, monkeypatch, writes):
    calls, _ = _twice(ws, monkeypatch, APPROVED)
    _send(ws, SHA2)                                         # retry:真的審
    _push(monkeypatch, SHA3)
    assert _send(ws, SHA3) == (200, "done 3") and len(calls) == 2


def _ordered_review(ws, monkeypatch, results):
    """依呼叫順序回不同結果的 review_mr 替身:results 是 [(報告, 秒數)];回呼叫次數的清單。"""
    import asyncio
    calls = []

    async def fake(cfg, mr_id, *a, **kw):
        report, delay = results[min(len(calls), len(results) - 1)]
        calls.append(mr_id)
        await asyncio.sleep(delay)
        return dict(report)
    monkeypatch.setattr(ws, "review_mr", fake)
    return calls


def test_retry_還在審時推新的_commit_新的照樣審(ws, monkeypatch, writes):
    """放行 → 推 SHA2 跳過 → 對 SHA2 retry(擋下,比較慢)→ retry 還在審時推 SHA3:SHA3 要真的審。"""
    monkeypatch.setattr(ws, "REVIEW_REPEAT", "first")
    calls = _ordered_review(ws, monkeypatch, [(APPROVED, 0), (BLOCKED, 0.8), (BLOCKED, 0)])
    _push(monkeypatch, SHA)
    _send(ws, SHA)
    _push(monkeypatch, SHA2)
    assert _send(ws, SHA2) == (200, "done 3")                # 跳過
    retry = _start(ws, body={"mr_iid": 5, "sha": SHA2})[1]   # retry:真的審,還在跑
    _push(monkeypatch, SHA3)
    third = _start(ws, body={"mr_iid": 5, "sha": SHA3})[1]
    assert _result(ws, third) == (200, "done 4")             # 照樣審,不是跳過的 3
    _result(ws, retry)
    assert len(calls) == 3


def test_retry_晚到的結果不把紀錄蓋回舊的_commit(ws, monkeypatch, writes):
    """放行 → SHA2 跳過 → retry SHA2(放行,比較慢)→ 推 SHA3(還在審,照樣審)→ SHA2 審完 →
    對 SHA3 按 retry:要真的審,不能因為紀錄被 SHA2 蓋回去而跳過。"""
    monkeypatch.setattr(ws, "REVIEW_REPEAT", "first")
    calls = _ordered_review(ws, monkeypatch, [(APPROVED, 0), (APPROVED, 0.8), (APPROVED, 0), (APPROVED, 0)])
    _push(monkeypatch, SHA)
    _send(ws, SHA)
    _push(monkeypatch, SHA2)
    _send(ws, SHA2)                                           # 跳過
    retry = _start(ws, body={"mr_iid": 5, "sha": SHA2})[1]
    _push(monkeypatch, SHA3)
    _send(ws, SHA3)
    _result(ws, retry)
    assert _send(ws, SHA3) == (200, "done 0")                # retry SHA3:真的審
    assert len(calls) == 4


@pytest.mark.parametrize("force", [True, "true", 1])
def test_請求裡帶_force_沒有作用(ws, monkeypatch, writes, force):
    """沒有「強制重審」的欄位:重審只看是不是同一個 commit 又送來。"""
    calls, second = _twice(ws, monkeypatch, APPROVED, force=force)
    assert len(calls) == 1 and second == (200, "done 3")


def test_換了目標分支就重審(ws, monkeypatch, writes):
    monkeypatch.setattr(ws, "REVIEW_REPEAT", "first")
    calls = _fake_review(ws, monkeypatch, APPROVED)
    _push(monkeypatch, SHA, target_branch="main")
    _send(ws, SHA)
    _push(monkeypatch, SHA2, target_branch="release")
    assert _send(ws, SHA2) == (200, "done 0") and len(calls) == 2


def test_換回原本的目標分支_不用舊的放行紀錄(ws, monkeypatch, writes):
    """目標 main 放行 → 改 dev、擋下 → 改回 main:最新一次真的審是擋下,照樣審。"""
    monkeypatch.setattr(ws, "REVIEW_REPEAT", "first")
    calls = _ordered_review(ws, monkeypatch, [(APPROVED, 0), (BLOCKED, 0), (APPROVED, 0)])
    _push(monkeypatch, SHA, target_branch="main")
    _send(ws, SHA)
    _push(monkeypatch, SHA2, target_branch="dev")
    assert _send(ws, SHA2) == (200, "done 4")
    _push(monkeypatch, SHA3, target_branch="main")
    assert _send(ws, SHA3) == (200, "done 0") and len(calls) == 3


def test_目標分支取不到時照樣審(ws, monkeypatch, writes):
    monkeypatch.setattr(ws, "REVIEW_REPEAT", "first")
    calls = _fake_review(ws, monkeypatch, APPROVED)
    _push(monkeypatch, SHA, target_branch="")
    _send(ws, SHA)
    _push(monkeypatch, SHA2, target_branch="")
    assert _send(ws, SHA2) == (200, "done 0") and len(calls) == 2


def test_服務重新啟動後忘記_照樣審(ws, monkeypatch, writes):
    monkeypatch.setattr(ws, "REVIEW_REPEAT", "first")
    calls = _fake_review(ws, monkeypatch, APPROVED)
    _push(monkeypatch, SHA)
    _send(ws, SHA)
    ws._ci_history.clear()                      # 等同重新啟動:紀錄只在記憶體
    _push(monkeypatch, SHA2)
    assert _send(ws, SHA2) == (200, "done 0") and len(calls) == 2


def test_紀錄超過上限時丟掉最舊的_之後照樣審(ws, monkeypatch, writes):
    monkeypatch.setattr(ws, "REVIEW_REPEAT", "first")
    monkeypatch.setattr(ws, "MAX_CI_HISTORY", 2)
    calls = _fake_review(ws, monkeypatch, APPROVED)
    _push(monkeypatch, SHA)
    _send(ws, SHA)
    with ws._lock:
        ws._history_put(6, code=0)
        ws._history_put(7, code=0)              # MR !5 的紀錄被擠掉
    _push(monkeypatch, SHA2)
    assert _send(ws, SHA2) == (200, "done 0") and len(calls) == 2


def test_第一次還在跑時第二次照樣審(ws, monkeypatch, writes):
    """還沒跑完就沒有結論:不能拿「正在審」當成「審過了」。"""
    monkeypatch.setattr(ws, "REVIEW_REPEAT", "first")
    calls = _fake_review(ws, monkeypatch, APPROVED, sleep=0.5)
    _push(monkeypatch, SHA)
    first = _start(ws)[1]
    _push(monkeypatch, SHA2)
    second = _start(ws, body={"mr_iid": 5, "sha": SHA2})[1]
    assert _result(ws, second) == (200, "done 0")              # 真的審了,不是跳過的 3
    _result(ws, first)
    assert len(calls) == 2


def test_不是最後一份的審查結果不記(ws):
    """比較晚結束的舊審查(例如 retry 那份)不能把紀錄蓋回舊的 commit。"""
    with ws._lock:
        ws._ci_reviews["old"] = {"job": 1, "mr": 5, "sha": SHA, "branch": "main", "code": None}
        ws._ci_reviews["new"] = {"job": 2, "mr": 5, "sha": SHA2, "branch": "main", "code": None}
        ws._history_put(5, latest="new", last_sha=SHA2)
        ws._remember_ci_result("old", 4)
        assert "code" not in ws._ci_history[5]
        ws._remember_ci_result("new", 0)
        assert (ws._ci_history[5]["code"], ws._ci_history[5]["reviewed_sha"]) == (0, SHA2)
        for ticket in ("old", "new"):             # 這裡只測紀錄:不留還沒結束的審查給 fixture 等
            ws._ci_reviews.pop(ticket)


def test_寫回失敗不影響_job_的結果(ws, monkeypatch):
    from toolbox import gitlab

    def boom(*a, **k):
        raise RuntimeError("連不上")
    monkeypatch.setattr(gitlab, "post_note", boom)
    monkeypatch.setattr(gitlab, "set_label", boom)
    _, second = _twice(ws, monkeypatch, APPROVED)
    assert second == (200, "done 3")


# ─────────────────── 4. gitlab.mr_head、gitlab.post_note ───────────────────

def test_mr_head_帶目標分支(monkeypatch):
    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "_api", lambda method, path, **kw: {
        "title": "t", "sha": SHA, "state": "opened", "source_project_id": 42, "target_branch": "release"})
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
