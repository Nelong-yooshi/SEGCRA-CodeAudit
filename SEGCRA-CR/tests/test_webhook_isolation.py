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
    monkeypatch.setattr(ws, "_notify_incomplete", lambda iid, why, sha=None: notified.append((iid, why)))
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
    monkeypatch.setattr(ws, "_notify_incomplete", lambda iid, why, sha=None: notified.append((iid, why)))
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
    calls = []

    async def fake_review(cfg, mr_id, profile_name=None):
        import asyncio
        # 第幾次呼叫決定行為,**不是**看「取消了沒有」:後者在整套件一起跑(機器忙)
        # 時會偶發失敗 —— 第二次可能比第一次的 CancelledError 先開始,於是兩次都睡 30 秒。
        calls.append(mr_id)
        if len(calls) == 1:
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


# ─────────────── action=update 不等於推了新 commit(#53 review 第 2 點) ───────────────

class _Resp:
    """最小的假 HTTP 處理器:只驗 do_POST 的分派,不開真的 port。"""

    def __init__(self):
        self.code = None
        self.msg = None

    def _send(self, code, msg="ok"):
        self.code, self.msg = code, msg
        return None


def _post(ws, monkeypatch, payload, triggered):
    """跑一次 Handler.do_POST 的分派邏輯(略過 HTTP 與 token 驗證)。"""
    import json as _json
    monkeypatch.setattr(ws, "SECRET", "s")
    monkeypatch.setattr(ws, "_trigger",
                        lambda iid, head_sha=None: triggered.append((iid, head_sha)) or "ok")
    body = _json.dumps(payload).encode()

    class H(ws.Handler):
        def __init__(self):
            self.headers = {"X-Gitlab-Token": "s", "Content-Length": str(len(body))}
            self.rfile = __import__("io").BytesIO(body)
            self.path = "/gitlab-webhook"
            self.code = self.msg = None

        def _send(self, code, msg="ok"):
            self.code, self.msg = code, msg

    h = H()
    ws.Handler.do_POST(h)
    return h


def test_只改label的update不會觸發審查(ws, monkeypatch):
    """GitLab 改 label、標題、描述、指派人都是 action=update——**包括審查自己最後
    set_label 那一下**。以前的 debounce 剛好把審查中收到的這種事件擋掉;改成「取消舊的
    重審」之後,每個 MR 至少會多跑一次完整審查,而且兩次決策不同時 label 又變、再觸發
    一次。只有推了新 commit 才會帶 oldrev,所以用它分辨。"""
    triggered = []
    h = _post(ws, monkeypatch, {"object_kind": "merge_request",
                            "object_attributes": {"action": "update", "iid": 3,
                                                  "labels": [{"title": "ai-review::blocked"}]}},
              triggered)
    assert triggered == [], "只改 label 不該觸發重審"
    assert h.code == 200 and "沒有新 commit" in h.msg


def test_推了新commit的update會觸發並帶上head_sha(ws, monkeypatch):
    triggered = []
    _post(ws, monkeypatch, {"object_kind": "merge_request",
                        "object_attributes": {"action": "update", "iid": 3,
                                              "oldrev": "a" * 40,
                                              "last_commit": {"id": "b" * 40}}},
          triggered)
    assert triggered == [(3, "b" * 40)], triggered


@pytest.mark.parametrize("action", ["open", "reopen"])
def test_開啟與重開照樣觸發(action, ws, monkeypatch):
    """oldrev 只在 update 時才有意義;open/reopen 本來就沒有,不可以被一起擋掉。"""
    triggered = []
    _post(ws, monkeypatch, {"object_kind": "merge_request",
                        "object_attributes": {"action": action, "iid": 9,
                                              "last_commit": {"id": "c" * 40}}},
          triggered)
    assert triggered == [(9, "c" * 40)]


def test_審查未完成要把commit_status設成失敗(ws, monkeypatch):
    """留言只是給人看的;合併閘門靠的是 commit status(Pipelines must succeed)。
    只留言不設狀態,等於審查沒跑完卻沒有東西擋著合併。"""
    calls = {}
    import types
    fake = types.SimpleNamespace(
        post_summary=lambda *a, **k: calls.setdefault("summary", a),
        set_commit_status=lambda *a, **k: calls.setdefault("status", a),
        get_mr_diff=lambda mr_id: '{"head_sha": "deadbeef"}')
    import toolbox
    monkeypatch.setattr(toolbox, "gitlab", fake, raising=False)
    ws._notify_incomplete(11, "超過上限")
    assert "summary" in calls, "留言不可少"
    sha, state, name, desc, mr_id = calls["status"]
    assert (sha, state, name) == ("deadbeef", "failed", "segcra/review")
    assert "審查未完成" in desc and mr_id == "11"


def test_留言失敗不影響commit_status(ws, monkeypatch):
    """兩個動作各自 try:留言失敗不該讓狀態也設不上——狀態才是擋合併的那個。"""
    calls = {}
    import types

    def boom(*a, **k):
        raise RuntimeError("留言壞了")

    fake = types.SimpleNamespace(
        post_summary=boom,
        set_commit_status=lambda *a, **k: calls.setdefault("status", a),
        get_mr_diff=lambda mr_id: '{"head_sha": "abc"}')
    import toolbox
    monkeypatch.setattr(toolbox, "gitlab", fake, raising=False)
    ws._notify_incomplete(12, "程式發生 RuntimeError", "abc")
    assert calls["status"][0] == "abc" and calls["status"][1] == "failed"
