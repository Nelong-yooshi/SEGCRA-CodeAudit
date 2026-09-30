#!/usr/bin/env python3
"""凍結一條規則的測資計畫,供 `run_eval.py --frozen-testdata` 使用。

  python eval/freeze_testdata.py R-140                  # 生成,0 缺口才存檔
  python eval/freeze_testdata.py R-140 --attempts 8      # 拉高嘗試次數,更容易抽到 0 缺口
  python eval/freeze_testdata.py R-140 --profile fast    # 換模型
  python eval/freeze_testdata.py R-140 --force           # 有缺口也要存(不建議,見下)

只做這一件事:呼叫真的 `generate_cases()`(不經任何快取),印出結果,
再決定要不要存檔。凍結是人主動觸發的動作,不是自動發生的——不然凍住的
可能是一次不完整的取樣,而且不會有人注意到 golden set 從此少測了幾個條件。

**存檔後請 commit**:凍結檔與 `eval/golden/`、`specs/` 同類,是受測內容本身
(理由見 `eval/testdata_cache.py`)。它也已列入 baseline 指紋,換了凍結檔
兩份 baseline 就不可比——這正是它該被看見、該進 PR 的原因。
"""
import argparse
import asyncio
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))

from orchestrator.config import load_config  # noqa: E402
from orchestrator.spec_exec import generate_cases  # noqa: E402

import testdata_cache  # noqa: E402 - 與本檔同目錄(eval/),執行時已在 sys.path[0]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec_code", help="規則碼,如 R-140(對應 specs/R-140.md)")
    ap.add_argument("--attempts", type=int, default=5,
                    help="最多重試幾次去抽到 0 缺口(預設 5,比 generate_cases() "
                        "內建的 3 次更寬——凍結是一次性操作,值得多花幾次呼叫)")
    ap.add_argument("--profile", default=None, help="模型 profile(預設用 roles.testgen)")
    ap.add_argument("--force", action="store_true",
                    help="有覆蓋缺口也要存檔。不建議:golden set 跑起來會少測到"
                        "那些條件,而且會一直是同一批案例、不會自動變好")
    args = ap.parse_args()

    spec_path = PKG_ROOT / "specs" / f"{args.spec_code}.md"
    if not spec_path.exists():
        print(f"找不到 {spec_path}")
        return 1
    spec_text = spec_path.read_text(encoding="utf-8")

    cfg = load_config()
    plan, gaps, dropped = await generate_cases(
        cfg, args.spec_code, spec_text, profile_name=args.profile,
        max_attempts=args.attempts)

    print(f"{args.spec_code}: {len(plan['cases'])} 案例、"
          f"{len(plan['conditions'])} 個條件、{len(gaps)} 個缺口、{len(dropped)} 個被剔除")
    for g in gaps:
        print(f"  - {g}")

    if not plan["cases"]:
        print("沒有任何合法案例,不存檔。")
        return 1

    try:
        testdata_cache.save(
            args.spec_code, spec_text, plan, gaps,
            profile=(args.profile or cfg.roles.get("testgen") or cfg.default_profile),
            force=args.force)
    except ValueError as e:
        print(f"\n{e}")
        return 1

    print(f"\n已凍結到 eval/testdata_cache/{args.spec_code}.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
