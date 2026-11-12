"""危險 SQL 原語的清單(規則層 R006–R008、R011 用)。

每一項是一筆資料:屬於哪條規則、怎麼比對、出處。要補新的寫法只改這裡,不用動正規式。

清單照這幾份整理:

- **Microsoft 的 Server configuration options**:預設關閉、要用 `sp_configure` 打開才能用的
  功能(`Ole Automation Procedures`、`clr enabled`、`Database Mail XPs`、`external scripts
  enabled`、`Agent XPs`、`Ad Hoc Distributed Queries`、`polybase enabled`、`external rest
  endpoint enabled`、`xp_cmdshell`…)。每一個功能對應一組寫法,下面的出處寫的是對應的
  設定名稱。新版本加功能時,這頁會先出現。
- **Microsoft 各語法與系統預存程序的說明頁**:出處的網址就是那一頁。
- **MITRE ATT&CK T1505.001(SQL Stored Procedures)**:攻擊者用預存程序在主機上執行程式、
  常駐。
- 延伸預存程序 `xp_dirtree`、`xp_regread` 這類沒有官方文件(Microsoft 沒有公開說明頁),
  出處寫「review 列出」:是 review 依攻擊手法整理出來的。

只列「要打開才能用」的功能不夠:網路路徑、帳號與角色這些預設就能用,不會出現在設定清單裡,
所以另外列(資料外送、權限與帳號兩類)。

比對方式(`kind`):

- `proc`:系統預存程序的名稱。**前面接什麼都算**(`sys.`、`master.dbo.`、`master..`、
  `msdb.dbo.`、方括號、雙引號),也不要求前面有 `EXEC`(批次的第一句可以不寫)。
  `prefix=True` 時名稱是開頭(`sp_OA` 開頭的一整組)。
- `statement`:語句的關鍵字序列。每一格可以是一個字,或幾個字裡的任一個;字與字之間
  要有空白或註解(`CREATE/**/ENDPOINT` 是合法的 T-SQL)。

另外有兩種**不靠逐一列名稱**的比對,寫在 `sqltools.py`(它們不是一個名字,而是一種寫法):

- `sp_configure` 帶第二個參數(設定值)= 改伺服器設定;只帶一個參數是讀設定,不算。
- 字串常值以 `\\\\` 或 `//` 開頭 = 網路路徑,不管是哪個程序用到它。
- `EXEC` 呼叫任何 `xp_` 開頭的延伸預存程序 = 在 SQL Server 的處理序裡執行 DLL。
"""
from __future__ import annotations

from dataclasses import dataclass

# 規則碼
SERVER_CONFIG = "R011"     # 改伺服器設定、建立伺服器層級的物件
EXTERNAL = "R006"          # 資料外送與外部連線
HOST_EXEC = "R007"         # 執行作業系統命令或外部程式、存取主機
PRIVILEGE = "R008"         # 權限與帳號

_LEARN = "https://learn.microsoft.com/sql/"
_SP = "relational-databases/system-stored-procedures/"
_ST = "t-sql/statements/"
CONFIG_OPTIONS = _LEARN + "database-engine/configure-windows/server-configuration-options-sql-server"
MITRE_T1505_001 = "https://attack.mitre.org/techniques/T1505/001/"
REVIEW = "review 列出(沒有官方說明頁)"


def _sp(page: str) -> str:
    return f"{_LEARN}{_SP}{page}-transact-sql"


def _st(page: str) -> str:
    return f"{_LEARN}{_ST}{page}-transact-sql"


def _option(name: str) -> str:
    return f"Server configuration options:{name}(預設關閉) {CONFIG_OPTIONS}"


@dataclass(frozen=True)
class Primitive:
    rule: str
    kind: str                      # "proc" | "statement"
    pattern: tuple                 # proc:(名稱,);statement:每一格是字或幾個字的 tuple
    source: str
    prefix: bool = False           # proc 才用:名稱是開頭

    @property
    def label(self) -> str:
        """訊息裡顯示的寫法(比對到的原文另外印;這裡給清單與文件用)。"""
        if self.kind == "proc":
            return self.pattern[0] + ("*" if self.prefix else "")
        return " ".join("/".join(w) if isinstance(w, tuple) else w for w in self.pattern)


def _proc(rule, name, source, prefix=False):
    return Primitive(rule, "proc", (name,), source, prefix)


def _stmt(rule, words, source):
    return Primitive(rule, "statement", tuple(words), source)


# ── 1. 改伺服器設定、建立伺服器層級的物件(R011) ──
# `sp_configure` 帶設定值另外比對(不列選項名稱:任何設定被改都算)。
SERVER_CONFIG_LIST = (
    _stmt(SERVER_CONFIG, ["RECONFIGURE"], _LEARN + "t-sql/language-elements/reconfigure-transact-sql"),
    _stmt(SERVER_CONFIG, ["ALTER", "DATABASE"],
          _LEARN + _ST + "alter-database-transact-sql-set-options(TRUSTWORTHY、DB_CHAINING 等)"),
    _stmt(SERVER_CONFIG, ["ALTER", "SERVER", "CONFIGURATION"], _st("alter-server-configuration")),
    _proc(SERVER_CONFIG, "sp_procoption", _sp("sp-procoption") + "(SQL Server 啟動時自動執行);"
          + _option("scan for startup procs")),
    # 伺服器層級觸發程序(例如每次登入都會跑);事件通知 `ON SERVER` 不在這裡
    _stmt(SERVER_CONFIG, ["ON", "ALL", "SERVER"], _st("create-trigger") + "(DDL/登入觸發程序)"),
    _stmt(SERVER_CONFIG, [("CREATE", "ALTER"), "ENDPOINT"], _st("create-endpoint") + "(開網路監聽埠)"),
    _stmt(SERVER_CONFIG, [("CREATE", "ALTER"), "EVENT", "SESSION"], _st("create-event-session")),
)

# ── 2. 資料外送與外部連線(R006) ──
# 網路路徑另外比對(字串常值以 \\ 或 // 開頭)。OPENROWSET 等與四段式名稱在 sqltools.py。
EXTERNAL_LIST = (
    _proc(EXTERNAL, "sp_send_dbmail", _sp("sp-send-dbmail") + ";" + _option("Database Mail XPs")),
    _proc(EXTERNAL, "xp_sendmail", REVIEW + "(舊版 SQL Mail)"),
    _proc(EXTERNAL, "sp_addlinkedserver", _sp("sp-addlinkedserver")),
    _proc(EXTERNAL, "sp_addlinkedsrvlogin", _sp("sp-addlinkedsrvlogin")),
    _proc(EXTERNAL, "sp_serveroption", _sp("sp-serveroption")),
    _proc(EXTERNAL, "sp_addserver", _sp("sp-addserver")),
    _proc(EXTERNAL, "sp_addremotelogin", _sp("sp-addremotelogin") + ";" + _option("remote access")),
    _proc(EXTERNAL, "sp_addumpdevice", _sp("sp-addumpdevice")),
    _proc(EXTERNAL, "sp_invoke_external_rest_endpoint", _sp("sp-invoke-external-rest-endpoint")
          + ";" + _option("external rest endpoint enabled")),
    _stmt(EXTERNAL, [("CREATE", "ALTER"), "EXTERNAL", "DATA", "SOURCE"],
          _st("create-external-data-source") + ";" + _option("polybase enabled")),
    _stmt(EXTERNAL, ["CREATE", "EXTERNAL", "TABLE"],
          _st("create-external-table") + ";" + _option("allow polybase export")),
    # 備份 = 把整個資料庫或金鑰寫到資料庫以外;還原 = 把外部的內容帶進來
    _stmt(EXTERNAL, ["BACKUP", ("DATABASE", "LOG", "CERTIFICATE", "MASTER", "SERVICE", "SYMMETRIC")],
          _st("backup") + "、" + _st("backup-certificate") + "、" + _st("backup-master-key")),
    _stmt(EXTERNAL, ["RESTORE", ("DATABASE", "LOG", "FILELISTONLY", "HEADERONLY", "LABELONLY",
                                 "VERIFYONLY", "MASTER", "SERVICE", "SYMMETRIC")],
          _st("restore-statements")),
)

# ── 3. 執行作業系統命令或外部程式、存取主機(R007) ──
# 用 EXEC 呼叫的其他 xp_ 開頭延伸預存程序另外比對。
HOST_EXEC_LIST = (
    _proc(HOST_EXEC, "xp_cmdshell", _option("xp_cmdshell") + ";" + MITRE_T1505_001),
    _proc(HOST_EXEC, "sp_OA", _sp("sp-oacreate") + ";" + _option("Ole Automation Procedures"),
          prefix=True),
    _proc(HOST_EXEC, "sp_execute_external_script", _sp("sp-execute-external-script") + ";"
          + _option("external scripts enabled")),
    _stmt(HOST_EXEC, [("CREATE", "ALTER"), "EXTERNAL", "LIBRARY"], _st("create-external-library")),
    _stmt(HOST_EXEC, [("CREATE", "ALTER"), "ASSEMBLY"],
          _st("create-assembly") + ";" + _option("clr enabled") + ";" + MITRE_T1505_001),
    _proc(HOST_EXEC, "sp_addextendedproc", _sp("sp-addextendedproc") + ";" + MITRE_T1505_001),
    # SQL Agent 作業:作業步驟的 @subsystem 是 CmdExec、PowerShell 時就是執行命令;T-SQL 步驟
    # 也是在審查與執行驗證之外、之後才執行的程式
    _proc(HOST_EXEC, "sp_add_job", _sp("sp-add-job") + ";" + _option("Agent XPs")),
    _proc(HOST_EXEC, "sp_add_jobstep", _sp("sp-add-jobstep") + ";" + _option("Agent XPs")),
    _proc(HOST_EXEC, "sp_update_jobstep", _sp("sp-update-jobstep")),
    _proc(HOST_EXEC, "sp_add_jobserver", _sp("sp-add-jobserver")),
    _proc(HOST_EXEC, "sp_start_job", _sp("sp-start-job")),
    _proc(HOST_EXEC, "sp_add_proxy", _sp("sp-add-proxy")),
    _proc(HOST_EXEC, "sp_grant_proxy_to_subsystem", _sp("sp-grant-proxy-to-subsystem")),
    # 主機的登錄檔、服務與檔案系統(沒有官方說明頁的延伸預存程序)
    _proc(HOST_EXEC, "xp_reg", REVIEW + "(登錄檔)", prefix=True),
    _proc(HOST_EXEC, "xp_instance_reg", REVIEW + "(登錄檔)", prefix=True),
    _proc(HOST_EXEC, "xp_servicecontrol", REVIEW + "(啟停主機上的服務)"),
    _proc(HOST_EXEC, "xp_dirtree", REVIEW + "(列目錄;路徑可以是網路路徑)"),
    _proc(HOST_EXEC, "xp_subdirs", REVIEW + "(列目錄)"),
    _proc(HOST_EXEC, "xp_fileexist", REVIEW + "(檢查檔案)"),
    _proc(HOST_EXEC, "xp_fixeddrives", REVIEW + "(列磁碟)"),
    _proc(HOST_EXEC, "xp_availablemedia", REVIEW + "(列磁碟)"),
    _proc(HOST_EXEC, "xp_create_subdir", REVIEW + "(建目錄)"),
    _proc(HOST_EXEC, "xp_delete_file", REVIEW + "(刪檔案)"),
)

# ── 4. 權限與帳號(R008) ──
# GRANT/REVOKE/DENY、EXECUTE AS、ALTER (SERVER) ROLE 在 sqltools.py(關鍵字後面要接空白)。
_SEC_SP = _sp("security-stored-procedures")
PRIVILEGE_LIST = (
    _proc(PRIVILEGE, "sp_addrolemember", _sp("sp-addrolemember")),
    _proc(PRIVILEGE, "sp_droprolemember", _sp("sp-droprolemember")),
    _proc(PRIVILEGE, "sp_addsrvrolemember", _sp("sp-addsrvrolemember")),
    _proc(PRIVILEGE, "sp_dropsrvrolemember", _sp("sp-dropsrvrolemember")),
    _proc(PRIVILEGE, "sp_addlogin", _sp("sp-addlogin")),
    _proc(PRIVILEGE, "sp_droplogin", _sp("sp-droplogin")),
    _proc(PRIVILEGE, "sp_grantlogin", _sp("sp-grantlogin")),
    _proc(PRIVILEGE, "sp_revokelogin", _sp("sp-revokelogin")),
    _proc(PRIVILEGE, "sp_denylogin", _sp("sp-denylogin")),
    _proc(PRIVILEGE, "sp_adduser", _sp("sp-adduser")),
    _proc(PRIVILEGE, "sp_dropuser", _sp("sp-dropuser")),
    _proc(PRIVILEGE, "sp_grantdbaccess", _sp("sp-grantdbaccess")),
    _proc(PRIVILEGE, "sp_revokedbaccess", _sp("sp-revokedbaccess")),
    _proc(PRIVILEGE, "sp_change_users_login", _sp("sp-change-users-login")),
    _proc(PRIVILEGE, "sp_changedbowner", _sp("sp-changedbowner")),
    _proc(PRIVILEGE, "sp_password", _sp("sp-password")),
    _proc(PRIVILEGE, "sp_addapprole", _sp("sp-addapprole")),
    _proc(PRIVILEGE, "sp_setapprole", _sp("sp-setapprole")),
    _stmt(PRIVILEGE, ["ALTER", "AUTHORIZATION"], _st("alter-authorization")),
    _stmt(PRIVILEGE, [("CREATE", "ALTER", "DROP"), "LOGIN"], _st("create-login") + "、" + _st("alter-login")),
    _stmt(PRIVILEGE, [("CREATE", "ALTER", "DROP"), "USER"], _st("create-user") + "、" + _st("alter-user")),
    _stmt(PRIVILEGE, [("CREATE", "DROP"), "ROLE"], _st("create-role")),
    _stmt(PRIVILEGE, [("CREATE", "ALTER", "DROP"), "APPLICATION", "ROLE"], _st("create-application-role")),
    _stmt(PRIVILEGE, [("CREATE", "DROP"), "SERVER", "ROLE"], _st("create-server-role")),
    _stmt(PRIVILEGE, [("CREATE", "ALTER"), "CREDENTIAL"], _st("create-credential")),
    _stmt(PRIVILEGE, [("CREATE", "ALTER"), "DATABASE", "SCOPED", "CREDENTIAL"],
          _st("create-database-scoped-credential")),
    _stmt(PRIVILEGE, ["SETUSER"], _st("setuser") + "(舊的換身分寫法,等於 EXECUTE AS)"),
)

ALL = SERVER_CONFIG_LIST + EXTERNAL_LIST + HOST_EXEC_LIST + PRIVILEGE_LIST


def procs(rule: str | None = None) -> list[Primitive]:
    return [p for p in ALL if p.kind == "proc" and (rule is None or p.rule == rule)]


def statements(rule: str) -> list[Primitive]:
    return [p for p in ALL if p.kind == "statement" and p.rule == rule]
