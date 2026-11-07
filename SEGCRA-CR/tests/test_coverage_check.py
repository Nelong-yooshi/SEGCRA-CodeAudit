"""eval/coverage_check.py:凍結測資的覆蓋檢查。

真的檢查要在沙盒(SQL Server)跑。這裡用 SQLite 當替身執行案例,只拿來驗工具本身的判斷
(要素一變、有案例結果不同才算測到;跑不起來不算測到;正確的 SQL 就不符時不下結論)。
R-140 的正確 SQL 只用到 SQLite 也有的語法;執行窗的參數換成「日期 00:00:00」的字串,
跟 T-SQL 拿 DATE 比 DATETIME2 的結果一致(日期當成當天 0 點)。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import coverage_check as cc  # noqa: E402
import testdata_cache  # noqa: E402

from orchestrator.spec_exec import WIN_END, WIN_START  # noqa: E402


def sqlite_execute(sql: str, plan: dict) -> dict:
    """spec_exec.execute_cases 的替身:每個案例一個新的記憶體資料庫,有輸出列 = 命中。"""
    out = {"case_results": [], "mismatches": [], "sql_error": None,
           "testdata_error": None, "sandbox_error": None}
    body = (sql.strip().rstrip(";")
            .replace("@start_date", f"'{WIN_START} 00:00:00'")
            .replace("@end_date", f"'{WIN_END} 00:00:00'"))
    for case in plan["cases"]:
        con = sqlite3.connect(":memory:")
        try:
            for ddl in plan["schema_ddl"]:
                con.execute(ddl)
            for row in case["rows"]:
                con.execute(f"INSERT INTO {row[0]} VALUES ({','.join('?' * (len(row) - 1))})", row[1:])
            try:
                rows = con.execute(body).fetchall()
            except sqlite3.Error as e:
                out["sql_error"] = str(e)
                return out
        finally:
            con.close()
        r = {"case_id": case["case_id"], "expect_flagged": case["expect_flagged"],
             "actual_flagged": len(rows) > 0}
        out["case_results"].append(r)
        if r["actual_flagged"] != r["expect_flagged"]:
            out["mismatches"].append(r)
    return out


def _r140_plan():
    return json.loads(testdata_cache._path("R-140").read_text(encoding="utf-8"))["plan"]


@pytest.mark.parametrize("code", sorted(cc.TARGETS))
def test_每個目標都套得上正確的_SQL_而且真的改了東西_互不重複(code):
    sql = cc.reference_sql(code)
    targets = cc.targets_sql(code, sql)
    assert len(targets) == len(cc.TARGETS[code])
    assert all(mutated != sql for _, _, mutated in targets)
    assert len({m for _, _, m in targets}) == len(targets)         # 沒有兩個目標改成同一份
    assert len({n for _, n, _ in targets}) == len(targets)         # 名稱也不重複
    assert {c for c, _, _ in targets} <= set(cc.CATEGORIES)


def test_R140_各類都有目標():
    cats = {c for c, *_ in cc.TARGETS["R-140"]}
    assert cats == set(cc.CATEGORIES) - {"指標計算"}               # R-140 沒有衍生指標


def test_RETAIL_每個子條件都有拿掉_反過來與兩端():
    """20 個子條件,每個:拿掉、反過來(條件兩向)+ 兩端或等於的兩側(門檻邊界)。"""
    names = [n for _, n, *_ in cc.TARGETS["RETAIL_M1_EVAL"]]
    subs = [c[0] for b in cc._RETAIL_BRANCHES for c in b]
    assert len(subs) == 20
    for cid in subs:
        mine = [n for n in names if n.startswith(cid + " ")]
        assert any(n.endswith(":拿掉") for n in mine) and any(n.endswith(":反過來") for n in mine)
        form = next(c[2] for b in cc._RETAIL_BRANCHES for c in b if c[0] == cid)
        assert len(mine) == {"between": 6, "eq": 4, "le": 4}[form], (cid, mine)


def test_RETAIL_重複出現的子條件只改它所屬的分支():
    """`eod_balance <= 1000` 三個分支都有;改錯分支的話,報告說的是另一個條件沒測到。"""
    sql = cc.reference_sql("RETAIL_M1_EVAL")
    marks = [sql.index(f"-- Condition {x}") for x in "ABC"] + [sql.index("-- ==================== 4.")]
    for _, name, mutated in cc.targets_sql("RETAIL_M1_EVAL", sql):
        cid = name.split(" ")[0]
        if not (len(cid) == 3 and cid[1] == "-"):
            continue
        at = next(i for i, (a, b) in enumerate(zip(sql, mutated)) if a != b)
        b = int(cid[0]) - 1
        assert marks[b] < at < marks[b + 1], name


def test_欄位名是別的欄位的字尾時不會改到別的欄位():
    """`outbound_amt` 是 `kiosk_outbound_amt` 的字尾。"""
    sql = cc.reference_sql("RETAIL_M1_EVAL")
    for _, name, mutated in cc.targets_sql("RETAIL_M1_EVAL", sql):
        if name.startswith(("1-3 ", "2-3 ")):
            assert mutated.count("kiosk_outbound_amt BETWEEN") == sql.count("kiosk_outbound_amt BETWEEN")


def test_正確的_SQL_在替身上全部符合預期():
    """替身本身要先對:不然下面的覆蓋數字沒有意義。"""
    ok = sqlite_execute(cc.reference_sql("R-140"), _r140_plan())
    assert ok["sql_error"] is None and ok["mismatches"] == [] and ok["case_results"]


def test_R140_凍結檔的覆蓋():
    """現在的凍結檔(模型逐筆生成)有三個要素沒有案例落在上面:清單外的代碼、
    小額門檻上面差一格(1,000.01)、時間窗的起點。其餘都測到。"""
    result = cc.coverage("R-140", _r140_plan(), cc.reference_sql("R-140"), sqlite_execute)
    assert result["reference_error"] is None
    missed = {n for _, n, s, _ in result["rows"] if s != "測到"}
    assert missed == {"多算出帳 OP21(清單外)", "小額:1,000.01 不列計(> 1000.01)", "起點不含(> @start_date)"}
    assert result["by_category"]["門檻邊界"] == (3, 4)
    assert sum(h for h, _ in result["by_category"].values()) == len(result["rows"]) - 3


def test_補上落在要素上的案例就測到():
    """「測到」看的是有沒有案例在那個要素上,所以補一個剛好在時間窗起點的案例就該轉成測到。"""
    plan = _r140_plan()
    base = next(c for c in plan["cases"] if c["case_id"] == "C1-base")
    start = dict(base, case_id="start-edge",
                 rows=[[*r[:2], f"{WIN_START} 00:00:00", *r[3:]] for r in base["rows"]])
    plan = dict(plan, cases=[*plan["cases"], start])
    result = cc.coverage("R-140", plan, cc.reference_sql("R-140"), sqlite_execute)
    row = next(r for r in result["rows"] if r[1] == "起點不含(> @start_date)")
    assert row[2] == "測到" and row[3] == ["start-edge"]


def test_改過的_SQL_跑不起來時不算測到(monkeypatch):
    sql = cc.reference_sql("R-140")

    def execute(s, plan):
        if s == sql:
            return sqlite_execute(s, plan)
        return {"mismatches": [], "sql_error": "語法錯誤"}
    result = cc.coverage("R-140", _r140_plan(), sql, execute)
    assert {s for _, _, s, _ in result["rows"]} == {"無法判斷"}
    assert all(h == 0 for h, _ in result["by_category"].values())


@pytest.mark.parametrize("bad,expected", [
    ({"sql_error": "boom", "mismatches": []}, "正確的 SQL 跑不起來"),
    ({"sandbox_error": "連不上", "mismatches": []}, "正確的 SQL 跑不起來"),
    ({"mismatches": [{"case_id": "C1-base"}]}, "正確的 SQL 就有案例不符"),
])
def test_正確的_SQL_就有問題時不下覆蓋結論(bad, expected):
    calls = []

    def execute(s, plan):
        calls.append(s)
        return bad
    result = cc.coverage("R-140", _r140_plan(), cc.reference_sql("R-140"), execute)
    assert expected in result["reference_error"] and result["rows"] == []
    assert len(calls) == 1                                  # 不再跑任何改過的版本
    assert expected in cc.report("R-140", result)


def test_報告列出每一類與沒測到的要素():
    result = cc.coverage("R-140", _r140_plan(), cc.reference_sql("R-140"), sqlite_execute)
    text = cc.report("R-140", result)
    assert "| 門檻邊界 | 3 / 4 |" in text and "| **合計** | **21 / 24** |" in text
    assert "[時間窗] 起點不含(> @start_date):沒測到" in text


def test_不認得的類別直接報錯(monkeypatch):
    monkeypatch.setitem(cc.TARGETS, "R-140", [("時間窗口", "x", "t.tx_time < @end_date", "1 = 1")])
    with pytest.raises(ValueError, match="不認得的類別"):
        cc.targets_sql("R-140", cc.reference_sql("R-140"))


def test_原文對不上正確的_SQL_時直接報錯(monkeypatch):
    """正確的 SQL 改版後目標悄悄失效(什麼都沒改到)比報錯更糟。"""
    monkeypatch.setitem(cc.TARGETS, "R-140", [("時間窗", "x", "不存在的原文", "y")])
    with pytest.raises(ValueError):
        cc.targets_sql("R-140", cc.reference_sql("R-140"))
