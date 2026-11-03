"""dbt 設定檔讓 dbt 整個專案解析失敗時報 blocker:YAML 語法錯誤、`models:` 不是清單、MR 刪掉了 dbt_project.yml。

照 dbt-core 1.12 的實測(dbt parse):dbt_project.yml 或屬性檔有 YAML 語法錯誤、屬性檔的 `models:`
不是清單、dbt_project.yml 不存在,整個專案解析失敗;屬性檔描述不存在的 model 只有警告(不報)。
只看 MR 改到的檔;屬性檔在子行程裡解析(待審 repo 的 yml)。
"""
import asyncio
import json

import pytest

from orchestrator import dbt_context, pipeline
from orchestrator.config import load_config
from orchestrator.dbt_context import (PROJECT_FILE_REMOVED, PROJECT_YAML_INVALID, check_yaml_files_isolated,
                                      prepare_dbt_context)
from orchestrator.dbt_project import (YAML_MODELS_NOT_LIST, YAML_SYNTAX, DbtProjectError,
                                      DbtProjectSyntaxError, load_dbt_project, read_layout,
                                      yaml_file_problems)
from tests.test_dbt_affected import CFG, HEAD, _gz, _Loader
# 每題清掉取回專案的快取(同一個 commit 編號)、專案資訊改在同行程整理
from tests.test_dbt_affected import _fresh_cache, _summarize_in_process  # noqa: F401

BAD_YAML = "version: 2\nmodels:\n  - name: ok\n   description: [x\n"
GOOD_YML = "version: 2\nmodels:\n  - name: mrt_A\n    description: x\n"


# ─────────────────── 屬性檔的檢查 ───────────────────

def test_YAML_語法錯誤記下行號():
    assert yaml_file_problems({"models/a.yml": BAD_YAML}) == [
        {"path": "models/a.yml", "kind": YAML_SYNTAX, "line": 4}]


@pytest.mark.parametrize("text", ["version: 2\nmodels: abc\n", "version: 2\nmodels:\n  name: x\n"])
def test_models_不是清單(text):
    assert yaml_file_problems({"models/a.yml": text}) == [
        {"path": "models/a.yml", "kind": YAML_MODELS_NOT_LIST, "line": 0}]


@pytest.mark.parametrize("text", [
    GOOD_YML,
    "",                                         # 空檔
    "version: 2\nmodels:\n",                    # models 沒有值
    "version: 2\nsources:\n  - name: raw\n",
    "version: 2\nmodels:\n  - name: no_such_model\n",   # 描述不存在的 model:dbt 只有警告
    "- a\n- b\n",                               # 最上層不是物件:不在這裡判斷
])
def test_dbt_解析得過或不在範圍內的不報(text):
    assert yaml_file_problems({"models/a.yml": text}) == []


def test_太大的檔不檢查(monkeypatch):
    from orchestrator import dbt_project
    monkeypatch.setattr(dbt_project, "MAX_SNAPSHOT_YML_CHARS", 10)
    assert yaml_file_problems({"models/a.yml": BAD_YAML}) == []


def test_依路徑排序():
    got = yaml_file_problems({"models/b.yml": BAD_YAML, "models/a.yml": "models: x\n"})
    assert [g["path"] for g in got] == ["models/a.yml", "models/b.yml"]


def test_子行程裡檢查的結果跟同行程一樣():
    texts = {"models/a.yml": BAD_YAML, "models/b.yml": GOOD_YML, "models/c.yml": "models: x\n"}
    assert check_yaml_files_isolated(texts) == yaml_file_problems(texts)


@pytest.mark.parametrize("returned", [None, "x", [1], {"a": 1}])
def test_子行程失敗或回傳不對時是沒檢查完(monkeypatch, returned):
    monkeypatch.setattr(dbt_context, "run_isolated", lambda *a, **k: returned)
    assert check_yaml_files_isolated({"models/a.yml": GOOD_YML}) is None


# ─────────────────── dbt_project.yml 不是合法的 YAML ───────────────────

def test_dbt_project_yml_語法錯誤是語法錯誤的那一類():
    with pytest.raises(DbtProjectSyntaxError):
        read_layout("name: x\nmodel-paths: [models\n")
    with pytest.raises(DbtProjectError) as other:
        read_layout("- a\n")                     # 最上層不是物件:dbt 不一定失敗,不是這一類
    assert not isinstance(other.value, DbtProjectSyntaxError)


def _load(files):
    return load_dbt_project(HEAD, "", download=lambda sha, path, *, max_bytes: _gz(files, path))


def test_取回專案時分得出語法錯誤():
    bad = _load({"dbt_project.yml": "name: x\nmodel-paths: [models\n", "models/a.sql": "select 1\n"})
    assert not bad.ok and bad.yaml_syntax_error
    other = _load({"dbt_project.yml": "- a\n", "models/a.sql": "select 1\n"})
    assert not other.ok and not other.yaml_syntax_error
    missing = _load({"models/a.sql": "select 1\n"})
    assert not missing.ok and not missing.yaml_syntax_error


def _ctx(files, mr_files, read_file=None):
    mr = {"sha": HEAD, "head_sha": HEAD, "base_sha": HEAD, "truncated": False, "files": mr_files}
    return mr, prepare_dbt_context(mr, CFG, real_mode=True, load=_Loader(files), read_file=read_file)


BROKEN_PROJECT = {"dbt_project.yml": "name: x\nmodel-paths: [models\n", "models/a.sql": "select 1\n"}


def test_dbt_project_yml_語法錯誤時記下原因():
    mr, ctx = _ctx(BROKEN_PROJECT, [{"path": "dbt_project.yml", "diff": "@@ -1 +1 @@\n+x"}])
    assert ctx.error and ctx.broken == PROJECT_YAML_INVALID


def test_其他取不回的原因不記():
    mr, ctx = _ctx({"dbt_project.yml": "- a\n", "models/a.sql": "select 1\n"},
                   [{"path": "models/a.sql", "diff": "@@ -1 +1 @@\n+x"}])
    assert ctx.error and ctx.broken is None


@pytest.mark.parametrize("change", [
    {"path": "dbt_project.yml", "deleted": True, "diff": ""},
    {"path": "old/dbt_project.yml", "old_path": "dbt_project.yml", "diff": ""},
])
def test_MR_刪掉或移走了_dbt_project_yml(change):
    mr, ctx = _ctx({"models/a.sql": "select 1\n"}, [change, {"path": "models/a.sql", "diff": "@@ -1 +1 @@\n+x"}],
                   read_file=lambda sha, path, *, max_bytes: None)
    assert ctx.broken == PROJECT_FILE_REMOVED and "刪掉或移走" in ctx.error


def test_讀不到但不是_MR_刪的_照舊寫專案目錄或權限():
    mr, ctx = _ctx({"models/a.sql": "select 1\n"}, [{"path": "models/a.sql", "diff": "@@ -1 +1 @@\n+x"}],
                   read_file=lambda sha, path, *, max_bytes: None)
    assert ctx.broken is None and "唯讀 token" in ctx.error


# ─────────────────── 管線:MR 改到的設定檔 ───────────────────

@pytest.fixture
def in_process(monkeypatch):
    monkeypatch.setattr(pipeline, "check_yaml_files_isolated", yaml_file_problems)


# dbt_project.yml 的 models: 是設定(物件),不是屬性檔的 models 清單,不可以送去檢查;a.sql 剛好不是
# 合法的 YAML,不是屬性檔也不可以送去檢查
PROJECT = {"dbt_project.yml": "name: sample\nmodels:\n  sample:\n    +materialized: view\n",
           "models/a.sql": "select a: [1\n", "models/schema.yml": GOOD_YML}


def test_dbt_project_yml_語法錯誤而且是_MR_改的_要報():
    mr, ctx = _ctx(BROKEN_PROJECT, [{"path": "dbt_project.yml", "diff": "@@ -1 +1 @@\n+x"}])
    assert pipeline._dbt_yaml_issues(mr, ctx) == [
        {"path": "dbt_project.yml", "kind": PROJECT_YAML_INVALID, "line": 0}]


def test_dbt_project_yml_本來就壞但這次沒改_不報():
    mr, ctx = _ctx(BROKEN_PROJECT, [{"path": "models/a.sql", "diff": "@@ -1 +1 @@\n+x"}])
    assert ctx.broken == PROJECT_YAML_INVALID
    assert pipeline._dbt_yaml_issues(mr, ctx) == []


def test_刪掉_dbt_project_yml_要報():
    mr, ctx = _ctx({"models/a.sql": "select 1\n"},
                   [{"path": "dbt_project.yml", "deleted": True, "diff": ""}],
                   read_file=lambda sha, path, *, max_bytes: None)
    assert pipeline._dbt_yaml_issues(mr, ctx) == [
        {"path": "dbt_project.yml", "kind": PROJECT_FILE_REMOVED, "line": 0}]


def test_改到的屬性檔有語法錯誤(in_process):
    mr, ctx = _ctx({**PROJECT, "models/schema.yml": BAD_YAML},
                   [{"path": "models/schema.yml", "diff": "@@ -1 +1 @@\n+x"}])
    assert pipeline._dbt_yaml_issues(mr, ctx) == [
        {"path": "models/schema.yml", "kind": YAML_SYNTAX, "line": 4}]


def test_改到的屬性檔_models_不是清單(in_process):
    mr, ctx = _ctx({**PROJECT, "models/schema.yml": "version: 2\nmodels: abc\n"},
                   [{"path": "models/schema.yml", "diff": "@@ -1 +1 @@\n+x"}])
    assert [i["kind"] for i in pipeline._dbt_yaml_issues(mr, ctx)] == [YAML_MODELS_NOT_LIST]


def test_沒改到的屬性檔壞掉不報(in_process):
    mr, ctx = _ctx({**PROJECT, "models/other.yml": BAD_YAML},
                   [{"path": "models/schema.yml", "diff": "@@ -1 +1 @@\n+x"}])
    assert pipeline._dbt_yaml_issues(mr, ctx) == []


@pytest.mark.parametrize("change", [
    {"path": "models/schema.yml", "deleted": True, "diff": ""},     # 刪掉的檔不檢查
    {"path": "dbt_project.yml", "diff": "@@ -1 +1 @@\n+x"},         # 合法的 dbt_project.yml
    {"path": "models/a.sql", "diff": "@@ -1 +1 @@\n+x"},            # 不是屬性檔
])
def test_其他改到的檔不檢查(in_process, change):
    mr, ctx = _ctx({**PROJECT, "models/schema.yml": BAD_YAML}, [change])
    assert pipeline._dbt_yaml_issues(mr, ctx) == []


def test_沒有專案時不檢查():
    assert pipeline._dbt_yaml_issues({"files": []}, None) == []


def test_屬性檔沒檢查完時記成無法確認(monkeypatch):
    monkeypatch.setattr(pipeline, "check_yaml_files_isolated", lambda texts: None)
    mr, ctx = _ctx(PROJECT, [{"path": "models/schema.yml", "diff": "@@ -1 +1 @@\n+x"}])
    assert pipeline._dbt_yaml_issues(mr, ctx) == [
        {"path": "models/schema.yml", "kind": pipeline._YAML_CHECK_FAILED, "line": 0}]


def test_屬性檔交給子行程的是_MR_的路徑與_head_的內容(monkeypatch):
    seen = []
    monkeypatch.setattr(pipeline, "check_yaml_files_isolated", lambda texts: seen.append(texts) or [])
    mr, ctx = _ctx(PROJECT, [{"path": "models/schema.yml", "diff": "@@ -1 +1 @@\n+x"}])
    pipeline._dbt_yaml_issues(mr, ctx)
    assert seen == [{"models/schema.yml": GOOD_YML}]


# ─────────────────── finding ───────────────────

@pytest.mark.parametrize("kind", [PROJECT_YAML_INVALID, PROJECT_FILE_REMOVED, YAML_SYNTAX, YAML_MODELS_NOT_LIST])
def test_整個專案會失敗的補_blocker(kind):
    report = pipeline.enforce_dbt_project_broken(
        {"findings": []}, [{"path": "models/schema.yml", "kind": kind, "line": 4}])
    [fd] = report["findings"]
    assert fd["severity"] == "blocker" and fd["title"] == pipeline._PROJECT_BROKEN_TITLE
    assert fd["file"] == "models/schema.yml" and fd["line"] == 4 and "第 4 行附近" in fd["detail"]
    assert pipeline._YAML_PROBLEM_TEXT[kind] in fd["detail"]


def test_沒檢查完的補_major():
    report = pipeline.enforce_dbt_project_broken(
        {"findings": []}, [{"path": "models/schema.yml", "kind": pipeline._YAML_CHECK_FAILED, "line": 0}])
    [fd] = report["findings"]
    assert fd["severity"] == "major" and fd["title"] == pipeline._YAML_UNCHECKED_TITLE


@pytest.mark.parametrize("issue", [{"path": "a.yml", "kind": "unknown"}, {"kind": YAML_SYNTAX}, "x", None])
def test_形狀不對的不補(issue):
    assert pipeline.enforce_dbt_project_broken({"findings": []}, [issue]) == {"findings": []}


@pytest.mark.parametrize("line", [0, -1, True, "3", None])
def test_行號不對時不寫行號(line):
    [fd] = pipeline.enforce_dbt_project_broken(
        {"findings": []}, [{"path": "a.yml", "kind": YAML_SYNTAX, "line": line}])["findings"]
    assert fd["line"] == 0 and "行附近" not in fd["detail"]


def test_不是清單時不出錯():
    assert pipeline.enforce_dbt_project_broken({"findings": []}, None) == {"findings": []}


# ─────────────────── 走整條 review_mr ───────────────────

def _review(monkeypatch, tmp_path, issues, *, dry_run=False, boom=False):
    from toolbox import gitlab

    fixtures, output = tmp_path / "fixtures", tmp_path / "out"
    fixtures.mkdir()
    output.mkdir()
    (fixtures / "mr_962.json").write_text(json.dumps({
        "title": "調整", "description": "例行維護", "sha": "e1aa" + "0" * 36,
        "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+SELECT 1 AS id",
                   "full_content": "SELECT 1 AS id"}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    async def fake_run_agent(cfg, profile, system, user, hub, **kw):
        return json.dumps({"summary": "x", "score": 100, "findings": []}, ensure_ascii=False)

    def fake_issues(mr, dbt_ctx):
        if boom:
            raise RuntimeError("壞了")
        return issues
    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline, "_dbt_yaml_issues", fake_issues)
    return asyncio.run(pipeline.review_mr(load_config(), "962", dry_run=dry_run))


BROKEN = [{"path": "models/schema.yml", "kind": YAML_SYNTAX, "line": 4}]


def _blockers(report):
    return [f["severity"] for f in report["findings"] if f["title"] == pipeline._PROJECT_BROKEN_TITLE]


def test_整條_review_mr_補_blocker_決策是擋下(monkeypatch, tmp_path):
    report = _review(monkeypatch, tmp_path, BROKEN)
    assert _blockers(report) == ["blocker"] and report["decision"] == "blocked"


def test_整條_review_mr_沒有問題時不補(monkeypatch, tmp_path):
    assert _blockers(_review(monkeypatch, tmp_path, [])) == []


def test_dry_run_也補(monkeypatch, tmp_path):
    assert _blockers(_review(monkeypatch, tmp_path, BROKEN, dry_run=True)) == ["blocker"]


def test_檢查這一步出錯時記成未完整_審查照常產出(monkeypatch, tmp_path):
    report = _review(monkeypatch, tmp_path, BROKEN, boom=True)
    assert any(f.get("stage") == "dbt 設定檔檢查" for f in report["findings"])
