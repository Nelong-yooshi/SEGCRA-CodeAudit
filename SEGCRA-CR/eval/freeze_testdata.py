#!/usr/bin/env python3
"""凍結一條規則的測資計畫,供 `run_eval.py --frozen-testdata` 使用。

  python eval/freeze_testdata.py R-140                  # 生成,0 缺口才存檔
  python eval/freeze_testdata.py RETAIL_M1_EVAL --resume --force   # 接著上次的缺口補
  python eval/freeze_testdata.py R-140 --attempts 8      # 拉高嘗試次數,更容易抽到 0 缺口
  python eval/freeze_testdata.py R-140 --profile fast    # 換模型
  python eval/freeze_testdata.py R-140 --force           # 有缺口也要存(不建議,見下)

存檔後、commit 前再跑 `python eval/check_frozen_testdata.py R-140`:0 缺口只代表每個
條件都有兩向案例,不代表反向案例真的只讓那一個條件不成立。

只做這一件事:呼叫真的 `generate_cases()`(不經任何快取),印出結果,
再決定要不要存檔。凍結是人主動觸發的動作,不是自動發生的——不然凍住的
可能是一次不完整的取樣,而且不會有人注意到 golden set 從此少測了幾個條件。

**存檔後請 commit**:凍結檔與 `eval/golden/`、`specs/` 同類,是受測內容本身
(理由見 `eval/testdata_cache.py`)。它也已列入 baseline 指紋,換了凍結檔
兩份 baseline 就不可比——這正是它該被看見、該進 PR 的原因。
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))

from orchestrator.config import load_config  # noqa: E402
from orchestrator import spec_exec  # noqa: E402
from orchestrator.spec_exec import generate_cases  # noqa: E402

import testdata_cache  # noqa: E402 - 與本檔同目錄(eval/),執行時已在 sys.path[0]


def _reference_check(spec_code: str, plan: dict) -> tuple[str | None, float | None]:
    """用該規則的正向對照 SQL 跑一遍測資;回傳問題描述,沒問題回 None。

    回 (問題描述或 None, 沙盒跑一輪花幾秒或 None)。後者是「一個 MR 用這份測資
    跑完執行驗證要多久」的直接量測(#47 review 要求記時間)——它和生成的耗時差
    三個數量級(實測 2.6 秒 vs 70 分鐘),是「生成該離線化」的直接證據。

    沙盒不可用、或這條規則還沒設定正向對照 → 回 (None, None)(跳過,不阻擋):
    這道檢查是加分的,不該讓沒有沙盒的環境無法凍結測資。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import check_frozen_testdata as cft

        from orchestrator.spec_exec import execute_cases
        if spec_code not in cft.REFERENCE:
            # 跳過也要說一聲:直接回 None 看起來跟「驗過了」一樣。
            print(f"[跳過正向對照] {spec_code} 沒有設定正向對照 SQL"
                  f"(見 eval/check_frozen_testdata.py 的 REFERENCE),"
                  f"**這份測資的預期值沒有被驗過**。")
            return None, None
        t0 = time.monotonic()
        ex = execute_cases(cft.reference_sql(spec_code), plan)
        secs = round(time.monotonic() - t0, 2)
        env_err = ex["sandbox_error"] or ex["sql_error"] or ex["testdata_error"]
        if env_err:
            print(f"[跳過正向對照] 環境問題,不是測資的錯:{str(env_err)[:120]}"
                  f"——**這份測資的預期值沒有被驗過**,有沙盒時請補跑 "
                  f"python eval/check_frozen_testdata.py {spec_code}")
            return None, None
        bad = [m.get("case_id") for m in ex["mismatches"]]
        if not bad:
            print(f"[正向對照] {len(ex['case_results'])} 個案例全部符合"
                  f"{cft.REFERENCE[spec_code]} 的行為;沙盒跑一輪 {secs} 秒。")
            return None, secs
        return (f"正向對照({cft.REFERENCE[spec_code]})跑這份測資有 {len(bad)} "
                f"個案例不符預期:{', '.join(str(b) for b in bad)}\n"
                f"0 缺口不代表測資是對的——這些案例的 expect_flagged 與正確實作"
                f"的行為不一致,凍結後 golden set 會一直拿錯的預期去跑。"), secs
    except Exception as e:
        # 這道檢查壞掉不該讓凍結無法進行(它是加分的,不是必要路徑),
        # 但一定要說出來——否則看起來跟「驗過了」一樣。
        print(f"[跳過正向對照] 檢查本身出錯({type(e).__name__}: {str(e)[:100]})"
              f"——**這份測資的預期值沒有被驗過**。"
              f"請單獨跑 python eval/check_frozen_testdata.py {spec_code} 看詳細錯誤。")
        return None, None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec_code", help="規則碼,如 R-140(對應 specs/R-140.md)")
    ap.add_argument("--attempts", type=int, default=5,
                    help="最多重試幾次去抽到 0 缺口(預設 5,比 generate_cases() "
                        "內建的 3 次更寬——凍結是一次性操作,值得多花幾次呼叫)")
    ap.add_argument("--profile", default=None, help="模型 profile(預設用 roles.testgen)")
    ap.add_argument("--resume", action="store_true",
                    help="接著現有凍結檔的缺口繼續補,不重新生成整份。大規則一輪"
                        "補不完(實測 RETAIL_M1_EVAL 13 次呼叫 151 分鐘補到 30/46),"
                        "而補缺是可累積的——再跑一輪就會往前推進")
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

    seed = None
    if args.resume:
        path = PKG_ROOT / "eval" / "testdata_cache" / f"{args.spec_code}.json"
        if not path.exists():
            print(f"--resume 需要現有的凍結檔,但找不到 {path}")
            return 1
        data = json.loads(path.read_text(encoding="utf-8"))
        seed = data.get("plan")
        if data.get("spec_sha256") != testdata_cache._sha(spec_text):
            # 規格變了,舊的條件編號可能對不上——接著補會補到錯的東西上。
            print("規格已經改過(spec_sha256 不符),不能接著補:"
                  "條件可能已經不同。請重新生成。")
            return 1
        if data.get("prompt_sha256") != testdata_cache._prompt_hash():
            print("注意:TESTGEN prompt 已經改過(prompt_sha256 不符)。"
                  "條件編號仍沿用舊的那一份,接著補出來的案例會是兩個版本的混合。")
        have = len((seed or {}).get("cases") or [])
        print(f"接著現有的 {have} 個案例繼續補(來自 {data.get('generated_at')})")

    # 計時與呼叫次數:這一份測資「生出來花了多少」要跟「抓得到幾個突變」一起看
    # (#47 review)。數模型呼叫次數的方式是包一層 run_agent——它是這條路徑上
    # 唯一會呼叫模型的地方。
    calls = {"n": 0}
    real_run_agent = spec_exec.run_agent

    async def counting_run_agent(*a, **kw):
        calls["n"] += 1
        return await real_run_agent(*a, **kw)

    spec_exec.run_agent = counting_run_agent
    t0 = time.monotonic()
    try:
        plan, gaps, dropped = await generate_cases(
            cfg, args.spec_code, spec_text, profile_name=args.profile,
            max_attempts=args.attempts, seed_plan=seed)
    finally:
        spec_exec.run_agent = real_run_agent
    gen_seconds = round(time.monotonic() - t0, 1)
    print(f"生成耗時 {gen_seconds / 60:.1f} 分鐘({calls['n']} 次模型呼叫)")

    print(f"{args.spec_code}: {len(plan['cases'])} 案例、"
          f"{len(plan['conditions'])} 個條件、{len(gaps)} 個缺口、{len(dropped)} 個被剔除")
    for g in gaps:
        print(f"  - {g}")

    if not plan["cases"]:
        print("沒有任何合法案例,不存檔。")
        return 1

    # 0 缺口**不代表**測資是對的。實測 RETAIL_M1_EVAL:46 個案例、0 缺口,
    # 其中 12 個連正確的 SQL 都過不了(10 個是「只滿足自己宣稱的那一條、
    # 其他條件湊不齊」,2 個是模型把資料算錯)。那種測資凍結進 repo 之後,
    # golden set 會一直拿錯的預期去跑,而且不會有人發現。
    # 所以只要這條規則有正向對照(check_frozen_testdata.py 的 REFERENCE),
    # 就在存檔前先用它驗一遍。沒有沙盒或沒有對照時跳過,不阻擋。
    verdict, sandbox_seconds = _reference_check(args.spec_code, plan)
    if verdict:
        print(f"\n{verdict}")
        if not args.force:
            print("不存檔。修好測資或用 --force 強制存(不建議:golden set 會拿"
                  "錯的預期去跑)。")
            return 1
        print("--force:仍然存檔。")

    # 生成方式:程式照結構產生的測資會在 plan 裡帶 generation;沒帶的是模型逐筆寫的
    # (結構化生成接上之前唯一的方式)。不認得的值 save() 會擋下來。
    generation = plan.get("generation", "model_cases")
    try:
        testdata_cache.save(
            args.spec_code, spec_text, plan, gaps,
            profile=(args.profile or cfg.roles.get("testgen") or cfg.default_profile),
            model=testdata_cache.testgen_model(cfg, args.profile),
            generation=generation,
            force=args.force,
            cost={"llm_calls": calls["n"], "generation_seconds": gen_seconds,
                  "sandbox_seconds": sandbox_seconds,
                  "resumed_from": (seed is not None)})
    except ValueError as e:
        print(f"\n{e}")
        return 1

    print(f"\n已凍結到 eval/testdata_cache/{args.spec_code}.json"
          f"(生成方式:{testdata_cache.GENERATIONS[generation]})")
    print("0 缺口不代表每個條件都驗到了:commit 前請逐條看過反向案例,"
          "並跑 eval/check_frozen_testdata.py 確認寫錯的 SQL 都抓得到。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
