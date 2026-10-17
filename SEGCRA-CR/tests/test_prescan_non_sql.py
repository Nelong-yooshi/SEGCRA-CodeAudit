"""預掃只對 .sql 跑 SQL 規則與 lint;其他檔案只留規則層的嚴重命中(不帶原文)。

以前每個變更檔都送進 sqlglot:MR 裡的說明文件、yml 也一樣,只會得到「預掃無法解析此檔的 SQL」
的 major,錯誤訊息還帶著檔案內容,進到 prompt 與留言。現在:
- 說明文件(.md / .markdown / .rst):補一條 info,不影響決策;
- 其他非 SQL 檔(設定檔、程式碼、副檔名不認得的):補一條 major,不得自動放行——這些檔案以前
  因為「無法解析」被擋下,只給 info 的話,改 CI 設定或腳本的 MR 就可能被自動放行;
- 兩種都照樣送進規則層(解析錯誤丟掉不用),只留 major 以上的命中(寫死的憑證最常出現在
  設定檔),而且只留規則代號、嚴重度與固定文字:命中的訊息含比對到的原文(例如整段密碼);
- dbt 專案裡的屬性檔與 dbt_project.yml 不補「非 SQL 檔」,交給設定比對決定。
"""
import asyncio
import json

import pytest

from orchestrator import pipeline, spec_exec
from orchestrator.config import load_config
from orchestrator.pipeline import (_NON_SQL_DOC_TITLE, _NON_SQL_OTHER_TITLE, _PARSE_FAIL_TITLE,
                                   _non_sql_kind, enforce_non_sql, prescan)
from orchestrator.tool_hub import ToolHub
from toolbox import gitlab
from toolbox.sqltools import run_rules

MARKER = "zz_marker_text"
README = f"# 說明\n\n這個 MR 調整 {MARKER} 的篩選條件,依 R-001 處理。\n"
CLEAN_SQL = "SELECT id, amount FROM transactions WHERE amount > 0;"


class _SpyHub:
    """記下送進規則層與 lint 的內容,結果交給真的規則層。"""

    def __init__(self):
        self.calls = []

    async def call_json(self, name, args):
        self.calls.append((name, args.get("sql")))
        if name == "sqltools__run_rules":
            return json.loads(run_rules(args["sql"]))
        return []


def _prescan(files):
    hub = _SpyHub()
    return asyncio.run(prescan(hub, files)), hub


def _file(path, text):
    return {"path": path, "diff": "@@ -0,0 +1 @@\n+x", "full_content": text}


# ─────────────────── 怎麼分類 ───────────────────

@pytest.mark.parametrize("path, kind", [
    ("sql/rules/r001.sql", None), ("a.md.sql", None),
    ("models/MODEL.SQL", "other"), ("q.Sql", "other"),        # .sql 分大小寫(跟 dbt 與接線一樣)
    ("README.md", "doc"), ("docs/Guide.MD", "doc"), ("notes.markdown", "doc"), ("x.rst", "doc"),
    ("a.sql.md", "doc"),
    ("models/schema.yml", "other"), ("dbt_project.yaml", "other"), (".gitlab-ci.yml", "other"),
    ("scripts/run.py", "other"), ("proc/purge.prc", "other"), ("notes.txt", "other"),
    ("Makefile", "other"), ("", "other"), (None, "other"),
])
def test_依副檔名分類_sql分大小寫(path, kind):
    assert _non_sql_kind(path) == kind


@pytest.mark.parametrize("rel, kind", [
    ("models/schema.yml", "dbt"), ("dbt_project.yml", "dbt"), ("snapshots/s.yaml", "dbt"),
    ("packages.yml", "other"), ("selectors.yml", "other"), ("seeds/codes.csv", "other"),
    ("models/notes.md", "doc"),
])
def test_dbt專案裡的屬性檔_另外分類(rel, kind):
    assert _non_sql_kind(rel, rel) == kind


# ─────────────────── 預掃 ───────────────────

def test_說明文件以前會被報成無法解析():
    """這組測試的前提:說明文件送進規則層會解析失敗,錯誤訊息帶著檔案內容。"""
    result = json.loads(run_rules(README))
    assert isinstance(result, dict) and result.get("error")


def test_說明文件不留解析錯誤_不跑lint_不回顯內容():
    pre, hub = _prescan([_file("README.md", README)])
    assert pre == [{"path": "README.md", "non_sql": "doc", "rules": [], "lint": []}]
    assert [name for name, _ in hub.calls] == ["sqltools__run_rules"]
    assert MARKER not in json.dumps(pre, ensure_ascii=False)


def test_說明文件提到規則編號_不會變成檢核點():
    """`R-001` 在 SQL 檔是「要與規格逐項核對」的檢核點;在說明文件裡只是提到。"""
    pre, _ = _prescan([_file("README.md", README)])
    assert not any(h["rule"] == "H003" for h in pre[0]["rules"])


def test_說明文件裡的SQL範例_不當成會執行的SQL():
    """內容剛好整份都是 SQL、解析得過時,語法樹規則會有命中;說明文件不留。"""
    text = "DELETE FROM transactions;\n"
    assert [h["rule"] for h in json.loads(run_rules(text))] == ["R001"]
    pre, _ = _prescan([_file("docs/purge.md", text)])
    assert pre[0]["rules"] == []


@pytest.mark.parametrize("path", ["README.md", "profiles.yml", "scripts/load.py"])
def test_寫死的憑證_非SQL檔照樣抓到(path):
    text = 'target:\n  password: "Abc12345"\n'
    pre, _ = _prescan([_file(path, text)])
    assert [(h["rule"], h["severity"]) for h in pre[0]["rules"]] == [("R004", "blocker")]


def test_其他檔案是會執行的SQL時_語法樹規則的命中照樣留():
    """副檔名不是 .sql 的檔案仍可能是會被執行的 SQL(例如 .prc)。"""
    pre, _ = _prescan([_file("proc/purge.prc", "DELETE FROM transactions;")])
    assert [h["rule"] for h in pre[0]["rules"]] == ["R001"]
    assert pre[0]["non_sql"] == "other" and pre[0]["lint"] == []


def test_其他檔案只留major以上的命中():
    sql = "SELECT * FROM t WHERE x NOT IN (SELECT y FROM u);"     # R002 minor、R003 major
    pre, _ = _prescan([_file("proc/report.prc", sql)])
    assert [h["rule"] for h in pre[0]["rules"]] == ["R003"]


def test_SQL檔照舊():
    pre, hub = _prescan([_file("sql/a.sql", CLEAN_SQL)])
    assert "non_sql" not in pre[0]
    assert [name for name, _ in hub.calls] == ["sqltools__run_rules", "sqltools__lint"]


def test_規則層回傳格式不對時不出錯():
    class _Broken(_SpyHub):
        async def call_json(self, name, args):
            return "壞掉的回傳" if name == "sqltools__run_rules" else []

    pre = asyncio.run(prescan(_Broken(), [_file("a.yml", "x: 1")]))
    assert pre[0]["rules"] == []


# ─────────────────── 補的 finding ───────────────────

def _entry(path, kind, rules=()):
    return {"path": path, "non_sql": kind, "rules": list(rules), "lint": []}


def test_說明文件補info_其他檔案補major():
    report = enforce_non_sql({"findings": []},
                             [_entry("README.md", "doc"), _entry(".gitlab-ci.yml", "other"),
                              {"path": "a.sql", "rules": [], "lint": []}])
    assert [(f["file"], f["severity"], f["title"]) for f in report["findings"]] == [
        ("README.md", "info", _NON_SQL_DOC_TITLE),
        (".gitlab-ci.yml", "major", _NON_SQL_OTHER_TITLE)]


def test_模型輸出同標題的info_蓋不掉major():
    model = {"file": "run.py", "line": 0, "severity": "info", "title": _NON_SQL_OTHER_TITLE,
             "detail": "沒問題", "suggestion": "", "citations": []}
    report = enforce_non_sql({"findings": [model]}, [_entry("run.py", "other")])
    assert [f["severity"] for f in report["findings"]] == ["major"]
    assert report["findings"][0]["detail"] != "沒問題"


def test_每個檔案各一條():
    report = enforce_non_sql({"findings": []}, [_entry("a.md", "doc"), _entry("b.md", "doc")])
    assert [f["file"] for f in report["findings"]] == ["a.md", "b.md"]


# ─────────────────── 整條管線 ───────────────────

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

    async def fake_run_agent(cfg, profile, system, user, hub, **kw):
        return json.dumps({"summary": "x", "findings": []}, ensure_ascii=False)

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)

    def _review(files, dry_run=False):
        mr = {"title": "調整篩選", "description": "例行維護", "sha": "e1a1" + "0" * 36,
              "files": files}
        (fixtures / "mr_900.json").write_text(json.dumps(mr, ensure_ascii=False), encoding="utf-8")
        capture = {}
        report = asyncio.run(pipeline.review_mr(load_config(), "900", dry_run=dry_run,
                                                capture=capture))
        return report, capture

    return _review


def _sql_file():
    return {"path": "sql/maintenance/report.sql", "diff": "@@ -0,0 +1 @@\n+" + CLEAN_SQL,
            "full_content": CLEAN_SQL}


def _titles(report):
    return {(f["file"], f["severity"], f["title"]) for f in report["findings"]}


@pytest.mark.parametrize("dry_run", [False, True])
def test_整條管線_說明文件沒有無法解析的major_也不回顯(review, dry_run):
    readme = {"path": "README.md", "diff": "@@ -0,0 +1 @@\n+" + README, "full_content": README}
    report, capture = review([_sql_file(), readme], dry_run=dry_run)
    assert ("README.md", "info", _NON_SQL_DOC_TITLE) in _titles(report)
    assert not any(f["title"] == _PARSE_FAIL_TITLE for f in report["findings"])
    assert MARKER not in json.dumps(capture["prescan_raw"], ensure_ascii=False)
    assert MARKER not in json.dumps(report["findings"], ensure_ascii=False)


@pytest.mark.parametrize("dry_run", [False, True])
def test_整條管線_CI設定檔的變更不得自動放行(review, dry_run):
    ci = "deploy:\n  script: ./deploy.sh\n"
    report, _ = review([_sql_file(), {"path": ".gitlab-ci.yml", "diff": "@@ -0,0 +1 @@\n+" + ci,
                                      "full_content": ci}], dry_run=dry_run)
    assert (".gitlab-ci.yml", "major", _NON_SQL_OTHER_TITLE) in _titles(report)
    if not dry_run:
        assert report["decision"] != "auto_approved"


# ─────────────────── 命中的原文不可以帶出去 ───────────────────

SECRET = "S3cr3t-P@ss"


@pytest.mark.parametrize("path", ["profiles.yml", "scripts/load.py", "README.md"])
def test_非SQL檔的命中_不帶訊息原文與語句(path):
    pre, _ = _prescan([_file(path, f'target:\n  password: "{SECRET}"\n')])
    assert pre[0]["rules"] and SECRET not in json.dumps(pre, ensure_ascii=False)
    assert all(set(h) <= {"rule", "severity", "message", "line"} for h in pre[0]["rules"])
    report = pipeline.enforce_rules({"findings": []}, pre)
    assert report["findings"] and SECRET not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize("dry_run", [False, True])
def test_整條管線_設定檔裡的密碼不會出現在報告與prompt(review, dry_run):
    secret_file = {"path": "profiles.yml", "diff": f'@@ -0,0 +1 @@\n+password: "{SECRET}"',
                   "full_content": f'password: "{SECRET}"\n'}
    report, capture = review([_sql_file(), secret_file], dry_run=dry_run)
    assert SECRET not in json.dumps(report, ensure_ascii=False)
    assert SECRET not in json.dumps(capture.get("prescan_final", []), ensure_ascii=False)


# ─────────────────── dbt 專案裡的屬性檔 ───────────────────

def test_dbt的屬性檔_不補非SQL檔的major():
    pre, _ = _prescan([_file("models/schema.yml", "version: 2\nmodels:\n  - name: m\n")])
    assert pre[0]["non_sql"] == "dbt"
    assert enforce_non_sql({"findings": []}, pre)["findings"] == []


def test_專案目錄外的yml_照樣是其他檔案():
    pre, _ = _prescan([_file("docs/example/models/schema.yml", "version: 2\n")])
    assert pre[0]["non_sql"] == "other"
