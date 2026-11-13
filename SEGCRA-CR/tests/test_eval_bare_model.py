"""評測的裸模型對照(`eval/run_eval.py --baseline`)——不呼叫 LLM。

同一批 golden 題目跑「完整管線」與「裸模型」(review_mr 的 baseline 模式:只給模型看 diff),
比問題偵測與決策。裸模型只輸出 verdict,要對應成管線的三態才能比;層級斷言驗的是管線的防線,
裸模型不適用。

  1. verdict → 決策的對應;對不上的記成無法判讀,不猜
  2. 決策有沒有落在預期裡(字串或清單;沒寫預期的不計)
  3. 不該放行卻自動放行(最危險的錯)單獨計數
  4. 整輪:裸模型模式真的帶 baseline=True、不跑層級斷言、結果檔寫明模式與決策統計
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import run_eval  # noqa: E402


# ─────────────────── 1. verdict → 決策 ───────────────────

@pytest.mark.parametrize("verdict, decision", [
    ("approve", "auto_approved"), ("needs_changes", "needs_human"), ("reject", "blocked"),
    ("APPROVE", run_eval.BARE_UNREADABLE), ("ok", run_eval.BARE_UNREADABLE),
    (None, run_eval.BARE_UNREADABLE), (1, run_eval.BARE_UNREADABLE),
])
def test_裸模型的_verdict_對應成決策(verdict, decision):
    assert run_eval.report_decision({"_mode": "baseline", "verdict": verdict}) == decision


def test_管線的報告照用自己的決策():
    assert run_eval.report_decision({"decision": "blocked", "verdict": "approve"}) == "blocked"
    assert run_eval.report_decision({}) == "-"


# ─────────────────── 2. 決策對不對 ───────────────────

@pytest.mark.parametrize("want, got, ok", [
    ("blocked", "blocked", True), ("blocked", "needs_human", False),
    (["needs_human", "blocked"], "needs_human", True), (["needs_human", "blocked"], "auto_approved", False),
    (None, "blocked", None),
])
def test_決策有沒有落在預期裡(want, got, ok):
    golden = {} if want is None else {"expect_decision": want}
    assert run_eval.decision_ok(golden, got) is ok


# ─────────────────── 3. 不該放行卻自動放行 ───────────────────

@pytest.mark.parametrize("want, got, missed", [
    ("blocked", "auto_approved", True),
    (["needs_human"], "auto_approved", True),
    (["auto_approved", "needs_human"], "auto_approved", False),    # 允許放行的不算
    ("blocked", "needs_human", False),                             # 錯了但沒有放行
    (None, "auto_approved", False),                                # 沒寫預期的不計
])
def test_不該放行卻自動放行(want, got, missed):
    golden = {} if want is None else {"expect_decision": want}
    assert run_eval.missed_block(golden, got) is missed


# ─────────────────── 4. 整輪 ───────────────────

def _args(**kw):
    base = dict(profile=None, layer=None, case=None, dry_run=False, json=None, dump_reports=None,
                from_reports=None, frozen_testdata=False, baseline=False)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def one_case(monkeypatch):
    golden = {"layer": "rule-base", "intent": "x", "expect_decision": "blocked",
              "expect_findings": [{"severity": "blocker"}]}
    case = {"expected": [{"file": "a.sql", "line": 1, "severity": "blocker", "keywords": ["WHERE"]}],
            "_golden": golden}
    monkeypatch.setattr(run_eval, "load_cases", lambda layer, ids: [("901", case)])
    calls = []

    async def fake_review(cfg, mr_id, profile=None, dry_run=False, baseline=False):
        calls.append(baseline)
        if baseline:
            return {"_mode": "baseline", "verdict": "approve", "summary": "看起來沒問題",
                    "findings": []}
        return {"decision": "blocked", "findings": [{"file": "a.sql", "line": 1, "severity": "blocker",
                                                     "title": "DELETE 沒有 WHERE", "detail": ""}]}
    monkeypatch.setattr(run_eval, "review_mr", fake_review)
    return calls


def test_裸模型模式_帶_baseline_不跑層級斷言(one_case, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(run_eval, "check_golden", lambda *a, **k: called.append(1) or [])
    out = tmp_path / "bare.json"
    code = asyncio.run(run_eval.run(_args(baseline=True, json=str(out))))
    assert one_case == [True] and called == []
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["mode"] == "baseline"
    assert (data["decision_right"], data["decision_wrong"], data["missed_block"]) == (0, 1, 1)
    assert data["cases"][0]["decision"] == "auto_approved" and data["cases"][0]["missed_block"]
    assert code == 0                                    # 裸模型沒有層級斷言,不會因此失敗


def test_管線模式照舊(one_case, tmp_path):
    out = tmp_path / "pipe.json"
    asyncio.run(run_eval.run(_args(json=str(out))))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert one_case == [False]
    assert data["mode"] == "pipeline"
    assert (data["decision_right"], data["missed_block"]) == (1, 0)


def test_讀存檔重新評分時認得裸模型的報告(one_case, tmp_path):
    d = tmp_path / "reports"
    d.mkdir()
    (d / "mr_901.json").write_text(json.dumps({"_mode": "baseline", "verdict": "reject",
                                               "findings": []}), encoding="utf-8")
    out = tmp_path / "re.json"
    asyncio.run(run_eval.run(_args(from_reports=str(d), json=str(out))))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["cases"][0]["decision"] == "blocked" and data["cases"][0]["checks"] == 0
