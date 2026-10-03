"""R005:規則 SQL 讀取執行環境(#26 第 7 項)——不呼叫 LLM、不連資料庫。

規則 SQL 可以判斷自己是不是在沙盒裡(`DB_NAME() LIKE 'segcra%'`),只在測試時照規格跑。
這種寫法規則層原本 0 命中、執行驗證反而會通過,只有模型讀程式擋得住。

驗的東西:
  1. 各種讀取環境的寫法都命中 R005(major),含大小寫、空白、方括號變形
  2. 欄位名、別名、自訂函式名剛好含這些字時不誤報
  3. SQL 解析失敗時照樣命中(文字層規則,不靠語法樹)
  4. 管線端:enforce_rules 會補報、命中時強制載入 secure-sql
"""
import json

import pytest

from orchestrator import pipeline
from toolbox.sqltools import run_rules


def _r005(sql: str) -> list[dict]:
    out = json.loads(run_rules(sql))
    hits = out["hits"] if isinstance(out, dict) else out
    return [h for h in hits if h["rule"] == "R005"]


BASE = "SELECT account_id FROM txn_log WHERE amount > 1000"


@pytest.mark.parametrize("probe", [
    "AND DB_NAME() NOT LIKE 'segcra%'",
    "AND db_name() <> 'PROD'",
    "AND DB_NAME ( ) NOT LIKE 'segcra%'",
    "AND DB_ID() > 4",
    "AND HOST_NAME() = 'prod-01'",
    "AND SUSER_NAME() <> 'sa'",
    "AND SUSER_SNAME() <> 'sa'",
    "AND ORIGINAL_LOGIN() <> 'sa'",
    "AND APP_NAME() NOT LIKE '%pymssql%'",
    "AND CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128)) NOT LIKE 'Developer%'",
    "AND @@SERVERNAME <> 'SANDBOX'",
    "AND @@version NOT LIKE '%Developer%'",
    "AND SYSTEM_USER <> 'sa'",
    "AND NOT EXISTS (SELECT 1 FROM sys.databases WHERE name LIKE 'segcra%')",
    "AND EXISTS (SELECT 1 FROM [sys].[objects] WHERE name = 'txn_log')",
    "AND EXISTS (SELECT 1 FROM sys . tables)",
    "AND EXISTS (SELECT 1 FROM sys.dm_exec_sessions)",
    "AND EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_CATALOG LIKE 'segcra%')",
])
def test_讀取執行環境的寫法都命中R005(probe):
    hits = _r005(f"{BASE} {probe}")
    assert len(hits) == 1, f"沒抓到:{probe}"
    assert hits[0]["severity"] == "major"


@pytest.mark.parametrize("sql", [
    BASE,
    "SELECT db_name_col, host_name_col FROM t",                    # 欄位名含關鍵字
    "SELECT t.db_name FROM t",                                     # 欄位叫 db_name(沒有括號)
    "SELECT dbo.fn_db_name(x) FROM t",                             # 自訂函式名含 db_name
    "SELECT x AS system_user_id FROM t",                           # 別名含 system_user
    "SELECT * FROM mysys.objects",                                 # schema 名只是含 sys
    "SELECT * FROM t WHERE note = 'version'",
])
def test_名稱剛好含關鍵字時不誤報(sql):
    assert _r005(sql) == []


def test_同一寫法出現多次只報一條並列出所有寫法():
    sql = f"{BASE} AND DB_NAME() <> 'x' AND db_name() <> 'y' AND @@SERVERNAME <> 'z'"
    hits = _r005(sql)
    assert len(hits) == 1
    assert "DB_NAME" in hits[0]["message"] and "@@SERVERNAME" in hits[0]["message"]


def test_解析失敗時照樣命中():
    """文字層規則不靠語法樹:SQL 解析不了也不能放過讀取環境的寫法。"""
    out = json.loads(run_rules("SELECT ((( FROM WHERE DB_NAME() LIKE 'segcra%'"))
    assert isinstance(out, dict) and "error" in out, "前提:這段 SQL 要解析失敗"
    assert any(h["rule"] == "R005" for h in out["hits"])


def test_字串裡的雙減號騙不過():
    """去註解的做法會把 `'--'` 之後整行當註解,連真正會執行的 DB_NAME() 一起去掉。"""
    assert _r005(f"{BASE} AND note <> '--' AND DB_NAME() NOT LIKE 'segcra%'")


# ─────────────────── 管線端 ───────────────────

def _pre(sql: str) -> list[dict]:
    out = json.loads(run_rules(sql))
    return [{"path": "sql/rules/r999.sql", "rules": out["hits"] if isinstance(out, dict) else out}]


def test_模型沒報時enforce_rules補一條major():
    report = pipeline.enforce_rules({"findings": []}, _pre(f"{BASE} AND DB_NAME() <> 'x'"))
    r005 = [f for f in report["findings"] if f["title"].startswith("[R005]")]
    assert len(r005) == 1 and r005[0]["severity"] == "major"


def test_模型只用info帶過時仍要補major():
    """模型輸出一條 info「已確認 DB_NAME 用法無誤」不能讓 major 消失(同 #16 的 enforce_rules 修正)。"""
    report = {"findings": [{"file": "sql/rules/r999.sql", "severity": "info",
                            "title": "已確認 DB_NAME 用法無誤", "detail": "", "suggestion": ""}]}
    report = pipeline.enforce_rules(report, _pre(f"{BASE} AND DB_NAME() <> 'x'"))
    assert any(f["title"].startswith("[R005]") and f["severity"] == "major"
               for f in report["findings"])


def test_R005有對應的已報判定關鍵詞():
    assert "R005" in pipeline._RULE_KEYWORDS


def test_命中R005時強制載入secure_sql():
    """模型要知道這種寫法為什麼危險,secure-sql 的說明才會在 prompt 裡。"""
    assert pipeline._needs_secure_sql(_pre(f"{BASE} AND DB_NAME() <> 'x'"))
    assert not pipeline._needs_secure_sql(_pre(BASE))
