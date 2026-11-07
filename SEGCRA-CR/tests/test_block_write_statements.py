"""程序性區塊裡的寫入語句(#106)——不呼叫 LLM、不連資料庫。

`CREATE PROCEDURE dbo.p AS BEGIN DELETE FROM t; END;` 原本一條規則都不報,也不報
解析錯誤:報告長得跟掃過了沒問題一樣。三種成因:

  1. 解析得出來,但 R001 只看最外層(最外層是 Create / IfBlock / WhileBlock)
  2. 整段退回 Command(只剩原文、沒有語法樹):TRY…CATCH
  3. 解析錯:`IF @x = 1` 換行接 `DELETE FROM t`,讀成條件 `@x = 1 AS DELETE`,
     成立時變成 `SELECT * FROM t` —— 刪除被看成讀取

部署端現在的 MR 多是 dbt model、一個檔一句碰不到;但預存程序、hook、腳本裡的 T-SQL
常用這些寫法。規則看不到的時候,至少要讓人知道它沒看。
"""
import json

import pytest

from toolbox.sqltools import run_rules


def _out(sql):
    out = json.loads(run_rules(sql))
    hits = out["hits"] if isinstance(out, dict) else out
    error = out.get("error") if isinstance(out, dict) else None
    return hits, error


def _rules(sql, code="R001"):
    hits, _ = _out(sql)
    return [h["rule"] for h in hits if h["rule"] == code]


# ─────────────────── 1. issue 列的每一種寫法 ───────────────────

@pytest.mark.parametrize("sql,label", [
    ("CREATE PROCEDURE dbo.p AS BEGIN DELETE FROM t; END;", "預存程序"),
    ("CREATE PROCEDURE dbo.p AS DELETE FROM t;", "預存程序(沒有 BEGIN)"),
    ("IF @x = 1 BEGIN DELETE FROM t; END;", "IF 區塊"),
    ("IF @x = 1 DELETE FROM t;", "IF 單句"),
    ("IF @x = 1\nDELETE FROM t", "IF 換行(原本被讀成 SELECT)"),
    ("WHILE @i < 3 BEGIN UPDATE t SET a = 1; END;", "WHILE"),
    ("BEGIN TRY DELETE FROM t; END TRY BEGIN CATCH SELECT 1; END CATCH;", "TRY…CATCH"),
    ("ALTER PROCEDURE dbo.p AS BEGIN UPDATE t SET a = 1; END;", "ALTER PROCEDURE"),
])
def test_區塊裡的寫入語句照樣命中R001(sql, label):
    assert _rules(sql) == ["R001"], label


@pytest.mark.parametrize("sql,label", [
    ("BEGIN UPDATE t SET a = 1; DELETE FROM t; END;", "兩句都要報"),
    ("IF @x = 1 BEGIN DELETE FROM a; DELETE FROM b; END;", "IF 區塊裡兩句"),
])
def test_第一句不會被漏掉(sql, label):
    """切句之後第二句剛好變成獨立的一句,本來就報得到;第一句跟 BEGIN 黏在一起,
    原本報不到 —— 報告看起來「有抓到東西」,其實漏了一半。"""
    assert _rules(sql) == ["R001", "R001"], label


# ─────────────────── 2. MERGE 不可以誤報 ───────────────────

@pytest.mark.parametrize("sql", [
    "MERGE t AS tgt USING s AS src ON tgt.id = src.id WHEN MATCHED THEN UPDATE SET tgt.a = src.a;",
    "MERGE t AS tgt USING s AS src ON tgt.id = src.id WHEN MATCHED THEN DELETE;",
    ("MERGE t AS tgt USING s AS src ON tgt.id = src.id"
     " WHEN MATCHED THEN UPDATE SET tgt.a = src.a"
     " WHEN NOT MATCHED THEN INSERT (id) VALUES (src.id);"),
])
def test_MERGE的WHEN分支不是全表操作(sql):
    """`WHEN MATCHED THEN UPDATE` 在語法樹裡也是一個沒有 WHERE 的 Update,但條件寫在
    WHEN 上。改成整棵樹找寫入語句之後,不排除的話每個 MERGE 都會被報成 blocker。"""
    assert _rules(sql) == []


# ─────────────────── 3. 拆不開時要說「沒看」,不是「沒問題」 ───────────────────

def test_剝不開的區塊記成解析失敗():
    """不可以回報成沒有命中 —— 那等於告訴看的人這段掃過了沒問題。"""
    hits, error = _out("BEGIN TRY DELETE FROM WHERE; END TRY")
    assert [h for h in hits if h["rule"] == "R001"] == []
    assert error and "程序性區塊" in error


def test_解析失敗的說明不帶原文():
    """說明會貼回 MR 留言(見 parse_error_text)。我們自己丟的例外也一樣,
    不可以把類別名或 MR 的文字帶出去。"""
    _, error = _out("BEGIN TRY DELETE FROM 客戶資料 WHERE 身分證 = 'A123456789'; END TRY")
    assert error is None or ("A123456789" not in error and "_BlockNotParsed" not in error)


# ─────────────────── 4. 原本就正常的不受影響 ───────────────────

@pytest.mark.parametrize("sql", [
    "-- DELETE FROM t",                       # 只有註解
    "SELECT 'DELETE FROM t' AS s",            # 字串裡的關鍵字
    "END;",                                   # 只有區塊結尾
    "DELETE FROM t WHERE x = 1;",             # 有 WHERE
    "SELECT * FROM t",                        # 不是寫入
])
def test_這些不該被當成區塊問題(sql):
    hits, error = _out(sql)
    assert error is None, sql
    assert [h for h in hits if h["rule"] == "R001"] == [], sql


@pytest.mark.parametrize("sql,label", [
    ("ALTER PROCEDURE dbo.p AS BEGIN UPDATE t SET a = 1; END;", "ALTER PROCEDURE"),
    ("CREATE OR ALTER PROC dbo.p AS DELETE FROM t;", "CREATE OR ALTER PROC"),
    ("CREATE TRIGGER tr ON t AFTER INSERT AS BEGIN DELETE FROM audit; END;", "觸發程序"),
])
def test_程序標頭剝到AS之後照樣命中(sql, label):
    """`CREATE PROCEDURE` 解析得出 Create(走語法樹找得到裡面的寫入語句),但
    `ALTER PROCEDURE`、`CREATE TRIGGER` 會整段退回 Command —— 標頭剝到 AS 為止。"""
    assert _rules(sql) == ["R001"], label


@pytest.mark.parametrize("sql,label", [
    ("BEGIN TRY DELETE FROM t END TRY", "沒有分號的 TRY 區塊"),
    ("BEGIN UPDATE t SET a = 1 END", "沒有分號的 BEGIN…END"),
])
def test_區塊的結尾也要剝(sql, label):
    """`BEGIN TRY … END TRY` 的 END 在最後面,只剝開頭的話剝完仍然解析不了。"""
    assert _rules(sql) == ["R001"], label


def test_剝完解析得過但寫入語句仍然不見時也要說沒看():
    """另一條路:剝完**解析得過**,但那些寫入語句還是沒出現在語法樹裡。
    跟剝不開一樣不可以回報成沒有命中 —— 兩條路都要記成解析失敗。"""
    hits, error = _out("SET XACT_ABORT ON DELETE FROM t")
    assert [h for h in hits if h["rule"] == "R001"] == []
    assert error and "程序性區塊" in error
