"""#45:內層任何一個環節出事,外層都要照常產出審查結果——不呼叫 LLM、不連資料庫。

原則(issue 原文):外層不能因為內層出事而跟著出事。每個環節各自隔離、各自有時間
上限;失敗或逾時就降級成一條 finding,其他環節照跑,整份審查照常產出,
而且**不得自動放行**。

在正式環境,原本的行為是「MR 送進來之後審查器沒有任何回應:沒擋、沒放、
也沒人知道」——那是最糟的失敗型態。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import pipeline


def _report(findings=()):
    return {"summary": "x", "findings": list(findings), "score": 100}


# ─────────────────── 單一環節丟例外 ───────────────────

STAGES = [
    ("validate_citations", "引用白名單"),
    ("sanitize_findings", "輸出清理"),
    ("enforce_hints", "檢核點補報"),
    ("enforce_rules", "規則命中補報"),
    ("enforce_parse", "解析失敗揭露"),
    ("enforce_style", "風格補報"),
    ("enforce_dbt_notice", "dbt 表名提醒"),
    ("enforce_dbt_render_failure", "dbt 展開失敗揭露"),
    ("enforce_injection", "注入防線"),
    ("enforce_unreviewable", "未審查內容揭露"),
    ("enforce_tool_refusals", "工具拒絕揭露"),
    ("apply_rubric", "評分"),
    ("apply_policy", "決策閘門"),
]


@pytest.mark.parametrize("fname,stage", STAGES)
def test_環節丟例外時報告照出並記下是哪個環節(fname, stage):
    """包裝過的環節吃到例外:回傳進來的 report、補一條 major 說明是哪個環節。"""
    def boom(report, *a, **kw):
        raise RuntimeError("內部爆掉:這段訊息不可以出現在報告裡")

    wrapped = pipeline._isolated(stage, boom)
    report = _report([{"severity": "minor", "title": "模型原本的發現"}])
    out = wrapped(report, "其他參數")
    assert out is report, "要沿用進來的 report,不可以吃掉前面環節的結果"
    inc = pipeline.review_incomplete(out)
    assert len(inc) == 1 and stage in inc[0], inc
    assert any(f.get("title") == "模型原本的發現" for f in out["findings"]), \
        "其他環節的 finding 要留著"


@pytest.mark.parametrize("fname,stage", STAGES)
def test_環節失敗的那條是major(fname, stage):
    """major 會讓 auto_approve 的條件不成立(allowed_severities 只許 info)。"""
    wrapped = pipeline._isolated(stage, lambda r, *a, **k: (_ for _ in ()).throw(ValueError()))
    out = wrapped(_report())
    bad = [f for f in out["findings"] if f["title"].startswith("審查未完整")]
    assert bad and all(f["severity"] == "major" for f in bad)


def test_不回顯例外訊息():
    """例外訊息可能帶著模型輸出或 MR 內容,而報告會貼回 GitLab(#45 明訂不得回顯)。"""
    secret = "SELECT * FROM 客戶資料 WHERE 身分證='A123456789'"

    def boom(report, *a, **kw):
        raise RuntimeError(secret)

    out = pipeline._isolated("注入防線", boom)(_report())
    blob = json.dumps(out, ensure_ascii=False)
    assert secret not in blob, "例外訊息被寫進報告了"
    assert "RuntimeError" in blob, "要寫出例外**類型**,不然查不出是什麼問題"


def test_正常時行為完全不變():
    """格式正確、各環節都正常的審查,輸出要跟改之前完全相同(#45 的完成條件)。"""
    def ok(report, *a, **kw):
        report["findings"].append({"severity": "info", "title": "正常"})
        return report

    out = pipeline._isolated("評分", ok)(_report())
    assert pipeline.review_incomplete(out) == []
    assert [f["title"] for f in out["findings"]] == ["正常"]


# ─────────────────── 執行驗證的時間上限 ───────────────────

def test_執行驗證逾時要中止並記一條(monkeypatch):
    """沙盒的單一查詢有 120 秒上限,但整段原本沒有——黑盒實測一題光在沙盒就
    跑了 38 分鐘,整題超過 1 小時。"""
    async def never_returns(*a, **kw):
        await asyncio.sleep(999)

    monkeypatch.setattr(pipeline, "SPEC_EXEC_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(pipeline, "run_spec_exec", never_returns)

    async def run():
        report = _report()
        try:
            await asyncio.wait_for(pipeline.run_spec_exec(None, None, None, None, None),
                                   timeout=pipeline.SPEC_EXEC_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            report["findings"].append(
                pipeline._incomplete_finding("執行驗證",
                                             timeout=pipeline.SPEC_EXEC_TIMEOUT_SECONDS))
        return report

    out = asyncio.run(run())
    inc = pipeline.review_incomplete(out)
    assert len(inc) == 1 and "逾時" in inc[0], inc
    assert "1 秒" in out["findings"][0]["detail"]


def test_上限可以用環境變數設定():
    """issue 寫「例如 20 分鐘,可設定」。預設 1200 秒。"""
    assert pipeline.SPEC_EXEC_TIMEOUT_SECONDS == 1200


# ─────────────────── 決策不得因為沒跑完而放行 ───────────────────

def test_有未完成的環節時不得自動放行():
    """#45:不得 auto_approved,也不得把「沒跑完」當成「沒問題」。"""
    report = _report()
    report["findings"].append(pipeline._incomplete_finding("執行驗證", timeout=1200))
    mr = {"files": [{"path": "m.sql", "diff": "@@ -0,0 +1 @@\n+SELECT 1"}]}
    policy = {"auto_approve": {"max_diff_lines": 30, "min_score": 95,
                               "allowed_severities": ["info"],
                               "forbid_pending_hints": True}}
    out = pipeline.apply_policy(report, mr, policy)
    assert out["decision"] != "auto_approved", out.get("decision")


def test_決策閘門自己壞掉時一律交人工(monkeypatch):
    """決策留空比誤判更危險:呼叫端會以為沒問題。"""
    report = _report()
    broken = pipeline._isolated("決策閘門",
                                lambda r, *a, **k: (_ for _ in ()).throw(KeyError("policy")))
    out = broken(report)
    assert not out.get("decision"), "這個情境下閘門沒跑完,所以沒有決策"
    # review_mr 會在這裡補上 needs_human(見 pipeline.review_mr 的決策段)
    assert pipeline.review_incomplete(out), "至少要留下「未完整」的記錄"


# ─────────────────── 真的走 review_mr 的逾時路徑 ───────────────────
#
# 上面那個逾時測試自己重寫了 asyncio.wait_for,所以把 pipeline 裡的上限拿掉也不會紅
# (突變驗證時那個突變點**存活**了)。這一組驅動真正的 review_mr。

def _mr_fixture(tmp_path, mr_id="950"):
    import json as _json
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / f"mr_{mr_id}.json").write_text(_json.dumps({
        "title": "R-201 調整", "description": "例行維護",
        "sha": "e1a1" + "0" * 36,
        "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+SELECT 1",
                   "full_content": "SELECT account_id FROM txn_log"}],
    }, ensure_ascii=False), encoding="utf-8")
    return fixtures


def _review_with_hanging_spec_exec(monkeypatch, tmp_path, timeout_seconds):
    """讓 run_spec_exec 永遠不回來,其餘環節都用假的,跑真正的 review_mr。"""
    import json as _json

    from orchestrator import pipeline as pl
    from orchestrator.config import load_config
    from toolbox import gitlab

    fixtures = _mr_fixture(tmp_path)
    output = tmp_path / "out"
    output.mkdir(exist_ok=True)
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))
    monkeypatch.setattr(pl, "SPEC_EXEC_TIMEOUT_SECONDS", timeout_seconds)

    model_report = _json.dumps({"summary": "沒問題", "score": 100, "findings": []},
                               ensure_ascii=False)

    async def fake_run_agent(cfg, profile, system, user, hub, verbose=True,
                             use_tools=True, trace=None, meta=None):
        return model_report

    async def hangs(*a, **kw):
        await asyncio.sleep(60)

    monkeypatch.setattr(pl, "run_agent", fake_run_agent)
    monkeypatch.setattr(pl, "run_spec_exec", hangs)
    return asyncio.run(pl.review_mr(load_config(), "950"))


def test_review_mr_的執行驗證逾時會中止並照常產出報告(monkeypatch, tmp_path):
    """整份審查照常產出,只有執行驗證那一段沒有結果。"""
    report = _review_with_hanging_spec_exec(monkeypatch, tmp_path, 1)
    inc = pipeline.review_incomplete(report)
    assert any("執行驗證" in t and "逾時" in t for t in inc), inc
    assert report.get("decision"), "報告還是要有決策"
    assert "summary" in report and "score" in report, "報告本體要完整"


def test_執行驗證逾時時不得自動放行(monkeypatch, tmp_path):
    report = _review_with_hanging_spec_exec(monkeypatch, tmp_path, 1)
    assert report["decision"] != "auto_approved", report["decision"]


def test_執行驗證逾時時沒有_spec_exec欄位(monkeypatch, tmp_path):
    """沒跑完就不該有結果欄位——有欄位會讓判讀的人以為跑過了。"""
    report = _review_with_hanging_spec_exec(monkeypatch, tmp_path, 1)
    assert "_spec_exec" not in report
