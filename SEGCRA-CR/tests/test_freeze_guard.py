"""凍結前的正向對照檢查——0 缺口不代表測資是對的。

實測 RETAIL_M1_EVAL(2026-10-13):46 個案例、**0 缺口**,其中 12 個連正確的
SQL 都過不了(10 個是「只滿足自己宣稱的那一條、其他條件湊不齊」,2 個是模型
把資料算錯)。沒有這道擋,那份測資會因為「0 缺口」被直接凍結進 repo,
golden set 從此一直拿錯的預期去跑,而且不會有人發現。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import freeze_testdata as fz  # noqa: E402

PLAN = {"schema_ddl": ["CREATE TABLE t (id INT)"],
        "conditions": [{"id": "C1", "desc": "x"}],
        "cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
                   "expect_flagged": True, "note": "", "rows": [["t", 1]]}]}


def test_沒有正向對照的規則直接跳過(monkeypatch):
    """多數規則還沒設定正向對照;這道檢查是加分的,不該讓它們無法凍結。"""
    verdict, secs = fz._reference_check("NO_SUCH_RULE", PLAN)
    assert verdict is None and secs is None


def test_對照跑出不符時回報並列出案例(monkeypatch):
    import check_frozen_testdata as cft
    monkeypatch.setitem(cft.REFERENCE, "FAKE", "mr_000")
    monkeypatch.setattr(cft, "reference_sql", lambda code: "SELECT 1")
    monkeypatch.setattr(fz, "_reference_check", fz._reference_check)  # 不替換,只確認可取用

    import orchestrator.spec_exec as se
    monkeypatch.setattr(se, "execute_cases", lambda sql, plan: {
        "sandbox_error": None, "sql_error": None, "testdata_error": None,
        "mismatches": [{"case_id": "C1-T"}], "case_results": [], "engine": "stub"})
    msg, secs = fz._reference_check("FAKE", PLAN)
    assert msg and "C1-T" in msg
    assert isinstance(secs, float), "不符預期時也要回報沙盒耗時"
    assert "0 缺口不代表測資是對的" in msg


def test_沙盒不可用時不阻擋(monkeypatch):
    """環境問題不是測資的錯——本機沒有 MS SQL 沙盒時仍然要能凍結。"""
    import check_frozen_testdata as cft
    monkeypatch.setitem(cft.REFERENCE, "FAKE", "mr_000")
    monkeypatch.setattr(cft, "reference_sql", lambda code: "SELECT 1")
    import orchestrator.spec_exec as se
    monkeypatch.setattr(se, "execute_cases", lambda sql, plan: {
        "sandbox_error": "連不到沙盒", "sql_error": None, "testdata_error": None,
        "mismatches": [{"case_id": "C1-T"}], "case_results": [], "engine": None})
    assert fz._reference_check("FAKE", PLAN) == (None, None)


def test_全部符合時回None並附上沙盒耗時(monkeypatch):
    import check_frozen_testdata as cft
    monkeypatch.setitem(cft.REFERENCE, "FAKE", "mr_000")
    monkeypatch.setattr(cft, "reference_sql", lambda code: "SELECT 1")
    import orchestrator.spec_exec as se
    monkeypatch.setattr(se, "execute_cases", lambda sql, plan: {
        "sandbox_error": None, "sql_error": None, "testdata_error": None,
        "mismatches": [], "case_results": [], "engine": "stub"})
    verdict, secs = fz._reference_check("FAKE", PLAN)
    assert verdict is None
    assert isinstance(secs, float), "「一個 MR 在沙盒跑完要多久」要量出來(#47 review)"


# ─────────────────── 凍結檔要記下「生出來花了多少」(#47 review 第 5 點) ───────────────────

def test_凍結檔要記下呼叫次數與耗時(tmp_path, monkeypatch):
    """為什麼存進檔案而不是只印出來:這些數字決定「哪些規格適合線上現場生」與
    「上限該訂多少」,不該每次要用就重跑一輪(大規格一輪 70 分鐘);評測也要
    記下每份規格花多久。"""
    import json as _json
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "eval"))
    import testdata_cache as tc

    monkeypatch.setattr(tc, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(tc, "_path", lambda code: tmp_path / f"{code}.json")
    plan = {"schema_ddl": ["CREATE TABLE t (id INT)"], "conditions": [],
            "cases": [{"case_id": "a", "rows": [["t", 1], ["t", 2]]},
                      {"case_id": "b", "rows": [["t", 3]]}]}
    tc.save("R-X", "規格", plan, [], profile="review", model="m", generation="model_cases",
            cost={"llm_calls": 4, "generation_seconds": 3600.0,
                  "sandbox_seconds": 2.6, "resumed_from": False})
    d = _json.loads((tmp_path / "R-X.json").read_text(encoding="utf-8"))
    assert d["cost"]["llm_calls"] == 4
    assert d["cost"]["generation_seconds"] == 3600.0
    assert d["cost"]["sandbox_seconds"] == 2.6
    assert d["case_count"] == 2
    assert d["row_count"] == 3, "資料列數也要記:它是模型要算的量,和錯誤率相關"


def test_沒給cost也存得起來(tmp_path, monkeypatch):
    """舊的呼叫端(或沒有沙盒的環境)不傳 cost,不該因此存不了。"""
    import json as _json
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "eval"))
    import testdata_cache as tc

    monkeypatch.setattr(tc, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(tc, "_path", lambda code: tmp_path / f"{code}.json")
    tc.save("R-Y", "規格", {"schema_ddl": [], "conditions": [], "cases": []}, [],
            profile="review", model="m", generation="model_cases")
    d = _json.loads((tmp_path / "R-Y.json").read_text(encoding="utf-8"))
    assert d["cost"] == {} and d["row_count"] == 0
