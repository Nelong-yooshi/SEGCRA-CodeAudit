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


# ─────────────────── #27 review 補齊的項目 ───────────────────

@pytest.mark.parametrize("probe", [
    # 三段式的系統表名稱(review 指出的 bug:原本 (?<![\w.]) 把前面的點排除掉,
    # 所以 master.sys.databases 抓不到,[master].[sys].[databases] 反而抓得到)
    "AND EXISTS (SELECT 1 FROM master.sys.databases WHERE name LIKE 'segcra%')",
    "AND EXISTS (SELECT 1 FROM [master].[sys].[databases])",
    "AND EXISTS (SELECT 1 FROM master..sysdatabases)",
    "AND EXISTS (SELECT 1 FROM master.dbo.sysdatabases)",
    "AND EXISTS (SELECT 1 FROM msdb.sys.objects)",
    # 系統目錄一律算(原本只列了幾個名字)
    "AND EXISTS (SELECT 1 FROM sys.server_principals WHERE name = 'sa')",
    "AND EXISTS (SELECT 1 FROM sys.configurations)",
    "AND EXISTS (SELECT 1 FROM sys.all_objects)",
    "AND EXISTS (SELECT 1 FROM sys.database_principals)",
    # 其他身分函式
    "AND CURRENT_USER <> 'dbo'",
    "AND SESSION_USER <> 'sa'",
    "AND USER_NAME() <> 'dbo'",
    "AND USER_ID() > 1",
    "AND IS_MEMBER('db_owner') = 0",
    "AND IS_SRVROLEMEMBER('sysadmin') = 0",
    "AND HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE') = 0",
    # 其他資料庫與連線資訊
    "AND ORIGINAL_DB_NAME() <> 'PROD'",
    "AND SESSION_CONTEXT(N'env') <> 'prod'",
    "AND CONTEXT_INFO() IS NULL",
    "AND CONNECTIONPROPERTY('net_transport') = 'TCP'",
    # 函式名與括號之間夾註解(T-SQL 合法)
    "AND DB_NAME/**/() NOT LIKE 'segcra%'",
    "AND DB_NAME /* which db? */ () <> 'PROD'",
    "AND HOST_NAME/**/() = 'prod-01'",
])
def test_review_補齊的寫法都命中R005(probe):
    hits = _r005(f"{BASE} {probe}")
    assert len(hits) == 1, f"沒抓到:{probe}"
    assert hits[0]["severity"] == "major"


@pytest.mark.parametrize("sql", [
    'SELECT * FROM "sys"."databases"',                       # 雙引號識別字
    'SELECT * FROM "master"."sys"."objects"',                # 三段式都加雙引號
    'SELECT * FROM "sys".objects',                           # 只有前半加
    'SELECT * FROM sys."objects"',                           # 只有後半加
    'SELECT * FROM "INFORMATION_SCHEMA"."TABLES"',
    'SELECT * FROM "master"."dbo"."sysdatabases"',           # 舊式系統表名加雙引號
])
def test_雙引號識別字照樣命中R005(sql):
    """SQL Server 預設 QUOTED_IDENTIFIER ON,`"sys"."databases"` 是可用的寫法。
    只認方括號的話,換一種引號就繞過去了(#27 review 留給規則層那張處理的項目)。"""
    hits = _r005(sql)
    assert len(hits) == 1, f"沒抓到:{sql}"
    assert hits[0]["severity"] == "major"


@pytest.mark.parametrize("sql", [
    "EXEC sys.sp_executesql N'SELECT 1'",
    "EXEC [sys].[sp_executesql] N'SELECT 1'",
    'EXEC "sys"."sp_executesql" N\'SELECT 1\'',
])
def test_sp_executesql不重複報(sql):
    """這一支已經由 R009(動態 SQL)報。同一句出現兩條說法不同的 finding,看的人會
    以為是兩件事。"""
    assert _r005(sql) == []


@pytest.mark.parametrize("sql", [
    # 回傳執行環境的系統預存程序:跟 HOST_NAME()、@@SERVERNAME 是同一類資訊
    "EXEC sys.sp_who",
    "EXEC sys.sp_helpserver",
    "EXEC sys.sp_server_info",
    "EXEC sys.sp_helpdb",
    # 把帳號加進 sysadmin / db_owner:R008 只認 GRANT/REVOKE/DENY 這些新式寫法
    "EXEC sys.sp_addsrvrolemember 'app', 'sysadmin'",
    "EXEC sys.sp_addrolemember 'db_owner', 'app'",
    'EXEC "sys"."sp_addsrvrolemember" \'app\', \'sysadmin\'',
])
def test_sp_executesql以外的系統預存程序照樣命中(sql):
    """只排除別條已經報的那一支,不是整個 `sys.sp_` 都排除:其餘的沒有別的規則接手,
    排掉就變成一條都不報。類別不完全貼切,但報得出來比漏掉好。"""
    hits = _r005(sql)
    assert len(hits) == 1, f"沒抓到:{sql}"
    assert hits[0]["severity"] == "major"


@pytest.mark.parametrize("sql", [
    # 延續原本那組:名稱剛好含關鍵字不誤報
    "SELECT current_user_id, session_user_id FROM t",        # 欄位名含 niladic 函式名
    "SELECT t.user_name FROM t",                             # 欄位叫 user_name(沒有括號)
    "SELECT dbo.fn_user_name(x) FROM t",                     # 自訂函式名含 user_name
    "SELECT dbo.is_member_flag FROM t",                      # 欄位含 is_member
    "SELECT * FROM mysys.databases",                         # schema 名只是含 sys
    "SELECT * FROM t_sysobjects",                            # 表名以 sysobjects 結尾的字
    "SELECT sysname_col FROM t",                             # 以 sys 開頭但不是系統表
    "DECLARE @n sysname = 'x'",                              # sysname 是 T-SQL 型別
    "SELECT * FROM mydb.dbo.accounts",                       # 一般的三段式名稱
    "SELECT original_db_name_col FROM t",
])
def test_review補齊之後仍然不誤報(sql):
    assert _r005(sql) == []


def test_現有的SQL檔沒有誤報():
    """PR 原本的驗收是「現有 71 個 SQL 檔 0 誤報」。這次把比對放寬(sys. 一律算、
    多了身分函式、允許夾註解),要確認放寬沒有把正常的程式掃進來。

    掃兩個來源:repo 裡的 .sql 檔(54 個),以及 golden set / fixtures 的 JSON 裡
    `full_content` 與 `diff` 兩個欄位的 SQL(64 段)——golden case 的程式是放在
    JSON 裡的,只掃 .sql 檔會漏掉受測的那些。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    bad, n = [], 0
    for f in sorted(root.rglob("*.sql")):
        if ".venv" in f.parts:
            continue
        n += 1
        hits = _r005(f.read_text(encoding="utf-8", errors="replace"))
        if hits:
            bad.append((str(f.relative_to(root)), hits[0]["message"][:60]))
    for f in sorted(root.glob("eval/golden/*.json")) + sorted(root.glob("tests/fixtures/*.json")):
        for fl in json.loads(f.read_text(encoding="utf-8")).get("files") or []:
            for key in ("full_content", "diff"):
                if not fl.get(key):
                    continue
                n += 1
                if _r005(fl[key]):
                    bad.append((f"{f.name}:{fl.get('path')}:{key}", ""))
    assert bad == [], f"誤報:{bad}"
    assert n >= 110, f"只掃到 {n} 段,來源可能漏了(應該有 54 個 .sql + 64 段 JSON)"


def test_已報判定的關鍵詞不含沙盒():
    """review 的小建議:「沙盒」範圍太廣——同一個檔案裡模型的其他 major 只要提到
    沙盒(例如「沙盒無法連線」),R005 就會被當成已經報過而不補報。"""
    kws = pipeline._RULE_KEYWORDS["R005"]
    assert "沙盒" not in kws, "「沙盒」會讓別的 finding 把 R005 吃掉"
    assert "執行環境" in kws and "sys." in kws


def test_模型報了別的沙盒問題時R005照樣補報():
    """上一條的實際後果:模型只報「沙盒不可用」時,R005 仍然要補。"""
    pre = [{"path": "models/m.sql",
            "rules": [{"rule": "R005", "severity": "major",
                       "message": "規則 SQL 讀取執行環境:DB_NAME"}]}]
    report = {"findings": [{"file": "models/m.sql", "severity": "major",
                            "title": "執行驗證沙盒不可用", "detail": "沙盒連線失敗"}]}
    out = pipeline.enforce_rules(report, pre)
    assert any("執行環境" in (f.get("title", "") + f.get("detail", ""))
               for f in out["findings"]), "R005 被「沙盒」那條吃掉了"
