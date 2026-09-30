"""eval/testdata_cache.py — 凍結/重用 generate_cases() 產出的快取層,只給 eval
用,不呼叫 LLM、不連資料庫。

驗的東西:
  1. save() 預設拒絕凍結有覆蓋缺口的版本;force=True 才能覆蓋。
  2. save() → load() 能原樣拿回同一份 plan。
  3. 規格內容改了(雜湊對不上)→ load() 回 None,不會誤用過期的測資計畫。
  4. wrap():快取命中就不呼叫真的 generate_cases();沒命中才呼叫,而且不會
     自動幫你存檔(存檔是 freeze_testdata.py 的人工動作,不是這裡的副作用)。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import testdata_cache  # noqa: E402


def _plan():
    return {"schema_ddl": ["CREATE TABLE t (id INT);"],
            "conditions": [{"id": "C1", "desc": "條件 C1"}],
            "cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
                      "rows": [["t", 1]], "expect_flagged": True, "note": "x"}]}


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    """每個測試用獨立的暫存目錄,不會動到真的 eval/testdata_cache/。"""
    monkeypatch.setattr(testdata_cache, "CACHE_DIR", tmp_path / "testdata_cache")


def test_有覆蓋缺口時預設拒絕凍結():
    with pytest.raises(ValueError, match="覆蓋缺口"):
        testdata_cache.save("R-999", "規格內容", _plan(), ["條件 C1 缺 false 向案例"],
                            profile="review")
    assert testdata_cache.load("R-999", "規格內容") is None, "拒絕存檔就不該留下任何檔案"


def test_force可以覆蓋有缺口的版本():
    testdata_cache.save("R-999", "規格內容", _plan(), ["條件 C1 缺 false 向案例"],
                        profile="review", force=True)
    assert testdata_cache.load("R-999", "規格內容") == _plan()


def test_存檔後可以原樣拿回同一份plan():
    testdata_cache.save("R-999", "規格內容", _plan(), [], profile="review")
    assert testdata_cache.load("R-999", "規格內容") == _plan()


def test_規格內容改了雜湊對不上就當沒凍結過():
    testdata_cache.save("R-999", "舊規格", _plan(), [], profile="review")
    assert testdata_cache.load("R-999", "新規格") is None, \
        "規格已經變了,不能還用舊測資計畫——會驗到不是這次規格要求的東西"


def test_從沒凍結過回None不炸():
    assert testdata_cache.load("R-000", "隨便什麼規格") is None


def test_wrap_快取命中時不呼叫真的generate_cases(monkeypatch):
    testdata_cache.save("R-999", "規格內容", _plan(), [], profile="review")

    calls = []

    async def fake_generate(cfg, code, text, profile_name=None):
        calls.append((code, text))
        return {"schema_ddl": [], "conditions": [], "cases": []}, ["不該被呼叫"], []

    wrapped = testdata_cache.wrap(fake_generate)
    plan, gaps, dropped = asyncio.run(wrapped(None, "R-999", "規格內容"))

    assert calls == [], "快取命中就不該呼叫真的 generate_cases"
    assert plan == _plan()
    assert gaps == [] and dropped == []


def test_wrap_沒命中時照常呼叫真的generate_cases_也不會自動存檔(monkeypatch):
    async def fake_generate(cfg, code, text, profile_name=None):
        return _plan(), [], []

    wrapped = testdata_cache.wrap(fake_generate)
    plan, gaps, dropped = asyncio.run(wrapped(None, "R-888", "規格內容"))

    assert plan == _plan()
    assert testdata_cache.load("R-888", "規格內容") is None, \
        "wrap() 沒命中時只轉呼叫,不該有存檔這個副作用——存檔是人工動作"


# ─────────────────── run_eval 的接線 ───────────────────

class _Args:
    """`run_eval.run()` 在接線與早退之間會讀到的欄位。"""
    def __init__(self, **kw):
        self.frozen_testdata = False
        self.layer = None
        self.case = "不存在的case"     # 篩不到任何 case → run() 早退,不碰模型
        self.dry_run = False
        self.profile = None
        self.json = None
        self.dump_reports = None
        self.from_reports = None
        self.__dict__.update(kw)


@pytest.mark.parametrize("frozen,should_wrap", [(True, True), (False, False)])
def test_run_eval的frozen_testdata旗標真的會換掉generate_cases(
        monkeypatch, frozen, should_wrap):
    """`wrap()` 本身在上面測過了,但「`--frozen-testdata` 有沒有真的把它接上去」
    是另一件事——那是 `run_eval.run()` 裡的四行,壞掉的話旗標會**靜默失效**:
    照樣跑、照樣呼叫 LLM,只是慢得莫名其妙,沒有任何錯誤訊息。

    **所以這個測試必須真的呼叫 `run_eval.run()`**,不能在測試裡把接線自己重做
    一遍再斷言——那樣等於驗自己剛做的動作,把那四行刪掉測試照樣綠,正是它
    宣稱要防的失效模式。(同 FINDINGS.md「發現七」:驗不到東西的檢查比沒有更糟。)

    做法:給一個篩不到任何 case 的 `--case`,`run()` 會在接線**之後**、跑管線
    **之前**早退回 1,於是不需要模型也不需要沙盒。
    """
    import run_eval
    from orchestrator import spec_exec

    original = spec_exec.generate_cases
    monkeypatch.setattr(spec_exec, "generate_cases", original)   # 結束自動還原

    rc = asyncio.run(run_eval.run(_Args(frozen_testdata=frozen)))
    assert rc == 1, "篩不到 case 應早退回 1(代表沒有真的去跑管線)"

    replaced = spec_exec.generate_cases is not original
    assert replaced is should_wrap, (
        "旗標開啟時 generate_cases 必須被換成快取版" if should_wrap
        else "沒給旗標時不該動到 generate_cases")
    if replaced:
        assert asyncio.iscoroutinefunction(spec_exec.generate_cases), "換上去的必須還是 async"


def test_frozen_testdata與dry_run互斥有擋下來():
    """`--dry-run` 根本跑不到測資生成,兩個一起下是使用者誤解了其中一個的作用,
    當場講清楚比讓他等完一輪才發現沒效果好。"""
    import subprocess, sys, pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(root / "eval" / "run_eval.py"),
                        "--dry-run", "--frozen-testdata"],
                       capture_output=True, text=True, cwd=root)
    assert r.returncode != 0
    assert "沒有效果" in r.stderr or "沒有效果" in r.stdout
