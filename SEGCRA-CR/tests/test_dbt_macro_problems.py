"""MR 改到的 macro 檔載入失敗時補 finding。

Jinja 語法錯誤(區塊沒閉合、運算式寫錯、不認得的標籤…)dbt 一樣解析不了、整個專案編譯失敗,補 blocker。
不認得的濾器、重複定義的 block(dbt 解析得過、用到時才失敗)、巢狀太深、載入時失敗,補 major「無法確認」。
dbt 加的濾器展開器都有,用到的 macro 不報。
"""
import asyncio
import json

import pytest

from orchestrator import dbt_context, dbt_project, dbt_render, pipeline
from orchestrator.config import DBT_DATABASE_PLACEHOLDER, load_config
from orchestrator.dbt_context import prepare_dbt_context
from orchestrator.dbt_render import (MACRO_LOAD_FAILED, MACRO_SYNTAX, MACRO_TOO_DEEP,
                                     MACRO_UNSUPPORTED, MacroCheck, check_macro_files)
from tests.test_dbt_affected import CFG, MACRO_MR, _Loader, _mr, _project

DB = DBT_DATABASE_PLACEHOLDER


@pytest.fixture(autouse=True)
def _fresh_project(monkeypatch):
    """取回的專案依 commit 快取,每題清掉;專案資訊在正式路徑是子行程整理的,測試換成同行程。"""
    monkeypatch.setattr(dbt_context, "_summarize_isolated", dbt_context._summarize)
    dbt_project.clear_cache()
    yield
    dbt_project.clear_cache()


def _issues(files: dict, **kw):
    result = check_macro_files(macro_files=files, database=DB, **kw)
    assert result.ok
    return {i["path"]: (i["kind"], i["line"]) for i in result.issues}


# ─────────────────── 分類 ───────────────────

@pytest.mark.parametrize("text,line", [
    ("{% macro m() %}\nselect {{ x( }}\n{% endmacro %}", 2),            # 運算式寫錯
    ("{% macro m() %}\n{% if x %}select 1\n{% endmacro %}", 3),          # if 沒閉合(Jinja 說不認得 endmacro)
    ("{% macro m() %}\n{% else %}\n{% endmacro %}", 2),                  # 多出來的 else
    ("{% macro m() %}\nselect 1\n", 1),                                   # macro 沒閉合
    ("{% macro m() %}{% for x in y %}{% endif %}{% endmacro %}", 1),     # 結束標籤對錯
    ("{% macro m() %}\n{% weird %}\n{% endmacro %}", 2),                 # 不認得的標籤
    ("{% macro m() %}\n{% if x %}\n{% iff y %}{% endif %}{% endmacro %}", 3),   # 打錯字,在區塊裡
    ("{% macro m() %}\n{% data_test t %}{% enddata_test %}{% endmacro %}", 2),   # dbt 也不認得
])
def test_Jinja語法錯誤是_syntax_並記下行號(text, line):
    assert _issues({"macros/a.sql": text}) == {"macros/a.sql": (MACRO_SYNTAX, line)}


@pytest.mark.parametrize("text", [
    "{% macro m() %}{{ x | no_such_filter }}{% endmacro %}",             # 不認得的濾器
    "{% block a %}{% endblock %}{% block a %}{% endblock %}",            # 重複定義的 block
])
def test_dbt解析得過_用到才失敗的寫法是_unsupported(text):
    assert _issues({"macros/a.sql": text})["macros/a.sql"][0] == MACRO_UNSUPPORTED


@pytest.mark.parametrize("name", ["as_text", "as_bool", "as_native", "as_number", "is_list"])
def test_dbt加的濾器展開器都有_用到的macro不報(name):
    text = "{% macro m(x) %}{{ x | " + name + " }}{% endmacro %}"
    assert _issues({"macros/a.sql": text}) == {}


@pytest.mark.parametrize("expr,out", [
    ("'a' | as_text", "a"), ("1 | as_number", "1"), ("true | as_bool", "True"),
    ("'x' | as_native", "x"), ("[1] | is_list", "True"), ("'a' | is_list", "False"),
    ("(1,) | is_list", "False"), ("{} | is_list", "False"),
])
def test_dbt加的濾器照dbt的行為展開(expr, out):
    """dbt_common 的 TEXT_FILTERS:as_text / as_bool / as_native / as_number 原樣回傳,
    is_list 只有 list 是 True。"""
    r = dbt_render.render_model("models/m.sql", source="select {{ " + expr + " }}", database=DB)
    assert r.ok and r.sql == f"select {out}"


def test_巢狀太深是_too_deep():
    text = "{% macro m() %}{{ " + "(" * 400 + "1" + ")" * 400 + " }}{% endmacro %}"
    assert _issues({"macros/a.sql": text})["macros/a.sql"][0] == MACRO_TOO_DEEP


def test_解析得過但載入失敗是_load_failed():
    text = "{% set x = not_defined_anywhere() %}{% macro m() %}1{% endmacro %}"
    assert _issues({"macros/a.sql": text}) == {"macros/a.sql": (MACRO_LOAD_FAILED, 0)}


@pytest.mark.parametrize("text", [
    "{% macro m() %}select 1{% endmacro %}",
    "{% materialization m, default %}select 1{% endmaterialization %}",
    "{% test not_null(model) %}select 1{% endtest %}",
    "{% snapshot s %}select 1{% endsnapshot %}",
    "{% docs d %}說明{% enddocs %}",
    "{% macro m() %}{% for x in [1] %}{% continue %}{% endfor %}{% endmacro %}",
])
def test_正常的macro檔與dbt的區塊標籤沒有問題(text):
    assert _issues({"macros/a.sql": text}) == {}


def test_用跟展開一樣的參數載入_模組層用到資料庫名才不會假失敗():
    text = "{% set d = target.database ~ '_x' %}{% macro m() %}{{ d }}{% endmacro %}"
    assert _issues({"macros/a.sql": text}) == {}
    no_db = check_macro_files(macro_files={"macros/a.sql": text})
    assert [i["kind"] for i in no_db.issues] == [MACRO_LOAD_FAILED]


def test_只看_macro_目錄底下的檔():
    assert _issues({"models/a.sql": "{{ x( }}", "macros/b.sql": "{{ y( }}"},
                   macro_dirs=("macros",)) == {"macros/b.sql": (MACRO_SYNTAX, 1)}


def test_從磁碟讀macro檔_不是UTF8時記成載入失敗(tmp_path):
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "bad.sql").write_bytes(bytes([0xFF, 0xFE]) + b"{% macro m() %}1{% endmacro %}")
    env, _, _, problems = dbt_render.build_env(code_root=tmp_path, database=DB)
    assert problems and env.segcra_macro_issues == [
        {"path": "macros/bad.sql", "kind": MACRO_LOAD_FAILED, "line": 0}]


def test_結果不帶錯誤訊息裡的樣板內容():
    result = check_macro_files(macro_files={"macros/a.sql": "{% ZZ_SECRET_TAG_ZZ %}"}, database=DB)
    assert "ZZ_SECRET_TAG_ZZ" not in json.dumps(result.issues, ensure_ascii=False)


def test_資源上限等使檢查沒做完時_ok是False(monkeypatch):
    monkeypatch.setattr(dbt_render, "MAX_MACRO_FILES", 1)
    files = {"macros/a.sql": "{% macro a() %}1{% endmacro %}",
             "macros/b.sql": "{% macro b() %}1{% endmacro %}"}
    assert check_macro_files(macro_files=files, database=DB) == MacroCheck(ok=False)


def test_與內建同名的macro使檢查沒做完():
    files = {"macros/a.sql": "{% macro ref() %}1{% endmacro %}"}
    assert check_macro_files(macro_files=files, database=DB).ok is False


def test_隔離子行程跑得起來_結果帶得回來():
    """正式路徑走子行程:結果要能從子行程送回來。"""
    result = dbt_render.check_macro_files_isolated(
        macro_files={"macros/a.sql": "{{ x( }}"}, database=DB)
    assert result == MacroCheck(ok=True, issues=({"path": "macros/a.sql", "kind": MACRO_SYNTAX,
                                                    "line": 1},))


@pytest.mark.parametrize("returned", [None, "x", RuntimeError("boom")])
def test_子行程失敗或回傳不對時_ok是False(monkeypatch, returned):
    def fake(fn, args, kwargs, *, timeout_s, max_memory_mb, on_failure):
        return on_failure("逾時") if isinstance(returned, Exception) else returned
    monkeypatch.setattr(dbt_render, "run_isolated", fake)
    assert dbt_render.check_macro_files_isolated(macro_files={}, database=DB) == MacroCheck(ok=False)


# ─────────────────── 管線:只看 MR 改到的 macro 檔 ───────────────────

@pytest.fixture
def _in_process(monkeypatch):
    calls = []

    def check(**kwargs):
        calls.append(kwargs)
        return check_macro_files(**kwargs)
    monkeypatch.setattr(pipeline, "check_macro_files_isolated", check)
    return calls


def _ctx(body, mr=MACRO_MR, extra=None):
    return prepare_dbt_context(mr, CFG, real_mode=True, load=_Loader(_project(body, extra)))


def test_改到的macro檔有語法錯誤_記下MR裡的路徑與行號(_in_process):
    ctx = _ctx("\n{{ x( }}")
    assert pipeline._macro_file_issues(MACRO_MR, ctx, CFG) == [
        {"path": "macros/act.sql", "kind": MACRO_SYNTAX, "line": 2}]
    [kw] = _in_process
    assert kw["macro_files"] is ctx.macro_files and kw["database"] == DB
    assert kw["macro_dirs"] == ctx.project.macro_paths and kw["project"] is ctx.info


def test_沒改到的macro檔壞掉不報(_in_process):
    ctx = _ctx("SELECT 1 AS id", extra={"macros/broken.sql": "{{ x( }}"})
    assert pipeline._macro_file_issues(MACRO_MR, ctx, CFG) == []


def test_沒改到macro檔時不檢查(_in_process):
    mr = _mr(["models/mrt_BIG.sql"])
    ctx = _ctx("SELECT 1 AS id", mr=mr)
    assert pipeline._macro_file_issues(mr, ctx, CFG) == [] and _in_process == []


def test_沒有專案時不檢查(_in_process):
    assert pipeline._macro_file_issues(MACRO_MR, None, CFG) == [] and _in_process == []


def test_刪除的macro檔不檢查(_in_process):
    mr = _mr(["macros/act.sql"])
    mr["files"][0]["deleted"] = True
    assert pipeline._macro_file_issues(mr, _ctx("SELECT 1 AS id", mr=mr), CFG) == []
    assert _in_process == []


def test_改到的macro檔不在取回的專案裡時不檢查(_in_process):
    files = {k: v for k, v in _project("SELECT 1 AS id").items() if k != "macros/act.sql"}
    ctx = prepare_dbt_context(MACRO_MR, CFG, real_mode=True, load=_Loader(files))
    assert pipeline._macro_file_issues(MACRO_MR, ctx, CFG) == [] and _in_process == []


def test_專案不在repo根目錄時_回報MR裡的路徑(_in_process):
    mr = _mr(["dbt/macros/act.sql"])
    cfg = {**CFG, "project_dir": "dbt"}
    ctx = prepare_dbt_context(mr, cfg, real_mode=True, load=_Loader(_project("{{ x( }}")))
    assert pipeline._macro_file_issues(mr, ctx, cfg) == [
        {"path": "dbt/macros/act.sql", "kind": MACRO_SYNTAX, "line": 1}]


def test_資料庫名不是約定的假名時不載入_記成無法確認(_in_process):
    ctx = _ctx("SELECT 1 AS id")
    out = pipeline._macro_file_issues(MACRO_MR, ctx, {**CFG, "database": "OTHER"})
    assert out == [{"path": "macros/act.sql", "kind": pipeline._MACRO_CHECK_FAILED, "line": 0}]
    assert _in_process == []


def test_檢查沒做完時_改到的檔一律記成無法確認(monkeypatch):
    monkeypatch.setattr(pipeline, "check_macro_files_isolated", lambda **kw: MacroCheck(ok=False))
    out = pipeline._macro_file_issues(MACRO_MR, _ctx("SELECT 1 AS id"), CFG)
    assert out == [{"path": "macros/act.sql", "kind": pipeline._MACRO_CHECK_FAILED, "line": 0}]


# ─────────────────── finding ───────────────────

def _findings(issues, report=None):
    report = pipeline.enforce_dbt_macro_problems(report or {"findings": []}, issues)
    return [f for f in report["findings"]
            if f["title"] in (pipeline._MACRO_SYNTAX_TITLE, pipeline._MACRO_UNCERTAIN_TITLE)]


def test_語法錯誤補blocker_寫出檔名與行號():
    [f] = _findings([{"path": "macros/act.sql", "kind": MACRO_SYNTAX, "line": 3}])
    assert (f["file"], f["line"], f["severity"]) == ("macros/act.sql", 3, "blocker")
    assert f["title"] == pipeline._MACRO_SYNTAX_TITLE
    assert "`macros/act.sql` 第 3 行附近" in f["detail"] and "整個載入失敗" in f["detail"]


@pytest.mark.parametrize("kind", [MACRO_UNSUPPORTED, MACRO_TOO_DEEP, MACRO_LOAD_FAILED,
                                  pipeline._MACRO_CHECK_FAILED])
def test_其他種類補major_無法確認(kind):
    [f] = _findings([{"path": "macros/act.sql", "kind": kind, "line": 0}])
    assert (f["severity"], f["line"], f["title"]) == ("major", 0, pipeline._MACRO_UNCERTAIN_TITLE)
    assert pipeline._MACRO_UNCERTAIN_REASONS[kind] in f["detail"] and "不得自動放行" in f["detail"]


@pytest.mark.parametrize("issue", [
    None, "x", {"kind": MACRO_SYNTAX, "line": 1}, {"path": 5, "kind": MACRO_SYNTAX},
    {"path": "macros/a.sql", "kind": "other", "line": 1},
])
def test_形狀不對的內容不補也不出錯(issue):
    assert _findings([issue]) == []


@pytest.mark.parametrize("line", [True, -1, "3", None])
def test_行號不對時寫0(line):
    [f] = _findings([{"path": "macros/a.sql", "kind": MACRO_SYNTAX, "line": line}])
    assert f["line"] == 0 and "第" not in f["detail"].split("有 Jinja")[0]


def test_路徑用code格式_不回顯不可列印字元():
    [f] = _findings([{"path": "macros/a`\x1b.sql", "kind": MACRO_SYNTAX, "line": 1}])
    assert "\x1b" not in f["detail"] and "``" in f["detail"]


def test_模型自己輸出同標題的info會被取代():
    fake = {"file": "macros/act.sql", "line": 0, "severity": "info",
            "title": pipeline._MACRO_SYNTAX_TITLE, "detail": "沒事", "suggestion": "", "citations": []}
    [f] = _findings([{"path": "macros/act.sql", "kind": MACRO_SYNTAX, "line": 1}],
                    {"findings": [fake]})
    assert f["severity"] == "blocker" and f["detail"] != "沒事"


def test_issues不是清單時不出錯():
    assert _findings(None) == [] and _findings("x") == []


# ─────────────────── 走整條 review_mr ───────────────────

def _review(monkeypatch, tmp_path, issues, *, dry_run=False, boom=False):
    from toolbox import gitlab

    fixtures, output = tmp_path / "fixtures", tmp_path / "out"
    fixtures.mkdir()
    output.mkdir()
    (fixtures / "mr_961.json").write_text(json.dumps({
        "title": "調整", "description": "例行維護", "sha": "e1a9" + "0" * 36,
        "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+SELECT 1 AS id",
                   "full_content": "SELECT 1 AS id"}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    async def fake_run_agent(cfg, profile, system, user, hub, **kw):
        return json.dumps({"summary": "x", "score": 100, "findings": []}, ensure_ascii=False)

    def fake_issues(mr, dbt_ctx, dbt_cfg):
        if boom:
            raise RuntimeError("壞了")
        return issues
    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline, "_macro_file_issues", fake_issues)
    return asyncio.run(pipeline.review_mr(load_config(), "961", dry_run=dry_run))


SYNTAX = [{"path": "macros/act.sql", "kind": MACRO_SYNTAX, "line": 2}]


def test_整條review_mr_語法錯誤補blocker_不得放行(monkeypatch, tmp_path):
    report = _review(monkeypatch, tmp_path, SYNTAX)
    assert [f["severity"] for f in report["findings"]
            if f["title"] == pipeline._MACRO_SYNTAX_TITLE] == ["blocker"]
    assert report["decision"] != "auto_approved"


def test_整條review_mr_沒有問題時不補(monkeypatch, tmp_path):
    report = _review(monkeypatch, tmp_path, [])
    assert not [f for f in report["findings"] if f["title"] == pipeline._MACRO_SYNTAX_TITLE]


def test_dry_run_也補(monkeypatch, tmp_path):
    report = _review(monkeypatch, tmp_path, SYNTAX, dry_run=True)
    assert [f["severity"] for f in report["findings"]
            if f["title"] == pipeline._MACRO_SYNTAX_TITLE] == ["blocker"]


def test_檢查這一步出錯時記成未完整_審查照常產出(monkeypatch, tmp_path):
    report = _review(monkeypatch, tmp_path, SYNTAX, boom=True)
    assert any(f.get("stage") == "macro 檔檢查" for f in report["findings"])
