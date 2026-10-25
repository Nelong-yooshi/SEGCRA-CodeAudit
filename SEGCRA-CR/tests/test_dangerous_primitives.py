"""危險 SQL 原語 R006–R010(#41 第 1 點)——不呼叫 LLM、不連資料庫。

這幾種原本會被報成「語法錯誤」「效能問題」「缺暫存表」:類別錯了,看留言的人就不會
知道這是資料外送或權限變更。而且「模型抓得到」不等於「有保證」——同一份 MR 換一次
取樣就可能漏掉,所以要有規則。

驗的東西:
  1. 每一類的代表寫法都命中、而且給的是那一類的理由
  2. 正常的業務查詢零誤報(repo 裡所有 .sql 與 golden case 全掃)
  3. 解析失敗時照樣命中(文字層規則,不靠語法樹)
  4. 管線端:enforce_rules 會補報、命中時強制載入 secure-sql
"""
import json
import pathlib

import pytest

from orchestrator import pipeline
from toolbox.sqltools import run_rules

NEW_RULES = ("R006", "R007", "R008", "R009", "R010")


def _hits(sql: str, code: str | None = None) -> list[dict]:
    out = json.loads(run_rules(sql))
    hits = out["hits"] if isinstance(out, dict) else out
    return [h for h in hits if code is None or h["rule"] == code]


# ─────────────────── 1. 每一類都命中 ───────────────────

@pytest.mark.parametrize("sql", [
    "SELECT * FROM OPENROWSET(BULK N'x.csv', SINGLE_CLOB) AS a",
    "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'Server=10.0.0.9') .db.dbo.t",
    "SELECT * FROM OPENQUERY(remote_srv, 'SELECT 1')",
    "SELECT * FROM srv.db.dbo.txn_log",
    "SELECT * FROM [report srv].[db].[dbo].[txn_log]",
    "SELECT * FROM srv.db..txn_log",                       # 中間段可以是空的
    "BULK INSERT txn FROM '\\\\share\\in.csv'",
    "SELECT * FROM OPENROWSET/**/('x')",                   # 函式名與括號間夾註解
])
def test_外部資料源讀寫命中R006(sql):
    hits = _hits(sql, "R006")
    assert hits, sql
    assert hits[0]["severity"] == "blocker"
    assert "外部資料源讀寫" in hits[0]["message"]


@pytest.mark.parametrize("sql", [
    "EXEC xp_cmdshell 'dir'",
    "EXEC master..xp_cmdshell 'dir'",                      # 限定名稱是最常見的寫法
    "EXEC [master].[dbo].[xp_cmdshell] 'dir'",
    "EXEC sp_configure 'show advanced options', 1; EXEC xp_cmdshell 'whoami'",
])
def test_作業系統命令命中R007(sql):
    hits = _hits(sql, "R007")
    assert hits, sql
    assert hits[0]["severity"] == "blocker"
    assert "作業系統" in hits[0]["message"]


@pytest.mark.parametrize("sql", [
    "GRANT SELECT ON txn_log TO app_user",
    "REVOKE SELECT ON txn_log FROM app_user",
    "DENY DELETE ON txn_log TO app_user",
    "EXECUTE AS LOGIN = 'sa'; SELECT 1",
    "ALTER ROLE db_owner ADD MEMBER app_user",
    "ALTER SERVER ROLE sysadmin ADD MEMBER app_user",
])
def test_權限授予或切換命中R008(sql):
    hits = _hits(sql, "R008")
    assert hits, sql
    assert hits[0]["severity"] == "major"
    assert "權限" in hits[0]["message"]


@pytest.mark.parametrize("sql", [
    "EXEC('SELECT * FROM ' + @tbl)",
    "EXECUTE (@sql)",
    "EXEC sp_executesql @sql, N'@a INT', @a = 1",
    "EXEC/**/('SELECT 1')",
])
def test_動態SQL命中R009(sql):
    hits = _hits(sql, "R009")
    assert hits, sql
    assert "動態 SQL" in hits[0]["message"]


def test_動態SQL的理由要提到可能藏有讀取環境的寫法():
    """`EXEC('SELECT DB_' + 'NAME()')` 這種拆字串的寫法 R005 的文字比對看不到,
    所以這條命中時要把那件事講出來(#41 第 4 點的補充)。"""
    msg = _hits("EXEC('SELECT DB_' + 'NAME()')", "R009")[0]["message"]
    assert "讀取執行環境" in msg


def test_不帶括號的預存程序呼叫不算動態SQL():
    """`EXEC some_proc @a` 是一般呼叫,不是把字串組出來執行。"""
    assert not _hits("EXEC dbo.refresh_summary @day = '2026-06-01'", "R009")


@pytest.mark.parametrize("sql", [
    "{% set r = run_query('DELETE FROM audit_log') %}",
    "{% call statement('x') %} TRUNCATE TABLE stg {% endcall %}",
])
def test_dbt編譯期副作用命中R010(sql):
    hits = _hits(sql, "R010")
    assert hits, sql
    assert hits[0]["severity"] == "blocker"
    assert "編譯期" in hits[0]["message"]


def test_run_query只做查詢不算副作用():
    """compile 時跑一次 SELECT 是 dbt 的常見寫法(例如取欄位清單),不是副作用。"""
    assert not _hits("{% set r = run_query('SELECT DISTINCT code FROM dim') %}", "R010")


# ─────────────────── 2. 零誤報 ───────────────────

def _repo_sql() -> list[tuple[str, str]]:
    root = pathlib.Path(__file__).resolve().parents[1]
    out = [(str(f.relative_to(root)), f.read_text(encoding="utf-8", errors="replace"))
           for f in root.rglob("*.sql") if ".venv" not in str(f)]
    for f in (root / "eval" / "golden").glob("mr_*.json"):
        mr = json.loads(f.read_text(encoding="utf-8"))
        for fl in mr.get("files", []):
            for key in ("full_content", "diff"):
                if fl.get(key):
                    out.append((f"{f.name}:{fl.get('path')}:{key}", str(fl[key])))
    return out


def test_現有的SQL與golden_case一條都不誤報():
    """完成條件寫的「正常的業務查詢零誤報」。寫死主機的連線字串(10.0.0.9)曾經被
    四段式名稱的比對吃進去,就是這道掃描抓到的。"""
    bad = [(name, h["rule"], h["message"][:60])
           for name, sql in _repo_sql()
           for h in _hits(sql) if h["rule"] in NEW_RULES]
    assert bad == []


def test_掃到的檔案數量不是零():
    """上面那條若因為路徑算錯而掃到 0 個檔,會變成一條永遠綠的測試。"""
    assert len(_repo_sql()) > 50


# ─────────────────── 3. 解析失敗時照樣命中 ───────────────────

def test_解析失敗時照樣命中():
    hits = _hits("SELECT * FROM OPENROWSET(BULK 'x') AS a WHERE ((", "R006")
    assert hits, "文字層規則不該因為語法樹建不起來就失效"


# ─────────────────── 4. 管線端 ───────────────────

def _pre(sql: str, path: str = "sql/rules/r999.sql") -> list[dict]:
    out = json.loads(run_rules(sql))
    return [{"path": path, "rules": out["hits"] if isinstance(out, dict) else out}]


def test_模型沒報時enforce_rules補報():
    pre = _pre("SELECT * FROM srv.db.dbo.t")
    report = pipeline.enforce_rules({"findings": []}, pre)
    assert any(f["severity"] == "blocker" and "外部資料源讀寫" in f["detail"]
               for f in report["findings"]), report["findings"]


def test_命中時強制載入secure_sql():
    for sql in ("SELECT * FROM srv.db.dbo.t", "EXEC xp_cmdshell 'dir'",
                "GRANT SELECT ON t TO u", "EXEC('SELECT ' + @c)",
                "{% set r = run_query('DELETE FROM a') %}"):
        assert pipeline._needs_secure_sql(_pre(sql)), sql


def test_每條新規則都有已報判定的關鍵詞():
    """沒有關鍵詞的話,`_RULE_KEYWORDS.get(code, [code])` 會退回用規則碼比對 ——
    模型的報告裡不會出現「R006」,於是每次都補報一條,變成固定的重複。"""
    for code in NEW_RULES:
        assert pipeline._RULE_KEYWORDS.get(code), code


# ─────────────────── 5. 環境相依但只提醒的寫法(#41 第 4 點)───────────────────
#
# 這幾種正常程式也會用,所以給 hint 不給 major。但要知道 hint 在管線裡不是「只是
# 提醒」:模型沒回應的話會變成 `[H00x] 檢核點待人工確認`,而那會擋掉自動放行。

@pytest.mark.parametrize("sql,code", [
    ("IF OBJECT_ID('dbo.stg_txn') IS NOT NULL DROP TABLE dbo.stg_txn", "H006"),
    ("SELECT COL_LENGTH('txn_log', 'amount')", "H006"),
    ("SELECT @@LANGUAGE", "H007"),
    ("SET DATEFIRST 1; SELECT @@DATEFIRST", "H007"),
    ("{% if target.name == 'prod' %}SELECT 1{% endif %}", "H008"),
    ("SELECT env_var('DBT_ENV')", "H008"),
    ("{% if execute %}SELECT 1{% endif %}", "H008"),
    ("SELECT 1 WHERE GETDATE() > '2026-12-31'", "H009"),
    ("SELECT 1 WHERE SYSDATETIME() >= '2027-01-01 00:00:00'", "H009"),
])
def test_環境相依的寫法給hint(sql, code):
    hits = _hits(sql, code)
    assert hits, sql
    assert hits[0]["severity"] == "hint", "這幾種正常程式也會用,不該給 major"


def test_暫存表的存在性檢查不誤報():
    """`OBJECT_ID('tempdb..#x')` 是建暫存表前的標準寫法,報它只會製造雜訊。"""
    assert not _hits("IF OBJECT_ID('tempdb..#tmp') IS NOT NULL DROP TABLE #tmp", "H006")


@pytest.mark.parametrize("sql", [
    "MERGE txn AS target USING stg AS s ON target.name = s.name",  # target 是 MERGE 的慣用別名
    "SELECT VAR(amount), VARP(amount) FROM txn",                   # VAR() 是變異數聚合函式
    "{{ var('window_days') }}",            # var() 是帶參數的一般寫法,幾乎每個 model 都用
    "{% set d = var('days') %}SELECT 1",
])
def test_這些寫法不算dbt編譯期的環境資訊(sql):
    assert not _hits(sql, "H008"), sql


def test_讀環境的寫法照樣是R005不是降成hint():
    """`DB_NAME()` 這類「規則不該知道」的仍然是 major —— 新增的 hint 不可以把它吃掉。"""
    hits = _hits("SELECT 1 WHERE DB_NAME() NOT LIKE 'segcra%'", "R005")
    assert hits and hits[0]["severity"] == "major"


def test_現有的SQL與golden_case的hint命中要說得出來():
    """hint 會擋自動放行(模型沒回應 → pending_hints),所以新增時要知道影響範圍。
    現在命中的只有 dbt 樣板裡的 `target.`/`var()`,而且不在 golden case 裡。"""
    hit = [(name, h["rule"]) for name, sql in _repo_sql()
           for h in _hits(sql) if h["rule"] in ("H006", "H007", "H008", "H009")]
    assert all(r == "H008" for _, r in hit), hit
    assert not [n for n, _ in hit if n.startswith("mr_")], f"golden case 被影響:{hit}"


# ─────────────────── 6. 被呼叫的 macro 也要掃(#41 第 4 點)───────────────────
#
# 展開後的 SQL 不夠:macro 裡的 `DB_NAME()` 會被 inline 進展開結果,那種本來就掃得到;
# 但編譯期的寫法(`{% if target.name %}`、`env_var()`)決定的是「展開出哪一段 SQL」,
# 它們自己不會出現在展開結果裡 —— 展開完就看不見了。

from orchestrator.pipeline import _macro_index, _macros_used   # noqa: E402

MACROS = {
    "macros/env.sql": "{% macro pick_schema() %}"
                      "{% if target.name == 'prod' %}mart{% else %}dev{% endif %}"
                      "{% endmacro %}",
    "macros/outer.sql": "{% macro build(col) %}{{ pick_schema() }}.{{ col }}{% endmacro %}",
    "macros/unused.sql": "{% macro never_called() %}{{ env_var('X') }}{% endmacro %}",
    "macros/readenv.sql": "{% macro db() %}DB_NAME(){% endmacro %}",
}


def test_遞迴追到macro再呼叫的macro():
    used = _macros_used("SELECT {{ build('amount') }} FROM t", _macro_index(MACROS))
    assert sorted(used) == ["macros/env.sql", "macros/outer.sql"]


def test_沒被呼叫的macro不掃():
    """多掃一個沒被呼叫的 macro,就會在跟這次變更無關的檔案上補一條提醒。"""
    used = _macros_used("SELECT 1", _macro_index(MACROS))
    assert used == {}
    used = _macros_used("SELECT {{ db() }}", _macro_index(MACROS))
    assert sorted(used) == ["macros/readenv.sql"]


def test_macro裡的讀取環境寫法掃得到():
    """完成條件:macro 裡的 DB_NAME() 會被抓到。"""
    assert _hits(MACROS["macros/readenv.sql"], "R005")


def test_macro裡的編譯期環境寫法掃得到():
    """這一種展開之後就不見了,只有掃原始碼看得到。"""
    assert _hits(MACROS["macros/env.sql"], "H008")


def test_同名macro只取先讀到的那個():
    """展開器自己會報同名衝突;這裡只要不因為同名而漏掉或重複掃。"""
    index = _macro_index({"macros/a.sql": "{% macro dup() %}1{% endmacro %}",
                          "macros/b.sql": "{% macro dup() %}2{% endmacro %}"})
    assert len(index) == 1


def test_權限關鍵字後面要接空白():
    """`grant_amount`、`deny_reason` 這種欄位名不能命中 —— 不要求空白的話,
    任何以 grant/revoke/deny 開頭的識別字都會被報成權限變更。"""
    assert not _hits("SELECT grant_amount, deny_reason FROM txn", "R008")
    assert not _hits("SELECT revoke_flag FROM t WHERE grant_id = 1", "R008")


def test_dbt環境的提醒只印關鍵字不印整段Jinja():
    """比對需要前面那段 Jinja 當界線,但把整段印進訊息,看留言的人不會知道命中的是什麼。"""
    msg = _hits("{% set env = target.schema %}SELECT 1", "H008")[0]["message"]
    assert "target.schema" in msg
    assert "{%" not in msg and "set" not in msg, msg


def test_MERGE的target別名加上name欄位不算dbt環境():
    """`MERGE` 的慣用別名就叫 target,而 `target.name` 正好落在要抓的欄位清單裡 ——
    不限定在 Jinja 區段裡的話,這段非常常見的 T-SQL 會被誤報。"""
    assert not _hits(
        "MERGE customers AS target USING stg AS s ON target.name = s.name", "H008")


# macro 掃描要走完整的 prescan 才釘得住接線:直接呼叫 _macro_scan 的話,
# 「把它從 prescan 拿掉」這個改法照樣是綠的。
import asyncio  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_dbt_context as DBT  # noqa: E402

from orchestrator.pipeline import prescan  # noqa: E402
from toolbox import sqltools  # noqa: E402

ENV_MACRO = ("{% macro pick_schema() %}"
             "{% if target.name == 'prod' %}mart{% else %}dev{% endif %}{% endmacro %}")
CALLER = ("{{ config(materialized='table') }}\n"
          "SELECT id FROM {{ ref('stg_txn') }} WHERE 1=1 -- {{ pick_schema() }}\n")


@pytest.fixture(autouse=True)
def _fresh_dbt_cache():
    """dbt 專案依 sha 快取。兩條測試用同一個 sha、不同的專案內容,不清的話第二條
    拿到的是第一條的專案 —— 單獨跑會過、整檔跑會紅。"""
    from orchestrator import dbt_project
    dbt_project.clear_cache()
    yield
    dbt_project.clear_cache()


class _RealRulesHub:
    """跟預掃一樣的介面,但 run_rules 跑真的規則層(要看得到命中才驗得到這件事)。"""

    async def call_json(self, name, args):
        if name == "sqltools__run_rules":
            return json.loads(sqltools.run_rules(args["sql"]))
        return []


def test_prescan會把改到的model呼叫的macro也掃一次():
    project = dict(DBT.PROJECT)
    project["macros/env.sql"] = ENV_MACRO
    project["models/mrt_big.sql"] = CALLER
    mr = DBT._mr([DBT._f("models/mrt_big.sql")])
    ctx = DBT._ctx(files=project, mr=mr)
    pre = asyncio.run(prescan(_RealRulesHub(), mr["files"], DBT.CFG, ctx))

    scanned = {e["path"]: e for e in pre if e.get("called_macro")}
    assert "macros/env.sql" in scanned, [e["path"] for e in pre]
    assert [h["rule"] for h in scanned["macros/env.sql"]["rules"]] == ["H008"]
    assert "macros/checks.sql" not in scanned, "沒被呼叫的 macro 不該被掃"


def test_沒開dbt時不掃macro():
    """dbt 沒開的時候整條路徑都不該動(行為與接線前相同)。"""
    mr = DBT._mr([DBT._f("models/mrt_big.sql", full_content=CALLER)])
    pre = asyncio.run(prescan(_RealRulesHub(), mr["files"], None, None))
    assert not [e for e in pre if e.get("called_macro")]


# ─────────────────── 7. 特製內容不得讓規則層變成平方時間 ───────────────────
#
# 這些內容由 MR 決定。沒有上限的話,「開頭、但不閉合」的寫法每一個出現位置都會一路
# 掃到檔尾才宣告失敗:`{{` 不閉合 4 萬字元要 35 秒、`DB_NAME /*` 8 萬字元要 14 秒
# (後者是 R005 本來就有的,新規則重用同一個 _GAP,等於把常數放大)。
#
# 門檻給得很寬:平方的版本在這個長度要幾百秒,所以慢機器也不會偽紅。

import time  # noqa: E402


# 這幾個 payload 有十幾萬字元,而 pytest 會把參數原文放進測試名稱、再寫進
# PYTEST_CURRENT_TEST 這個環境變數。Windows 的環境變數上限是 32767 字元,名稱一長
# 這幾條就會在 setup 階段直接 ValueError。所以一定要給 ids。
@pytest.mark.parametrize("payload,label", [
    ("{{" * 50_000, "不閉合的 Jinja 開頭"),
    ("DB_NAME /*" * 20_000, "不閉合的註解開頭"),
    ("EXEC /*" * 20_000, "不閉合的註解接在 EXEC 後面"),
    ("a.b.c." * 20_000, "大量的點分隔名稱"),
], ids=("jinja-open", "comment-open", "comment-after-exec", "dotted-names"))
def test_特製內容不會讓規則層卡住(payload, label):
    t = time.perf_counter()
    run_rules(payload)
    took = time.perf_counter() - t
    assert took < 5, f"{label}({len(payload):,} 字元)花了 {took:.1f} 秒"


def test_長度上限不影響正常的寫法():
    """收斂的是「掃多遠」,不是「抓不抓得到」。"""
    assert _hits("SELECT * FROM OPENROWSET/* 說明 */('x')", "R006")
    assert _hits("{% if  target.name == 'prod' %}x{% endif %}", "H008")
    assert _hits("{% if  target.name == 'prod' %}x{% endif %}", "H008")


# ─────────────── R009 的漏網寫法(#85 review 第 1 點)───────────────

@pytest.mark.parametrize("sql", [
    "EXEC sp_executesql N'SELECT 1'",
    "EXEC sys.sp_executesql N'SELECT 1'",          # Microsoft 文件建議的寫法
    "EXEC master.dbo.sp_executesql N'SELECT 1'",
    "EXEC master..sp_executesql N'SELECT 1'",
    "EXEC [sp_executesql] N'SELECT 1'",
])
def test_sp_executesql_前面有限定名稱也算(sql):
    """lookbehind 排除點與方括號的話,只抓得到最沒有修飾的那一種 ——
    跟 xp_cmdshell 是同一個坑。"""
    assert _hits(sql, "R009"), sql


@pytest.mark.parametrize("sql", [
    "DECLARE @p sysname = N'master..xp_' + N'cmdshell'; EXEC @p N'whoami';",
    "EXECUTE @proc_name",
])
def test_不帶括號的EXEC接變數也算動態SQL(sql):
    """變數裡放的是預存程序名稱時會呼叫那一支,延伸預存程序也可以。名稱用字串拼
    出來,R007 的文字比對就看不到 xp_cmdshell,呼叫的是哪一支要到執行時才知道。"""
    assert _hits(sql, "R009"), sql


@pytest.mark.parametrize("sql", [
    "EXEC @rc = dbo.refresh_summary",              # 接回傳值,一般呼叫
    "EXEC @rc=dbo.refresh_summary",
    "EXEC dbo.refresh_summary @day = '2026-06-01'",
])
def test_接回傳值與一般預存程序呼叫不算(sql):
    assert not _hits(sql, "R009"), sql


# ─────────── 被改到的樣板,原文也要掃(#85 review 第 2 點)───────────

def test_展開成功的樣板原文也掃環境相依的寫法():
    """`target.schema` 這類編譯期的寫法決定的是「展開出哪一段 SQL」,它們自己不會
    留在展開結果裡 —— 只掃展開後的話,用它們分環境的 model 一條都看不到。"""
    src = ("{% if target.schema != 'dbo' %}\nDELETE FROM audit_log;\n{% endif %}\n"
           "SELECT 1 AS ok")
    assert _hits(src, "H008"), "原文掃得到 target.schema"


def test_合併原文的命中時同一條不重複():
    from orchestrator.pipeline import _merge_env_hits
    hit = {"rule": "H008", "message": "m", "severity": "hint"}
    assert _merge_env_hits([dict(hit)], {"hits": [dict(hit)]}) == [hit]


def test_只併環境那一家族():
    """其餘規則由展開後的結果負責,重複報會指到同一件事兩次。"""
    from orchestrator.pipeline import _merge_env_hits
    out = _merge_env_hits([], {"hits": [{"rule": "R002", "message": "select *"},
                                        {"rule": "H008", "message": "target"}]})
    assert [h["rule"] for h in out] == ["H008"]


ENV_MODEL = ("{{ config(materialized='table') }}\n"
             "{% if target.schema != 'dbo' %}\nSELECT 1 AS dev\n"
             "{% else %}\nSELECT 2 AS prod\n{% endif %}\n")


def test_prescan對展開成功的樣板也掃原文():
    """走完整的 prescan:只驗規則層自己的話,把「原文也掃」那一步從 prescan 拿掉
    也不會紅 —— 而展開成功時 `target.schema` 早就不在展開結果裡了。"""
    project = dict(DBT.PROJECT)
    project["models/mrt_big.sql"] = ENV_MODEL
    mr = DBT._mr([DBT._f("models/mrt_big.sql")])
    ctx = DBT._ctx(files=project, mr=mr)
    pre = asyncio.run(prescan(_RealRulesHub(), mr["files"], DBT.CFG, ctx))

    entry = next(e for e in pre if e["path"] == "models/mrt_big.sql")
    assert "dbt_render_error" not in entry, "這個樣板要展開得成功,情境才成立"
    assert "H008" in [h["rule"] for h in entry["rules"]], entry["rules"]
