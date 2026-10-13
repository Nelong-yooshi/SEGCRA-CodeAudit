"""凍結前的正向對照檢查——0 缺口不代表測資是對的。

實測 RETAIL_M1_EVAL(2026-10-13):46 個案例、**0 缺口**,其中 12 個連正確的
SQL 都過不了(10 個是「只滿足自己宣稱的那一條、其他條件湊不齊」,2 個是模型
把資料算錯)。沒有這道擋,那份測資會因為「0 缺口」被直接凍結進 repo,
golden set 從此一直拿錯的預期去跑,而且不會有人發現。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

import freeze_testdata as fz  # noqa: E402

PLAN = {"schema_ddl": ["CREATE TABLE t (id INT)"],
        "conditions": [{"id": "C1", "desc": "x"}],
        "cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
                   "expect_flagged": True, "note": "", "rows": [["t", 1]]}]}


def test_沒有正向對照的規則直接跳過(monkeypatch):
    """多數規則還沒設定正向對照;這道檢查是加分的,不該讓它們無法凍結。"""
    assert fz._reference_check("NO_SUCH_RULE", PLAN) is None


def test_對照跑出不符時回報並列出案例(monkeypatch):
    import check_frozen_testdata as cft
    monkeypatch.setitem(cft.REFERENCE, "FAKE", "mr_000")
    monkeypatch.setattr(cft, "reference_sql", lambda code: "SELECT 1")
    monkeypatch.setattr(fz, "_reference_check", fz._reference_check)  # 不替換,只確認可取用

    import orchestrator.spec_exec as se
    monkeypatch.setattr(se, "execute_cases", lambda sql, plan: {
        "sandbox_error": None, "sql_error": None, "testdata_error": None,
        "mismatches": [{"case_id": "C1-T"}], "case_results": [], "engine": "stub"})
    msg = fz._reference_check("FAKE", PLAN)
    assert msg and "C1-T" in msg
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
    assert fz._reference_check("FAKE", PLAN) is None


def test_全部符合時回None(monkeypatch):
    import check_frozen_testdata as cft
    monkeypatch.setitem(cft.REFERENCE, "FAKE", "mr_000")
    monkeypatch.setattr(cft, "reference_sql", lambda code: "SELECT 1")
    import orchestrator.spec_exec as se
    monkeypatch.setattr(se, "execute_cases", lambda sql, plan: {
        "sandbox_error": None, "sql_error": None, "testdata_error": None,
        "mismatches": [], "case_results": [], "engine": "stub"})
    assert fz._reference_check("FAKE", PLAN) is None
