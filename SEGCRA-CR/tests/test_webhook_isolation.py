"""#45 的 webhook 那一層:每個 MR 互相隔離、總時間上限、逾時要留言、新 commit 取消舊審查。

不起 HTTP 伺服器、不呼叫 LLM、不連 GitLab —— 直接驅動 `_trigger` / `_run_review`,
把 `review_mr` 與留言的函式換成假的。
"""
import importlib
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture()
def ws(monkeypatch):
    """每個測試拿一份乾淨的 webhook_server(它有模組層的狀態)。"""
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "x")
    import webhook_server as mod
    importlib.reload(mod)
    return mod


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_一個MR卡住不影響其他MR(ws, monkeypatch):
    """#45 的完成條件:一個 MR 的審查卡住或丟例外時,另一個 MR 照常完成。"""
    done = []
    started = threading.Event()

    async def fake_review(cfg, mr_id, profile_name=None):
        if mr_id == "1":
            started.set()
            import asyncio
            await asyncio.sleep(30)        # 卡住
        done.append(mr_id)
        return {"decision": "needs_human", "score": 70, "findings": []}

    monkeypatch.setattr(ws, "review_mr", fake_review)
    monkeypatch.setattr(ws, "_notify_incomplete", lambda *a, **k: None)

    ws._trigger(1)
    assert started.wait(5), "第一個 MR 沒開始"
    ws._trigger(2)
    assert _wait(lambda: "2" in done), "第二個 MR 被第一個卡住了"


def test_丟例外的MR不影響其他MR(ws, monkeypatch):
    done, notified = [], []

    async def fake_review(cfg, mr_id, profile_name=None):
        if mr_id == "1":
            raise RuntimeError("炸了")
        done.append(mr_id)
        return {"decision": "auto_approved", "score": 100, "findings": []}

    monkeypatch.setattr(ws, "review_mr", fake_review)
    monkeypatch.setattr(ws, "_notify_incomplete", lambda iid, why: notified.append((iid, why)))
    ws._trigger(1)
    ws._trigger(2)
    assert _wait(lambda: "2" in done), "另一個 MR 沒跑完"
    assert _wait(lambda: notified), "炸掉的那個沒有留言告知"
    assert "RuntimeError" in notified[0][1]


def test_逾時要中止並在MR上留言(ws, monkeypatch):
    """不能無聲無息:正式環境裡「沒擋、沒放、也沒人知道」是最糟的失敗型態。"""
    notified = []

    async def never(cfg, mr_id, profile_name=None):
        import asyncio
        await asyncio.sleep(30)

    monkeypatch.setattr(ws, "review_mr", never)
    monkeypatch.setattr(ws, "REVIEW_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(ws, "_notify_incomplete", lambda iid, why: notified.append((iid, why)))
    ws._trigger(7)
    assert _wait(lambda: notified, timeout=8), "逾時之後沒有留言"
    assert notified[0][0] == 7 and "時間上限" in notified[0][1]


def test_留言內容要說明這不是沒問題(ws, monkeypatch):
    posted = {}

    def fake_post_summary(mr_id, body, score, verdict):
        posted.update(mr_id=mr_id, body=body, score=score, verdict=verdict)
        return "ok"

    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "post_summary", fake_post_summary)
    ws._notify_incomplete(9, "超過 2400 秒的時間上限")
    assert posted["mr_id"] == "9"
    assert "請人工處理" in posted["body"]
    assert "不是「沒問題」" in posted["body"], "要講清楚未完成不等於通過"


def test_新的commit會取消舊的審查(ws, monkeypatch):
    """原本是 debounce(略過新的那次),但那會讓新的 commit 不被審查 ——
    舊的那次審的是舊程式,結論貼到新 commit 上是錯的。"""
    cancelled = threading.Event()
    second_done = []

    async def fake_review(cfg, mr_id, profile_name=None):
        import asyncio
        if not cancelled.is_set():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        second_done.append(mr_id)
        return {"decision": "needs_human", "score": 60, "findings": []}

    monkeypatch.setattr(ws, "review_mr", fake_review)
    monkeypatch.setattr(ws, "_notify_incomplete", lambda *a, **k: None)
    ws._trigger(5)
    assert _wait(lambda: 5 in ws._inflight), "第一次審查沒登記"
    ws._trigger(5)                      # 新的 commit
    assert cancelled.wait(5), "舊的審查沒有被取消"
    assert _wait(lambda: second_done), "新的審查沒有跑"


def test_被取消時不留言(ws, monkeypatch):
    """新的那次會給出結論,舊的被取代時不該在同一個 MR 洗版。"""
    notified = []

    async def fake_review(cfg, mr_id, profile_name=None):
        import asyncio
        await asyncio.sleep(30)

    monkeypatch.setattr(ws, "review_mr", fake_review)
    monkeypatch.setattr(ws, "_notify_incomplete", lambda iid, why: notified.append(iid))
    ws._trigger(6)
    assert _wait(lambda: 6 in ws._inflight)
    assert ws._cancel(6)
    time.sleep(0.5)
    assert notified == [], f"被取代的那次不該留言:{notified}"


def test_總上限要比執行驗證那段的上限大(ws):
    """執行驗證自己有上限(預設 1200 秒);整份的天花板要比它大,
    否則執行驗證永遠來不及把「逾時」那條 finding 寫進報告。"""
    from orchestrator import pipeline
    assert ws.REVIEW_TIMEOUT_SECONDS > pipeline.SPEC_EXEC_TIMEOUT_SECONDS
