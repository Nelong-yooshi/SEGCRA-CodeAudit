"""webhook 什麼時候觸發審查:每份審查都是完整的模型呼叫,不該觸發的不要觸發。

- 留言:`/review` 要寫在留言開頭(後面是空白或結尾);網址、內文裡的 `/review`、`/reviews` 不算
- Draft 的 MR 不審(開發中反覆推 commit);標成 Ready 時審(那次是沒有 oldrev 的 update,
  要從 changes 認);Draft 期間要看,留言 `/review`
- 同一個 MR 短時間內連續觸發時,等 DEBOUNCE_SECONDS 沒有新事件才開始,只審最後一次

不起 HTTP 伺服器:直接呼叫 `_handle`(webhook 事件 → 要不要觸發)與 `_trigger`。
"""
import importlib
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

HEAD = "b" * 40


@pytest.fixture()
def ws(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "x")
    import webhook_server as mod
    importlib.reload(mod)
    return mod


@pytest.fixture()
def triggered(ws, monkeypatch):
    """把 _trigger 換成只記錄的替身:驗「要不要觸發」,不真的開審查。"""
    calls = []
    monkeypatch.setattr(ws, "_trigger", lambda iid, head_sha=None: calls.append((iid, head_sha)) or "ok")
    return calls


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ─────────────────── 留言的 /review ───────────────────

@pytest.mark.parametrize("note", ["/review", "/review\n", "  /review", "/review 請再看一次",
                                  "/review\n補充說明"])
def test_留言開頭的_review_算(ws, note):
    assert ws._review_command(note) is True


@pytest.mark.parametrize("note", [
    "https://gitlab.invalid/g/p/-/merge_requests/7/review",    # 網址裡
    "請看 /review 的說明",                                        # 內文裡
    "/reviews",                                                  # 不是這個指令
    "/reviewer 指派給我",
    "先說明\n/review",                                           # 不在開頭
    "/Review",
    "",
    None,
])
def test_其他寫法不算(ws, note):
    assert ws._review_command(note) is False


def _note(note, draft=False, username="dev"):
    return {"object_kind": "note", "user": {"username": username},
            "object_attributes": {"noteable_type": "MergeRequest", "note": note},
            "merge_request": {"iid": 7, "draft": draft, "last_commit": {"id": HEAD}}}


def test_留言_review_觸發審查(ws, triggered):
    ws._handle(_note("/review"))
    assert triggered == [(7, HEAD)]


def test_網址裡的_review_不觸發(ws, triggered):
    out = ws._handle(_note("看這裡 https://gitlab.invalid/g/p/-/merge_requests/7/review"))
    assert triggered == [] and out == "ignored (note)"


def test_Draft_期間留言_review_照樣審(ws, triggered):
    ws._handle(_note("/review", draft=True))
    assert triggered == [(7, HEAD)]


def test_審查帳號自己的留言不觸發(ws, triggered):
    ws._handle(_note("/review", username="ai-reviewer"))
    assert triggered == []


# ─────────────────── MR 事件:Draft 與 Ready ───────────────────

def _mr(action, oldrev=None, changes=None, **attrs):
    payload = {"object_kind": "merge_request",
               "object_attributes": {"action": action, "iid": 7, "last_commit": {"id": HEAD},
                                     **({"oldrev": oldrev} if oldrev else {}), **attrs}}
    if changes is not None:
        payload["changes"] = changes
    return payload


@pytest.mark.parametrize("action,oldrev", [("open", None), ("reopen", None), ("update", "a" * 40)])
def test_一般的_MR_照常觸發(ws, triggered, action, oldrev):
    ws._handle(_mr(action, oldrev, draft=False))
    assert triggered == [(7, HEAD)]


@pytest.mark.parametrize("action,oldrev", [("open", None), ("reopen", None), ("update", "a" * 40)])
@pytest.mark.parametrize("flag", ["draft", "work_in_progress"])
def test_Draft_的_MR_不審(ws, triggered, action, oldrev, flag):
    out = ws._handle(_mr(action, oldrev, **{flag: True}))
    assert triggered == [] and out == "ignored (draft)"


@pytest.mark.parametrize("key", ["draft", "work_in_progress"])
def test_標成_Ready_時審(ws, triggered, key):
    ws._handle(_mr("update", changes={key: {"previous": True, "current": False}}, draft=False))
    assert triggered == [(7, HEAD)]


@pytest.mark.parametrize("changes", [
    {"draft": {"previous": False, "current": True}},       # 標回 Draft
    {"title": {"previous": "a", "current": "b"}},           # 改標題
    {"labels": {"previous": [], "current": [{"title": "x"}]}},
    {"draft": {"previous": "true", "current": "false"}},    # 形狀不對
    {"draft": True},
    [],
    "draft",
])
def test_其他沒有新_commit_的_update_不審(ws, triggered, changes):
    out = ws._handle(_mr("update", changes=changes, draft=False))
    assert triggered == [] and out == "ignored (update 沒有新 commit)"


def test_沒有_changes_的_update_不審(ws, triggered):
    ws._handle(_mr("update", draft=False))
    assert triggered == []


# ─────────────────── 連續觸發只審最後一次 ───────────────────

@pytest.fixture()
def started(ws, monkeypatch):
    """把真的審查換成只記錄:驗 _trigger 的計時,不跑管線。"""
    calls = []
    monkeypatch.setattr(ws, "_run_review", lambda iid, head_sha=None: calls.append((iid, head_sha)))
    monkeypatch.setattr(ws, "DEBOUNCE_SECONDS", 0.4)
    return calls


def test_連續觸發只審最後一次(ws, started):
    for sha in ("c1", "c2", "c3"):
        ws._trigger(5, sha)
        time.sleep(0.1)
    assert _wait(lambda: started, timeout=3)
    time.sleep(0.6)
    assert started == [(5, "c3")]
    assert ws._pending == {}


def test_安靜期還沒過不開始(ws, started):
    ws._trigger(5, "c1")
    time.sleep(0.2)
    assert started == []
    assert _wait(lambda: started == [(5, "c1")], timeout=3)


def test_間隔夠久的兩次各審一次(ws, started):
    ws._trigger(5, "c1")
    assert _wait(lambda: len(started) == 1, timeout=3)
    ws._trigger(5, "c2")
    assert _wait(lambda: len(started) == 2, timeout=3)
    assert started == [(5, "c1"), (5, "c2")]


def test_不同的_MR_各自計時(ws, started):
    ws._trigger(5, "c1")
    ws._trigger(6, "d1")
    assert _wait(lambda: len(started) == 2, timeout=3)
    assert sorted(started) == [(5, "c1"), (6, "d1")]


def test_已經被換掉的計時器不開始(ws, started):
    """計時器已經在跑 _start、才被新的事件換掉:這一個不能再開一份。"""
    newer = threading.Timer(60, lambda: None)
    ws._pending[5] = newer
    ws._start(5, "old")
    assert started == [] and ws._pending[5] is newer
