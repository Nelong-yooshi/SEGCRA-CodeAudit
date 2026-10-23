"""程式補的規則 / 檢核點 finding 寫出那句在檔案裡的行號(#28 review 後續)。

review 的建議:補的 finding 不寫語句原文(那是 MR 作者寫的文字,會被貼回留言),改成由程式在
檔案裡找到那句的位置,寫行號——只有數字,沒有回顯的風險。

做法是用預掃同一個 sqlglot 方言把檔案切成語句,token 帶行號,語句文字由同一個函式產生,
所以能和命中的 statement 逐字比對。對不上、或不確定補的是哪一處時,一律維持 0,不猜。
"""
import asyncio
import json

import pytest

from orchestrator import pipeline, spec_exec
from orchestrator.config import load_config
from orchestrator.pipeline import _attach_statement_lines, _rendered_starts, enforce_hints, enforce_rules
from toolbox import gitlab
from toolbox.sqltools import run_rules

PATH = "sql/maintenance/purge.sql"
TWO_DELETES = """-- 清理腳本
DELETE FROM transactions;

/* 第二句 */
DELETE
FROM accounts;"""


def _pre(sql, path=PATH, **extra):
    hits = json.loads(run_rules(sql))
    hits = hits["hits"] if isinstance(hits, dict) else hits
    return [{"path": path, "rules": hits, "lint": [], **extra}]


def _files(sql, path=PATH):
    return [{"path": path, "diff": "@@ -1 +1 @@\n+x", "full_content": sql}]


def _lines(pre):
    return [(h["rule"], h.get("line")) for h in pre[0]["rules"]]


# ─────────────────── 切語句與行號 ───────────────────

def test_每句的行號是程式碼的起始行_不是前面註解的行():
    pre = _pre(TWO_DELETES)
    _attach_statement_lines(pre, _files(TWO_DELETES))
    assert _lines(pre) == [("R001", 2), ("R001", 5)]


def test_相同的語句出現兩次_依序對到第一句與第二句():
    sql = "DELETE FROM transactions;\nSELECT 1;\nDELETE FROM transactions;"
    pre = _pre(sql)
    _attach_statement_lines(pre, _files(sql))
    assert _lines(pre) == [("R001", 1), ("R001", 3)]


def test_同一句命中多條規則_各自對到同一行():
    sql = "SELECT 1;\nSELECT * FROM t WHERE x NOT IN (SELECT y FROM u);"
    pre = _pre(sql)
    _attach_statement_lines(pre, _files(sql))
    assert sorted(_lines(pre)) == [("R002", 2), ("R003", 2)]


def test_檔尾分號之後只有註解也對得上():
    """sqlglot 會把分號之後的註解切成一個「只有註解」的語句;不排除的話數量對不上。"""
    sql = "SELECT 1;\nDELETE FROM transactions;\n-- 以上\n/* 結束 */"
    pre = _pre(sql)
    _attach_statement_lines(pre, _files(sql))
    assert _lines(pre) == [("R001", 2)]


def test_切出來的語句與行號():
    starts = _rendered_starts("SELECT 1;\n\n  DELETE FROM a;;\nSELECT\n 2")
    assert {text: [line for line, *_ in found] for text, found in starts.items()} == {
        "SELECT 1": [1], "DELETE FROM a": [3], "SELECT 2": [4]}


@pytest.mark.parametrize("sql", ["SELECT (", "SELECT 'unterminated", ""])
def test_切不出來時回_None(sql):
    if sql:
        assert _rendered_starts(sql) is None or _rendered_starts(sql) == {}


def test_前200字相同_後面不同的語句分不出來就不寫():
    long_cols = ", ".join(f"col_{i}" for i in range(60))
    sql = (f"SELECT {long_cols} FROM t WHERE a = 1 AND b NOT IN (SELECT y FROM u);\n"
           f"SELECT {long_cols} FROM t WHERE a = 2;")
    pre = _pre(sql)
    assert any(h["rule"] == "R003" for h in pre[0]["rules"])
    _attach_statement_lines(pre, _files(sql))
    assert all(h.get("line") is None for h in pre[0]["rules"])


# ─────────────────── 哪些檔案不算 ───────────────────

@pytest.mark.parametrize("files", [
    [{"path": PATH, "diff": "@@ -1 +1 @@\n+DELETE FROM transactions;"}],          # 真實模式:沒有完整內容
    [{"path": PATH, "full_content": ""}],
    [{"path": "other.sql", "full_content": TWO_DELETES}],                        # 與預掃結果對不上
    ["not a dict"],
], ids=["no-full-content", "empty", "path-mismatch", "not-dict"])
def test_沒有完整內容或對不上時不寫(files):
    pre = _pre(TWO_DELETES)
    _attach_statement_lines(pre, files)
    assert all(h.get("line") is None for h in pre[0]["rules"])


@pytest.mark.parametrize("flag", ["spec_file", "dbt_macro", "dbt_render_error", "dbt_render_notice"])
def test_規格檔與_dbt_處理過的檔案不寫(flag):
    """dbt 展開後的 SQL 行號對不回原始檔(#35);規格檔不跑規則。"""
    pre = _pre(TWO_DELETES, **{flag: True})
    _attach_statement_lines(pre, _files(TWO_DELETES))
    assert all(h.get("line") is None for h in pre[0]["rules"])


def test_dbt_樣板不寫():
    sql = "{{ config(materialized='table') }}\nDELETE FROM transactions;"
    pre = _pre("DELETE FROM transactions;")
    _attach_statement_lines(pre, _files(sql))
    assert all(h.get("line") is None for h in pre[0]["rules"])


def test_檔案太大時不寫(monkeypatch):
    monkeypatch.setattr(pipeline, "MAX_LINE_SOURCE_CHARS", 10)
    pre = _pre(TWO_DELETES)
    _attach_statement_lines(pre, _files(TWO_DELETES))
    assert all(h.get("line") is None for h in pre[0]["rules"])


def test_沒有語句的命中不寫():
    sql = "SELECT password FROM users WHERE password = 'abc123';"
    pre = _pre(sql)
    _attach_statement_lines(pre, _files(sql))
    assert all(h.get("line") is None for h in pre[0]["rules"] if not h.get("statement"))


# ─────────────────── 補的 finding ───────────────────

def _missing(report, before):
    return report["findings"][len(before):]


def _reply(text, severity="blocker"):
    return {"file": PATH, "line": 0, "severity": severity, "title": "資料異動沒有條件",
            "detail": text, "suggestion": ""}


def test_模型一條都沒報_兩條補的都寫行號():
    pre = _pre(TWO_DELETES)
    _attach_statement_lines(pre, _files(TWO_DELETES))
    report = enforce_rules({"findings": []}, pre)
    assert [f["line"] for f in report["findings"]] == [2, 5]


def test_模型引用了第一句_補的第二句寫第二句的行號():
    pre = _pre(TWO_DELETES)
    _attach_statement_lines(pre, _files(TWO_DELETES))
    model = [_reply("`DELETE FROM transactions` 沒有 WHERE")]
    report = enforce_rules({"findings": list(model)}, pre)
    assert [f["line"] for f in _missing(report, model)] == [5]


def test_模型沒引用語句_不知道補的是哪一處時不寫行號():
    """退回比數量:模型那條可能講的是第二句,寫第二句的行號可能是錯的。數量仍然對。"""
    pre = _pre(TWO_DELETES)
    _attach_statement_lines(pre, _files(TWO_DELETES))
    model = [_reply("有一句 DELETE 沒有 WHERE")]
    missing = _missing(enforce_rules({"findings": list(model)}, pre), model)
    assert len(missing) == 1 and missing[0]["line"] == 0


def test_比數量的_finding_沒有抵掉任何一處時仍然確定():
    """模型那條嚴重度不夠(不算數),兩處都補,而且是哪兩處很確定。"""
    pre = _pre(TWO_DELETES)
    _attach_statement_lines(pre, _files(TWO_DELETES))
    model = [_reply("有一句 DELETE 沒有 WHERE", severity="info")]
    assert [f["line"] for f in _missing(enforce_rules({"findings": list(model)}, pre), model)] == [2, 5]


def test_檢核點補的_finding_也寫行號():
    sql = ("SELECT a.owner_id, b.branch_id, COUNT(*) AS n\n"
           "FROM accounts a JOIN branches b ON a.branch_id = b.id GROUP BY a.owner_id, b.branch_id;\n"
           "SELECT a.owner_id, c.region_id, COUNT(*) AS n\n"
           "FROM accounts a JOIN regions c ON a.region_id = c.id GROUP BY a.owner_id, c.region_id;")
    pre = _pre(sql)
    _attach_statement_lines(pre, _files(sql))
    report = enforce_hints({"findings": []}, pre)
    assert [(f["title"][:6], f["line"]) for f in report["findings"]] == [("[H002]", 1), ("[H002]", 3)]


@pytest.mark.parametrize("line", [None, "5", True, 5.0])
def test_行號不是整數時寫_0(line):
    pre = _pre("DELETE FROM transactions;")
    pre[0]["rules"][0]["line"] = line
    assert enforce_rules({"findings": []}, pre)["findings"][0]["line"] == 0


def test_補的內容仍然不回顯語句():
    sql = "DELETE FROM zz_marker_table;\nDELETE FROM zz_marker_two;"
    pre = _pre(sql)
    _attach_statement_lines(pre, _files(sql))
    for f in enforce_rules({"findings": []}, pre)["findings"]:
        assert "zz_marker" not in " ".join(str(v) for v in f.values())


# ─────────────────── 整條管線:prompt 不變、補的 finding 有行號 ───────────────────

@pytest.fixture
def review(monkeypatch, tmp_path):
    fixtures, output = tmp_path / "fixtures", tmp_path / "output"
    fixtures.mkdir()
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    async def fake_generate(cfg, code, text, profile_name=None):
        return {"schema_ddl": [], "conditions": [], "cases": []}, [], []

    monkeypatch.setattr(spec_exec, "generate_cases", fake_generate)
    monkeypatch.setattr(spec_exec, "execute_cases", lambda sql, plan: {
        "engine": "stub", "sandbox_error": None, "testdata_error": None, "sql_error": None,
        "case_results": [], "mismatches": []})

    def _review(model_findings, attach=True):
        mr = {"title": "清理", "description": "例行維護", "sha": "e1a1" + "0" * 36,
              "files": _files(TWO_DELETES)}
        (fixtures / "mr_900.json").write_text(json.dumps(mr, ensure_ascii=False), encoding="utf-8")
        seen = {}

        async def fake_run_agent(cfg, profile, system, user, hub, **kw):
            seen["user"] = user
            return json.dumps({"summary": "x", "findings": model_findings}, ensure_ascii=False)

        monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
        if not attach:
            monkeypatch.setattr(pipeline, "_attach_statement_lines", lambda *args: None)
        report = asyncio.run(pipeline.review_mr(load_config(), "900"))
        return report, seen["user"]

    return _review


def test_整條管線_補的第二句帶行號_prompt_一個字都沒變(review):
    model = [_reply("`DELETE FROM transactions` 沒有 WHERE")]
    with_lines, prompt_a = review(model)
    without, prompt_b = review(model, attach=False)
    assert prompt_a == prompt_b and '"line": 5' not in prompt_a
    added = [f for f in with_lines["findings"] if f["title"].startswith("[R001]")]
    assert [f["line"] for f in added] == [5]
    assert [f["line"] for f in without["findings"] if f["title"].startswith("[R001]")] == [0]
    assert with_lines["decision"] == "blocked"


def test_語句前面有註解時_模型引用程式碼也算引用了那句():
    """預掃的語句文字會帶著前面的註解(sqlglot 寫成 /* 清理腳本 */ DELETE ...);模型引用時不會
    連註解一起抄。以前這樣就認不出是哪一句,只能退回比數量(#28 的情況下補的數量仍然對,
    但是哪一處就不確定了)。"""
    pre = _pre(TWO_DELETES)
    assert pre[0]["rules"][0]["statement"].startswith("/* 清理腳本 */")
    model = [_reply("`DELETE FROM accounts` 沒有 WHERE")]          # 引用的是第二句
    _attach_statement_lines(pre, _files(TWO_DELETES))
    missing = _missing(enforce_rules({"findings": list(model)}, pre), model)
    assert [f["line"] for f in missing] == [2]                       # 補的是第一句


def test_樣板標記只在註解裡時也不寫():
    """dbt 樣板會被展開後才跑規則,展開後的行號對不回原始檔(#35)。標記寫在註解裡時,
    語句文字剛好對得上,但仍不能用原始檔的行號。"""
    for raw, rendered in (("-- {{ var('x') }}\nDELETE FROM transactions;", "DELETE FROM transactions;"),
                          ("SELECT '{{ x }}';\nDELETE FROM transactions;",        # 語句文字對得上的情況
                           "SELECT 'v';\nDELETE FROM transactions;")):
        pre = _pre(rendered)
        assert _rendered_starts(raw) is not None
        _attach_statement_lines(pre, _files(raw))
        assert all(h.get("line") is None for h in pre[0]["rules"]), raw


def test_切出來的語句數量與_token_對不上時不寫(monkeypatch):
    """兩邊數量不同時一一對應會錯位;寧可不寫。"""
    import sqlglot

    real = pipeline._parse
    monkeypatch.setattr(pipeline, "_parse",
                        lambda sql: [sqlglot.parse_one("SELECT 0", dialect="tsql"), *real(sql)])
    assert _rendered_starts(TWO_DELETES) is None
    pre = _pre(TWO_DELETES)
    _attach_statement_lines(pre, _files(TWO_DELETES))
    assert all(h.get("line") is None for h in pre[0]["rules"])
