"""一句解析失敗,不可以吃掉整個檔案的命中(#90 第 1 點)——不呼叫 LLM、不連資料庫。

原本 `run_rules` 把整份一次丟給解析器:其中一句壞掉(例如巢狀太深),整個檔案的
語句都不檢查。同一個檔裡那句 `DELETE FROM t;` 的 R001 blocker 跟著不見,只剩一條
「預掃無法解析此檔的 SQL」的 major,決策從**擋下**變成**交人工** —— 而那是可以
刻意做出來的。
"""
import json

import pytest

from toolbox.sqltools import run_rules, statement_chunks

DEEP = "SELECT " + "(1+" * 50 + "1" + ")" * 50 + ";"      # sqlglot 遞迴太深
PURGE = "DELETE FROM t;"


def _out(sql):
    o = json.loads(run_rules(sql))
    return (o, o["hits"]) if isinstance(o, dict) else (o, o)


def test_壞的那句不吃掉好的那句的命中():
    out, hits = _out(PURGE + "\n" + DEEP)
    assert "R001" in [h["rule"] for h in hits], "DELETE 無 WHERE 的 blocker 不見了"
    assert out.get("error"), "壞掉的那一句仍然要記成解析錯誤"


def test_順序顛倒也一樣():
    """壞的在前面時,後面的句子照樣要跑規則。"""
    _, hits = _out(DEEP + "\n" + PURGE)
    assert "R001" in [h["rule"] for h in hits]


def test_整份都好時沒有解析錯誤():
    out, hits = _out("SELECT a FROM t;\nSELECT b FROM u;")
    assert not isinstance(out, dict) or not out.get("error")


def test_只有一句而且壞掉時照舊回解析錯誤():
    out, _ = _out(DEEP)
    assert out.get("error")


# ─────────────────── 切句本身 ───────────────────

@pytest.mark.parametrize("sql,count", [
    ("DELETE FROM t;\nSELECT 1;", 2),
    ("SELECT ';' AS semi; DELETE FROM t", 2),       # 字串常值裡的分號不算
    ("SELECT 1 -- a; b\nFROM t;\nSELECT 2", 2),     # 註解裡的分號不算
    ("SELECT 1", 0),                                # 只有一句 → 照舊整份解析
])
def test_依分號切句(sql, count):
    assert len(statement_chunks(sql)) == count


def test_字串裡的分號不會把一句切成兩半():
    """用 sql.split(';') 的話,這一句會被切成兩段、兩段都解析不了 ——
    本來能解析的變成解析失敗,等於自己製造一次這張要修的病。"""
    out, hits = _out("DELETE FROM t WHERE note = 'a;b';")
    assert not isinstance(out, dict) or not out.get("error")
    assert hits == [] or all(h["rule"] != "R001" for h in hits)  # 有 WHERE,不該命中


def test_整份都是沒收尾的引號時記成解析錯誤():
    """沒收尾的引號吃到檔尾;整份只有這一段,它就是唯一的一句,解析失敗要看得見。"""
    out, _ = _out("SELECT 'no end")
    assert isinstance(out, dict) and out.get("error")


# ─────────────────── 訊息 ───────────────────

def test_行號對得回原檔():
    """逐句解析時行號是那一句之內的,要加上它在檔案裡的起始行。"""
    out, _ = _out("SELECT 1;\nSELECT 2;\nSELECT FROM FROM;")
    assert "第 3 行" in out["error"], out["error"]


def test_解析錯誤不帶原文():
    """訊息會變成 finding 內文貼回 MR(#60):只能有位置,不能有 SQL 原文。"""
    out, _ = _out("SELECT 1;\nSELECT secret_token, 'p@ssw0rd' FROM ((t;")
    err = out["error"]
    assert "secret_token" not in err and "p@ssw0rd" not in err, err


def test_很多句都壞掉時訊息不會爆長():
    out, _ = _out("".join(f"SELECT FROM FROM {i};\n" for i in range(40)))
    assert len(out["error"]) < 400, out["error"]
    assert "還有" in out["error"]


def test_文字層規則不受影響():
    """解析失敗的那一句仍然要被文字層規則看到 —— 否則一段解析不了的 SQL
    會連明碼密碼都一起放過。"""
    _, hits = _out("SELECT FROM FROM;\nSELECT 1 WHERE password = 'hunter2';")
    assert "R004" in [h["rule"] for h in hits]


def test_解析與切句的進入點都是公開的名字():
    """`pipeline` 與 `dbt_affected` 都在用它們。舊的 `_parse` 別名已經拿掉——
    底線開頭的名字被別的模組 import,看起來像內部實作卻是介面。"""
    from toolbox import sqltools
    assert sqltools.parse_statements("SELECT 1")
    assert sqltools.statement_chunks("SELECT 1; SELECT 2")
    assert not hasattr(sqltools, "_parse"), "別名應該已經拿掉"


# ─────────── 斷詞失敗不可以讓整份退回一次解析(#99 review 第 1 點)───────────
#
# 原本斷詞失敗就回空清單、整份當一句。但那正是這道要擋的事:在檔尾加一個沒收的
# `'` 或 `/*`,整份退回一次解析,前面那句 `DELETE FROM t;` 的 blocker 又不見了 ——
# 比巢狀運算式更容易做出來。

@pytest.mark.parametrize("tail,label", [
    ("SELECT 'x", "沒收的引號"),
    ("/* x", "沒收的區塊註解"),
    ('SELECT "x', "沒收的雙引號識別字"),
    ("SELECT [x", "沒收的方括號識別字"),
])
def test_檔尾沒收尾時前面的句子照樣跑規則(tail, label):
    _, hits = _out(PURGE + "\n" + DEEP + "\n" + tail)
    assert "R001" in [h["rule"] for h in hits], f"{label}:R001 不見了"


@pytest.mark.parametrize("sql,count", [
    ("DELETE FROM t;\nSELECT 'x", 2),             # 沒收的引號吃到檔尾,自成一句
    ("DELETE FROM t;\nSELECT 1; /* x", 3),
    ("SELECT [a;b] FROM t; DELETE FROM u", 2),    # 方括號識別字裡的分號不算
    ("SELECT 'a;b' AS x; DELETE FROM u", 2),      # 字串裡的分號不算
    ("SELECT 'it''s; here' AS x; DELETE FROM u", 2),   # '' 是跳脫,不是字串結束
    ("SELECT 1 /* a /* 巢狀 */ still */; DELETE FROM t", 2),   # T-SQL 的巢狀註解
    # 分號夾在內層 `*/` 與外層 `*/` 之間:只認最內層的話,這個分號會被當成切點
    ("SELECT 1 /* a /* x */ ; y */ FROM t; DELETE FROM u", 2),
])
def test_切句認得字串與註解(sql, count):
    assert len(statement_chunks(sql)) == count


def test_特製內容不會讓切句變慢():
    """切句是單趟掃描:沒收尾的就吃到檔尾,不會每個位置都重掃一次。"""
    import time
    t = time.perf_counter()
    run_rules("/*" * 8000)
    assert time.perf_counter() - t < 3


def test_切句不需要在函式裡匯入():
    """切句是純字元掃描,不靠 sqlglot 的斷詞器,所以函式裡不該有任何 import ——
    有的話代表又繞回斷詞器,沒收尾的引號會讓整份退回一次解析。"""
    import inspect

    from toolbox import sqltools
    src = inspect.getsource(sqltools.statement_chunks) + inspect.getsource(
        sqltools._semicolon_cuts)
    assert "import" not in src


# ─────────── 沒有分號的多句(#105 第 1 點)───────────
#
# `DELETE FROM t` 換行接 `SELECT 1 AS x`,兩句都正確但沒有分號。整份當成一句就解析
# 不了,於是一條 AST 規則都不跑 —— 那句 DELETE 沒有 WHERE 的 blocker 也跟著不見。
# 部署端的 dbt model 是一個檔一句碰不到,但 T-SQL 不寫分號很常見。

@pytest.mark.parametrize("sql,rules", [
    ("DELETE FROM t\nSELECT 1 AS x", ["R001"]),
    ("UPDATE t SET a = 1\nDELETE FROM u", ["R001", "R001"]),
    ("SELECT 1 AS x\nDELETE FROM t", ["R001"]),
    ("DELETE FROM a;\nDELETE FROM b\nSELECT 1 AS x", ["R001", "R001"]),  # 混著分號
    ("delete from t\nselect 1 as x", ["R001"]),                          # 小寫
])
def test_沒有分號也切得開(sql, rules):
    out, hits = _out(sql)
    assert not (isinstance(out, dict) and out.get("error")), out
    assert [h["rule"] for h in hits if h["rule"] == "R001"] == rules


@pytest.mark.parametrize("sql", [
    "INSERT INTO t\nSELECT 1",                      # 延續寫法:SELECT 是 INSERT 的一部分
    "SELECT 1\nUNION ALL\nSELECT 2",
    "DELETE FROM t\nWHERE x = 1",                   # WHERE 另起一行
    "WITH c AS (SELECT 1)\nSELECT * FROM c",
    "SELECT 'a\nDELETE FROM t'",                    # 字串裡的關鍵字
    "SELECT 1 AS x\n-- DELETE FROM t\nFROM t",     # 註解裡的關鍵字
])
def test_正常寫法不會被切錯(sql):
    """只在**解析不了**時才試關鍵字切,而這些本來就解析得過 —— 根本走不到那一步。"""
    out, hits = _out(sql)
    assert not (isinstance(out, dict) and out.get("error")), out
    assert [h for h in hits if h["rule"] == "R001"] == []


def test_切完還是解析不了就當作沒切過():
    """不要把「一句壞掉」變成「好幾句壞掉」:每一段都解析得過才採用。"""
    out, _ = _out("DELETE FROM t\nSELECT * FROM ((((")
    assert isinstance(out, dict) and out.get("error"), "整段仍然記成解析失敗"


def test_關鍵字切句也只有一份():
    """三處(規則層、行號對回、受影響比較)共用 parsed_chunks。"""
    from orchestrator import dbt_affected, pipeline
    from toolbox import sqltools

    sql = "DELETE FROM t\nSELECT 1 AS x"
    assert len(sqltools.parsed_chunks(sql)) == 2
    assert len(dbt_affected._statement_keys(sql)) == 2
    assert len(pipeline._rendered_starts(sql) or {}) == 2


def test_只切出一段不算切過():
    """切出來只有一段,等於沒切 —— 照舊回報那一段解析失敗,不要換一個位置重報一次。"""
    from toolbox.sqltools import _split_by_keyword

    assert _split_by_keyword("\nSELECT 1 AS x", 0) is None
    assert _split_by_keyword("DELETE FROM t\nSELECT 1 AS x", 0) is not None
