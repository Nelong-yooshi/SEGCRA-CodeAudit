"""三段式表名剝成純表名(#26 第 4 點)——不連資料庫。

`dbt compile` 的輸出一定帶三段式表名(`"<資料庫>"."<schema>"."<表>"`),而沙盒把測資
建在隨機命名的資料庫裡、表名是規格 DDL 的單純名稱。「規則 SQL 先 dbt compile 再送進
管線」是正式流程,所以這是真的會遇到的情況。

這裡釘住的是**剝的範圍**:剝完對得上測資的才剝,剝不對的留著讓它失敗(看得見),
會換成另一份資料的不剝。
"""
from pathlib import Path

import pytest

from orchestrator.spec_exec import (_cte_names, _ddl_tables, _ident_spans,
                                   _strip_table_qualifiers)

COMPILED = (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "dbt_compiled"
            / "sqlserver" / "mrt_RETAIL_M1.sql")


def _strip(sql, tables=("t",)):
    return _strip_table_qualifiers(sql, set(tables))


# ─────────────────── 剝哪些 ───────────────────

@pytest.mark.parametrize("sql,want", [
    ('SELECT 1 FROM "D"."dbo"."t"', "SELECT 1 FROM t"),
    ("SELECT 1 FROM [D].[dbo].[t]", "SELECT 1 FROM t"),
    ("SELECT 1 FROM D.dbo.t", "SELECT 1 FROM t"),
    ('SELECT 1 FROM "D"."dbo"."t" x JOIN "D"."dbo"."t" y ON 1=1',
     "SELECT 1 FROM t x JOIN t y ON 1=1"),
])
def test_三段式剝成純表名(sql, want):
    assert _strip(sql)[0] == want


def test_大小寫不同也剝():
    """編譯輸出與規格 DDL 的大小寫常常不一樣,而 SQL Server 預設的定序不分大小寫。"""
    out, why = _strip('SELECT 1 FROM "D"."dbo"."TXN_LOG_NET"', {"txn_log_net"})
    assert out == "SELECT 1 FROM TXN_LOG_NET" and why is None


def test_兩段式不動():
    """`dbo.t` 在沙盒本來就解得開(測資建在 dbo),動它只是多一次改寫。"""
    assert _strip("SELECT 1 FROM dbo.t")[0] == "SELECT 1 FROM dbo.t"


def test_不是測資裡的表就不剝():
    """剝錯會指到別的表,那種錯從結果看不出來;留著不剝只是查詢失敗,看得見。"""
    sql = 'SELECT 1 FROM "D"."dbo"."other"'
    assert _strip(sql)[0] == sql


def test_剝完會撞到CTE的不剝():
    """那會悄悄換成另一份資料 —— 查詢照樣跑得過,結果卻不是在驗這份 SQL。"""
    sql = 'WITH t AS (SELECT 1 c) SELECT 1 FROM "D"."dbo"."t"'
    assert _strip(sql)[0] == sql


def test_字串常值裡的點不當成表名():
    sql = """SELECT 1 FROM "D"."dbo"."t" WHERE note = 'x.y.t'"""
    assert _strip(sql)[0] == "SELECT 1 FROM t WHERE note = 'x.y.t'"


def test_註解裡的點不當成表名():
    sql = '-- 參考 "D"."dbo"."t"\nSELECT 1 FROM "D"."dbo"."t"'
    out, why = _strip(sql)
    assert out == '-- 參考 "D"."dbo"."t"\nSELECT 1 FROM t' and why is None


def test_沒有測資表名時整段不動():
    """測資還沒生出來(或規格沒有釘死 schema):沒有真值可以比,不要亂剝。"""
    sql = 'SELECT 1 FROM "D"."dbo"."t"'
    assert _strip_table_qualifiers(sql, set()) == (sql, None)


def test_切不出token時整段不動():
    """字串沒收尾之類的情況 tokenizer 會丟例外。那時不剝、也不報錯:這份 SQL 本來
    就跑不起來,錯誤交給沙盒講(它講得比我們準)。"""
    bad = "SELECT 1 FROM \"D\".\"dbo\".\"t\" WHERE x = '"
    assert _ident_spans(bad) is None, "前提:這段切不出 token"
    assert _strip(bad) == (bad, None)


# ─────────────────── 撞名:不剝,說清楚 ───────────────────

def test_不同資料庫的同名表不剝並且說明():
    """沙盒的測資只有一份,兩個來源對不起來。這時剝任何一邊都是猜的。"""
    out, why = _strip('SELECT 1 FROM "A"."dbo"."t" JOIN "B"."dbo"."t" ON 1=1')
    assert out == 'SELECT 1 FROM "A"."dbo"."t" JOIN "B"."dbo"."t" ON 1=1'
    assert why and "同名表" in why and "需人工執行驗證" in why


def test_同一個資料庫的不同schema也算撞名():
    out, why = _strip('SELECT 1 FROM "D"."a"."t" JOIN "D"."b"."t" ON 1=1')
    assert why and "t" in why
    assert out.count('"D"') == 2, "不剝"


# ─────────────────── 規格 DDL → 表名 ───────────────────

@pytest.mark.parametrize("ddl,want", [
    ("CREATE TABLE transactions(id INT);", {"transactions"}),
    ("CREATE TABLE dbo.transactions(id INT);", {"transactions"}),
    ("CREATE TABLE [db].[dbo].[transactions](id INT);", {"transactions"}),
    ('CREATE TABLE "dbo"."transactions"(id INT);', {"transactions"}),
    ("create table TxnLog(id INT);", {"txnlog"}),
    ("SELECT 1;", set()),                      # 不是建表的不算
])
def test_規格DDL取出表名(ddl, want):
    assert _ddl_tables([ddl]) == want


def test_DDL不是字串也不會爆():
    assert _ddl_tables(["CREATE TABLE t(id INT);", None, 3, {}]) == {"t"}


# ─────────────────── 真的編譯輸出 ───────────────────

def test_真的編譯輸出剝完沒有限定名():
    tables = {"txn_log_net", "retail_m1_earlyjob"}
    out, why = _strip_table_qualifiers(COMPILED.read_text(encoding="utf-8"), tables)
    assert why is None
    assert '"SAMPLE_DW"' not in out
    assert "FROM txn_log_net" in out and "FROM retail_m1_earlyjob e" in out


def test_編譯輸出的CTE名字都認得出來():
    """CTE 認不出來的話,剛好與測資同名的那個 CTE 會被換掉。"""
    assert _cte_names(COMPILED.read_text(encoding="utf-8")) == {
        "t_txn_base", "t_txn_agg1", "t_txn_agg2", "t_txn_full", "t_txn_final"}


# ─────────────────── 串進 execute_cases ───────────────────
#
# 剝這件事在 execute_cases 裡做(測資有哪些表只有它知道)。這兩個測試用假的 pymssql
# 把整條路跑完,看送進沙盒的到底是哪一份 SQL。

import sys          # noqa: E402
import types        # noqa: E402

from orchestrator import spec_exec  # noqa: E402

_PLAN = {"schema_ddl": ["CREATE TABLE t(id INT);"],
         "conditions": [],
         "cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
                    "rows": [["t", 1]], "expect_flagged": True}]}


def _fake_sandbox(monkeypatch, sent):
    """假的 pymssql:把每一句執行過的 SQL 記進 sent。"""
    class Cursor:
        def __init__(self):
            self.description = None

        def execute(self, sql, params=None):
            sent.append(sql)
            self.description = [("col",)] if sql.lstrip().upper().startswith("SELECT") else None

        def fetchone(self):
            return ("16.0.1000.6", "Developer Edition")

        def fetchall(self):
            return [(1,)]

        def close(self):
            pass

    class Con:
        def __init__(self, **kw):
            pass

        def cursor(self):
            return Cursor()

        def rollback(self):
            pass

        def close(self):
            pass

    fake = types.ModuleType("pymssql")
    fake.connect = lambda **kw: Con(**kw)
    monkeypatch.setitem(sys.modules, "pymssql", fake)
    monkeypatch.setattr(spec_exec, "_sandbox_params",
                        lambda: {"server": "x", "port": 1433, "user": "sa", "password": "pw"})
    monkeypatch.setattr(spec_exec, "_drop_stale_sandbox_dbs", lambda admin: None)


def test_送進沙盒的SQL已經剝掉限定詞(monkeypatch):
    sent = []
    _fake_sandbox(monkeypatch, sent)
    out = spec_exec.execute_cases('SELECT 1 FROM "D"."dbo"."t"', _PLAN)
    assert out["table_error"] is None and out["sql_error"] is None
    ran = [s for s in sent if "SELECT 1 FROM" in s]   # 受審 SQL 那一句
    assert ran and all('"D"' not in s for s in ran), ran


def test_撞名時不連沙盒(monkeypatch):
    """跑出來的結果不是在驗這份 SQL,所以連都不要連。"""
    sent = []
    _fake_sandbox(monkeypatch, sent)
    out = spec_exec.execute_cases('SELECT 1 FROM "A"."dbo"."t" JOIN "B"."dbo"."t" ON 1=1', _PLAN)
    assert out["table_error"] and "同名表" in out["table_error"]
    assert out["case_results"] == [] and sent == [], "一句都不該送出去"
    assert out["engine"] is None


def test_撞名回報成表名對不上_不是SQL有錯(monkeypatch):
    """說成「SQL 無法在測資上執行」會讓人去改沒問題的 SQL —— 這是我們驗不了,
    不是程式寫錯。"""
    import asyncio

    async def fake_generate(cfg, code, text, profile_name=None):
        return ({"schema_ddl": ["CREATE TABLE t(id INT);"],
                 "conditions": [{"id": "C1", "desc": "x"}],
                 "cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
                            "rows": [["t", 1]], "expect_flagged": True}]}, [], [])

    def fake_execute(sql, plan, deadline=None):
        return {"case_results": [], "mismatches": [], "sql_error": None,
                "testdata_error": None, "sandbox_error": None,
                "table_error": "受審 SQL 用了不同資料庫/schema 下的同名表(t)…",
                "engine": None, "timed_out": False}

    monkeypatch.setattr(spec_exec, "generate_cases", fake_generate)
    monkeypatch.setattr(spec_exec, "execute_cases", fake_execute)
    mr = {"files": [{"path": "models/a.sql", "full_content": "SELECT 1;"}]}
    r = asyncio.run(spec_exec.run_spec_exec(None, None, mr, "R-201", "規格內容"))
    assert r["passed"] is False
    titles = [f["title"] for f in r["findings"]]
    assert "執行驗證無法完成(表名無法對應沙盒的測資)" in titles, titles
    assert all("SQL 無法在測資上執行" not in t for t in titles)
    assert r["table_error"]
    assert [f["severity"] for f in r["findings"] if "表名" in f["title"]] == ["major"]


# ─────────────────── 跑批前的探針 ───────────────────
#
# 剝得對不對只有真的 SQL Server 驗得出來(單元測試只看得到字串),所以 eval/preflight.py
# 多一條探針:同一份最小案例,SQL 的表名寫成編譯輸出的形狀。

def _preflight():
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "eval"))
    import preflight
    return preflight


def test_探針用的是編譯輸出的形狀():
    pf = _preflight()
    assert '"PROBE_DW"."dbo"."preflight_probe"' in pf.PROBE_SQL_QUALIFIED
    # 探針的表就是那份最小 plan 建的表 —— 不然剝完一樣對不上
    assert "preflight_probe" in _ddl_tables(pf.PROBE_PLAN["schema_ddl"])


def test_探針送進沙盒的SQL已經剝乾淨(monkeypatch):
    """這裡只驗「送出去的是什麼」。一正一反比對得對不對要真的 SQL Server —— 假沙盒
    一律回傳列,反向案例必然不符;探針本身就是為了在真機上驗那件事而存在。"""
    sent = []
    _fake_sandbox(monkeypatch, sent)
    chk = _preflight().check_table_names()
    assert chk.data["table_error"] is None and chk.data["sql_error"] is None
    ran = [s for s in sent if "amt > 100" in s]       # 受審 SQL 那一句(前面帶 SET/DECLARE)
    assert ran and all('"PROBE_DW"' not in s for s in ran), ran


def test_沙盒不可用時探針是skip不是fail(monkeypatch):
    """那是環境問題,不是剝的邏輯壞了 —— 報成 fail 會讓人去查錯的地方。"""
    pf = _preflight()
    monkeypatch.setattr(spec_exec, "_sandbox_params",
                        lambda: {"server": "x", "port": 1433, "user": "sa", "password": ""})
    assert pf.check_table_names().level == "skip"
