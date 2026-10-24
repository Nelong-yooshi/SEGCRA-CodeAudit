"""會變成 Dagster 名稱的 dbt 名稱與設定,不符合 Dagster 的命名規則時補 blocker。

部署端用 Dagster(1.13)與 dagster-dbt(0.29)載入 dbt 專案,不符合時 Dagster 整個載入失敗。
規則分三種(資產名稱、group、資料檢查的名稱),設定可以寫在 config()、屬性檔、dbt_project.yml
(見 orchestrator/dbt_asset_names.py)。下面的情境照 Dagster 1.13 實測的結果:每一種位置都有
會擋的與不會擋的。
"""
import asyncio
import json

import pytest

from orchestrator import pipeline
from orchestrator.dbt_asset_names import (Layout, check_file, valid_asset_name, valid_name,
                                          versioned_models)
from orchestrator.dbt_context import DbtContext
from orchestrator.dbt_project import DbtLayout, DbtProject
from orchestrator.pipeline import (_DBT_ASSET_NAME_TITLE, _DBT_ASSET_NAME_UNKNOWN_TITLE,
                                   enforce_dbt_asset_names)


def _added(path, content="select 1"):
    lines = content.split("\n")
    diff = f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{ln}\n" for ln in lines)
    return {"path": path, "diff": diff, "full_content": content}


def _deleted(path):
    return {"path": path, "diff": "@@ -1,1 +0,0 @@\n-select 1\n"}


def _run(*files, ctx=None):
    return enforce_dbt_asset_names({"findings": []}, {"files": list(files)}, ctx)["findings"]


def _titles(findings):
    return [(f["file"], f["severity"], f["title"]) for f in findings]


SOURCES = """version: 2
sources:
  - name: {source}
    tables:
      - name: {table}
"""


# ─────────────────── 會擋的(Dagster 載入失敗,或 dbt parse 就失敗) ───────────────────

@pytest.mark.parametrize("path", [
    "models/每日交易.sql",            # 中文
    "models/mrt_每日.sql",            # 中英混合
    "models/mrt（daily）.sql",        # 全形括號
    "models/mrt@daily.sql",
    "models/mrt+daily.sql",
    "models/mrt#daily.sql",
    "models/mrt(daily).sql",
    "models/mrt daily.sql",           # 空白:dbt parse 就失敗
    "models/py/每日.py",              # Python model
    "seeds/分行.csv",
    "seeds/branch list.csv",
])
def test_model_seed_檔名不符合_補blocker(path):
    assert _titles(_run(_added(path))) == [(path, "blocker", _DBT_ASSET_NAME_TITLE)]


@pytest.mark.parametrize("source,table", [("交易來源", "txn"), ("core", "交易明細"),
                                          ("core src", "txn")])
def test_sources_的名稱或表名不符合_補blocker(source, table):
    f = _added("models/src/sources.yml", SOURCES.format(source=source, table=table))
    assert _titles(_run(f)) == [("models/src/sources.yml", "blocker", _DBT_ASSET_NAME_TITLE)]


def test_snapshot_區塊名稱用中文_補blocker():
    f = _added("snapshots/snap_branch.sql",
               "{% snapshot 分行快照 %}\nselect 1\n{% endsnapshot %}")
    assert _titles(_run(f)) == [("snapshots/snap_branch.sql", "blocker", _DBT_ASSET_NAME_TITLE)]


def test_屬性檔的_snapshots_名稱也查():
    f = _added("snapshots/props.yml", "version: 2\nsnapshots:\n  - name: 分行快照\n")
    assert _titles(_run(f)) == [("snapshots/props.yml", "blocker", _DBT_ASSET_NAME_TITLE)]


# ─────────────────── 不會擋的 ───────────────────

@pytest.mark.parametrize("f", [
    _added("models/中文目錄/mrt_daily.sql"),                     # 目錄名稱
    _added("snapshots/分行快照.sql",                              # snapshot 檔名
           "{% snapshot snap_branch %}\nselect 1\n{% endsnapshot %}"),
    _added("macros/中文.sql", "{% macro 計算() %}1{% endmacro %}"),  # macro 檔名與名稱
    _added("models/mrt_daily.sql", "select 1 as 金額"),            # 欄位
    _added("models/props.yml", "version: 2\nmodels:\n  - name: mrt_daily\n"
                               "    description: 每日交易\n    columns:\n      - name: 金額\n"
                               "        tests: [not_null]\n"),     # 欄位、測試、說明
    _added("docs/說明.md", "# 中文"),                              # dbt 以外的檔案
    _added("models/mrt-daily.sql"),                               # 連字號
    _added("models/mrt.daily.sql"),                               # 點
    _added("models/MRT_Daily.sql"),                               # 大寫
    _added("models/1st_daily.sql"),                               # 數字開頭
    _added("seeds/branch-list.csv"),
    _added("models/sources.yml", SOURCES.format(source="core_src", table="txn-log")),
], ids=lambda f: f["path"])
def test_不受影響的名稱不報(f):
    assert _run(f) == []


def test_刪除的檔案不檢查():
    assert _run(_deleted("models/每日交易.sql")) == []


def test_改名成合規的檔名不報_改名成不合規的報():
    good = {**_added("models/mrt_daily.sql"), "old_path": "models/每日.sql"}
    assert _run(good) == []
    bad = {**_added("models/每日.sql"), "old_path": "models/mrt_daily.sql"}
    assert _titles(_run(bad)) == [("models/每日.sql", "blocker", _DBT_ASSET_NAME_TITLE)]


def test_只改內容沒改名_檔名仍不合規也報():
    """已經在 main 上的不合規名稱,部署端本來就載入失敗;改到它時照樣標出來。"""
    f = {"path": "models/每日.sql", "diff": "@@ -1,1 +1,1 @@\n-select 1\n+select 2\n",
         "full_content": "select 2"}
    assert _titles(_run(f)) == [("models/每日.sql", "blocker", _DBT_ASSET_NAME_TITLE)]


# ─────────────────── 目錄設定 ───────────────────

def _ctx(files=None):
    project = DbtProject(ok=True, files=files or {}, model_paths=("transform",),
                         macro_paths=("macros",), seed_paths=("data",),
                         snapshot_paths=("snaps",))
    return DbtContext(prefix="dbt", project=project,
                      layout=DbtLayout(("transform", "macros", "data", "snaps")))


def test_照專案的目錄設定判斷():
    ctx = _ctx()
    assert _titles(_run(_added("dbt/transform/每日.sql"), ctx=ctx)) == [
        ("dbt/transform/每日.sql", "blocker", _DBT_ASSET_NAME_TITLE)]
    assert _titles(_run(_added("dbt/data/分行.csv"), ctx=ctx)) == [
        ("dbt/data/分行.csv", "blocker", _DBT_ASSET_NAME_TITLE)]
    # 預設目錄名稱在這個專案裡不是 model / seed 目錄
    assert _run(_added("dbt/models/每日.sql"), _added("dbt/seeds/分行.csv"), ctx=ctx) == []
    # 專案目錄外的檔案不是 dbt 的檔案
    assert _run(_added("other/transform/每日.sql"), ctx=ctx) == []


def test_屬性檔用取回專案的內容():
    """取回的專案裡有 head 的完整內容時以它為準(變更清單的內容可能只有片段)。"""
    ctx = _ctx({"transform/src.yml": SOURCES.format(source="交易", table="txn")})
    f = {"path": "dbt/transform/src.yml", "diff": "@@ -1 +1 @@\n-x\n+y\n"}
    assert _titles(_run(f, ctx=ctx)) == [("dbt/transform/src.yml", "blocker",
                                          _DBT_ASSET_NAME_TITLE)]


# ─────────────────── 看不出名稱:major ───────────────────

@pytest.mark.parametrize("f", [
    {"path": "models/src.yml", "diff": "@@ -1 +1 @@\n-x\n+y\n"},                # 拿不到內容
    _added("models/src.yml", "sources: [\n"),                                   # 解析不了
    _added("models/src.yml", "sources:\n  - tables: []\n"),                     # 沒寫名稱
    _added("models/src.yml", "sources:\n  - name: \"{{ var('s') }}\"\n"),       # 名稱是樣板
    _added("models/src.yml", "sources: core\n"),                                # 形狀不對
    _added("snapshots/s.sql", "{% snapshot {{ x }} %}select 1{% endsnapshot %}"),
    _added("snapshots/s.sql", "{%snapshot%}select 1{% endsnapshot %}"),
], ids=lambda f: f["diff"][-20:])
def test_看不出名稱時補major(f):
    assert _titles(_run(f)) == [(f["path"], "major", _DBT_ASSET_NAME_UNKNOWN_TITLE)]


def test_內容太長不解析():
    from orchestrator.dbt_config_change import MAX_TEXT_CHARS
    f = _added("models/src.yml", "#" * (MAX_TEXT_CHARS + 1))
    assert _titles(_run(f)) == [("models/src.yml", "major", _DBT_ASSET_NAME_UNKNOWN_TITLE)]


def test_別名展開成大量項目時不解析():
    """`*t` 讓同一份表清單被每個 source 引用:內容很短,逐項走訪卻是 300 × 300 次。"""
    import time
    tables = "".join(f"    - name: t{i}\n" for i in range(300))
    yml = "x: &t\n" + tables + "sources:\n" + "".join(
        f"  - name: s{i}\n    tables: *t\n" for i in range(300))
    t0 = time.monotonic()
    found = _run(_added("models/src.yml", yml))
    assert time.monotonic() - t0 < 5
    assert _titles(found) == [("models/src.yml", "major", _DBT_ASSET_NAME_UNKNOWN_TITLE)]
    assert "50000" in found[0]["detail"]


def test_空的屬性檔不報():
    assert _run(_added("models/src.yml", "")) == []


# ─────────────────── 報告的內容 ───────────────────

def test_列出名稱與種類_用_code_格式():
    f = _added("models/src.yml", SOURCES.format(source="交易來源", table="明細"))
    detail = _run(f)[0]["detail"]
    assert "source `交易來源`" in detail and "source 的表 `明細`" in detail
    assert "資產名稱只能用英數字、底線、`-`、`.`" in detail
    assert "group 只能用" not in detail                    # 只寫這個檔用到的規則


def test_名稱去掉不可列印字元並截長度():
    name = "交易\x1b[31m" + "x" * 500
    f = _added("models/src.yml", SOURCES.format(source=json.dumps(name), table="t"))
    detail = _run(f)[0]["detail"]
    assert "\x1b" not in detail and "x" * 300 not in detail


def test_名稱含反引號不會跳出_code_格式():
    f = _added("models/src.yml", SOURCES.format(source='"a`b 交易"', table="t"))
    assert "``a`b 交易``" in _run(f)[0]["detail"]


def test_一個檔案最多列出固定數量的名稱():
    n = pipeline.MAX_LISTED_ASSET_NAMES + 5
    yml = "sources:\n" + "".join(f"  - name: 來源{i}\n" for i in range(n))
    detail = _run(_added("models/src.yml", yml))[0]["detail"]
    assert f"來源{n - 1}" not in detail and f"等 {n} 個" in detail


def test_模型自己輸出同標題的_info_關不掉這條():
    report = {"findings": [{"file": "models/每日.sql", "line": 0, "severity": "info",
                            "title": _DBT_ASSET_NAME_TITLE, "detail": "已確認沒問題",
                            "suggestion": "", "citations": []}]}
    out = enforce_dbt_asset_names(report, {"files": [_added("models/每日.sql")]})
    assert [f["severity"] for f in out["findings"]] == ["blocker"]


def test_不看_dbt_開關_只看位置():
    """檢查的是名稱,不需要展開:dbt 沒開時一樣檢查 dbt 預設目錄裡的檔案。"""
    assert _run(_added("models/每日.sql")) != []
    out = enforce_dbt_asset_names({"findings": []}, {"files": [_added("models/每日.sql")]},
                                  None, {"enabled": False})
    assert out["findings"]


def test_變更清單的壞項目不中斷():
    out = enforce_dbt_asset_names({"findings": []}, {"files": [None, {"path": 3}, "x"]})
    assert out["findings"] == []


def test_valid_asset_name():
    assert valid_asset_name("mrt_daily-v2.final")
    assert not valid_asset_name("") and not valid_asset_name(None)
    assert not valid_asset_name("每日") and not valid_asset_name("a b")


def test_check_file_不是要檢查的檔案回_None():
    assert check_file("x", "macros/a.sql", Layout(), "") is None
    assert check_file("x", "models/a.md", Layout(), "") is None


# ─────────────────── 接進審查流程 ───────────────────

def test_決策是_blocked():
    report = enforce_dbt_asset_names({"summary": "x", "score": 100, "findings": []},
                                     {"files": [_added("models/每日.sql")]})
    mr = {"files": [_added("models/每日.sql")]}
    out = pipeline.apply_policy(pipeline.apply_rubric(report), mr,
                                {"auto_approve": {"max_diff_lines": 30, "min_score": 0,
                                                  "allowed_severities": ["info"],
                                                  "forbid_pending_hints": True}})
    assert out["decision"] == "blocked", out.get("decision")


def test_dry_run_也會補():
    class _Hub:
        async def call_json(self, name, args):
            return {}

        async def call(self, name, args):
            return ""

    report = asyncio.run(pipeline._dry_run_report(
        _Hub(), "900", {"files": [_added("models/每日.sql")]}, [], None, None))
    assert any(f["title"] == _DBT_ASSET_NAME_TITLE for f in report["findings"])


def test_review_mr_接上了(monkeypatch, tmp_path):
    from orchestrator.config import load_config
    from toolbox import gitlab

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "mr_952.json").write_text(json.dumps({
        "title": "新增每日交易 model", "description": "例行維護", "sha": "e1a3" + "0" * 36,
        "files": [_added("models/每日交易.sql", "select 1 as id")],
    }, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    async def fake_run_agent(cfg, profile, system, user, hub, verbose=True,
                             use_tools=False, trace=None, meta=None):
        return json.dumps({"summary": "沒問題", "score": 100, "findings": []}, ensure_ascii=False)

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    report = asyncio.run(pipeline.review_mr(load_config(), "952"))
    assert any(f["title"] == _DBT_ASSET_NAME_TITLE and f["severity"] == "blocker"
               for f in report["findings"]), report["findings"]
    assert report["decision"] == "blocked", report["decision"]


# ─────────────────── 三種規則與三個來源(Dagster 1.13 + dagster-dbt 0.29 實測的對照) ───────────────────

def _proj_ctx(files=None, root=None, versioned_yml=None):
    """專案在 repo 根目錄、預設目錄。versioned_yml:放進 models/ 的屬性檔(宣告帶版本的 model)。"""
    files = dict(files or {})
    if versioned_yml is not None:
        files["models/versions.yml"] = versioned_yml
    project = DbtProject(ok=True, files=files, model_paths=("models",), macro_paths=("macros",),
                         seed_paths=("seeds",), snapshot_paths=("snapshots",),
                         test_paths=("tests",),
                         root_files={"dbt_project.yml": "name: p\n", **(root or {})})
    return DbtContext(prefix="", project=project,
                      layout=DbtLayout(("models", "macros", "seeds", "snapshots", "tests")))


NO_VERSIONS = "version: 2\nmodels:\n  - name: other\n"
VERSIONED = "version: 2\nmodels:\n  - name: dim\n    versions:\n      - v: 1\n      - v: 2\n"


def _sev(*files, ctx=None):
    return [(f["file"], f["severity"]) for f in _run(*files, ctx=ctx)]


@pytest.mark.parametrize("rule,name,ok", [
    ("asset", "每日", False), ("asset", "a-b", True), ("asset", "a.b", True),
    ("asset", "a/b", False), ("asset", "a b", False),
    ("group", "財務", False), ("group", "fin-ops", False), ("group", "fin.ops", False),
    ("group", "fin/ops", True), ("group", "fin/ops/x_1", True), ("group", "fin/", False),
    ("group", "/fin", False), ("group", "fin//ops", False),
    ("check", "唯一", False), ("check", "uniq-1", False), ("check", "uniq.1", True),
    ("check", "uniq_1", True), ("check", "a/b", False),
])
def test_三種規則各自的字元限制(rule, name, ok):
    assert valid_name(rule, name) is ok


# 資產名稱:schema 設定(三個來源)、meta.dagster.asset_key
@pytest.mark.parametrize("f", [
    _added("models/m.sql", "{{ config(schema='報表') }}\nselect 1"),
    _added("models/m.sql", "{{ config(materialized='table', schema=\"報表\") }}\nselect 1"),
    _added("models/m.sql", "{{ config({'schema': '報表'}) }}\nselect 1"),
    _added("models/m.py", "def model(dbt, session):\n    dbt.config(schema='報表')\n    return 1\n"),
    _added("models/props.yml", "models:\n  - name: m\n    config:\n      schema: 報表\n"),
    _added("seeds/props.yml", "seeds:\n  - name: s\n    config:\n      schema: 報表\n"),
    _added("snapshots/s.sql", "{% snapshot snap %}\n{{ config(schema='報表') }}\nselect 1\n"
                               "{% endsnapshot %}"),
    _added("models/m.sql", "{{ config(meta={'dagster': {'asset_key': ['報表', 'm']}}) }}\nselect 1"),
    _added("models/props.yml", "models:\n  - name: m\n    meta:\n      dagster:\n"
                               "        asset_key: [報表, m]\n"),
    _added("models/props.yml", "models:\n  - name: m\n    config:\n      meta:\n        dagster:\n"
                               "          asset_key: [報表]\n"),
    _added("models/src.yml", "sources:\n  - name: core\n    tables:\n      - name: t\n"
                             "        meta:\n          dagster:\n            asset_key: [來源, t]\n"),
], ids=lambda f: f["path"] + ":" + f["full_content"][:30])
def test_資產名稱的設定不符合_補blocker(f):
    assert _sev(f) == [(f["path"], "blocker")]


@pytest.mark.parametrize("yml", [
    "models:\n  my_proj:\n    +schema: 報表\n",
    "models:\n  my_proj:\n    marts:\n      +schema: 報表\n",          # 設在子資料夾
    "models:\n  my_proj:\n    schema: 報表\n",                          # 沒加 +
    "seeds:\n  my_proj:\n    +schema: 報表\n",
    "snapshots:\n  my_proj:\n    +schema: 報表\n",
    "models:\n  my_proj:\n    +group: 財務\n",
    "models:\n  my_proj:\n    +meta:\n      dagster:\n        group: fin-ops\n",
    "models:\n  my_proj:\n    +meta:\n      dagster:\n        asset_key: [中文]\n",
])
def test_dbt_project_yml_改到時也查(yml):
    f = _added("dbt_project.yml", "name: my_proj\n" + yml)
    assert _sev(f) == [("dbt_project.yml", "blocker")]
    # 取回專案時,以專案根目錄的設定檔為準
    g = {"path": "dbt_project.yml", "diff": "@@ -1 +1 @@\n-x\n+y\n"}
    assert _sev(g, ctx=_proj_ctx(root={"dbt_project.yml": "name: my_proj\n" + yml})) == [
        ("dbt_project.yml", "blocker")]


@pytest.mark.parametrize("yml", [
    "name: p\nmodels:\n  my_proj:\n    +schema: mart-daily\n    +materialized: table\n",
    "name: p\nmodels:\n  my_proj:\n    +group: fin/ops\n    +tags: [每日]\n",
    "name: p\nmodels:\n  my_proj:\n    schema:\n      +schema: ok_schema\n",   # 名叫 schema 的資料夾
    "name: p\nvars:\n  分行: 1\n",
    "name: p\n",
])
def test_dbt_project_yml_沒有問題不報(yml):
    assert _run(_added("dbt_project.yml", yml)) == []
    ctx = _proj_ctx(root={"dbt_project.yml": yml}, versioned_yml=NO_VERSIONS)
    assert _run({"path": "dbt_project.yml", "diff": "@@ -1 +1 @@\n-x\n+y\n"}, ctx=ctx) == []


# group
@pytest.mark.parametrize("f", [
    _added("models/groups.yml", "groups:\n  - name: 財務\n    owner:\n      name: 王小明\n"),
    _added("models/groups.yml", "groups:\n  - name: fin-ops\n    owner:\n      email: a@b.c\n"),
    _added("models/groups.yml", "groups:\n  - name: fin.ops\n"),
    _added("models/m.sql", "{{ config(group='財務') }}\nselect 1"),
    _added("models/props.yml", "models:\n  - name: m\n    config:\n      group: fin-ops\n"),
    _added("models/props.yml", "models:\n  - name: m\n    group: 財務\n"),
    _added("models/props.yml", "models:\n  - name: m\n    meta:\n      dagster:\n        group: a.b\n"),
], ids=lambda f: f["full_content"][:40])
def test_group_不符合_補blocker(f):
    found = _run(f)
    assert [(x["file"], x["severity"]) for x in found] == [(f["path"], "blocker")]
    assert "group 只能用英數字與底線" in found[0]["detail"]


# 資料檢查
@pytest.mark.parametrize("f", [
    _added("models/props.yml", "models:\n  - name: m\n    data_tests:\n"
                               "      - unique:\n          name: 唯一\n"),
    _added("models/props.yml", "models:\n  - name: m\n    columns:\n      - name: 金額\n"
                               "        data_tests:\n          - not_null:\n              name: amt-nn\n"),
    _added("models/props.yml", "models:\n  - name: m\n    columns:\n      - name: id\n"
                               "        tests:\n          - name: 編號唯一\n            test_name: unique\n"),
    _added("models/src.yml", "sources:\n  - name: core\n    tables:\n      - name: t\n"
                             "        data_tests:\n          - unique:\n              name: 唯一\n"),
    _added("models/props.yml", "models:\n  - name: m\n    data_tests:\n      - unique:\n"
                               "          config:\n            name: u-1\n"),
    _added("tests/檢查餘額.sql", "select 1 where 1 = 0"),
    _added("tests/check-balance.sql", "select 1 where 1 = 0"),
], ids=lambda f: f["path"] + ":" + f["full_content"][-25:])
def test_資料檢查的名稱不符合_補blocker(f):
    found = _run(f)
    assert [(x["file"], x["severity"]) for x in found] == [(f["path"], "blocker")]
    assert "資料檢查的名稱只能用英數字、底線、`.`" in found[0]["detail"]


# 帶版本的 model
@pytest.mark.parametrize("yml", [
    "models:\n  - name: dim\n    versions:\n      - v: 版本二\n",                  # 版本號
    "models:\n  - name: dim\n    versions:\n      - v: 2\n        config:\n          alias: 維度\n",
    "models:\n  - name: dim\n    config:\n      alias: 維度\n    versions:\n      - v: 2\n",
    "models:\n  - name: dim\n    versions:\n      - v: 2\n        config:\n          schema: 報表\n",
])
def test_帶版本的_model_名稱不符合_補blocker(yml):
    assert _sev(_added("models/versions.yml", yml)) == [("models/versions.yml", "blocker")]


def test_帶版本的_model_預設名稱與點號都可以():
    yml = "models:\n  - name: dim\n    versions:\n      - v: 2\n      - v: 2.5\n"
    assert _run(_added("models/versions.yml", yml)) == []


def test_沒有版本的_alias_不影響():
    yml = "models:\n  - name: m\n    config:\n      alias: 每日\n"
    assert _run(_added("models/props.yml", yml)) == []
    f = _added("models/m.sql", "{{ config(alias='每日') }}\nselect 1")
    assert _run(f, ctx=_proj_ctx(versioned_yml=NO_VERSIONS)) == []
    project = "name: p\nmodels:\n  p:\n    +alias: 每日\n"
    g = {"path": "dbt_project.yml", "diff": "@@ -1 +1 @@\n-x\n+y\n"}
    assert _run(g, ctx=_proj_ctx(root={"dbt_project.yml": project}, versioned_yml=NO_VERSIONS)) == []
    # 專案有帶版本的 model、或不知道時:整個資料夾的 +alias 可能套到帶版本的 model
    assert _sev(g, ctx=_proj_ctx(root={"dbt_project.yml": project}, versioned_yml=VERSIONED)) == [
        ("dbt_project.yml", "major")]
    assert _sev(_added("dbt_project.yml", project)) == [("dbt_project.yml", "major")]


def test_SQL_的_alias_要看_model_有沒有版本():
    f = _added("models/dim_v2.sql", "{{ config(alias='維度') }}\nselect 1")
    assert _sev(f, ctx=_proj_ctx(versioned_yml=VERSIONED)) == [("models/dim_v2.sql", "blocker")]
    # 專案有帶版本的 model、但檔名對不上(可能用 defined_in 指定):不知道
    g = _added("models/other.sql", "{{ config(alias='維度') }}\nselect 1")
    assert _sev(g, ctx=_proj_ctx(versioned_yml=VERSIONED)) == [("models/other.sql", "major")]
    # 沒有取回專案:不知道有沒有版本
    assert _sev(f) == [("models/dim_v2.sql", "major")]
    # alias 本身符合規則時,有沒有版本都沒差
    ok = _added("models/other.sql", "{{ config(alias='dim-2') }}\nselect 1")
    assert _run(ok) == []


def test_versioned_models():
    layout = Layout()
    assert versioned_models({"models/v.yml": VERSIONED, "models/n.yml": NO_VERSIONS}, layout) == {"dim"}
    assert versioned_models({"models/n.yml": NO_VERSIONS}, layout) == frozenset()
    assert versioned_models({"models/v.yml": "models: [\n  versions"}, layout) is None
    assert versioned_models({"seeds/v.yml": VERSIONED}, layout) == frozenset()


# 值是樣板、寫法看不懂:major
@pytest.mark.parametrize("f", [
    _added("models/m.sql", "{{ config(schema=env_var('S', '報表')) }}\nselect 1"),
    _added("models/m.sql", "{{ config(schema=var('s')) }}\nselect 1"),
    _added("models/m.sql", "{{ config(schema='a' ~ 'b') }}\nselect 1"),
    _added("models/m.sql", "{{ config(**cfg) }}\nselect 1"),
    _added("models/m.sql", "{% do config.set('schema', '報表') %}\nselect 1"),
    _added("models/m.sql", "{{ config(schema='報表'\nselect 1"),
    _added("models/props.yml", "models:\n  - name: m\n    config:\n      schema: \"{{ env_var('S') }}\"\n"),
    _added("dbt_project.yml", "name: p\nmodels:\n  p:\n    +schema: \"{{ env_var('S') }}\"\n"),
    _added("models/props.yml", "models:\n  - name: m\n    meta: 文字\n"),
    _added("models/props.yml", "models:\n  - name: m\n    config:\n      group: [a]\n"),
    _added("models/props.yml", "models:\n  - name: m\n    data_tests: unique\n"),
    _added("snapshots/s.sql", "{% snapshot snap %}\n{{ config(target_schema='報表') }}\nselect 1\n"
                               "{% endsnapshot %}"),
], ids=lambda f: f["full_content"][:45])
def test_值是樣板或看不懂時補major(f):
    assert _sev(f) == [(f["path"], "major")]


def test_config_的參數是樣板時寫出是哪個參數():
    f = _added("models/m.sql", "{{ config(group=env_var('G')) }}\nselect 1")
    assert "config() 的 group 是樣板或運算式" in _run(f)[0]["detail"]


def test_檔名不合規而內容看不懂時_兩條都補():
    f = _added("models/每日.sql", "{{ config(schema=var('s')) }}\nselect 1")
    assert sorted(_sev(f)) == [("models/每日.sql", "blocker"), ("models/每日.sql", "major")]


# 不受影響的
@pytest.mark.parametrize("f", [
    _added("models/m.sql", "{{ config(materialized='table', tags=['每日'], schema='mart-daily') }}\n"
                           "select 1 as 金額"),
    _added("models/m.sql", "{{ config(meta={'owner': '王小明', 'dagster': {'group': 'fin/ops'}}) }}"),
    _added("models/m.sql", "-- config( 註解裡的括號\nselect 1"),
    _added("models/props.yml", "models:\n  - name: m\n    columns:\n      - name: 狀態\n"
                               "        data_tests:\n          - accepted_values:\n"
                               "              values: ['正常', '停用']\n          - not_null\n"),
    _added("models/exposures.yml", "exposures:\n  - name: 每日報表\n    type: dashboard\n"
                                   "    owner:\n      name: 王小明\n"),
    _added("models/props.yml", "models:\n  - name: m\n    config:\n      tags: [每日]\n"
                               "      meta:\n        負責人: 王小明\n"),
    _added("tests/generic/中文.sql", "{% test 中文(model) %}select 1{% endtest %}"),
    _added("tests/每日檢查_generic.sql", "{% test my_check(model) %}select 1{% endtest %}"),
    _added("tests/check.balance.sql", "select 1"),
    _added("models/m.py", "def model(dbt, session):\n    dbt.config(materialized='table')\n    return 1\n"),
], ids=lambda f: f["path"] + ":" + f["full_content"][:25])
def test_不影響_Dagster_名稱的設定不報(f):
    assert _run(f) == []


def test_設定值是布林時補major():
    f = _added("models/props.yml", "models:\n  - name: m\n    config:\n      schema: true\n")
    assert _sev(f) == [("models/props.yml", "major")]


def test_跟名稱無關的參數寫成樣板不影響():
    f = _added("models/m.sql", "{{ config(materialized=var('m'), tags=[var('t')], schema='ok_s') }}"
                               "\nselect 1")
    assert _run(f) == []


def test_Jinja_的_true_false_none_算常值():
    f = _added("models/m.sql", "{{ config(meta={'x': true, 'y': none, 'dagster': {'group': 'a-b'}}) }}"
                               "\nselect 1")
    assert _sev(f) == [("models/m.sql", "blocker")]


def test_dbt_project_yml_資料夾太深時不解析():
    yml = "name: p\nmodels:\n" + "".join("  " * (i + 1) + f"d{i}:\n" for i in range(80)) \
        + "  " * 81 + "+schema: ok\n"
    assert _sev(_added("dbt_project.yml", yml)) == [("dbt_project.yml", "major")]
