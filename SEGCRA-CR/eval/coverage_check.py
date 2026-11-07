#!/usr/bin/env python3
"""凍結測資的覆蓋檢查:規格的每個要素,有沒有一個案例在它一變的時候結果就不同。

  python eval/coverage_check.py R-140

「測到」的定義:**有一個案例,在這個要素一變的時候結果就會不同。** 做法是把正確的 SQL
只改這一個要素(拿掉一個代碼、門檻往旁邊挪一格、時間窗的端點含不含…),用凍結的案例在
沙盒跑:只要有一個案例的結果跟預期不同,這個要素就算測到;全部照舊,就是沒有案例落在這個
要素上,寫錯了也看不出來。

跟測資怎麼生成的無關:只讀凍結檔裡的案例(資料列與預期),模型逐筆寫的、程式照結構產生的
都能檢查,所以可以拿現在的凍結檔當對照基準。

覆蓋目標照這幾類列(每一類各自統計):

  條件兩向   每條篩選與每個子條件拿掉一次(要有「只有它不成立」的案例)、反過來一次
             (要有「它成立」的案例);分支之間的 OR 寫成 AND
  排除條件   每條排除條件拿掉一次、反過來一次
  清單代碼   清單裡每個代碼各拿掉一次;清單外的代碼加進去一次
  門檻邊界   每個門檻的兩端各改成不含端點、各往外挪一格也算進來
  NULL 語意  排除條件的 NULL 照算拿掉(NULL 要列計)
  時間窗     拿掉時間窗(窗外的資料)、起點與終點的含不含各改一次;排除名單的比對日
  指標計算   衍生指標的算法(最後一筆怎麼排序、比率的除零與進位)

報告開頭印出凍結檔記的生成方式(`testdata_cache.generation_label`),A、C 兩種測資才比得起來。

需要 MS SQL 沙盒(config/sandbox.env),不呼叫模型。正確的 SQL 取自正向對照的 golden case
(`check_frozen_testdata.REFERENCE`)。新規則要用時,在 TARGETS 加一組。

**等價的改法不列**:改了之後對任何資料結果都一樣的,不是測資的問題。例如金額有兩位小數時,
`>= 500000.01` 跟 `> 500000` 等價,所以門檻的下緣用 `>= 499999.99`。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))

from check_frozen_testdata import REFERENCE, mutate, reference_sql  # noqa: E402
import testdata_cache  # noqa: E402 - 與本檔同目錄(eval/)

CATEGORIES = ("條件兩向", "排除條件", "清單代碼", "門檻邊界", "NULL 語意", "時間窗", "指標計算")

# 規則碼 → [(類別, 要素, 正確 SQL 裡的原文, 改成[, 第幾處/共幾處])]
#   條件兩向 / 排除條件:拿掉 = 要有「只有它不成立」的案例;反過來 = 要有「它成立」的案例
TARGETS = {
    "R-140": [
        ("條件兩向", "1 交易範圍:拿掉", "WHERE t.op_code IN ('OP01','OP02','OP03')\n  AND ", "WHERE "),
        ("條件兩向", "1 交易範圍:反過來", "t.op_code IN ('OP01','OP02','OP03')",
         "t.op_code NOT IN ('OP01','OP02','OP03')"),
        ("條件兩向", "2 現金註記:拿掉", "  AND t.cash_flag = 'Y'\n", ""),
        ("條件兩向", "2 現金註記:反過來", "AND t.cash_flag = 'Y'", "AND NOT (t.cash_flag = 'Y')"),
        ("條件兩向", "7 門檻:拿掉", "HAVING SUM(t.amount) >= 500000", "HAVING 1 = 1"),
        ("排除條件", "3 排除沖銷:拿掉", "  AND COALESCE(t.rev_flag, 'N') <> 'Y'\n", ""),
        ("排除條件", "3 排除沖銷:反過來", "COALESCE(t.rev_flag, 'N') <> 'Y'",
         "COALESCE(t.rev_flag, 'N') = 'Y'"),
        ("排除條件", "4 排除小額:拿掉", "  AND t.amount > 1000\n", ""),
        ("排除條件", "4 排除小額:反過來", "AND t.amount > 1000", "AND t.amount <= 1000"),
        ("排除條件", "5 排除行銷撥入:拿掉", "  AND COALESCE(t.channel_name, '') <> 'PROMO'\n", ""),
        ("排除條件", "5 排除行銷撥入:反過來", "COALESCE(t.channel_name, '') <> 'PROMO'",
         "COALESCE(t.channel_name, '') = 'PROMO'"),
        ("清單代碼", "入帳少 OP01", "IN ('OP01','OP02','OP03')", "IN ('OP02','OP03')"),
        ("清單代碼", "入帳少 OP02", "IN ('OP01','OP02','OP03')", "IN ('OP01','OP03')"),
        ("清單代碼", "入帳少 OP03", "IN ('OP01','OP02','OP03')", "IN ('OP01','OP02')"),
        ("清單代碼", "多算出帳 OP21(清單外)", "IN ('OP01','OP02','OP03')",
         "IN ('OP01','OP02','OP03','OP21')"),
        ("門檻邊界", "小額:恰 1,000 也列計(>= 1000)", "t.amount > 1000", "t.amount >= 1000"),
        ("門檻邊界", "小額:1,000.01 不列計(> 1000.01)", "t.amount > 1000", "t.amount > 1000.01"),
        ("門檻邊界", "總額:恰 500,000 不通報(> 500000)", "SUM(t.amount) >= 500000", "SUM(t.amount) > 500000"),
        ("門檻邊界", "總額:499,999.99 也通報(>= 499999.99)", "SUM(t.amount) >= 500000",
         "SUM(t.amount) >= 499999.99"),
        ("NULL 語意", "rev_flag 為 NULL 不列計", "COALESCE(t.rev_flag, 'N') <> 'Y'", "t.rev_flag <> 'Y'"),
        ("NULL 語意", "channel_name 為 NULL 不列計", "COALESCE(t.channel_name, '') <> 'PROMO'",
         "t.channel_name <> 'PROMO'"),
        ("時間窗", "拿掉時間窗(窗外的資料也算)",
         "  AND t.tx_time >= @start_date AND t.tx_time < @end_date\n", ""),
        ("時間窗", "起點不含(> @start_date)", "t.tx_time >= @start_date", "t.tx_time > @start_date"),
        ("時間窗", "終點也含(<= @end_date)", "t.tx_time < @end_date", "t.tx_time <= @end_date"),
    ],
}

# RETAIL_M1_EVAL 的三個條件(分支之間 OR、分支內 AND),順序與正確 SQL 裡的 A/B/C 相同。
# (編號, 欄位, 形式, 下界, 上界, 一格):形式 "between" 含兩端、"eq" 等於、"le" 小於等於。
# 金額在正確 SQL 裡先轉成整數,所以一格是 1;比率四捨五入到小數 2 位,一格是 0.01。
_RETAIL_BRANCHES = [
    [("1-1", "kiosk_outbound_cnt", "between", 2, 3, 1),
     ("1-2", "kiosk_outbound_amt", "between", 50000, 100000, 1),
     ("1-3", "outbound_amt", "between", 90000, 110000, 1),
     ("1-4", "inbound_amt", "between", 80000, 100000, 1),
     ("1-5", "inbound_cnt", "eq", 2, 2, 1),
     ("1-6", "io_ratio", "between", 0.60, 1.20, 0.01),
     ("1-7", "eod_balance", "le", None, 1000, 1)],
    [("2-1", "kiosk_outbound_cnt", "between", 3, 4, 1),
     ("2-2", "kiosk_outbound_amt", "between", 50000, 100000, 1),
     ("2-3", "outbound_amt", "between", 90000, 110000, 1),
     ("2-4", "inbound_amt", "between", 80000, 120000, 1),
     ("2-5", "inbound_cnt", "between", 3, 8, 1),
     ("2-6", "io_ratio", "between", 0.60, 1.20, 0.01),
     ("2-7", "eod_balance", "le", None, 1000, 1)],
    [("3-1", "kiosk_outbound_cnt", "between", 4, 10, 1),
     ("3-2", "kiosk_outbound_amt", "between", 90000, 100000, 1),
     ("3-3", "inbound_amt", "between", 80000, 100000, 1),
     ("3-4", "inbound_cnt", "between", 2, 3, 1),
     ("3-5", "io_ratio", "between", 1.00, 1.50, 0.01),
     ("3-6", "eod_balance", "le", None, 1000, 1)],
]


def _num(v, step) -> str:
    """照正確 SQL 的寫法印數字:比率兩位小數,其餘整數。"""
    return f"{v:.2f}" if isinstance(step, float) else str(int(v))


def _expr(col, form, lo, hi, step) -> str:
    if form == "eq":
        return f"{col} = {_num(lo, step)}"
    if form == "le":
        return f"{col} <= {_num(hi, step)}"
    return f"{col} BETWEEN {_num(lo, step)} AND {_num(hi, step)}"


def _branch_targets(branches) -> list[tuple]:
    """每個子條件:拿掉、反過來(條件兩向),以及兩端各「不含端點」與「外一格也算」(門檻邊界)。

    原文前面加一個空白再找:`outbound_amt` 是 `kiosk_outbound_amt` 的字尾,不加的話
    會找到別的欄位。同一段在幾個分支重複出現時(例如三個分支都有 `eod_balance <= 1000`),
    用「第幾處/共幾處」指名只改其中一處,才看得出是哪一個分支的條件沒測到。
    """
    exprs = [" " + _expr(*c[1:]) for b in branches for c in b]
    seen: dict[str, int] = {}
    out = []
    for b in branches:
        for cid, col, form, lo, hi, step in b:
            old = " " + _expr(col, form, lo, hi, step)
            seen[old] = seen.get(old, 0) + 1
            where = f"{seen[old]}/{exprs.count(old)}"
            n = lambda v: _num(v, step)  # noqa: E731
            out += [("條件兩向", f"{cid} {col}:拿掉", old, " 1 = 1", where),
                    ("條件兩向", f"{cid} {col}:反過來", old, f" NOT ({old.strip()})", where)]
            if form == "between":
                out += [
                    ("門檻邊界", f"{cid} 下界 {n(lo)} 不含", old, f" {col} > {n(lo)} AND {col} <= {n(hi)}", where),
                    ("門檻邊界", f"{cid} 上界 {n(hi)} 不含", old, f" {col} >= {n(lo)} AND {col} < {n(hi)}", where),
                    ("門檻邊界", f"{cid} 下界外一格({n(lo - step)})也算", old,
                     f" {col} BETWEEN {n(lo - step)} AND {n(hi)}", where),
                    ("門檻邊界", f"{cid} 上界外一格({n(hi + step)})也算", old,
                     f" {col} BETWEEN {n(lo)} AND {n(hi + step)}", where)]
            elif form == "eq":
                out += [
                    ("門檻邊界", f"{cid} 少一({n(lo - step)})也算", old, f" {col} BETWEEN {n(lo - step)} AND {n(lo)}", where),
                    ("門檻邊界", f"{cid} 多一({n(lo + step)})也算", old, f" {col} BETWEEN {n(lo)} AND {n(lo + step)}", where)]
            else:
                out += [
                    ("門檻邊界", f"{cid} 恰 {n(hi)} 不算", old, f" {col} < {n(hi)}", where),
                    ("門檻邊界", f"{cid} 外一格({n(hi + step)})也算", old, f" {col} <= {n(hi + step)}", where)]
    return out


_IN = "op_code IN ('OP01','OP02','OP03')"
_KIOSK = "op_code IN ('OP11','OP12')"
_OUT = "op_code IN ('OP21','OP22','OP23')"
_EARLYJOB = ("WHERE NOT EXISTS (\n    SELECT 1\n    FROM retail_m1_earlyjob e\n"
             "    WHERE e.account_id = f.account_id\n"
             "    AND e.TABLE_DATE = DATEADD(DAY, -1, @start_date)\n)")

TARGETS["RETAIL_M1_EVAL"] = [
    ("條件兩向", "三個條件之間寫成 AND(規格是 OR)",
     "        )\n        OR\n        -- Condition B", "        )\n        AND\n        -- Condition B"),
    ("條件兩向", "指定入帳的方向反過來", "direction_flag = '0'", "direction_flag = '1'", "*/2"),
    ("條件兩向", "出帳的方向反過來", "direction_flag = '1'", "direction_flag = '0'", "*/3"),
    *_branch_targets(_RETAIL_BRANCHES),
    ("排除條件", "空白帳號:拿掉", "WHERE NULLIF(LTRIM(RTRIM(account_id)), '') IS NOT NULL\n    AND ", "WHERE "),
    ("排除條件", "空白帳號:反過來", "NULLIF(LTRIM(RTRIM(account_id)), '') IS NOT NULL",
     "NULLIF(LTRIM(RTRIM(account_id)), '') IS NULL"),
    ("排除條件", "空白帳號:只排除 NULL(只有空白的帳號照算)",
     "NULLIF(LTRIM(RTRIM(account_id)), '') IS NOT NULL", "account_id IS NOT NULL"),
    ("排除條件", "小額入帳:拿掉", "        AND amount > 1000\n", "", "*/2"),
    ("排除條件", "小額入帳:反過來", "AND amount > 1000", "AND amount <= 1000", "*/2"),
    ("排除條件", "EarlyJob:拿掉", _EARLYJOB, "WHERE 1 = 1"),
    ("排除條件", "EarlyJob:反過來", "WHERE NOT EXISTS (", "WHERE EXISTS ("),
    ("清單代碼", "指定入帳少 OP01", _IN, "op_code IN ('OP02','OP03')", "*/2"),
    ("清單代碼", "指定入帳少 OP02", _IN, "op_code IN ('OP01','OP03')", "*/2"),
    ("清單代碼", "指定入帳少 OP03", _IN, "op_code IN ('OP01','OP02')", "*/2"),
    ("清單代碼", "指定入帳多算 OP21(清單外)", _IN, "op_code IN ('OP01','OP02','OP03','OP21')", "*/2"),
    ("清單代碼", "自助設備出帳少 OP11", _KIOSK, "op_code IN ('OP12')", "*/2"),
    ("清單代碼", "自助設備出帳少 OP12", _KIOSK, "op_code IN ('OP11')", "*/2"),
    ("清單代碼", "自助設備出帳多算 OP21(清單外)", _KIOSK, "op_code IN ('OP11','OP12','OP21')", "*/2"),
    ("清單代碼", "指定出帳少 OP21", _OUT, "op_code IN ('OP22','OP23')"),
    ("清單代碼", "指定出帳少 OP22", _OUT, "op_code IN ('OP21','OP23')"),
    ("清單代碼", "指定出帳少 OP23", _OUT, "op_code IN ('OP21','OP22')"),
    ("清單代碼", "指定出帳多算 OP11(清單外)", _OUT, "op_code IN ('OP21','OP22','OP23','OP11')"),
    ("門檻邊界", "小額:恰 1,000 也列計(>= 1000)", "amount > 1000", "amount >= 1000", "*/2"),
    ("門檻邊界", "小額:1,001 不列計(> 1001)", "amount > 1000", "amount > 1001", "*/2"),
    ("時間窗", "拿掉交易日限制", "    AND TABLE_DATE = @start_date\n", ""),
    ("時間窗", "EarlyJob 比對同日(規格是前一日)", "DATEADD(DAY, -1, @start_date)", "@start_date"),
    ("時間窗", "EarlyJob 比對前兩日(規格是前一日)", "DATEADD(DAY, -1, @start_date)",
     "DATEADD(DAY, -2, @start_date)"),
    ("指標計算", "當日結餘取第一筆(規格是最後一筆)", "ORDER BY posted_date DESC, txn_time DESC",
     "ORDER BY posted_date ASC, txn_time ASC"),
    ("指標計算", "當日結餘不看入帳日(只依交易時間)", "ORDER BY posted_date DESC, txn_time DESC",
     "ORDER BY txn_time DESC"),
    ("指標計算", "io_ratio 分母為 0 時給 NULL(規格是 0)", "WHEN inbound_amt = 0 THEN 0",
     "WHEN inbound_amt = 0 THEN NULL"),
    ("指標計算", "io_ratio 不四捨五入到小數 2 位",
     "ROUND(CAST(outbound_amt AS FLOAT) / CAST(inbound_amt AS FLOAT), 2)",
     "CAST(outbound_amt AS FLOAT) / CAST(inbound_amt AS FLOAT)"),
]


def targets_sql(spec_code: str, sql: str) -> list[tuple[str, str, str]]:
    """[(類別, 要素, 改過的 SQL)]。原文對不上正確 SQL 時直接報錯(見 mutate)。"""
    out = []
    for category, name, old, new, *where in TARGETS[spec_code]:
        if category not in CATEGORIES:            # 打錯類別名稱的話,統計會少一類而沒人發現
            raise ValueError(f"{name}:不認得的類別 {category!r}(見 CATEGORIES)")
        out.append((category, name, mutate(sql, old, new, *where)))
    return out


def coverage(spec_code: str, plan: dict, sql: str, execute) -> dict:
    """每個要素測到了沒。execute(sql, plan) 是 spec_exec.execute_cases 的介面。

    回傳 {"reference_error": 正確 SQL 跑不起來或有案例不符時的說明,
          "rows": [(類別, 要素, 狀態, 抓到的案例)], "by_category": {類別: (測到, 總數)}}。
    狀態:"測到"、"沒測到"、"無法判斷"(改過的 SQL 跑不起來,不算測到)。
    """
    ok = execute(sql, plan)
    err = ok.get("sandbox_error") or ok.get("sql_error") or ok.get("testdata_error")
    if err:
        return {"reference_error": f"正確的 SQL 跑不起來:{err}", "rows": [], "by_category": {}}
    if ok.get("mismatches"):
        ids = ", ".join(str(m.get("case_id")) for m in ok["mismatches"])
        return {"reference_error": f"正確的 SQL 就有案例不符(案例的預期錯了):{ids}",
                "rows": [], "by_category": {}}
    rows = []
    by_category: OrderedDict[str, list[int]] = OrderedDict((c, [0, 0]) for c in CATEGORIES)
    for category, name, mutated in targets_sql(spec_code, sql):
        ex = execute(mutated, plan)
        err = ex.get("sandbox_error") or ex.get("sql_error") or ex.get("testdata_error")
        caught = [str(m.get("case_id")) for m in ex.get("mismatches") or []]
        state = "無法判斷" if err else ("測到" if caught else "沒測到")
        rows.append((category, name, state, caught))
        by_category[category][1] += 1
        if state == "測到":
            by_category[category][0] += 1
    return {"reference_error": None, "rows": rows,
            "by_category": {c: tuple(v) for c, v in by_category.items() if v[1]}}


def report(spec_code: str, result: dict, generation: str = "") -> str:
    if result["reference_error"]:
        return f"{spec_code}:{result['reference_error']}"
    lines = [f"{spec_code} 覆蓋檢查" + (f"(測資的生成方式:{generation})" if generation else ""),
             "", "| 類別 | 測到 / 總數 |", "|---|---|"]
    total = [0, 0]
    for category, (hit, n) in result["by_category"].items():
        lines.append(f"| {category} | {hit} / {n} |")
        total[0] += hit
        total[1] += n
    lines.append(f"| **合計** | **{total[0]} / {total[1]}** |")
    missed = [r for r in result["rows"] if r[2] != "測到"]
    if missed:
        lines += ["", "沒測到的要素(沒有案例落在這裡,寫錯了也看不出來):"]
        lines += [f"  - [{c}] {n}:{s}" for c, n, s, _ in missed]
    lines += ["", "| 類別 | 要素 | 結果 | 抓到的案例 |", "|---|---|---|---|"]
    lines += [f"| {c} | {n} | {s} | {', '.join(ids) or '—'} |" for c, n, s, ids in result["rows"]]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("spec_code", help="規則碼,如 R-140(要有凍結檔、REFERENCE 與 TARGETS 設定)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    code = args.spec_code
    if code not in REFERENCE or code not in TARGETS:
        print(f"{code} 沒有設定正向對照或覆蓋目標(見 check_frozen_testdata.REFERENCE 與本檔的 TARGETS)")
        return 1
    path = testdata_cache._path(code)
    if not path.exists():
        print(f"找不到凍結檔 {path}")
        return 1
    from orchestrator.spec_exec import _shape_check, execute_cases
    data = json.loads(path.read_text(encoding="utf-8"))
    plan, _, _ = _shape_check(data["plan"])
    result = coverage(code, plan, reference_sql(code), execute_cases)
    print(report(code, result, testdata_cache.generation_label(data)))
    if result["reference_error"]:
        return 1
    return 0 if all(r[2] == "測到" for r in result["rows"]) else 1


if __name__ == "__main__":
    sys.exit(main())
