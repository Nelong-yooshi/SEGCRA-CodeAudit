"""沙盒逾時之後,背景執行緒的收尾不得丟例外(#53 的 `_in_thread`)。

逾時的時候 `wait_for` 準時回來、審查往下走 —— 那部分是對的。但那個 daemon 執行緒
還在跑,等它跑完要把結果交回事件迴圈,而那時迴圈可能已經關掉了。

結果本來就不要了,放掉是對的;問題是**放掉的方式**:`call_soon_threadsafe` 會丟
`RuntimeError: Event loop is closed`,而它發生在執行緒裡沒有人接 ——
正式環境的 log 每逾時一次就多一段看起來很嚴重的 traceback,排查的人會先去追它。
"""
import asyncio
import threading
import time

import pytest

from orchestrator.spec_exec import _in_thread


def _run_and_abandon(fn, timeout=0.1):
    """跑一個會超時的 `_in_thread`,然後關掉迴圈 —— 重現「執行緒比迴圈活得久」。

    回傳那個背景執行緒,呼叫端可以 join 它、確認它安靜結束。
    """
    before = set(threading.enumerate())

    async def main():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(_in_thread(fn), timeout=timeout)

    asyncio.run(main())          # 離開時迴圈就關了,而背景執行緒還在跑
    worker = [t for t in threading.enumerate()
              if t not in before and t.name == "spec-exec-sandbox"]
    assert worker, "背景執行緒應該還活著(這個情境才成立)"
    return worker[0]


def test_迴圈關掉之後才跑完_不得丟例外():
    seen = []
    threading.excepthook = lambda a: seen.append(a)      # 執行緒裡沒人接的例外會走這裡
    try:
        worker = _run_and_abandon(lambda: time.sleep(0.4) or "done")
        worker.join(timeout=5)
        assert not worker.is_alive()
    finally:
        threading.excepthook = threading.__excepthook__
    assert seen == [], f"背景執行緒丟了沒人接的例外:{[a.exc_value for a in seen]}"


def test_函式自己丟例外時也一樣():
    """失敗那條路同樣會碰到已關閉的迴圈,兩條路都要安靜收掉。"""
    seen = []
    threading.excepthook = lambda a: seen.append(a)

    def boom():
        time.sleep(0.4)
        raise ValueError("沙盒掛了")

    try:
        worker = _run_and_abandon(boom)
        worker.join(timeout=5)
        assert not worker.is_alive()
    finally:
        threading.excepthook = threading.__excepthook__
    assert seen == [], f"背景執行緒丟了沒人接的例外:{[a.exc_value for a in seen]}"


def test_沒逾時的時候結果照常回來():
    """收尾改安靜之後,正常那條路不能跟著被吃掉。"""
    assert asyncio.run(_in_thread(lambda: "ok")) == "ok"


def test_沙盒丟例外時不得卡住():
    """`except ... as e` 的 e 在區塊結束就被刪掉,而收尾的 lambda 是稍後才跑的。

    直接捕捉 e 的話,lambda 跑的時候丟 NameError,fut 永遠不會有結果,await 就
    **永遠掛著** —— 沙盒丟例外時整個執行驗證卡住,要等外層的時間上限才被砍掉,
    而那是二十分鐘。現有的測試抓不到:它們用的替身都是「回傳錯誤值」,不是真的丟。
    """
    def boom():
        raise ValueError("沙盒掛了")

    async def main():
        # 真的卡住的話這一行會等到逾時;上限給得很短,卡住就是紅的
        with pytest.raises(ValueError, match="沙盒掛了"):
            await asyncio.wait_for(_in_thread(boom), timeout=5)

    asyncio.run(main())


def test_沒逾時的時候例外照常往外丟():
    async def main():
        with pytest.raises(ValueError, match="沙盒掛了"):
            await _in_thread(_raise)

    def _raise():
        raise ValueError("沙盒掛了")

    asyncio.run(main())
