"""一句解析失敗,不可以吃掉整個檔案的命中(#90 第 1 點)——不呼叫 LLM、不連資料庫。

原本 `run_rules` 把整份一次丟給解析器:其中一句壞掉(例如巢狀太深),整個檔案的
語句都不檢查。同一個檔裡那句 `DELETE FROM t;` 的 R001 blocker 跟著不見,只剩一條
「預掃無法解析此檔的 SQL」的 major,決策從**擋下**變成**交人工** —— 而那是可以
刻意做出來的。
"""
import json

import pytest

from toolbox.sqltools import _statement_chunks, run_rules

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
    assert len(_statement_chunks(sql)) == count


def test_字串裡的分號不會把一句切成兩半():
    """用 sql.split(';') 的話,這一句會被切成兩段、兩段都解析不了 ——
    本來能解析的變成解析失敗,等於自己製造一次這張要修的病。"""
    out, hits = _out("DELETE FROM t WHERE note = 'a;b';")
    assert not isinstance(out, dict) or not out.get("error")
    assert hits == [] or all(h["rule"] != "R001" for h in hits)  # 有 WHERE,不該命中


def test_斷詞失敗時退回整份解析():
    """引號沒收這種連斷詞都失敗的內容,行為跟以前一樣(整份當一句)。"""
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


def test_解析進入點有公開的名字():
    """`pipeline` 與 `dbt_affected` 都在用它(#82 review)。舊名字先留著當別名:
    那兩個檔案現在有在途的改動,一起改會撞在一起。"""
    from toolbox import sqltools
    assert sqltools.parse_statements("SELECT 1")
    assert sqltools._parse is sqltools.parse_statements
