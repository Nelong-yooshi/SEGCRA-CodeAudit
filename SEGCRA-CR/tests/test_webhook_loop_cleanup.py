"""webhook 每份審查用自己的事件迴圈;關掉迴圈前,還沒結束的工作要先取消、等它們收完。

直接 `loop.close()` 的話,審查裡另外開的工作(背景的模型呼叫等)不會被取消:外層逾時、
或新 commit 取消舊審查時,打到模型的請求沒有人收尾。這裡驗:外層逾時與新 commit 取消時,
迴圈關掉前沒有任何還沒結束的工作;吞掉取消的工作不會讓執行緒卡住;收尾出錯不往外丟。

不起 HTTP 伺服器、不呼叫 LLM、不連 GitLab:直接驅動 `_run_review`,把 `review_mr` 換成假的。
"""
import asyncio
import importlib
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

SECRET = "ZZ_HOST_192.0.2.10:443_ZZ"      # 例外訊息裡的內部細節(RFC 5737 的範例位址),不可以進 log


@pytest.fixture()
def ws(monkeypatch):
    """每個測試拿一份乾淨的 webhook_server(它有模組層的狀態)。"""
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "x")
    import webhook_server as mod
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_notify_incomplete", lambda *a, **k: None)
    return mod


class _Seen:
    """假的審查留下的東西:它的事件迴圈、它開的背景工作、背景工作有沒有被取消。"""

    def __init__(self):
        self.loop = None
        self.background = None
        self.cancelled = threading.Event()
        self.started = threading.Event()


def _review_with_background(seen: _Seen, *, ignore_cancel=False, review_sleeps=30):
    async def background():
        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                seen.cancelled.set()
                if not ignore_cancel:
                    raise

    async def fake_review(cfg, mr_id, profile_name=None):
        seen.loop = asyncio.get_running_loop()
        seen.background = asyncio.ensure_future(background())
        seen.started.set()
        await asyncio.sleep(review_sleeps)
        return {"decision": "needs_human", "score": 70, "findings": []}
    return fake_review


def _left_running(loop) -> list:
    return [t for t in asyncio.all_tasks(loop) if not t.done()]


def test_外層逾時時_迴圈關掉前沒有還沒結束的工作(ws, monkeypatch):
    seen = _Seen()
    monkeypatch.setattr(ws, "review_mr", _review_with_background(seen))
    monkeypatch.setattr(ws, "REVIEW_TIMEOUT_SECONDS", 0.3)
    ws._run_review(41)
    assert seen.loop.is_closed()
    assert seen.cancelled.is_set() and seen.background.cancelled()
    assert _left_running(seen.loop) == []


def test_新_commit_取消時_迴圈關掉前沒有還沒結束的工作(ws, monkeypatch):
    seen = _Seen()
    monkeypatch.setattr(ws, "review_mr", _review_with_background(seen))
    t = threading.Thread(target=ws._run_review, args=(42,), daemon=True)
    t.start()
    assert seen.started.wait(5)
    assert ws._cancel(42)
    t.join(5)
    assert not t.is_alive()
    assert seen.loop.is_closed()
    assert seen.cancelled.is_set() and seen.background.cancelled()
    assert _left_running(seen.loop) == []
    assert 42 not in ws._inflight


def test_正常審完時背景工作也收掉(ws, monkeypatch):
    seen = _Seen()
    monkeypatch.setattr(ws, "review_mr", _review_with_background(seen, review_sleeps=0))
    ws._run_review(43)
    assert seen.loop.is_closed() and seen.background.cancelled()


def test_沒有背景工作時照常關掉(ws, monkeypatch):
    loops = []

    async def fake_review(cfg, mr_id, profile_name=None):
        loops.append(asyncio.get_running_loop())
        return {"decision": "auto_approved", "score": 100, "findings": []}
    monkeypatch.setattr(ws, "review_mr", fake_review)
    ws._run_review(44)
    assert loops[0].is_closed()


def test_吞掉取消的工作不會讓執行緒卡住(ws, monkeypatch, capsys):
    """收尾等待有上限:為吞掉取消、繼續跑的工作卡住整條執行緒不值得,照關並記下來。"""
    seen = _Seen()
    monkeypatch.setattr(ws, "review_mr", _review_with_background(seen, ignore_cancel=True))
    monkeypatch.setattr(ws, "REVIEW_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(ws, "_LOOP_CLEANUP_SECONDS", 0.3)
    t = threading.Thread(target=ws._run_review, args=(45,), daemon=True)
    started = time.monotonic()
    t.start()
    t.join(10)
    assert not t.is_alive(), "收尾沒有上限,執行緒卡住了"
    assert time.monotonic() - started < 5
    assert seen.cancelled.is_set() and seen.loop.is_closed()
    out = capsys.readouterr().out
    assert "1 個工作取消後" in out and "仍沒結束" in out


def test_背景工作被取消時丟出的例外只記類型_不記訊息(ws, monkeypatch, capsys):
    async def slow_boom():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise ValueError(SECRET)            # 被取消時丟出別的例外

    async def fake_review(cfg, mr_id, profile_name=None):
        asyncio.ensure_future(slow_boom())
        await asyncio.sleep(0)
        return {"decision": "needs_human", "score": 70, "findings": []}

    monkeypatch.setattr(ws, "review_mr", fake_review)
    ws._run_review(46)
    out = capsys.readouterr().out
    assert "收尾時有工作丟出 ValueError" in out
    assert SECRET not in out and "192.0.2.10" not in out


def test_收尾出錯不往外丟(ws, monkeypatch, capsys):
    async def fake_review(cfg, mr_id, profile_name=None):
        return {"decision": "auto_approved", "score": 100, "findings": []}

    async def broken(self):
        raise OSError(SECRET)
    monkeypatch.setattr(ws, "review_mr", fake_review)
    monkeypatch.setattr(asyncio.BaseEventLoop, "shutdown_asyncgens", broken)
    ws._run_review(47)                          # 不丟例外
    out = capsys.readouterr().out
    assert "收尾(shutdown_asyncgens)出錯:OSError" in out
    assert SECRET not in out


def test_關掉之後這條執行緒不再指向那個迴圈(ws, monkeypatch):
    async def fake_review(cfg, mr_id, profile_name=None):
        return {"decision": "auto_approved", "score": 100, "findings": []}
    monkeypatch.setattr(ws, "review_mr", fake_review)
    result = {}

    def run():
        ws._run_review(48)
        try:
            result["loop"] = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            result["loop"] = None
    t = threading.Thread(target=run)
    t.start()
    t.join(5)
    assert result["loop"] is None, "關掉的迴圈還掛在這條執行緒上"
