"""dbt 展開器與 macro 反查「做不到、無法確認」的,記成待驗證(帶 stage)——不呼叫 LLM、不連資料庫。

確定的(Jinja 語法錯誤、多出的規則命中)照規則本身的嚴重度;做不到的不借成確定的缺陷:帶 stage,
review_incomplete 認得到,決策不放行。分數不扣由待驗證的計分處理。
"""
import pytest

from orchestrator import pipeline
from orchestrator.dbt_affected import AffectedCheck, impact_finding, impact_unverified_finding
from orchestrator.dbt_impact import AffectedModel, ImpactReport
from orchestrator.dbt_render import MACRO_LOAD_FAILED, MACRO_SYNTAX, MACRO_TOO_DEEP, MACRO_UNSUPPORTED
from tests.test_dbt_affected import _cm


def _incomplete(finding):
    return bool(pipeline.review_incomplete({"findings": [finding], "summary": ""}))


@pytest.mark.parametrize("kind", [MACRO_UNSUPPORTED, MACRO_TOO_DEEP, MACRO_LOAD_FAILED, "check_failed"])
def test_macro_檔載入不了是待驗證(kind):
    f = pipeline._macro_problem_finding({"path": "macros/x.sql", "kind": kind, "line": 0})
    assert f["stage"] == "dbt macro 檢查" and f["title"].startswith("[dbt macro 檢查] 無法驗證") and _incomplete(f)


def test_macro_檔語法錯誤是確定的_照舊_blocker_沒有_stage():
    f = pipeline._macro_problem_finding({"path": "macros/x.sql", "kind": MACRO_SYNTAX, "line": 0})
    assert f["severity"] == "blocker" and "stage" not in f


def test_展開失敗_語法錯誤以外的是待驗證():
    f = pipeline._render_fail_finding({"path": "models/m.sql", "dbt_render_error": "UndefinedError"})
    assert f["stage"] == "dbt 展開" and f["title"].startswith("[dbt 展開] 無法驗證") and _incomplete(f)


def test_展開失敗_樣板語法錯誤是確定的():
    f = pipeline._render_fail_finding({"path": "models/m.sql", "dbt_render_error": "x", "dbt_syntax_line": 3})
    assert f["severity"] == "blocker" and "stage" not in f


@pytest.mark.parametrize("title, stage", [
    (pipeline._HIDDEN_WRITE_TITLE, "dbt 展開"),
    (pipeline._UNREACHED_BRANCH_TITLE, "dbt 展開"),
    (pipeline._YAML_UNCHECKED_TITLE, "dbt 屬性檔檢查"),
    (pipeline._DBT_ASSET_NAME_UNKNOWN_TITLE, "dbt 名稱檢查"),
])
def test_其他做不到的標題寫出哪個環節沒能確認(title, stage):
    assert title.startswith(f"[{stage}] 無法驗證")


def test_反查做不到的是待驗證_規則命中照舊():
    check = AffectedCheck(impact=ImpactReport(affected_models=(AffectedModel("models/a.sql", "a", (), True),),
                                              uncertain=("x",)),
                          models=(_cm(new_hits=(("R003", "major"),)),))
    main, unv = impact_finding(check), impact_unverified_finding(check)
    assert main["severity"] == "major" and "stage" not in main
    assert unv["stage"] == "macro 反查" and _incomplete(unv)


def test_待驗證的整份報告_決策不是放行():
    f = pipeline._render_fail_finding({"path": "models/m.sql", "dbt_render_error": "UndefinedError"})
    report = {"findings": [f], "summary": "", "score": 100, "verdict": "approve"}
    policy = {"auto_approve": {"max_diff_lines": 1000, "min_score": 0, "allowed_severities": ["info", "major"],
                               "forbid_pending_hints": False}, "block": {"min_blockers": 1}}
    mr = {"files": [{"path": "models/m.sql", "diff": "+x"}]}
    assert pipeline.apply_policy(report, mr, policy)["decision"] != "auto_approved"
