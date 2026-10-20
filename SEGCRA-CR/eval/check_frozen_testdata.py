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

from orchestrator.spec_exec import (_label_gaps, _shape_check,  # noqa: E402
                                    _unverified, execute_cases,
                                    gaps_and_passed)

import testdata_cache  # noqa: E402 - 與本檔同目錄(eval/),執行時已在 sys.path[0]

# 規則碼 → 提供正確 SQL 的正向對照 golden case
REFERENCE = {"R-140": "mr_406", "RETAIL_M1_EVAL": "mr_701"}

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
    # RETAIL_M1_EVAL 是目前唯一的大規則(23 個條件、46 個「條件×方向」),也是測資
    # 生成唯一會走到「骨架 + 逐批補缺」那條路的規則(見 orchestrator/spec_exec.py 的
    # _FILL_BATCH)。沒有這組設定,補缺機制對覆蓋**品質**有沒有幫助就無從量測:
    # coverage_gaps 只數「每個條件有沒有兩向案例」,數得出數量、數不出有沒有驗到。
    #
    # 每一筆對應規格裡的一個條件,所以「抓不到」會直接指名是哪一條形同沒驗。
    # 門檻類的寫錯多半是**邊界**(含端點 vs 不含、上下界放寬一格),抓不到通常
    # 代表測資只生了「明顯不符」的案例、沒有踩在門檻上。
    "RETAIL_M1_EVAL": [
        # ── 排除資料(規格「排除資料」段的三項) ──
        ("拿掉空白帳號排除",
         "WHERE NULLIF(LTRIM(RTRIM(account_id)), '') IS NOT NULL\n    AND ", "WHERE "),
        ("小額入帳門檻寫成 >= 1000(規格是 > 1000)",
         "AND amount > 1000", "AND amount >= 1000", "*/2"),
        ("拿掉小額入帳排除",
         "        AND amount > 1000\n", "", "*/2"),
        ("拿掉 EarlyJob 排除",
         "WHERE NOT EXISTS (\n    SELECT 1\n    FROM retail_m1_earlyjob e\n"
         "    WHERE e.account_id = f.account_id\n"
         "    AND e.TABLE_DATE = DATEADD(DAY, -1, @start_date)\n)",
         "WHERE 1 = 1"),
        ("EarlyJob 比對日改成同日(規格是前一日)",
         "DATEADD(DAY, -1, @start_date)", "@start_date"),
        # ── 時間窗與衍生指標 ──
        ("拿掉交易日限制",
         "    AND TABLE_DATE = @start_date\n", ""),
        ("當日結餘取第一筆而非最後一筆",
         "ORDER BY posted_date DESC, txn_time DESC", "ORDER BY posted_date ASC, txn_time ASC"),
        ("io_ratio 分母為 0 時給 NULL(規格是 0)",
         "WHEN inbound_amt = 0 THEN 0", "WHEN inbound_amt = 0 THEN NULL"),
        ("io_ratio 不四捨五入到小數 2 位",
         "ROUND(CAST(outbound_amt AS FLOAT) / CAST(inbound_amt AS FLOAT), 2)",
         "CAST(outbound_amt AS FLOAT) / CAST(inbound_amt AS FLOAT)"),
        # ── 分類:op_code 與 direction_flag ──
        ("指定入帳少一個 op_code",
         "op_code IN ('OP01','OP02','OP03')", "op_code IN ('OP01','OP02')", "*/2"),
        ("自助設備出帳少一個 op_code",
         "op_code IN ('OP11','OP12')", "op_code IN ('OP11')", "*/2"),
        ("指定出帳少一個 op_code",
         "op_code IN ('OP21','OP22','OP23')", "op_code IN ('OP21','OP22')"),
        ("入帳/出帳方向寫反",
         "direction_flag = '0'", "direction_flag = '1'", "*/2"),
        # ── 條件 1(A):七項各一個 ──
        ("A 1-1 kiosk_outbound_cnt 上界放寬成 4",
         "kiosk_outbound_cnt BETWEEN 2 AND 3", "kiosk_outbound_cnt BETWEEN 2 AND 4"),
        ("A 1-2 kiosk_outbound_amt 下界寫成不含端點",
         "kiosk_outbound_amt BETWEEN 50000 AND 100000",
         "kiosk_outbound_amt > 50000 AND kiosk_outbound_amt <= 100000", "1/2"),
        ("A 1-3 outbound_amt 上界放寬成 120000",
         "outbound_amt BETWEEN 90000 AND 110000",
         "outbound_amt BETWEEN 90000 AND 120000", "1/2"),
        ("A 1-4 inbound_amt 下界寫成不含端點",
         "inbound_amt BETWEEN 80000 AND 100000",
         "inbound_amt > 80000 AND inbound_amt <= 100000", "1/2"),
        ("A 1-5 inbound_cnt 寫成 >= 2(規格是等於 2)",
         "inbound_cnt = 2", "inbound_cnt >= 2"),
        ("A 1-6 io_ratio 下界放寬成 0.50",
         "io_ratio BETWEEN 0.60 AND 1.20", "io_ratio BETWEEN 0.50 AND 1.20", "1/2"),
        ("A 1-7 eod_balance 寫成 < 1000(規格是 <= 1000)",
         "eod_balance <= 1000", "eod_balance < 1000", "1/3"),
        # ── 條件 2(B) ──
        ("B 2-1 kiosk_outbound_cnt 下界放寬成 2",
         "kiosk_outbound_cnt BETWEEN 3 AND 4", "kiosk_outbound_cnt BETWEEN 2 AND 4"),
        ("B 2-2 kiosk_outbound_amt 上界放寬成 120000",
         "kiosk_outbound_amt BETWEEN 50000 AND 100000",
         "kiosk_outbound_amt BETWEEN 50000 AND 120000", "2/2"),
        ("B 2-3 outbound_amt 下界寫成不含端點",
         "outbound_amt BETWEEN 90000 AND 110000",
         "outbound_amt > 90000 AND outbound_amt <= 110000", "2/2"),
        ("B 2-4 inbound_amt 上界縮成 100000",
         "inbound_amt BETWEEN 80000 AND 120000", "inbound_amt BETWEEN 80000 AND 100000"),
        ("B 2-5 inbound_cnt 上界放寬成 9",
         "inbound_cnt BETWEEN 3 AND 8", "inbound_cnt BETWEEN 3 AND 9"),
        ("B 2-6 io_ratio 上界放寬成 1.30",
         "io_ratio BETWEEN 0.60 AND 1.20", "io_ratio BETWEEN 0.60 AND 1.30", "2/2"),
        ("B 2-7 eod_balance 寫成 < 1000",
         "eod_balance <= 1000", "eod_balance < 1000", "2/3"),
        # ── 條件 3(C) ──
        ("C 3-1 kiosk_outbound_cnt 下界放寬成 3",
         "kiosk_outbound_cnt BETWEEN 4 AND 10", "kiosk_outbound_cnt BETWEEN 3 AND 10"),
        ("C 3-2 kiosk_outbound_amt 下界放寬成 50000",
         "kiosk_outbound_amt BETWEEN 90000 AND 100000",
         "kiosk_outbound_amt BETWEEN 50000 AND 100000"),
        ("C 3-3 inbound_amt 上界放寬成 120000",
         "inbound_amt BETWEEN 80000 AND 100000",
         "inbound_amt BETWEEN 80000 AND 120000", "2/2"),
        ("C 3-4 inbound_cnt 上界放寬成 4",
         "inbound_cnt BETWEEN 2 AND 3", "inbound_cnt BETWEEN 2 AND 4"),
        ("C 3-5 io_ratio 下界放寬成 0.90",
         "io_ratio BETWEEN 1.00 AND 1.50", "io_ratio BETWEEN 0.90 AND 1.50"),
        ("C 3-6 eod_balance 寫成 < 1000",
         "eod_balance <= 1000", "eod_balance < 1000", "3/3"),
        # 條件之間是 OR、條件內是 AND(規格「補充」段明寫)——寫成 AND 會漏掉大量帳戶
        ("三個條件之間寫成 AND(規格是 OR)",
         "        )\n        OR\n        -- Condition B", "        )\n        AND\n        -- Condition B"),
    ],
}


def reference_sql(spec_code: str) -> str:
    case = json.loads((PKG_ROOT / "eval" / "golden" / f"{REFERENCE[spec_code]}.json")
                      .read_text(encoding="utf-8"))
    return case["files"][0]["full_content"]


def mutate(sql: str, old: str, new: str, where: str = "1/1") -> str:
    """把正確 SQL 的某一處改錯。

    `where` 是 "第幾處/共幾處":同一段門檻在條件 A/B/C 裡會重複出現
    (例如 `eod_balance <= 1000` 三個區塊都有),要能指名只改其中一處,
    才看得出「是哪一個條件沒被驗到」。`"*/2"` = 兩處都改(例如入帳的
    CASE 在 cnt 與 amt 各出現一次,只改一邊會變成兩個指標互相矛盾,
    那是測不到規格條件的假訊號)。

    「共幾處」要寫明是為了**偵測漂移**:正確 SQL 改版後出現次數變了,
    這裡會直接報錯,而不是悄悄改到別的地方(或什麼都沒改)。
    """
    nth, _, total = where.partition("/")
    got = sql.count(old)
    if got != int(total):
        raise ValueError(f"寫錯的版本對不上正確 SQL(原文出現 {got} 次,"
                         f"設定寫 {total} 次):{old!r}")
    if nth == "*":
        return sql.replace(old, new)
    i = -1
    for _ in range(int(nth)):
        i = sql.index(old, i + 1)
    return sql[:i] + new + sql[i + len(old):]


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
    # 預期值符不符只是一半:標籤檢查(每個案例的資料有沒有真的測到它宣稱的那一條)
    # 與最後的 passed 也要印,不然「10/10 符合預期」可以跟一堆標籤缺口同時成立,
    # 而凍結檔能不能用看的是後者。
    #
    # 缺口與 passed 由 spec_exec 的共用函式算:這裡自己算一份的話,正式流程多一種
    # 缺口時腳本不會跟著變,印出來的結論就跟真的不一樣。
    label_gaps = _label_gaps(ok["case_results"])
    unverified = _unverified(ok["case_results"])
    all_gaps, passed = gaps_and_passed(plan, ok, gaps, ok["mismatches"])
    print(f"正確的 SQL:{len(ok['case_results'])} 個案例全部符合預期")
    print(f"標籤檢查:{len(label_gaps)} 個案例的資料與標籤不符、"
          f"{len(unverified)} 個案例的標籤未經驗證")
    for g in label_gaps:
        print(f"  - {g}")
    for u in unverified:
        print(f"  - {u['case_id']}:{u['reason']}")
    print(f"執行驗證的結論:passed={passed}(缺口 {len(all_gaps)} 個)"
          f"{'' if passed else ' → 不得自動放行,凍結檔還不能用'}")
    for g in all_gaps:
        if g not in label_gaps:              # 標籤那幾條上面已經逐條印過
            print(f"  - {g}")
    print()

    if not passed:
        print("‼ 凍結檔本身還不能用:上面的缺口要先處理(抓不抓得到寫錯的版本另外看)\n")
    survived = []
    print("| SQL 寫錯成 | 凍結檔抓得到嗎 | 抓到的案例 |")
    print("|---|---|---|")
    for name, old, new, *rest in MUTANTS[code]:
        ex = execute_cases(mutate(sql, old, new, *rest), plan)
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
