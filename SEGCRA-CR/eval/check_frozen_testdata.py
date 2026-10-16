#!/usr/bin/env python3
"""檢查凍結的測資計畫抓不抓得到寫錯的 SQL——凍結前後都該跑一次。

  python eval/check_frozen_testdata.py R-140

`coverage_gaps` 為 0 只代表「每個條件都有 true/false 兩向的案例」,不代表每個條件
都驗到了:反向案例若同時讓兩個條件不成立(例如只有一筆 1,000 元、總額也低於
50 萬門檻),那個條件寫對寫錯結果都一樣,0 缺口照樣成立。

所以這裡直接在沙盒上量:
  1. 正確的 SQL(取自該規則的正向對照 golden case)跑凍結的案例 → 必須全部符合預期
  2. 每個「寫錯的版本」跑凍結的案例 → 至少要有一個案例不符,才算抓得到
任一個寫錯的版本沒被抓到,就是凍結檔有個條件形同沒驗,不該凍結(或要補案例)。

需要 MS SQL 沙盒(config/sandbox.env),不呼叫 LLM。新規則要用時,在 REFERENCE 與
MUTANTS 各加一筆:正向對照 case 與照規格逐條寫錯的版本。
"""
import argparse
import json
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))

from orchestrator.spec_exec import _shape_check, execute_cases  # noqa: E402

import testdata_cache  # noqa: E402 - 與本檔同目錄(eval/),執行時已在 sys.path[0]

# 規則碼 → 提供正確 SQL 的正向對照 golden case
REFERENCE = {"R-140": "mr_406"}

# 規則碼 → [(寫錯的方式, 正確 SQL 裡的原文, 改成)]。改成空字串 = 整個拿掉這個條件。
MUTANTS = {
    "R-140": [
        ("拿掉條件 1(op_code 範圍)", "WHERE t.op_code IN ('OP01','OP02','OP03')\n  AND ", "WHERE "),
        ("拿掉條件 2(現金註記)", "  AND t.cash_flag = 'Y'\n", ""),
        ("拿掉條件 3(排除沖銷)", "  AND COALESCE(t.rev_flag, 'N') <> 'Y'\n", ""),
        ("拿掉條件 4(排除小額)", "  AND t.amount > 1000\n", ""),
        ("條件 4 寫成 >= 1000", "t.amount > 1000", "t.amount >= 1000"),
        ("條件 3 漏掉 NULL 語意(沒用 COALESCE)", "COALESCE(t.rev_flag, 'N') <> 'Y'", "t.rev_flag <> 'Y'"),
        ("拿掉條件 5(排除行銷撥入)", "  AND COALESCE(t.channel_name, '') <> 'PROMO'\n", ""),
        ("條件 5 漏掉 NULL 語意(沒用 COALESCE)", "COALESCE(t.channel_name, '') <> 'PROMO'",
         "t.channel_name <> 'PROMO'"),
        ("拿掉時間窗", "  AND t.tx_time >= @start_date AND t.tx_time < @end_date\n", ""),
        ("門檻寫成 > 500000", "HAVING SUM(t.amount) >= 500000", "HAVING SUM(t.amount) > 500000"),
    ],
}


def reference_sql(spec_code: str) -> str:
    case = json.loads((PKG_ROOT / "eval" / "golden" / f"{REFERENCE[spec_code]}.json")
                      .read_text(encoding="utf-8"))
    return case["files"][0]["full_content"]


def mutate(sql: str, old: str, new: str) -> str:
    if sql.count(old) != 1:
        raise ValueError(f"寫錯的版本對不上正確 SQL(原文出現 {sql.count(old)} 次):{old!r}")
    return sql.replace(old, new)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec_code", help="規則碼,如 R-140(要有凍結檔與 REFERENCE/MUTANTS 設定)")
    args = ap.parse_args()
    code = args.spec_code
    if code not in REFERENCE or code not in MUTANTS:
        print(f"{code} 沒有設定正向對照或寫錯的版本(見本檔的 REFERENCE / MUTANTS)")
        return 1

    path = testdata_cache._path(code)
    if not path.exists():
        print(f"找不到凍結檔 {path}")
        return 1
    plan, gaps, dropped = _shape_check(json.loads(path.read_text(encoding="utf-8"))["plan"])
    print(f"{code} 凍結檔:{len(plan['cases'])} 案例、{len(gaps)} 缺口、{len(dropped)} 個形狀不合法被剔除")

    sql = reference_sql(code)
    ok = execute_cases(sql, plan)
    for k in ("sandbox_error", "sql_error", "testdata_error"):
        if ok[k]:
            print(f"正確的 SQL 跑不起來({k}):{ok[k]}")
            return 1
    if ok["mismatches"]:
        print("正確的 SQL 就有案例不符——案例本身的預期錯了,或正向對照的 SQL 錯了:")
        for m in ok["mismatches"]:
            print(f"  - {m.get('case_id')}:預期 {m.get('expect_flagged')},實際 {m.get('actual_flagged')}")
        return 1
    print(f"正確的 SQL:{len(ok['case_results'])} 個案例全部符合預期\n")

    survived = []
    print("| SQL 寫錯成 | 凍結檔抓得到嗎 | 抓到的案例 |")
    print("|---|---|---|")
    for name, old, new in MUTANTS[code]:
        ex = execute_cases(mutate(sql, old, new), plan)
        err = ex["sandbox_error"] or ex["sql_error"] or ex["testdata_error"]
        if err:
            print(f"| {name} | 無法判斷(執行失敗) | {err[:80]} |")
            survived.append(name)
            continue
        caught = [m.get("case_id") for m in ex["mismatches"]]
        print(f"| {name} | {'抓得到' if caught else '**抓不到**'} | {', '.join(caught) or '—'} |")
        if not caught:
            survived.append(name)

    if survived:
        print(f"\n{len(survived)} 種寫錯的版本沒被抓到:{'、'.join(survived)}")
        print("對應的條件形同沒驗。請補一個「只有該條件不成立」的反向案例。")
        return 1
    print(f"\n{len(MUTANTS[code])} 種寫錯的版本全部抓得到。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
