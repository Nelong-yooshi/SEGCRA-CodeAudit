"""評測報告的各階段耗時統計,以及在正式環境的截止時間下有幾份會被縮短。

評測平常把整份審查的上限設大(見 docs/07「評測的截止時間」),量的是審查品質、不受模型主機的
快慢影響;時間另外用這支看。讀 `run_eval.py --dump-reports` 存下的報告(或任何 review_mr 回傳的
報告 JSON),用報告的 `_stage_seconds`:

    python eval/stage_time_summary.py <報告目錄或檔案> [...] [--budget 2100] [--main-limit 1800]

`--budget` 是正式環境從審查開始到截止的秒數(預設值下是 2400 − 300),`--main-limit` 是主審查
自己的上限。輸出各階段的中位數、P90、最大值,以及「照正式的截止時間跑會被縮短或不開始」的
份數和是哪一步先碰到。評測主機比正式環境慢時,這個份數是上限,不是正式環境的實際數字。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

# 依序跑的階段(見 pipeline.review_mr 的 _StageClock);只有模型的步驟會被截止時間縮短
STAGES = ("prepare", "main_review", "cold_read", "postprocess", "spec_exec", "finish")
MODEL_STAGES = ("main_review", "cold_read", "spec_exec")


def _reports(paths):
    for p in paths:
        p = Path(p)
        for f in (sorted(p.glob("*.json")) if p.is_dir() else [p]):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and "findings" in data:
                yield f.stem, data


def first_cut(seconds: dict, budget: float, main_limit: float) -> str | None:
    """照正式的截止時間跑,哪一步會先被縮短或不開始;都來得及時回 None。"""
    if seconds.get("main_review", 0) > main_limit:
        return "main_review"
    elapsed = 0.0
    for stage in STAGES[:-1]:                    # 收尾不受截止時間限制
        elapsed += seconds.get(stage, 0)
        if stage in MODEL_STAGES and elapsed > budget:
            return stage
    return None


def _q(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def summarize(paths, budget: float, main_limit: float) -> dict:
    timed, untimed, cut, deadlines = {}, [], {}, set()
    for name, report in _reports(paths):
        seconds = report.get("_stage_seconds")
        if not isinstance(seconds, dict):
            untimed.append(name)
            continue
        timed[name] = seconds
        if report.get("_review_deadline_seconds") is not None:
            deadlines.add(report["_review_deadline_seconds"])
        stage = first_cut(seconds, budget, main_limit)
        if stage:
            cut[name] = stage
    stats = {}
    for stage in STAGES + ("total",):
        vals = [s[stage] for s in timed.values() if isinstance(s.get(stage), (int, float))]
        if vals:
            stats[stage] = {"p50": statistics.median(vals), "p90": _q(vals, 0.9), "max": max(vals)}
    return {"reports": len(timed), "untimed": untimed, "deadlines": sorted(deadlines),
            "stages": stats, "would_be_cut": cut}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--budget", type=float, default=2100)
    ap.add_argument("--main-limit", type=float, default=1800)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    r = summarize(args.paths, args.budget, args.main_limit)
    print(f"有時間紀錄的報告 {r['reports']} 份;沒有的 {len(r['untimed'])} 份")
    print(f"這批報告跑的時候的截止時間(秒):{r['deadlines'] or '沒有記錄'}")
    if any(d <= args.budget for d in r["deadlines"]):
        print("  注意:有報告是在正式環境的截止時間(或更短)下跑的,品質的數字裡混了時間的影響")
    for stage, s in r["stages"].items():
        print(f"  {stage:12s} 中位數 {s['p50']:8.1f}  P90 {s['p90']:8.1f}  最大 {s['max']:8.1f}")
    cut = r["would_be_cut"]
    print(f"照正式的截止時間({args.budget:.0f} 秒,主審查 {args.main_limit:.0f} 秒)會被縮短或不開始:"
          f"{len(cut)} / {r['reports']} 份")
    for stage in MODEL_STAGES:
        names = sorted(n for n, s in cut.items() if s == stage)
        if names:
            print(f"  先碰到的是 {stage}:{len(names)} 份 {', '.join(names)}")


if __name__ == "__main__":
    main()
