"""dbt 樣板展開後的規則命中,行號對回原始檔。

規則層掃的是展開後的 SQL,命中的語句要先在展開結果裡找到起始行,再用展開器的對應表(哨兵法)
換回原始檔的行。寫得出行號才寫,對不回就維持 0、不猜:
  * 空白控制(`{%- -%}`)把兩句接成同一個展開後的行時,第二句不寫(會指到第一句那一行)
  * macro 或表達式產生的語句不寫(原始檔沒有那句)
  * 對應表不是逐行精確的(近似對齊)、只有 diff 片段、展開結果太大,都不寫
預掃與規則層都跑真的(展開在同一個行程)。
"""
import asyncio
import dataclasses
import json

import pytest

from orchestrator import pipeline
from orchestrator.config import DBT_DATABASE_PLACEHOLDER, load_config
from orchestrator.dbt_render import render_model
from toolbox import sqltools

CFG = {"enabled": True, "database": DBT_DATABASE_PLACEHOLDER}
MACROS = {"macros/purge.sql": "{% macro purge(t) %}\nDELETE\nFROM {{ t }}\n{% endmacro %}\n",
          "macros/sel.sql": "{% macro sel() %}SELECT *{% endmacro %}\n"}


class _Hub:
    """規則層跑真的;lint 回空清單(不在這裡測)。"""

    async def call_json(self, name, args):
        if name == "sqltools__run_rules":
            return json.loads(sqltools.run_rules(args["sql"]))
        return []


@pytest.fixture(autouse=True)
def _render_in_process(monkeypatch):
    """預掃正式路徑走隔離子行程;測試換成同行程,行為相同、快很多。
    樣板沒有專案時,macro 由 MACROS 提供。"""
    def render(*args, **kwargs):
        kwargs.setdefault("macro_files", MACROS)
        return render_model(*args, **kwargs)
    monkeypatch.setattr(pipeline, "render_model_isolated", render)


def _lines(content, path="models/m.sql", files=None):
    """預掃 + 對回行號。回傳 [(規則, 行號, 第幾句, 分支)]。"""
    files = files or [{"path": path, "full_content": content,
                       "diff": "@@ -1 +1 @@\n+x"}]
    line_maps: dict = {}
    pre = asyncio.run(pipeline.prescan(_Hub(), files, CFG, None, None, line_maps))
    pipeline._attach_statement_lines(pre, files, line_maps)
    return [(h["rule"], h.get("line", 0), h.get("statement_index"), h.get("branch"))
            for h in pre[0]["rules"] if h["rule"].startswith("R")]


# ─────────────────── 寫得出行號 ───────────────────

def test_model_自己寫的語句_行號指到原始檔那一行():
    src = ("{{ config(materialized='table') }}\n\nselect a\nfrom {{ ref('x') }};\n\n"
           "DELETE FROM {{ ref('y') }};\nSELECT *\nFROM t\n")
    assert _lines(src) == [("R001", 6, 2, None), ("R002", 7, 3, None)]


def test_開頭有空行與跨行的_config():
    src = "\n\n{{ config(\n    materialized='table'\n) }}\n\n  DELETE FROM {{ ref('y') }}\n"
    assert _lines(src) == [("R001", 7, 1, None)]


def test_迴圈產生的語句都指到迴圈內那一行():
    src = "{% for i in range(2) %}\nDELETE FROM t{{ i }};\n{% endfor %}\n"
    assert [(r, ln) for r, ln, _, _ in _lines(src)] == [("R001", 2), ("R001", 2)]


def test_CRLF_的原始檔():
    src = "select 1;\r\n\r\nDELETE FROM {{ ref('y') }};\r\n"
    assert [(r, ln) for r, ln, _, _ in _lines(src)] == [("R001", 3)]


def test_增量分支的命中用增量那次的對應表():
    src = ("select id from {{ ref('x') }};\n"
           "{% if is_incremental() %}\n"
           "DELETE FROM {{ this }};\n"
           "{% endif %}\n")
    assert _lines(src) == [("R001", 3, 2, "incremental")]


# ─────────────────── 對不回就不寫 ───────────────────

def test_macro_產生的語句不寫行號_但記下是第幾句():
    src = "select a from t;\n{{ purge('old_tbl') }};\nselect 1\n"
    assert _lines(src) == [("R001", 0, 2, None)]


def test_表達式產生的語句開頭不寫行號():
    assert _lines("{{ sel() }}\nFROM t\n") == [("R002", 0, 1, None)]


def test_空白控制把兩句接成一行時_第二句不寫():
    src = "select a from t;\n{%- if true -%}\nDELETE FROM u\n{%- endif %}\n"
    assert _lines(src) == [("R001", 0, 2, None)]


def test_語句前面同一行有註解也不寫():
    assert _lines("select 1;\n/* x */ DELETE FROM {{ ref('y') }}\n") == [("R001", 0, 2, None)]


def test_對應表不是哨兵法時不寫(monkeypatch):
    def approx(*args, **kwargs):
        kwargs.setdefault("macro_files", MACROS)
        return dataclasses.replace(render_model(*args, **kwargs), map_method="difflib")
    monkeypatch.setattr(pipeline, "render_model_isolated", approx)
    assert _lines("select 1;\nDELETE FROM {{ ref('y') }};\n") == [("R001", 0, None, None)]


def test_只有_diff_片段時不寫():
    files = [{"path": "models/m.sql", "diff": "@@ -5,1 +5,2 @@\n+select 1;\n+DELETE FROM {{ ref('y') }};"}]
    assert [(r, ln) for r, ln, _, _ in _lines(None, files=files)] == [("R001", 0)]


def test_展開結果太大時不寫(monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_LINE_SOURCE_CHARS", 10)
    assert _lines("select 1;\nDELETE FROM {{ ref('y') }};\n") == [("R001", 0, None, None)]


def test_切不出語句時不寫(monkeypatch):
    monkeypatch.setattr(pipeline, "_rendered_starts", lambda sql: None)
    assert _lines("select 1;\nDELETE FROM {{ ref('y') }};\n") == [("R001", 0, None, None)]


# ─────────────────── 小零件 ───────────────────

@pytest.mark.parametrize("line,prefix", [
    ("DELETE FROM {{ ref('y') }}", "DELETE FROM"),
    ("  SELECT * ", "SELECT *"),
    ("{{ purge() }}", ""),
    ("x {# 註解 #} y", "x"),
    ("a {%- if b %}", "a"),
    ("沒有標記", "沒有標記"),
    ("", ""),
])
def test_固定文字(line, prefix):
    assert pipeline._literal_prefix(line) == prefix


def test_展開後語句的起始位置():
    starts = pipeline._rendered_starts("SELECT 1;  DELETE FROM a;\n  /*c*/ UPDATE x SET a = 1")
    flat = sorted((n, line, alone) for v in starts.values() for line, _, alone, n in v)
    assert flat == [(1, 1, True), (2, 1, False), (3, 2, False)]


@pytest.mark.parametrize("sql", ["SELECT (", ""])
def test_展開後切不出來時回_None(sql):
    assert pipeline._rendered_starts(sql) in (None, {})


def test_完整內容的判斷():
    f = {"path": "models/m.sql", "full_content": "SELECT 1"}
    assert pipeline._is_full_text(f, "SELECT 1", None)
    assert not pipeline._is_full_text(f, "SELECT", None)
    assert not pipeline._is_full_text({"path": "models/m.sql"}, "", None)


def test_完整內容的判斷_取回的專案內容也算():
    from orchestrator.dbt_context import DbtContext
    from orchestrator.dbt_project import DbtProject
    ctx = DbtContext(prefix="", project=DbtProject(ok=True, files={"models/m.sql": "HEAD"},
                                                   model_paths=("models",)))
    assert pipeline._is_full_text({"path": "models/m.sql"}, "HEAD", ctx)
    assert not pipeline._is_full_text({"path": "models/m.sql"}, "OTHER", ctx)


def test_對應表不進預掃結果_prompt_不變():
    src = "select 1;\nDELETE FROM {{ ref('y') }};\n"
    files = [{"path": "models/m.sql", "full_content": src, "diff": "@@ -1 +1 @@\n+x"}]
    with_maps = asyncio.run(pipeline.prescan(_Hub(), files, CFG, None, None, {}))
    without = asyncio.run(pipeline.prescan(_Hub(), files, CFG, None, None))
    assert json.dumps(with_maps, ensure_ascii=False) == json.dumps(without, ensure_ascii=False)


# ─────────────────── 報告 ───────────────────

def _report_for(src):
    files = [{"path": "models/m.sql", "full_content": src, "diff": "@@ -1 +1 @@\n+x"}]
    line_maps: dict = {}
    pre = asyncio.run(pipeline.prescan(_Hub(), files, CFG, None, None, line_maps))
    pipeline._attach_statement_lines(pre, files, line_maps)
    return pipeline.enforce_rules({"findings": []}, pre)["findings"]


def test_補的_finding_寫原始檔的行號():
    (fd,) = _report_for("select 1;\n\nDELETE FROM {{ ref('y') }};\n")
    assert fd["line"] == 3 and "展開後的第" not in fd["detail"]


def test_寫不出行號時說明寫是展開後的第幾句_分得出是哪一句():
    fds = _report_for("{{ purge('a') }};\n{{ purge('b') }};\n{{ purge('c') }};\n")
    assert [fd["line"] for fd in fds] == [0, 0, 0]
    assert ["展開後的第 1 句" in fds[0]["detail"], "展開後的第 2 句" in fds[1]["detail"],
            "展開後的第 3 句" in fds[2]["detail"]] == [True, True, True]


def test_對應不確定時不寫第幾句():
    """模型的 finding 只靠數量對應時,不知道剩下的是哪幾句:行號與第幾句都不寫。"""
    hit = {"rule": "R001", "statement": "DELETE FROM a", "statement_index": 2}
    assert pipeline._statement_note(hit, exact=False) == ""
    assert pipeline._statement_note(hit, exact=True) == "(dbt 樣板展開後的第 2 句)"
    assert pipeline._statement_note({**hit, "line": 5}, exact=True) == ""
    assert pipeline._statement_note({**hit, "statement_index": True}, exact=True) == ""


def test_行號對回失敗時記成未完整_審查照常產出(monkeypatch, tmp_path):
    from toolbox import gitlab

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "mr_957.json").write_text(json.dumps({
        "title": "調整", "description": "例行維護", "sha": "e1a5" + "0" * 36,
        "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+DELETE FROM t",
                   "full_content": "DELETE FROM t"}],
    }, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    async def fake_run_agent(cfg, profile, system, user, hub, **kw):
        return json.dumps({"summary": "x", "score": 100, "findings": []}, ensure_ascii=False)

    def boom(*args):
        raise RuntimeError("對回失敗")

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline, "_attach_statement_lines", boom)
    report = asyncio.run(pipeline.review_mr(load_config(), "957"))
    assert any(f.get("stage") == "命中行號" for f in report["findings"])
    assert any(f["title"].startswith("[R001]") for f in report["findings"])


def test_review_mr_開啟_dbt_時_補的_finding_寫原始檔的行號(monkeypatch, tmp_path):
    """整條 review_mr:預掃記下的對應表要一路傳到對回行號那一步。"""
    from toolbox import gitlab

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    src = "select 1 as x;\n\nDELETE FROM {{ ref('y') }};\n"
    (fixtures / "mr_958.json").write_text(json.dumps({
        "title": "調整", "description": "例行維護", "sha": "e1a6" + "0" * 36,
        "files": [{"path": "models/m.sql", "diff": "@@ -1 +1 @@\n+x", "full_content": src}],
    }, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    async def fake_run_agent(cfg, profile, system, user, hub, **kw):
        return json.dumps({"summary": "x", "score": 100, "findings": []}, ensure_ascii=False)

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    cfg = load_config()
    cfg.dbt = dict(CFG)
    report = asyncio.run(pipeline.review_mr(cfg, "958"))
    r001 = [f for f in report["findings"] if f["title"].startswith("[R001]")]
    assert [(f["file"], f["line"]) for f in r001] == [("models/m.sql", 3)]
