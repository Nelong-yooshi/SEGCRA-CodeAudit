"""會變成 Dagster 資產名稱的 dbt 名稱,不符合 Dagster 的命名規則時補 blocker。

部署端用 Dagster(1.13)與 dagster-dbt 載入 dbt 專案。資產名稱要符合 `^[A-Za-z0-9_]+$`,
`-` 與 `.` 會先被 dagster-dbt 換成底線;不符合時 Dagster 整個載入失敗。下面的情境照
Dagster 1.13 實測的結果:會擋的、不會擋的各一條。
"""
import asyncio
import json

import pytest

from orchestrator import pipeline
from orchestrator.dbt_asset_names import Layout, check_file, valid_asset_name
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
    _added("models/mrt_daily.sql", "{{ config(alias='每日') }}\nselect 1 as 金額"),  # alias、欄位
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
    assert "^[A-Za-z0-9_]+$" in detail


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
