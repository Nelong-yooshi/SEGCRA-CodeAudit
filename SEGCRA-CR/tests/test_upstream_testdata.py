"""上游由測資代替時,模型產生的測資沒放齊表。

真模型實測(gemma,沿 ref() 推出 4 個上游 + 1 張原始表)看到兩種情況:
  1. schema_ddl 漏列規格裡的一張表 → SQL 讀到沙盒沒有的表,整份執行驗證做不了
  2. 資料只放進看起來像明細的表,規則直接讀的加總表是空的 → 正確的 SQL 也「應命中卻沒命中」
這裡驗程式自己補上這兩處(不靠模型):
  - schema_ddl 照規格補齊漏列的表
  - 應命中卻沒命中、SQL 讀的表在案例裡沒有資料 → 剔除、列覆蓋缺口,不交給仲裁、不報不符
  - 沒有上游代替時行為不變
"""
import asyncio

import pytest

from orchestrator import spec_exec
from orchestrator.config import load_config

SPEC = ("# DEMO 規則\n\n## 資料表定義\n\n```sql\n"
        "CREATE TABLE [CUST_RAW] ([ACCT_NO] VARCHAR(12), [CUST_LEVEL] CHAR(1));\n```\n\n```sql\n"
        "CREATE TABLE [dbo].[stg_txn] ([ACCT_NO] varchar(12), [TXN_AMT] decimal(16,2));\n"
        "CREATE TABLE [dbo].[int_daily] ([ACCT_NO] varchar(12), [DAY_AMT] decimal(38,2));\n"
        "CREATE TABLE [dbo].[snap_cust] ([ACCT_NO] varchar(12), [CUST_LEVEL] char(1), "
        "[dbt_valid_to] datetime2(7));\n"
        "CREATE TABLE [dbo].[rule_x0_earlyjob] ([ACCT_NO] varchar(12));\n```\n\n"
        "## 需求項目\n1. 當日 ATM 提領累計達 100000 且客戶等級為 A,通報\n")
SQL = ("WITH d AS (SELECT ACCT_NO, DAY_AMT FROM [dbo].[int_daily]),\n"
       "lv AS (SELECT ACCT_NO, CUST_LEVEL FROM [dbo].[snap_cust] WHERE dbt_valid_to IS NULL),\n"
       "ej AS (SELECT ACCT_NO FROM [dbo].[rule_x0_earlyjob]),\n"
       "r AS (SELECT DISTINCT ACCT_NO FROM [dbo].[stg_txn]),\n"
       "c AS (SELECT ACCT_NO FROM [dbo].[CUST_RAW])\n"
       "SELECT d.ACCT_NO FROM d JOIN lv ON lv.ACCT_NO = d.ACCT_NO JOIN r ON r.ACCT_NO = d.ACCT_NO\n"
       "JOIN c ON c.ACCT_NO = d.ACCT_NO\n"
       "WHERE d.DAY_AMT >= 100000 AND lv.CUST_LEVEL = 'A'\n"
       "  AND NOT EXISTS (SELECT 1 FROM ej WHERE ej.ACCT_NO = d.ACCT_NO)")
# 模型實際產生的形狀:四張上游有列、原始表 CUST_RAW 漏列;資料只放 snap_cust 與 stg_txn
MODEL_DDL = ["CREATE TABLE [dbo].[stg_txn] ([ACCT_NO] varchar(12), [TXN_AMT] decimal(16,2))",
             "CREATE TABLE [dbo].[int_daily] ([ACCT_NO] varchar(12), [DAY_AMT] decimal(38,2))",
             "CREATE TABLE [dbo].[snap_cust] ([ACCT_NO] varchar(12), [CUST_LEVEL] char(1), "
             "[dbt_valid_to] datetime2(7))",
             "CREATE TABLE [dbo].[rule_x0_earlyjob] ([ACCT_NO] varchar(12))"]
PARTIAL = [["snap_cust", "A1", "A", None], ["stg_txn", "A1", 100000]]
FULL = [["int_daily", "A1", 100000], ["snap_cust", "A1", "A", None], ["stg_txn", "A1", 100000],
        ["CUST_RAW", "A1", "A"]]
STAND_INS = ("int_daily", "snap_cust", "rule_x0_earlyjob", "stg_txn")


def _mm(case_id, expect, actual, rows, direction="true"):
    return {"case_id": case_id, "condition_id": "C1", "direction": direction, "note": "",
            "expect_flagged": expect, "actual_flagged": actual, "rows": rows}


# ─────────────────── schema_ddl 照規格補齊 ───────────────────

def test_模型漏列的表照規格補上():
    plan = spec_exec._with_spec_ddl({"schema_ddl": list(MODEL_DDL), "cases": []}, SPEC)
    assert spec_exec._ddl_tables(plan["schema_ddl"]) == {
        "cust_raw", "stg_txn", "int_daily", "snap_cust", "rule_x0_earlyjob"}
    assert plan["schema_ddl"][:4] == MODEL_DDL                     # 模型列的照留、順序不變


def test_都列了就不動():
    full = MODEL_DDL + ["CREATE TABLE [CUST_RAW] ([ACCT_NO] VARCHAR(12), [CUST_LEVEL] CHAR(1))"]
    plan = {"schema_ddl": full, "cases": []}
    assert spec_exec._with_spec_ddl(plan, SPEC) is plan


def test_規格解析不了時照原樣():
    plan = {"schema_ddl": list(MODEL_DDL), "cases": []}
    bad = "# X\n\n## 資料表定義\n\n```sql\nCREATE TABLE t (a INT\n```\n"
    assert spec_exec._with_spec_ddl(plan, bad) is plan


# ─────────────────── 讀的表沒有資料時不算不符 ───────────────────

def _ddl(*names):
    return {"schema_ddl": [f"CREATE TABLE [{n}] ([A] INT)" for n in names]}


def test_一定要有資料的表_沿_CTE_找_排除名單不算():
    plan = {"schema_ddl": MODEL_DDL + ["CREATE TABLE [CUST_RAW] ([ACCT_NO] VARCHAR(12))",
                                       "CREATE TABLE [unused] ([X] INT)"]}
    # rule_x0_earlyjob 只在 NOT EXISTS 裡:排除名單本來就該是空的
    assert spec_exec._required_tables(SQL, plan) == {"cust_raw", "stg_txn", "int_daily", "snap_cust"}


@pytest.mark.parametrize("sql, want", [
    ("SELECT a.A FROM a JOIN b ON a.A = b.A LEFT JOIN c ON c.A = a.A", {"a", "b"}),
    ("SELECT a.A FROM a CROSS JOIN b", {"a", "b"}),
    ("SELECT a.A FROM a RIGHT JOIN b ON a.A = b.A JOIN c ON c.A = b.A", {"b", "c"}),
    ("SELECT a.A FROM a FULL JOIN b ON a.A = b.A", set()),
    ("SELECT s.A FROM (SELECT A FROM a JOIN b ON a.A = b.A) s", {"a", "b"}),
    ("WITH x AS (SELECT A FROM a), y AS (SELECT A FROM x) SELECT A FROM y", {"a"}),
    ("SELECT A FROM a WHERE A IN (SELECT A FROM b) AND EXISTS (SELECT 1 FROM c)", {"a"}),
    ("SELECT A FROM a UNION ALL SELECT A FROM b", set()),
    ("SELECT A FROM [dbo].[a] JOIN dbo.b ON 1 = 1", {"a", "b"}),
    ("SELECT A FROM a JOIN other_table ON 1 = 1", {"a"}),          # 不是測資表的不算
    ("SELECT FROM WHERE (((", set()),                              # 解析不了:不剔除
])
def test_一定要有資料的表_各種寫法(sql, want):
    assert spec_exec._required_tables(sql, _ddl("a", "b", "c")) == want


def test_表名跟_CTE_同名時_帶_schema_的是表():
    sql = "WITH a AS (SELECT A FROM b) SELECT x.A FROM [dbo].[a] x JOIN a ON 1 = 1"
    assert spec_exec._required_tables(sql, _ddl("a", "b", "c")) == {"a", "b"}


def test_應命中的案例缺了讀的表_剔除並寫出是哪幾張():
    plan = spec_exec._with_spec_ddl({"schema_ddl": list(MODEL_DDL)}, SPEC)
    kept, empty = spec_exec._empty_read_misses(SQL, plan, [_mm("T", True, False, PARTIAL)])
    assert kept == []
    ((mm, missing),) = empty
    assert mm["case_id"] == "T" and missing == ["cust_raw", "int_daily"]


def test_一定要有的表都有_排除名單是空的_應命中卻沒命中照樣交給仲裁():
    """真模型實測:資料放齊、排除名單空著,埋錯(>= 改 >)的 SQL 在剛好達門檻的案例沒命中——那是真的錯。"""
    plan = spec_exec._with_spec_ddl({"schema_ddl": list(MODEL_DDL)}, SPEC)
    mm = _mm("T", True, False, FULL)
    kept, empty = spec_exec._empty_read_misses(SQL, plan, [mm])
    assert kept == [mm] and empty == []


@pytest.mark.parametrize("mm", [
    _mm("全放了", True, False, FULL),                                     # 一定要有的都有:交給仲裁
    _mm("不應命中卻命中", False, True, PARTIAL, "false"),               # 不是這一種
    _mm("不應命中", False, False, PARTIAL, "false"),
])
def test_其他的不符照樣交給仲裁(mm):
    plan = spec_exec._with_spec_ddl({"schema_ddl": list(MODEL_DDL)}, SPEC)
    kept, empty = spec_exec._empty_read_misses(SQL, plan, [mm])
    assert kept == [mm] and empty == []


def test_表名帶_schema_或方括號也認得():
    plan = spec_exec._with_spec_ddl({"schema_ddl": list(MODEL_DDL)}, SPEC)
    rows = [["[dbo].[int_daily]", "A1", 1], ["dbo.snap_cust", "A1", "A", None], ["[stg_txn]", "A1", 1],
            ["CUST_RAW", "A1", "A"], ["rule_x0_earlyjob", "Z"]]
    kept, empty = spec_exec._empty_read_misses(SQL, plan, [_mm("T", True, False, rows)])
    assert empty == []


# ─────────────────── 整條 run_spec_exec ───────────────────

def _run(monkeypatch, stand_ins, rows=PARTIAL, plan_ddl=None):
    seen = {"executed_ddl": None, "arbitrated": []}
    plan = {"schema_ddl": list(plan_ddl or MODEL_DDL),
            "conditions": [{"id": "C1", "desc": "累計達 100000 且等級 A"}],
            "cases": [{"case_id": "T", "condition_id": "C1", "direction": "true",
                       "rows": rows, "expect_flagged": True},
                      {"case_id": "F", "condition_id": "C1", "direction": "false",
                       "rows": rows, "expect_flagged": False}]}

    async def generate(cfg, code, text, profile_name=None):
        return plan, [], []

    def execute(sql, plan_, deadline=None):
        seen["executed_ddl"] = list(plan_["schema_ddl"])
        return {"engine": "stub", "sandbox_error": None, "testdata_error": None, "sql_error": None,
                "case_results": [{"case_id": "T", "ok": False}, {"case_id": "F", "ok": True}],
                "mismatches": [_mm("T", True, False, rows)]}

    async def arbitrate(cfg, spec_text, sql, mm, profile_name=None):
        seen["arbitrated"].append(mm["case_id"])
        return {"who_is_wrong": "sql", "reason": "實作沒有命中"}

    monkeypatch.setattr(spec_exec, "generate_cases", generate)
    monkeypatch.setattr(spec_exec, "execute_cases", execute)
    monkeypatch.setattr(spec_exec, "arbitrate", arbitrate)
    mr = {"files": [{"path": "models/P02/mrt_demo.sql", "full_content": SQL}]}
    result = asyncio.run(spec_exec.run_spec_exec(load_config(), None, mr, "DEMO", SPEC,
                                                 stand_ins=stand_ins))
    return result, seen


def test_上游代替時_沒放齊的案例剔除_不報不符_也不交給仲裁(monkeypatch):
    result, seen = _run(monkeypatch, STAND_INS)
    assert "cust_raw" in spec_exec._ddl_tables(seen["executed_ddl"])         # 漏列的表補上了
    assert seen["arbitrated"] == []
    titles = {f["title"] for f in result["findings"]}
    assert spec_exec.STAND_IN_MISMATCH_TITLE not in titles
    assert "執行驗證失敗:實作與規格行為不符" not in titles
    (gap,) = [g for g in result["coverage_gaps"] if "案例 T" in g]
    assert "int_daily" in gap and "測資可能沒放齊" in gap
    assert any(d["case_id"] == "T" and d["by"] == "empty-read" for d in result["dropped_cases"])
    assert result["passed"] is False                                         # 該向沒驗到


def test_上游代替時_表都有資料的不符照舊交給仲裁(monkeypatch):
    result, seen = _run(monkeypatch, STAND_INS, rows=FULL + [["rule_x0_earlyjob", "Z9"]])
    assert seen["arbitrated"] == ["T"]
    assert spec_exec.STAND_IN_MISMATCH_TITLE in {f["title"] for f in result["findings"]}


def test_沒有上游代替時_不補表也不剔除(monkeypatch):
    result, seen = _run(monkeypatch, ())
    assert "cust_raw" not in spec_exec._ddl_tables(seen["executed_ddl"])
    assert seen["arbitrated"] == ["T"]
    assert "執行驗證失敗:實作與規格行為不符" in {f["title"] for f in result["findings"]}


# ─────────────────── 資料列的表名去掉 dbo. ───────────────────

@pytest.mark.parametrize("name, want", [
    ("dbo.stg_txn", "stg_txn"), ("[dbo].[stg_txn]", "stg_txn"), ("[stg_txn]", "stg_txn"),
    ("DBO.stg_txn", "stg_txn"), (" dbo.stg_txn ", "stg_txn"), ("stg_txn", "stg_txn"),
    ("other.stg_txn", "other.stg_txn"), ("dbo.x;drop", "dbo.x;drop"), ("a.b.c", "a.b.c"),
])
def test_資料列的表名去掉_dbo(name, want):
    assert spec_exec._bare_row_tables({"rows": [[name, 1], "壞的列"]})["rows"] == [[want, 1], "壞的列"]


def test_表名寫成_dbo_的案例不再整個丟掉():
    plan = {"schema_ddl": ["CREATE TABLE [dbo].[stg_txn] ([ACCT_NO] varchar(12), [TXN_AMT] decimal(16,2))"],
            "conditions": [{"id": "C1", "desc": "金額達門檻", "branch": "C1"}],
            "cases": [{"case_id": "B", "kind": "base", "branch": "C1",
                       "rows": [["dbo.stg_txn", "A1", 100000]]}]}
    cleaned, _gaps, dropped = spec_exec._shape_check(plan)
    assert dropped == [] and cleaned["cases"][0]["rows"] == [["stg_txn", "A1", 100000]]


@pytest.mark.parametrize("c", [None, {"case_id": "x"}, {"rows": "不是清單"}])
def test_不是預期形狀的案例照原樣(c):
    assert spec_exec._bare_row_tables(c) == c
