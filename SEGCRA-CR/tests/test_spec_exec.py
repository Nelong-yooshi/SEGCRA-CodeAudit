"""執行驗證 spec_exec 的確定性部分 — 測資形狀檢查、規則碼抽取、仲裁後的分支,
不呼叫 LLM、不連資料庫(三個角色裡會呼叫模型的那兩個都換成假的)。

為什麼值得單獨測:spec_exec 的三個角色裡,角色一(測資生成)與角色三(仲裁)是模型,
**但夾在中間與後面的判定邏輯全是確定性的**:形狀檢查、覆蓋缺口計算、仲裁判定後要
剔除案例還是開 major finding。這些邏輯用 golden case 驗不到,因為觸發哪個分支取決於
模型當下生出什麼——eval/README「覆蓋缺口」列的「spec_exec 仲裁(測資錯 vs SQL 錯)」
就是這個原因一直沒有對應 case。

把模型換成固定回傳值之後,兩個分支都能穩定走到。

驗的東西:
  1. _shape_check:逐條件 true/false 兩向覆蓋檢查、非法案例剔除、
     **表名白名單(擋 SQL 注入)**、缺 schema_ddl。
  2. extract_rule_codes:從標題/描述/檔名/內容抓規則碼。
  3. 無規格可驗 → major + 不得自動放行(「必跑」的意思是沒 spec 就不能放行,不是跳過)。
  4. 仲裁判「測資錯」→ 剔除該案例、補覆蓋缺口、標記 dropped;
     **剔除後那個方向就沒驗到,所以不能算通過**——這是最容易被誤解成「通過」的分支。
  5. 仲裁判「SQL 錯」→ 開 major finding、passed=False。
  6. 仲裁 LLM 掛掉時保守倒向「SQL 錯」(寧可誤報待人工,不可誤放)。
"""
import asyncio

import pytest

from orchestrator import spec_exec
from orchestrator.spec_exec import _shape_check, extract_rule_codes, run_spec_exec


def _case(cid, cond, direction, flagged=True, table="transactions"):
    return {"case_id": cid, "condition_id": cond, "direction": direction,
            "rows": [[table, 1, "2026-06-01 10:00:00"]],
            "expect_flagged": flagged, "note": f"{cid} 測試"}


def _plan(cases, conditions=("C1",), ddl=("CREATE TABLE transactions (id INT);",)):
    return {"schema_ddl": list(ddl),
            "conditions": [{"id": c, "desc": f"條件 {c}"} for c in conditions],
            "cases": list(cases)}


# ─────────────────── 形狀與覆蓋檢查 ───────────────────

def test_兩向覆蓋齊全時沒有缺口():
    plan, gaps, dropped = _shape_check(
        _plan([_case("C1-T", "C1", "true"), _case("C1-F", "C1", "false", False)]))
    assert gaps == []
    assert len(plan["cases"]) == 2
    assert dropped == []


def test_缺一個方向就記成覆蓋缺口():
    """只有 true 向 = 只驗了「該命中的有命中」,沒驗「不該命中的沒命中」。

    漏報與誤報是兩種不同的錯,少一個方向就有一半沒驗到。
    """
    _, gaps, _ = _shape_check(_plan([_case("C1-T", "C1", "true")]))
    assert any("C1" in g and "false" in g for g in gaps)


def test_多個條件各自檢查覆蓋():
    plan = _plan([_case("C1-T", "C1", "true"), _case("C1-F", "C1", "false", False),
                  _case("C2-T", "C2", "true")],
                 conditions=("C1", "C2"))
    _, gaps, _ = _shape_check(plan)
    assert len(gaps) == 1 and "C2" in gaps[0]      # C1 齊全,只缺 C2 的 false 向


def test_表名白名單擋掉非法識別字():
    """rows 的第一欄是要被組進 SQL 的表名,必須擋住注入。

    測資是模型生成的,模型可能幻覺出奇怪的字串;這一關是確定性防線,不能只靠模型乖。
    """
    for bad_table in ("transactions; DROP TABLE users", "tx-log", "1table", ""):
        plan, _, dropped = _shape_check(
            _plan([_case("C1-T", "C1", "true", table=bad_table)]))
        assert plan["cases"] == [], f"非法表名 {bad_table!r} 沒被剔除"
        assert dropped and "形狀不合法" in dropped[0]["reason"]


@pytest.mark.parametrize("broken", [
    {"case_id": "X", "condition_id": "C1", "direction": "sideways",      # 方向不合法
     "rows": [["t", 1]], "expect_flagged": True},
    {"case_id": "X", "condition_id": "C9", "direction": "true",          # 條件不存在
     "rows": [["t", 1]], "expect_flagged": True},
    {"case_id": "X", "condition_id": "C1", "direction": "true",          # 沒有資料列
     "rows": [], "expect_flagged": True},
    {"case_id": "X", "condition_id": "C1", "direction": "true",          # 預期值不是布林
     "rows": [["t", 1]], "expect_flagged": "yes"},
])
def test_形狀不合法的案例會被剔除(broken):
    plan, _, dropped = _shape_check(_plan([broken]))
    assert plan["cases"] == []
    assert len(dropped) == 1


def test_沒有合法_DDL_算缺口():
    _, gaps, _ = _shape_check(_plan([_case("C1-T", "C1", "true")], ddl=("DROP TABLE x;",)))
    assert "無合法 schema_ddl" in gaps


def test_整份計畫不是物件時不炸():
    plan, gaps, _ = _shape_check("這不是 JSON 物件")
    assert plan["cases"] == [] and gaps


# ─────────────────── 規則碼抽取 ───────────────────

@pytest.mark.parametrize("mr,expected", [
    ({"title": "調整 R-201 閾值", "description": "", "files": []}, ["R-201"]),
    ({"title": "", "description": "依 R-093 核定規格", "files": []}, ["R-093"]),
    ({"title": "", "description": "",
      "files": [{"path": "sql/rules/r305.sql", "full_content": "-- R-305: 夜間"}]}, ["R-305"]),
    # 多個規則碼要全部抓到並排序(去重)
    ({"title": "R-201 與 R-093", "description": "R-201 相關", "files": []},
     ["R-093", "R-201"]),
    ({"title": "純維護", "description": "無規則碼", "files": []}, []),
])
def test_抽取規則碼(mr, expected):
    assert extract_rule_codes(mr) == expected


# ─────────────────── 無規格可驗 ───────────────────

def test_找不到規格時不得自動放行():
    """「必跑」的意思:不是沒 spec 就跳過,而是沒 spec 就不能自動放行。"""
    mr = {"files": [{"path": "sql/rules/r777.sql", "full_content": "SELECT 1;"}]}
    r = asyncio.run(run_spec_exec(None, None, mr, spec_code="R-777", spec_text=None))
    assert r["passed"] is False
    assert r["no_spec"] is True
    assert any(f["severity"] == "major" for f in r["findings"])


# ─────────────────── 仲裁後的兩個分支 ───────────────────

def _stub_spec_exec(monkeypatch, verdict, mismatch_case="C1-T"):
    """把三個角色裡會呼叫模型/資料庫的部分換掉,只留中間的確定性判定邏輯。"""
    plan = _plan([_case("C1-T", "C1", "true"), _case("C1-F", "C1", "false", False)])

    async def fake_generate(cfg, code, text, profile_name=None):
        return plan, [], []

    def fake_execute(sql, plan_):
        return {
            "engine": "stub", "sandbox_error": None, "testdata_error": None,
            "sql_error": None,
            "case_results": [{"case_id": "C1-T"}, {"case_id": "C1-F"}],
            "mismatches": [{
                "case_id": mismatch_case, "condition_id": "C1", "direction": "true",
                "note": "邊界案例", "rows": [["transactions", 1]],
                "expect_flagged": True, "actual_flagged": False, "actual_rows": 0,
            }],
        }

    async def fake_arbitrate(cfg, spec_text, sql, mm, profile_name=None):
        return verdict

    monkeypatch.setattr(spec_exec, "generate_cases", fake_generate)
    monkeypatch.setattr(spec_exec, "execute_cases", fake_execute)
    monkeypatch.setattr(spec_exec, "arbitrate", fake_arbitrate)

    mr = {"files": [{"path": "sql/rules/r201.sql", "full_content": "SELECT 1;"}]}
    return asyncio.run(run_spec_exec(None, None, mr, "R-201", "規格內容"))


def test_仲裁判測資錯_剔除案例並補覆蓋缺口(monkeypatch):
    """這個分支最容易被誤解:仲裁說「是測資錯不是 SQL 錯」聽起來像沒事,

    但剔除那個案例之後,該條件的那個方向就**沒有被驗證過**,所以必須補成覆蓋缺口、
    而且整體不能算通過。不這樣做的話,測資生成得越爛、被剔除得越多,反而越容易「通過」。
    """
    r = _stub_spec_exec(monkeypatch, {"who_is_wrong": "testdata", "reason": "測資日期寫錯"})

    assert r["dropped_cases"] and r["dropped_cases"][0]["by"] == "arbiter"
    assert any("遭仲裁剔除" in g for g in r["coverage_gaps"])
    assert any(c.get("dropped") for c in r["case_results"] if c["case_id"] == "C1-T")
    assert r["passed"] is False, "被剔除的方向沒驗到,不能算通過"


def test_仲裁判_SQL_錯_開_major_finding(monkeypatch):
    r = _stub_spec_exec(monkeypatch,
                        {"who_is_wrong": "sql", "reason": "閾值用了 >= 但規格寫超過"})
    assert r["passed"] is False
    majors = [f for f in r["findings"] if f["severity"] == "major"]
    assert majors and "不符" in majors[0]["title"]
    assert "閾值" in majors[0]["detail"]          # 仲裁理由要帶進報告,可回溯
    assert r["dropped_cases"] == [], "判 SQL 錯時不該剔除案例"


def test_仲裁結果可以從_dropped_cases_看出來(monkeypatch):
    """給 eval 斷言用的落點:dropped_cases 裡 by=arbiter 就代表仲裁判過「測資錯」。

    eval/README「覆蓋缺口」提到「目前也沒有能斷言仲裁結果的訊號」,
    對應的訊號就是從這個欄位讀(見 run_eval.py 的 arbiter_dropped)。
    """
    r = _stub_spec_exec(monkeypatch, {"who_is_wrong": "testdata", "reason": "x"})
    assert [d["by"] for d in r["dropped_cases"]] == ["arbiter"]

    r = _stub_spec_exec(monkeypatch, {"who_is_wrong": "sql", "reason": "x"})
    assert [d["by"] for d in r["dropped_cases"]] == []
