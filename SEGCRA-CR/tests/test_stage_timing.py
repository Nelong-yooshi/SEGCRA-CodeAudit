"""各階段耗時(報告的 `_stage_seconds`)與用它做的兩支工具。

各步的上限與收尾保留要照量到的數字訂;評測平常把整份的上限設大、量審查品質,時間另外用
`eval/stage_time_summary.py` 看有幾份在正式的截止時間下會被縮短(見 docs/07)。所以時間要
真的記到、記在對的階段,收尾要包含回寫。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from orchestrator import pipeline
from tests.test_model_call_failure import staged  # noqa: F401 — fixture

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import measure_finish  # noqa: E402
import stage_time_summary as sts  # noqa: E402

REPORT = json.dumps({"summary": "ok", "findings": []})
STAGES = ["prepare", "main_review", "cold_read", "postprocess", "spec_exec", "finish", "total"]


def _slow_main(seconds, text=REPORT):
    async def main():
        await asyncio.sleep(seconds)
        return text
    return main


def test_各階段都記到_順序固定_加起來是總時間(staged):
    report, _ = staged(_slow_main(0.3))
    s = report["_stage_seconds"]
    assert list(s) == STAGES
    assert all(isinstance(v, float) and v >= 0 for v in s.values())
    assert s["main_review"] >= 0.3
    assert abs(sum(s[k] for k in STAGES[:-1]) - s["total"]) <= 0.4      # 各自四捨五入到 0.1
    assert report["_review_deadline_seconds"] == pipeline.REVIEW_BUDGET_SECONDS


def test_冷讀的時間記在冷讀(staged, monkeypatch):
    from orchestrator.cold_read import ColdRead

    async def slow_cold(cfg, mr, head_texts=None):
        await asyncio.sleep(0.3)
        return ColdRead(flagged=False)
    monkeypatch.setattr(pipeline, "_cold_read", slow_cold)
    s = staged(_slow_main(0))[0]["_stage_seconds"]
    assert s["cold_read"] >= 0.3 and s["main_review"] < 0.3


def test_執行驗證的時間記在執行驗證(staged, monkeypatch):
    async def slow_spec(cfg, hub, mr, *a, **kw):
        await asyncio.sleep(0.3)
        return {"passed": True, "findings": []}
    monkeypatch.setattr(pipeline, "run_spec_exec", slow_spec)
    s = staged(_slow_main(0))[0]["_stage_seconds"]
    assert s["spec_exec"] >= 0.3 and s["cold_read"] < 0.3


def test_收尾包含回寫(staged, monkeypatch):
    """回寫(逐條貼回 MR)是收尾保留要涵蓋的主要部分,收尾的秒數要在回寫完才記。"""
    original = pipeline._format_comment

    def slow_format(fd):
        time.sleep(0.15)
        return original(fd)
    monkeypatch.setattr(pipeline, "_format_comment", slow_format)
    report, _ = staged(_slow_main(0), sql="DELETE FROM t")   # R001:至少一條 finding 要貼
    assert report["findings"]
    assert report["_stage_seconds"]["finish"] >= 0.15 * len(report["findings"]) - 0.05


def test_已有更新的_commit_不回寫時也記到收尾(staged, monkeypatch):
    async def superseded(*a, **kw):
        return True
    monkeypatch.setattr(pipeline, "_superseded", superseded)
    report, _ = staged(_slow_main(0))
    assert report.get("writeback_skipped")
    assert list(report["_stage_seconds"]) == STAGES


def test_模型自己輸出的同名欄位被蓋掉(staged):
    text = json.dumps({"summary": "ok", "findings": [], "_stage_seconds": {"total": 0},
                       "_review_deadline_seconds": 99999})
    report, _ = staged(_slow_main(0, text))
    assert list(report["_stage_seconds"]) == STAGES
    assert report["_review_deadline_seconds"] == pipeline.REVIEW_BUDGET_SECONDS


# ─────────────────── eval/stage_time_summary.py ───────────────────

def _sec(**kw):
    base = dict.fromkeys(STAGES[:-1], 0.0)
    base.update(kw)
    base["total"] = sum(base.values())
    return base


@pytest.mark.parametrize("seconds,expected", [
    (_sec(prepare=100, main_review=900, cold_read=50, spec_exec=600, finish=30), None),
    (_sec(main_review=1801), "main_review"),                         # 主審查自己的上限
    (_sec(prepare=300, main_review=1700, cold_read=200), "cold_read"),
    (_sec(prepare=300, main_review=1000, cold_read=100, spec_exec=800), "spec_exec"),
    (_sec(main_review=1000, spec_exec=1000, finish=500), None),      # 收尾不受截止時間限制
    (_sec(prepare=2200, main_review=5), "main_review"),              # 預掃就用完:主審查先碰到
    (_sec(prepare=1000, main_review=500, cold_read=100, postprocess=600), "spec_exec"),
    (_sec(prepare=600, main_review=1500), None),                     # 剛好到截止:來得及
    (_sec(prepare=600, main_review=1500, cold_read=0.1), "cold_read"),
])
def test_照正式的截止時間跑_哪一步先被縮短(seconds, expected):
    assert sts.first_cut(seconds, 2100, 1800) == expected


def test_統計(tmp_path, capsys, monkeypatch):
    (tmp_path / "mr_1.json").write_text(json.dumps(
        {"findings": [], "_stage_seconds": _sec(main_review=100), "_review_deadline_seconds": 86100}))
    (tmp_path / "mr_2.json").write_text(json.dumps(
        {"findings": [], "_stage_seconds": _sec(prepare=300, main_review=1700, cold_read=200),
         "_review_deadline_seconds": 86100}))
    (tmp_path / "mr_3.json").write_text(json.dumps({"findings": []}))           # 舊的報告:沒有時間
    (tmp_path / "other.json").write_text(json.dumps({"cases": []}))            # 不是報告
    (tmp_path / "broken.json").write_text("{")
    r = sts.summarize([tmp_path], 2100, 1800)
    assert r["reports"] == 2 and r["untimed"] == ["mr_3"]
    assert r["would_be_cut"] == {"mr_2": "cold_read"}
    assert r["deadlines"] == [86100]
    assert r["stages"]["main_review"]["max"] == 1700

    monkeypatch.setattr(sys, "argv", ["x", str(tmp_path)])
    sts.main()
    out = capsys.readouterr().out
    assert "1 / 2 份" in out and "mr_2" in out and "注意" not in out


def test_用正式的截止時間跑的評測要提醒(tmp_path, capsys, monkeypatch):
    (tmp_path / "mr_1.json").write_text(json.dumps(
        {"findings": [], "_stage_seconds": _sec(main_review=100), "_review_deadline_seconds": 2100}))
    monkeypatch.setattr(sys, "argv", ["x", str(tmp_path)])
    sts.main()
    assert "注意" in capsys.readouterr().out


# ─────────────────── eval/measure_finish.py ───────────────────

def test_量收尾的工具跑得動_回寫走真的_gitlab_模組(tmp_path, monkeypatch):
    """假的 GitLab 只聽 127.0.0.1;收尾的請求要有逐條貼回、label 與 commit status 各一次。"""
    from toolbox import gitlab
    keys = ("REAL_MODE", "GITLAB_URL", "GITLAB_TOKEN", "GITLAB_PROJECT", "GITLAB_READ_TOKEN")
    before = {k: getattr(gitlab, k) for k in keys}
    monkeypatch.setenv("REVIEW_OUTPUT", str(tmp_path))
    r = measure_finish.measure(files=2, model_findings=1, latency=0.0)
    assert {k: getattr(gitlab, k) for k in keys} == before          # 量完把設定還原
    req = r["finish_requests"]
    assert r["findings"] >= 3                       # 兩條 R001 + 一條模型的
    assert req["PUT label"] == 1 and req["POST commit status"] == 1
    assert req["GET diffs"] == 1                    # 只算收尾的(回寫前比對 head 那一次)
    assert req.get("POST 行內討論", 0) + req.get("POST 一般留言", 0) >= r["findings"] + 1  # + 總結
    assert list(r["stage_seconds"]) == STAGES
