"""審查時給 dbt 變數(排程每次執行時傳入的值)——不呼叫 LLM、不連資料庫。

部署端的 model 用 `var("target_date")` 取「這次執行的日期」,值由排程每次傳入。審查時沒有人傳:
沒有預設值的 `var()` 展開失敗(dbt 沒傳值時也是),報告多一條正式環境不會發生的 major,規則層
與執行驗證也跑不到這個 model。

  1. 設定檔:dbt.vars(固定值)、dbt.run_date_vars(這次執行的日期)的格式檢查
  2. 一份審查的變數:run_date_vars 一律是執行驗證測資的日期(WIN_START),蓋過 dbt.vars
  3. 展開:一份審查裡的展開都帶同一組;沒開始一份審查時不帶;呼叫端指定的照用
  4. 真實案例的形狀(同一個變數一處有預設值、一處沒有,加上前一天的比對)
  5. 沒給值時的錯誤訊息說得出要設什麼
  6. 整條 review_mr:有設定時不再報「展開失敗」
"""
from __future__ import annotations

import contextvars
import json

import pytest

from orchestrator import dbt_render, pipeline
from orchestrator.config import DBT_DATABASE_PLACEHOLDER as DB
from orchestrator.config import _load_dbt_section
from orchestrator.dbt_render import render_model, render_model_isolated, start_review_invocation
from orchestrator.spec_exec import WIN_START

# 真實案例的形狀:同一個變數一處有預設值、一處沒有;讀上游前一天的結果
REAL_SHAPE = ("{% set target_date = var(\"target_date\", \"2026-02-01\") %}\n"
              "SELECT a FROM t WHERE TABLE_DATE = '{{ target_date }}'\n"
              "AND NOT EXISTS (SELECT 1 FROM e WHERE e.TABLE_DATE = "
              "DATEADD(DAY, -1, '{{ var(\"target_date\") }}'))")


def _in_review(fn, variables=None):
    """在一份審查裡跑(另一份 context,不把這份審查的值留給後面的測試)。"""
    def run():
        start_review_invocation(variables)
        return fn()
    return contextvars.copy_context().run(run)


# ─────────────────── 1. 設定檔 ───────────────────

def test_設定檔收兩個設定():
    out = _load_dbt_section({"dbt": {"vars": {"region": "north", "lookback": 7, "full": True},
                                      "run_date_vars": ["target_date", "target_date"]}})
    assert out["vars"] == {"region": "north", "lookback": 7, "full": True}
    assert out["run_date_vars"] == ["target_date"]                      # 重複的只留一個


def test_沒設定時回傳的內容不變():
    out = _load_dbt_section({"dbt": {}})
    assert "vars" not in out and "run_date_vars" not in out


@pytest.mark.parametrize("section", [
    {"vars": ["target_date"]},                     # 不是物件
    {"vars": {"bad name": "x"}},                   # 名稱不是識別字
    {"vars": {"x": ["a", "b"]}},                   # 值不是純量
    {"vars": {"x": {"a": 1}}},
    {"vars": {"x": None}},
    {"run_date_vars": "target_date"},              # 不是清單
    {"run_date_vars": ["target-date"]},
    {"run_date_vars": [1]},
])
def test_設定檔格式不對時擋下(section):
    with pytest.raises(ValueError):
        _load_dbt_section({"dbt": section})


# ─────────────────── 2. 一份審查的變數 ───────────────────

def test_日期變數一律是測資的日期_蓋過固定值():
    got = pipeline._review_variables({"vars": {"region": "north", "target_date": "2020-01-01"},
                                      "run_date_vars": ["target_date", "as_of"]})
    assert got == {"region": "north", "target_date": WIN_START, "as_of": WIN_START}


@pytest.mark.parametrize("cfg", [None, {}, {"enabled": True}, "x"])
def test_沒有設定時沒有變數(cfg):
    assert pipeline._review_variables(cfg) == {}


# ─────────────────── 3. 展開 ───────────────────

def test_一份審查裡的展開都帶同一組變數():
    def work():
        a = render_model_isolated("models/m.sql", source="{{ var('target_date') }}")
        b = render_model_isolated("models/m.sql", source="{{ var('target_date') }}", incremental=True)
        return a, b
    a, b = _in_review(work, {"target_date": WIN_START})
    assert a.ok and b.ok, (a.error, b.error)
    assert a.sql == b.sql == WIN_START


def test_沒開始一份審查時不帶變數():
    r = contextvars.copy_context().run(
        lambda: render_model_isolated("models/m.sql", source="{{ var('target_date') }}"))
    assert not r.ok and "target_date" in r.error


def test_呼叫端指定的變數照用():
    r = _in_review(lambda: render_model_isolated(
        "models/m.sql", source="{{ var('target_date') }}", variables={"target_date": "x"}),
        {"target_date": WIN_START})
    assert r.ok and r.sql == "x"


def test_不把變數留給後面():
    _in_review(lambda: None, {"target_date": WIN_START})
    assert dbt_render._REVIEW_VARIABLES.get() is None


# ─────────────────── 4. 真實案例的形狀 ───────────────────

def test_真實案例的形狀_兩處都是測資的日期():
    r = _in_review(lambda: render_model_isolated("models/m.sql", source=REAL_SHAPE, database=DB),
                   pipeline._review_variables({"run_date_vars": ["target_date"]}))
    assert r.ok, r.error
    assert f"TABLE_DATE = '{WIN_START}'" in r.sql
    assert f"DATEADD(DAY, -1, '{WIN_START}')" in r.sql


def test_真實案例的形狀_沒設定時照舊展開失敗():
    r = render_model("models/m.sql", source=REAL_SHAPE, database=DB)
    assert not r.ok and "target_date" in r.error


# ─────────────────── 5. 錯誤訊息 ───────────────────

def test_沒給值時說得出要設什麼():
    r = render_model("models/m.sql", source="{{ var('target_date') }}")
    assert not r.ok
    assert "dbt.vars" in r.error and "dbt.run_date_vars" in r.error


# ─────────────────── 6. 整條 review_mr ───────────────────

from tests.test_dbt_affected import (CFG, _Loader, review)  # noqa: E402,F401
from tests.test_dbt_affected import _fresh_cache  # noqa: E402,F401

MODEL = "SELECT a FROM t WHERE TABLE_DATE = '{{ var(\"target_date\") }}'\n"


def _files():
    return {"dbt_project.yml": "name: sample\n", "models/mrt_BIG.sql": MODEL}


def _run(review, cfg_dbt):
    files = [{"path": "models/mrt_BIG.sql", "diff": "@@ -0,0 +1 @@\n+" + MODEL}]
    report, _ = review(files, _Loader(_files()), cfg_dbt=cfg_dbt)
    return report


def test_review_mr_有設定時不再報展開失敗(review):
    report = _run(review, {**CFG, "run_date_vars": ["target_date"]})
    titles = {f["title"] for f in report["findings"]}
    assert pipeline._RENDER_FAIL_TITLE not in titles, json.dumps(report["findings"], ensure_ascii=False)


def test_review_mr_沒設定時照舊報展開失敗(review):
    report = _run(review, dict(CFG))
    assert pipeline._RENDER_FAIL_TITLE in {f["title"] for f in report["findings"]}
