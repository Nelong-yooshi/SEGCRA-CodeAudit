"""決策閘門的硬條件:規則層有命中、冷讀命中或沒看完,不得自動放行,不可由設定關閉。

預設設定只許 info 自動放行,規則命中(至少 minor)與冷讀那條 finding(major)本來就擋得住;
這裡驗的是設定放寬之後也一樣擋得住。兩個訊號都由程式寫:規則命中從預掃結果算
(`_rule_hits`),冷讀的紀錄由冷讀那一道寫(`_cold_read`),模型輸出的同名欄位會被蓋掉。
"""
import asyncio
import json
from dataclasses import replace

import pytest

from orchestrator import pipeline

# 放寬到 major 也能自動放行的設定:驗硬條件不靠設定
LOOSE = {"auto_approve": {"max_diff_lines": 30, "min_score": 0,
                          "allowed_severities": ["info", "minor", "major"],
                          "forbid_pending_hints": False},
         "block": {"min_blockers": 1}}
MR = {"files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+SELECT a FROM t"}]}


def _report(**extra):
    return {"summary": "x", "score": 100, "findings": [],
            "_spec_exec": {"passed": True}, **extra}


def _decide(report):
    return pipeline.apply_policy(report, MR, LOOSE)


# ─────────────────── 規則命中的彙整 ───────────────────

def test_收集規則層的命中代號_排序不重複():
    pre = [{"path": "a.sql", "rules": [{"rule": "R002"}, {"rule": "R001"}, {"rule": "R002"}]},
           {"path": "b.sql", "rules": [{"rule": "R005"}]}]
    assert pipeline._rule_hit_codes(pre) == ["R001", "R002", "R005"]


def test_檢核點與風格不算():
    pre = [{"path": "a.sql", "rules": [{"rule": "H003"}, {"rule": "S-naming"}]}]
    assert pipeline._rule_hit_codes(pre) == []


@pytest.mark.parametrize("pre", [
    [], None, "x",
    [{"path": "a.sql"}],                               # 沒有 rules
    [{"path": "a.sql", "rules": ["R001", None, {"rule": 1}, {"x": "R001"}]}],
    ["not a dict"],
])
def test_形狀不對的內容不出錯也不當成命中(pre):
    assert pipeline._rule_hit_codes(pre) == []


# ─────────────────── 決策閘門 ───────────────────

def test_設定放寬時_沒有命中照常自動放行():
    """對照組:確認放寬的設定本身會放行,下面幾條才是硬條件擋下的。"""
    assert _decide(_report(findings=[{"severity": "major", "title": "x", "file": "a"}]))["decision"] \
        == "auto_approved"


@pytest.mark.parametrize("hits", [["R002"], ["R004"], ["R001", "R005"]])
def test_規則層有命中就不得自動放行_設定放寬也一樣(hits):
    out = _decide(_report(_rule_hits=hits))
    assert out["decision"] == "needs_human"
    assert out["_policy_signals"]["rule_hits"] == hits


@pytest.mark.parametrize("cold", [
    {"flagged": True, "truncated": False},
    {"flagged": False, "truncated": True},
    {"flagged": True, "truncated": True},
])
def test_冷讀命中或沒看完就不得自動放行_設定放寬也一樣(cold):
    out = _decide(_report(_cold_read=cold))
    assert out["decision"] == "needs_human" and out["_policy_signals"]["cold_read"] is True


@pytest.mark.parametrize("cold", [
    {"flagged": False, "truncated": False},
    None,                                        # 冷讀沒跑(失敗時另有未完整的 finding)
    "flagged",
    {"flagged": "true", "truncated": 1},         # 不是 True 的不算
])
def test_冷讀沒命中也看完時不擋(cold):
    out = _decide(_report(_cold_read=cold))
    assert out["decision"] == "auto_approved" and out["_policy_signals"]["cold_read"] is False


@pytest.mark.parametrize("hits", [[], None])
def test_沒有命中時不擋(hits):
    out = _decide(_report(_rule_hits=hits))
    assert out["decision"] == "auto_approved" and out["_policy_signals"]["rule_hits"] == []


def test_有_blocker_時照樣擋下():
    out = _decide(_report(_rule_hits=["R001"],
                          findings=[{"severity": "blocker", "title": "x", "file": "a"}]))
    assert out["decision"] == "blocked"


# ─────────────────── 走整條 review_mr ───────────────────

def _review(monkeypatch, tmp_path, model_report, *, boom=False):
    from orchestrator.config import load_config
    from toolbox import gitlab

    fixtures, output = tmp_path / "fixtures", tmp_path / "out"
    fixtures.mkdir()
    output.mkdir()
    (fixtures / "mr_964.json").write_text(json.dumps({
        "title": "調整", "description": "例行維護", "sha": "e1ab" + "0" * 36,
        "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+SELECT * FROM t WHERE id = 1",
                   "full_content": "SELECT * FROM t WHERE id = 1"}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    async def fake_run_agent(cfg, profile, system, user, hub, **kw):
        return json.dumps(model_report, ensure_ascii=False)
    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    if boom:
        def broken(pre):
            raise RuntimeError("壞了")
        monkeypatch.setattr(pipeline, "_rule_hit_codes", broken)
    cfg = load_config()
    return asyncio.run(pipeline.review_mr(replace(cfg, policy=LOOSE), "964"))


def test_規則命中由程式寫_模型輸出的同名欄位被蓋掉(monkeypatch, tmp_path):
    """SELECT * 命中 R002(minor):模型自己在輸出裡寫 `_rule_hits: []` 也蓋不掉。"""
    report = _review(monkeypatch, tmp_path,
                     {"summary": "x", "score": 100, "findings": [], "_rule_hits": []})
    assert "R002" in report["_rule_hits"]
    assert "R002" in report["_policy_signals"]["rule_hits"]
    assert report["decision"] != "auto_approved"


def test_彙整出錯時記成未完整_不得自動放行(monkeypatch, tmp_path):
    report = _review(monkeypatch, tmp_path, {"summary": "x", "score": 100, "findings": []}, boom=True)
    assert any(f.get("stage") == "規則命中彙整" for f in report["findings"])
    assert report["_rule_hits"] is None
    assert report["decision"] != "auto_approved"
