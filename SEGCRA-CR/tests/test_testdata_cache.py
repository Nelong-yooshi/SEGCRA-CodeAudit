"""eval/testdata_cache.py — 凍結/重用 generate_cases() 產出的快取層,只給 eval
用,不呼叫 LLM、不連資料庫。

驗的東西:
  1. save() 預設拒絕凍結有覆蓋缺口的版本;force=True 才能覆蓋。
  2. save() → load() 能原樣拿回同一份 plan。
  3. 規格內容、測資生成 prompt、模型任一改了 → load() 回 None,不會誤用過期的
     測資計畫;沒記 prompt 雜湊/模型的舊檔也一樣。
  4. wrap():快取命中就不呼叫真的 generate_cases();沒命中才呼叫,而且不會
     自動幫你存檔(存檔是 freeze_testdata.py 的人工動作,不是這裡的副作用)。
  5. wrap() 命中時回傳的是重跑 _shape_check() 的缺口與剔除案例,不是一律回空。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import testdata_cache  # noqa: E402

MODEL = "gemma4:31b"


def _plan():
    return {"schema_ddl": ["CREATE TABLE t (id INT);"],
            "conditions": [{"id": "C1", "desc": "條件 C1"}],
            "cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
                       "rows": [["t", 1]], "expect_flagged": True, "note": "x"},
                      {"case_id": "C1-F", "condition_id": "C1", "direction": "false",
                       "rows": [["t", 2]], "expect_flagged": False, "note": "x"}]}


class _Cfg:
    """wrap() 只用到 role_profile()/profile() 取模型名稱。"""
    def __init__(self, model=MODEL):
        self._model = model

    def role_profile(self, role):
        return type("P", (), {"model": self._model})()

    def profile(self, name=None):
        return self.role_profile(None)


def _save(code="R-999", spec="規格內容", plan=None, gaps=(), **kw):
    kw.setdefault("model", MODEL)
    testdata_cache.save(code, spec, plan or _plan(), list(gaps), profile="review", **kw)


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    """每個測試用獨立的暫存目錄,不會動到真的 eval/testdata_cache/。"""
    monkeypatch.setattr(testdata_cache, "CACHE_DIR", tmp_path / "testdata_cache")


def test_有覆蓋缺口時預設拒絕凍結():
    with pytest.raises(ValueError, match="覆蓋缺口"):
        _save(gaps=["條件 C1 缺 false 向案例"])
    assert testdata_cache.load("R-999", "規格內容", model=MODEL) is None, \
        "拒絕存檔就不該留下任何檔案"


def test_force可以覆蓋有缺口的版本():
    _save(gaps=["條件 C1 缺 false 向案例"], force=True)
    assert testdata_cache.load("R-999", "規格內容", model=MODEL) == _plan()


def test_存檔後可以原樣拿回同一份plan():
    _save()
    assert testdata_cache.load("R-999", "規格內容", model=MODEL) == _plan()


def test_規格內容改了雜湊對不上就當沒凍結過():
    _save(spec="舊規格")
    assert testdata_cache.load("R-999", "新規格", model=MODEL) is None, \
        "規格已經變了,不能還用舊測資計畫——會驗到不是這次規格要求的東西"


def test_測資生成prompt改了就當沒凍結過(monkeypatch):
    _save()
    monkeypatch.setattr(testdata_cache.spec_exec, "testgen_system", lambda: "改過的 prompt")
    assert testdata_cache.load("R-999", "規格內容", model=MODEL) is None, \
        "prompt 改了,現在的生成器不會產出這批案例,不能當成它的產出"


def test_模型換了就當沒凍結過():
    _save()
    assert testdata_cache.load("R-999", "規格內容", model="qwen2.5-coder:7b") is None


@pytest.mark.parametrize("missing", ["prompt_sha256", "model"])
def test_沒記prompt雜湊或模型的舊檔當沒凍結過(missing):
    _save()
    p = testdata_cache._path("R-999")
    data = json.loads(p.read_text(encoding="utf-8"))
    del data[missing]
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert testdata_cache.load("R-999", "規格內容", model=MODEL) is None


def test_從沒凍結過回None不炸():
    assert testdata_cache.load("R-000", "隨便什麼規格", model=MODEL) is None


def test_wrap_快取命中時不呼叫真的generate_cases():
    _save()

    calls = []

    async def fake_generate(cfg, code, text, profile_name=None):
        calls.append((code, text))
        return {"schema_ddl": [], "conditions": [], "cases": []}, ["不該被呼叫"], []

    wrapped = testdata_cache.wrap(fake_generate)
    plan, gaps, dropped = asyncio.run(wrapped(_Cfg(), "R-999", "規格內容"))

    assert calls == [], "快取命中就不該呼叫真的 generate_cases"
    assert plan == _plan()
    assert gaps == [] and dropped == []


def test_wrap_force凍結的缺口讀回時不會歸零():
    """review 實測的情境:--force 凍結時 3 個缺口,讀回卻是 0 個,評測以為覆蓋完整。"""
    plan = {"schema_ddl": ["CREATE TABLE t (id INT);"],
            "conditions": [{"id": c, "desc": c} for c in ("C1", "C2", "C3", "C4")],
            "cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
                       "rows": [["t", 1]], "expect_flagged": True, "note": ""},
                      {"case_id": "C2-F", "condition_id": "C2", "direction": "false",
                       "rows": [["t", 2]], "expect_flagged": False, "note": ""},
                      {"case_id": "C3-T", "condition_id": "C3", "direction": "true",
                       "rows": [["t", 3]], "expect_flagged": True, "note": ""},
                      {"case_id": "C3-F", "condition_id": "C3", "direction": "false",
                       "rows": [["t", 4]], "expect_flagged": False, "note": ""}]}
    frozen_gaps = ["條件 C1 缺 false 向案例", "條件 C2 缺 true 向案例",
                   "條件 C4 缺 true 向案例", "條件 C4 缺 false 向案例"]
    _save(plan=plan, gaps=frozen_gaps, force=True)

    async def fake_generate(*a, **kw):
        raise AssertionError("快取命中就不該呼叫真的 generate_cases")

    _, gaps, _ = asyncio.run(testdata_cache.wrap(fake_generate)(_Cfg(), "R-999", "規格內容"))
    assert sorted(gaps) == sorted(frozen_gaps)


def test_wrap_手改壞的案例讀回時被剔除且看得到():
    """凍結檔是任何 PR 都改得到的檔案:形狀壞掉的案例要被剔除、列進 dropped,
    剔除後缺的方向也要列進缺口。"""
    plan = _plan()
    plan["cases"][1]["condition_id"] = "C9"     # 指向不存在的條件
    _save(plan=plan)

    async def fake_generate(*a, **kw):
        raise AssertionError("快取命中就不該呼叫真的 generate_cases")

    cleaned, gaps, dropped = asyncio.run(
        testdata_cache.wrap(fake_generate)(_Cfg(), "R-999", "規格內容"))
    assert [c["case_id"] for c in cleaned["cases"]] == ["C1-T"]
    assert len(dropped) == 1
    assert gaps == ["條件 C1 缺 false 向案例"]


def test_wrap_模型不同就不吃凍結檔():
    _save()
    calls = []

    async def fake_generate(cfg, code, text, profile_name=None):
        calls.append(code)
        return _plan(), [], []

    asyncio.run(testdata_cache.wrap(fake_generate)(_Cfg("qwen2.5-coder:7b"), "R-999", "規格內容"))
    assert calls == ["R-999"], "凍結檔是別的模型生的,應照常呼叫真的 generate_cases"


def test_wrap_沒命中時照常呼叫真的generate_cases_也不會自動存檔():
    async def fake_generate(cfg, code, text, profile_name=None):
        return _plan(), [], []

    wrapped = testdata_cache.wrap(fake_generate)
    plan, gaps, dropped = asyncio.run(wrapped(_Cfg(), "R-888", "規格內容"))

    assert plan == _plan()
    assert testdata_cache.load("R-888", "規格內容", model=MODEL) is None, \
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
