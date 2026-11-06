"""MR 改到的 macro 檔裡、從呼叫它的 model 展開時沒有出現的寫入語句。

把條件與 DELETE 搬進 macro、model 只呼叫它:model 原文沒有寫入關鍵字,只看 model 的檢查完全不會看到。
這裡用同一套方法(固定文字插記號、記號後面要接著那段固定文字、輸出去掉記號要跟沒插記號時相同)檢查
macro 檔,從呼叫它的 model 展開。只看 macro 與自訂物化的區塊裡面(區塊外 dbt 不執行)。

展開都跑真的(同一個行程,正式路徑是隔離子行程)。
"""
import asyncio

import pytest

from orchestrator import dbt_hidden_writes as hw
from orchestrator import pipeline
from orchestrator.dbt_render import render_model
from orchestrator.pipeline import _HIDDEN_WRITE_TITLE, _hidden_write_finding, _macro_hidden_writes
from tests.test_dbt_affected import (CFG, DB, MACRO_MR, _Hub, _Loader, _macro, _mr, _project,
                                     prepare_dbt_context, review)
# 匯入就會套用:專案資訊在同一個行程整理、每個測試清快取
from tests.test_dbt_affected import _fresh_cache, _summarize_in_process  # noqa: F401

MACRO = "macros/m.sql"


def _writes_only(out: dict) -> dict:
    """只留寫入語句那幾項:這一段的測試對準寫入關鍵字;分支裡的輸出另有一段測試。"""
    out = {k: v for k, v in out.items() if k != hw.UNREACHED_OUTPUT}
    if hw.UNREACHED not in out:
        out.pop(hw.MODELS_CHECKED, None)
    return out


def _check(macro_src, models: dict, others=None, calls=None, outputs=False, **kw):
    """models:{model 路徑: 內容},依序當成呼叫它的 model。
    outputs=False 時拿掉分支輸出那一項(見 _writes_only)。"""
    def render(text, model, incremental):
        if calls is not None:
            calls.append((model, incremental))
        return render_model(model, source=models[model], database=DB,
                            macro_files={**(others or {}), MACRO: text},
                            **({"incremental": True} if incremental else {}))
    out = hw.find_hidden_writes_in_macro(macro_src, list(models), render, **kw)
    return out if outputs else _writes_only(out)


HIDE = "{% macro body() %}SELECT 1 AS a\n{% if invocation_id == 'never' %};DELETE FROM t{% endif %}{% endmacro %}"
CALLER = {"models/a.sql": "{{ body() }}"}


# ─────────────────── 藏在 macro 裡的寫入語句 ───────────────────

def test_藏在_macro_沒展開到的分支會被標出來():
    assert _check(HIDE, CALLER) == {hw.UNREACHED: [{"line": 2, "keyword": "DELETE"}],
                                     hw.MODELS_CHECKED: 1}


def test_有展開到的不報():
    src = "{% macro body() %}SELECT 1 AS a;\nDELETE FROM t WHERE d < 1{% endmacro %}"
    assert _check(src, CALLER) == {}


def test_區塊外的內容不看_dbt_不執行():
    src = "-- DELETE FROM 舊表的說明\n{% set q = 'DELETE FROM t' %}\n" \
          "{% macro body() %}SELECT 1 AS a{% endmacro %}\nDROP TABLE x"
    calls = []
    assert _check(src, CALLER, calls=calls) == {}
    assert calls == []                      # 區塊裡沒有寫入關鍵字:不用展開


def test_區塊裡標記字串的寫入語句照報():
    src = "{% macro body() %}{% set q = 'DELETE FROM t' %}{% do run_query(q) %}SELECT 1{% endmacro %}"
    assert _check(src, CALLER) == {hw.IN_TAGS: [{"line": 1, "keyword": "DELETE"}]}


def test_換一個_model_才展開到的也算展開到():
    src = "{% macro body(x) %}SELECT 1 AS a{% if x %};DELETE FROM t{% endif %}{% endmacro %}"
    models = {"models/a.sql": "{{ body(false) }}", "models/b.sql": "{{ body(true) }}"}
    assert _check(src, models) == {}
    # 只看第一個 model 時就是沒展開到
    assert _check(src, models, max_models=1) == {hw.UNREACHED: [{"line": 1, "keyword": "DELETE"}],
                                                 hw.MODELS_CHECKED: 1}


def test_全部展開到就不再展開後面的_model():
    src = "{% macro body() %}SELECT 1 AS a;DELETE FROM t WHERE d < 1{% endmacro %}"
    models = {"models/a.sql": "{{ body() }}", "models/b.sql": "{{ body() }}"}
    calls = []
    assert _check(src, models, calls=calls) == {}
    assert {m for m, _ in calls} == {"models/a.sql"}


def test_最多展開幾個_model():
    models = {f"models/m{i}.sql": "SELECT 1 AS a" for i in range(hw.MAX_MACRO_MODELS + 2)}
    calls = []
    out = _check(HIDE, models, calls=calls)
    assert out[hw.MODELS_CHECKED] == hw.MAX_MACRO_MODELS
    assert len({m for m, _ in calls}) == hw.MAX_MACRO_MODELS


def test_增量分支的在增量模式展開到_不報():
    src = ("{% macro body() %}SELECT 1 AS a{% if is_incremental() %};DELETE FROM t WHERE d < 1"
           "{% endif %}{% endmacro %}")
    calls = []
    assert _check(src, CALLER, calls=calls) == {}
    assert ("models/a.sql", True) in calls


def test_macro_的輸出被收起來丟掉_算沒展開到():
    models = {"models/a.sql": "{% set _ %}{{ body() }}{% endset %}SELECT 1 AS a"}
    assert _check(HIDE, models)[hw.UNREACHED] == [{"line": 2, "keyword": "DELETE"}]


def test_輸出會因為記號改變時記成無法確認():
    src = "{% macro body() %}{% set s %}DELETE FROM t{% endset %}SELECT {{ s | length }} AS n{% endmacro %}"
    assert _check(src, CALLER) == {
        hw.UNVERIFIED: "加了檢查記號後展開結果不同,樣板會因為內容改變輸出,無法確認"}


def test_加了記號後展開失敗時記成無法確認():
    def render(text, model, incremental):
        if "__SEGCRA_W" in text:
            return render_model(model, source="{{ nope() }}", database=DB)
        return render_model(model, source=CALLER[model], database=DB, macro_files={MACRO: text})
    out = hw.find_hidden_writes_in_macro(HIDE, list(CALLER), render)
    assert out == {hw.UNVERIFIED: "加了檢查記號後展開失敗,無法確認寫入語句有沒有展開到"}


@pytest.mark.parametrize("models", [{}, {"models/a.sql": "{{ nope() }}"}], ids=["no-caller", "all-fail"])
def test_沒有_model_可以展開時記成無法確認(models):
    out = _check(HIDE, models)
    assert set(out) == {hw.UNVERIFIED} and "無法確認" in out[hw.UNVERIFIED]


def test_第一個_model_展開失敗就換下一個():
    models = {"models/bad.sql": "{{ nope() }}", "models/a.sql": "{{ body() }}"}
    assert _check(HIDE, models) == {hw.UNREACHED: [{"line": 2, "keyword": "DELETE"}],
                                     hw.MODELS_CHECKED: 1}


def test_關鍵字太多時不逐一確認_也不展開():
    src = "{% macro body() %}" + ";DELETE FROM t" * (hw.MAX_MARKED_KEYWORDS + 1) + "{% endmacro %}"
    calls = []
    out = _check(src, CALLER, calls=calls)
    assert set(out) == {hw.UNVERIFIED} and calls == []


def test_不是字串時回空的():
    assert hw.find_hidden_writes_in_macro(None, [], lambda *a: None) == {}


# ─────────────────── 區塊範圍 ───────────────────

def test_區塊範圍():
    src = ("OUT1\n{%- macro a() -%}IN_A{%- endmacro -%}\nOUT2\n"
           "{% materialization custom, default %}IN_M{% endmaterialization %}\nOUT3{% macro b() %}IN_B")
    blocks = hw.macro_blocks(src)
    words = ("OUT1", "OUT2", "OUT3", "IN_A", "IN_B", "IN_M")
    inside = {w for w in words if any(s <= src.index(w) < e for s, e in blocks)}
    assert inside == {"IN_A", "IN_B", "IN_M"}
    assert blocks[-1][1] == len(src)            # 沒有結尾的區塊算到檔尾


# ─────────────────── 預掃 ───────────────────

@pytest.fixture
def _in_process(monkeypatch):
    calls = []

    def render(*args, timeout_s=None, **kwargs):
        calls.append(args[0])
        return render_model(*args, **kwargs)
    monkeypatch.setattr(pipeline, "render_model_isolated", render)
    return calls


HIDDEN_BODY = "SELECT 1 AS id{% if invocation_id == 'never' %};DELETE FROM dbo.txn{% endif %}"


def _prescan_macro(body):
    ctx = prepare_dbt_context(MACRO_MR, CFG, real_mode=True, load=_Loader(_project(body)))
    files = [{"path": "macros/act.sql", "diff": "@@ -1 +1 @@\n+x"}]
    return asyncio.run(pipeline.prescan(_Hub(), files, CFG, ctx))[0], ctx


def test_預掃記下_macro_檔沒有展開到的寫入語句(_in_process):
    entry, _ = _prescan_macro(HIDDEN_BODY)
    assert entry["dbt_macro"] is True
    assert entry["dbt_hidden_writes"] == {hw.UNREACHED: [{"line": 1, "keyword": "DELETE"}],
                                          hw.UNREACHED_OUTPUT: [{"line": 1}], hw.MODELS_CHECKED: 1}
    # 從呼叫它的 model 展開(mrt_OTHER 不呼叫它)
    assert set(_in_process) == {"models/mrt_BIG.sql"}


def test_預掃_macro_檔沒有藏東西時沒有這個欄位(_in_process):
    entry, _ = _prescan_macro("SELECT 1 AS id;DELETE FROM dbo.txn WHERE d < 1")
    assert "dbt_hidden_writes" not in entry


def test_被刪掉的_macro_檔不檢查(_in_process):
    _, ctx = _prescan_macro(HIDDEN_BODY)
    assert _macro_hidden_writes("macros/gone.sql", CFG, ctx) == {}


def test_資料庫名稱不是約定的假名時記成無法確認(_in_process):
    _, ctx = _prescan_macro(HIDDEN_BODY)
    out = _macro_hidden_writes("macros/act.sql", {**CFG, "database": "PROD"}, ctx)
    assert set(out) == {hw.UNVERIFIED} and "約定的假名" in out[hw.UNVERIFIED]


def test_檢查本身出錯時記成無法確認(_in_process, monkeypatch):
    _, ctx = _prescan_macro(HIDDEN_BODY)

    def boom(*a, **kw):
        raise ValueError("x")
    monkeypatch.setattr(pipeline, "find_hidden_writes_in_macro", boom)
    assert _macro_hidden_writes("macros/act.sql", CFG, ctx) == {
        hw.UNVERIFIED: "檢查寫入語句時發生非預期錯誤(ValueError)"}


def test_呼叫鏈短的_model_先展開(_in_process):
    """直接呼叫被改 macro 的 model 比較可能展開到它的每個分支。"""
    extra = {"macros/mid.sql": _macro("mid", "{{ body() }}"),
             "models/mrt_A_INDIRECT.sql": "{{ mid() }}\n"}
    ctx = prepare_dbt_context(MACRO_MR, CFG, real_mode=True,
                              load=_Loader(_project(HIDDEN_BODY, extra)))
    _macro_hidden_writes("macros/act.sql", CFG, ctx)
    assert _in_process[0] == "models/mrt_BIG.sql"


def test_macro_檔的_finding_寫出展開了幾個_model():
    fd = _hidden_write_finding({"path": "macros/act.sql", "dbt_hidden_writes": {
        hw.UNREACHED: [{"line": 3, "keyword": "DELETE"}], hw.MODELS_CHECKED: 2}})
    assert (fd["severity"], fd["title"], fd["line"]) == ("major", _HIDDEN_WRITE_TITLE, 3)
    assert "呼叫這個 macro 檔的 model" in fd["detail"] and "2 個 model" in fd["detail"]
    assert "DELETE(第 3 行)" in fd["detail"]


@pytest.mark.parametrize("models", [True, "2", None])
def test_展開數不是整數時照一般的寫法(models):
    hidden = {hw.UNREACHED: [{"line": 3, "keyword": "DELETE"}]}
    if models is not None:
        hidden[hw.MODELS_CHECKED] = models
    fd = _hidden_write_finding({"path": "macros/act.sql", "dbt_hidden_writes": hidden})
    assert "呼叫這個 macro 檔" not in fd["detail"] and "DELETE(第 3 行)" in fd["detail"]


# ─────────────────── 整條 review_mr ───────────────────

FRAGMENT = "@@ -1 +1 @@\n+SELECT 1 AS id"


def test_整條review_mr_藏在_macro_的寫入語句不得自動放行(review, _in_process):
    report, _ = review([{"path": "macros/act.sql", "diff": FRAGMENT}],
                       _Loader(_project(HIDDEN_BODY), _project("SELECT 1 AS id")))
    (fd,) = [f for f in report["findings"] if f["title"] == _HIDDEN_WRITE_TITLE]
    assert fd["severity"] == "major" and fd["file"] == "macros/act.sql"
    assert report["decision"] == "needs_human"


def test_整條review_mr_沒有藏東西時不補(review, _in_process):
    report, _ = review([{"path": "macros/act.sql", "diff": FRAGMENT}],
                       _Loader(_project("SELECT 1 AS id"), _project("SELECT 2 AS id")))
    assert not [f for f in report["findings"] if f["title"] == _HIDDEN_WRITE_TITLE]


# ─────────────────── 沒展開到的分支裡有輸出 ───────────────────

@pytest.mark.parametrize("body", [
    "{{ 'DEL' ~ 'ETE' }} FROM t",
    "{% set k = 'DEL' + 'ETE' %}{{ k }} FROM t",
])
def test_macro_沒展開到的分支用樣板組出關鍵字時記成無法確認(body):
    src = ("{% macro body() %}SELECT 1 AS a\n{% if invocation_id == 'never' %};" + body
           + "{% endif %}{% endmacro %}")
    assert _check(src, CALLER) == {}                 # 寫入關鍵字那一項看不到
    assert _check(src, CALLER, outputs=True) == {hw.UNREACHED_OUTPUT: [{"line": 2}],
                                                  hw.MODELS_CHECKED: 1}


def test_macro_分支換一個_model_才走到的也算產生過():
    src = "{% macro body(x) %}SELECT 1 AS a{% if x %}{{ ', 2 AS b' }}{% endif %}{% endmacro %}"
    models = {"models/a.sql": "{{ body(false) }}", "models/b.sql": "{{ body(true) }}"}
    calls = []
    assert _check(src, models, calls=calls, outputs=True) == {}
    assert {m for m, _ in calls} == {"models/a.sql", "models/b.sql"}
    assert _check(src, models, max_models=1, outputs=True) == {hw.UNREACHED_OUTPUT: [{"line": 1}],
                                                               hw.MODELS_CHECKED: 1}


def test_macro_區塊外的分支不看():
    src = "{% if false %}DROP{% endif %}\n{% macro body() %}SELECT 1 AS a{% endmacro %}"
    calls = []
    assert _check(src, CALLER, calls=calls, outputs=True) == {}
    assert calls == []


def test_用_return_回傳的_macro_分支有輸出時記成無法確認():
    """dbt 丟掉 return() 那個 macro 的輸出:記號看不出分支有沒有走到。"""
    src = ("{% macro body() %}{% if invocation_id == 'never' %}{{ return('DEL' ~ 'ETE FROM t') }}"
           "{% endif %}{{ return('SELECT 1 AS a') }}{% endmacro %}")
    out = _check(src, CALLER, outputs=True)
    assert set(out) == {hw.UNVERIFIED} and "return()" in out[hw.UNVERIFIED], out


def test_用_return_回傳但沒有分支的_macro_不報():
    src = "{% macro body() %}{% set c = {'a': 1} %}\n{{ return('SELECT 1 AS a') }}{% endmacro %}"
    assert _check(src, CALLER, outputs=True) == {}


def test_macro_檔的分支輸出_finding_寫出展開了幾個_model():
    fd = pipeline._unreached_branch_finding({"path": "macros/act.sql", "dbt_hidden_writes": {
        hw.UNREACHED_OUTPUT: [{"line": 4}], hw.MODELS_CHECKED: 2}})
    assert (fd["severity"], fd["line"]) == ("major", 4)
    assert "呼叫這個 macro 檔的 model" in fd["detail"] and "2 個 model" in fd["detail"]


@pytest.mark.parametrize("body", [
    "SELECT 1 AS id{% if invocation_id == 'never' %};{{ 'DEL' ~ 'ETE' }} FROM dbo.txn{% endif %}",
    "SELECT 1 AS id{% if invocation_id == 'never' %}{% set k = 'DEL' + 'ETE' %};{{ k }} FROM dbo.txn"
    "{% endif %}",
])
def test_整條review_mr_macro_裡樣板組出關鍵字藏起來的不得自動放行(review, _in_process, body):
    report, _ = review([{"path": "macros/act.sql", "diff": FRAGMENT}],
                       _Loader(_project(body), _project("SELECT 1 AS id")))
    assert not [f for f in report["findings"] if f["title"] == _HIDDEN_WRITE_TITLE]
    (fd,) = [f for f in report["findings"] if f["title"] == pipeline._UNREACHED_BRANCH_TITLE]
    assert fd["severity"] == "major" and fd["file"] == "macros/act.sql"
    assert report["decision"] == "needs_human"
