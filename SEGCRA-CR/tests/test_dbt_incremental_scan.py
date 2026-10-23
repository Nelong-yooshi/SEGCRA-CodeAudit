"""增量分支也要掃到:預掃與受影響 model 的前後比較,都在需要時以增量模式再展開一次。

增量 model 常把刪改舊資料的語句放在 `{% if is_incremental() %}` 裡;只照 dbt compile
(is_incremental() 為 False)展開,規則層看不到那一段。
"""
import asyncio

import pytest

from orchestrator import dbt_context, dbt_project, pipeline
from orchestrator.config import DBT_DATABASE_PLACEHOLDER
from orchestrator.dbt_affected import check_affected_models
from orchestrator.dbt_render import render_model
from tests.test_dbt_affected import CFG as AFFECTED_CFG
from tests.test_dbt_affected import MACRO_MR, _Hub, _Loader, _project, prepare_dbt_context

DB = DBT_DATABASE_PLACEHOLDER
CFG = {"enabled": True, "database": DB}

# 非增量那次是一句乾淨的 SELECT;增量那次多一句沒有 WHERE 的 DELETE
MODEL = ("select id from {{ ref('txn') }} where id > 0;\n"
         "{% if is_incremental() %}delete from {{ this }};{% endif %}")


@pytest.fixture(autouse=True)
def _fresh_project_cache(monkeypatch):
    """取回的專案依 commit 快取;每題用同樣的假 commit,不清掉會拿到上一題的專案。
    專案資訊在正式路徑是子行程整理的,測試換成同行程。"""
    monkeypatch.setattr(dbt_context, "_summarize_isolated", dbt_context._summarize)
    dbt_project.clear_cache()
    yield
    dbt_project.clear_cache()


@pytest.fixture(autouse=True)
def _render_in_process(monkeypatch):
    """預掃正式路徑走隔離子行程;測試換成同行程,行為相同、快很多。"""
    calls = []

    def render(*args, **kwargs):
        calls.append(kwargs.get("incremental", False))
        return render_model(*args, **kwargs)

    monkeypatch.setattr(pipeline, "render_model_isolated", render)
    return calls


def _prescan(content, path="models/mrt_inc.sql", rendered=None):
    hub = _Hub()
    entries = asyncio.run(pipeline.prescan(hub, [{"path": path, "full_content": content}], CFG,
                                           None, rendered))
    return entries[0], hub


# ─────────────────── 預掃 ───────────────────

def test_增量分支的命中預掃看得到_並標明來自增量分支(_render_in_process):
    entry, hub = _prescan(MODEL)
    # 後兩次是檢查寫入語句有沒有展開到(原文有 delete,插記號後兩種模式各再展開一次)
    assert _render_in_process == [False, True, False, True]
    assert [(h["rule"], h.get("branch")) for h in entry["rules"]] == [("R001", "incremental")]
    assert len(hub.sqls) == 2 and "delete" in hub.sqls[1]


def test_沒有呼叫is_incremental時只展開一次(_render_in_process):
    entry, hub = _prescan("select * from {{ ref('txn') }}")
    assert _render_in_process == [False]
    assert len(hub.sqls) == 1
    assert [h["rule"] for h in entry["rules"]] == ["R002"]


@pytest.mark.parametrize("path", ["models/mrt_inc.sql", "models/mrt-inc.sql",
                                  "models/2026_inc.sql", "models/mrt.inc.sql"])
def test_this_is_defined_裡的語句一般模式就掃得到(_render_in_process, path):
    """dbt 每次執行都有 this;展開器只在增量模式定義它、或給不出值時當成未定義的話,這句 DELETE
    兩種模式都看不到。檔名不能當表名(連字號、數字開頭、點)時也一樣要看得到。"""
    entry, _ = _prescan("select 1 as x\n{% if this is defined %}\n;DELETE FROM important_table\n"
                        "{% endif %}", path=path)
    # 第二次是檢查寫入語句有沒有展開到(插記號再展開);一般模式就展開到,不報
    assert _render_in_process == [False, False]
    assert [(h["rule"], h.get("branch")) for h in entry["rules"]] == [("R001", None)]
    assert "dbt_render_error" not in entry
    assert "dbt_hidden_writes" not in entry


def test_非增量model直接寫this_不會展開失敗():
    entry, _ = _prescan("select a.id from {{ ref('txn') }} a join {{ this }} b on a.id = b.id")
    assert "dbt_render_error" not in entry, entry.get("dbt_render_error")


def test_兩次都有的命中只算一次():
    entry, _ = _prescan("select * from {{ ref('txn') }};\n"
                        "{% if is_incremental() %}delete from {{ this }};{% endif %}")
    assert sorted(((h["rule"], h.get("branch") or "") for h in entry["rules"])) == [
        ("R001", "incremental"), ("R002", "")]


def test_同一句在增量分支多出現一次_多的那次照算():
    src = ("select * from t;\n{% if is_incremental() %}select * from t;{% endif %}")
    entry, _ = _prescan("{{ '' }}" + src)
    assert sorted(((h["rule"], h.get("branch") or "") for h in entry["rules"])) == [
        ("R002", ""), ("R002", "incremental")]


def test_增量分支展開失敗_照展開失敗處理():
    entry, _ = _prescan("select 1;\n{% if is_incremental() %}{{ not_defined() }}{% endif %}")
    assert entry["dbt_render_error"].startswith("增量分支(is_incremental() 為 True)展開失敗")


def test_增量分支解析不了_記成無法解析():
    """語法樹規則在那一段沒有執行,不能當成沒有命中。"""
    hub = _Hub(parse_error_on="BROKEN")
    entries = asyncio.run(pipeline.prescan(
        hub, [{"path": "models/m.sql", "full_content":
               "select 1;\n{% if is_incremental() %}BROKEN{% endif %}"}], CFG, None, None))
    assert entries[0]["parse_error"].startswith("增量分支(is_incremental() 為 True)")


def test_兩份展開結果都進注入掃描():
    rendered = []
    _prescan(MODEL, rendered=rendered)
    assert len(rendered) == 2 and "delete" not in rendered[0]["full_content"]
    assert "delete" in rendered[1]["full_content"]


def test_合併命中時_形狀不對的項目略過不當掉():
    merged = pipeline._merge_incremental_hits([{"rule": "R1", "statement": "a"}, "x"],
                                              [None, {"rule": "R1", "statement": "a"},
                                               {"rule": "R1", "statement": "b"}])
    assert merged == [{"rule": "R1", "statement": "a"}, "x",
                      {"rule": "R1", "statement": "b", "branch": "incremental"}]


# ─────────────────── 受影響 model 的前後比較 ───────────────────

def _affected(head_body, base_body, **kw):
    loader = _Loader(_project(head_body), _project(base_body))
    ctx = prepare_dbt_context(MACRO_MR, AFFECTED_CFG, real_mode=True, load=loader)
    renders = []

    def render(*args, timeout_s=None, **kwargs):
        assert timeout_s is not None and timeout_s > 0      # 正式路徑會帶剩下的時間
        renders.append(kwargs.get("incremental", False))
        return render_model(*args, **kwargs)

    check = asyncio.run(check_affected_models(_Hub(), MACRO_MR, AFFECTED_CFG, ctx,
                                              find_spec=_no_spec, render=render))
    big = next(m for m in check.models if m.path.endswith("mrt_BIG.sql"))
    return big, renders


async def _no_spec(hub, mr, *, by_path=False):
    return None, None


def test_只在增量分支多出的命中_受影響model照樣抓到():
    base = "SELECT 1 AS id{% if is_incremental() %}; SELECT 2 AS id{% endif %}"
    head = "SELECT 1 AS id{% if is_incremental() %}; DELETE FROM t{% endif %}"
    big, renders = _affected(head, base)
    assert big.error is None
    assert ("R001", "blocker") in big.new_hits
    assert renders.count(True) == 2              # 變更前、變更後各以增量模式展開一次


def test_沒有呼叫is_incremental時_不以增量模式展開():
    big, renders = _affected("SELECT * FROM t", "SELECT 1 AS id")
    assert True not in renders
    assert big.new_hits == (("R002", "minor"),)


def test_兩種模式都多出的命中只算一次():
    base = "SELECT 1 AS id{% if is_incremental() %}; SELECT 2 AS id{% endif %}"
    head = "SELECT * FROM t{% if is_incremental() %}; SELECT 2 AS id{% endif %}"
    big, _ = _affected(head, base)
    assert list(big.new_hits) == [("R002", "minor")]


def test_變更後的增量分支展開失敗_算這個model的錯誤():
    base = "SELECT 1 AS id{% if is_incremental() %}; SELECT 2 AS id{% endif %}"
    head = "SELECT 1 AS id{% if is_incremental() %}{{ not_defined() }}{% endif %}"
    big, _ = _affected(head, base)
    assert big.error and big.error.startswith("增量分支(is_incremental() 為 True)")


def test_變更前的增量分支展開失敗_算無法比較():
    base = "SELECT 1 AS id{% if is_incremental() %}{{ not_defined() }}{% endif %}"
    head = "SELECT 1 AS id{% if is_incremental() %}; SELECT 2 AS id{% endif %}"
    big, _ = _affected(head, base)
    assert big.error is None and big.no_baseline is True


def test_補報寫明命中在增量分支裡():
    entry, _ = _prescan(MODEL)
    report = pipeline.enforce_rules({"findings": []}, [entry])
    [fd] = report["findings"]
    assert fd["title"].startswith("[R001]") and "增量分支" in fd["detail"]
    plain, _ = _prescan("select 1;\ndelete from t;")
    [fd2] = pipeline.enforce_rules({"findings": []}, [plain])["findings"]
    assert "增量分支" not in fd2["detail"]
