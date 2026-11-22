"""三段式表名剝成 dbo.<表>——不連資料庫。

帶三段式表名(`"<資料庫>"."<schema>"."<表>"`)的 SQL 要剝成 `dbo.<表>` 才對得上沙盒的
測資(表名是規格 DDL 的單純名稱、建在 dbo)。會帶三段式的是 MR 裡本來就寫成三段式的
SQL(手寫的跨資料庫寫法,或直接提交 compile 的輸出),以及評測的 mr_701/702;dbt 樣板
由管線自己展開成 `[表]`,不會有三段式。

這裡釘住的是**剝的範圍與形式**:剝成 dbo.<表>(同名 CTE 不受影響)、T-SQL tokenizer 認得
方括號/省略 schema、對應不到一份測資的(撞名/四段式/找不到物件)記待驗證、不判 SQL 錯。
"""
from pathlib import Path

import pytest

from orchestrator.spec_exec import (_ddl_tables, _ident_spans,
                                   _strip_table_qualifiers)

COMPILED = (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "dbt_compiled"
            / "sqlserver" / "mrt_RETAIL_M1.sql")


def _strip(sql, tables=("t",)):
    return _strip_table_qualifiers(sql, set(tables))


# ─────────────────── 剝哪些(剝成 dbo.<表>,不是純 <表>)───────────────────
# 剝成兩段式 `dbo.<表>`:SQL Server 不會把它解成 CTE,沙盒的表就在 dbo,所以不必再
# 維護一份 CTE 名單(註解一夾 regex 就抓不到,反而把同名引入判成 SQL 錯)。

@pytest.mark.parametrize("sql,want", [
    ('SELECT 1 FROM "D"."dbo"."t"', "SELECT 1 FROM dbo.t"),
    ("SELECT 1 FROM D.dbo.t", "SELECT 1 FROM dbo.t"),
    ("SELECT 1 FROM D..t", "SELECT 1 FROM dbo.t"),          # 省略 schema(T-SQL 允許)
    ('SELECT 1 FROM "D"."dbo"."t" x JOIN "D"."dbo"."t" y ON 1=1',
     "SELECT 1 FROM dbo.t x JOIN dbo.t y ON 1=1"),
])
def test_三段式剝成dbo表名(sql, want):
    assert _strip(sql)[0] == want


@pytest.mark.parametrize("sql,want", [
    # 方括號識別字(有空白、跳脫的 ]):要修的 3,T-SQL tokenizer 整段認得出來;表名
    # 本身是單純識別字時裸寫 dbo.t
    ("SELECT 1 FROM [my db].[dbo].[t]", "SELECT 1 FROM dbo.t"),
    ("SELECT 1 FROM [d]]b].[dbo].[t]", "SELECT 1 FROM dbo.t"),
    # 表名本身含空白 → 用方括號包回去
    ("SELECT 1 FROM [db].[dbo].[my t]", "SELECT 1 FROM dbo.[my t]"),
])
def test_方括號識別字也剝得對(sql, want):
    assert _strip(sql, ("t", "my t"))[0] == want


def test_四段式記待驗證():
    """四段式(跨 linked server)對應不到沙盒的一份測資 → 不剝、記待驗證,不是說 SQL 有錯。"""
    out, why = _strip("SELECT 1 FROM srv.db.dbo.t")
    assert out == "SELECT 1 FROM srv.db.dbo.t"
    assert why and "四段式" in why and "需人工執行驗證" in why


def test_大小寫不同也剝():
    """編譯輸出與規格 DDL 的大小寫常常不一樣,而 SQL Server 預設的定序不分大小寫。"""
    out, why = _strip('SELECT 1 FROM "D"."dbo"."TXN_LOG_NET"', {"txn_log_net"})
    assert out == "SELECT 1 FROM dbo.TXN_LOG_NET" and why is None


def test_兩段式不動():
    """`dbo.t` 在沙盒本來就解得開(測資建在 dbo),動它只是多一次改寫。"""
    assert _strip("SELECT 1 FROM dbo.t")[0] == "SELECT 1 FROM dbo.t"


def test_不是測資裡的表就不剝():
    """剝錯會指到別的表,那種錯從結果看不出來;留著不剝只是查詢失敗,看得見。"""
    sql = 'SELECT 1 FROM "D"."dbo"."other"'
    assert _strip(sql)[0] == sql


def test_同名CTE的三段式剝成dbo表名():
    """要修的 2:剝成 `dbo.t` 之後,就算有同名 CTE `t`,`FROM dbo.t` 讀的仍是沙盒的表。
    CTE 名字前夾註解也不影響(不再用 regex 抓 CTE 名)。"""
    # 同名 CTE + 三段式引用 → 剝成 dbo.t(讀真實表),CTE 的無限定引用留著
    sql = 'WITH t AS (SELECT 1 c) SELECT 1 FROM "D"."dbo"."t"'
    assert _strip(sql)[0] == 'WITH t AS (SELECT 1 c) SELECT 1 FROM dbo.t'
    # CTE 名字前有行註解:regex 版會抓不到 CTE、把 FROM txn 當 CTE;現在剝成 dbo.txn
    sql2 = ("WITH a AS (SELECT 1 c),\n-- 第二步\ntxn AS (SELECT 1 id)\n"
            "SELECT * FROM db.dbo.txn")
    assert _strip(sql2, ("txn",))[0].endswith("FROM dbo.txn")
    # CTE 名字後夾區塊註解
    sql3 = "WITH txn /* 篩過 */ AS (SELECT 1 id) SELECT * FROM db.dbo.txn"
    assert _strip(sql3, ("txn",))[0].endswith("FROM dbo.txn")


def test_CTE內部引用真實表也剝():
    """`WITH txn AS (SELECT * FROM "DW"."dbo"."txn") SELECT * FROM txn`:CTE 裡面引用的
    三段式真實表要剝成 dbo.txn,外層無限定的 FROM txn 是 CTE、留著。"""
    sql = 'WITH txn AS (SELECT * FROM "DW"."dbo"."txn") SELECT * FROM txn'
    out, why = _strip(sql, ("txn",))
    assert why is None
    assert 'FROM dbo.txn)' in out and out.rstrip().endswith("SELECT * FROM txn")


def test_字串常值裡的點不當成表名():
    sql = """SELECT 1 FROM "D"."dbo"."t" WHERE note = 'x.y.t'"""
    assert _strip(sql)[0] == "SELECT 1 FROM dbo.t WHERE note = 'x.y.t'"


def test_註解裡的點不當成表名():
    sql = '-- 參考 "D"."dbo"."t"\nSELECT 1 FROM "D"."dbo"."t"'
    out, why = _strip(sql)
    assert out == '-- 參考 "D"."dbo"."t"\nSELECT 1 FROM dbo.t' and why is None


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
    """真實形狀的 dbt compile 輸出(與 ATM_C2 同家族):三段式表名剝成 dbo.<表>,
    沒有殘留的庫名,沒有撞名/四段式。"""
    tables = {"txn_log_net", "retail_m1_earlyjob"}
    out, why = _strip_table_qualifiers(COMPILED.read_text(encoding="utf-8"), tables)
    assert why is None
    assert '"SAMPLE_DW"' not in out
    assert "FROM dbo.txn_log_net" in out and "FROM dbo.retail_m1_earlyjob e" in out


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


def test_execution_error含table_error():
    """「這次執行有沒有失敗」只留一份判斷,一定要含 table_error——不然 coverage_check /
    freeze_testdata 等腳本會把「表名對不上沒跑成」當成「驗過了」。"""
    from orchestrator.spec_exec import execution_error
    base = {"sandbox_error": None, "sql_error": None, "testdata_error": None,
            "table_error": None}
    assert execution_error(base) is None
    assert execution_error({**base, "table_error": "撞名"}) == "撞名"
    assert execution_error({**base, "sandbox_error": "s", "table_error": "t"}) == "s"  # 第一個


class _MssqlErr(Exception):
    """模擬 pymssql 的 SQL Server 例外:(錯誤碼, 訊息),.number 帶錯誤碼。"""
    def __init__(self, number, msg):
        super().__init__(number, msg)
        self.number = number


@pytest.mark.parametrize("number", [208, 911, 7202])
def test_沙盒找不到物件時記待驗證不是SQL有錯(monkeypatch, number):
    """tokenizer 還認不出的限定名、或規格 DDL 漏了表 → SQL Server 回 208/911/7202。
    那是「對應不到沙盒的測資」,記待驗證(table_error),不是說這份 SQL 有錯。"""
    class Cursor:
        def __init__(self):
            self.description = None

        def execute(self, sql, params=None):
            if "GHOST" in sql.upper():            # 受審 SQL(找不到的表)
                raise _MssqlErr(number, "Invalid object name 'ghost'.")
            self.description = None

        def fetchone(self):
            return ("16.0", "Developer Edition")

        def fetchall(self):
            return []

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
    out = spec_exec.execute_cases("SELECT 1 FROM ghost", _PLAN)
    assert out["table_error"] and str(number) in out["table_error"]
    assert out["sql_error"] is None


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
    """那是環境問題,不是剝的邏輯壞了 —— 報成 fail 會讓人去查錯的地方。

    CI 刻意不裝 pymssql,`execute_cases` 第一行的 `import pymssql` 會丟
    `ModuleNotFoundError`,被 `check_table_names` 的 except 收成 fail。放一個空的假
    pymssql 讓 import 過:空密碼在用到 `.connect` 之前就先回 sandbox_error,所以假模組
    不需要有 connect。"""
    monkeypatch.setitem(sys.modules, "pymssql", types.ModuleType("pymssql"))
    pf = _preflight()
    monkeypatch.setattr(spec_exec, "_sandbox_params",
                        lambda: {"server": "x", "port": 1433, "user": "sa", "password": ""})
    assert pf.check_table_names().level == "skip"


@pytest.mark.parametrize("bad,expect", [
    ({"table_error": "受審 SQL 用了不同資料庫下的同名表(t)…"}, "同名表"),
    ({"sql_error": "Invalid object name 'PROBE_DW.dbo.preflight_probe'."}, "Invalid object name"),
    ({"testdata_error": "建表/灌資料失敗於 P1:…"}, "建表/灌資料失敗"),
])
def test_探針剝不掉時要紅_而且說對原因(monkeypatch, bad, expect):
    """這條探針的用處就是在真機上擋下「剝不掉」。

    **原因要講對**:剝不掉時案例一個都沒跑,接著那道「比對不符」也會紅,但它說的是
    「可能剝到別的表」—— 查錯方向。所以這裡比對 detail 裡有沒有原本的錯誤訊息。
    """
    base = {"case_results": [], "mismatches": [], "sql_error": None, "testdata_error": None,
            "sandbox_error": None, "table_error": None, "engine": "stub", "timed_out": False}
    monkeypatch.setattr(spec_exec, "execute_cases", lambda sql, plan: {**base, **bad})
    chk = _preflight().check_table_names()
    assert chk.level == "fail", chk.detail
    assert expect in chk.detail, chk.detail
