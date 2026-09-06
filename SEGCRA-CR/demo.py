#!/usr/bin/env python3
"""SEGCRA-CR demo — 對一個 MR 跑完整審查管線(mock 模式免 GitLab)。

  python demo.py --mr 001 --dry-run          # 不經 LLM 的管線煙霧測試
  python demo.py --mr 001                    # 完整審查(預設 profile: review)
  python demo.py --mr 002 --profile fast     # 換模型
  python demo.py --mr 001 --baseline         # 裸模型 A/B 對照(無管線防線)
"""
import argparse
import asyncio
import json
import os

from orchestrator.config import load_config
from orchestrator.pipeline import review_mr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mr", default="001", help="MR id(mock 模式對應 fixtures/mr_<id>.json)")
    ap.add_argument("--profile", default=None, help="模型 profile(review/fast)")
    ap.add_argument("--dry-run", action="store_true", help="不呼叫 LLM,只驗證管線")
    ap.add_argument("--baseline", action="store_true",
                    help="裸模型對照組(無 skills/memory/工具/執行驗證,不回寫 GitLab)")
    args = ap.parse_args()

    cfg = load_config()
    report = asyncio.run(review_mr(cfg, args.mr, args.profile,
                                   dry_run=args.dry_run, baseline=args.baseline))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.baseline:
        print("\n(baseline 模式:未回寫 GitLab)")
    elif os.environ.get("GITLAB_URL"):
        print(f"\n審查已回寫 GitLab MR !{args.mr}")
    else:
        print(f"\n審查結果已寫入 review_output/mr_{args.mr}_review.json(mock 模式)")


if __name__ == "__main__":
    main()
