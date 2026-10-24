"""sqltools 工具模組 — 確定性 SQL 分析(AST / lint / rule-base),行程內直接呼叫。

run_rules() 是組織既有 rule-base 的掛載點:預設先放三條示範規則,
導入時把既有規則逐條移植進 RULES。
"""
import json
import logging
import os
import re

import sqlglot
from sqlglot import exp

from orchestrator.config import env_seconds
from orchestrator.isolation import run_isolated

logging.disable(logging.INFO)  # sqlfluff 的 INFO 輸出會灌爆 stderr

# 規則程式跑在 MS SQL 上。用別的方言解析,T-SQL 的方括號識別字、TOP、WITH (NOLOCK)
# 會解析失敗,AST 規則(R001 DML 無 WHERE、R002 SELECT * …)就整組跳過。
DIALECT = os.environ.get("SQL_DIALECT", "tsql")


def _parse(sql: str):
    return sqlglot.parse(sql, dialect=DIALECT)


def parse_ast(sql: str) -> str:
    """解析 SQL,回傳結構摘要:語句類型、涉及的表/欄位/JOIN、是否有 WHERE。"""
    out = []
    try:
        statements = _parse(sql)
    except Exception as e:
        # 這條只回給模型,而模型本來就看得到整份 SQL,所以不必像預掃那樣只留位置
        return json.dumps({"error": f"parse failed: {e}"}, ensure_ascii=False)
    for st in statements:
        if st is None:
            continue
        info = {
            "type": st.key.upper(),
            "tables": sorted({t.name for t in st.find_all(exp.Table)}),
            "columns": sorted({c.name for c in st.find_all(exp.Column)}),
            "joins": [j.sql(dialect=DIALECT) for j in st.find_all(exp.Join)],
            "has_where": st.find(exp.Where) is not None,
            "aggregates": sorted({f.key.upper() for f in st.find_all(exp.AggFunc)}),
        }
        out.append(info)
    return json.dumps(out, ensure_ascii=False)


# lint 的耗時隨內容超線性成長,特製的內容(例如反覆的 `/*,`)幾千字元就可能要數十秒。
# 預掃與模型的工具呼叫都在審查的同一條執行緒上同步呼叫,卡住就是整份審查卡住,外層的
# asyncio 時間上限也插不進來。所以放進子行程跑,超過時間強制終止(見 orchestrator.isolation)。
# 超過字數上限的不跑:幾乎一定逾時,先擋下省掉等待。
LINT_TIMEOUT_SECONDS = env_seconds("LINT_TIMEOUT_SECONDS", 60)
LINT_MAX_CHARS = 100_000
LINT_MAX_MEMORY_MB = 1024
# 沒跑完時回傳的那一筆。不回空清單:空清單代表「檢查過、沒有違規」。
LINT_INCOMPLETE_CODE = "LINT_INCOMPLETE"
_LINT_INCOMPLETE = json.dumps(
    [{"line": 0, "code": LINT_INCOMPLETE_CODE,
      "description": "lint 沒有跑完(超過時間上限、內容過長或執行失敗),這份內容沒有 lint 結果"}],
    ensure_ascii=False)


def lint(sql: str) -> str:
    """以 sqlfluff 檢查風格與已知反模式,回傳違規清單。

    在子行程執行,有時間上限;沒跑完(逾時、內容過長、子行程失敗、sqlfluff 丟例外)時
    回傳只有一筆 LINT_INCOMPLETE_CODE 的清單。"""
    if isinstance(sql, str) and len(sql) > LINT_MAX_CHARS:
        return _LINT_INCOMPLETE
    return run_isolated(_lint_in_child, (sql,), timeout_s=LINT_TIMEOUT_SECONDS,
                        max_memory_mb=LINT_MAX_MEMORY_MB,
                        on_failure=lambda _message: _LINT_INCOMPLETE)


def _lint_in_child(sql: str) -> str:
    import sqlfluff

    try:
        results = sqlfluff.lint(sql, dialect=DIALECT)
    except Exception:
        # sqlfluff 對內容本身的問題會回報成違規、不丟例外;會丟的是執行環境(例如方言設錯)。
        # 當成沒跑完:不然每個檔都靜靜地沒有 lint 結果。例外訊息可能帶原文,不回傳。
        return _LINT_INCOMPLETE
    slim = [
        {"line": r.get("start_line_no") or r.get("line_no"),
         "code": r.get("code"),
         "description": r.get("description")}
        for r in results
    ]
    return json.dumps(slim[:50], ensure_ascii=False)


# --- rule-base 掛載點 ---------------------------------------------------
# 每條規則:(規則碼, severity, 檢查函式) — 檢查函式吃 AST statement,回傳違規訊息或 None

def _r001_dml_without_where(st):
    if isinstance(st, (exp.Update, exp.Delete)) and st.find(exp.Where) is None:
        return f"{st.key.upper()} 沒有 WHERE 條件,將影響全表"
    return None


def _r002_select_star(st):
    for sel in st.find_all(exp.Select):
        for e in sel.expressions:
            if isinstance(e, exp.Star):
                return "SELECT * — 請明列欄位(含視圖/子查詢)"
    return None


def _r003_not_in_subquery(st):
    for node in st.find_all(exp.Not):
        inner = node.this
        if isinstance(inner, exp.In) and inner.find(exp.Select) is not None:
            return "NOT IN + 子查詢:子查詢含 NULL 時整個條件不成立(NULL 陷阱),建議改 NOT EXISTS"
    for node in st.find_all(exp.In):
        # sqlglot 有時以 In(not=True) 表示
        if node.args.get("not") and node.find(exp.Select) is not None:
            return "NOT IN + 子查詢:NULL 陷阱,建議改 NOT EXISTS"
    return None


def _h001_reversal_hint(st):
    """提示型規則:不直接判定,把檢核點交給 LLM 結合慣例判斷。"""
    sel = st.expression if hasattr(st, "expression") else st
    if not isinstance(sel, exp.Select):
        sel = st.find(exp.Select)
    if sel is None or sel.find(exp.AggFunc) is None:
        return None
    tables = {t.name.lower() for t in sel.find_all(exp.Table)}
    txt = sel.sql(dialect=DIALECT).upper()
    if "transactions" in tables and not re.search(r"REV|REVERS|沖正|退匯", txt):
        return ("此規則聚合交易金額,但未見沖正/退匯處理;"
                "若團隊慣例未註明資料已淨額,應以 info 提問確認")
    return None


def _h002_grain_hint(st):
    """GROUP BY 混用多表欄位 + 有 JOIN → 通報粒度檢核點(聯名戶重複通報)。"""
    sel = st.expression if hasattr(st, "expression") else st
    if not isinstance(sel, exp.Select):
        sel = st.find(exp.Select)
    if sel is None or sel.find(exp.Join) is None:
        return None
    group = sel.args.get("group")
    if group is None:
        return None
    group_tables = {c.table for c in group.find_all(exp.Column) if c.table}
    if len(group_tables) > 1:
        return ("GROUP BY 混用多個資料表的欄位且查詢含 JOIN:"
                "一對多關聯(如聯名戶)會使同一主體產生多筆結果;請確認通報粒度是否正確")
    return None


RULES = [
    ("R001", "blocker", _r001_dml_without_where),
    ("R002", "minor", _r002_select_star),
    ("R003", "major", _r003_not_in_subquery),
    ("H001", "hint", _h001_reversal_hint),
    ("H002", "hint", _h002_grain_hint),
]

# --- 文字層資安規則(regex on 原始 SQL,比 AST 更能抓 hardcode 字串) ---
# R004:hardcode 憑證/密碼/金鑰(資安紅線,一律 blocker)
#
# 命中的那段文字**不可以進訊息**。訊息會走到三個地方:送進模型的預掃結果、
# enforce_rules 補的 finding(標題取訊息前 60 字)、以及貼回 MR 的行內留言與
# review_output/ 的結果檔。憑證本來就在 diff 裡,所以不是新的外洩;問題是作者把那一行
# 改掉之後,**留言與結果檔裡還留著一份**,而留言不會跟著 diff 消失。
# 其他程式補報的 finding 一律不回顯作者寫的文字(注入掃描、寫入工具、截斷、同檔多處),
# 這裡要一致。
_SECRET_RE = re.compile(
    r"""(?ix)
    (?P<key>password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token
       |token|credential|conn(ection)?[_-]?string)
    \s*[:=]\s*['"][^'"]{3,}['"]
    | (?P<url>://[^:/\s'"]+:[^@/\s'"]+@)          # user:pass@host 連線字串
    """)
# 命中的鍵名 → **我們自己的**分類標籤。連鍵名都不原樣回顯:大小寫與 `api-key`/`api_key`
# 這些寫法也是作者的文字,而分類標籤是固定字串,說明的資訊量一樣夠。
_SECRET_KINDS = {
    "password": "密碼", "passwd": "密碼", "pwd": "密碼",
    "secret": "密鑰", "apikey": "API 金鑰", "accesskey": "存取金鑰",
    "authtoken": "存取權杖", "token": "權杖", "credential": "憑證",
    "connectionstring": "連線字串", "connstring": "連線字串",
}


def parse_error_text(e: Exception) -> str:
    """解析失敗的說明:**只留位置,不帶任何原文**。

    為什麼不是「把疑似憑證的部分遮掉」:要防的是「解析錯誤把原文帶出去」,跟那段原文
    像不像密碼無關。用樣式遮只遮得到樣式認得的寫法(`N'…'` 的 Unicode 字串、位置參數
    `EXEC sp_addlogin 'app', '…'`、沒加引號的值都漏),而這條訊息會變成「預掃無法解析
    此檔的 SQL」那條 finding 的內文,貼回 MR 留言、寫進 review_output/。

    `ParseError.errors` 的 `line` / `col` 可以直接用;`description`、`start_context`、
    `highlight`、`end_context` 一概不用——它們夾著 token 的原文。其他例外(斷詞失敗等)
    的訊息同樣帶原文,所以只留例外類型。
    """
    spots = []
    for d in getattr(e, "errors", None) or []:
        if isinstance(d, dict) and d.get("line") is not None:
            spots.append(f"第 {d['line']} 行第 {d.get('col', '?')} 欄")
    where = "、".join(dict.fromkeys(spots))[:120] if spots else type(e).__name__
    return f"parse failed: 語法無法解析({where})"


def _secret_kind(m: "re.Match") -> str:
    """命中的是哪一類憑證(固定字串,不含任何比對到的原文)。"""
    if m.group("url"):
        return "連線字串含帳號與密碼"
    key = re.sub(r"[_-]", "", m.group("key") or "").lower()
    return f"{_SECRET_KINDS.get(key, '憑證')}類的鍵指定了字串常值"
# H004:看起來異常的查詢(撈憑證欄位、明碼比對密碼)——交 LLM 判斷是否正當
_CRED_COL_RE = re.compile(r"(?i)\bSELECT\b[^;]*\b(password|passwd|pwd|secret|"
                          r"api[_-]?key|token|private[_-]?key|cvv|card[_-]?no)\b")
_PW_COMPARE_RE = re.compile(r"(?i)\b(password|passwd|pwd)\b\s*[=]\s*['\"]")
# H005:對 account_id/主鍵套用遮罩(遮罩函式內含 account_id,且輸出別名仍是 account_id)——
# 若規格要求明碼輸出供下游作業,遮罩會使其失去作用。容忍巢狀括號故用有界 .*? 跨越
_MASKED_KEY_RE = re.compile(
    r"(?i)(SUBSTRING|SUBSTR|LEFT|RIGHT|MASK|REGEXP_REPLACE|OVERLAY|CONCAT)\s*\("
    r".{0,160}?\b(account_id|acct_id|account_no|acct_no)\b"
    r".{0,160}?\bAS\s+(account_id|acct_id|account_no)\b")
# R005:讀取執行環境(資料庫名、主機、登入身分、系統表)。規則程式不需要知道自己跑在
# 哪裡;會讀的話,就能在沙盒裡照規格跑、到正式環境才做別的事(例如
# `AND DB_NAME() NOT LIKE 'segcra%'`)——執行驗證反而會通過,規則層與沙盒都擋不住。
# 刻意**不排除註解裡的寫法**:去註解的 regex 會被字串裡的 `--` 騙(`'--' AND DB_NAME()`
# 會連真正會執行的呼叫一起被當成註解去掉);寫在註解裡的誤報只是多一次人工確認。
# 函式名與括號之間可以夾空白**或註解**:`DB_NAME/**/()` 在 T-SQL 是合法的,
# 只容許空白就漏掉了(#27 review)。`--` 到行尾的註解同理。
# 長度有上限:沒有上限的話,`DB_NAME /*` 這種**不閉合**的註解開頭,每一個出現位置都會
# 一路掃到檔尾才宣告失敗,整體是平方時間(實測 8 萬字元要 14 秒)。而這些內容由 MR
# 決定。函式名與括號之間的空白或註解本來就很短,給得很寬也只是常數。
_GAP = r"(?:\s|/\*(?:[^*]|\*(?!/)){0,500}\*/|--[^\n]*\n){0,30}"
# 需要括號的環境函式。分三類只是為了讀起來分得清,比對時三條都跑。
_ENV_FUNC_RE = re.compile(
    r"(?i)(?<![\w.@\[])("
    # 資料庫 / 連線資訊
    r"DB_NAME|DB_ID|ORIGINAL_DB_NAME|DATABASEPROPERTYEX|SESSION_CONTEXT|CONTEXT_INFO"
    r"|CONNECTIONPROPERTY"
    # 主機 / 伺服器
    r"|HOST_NAME|HOST_ID|SERVERPROPERTY|APP_NAME"
    # 登入身分 / 權限
    r"|SUSER_NAME|SUSER_SNAME|SUSER_ID|SUSER_SID|ORIGINAL_LOGIN|USER_NAME|USER_ID"
    r"|IS_MEMBER|IS_SRVROLEMEMBER|IS_ROLEMEMBER|HAS_PERMS_BY_NAME"
    r")" + _GAP + r"\(")
# 不帶括號的(niladic)環境函式與全域變數。這類沒有 `(` 可以當界線,所以兩邊都要
# 卡字界:`CURRENT_USER_ID` 這種欄位名不會命中(底線是 word char,\b 不成立)。
_ENV_VAR_RE = re.compile(r"(?i)@@(SERVERNAME|SERVICENAME|VERSION|SPID)\b"
                         r"|(?<![\w.@\[])(SYSTEM_USER|CURRENT_USER|SESSION_USER)\b")
# 系統目錄。`sys.` 開頭**一律**算:只列幾個名字會漏掉 sys.server_principals、
# sys.configurations、sys.all_objects…(#27 review)。
#
# 前面的 lookbehind 只排除 word char(`mysys.objects` 不算),**不排除點**——
# 原本寫 (?<![\w.]) 把 `master.sys.databases` 這種三段式名稱排除掉了,
# 而 `[master].[sys].[databases]` 反而抓得到,行為不一致(#27 review 指出的 bug)。
_ENV_CATALOG_RE = re.compile(
    r"(?i)(?<!\w)\[?sys\]?" + _GAP + r"\." + _GAP + r"\[?\w+\]?"
    # 舊式的系統表名(master..sysdatabases、master.dbo.sysobjects),沒有 sys. 前綴
    r"|(?<!\w)\[?sys(databases|objects|columns|users|logins|processes|servers|indexes)\]?(?!\w)"
    r"|(?<!\w)\[?INFORMATION_SCHEMA\]?" + _GAP + r"\.")


def _env_probes(sql: str) -> list[str]:
    """回傳 SQL 裡讀取執行環境的寫法(去重、保留出現順序)。"""
    found = []
    for rx in (_ENV_FUNC_RE, _ENV_VAR_RE, _ENV_CATALOG_RE):
        for m in rx.finditer(sql):
            token = " ".join(m.group(0).split()).rstrip("(").strip()
            if token.upper() not in (t.upper() for t in found):
                found.append(token)
    return found


# ── 危險 SQL 原語(R006–R010)──
#
# 這些是封閉的關鍵字與結構集合,跟自然語言注入相反:用文字比對就能做得精準,不需要
# 靠模型。原本這幾種會被報成「語法錯誤」「效能問題」「缺暫存表」—— 類別錯了,看留言
# 的人就不會知道這是資料外送或權限變更,所以每一條的理由固定寫明是哪一類。
#
# 一律走文字層(不靠語法樹):這些寫法常常伴隨解析失敗(dbt 樣板、非 T-SQL 語法),
# 而解析失敗正是最該報的情況之一。
_EXTERNAL_SRC_RE = re.compile(
    r"(?i)(?<![\w.@\[])(OPENROWSET|OPENDATASOURCE|OPENQUERY|OPENXML)" + _GAP + r"\("
    r"|(?<![\w.@\[])BULK" + _GAP + r"INSERT(?!\w)")
# 四段式名稱(伺服器.資料庫.schema.物件):跨 linked server 讀寫。三段式(資料庫.schema.
# 物件)是正常寫法,所以要剛好三個點,前後都不能再接點 —— 否則 a.b.c.d.e 也會命中。
# 中間兩段可以是空的(server.db..table),最前與最後一定要有名字。
# 每一段都要像識別字(字母或底線開頭,或是方括號包起來)—— 只寫 \w+ 的話
# IP 位址 10.0.0.9 也是「四段」,寫死主機的連線字串會被報成 linked server。
_IDENT_PART = r"(?:\[[^\]]+\]|[A-Za-z_]\w*)"
_FOUR_PART_RE = re.compile(
    r"(?<![\w.\[])" + _IDENT_PART + r"\." + _IDENT_PART + r"?\."
    + _IDENT_PART + r"?\." + _IDENT_PART + r"(?![\w.])")
# 作業系統命令。lookbehind **不排除點**:`master..xp_cmdshell` 正是最常見的寫法,
# 排除點就等於只抓得到沒有限定名稱的那一種。
_OS_CMD_RE = re.compile(r"(?i)(?<![\w@\[])\[?xp_cmdshell\]?(?!\w)")
# 權限授予或切換。關鍵字後面要接空白,`grant_amount` 這種欄位名才不會命中。
_PRIVILEGE_RE = re.compile(
    r"(?i)(?<![\w.@\[])(GRANT|REVOKE|DENY)\s+(?!BY\b)"
    r"|(?<![\w.@\[])EXECUTE\s+AS(?!\w)"
    r"|(?<![\w.@\[])ALTER\s+(SERVER\s+)?ROLE(?!\w)")
# 動態 SQL:EXEC(…)、sp_executesql,或 EXEC 後面直接接變數。
#
# `sp_executesql` 的 lookbehind **不排除點與方括號**:`sys.sp_executesql` 是 Microsoft
# 文件建議的寫法(系統預存程序建議加 sys.),`master.dbo.sp_executesql`、`[sp_executesql]`
# 也都是同一支。排除掉的話只抓得到最沒有修飾的那一種(跟 xp_cmdshell 同一個坑)。
#
# `EXEC @變數`(不帶括號)也算:變數裡放的是預存程序名稱時會呼叫那一支,延伸預存程序
# 也可以 —— 名稱用字串拼出來,R007 的文字比對就看不到 xp_cmdshell,呼叫的是哪一支要到
# 執行時才知道。`EXEC @rc = dbo.某支`(接回傳值)是一般呼叫,不算。
_DYNAMIC_SQL_RE = re.compile(
    r"(?i)(?<![\w.@\[])(?:EXEC|EXECUTE)" + _GAP + r"\("
    r"|(?<![\w@])\[?sp_executesql\]?(?!\w)"
    # `\b` 不可省:沒有它的話 `\w+` 會回溯成 `@r`,負向前瞻就看不到 `@rc = ` 的等號,
    # 於是「接回傳值」那種一般呼叫也會被當成動態 SQL。
    r"|(?<![\w.@\[])(?:EXEC|EXECUTE)\s+@\w+\b(?!" + _GAP + r"=)")
# dbt 編譯期副作用:run_query / {% call statement %} 裡執行 DML。每次 compile 都會跑,
# 卻不在編譯出來的 SQL 裡,所以沙盒看不到它。
_DBT_EXEC_RE = re.compile(r"(?i)(?<![\w.])(run_query|statement)" + _GAP + r"\(")
_DML_RE = re.compile(
    r"(?i)(?<![\w.@\[])(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE)\s")


def _dangerous_primitives(sql: str) -> list[tuple[str, str, str]]:
    """危險 SQL 原語。回傳 [(規則碼, severity, 訊息)]。"""
    hits = []
    external = [" ".join(m.group(0).split()).rstrip("(").strip()
                for m in _EXTERNAL_SRC_RE.finditer(sql)]
    external += [m.group(0) for m in _FOUR_PART_RE.finditer(sql)]
    if external:
        hits.append(("R006", "blocker",
                     f"外部資料源讀寫:{'、'.join(dict.fromkeys(external))[:200]}——"
                     "規則 SQL 只該讀寫本資料庫裡的表。讀寫外部來源等於把資料送出去、"
                     "或把外部輸入帶進來,沙盒驗不到對方那一端。請改用既有的資料表,"
                     "或說明這條路徑的正當用途與授權"))
    if _OS_CMD_RE.search(sql):
        hits.append(("R007", "blocker",
                     "作業系統命令:規則 SQL 呼叫 xp_cmdshell——這會在資料庫主機上執行"
                     "作業系統指令,不是查詢。一律禁止"))
    priv = [" ".join(m.group(0).split()).strip() for m in _PRIVILEGE_RE.finditer(sql)]
    if priv:
        hits.append(("R008", "major",
                     f"權限授予或切換:{'、'.join(dict.fromkeys(priv))[:120]}——規則 SQL 不該"
                     "變更授權,也不該換一個身分執行(換身分之後的行為沙盒驗不到)。"
                     "請移除,或說明正當用途"))
    if _DYNAMIC_SQL_RE.search(sql):
        hits.append(("R009", "major",
                     "動態 SQL:把字串組出來再執行(EXEC(…) 或 sp_executesql)——"
                     "實際執行的內容在編譯期看不到,規則層與沙盒都驗不到,而且可能藏有"
                     "讀取執行環境的寫法(例如 EXEC('SELECT DB_' + 'NAME()'),文字比對"
                     "看不到)。請改寫成靜態 SQL,或說明正當用途"))
    if _DBT_EXEC_RE.search(sql) and _DML_RE.search(sql):
        hits.append(("R010", "blocker",
                     "dbt 編譯期副作用:macro 裡用 run_query / statement 執行 DML——"
                     "每次 compile 都會跑,卻不在編譯出來的 SQL 裡,所以執行驗證看不到它。"
                     "請改成在 model 裡做,或說明正當用途"))
    return hits


# ── 只適合當提醒的環境相依寫法(H006–H009;#41 第 4 點)──
#
# 這些正常程式也會用,所以不給 major —— 但值得請人確認用途:它們都會讓同一段 SQL
# 在沙盒與正式環境得到不同結果,而執行驗證只跑得到沙盒那一邊。
# 真正「規則不該知道」的那些(DB_NAME、SUSER_NAME、sys. 目錄…)是 R005,給 major。

# 物件存在性探測。tempdb 的暫存表要排除:`OBJECT_ID('tempdb..#x')` 是建暫存表前的
# 標準寫法,報它只會製造雜訊。
_OBJ_PROBE_RE = re.compile(
    r"(?i)(?<![\w.@\[])(OBJECT_ID|COL_LENGTH|OBJECTPROPERTY|COLUMNPROPERTY)" + _GAP + r"\(")
_TEMPDB_ARG_RE = re.compile(r"(?i)\(\s*'[^']*tempdb[^']*'")
# 連線的語系與日期設定:沙盒與正式環境可能不同,同一段 SQL 的結果會跟著不同。
_LOCALE_RE = re.compile(r"(?i)@@(LANGUAGE|DATEFIRST|LANGID|DATEFORMAT)\b")
# dbt 編譯期的環境資訊。開發與正式分流本來就會用到,所以只提醒。
#
# `target.` 與 `var(` **只在 Jinja 區段裡**才算,否則會誤報兩種很常見的 T-SQL:
# `MERGE` 的慣用別名就叫 target(`target.id`),而 `VAR()` 是變異數聚合函式。
# `env_var(` 是 dbt 專有的名字,不受這個限制。
_JINJA = r"\{\{|\{%"
# 從 `{{`/`{%` 往後找關鍵字的範圍也要有上限:`{{` 開頭而**沒有閉合**的內容,每一個
# `{{` 都會掃到檔尾,整體是平方時間(實測 4 萬字元要 35 秒)。Jinja 的表達式很短。
_IN_JINJA = r"[^}]{0,200}?"
# 命中的「寫法」只取關鍵字本身(具名群組 tok):比對需要前面那段 Jinja 當界線,
# 但把 `{% set target_date = var` 整段印進訊息只會讓人看不懂命中的是什麼。
# **不含 `var()`**:它是帶參數的一般寫法,實際專案幾乎每個 model 都用;拿它當條件
# 來分環境很少見,真的那樣寫也會被「原文有寫入語句、展開結果沒有」那道擋到。
_DBT_ENV_RE = re.compile(
    r"(?i)(?:" + _JINJA + r")" + _IN_JINJA + r"(?<![\w.])(?P<tok>target" + _GAP + r"\." + _GAP
    + r"(?:name|database|schema|type|user))(?!\w)"
    r"|(?<![\w.])(?P<tok3>env_var)" + _GAP + r"\("
    r"|(?P<tok4>\{%" + _GAP + r"if" + _GAP + r"execute)(?!\w)")


def _dbt_env_token(m) -> str:
    for name in ("tok", "tok3", "tok4"):
        if m.group(name):
            return " ".join(m.group(name).split())
    return m.group(0)
# 現在時間跟寫死的日期比較:上線之後才會改變行為的寫法(定時炸彈)。
_NOW_FUNC = r"(?:GETDATE|GETUTCDATE|SYSDATETIME|SYSUTCDATETIME|CURRENT_TIMESTAMP)"
_DATE_LITERAL = r"'\d{4}[-/]?\d{2}[-/]?\d{2}[^']*'"
_TIME_BOMB_RE = re.compile(
    r"(?i)" + _NOW_FUNC + r"(?:" + _GAP + r"\(" + _GAP + r"\))?" + _GAP
    + r"(?:[<>=]=?|<>|!=|BETWEEN\s)" + _GAP + _DATE_LITERAL
    + r"|" + _DATE_LITERAL + _GAP + r"(?:[<>=]=?|<>|!=)" + _GAP + _NOW_FUNC)
# 定義不在這次 MR 裡的自訂函式或 view:讀取環境的程式可能藏在那裡面。
# 只認 schema 限定的呼叫(dbo.fn_x(...)),不然每個內建函式都會命中。
_USER_FUNC_RE = re.compile(r"(?i)(?<![\w.@\[])\[?dbo\]?\s*\.\s*\[?(\w+)\]?" + _GAP + r"\(")


def _env_hints(sql: str) -> list[tuple[str, str, str]]:
    """環境相依、但正常程式也會用的寫法。一律 hint:請人確認用途,不擋。"""
    hits = []
    probes = [m for m in _OBJ_PROBE_RE.finditer(sql)
              if not _TEMPDB_ARG_RE.match(sql, m.end() - 1)]
    if probes:
        names = dict.fromkeys(m.group(1).upper() for m in probes)
        hits.append(("H006", "hint",
                     f"檢查物件存不存在:{'、'.join(names)}——這會讓同一段 SQL 在沙盒與"
                     f"正式環境走不同分支,執行驗證只驗得到沙盒那一邊。請確認用途"))
    locale = dict.fromkeys(m.group(0).upper() for m in _LOCALE_RE.finditer(sql))
    if locale:
        hits.append(("H007", "hint",
                     f"讀取連線的語系/日期設定:{'、'.join(locale)}——沙盒與正式環境的設定"
                     f"可能不同,同一段 SQL 的結果會跟著不同。請確認用途"))
    dbt_env = dict.fromkeys(_dbt_env_token(m) for m in _DBT_ENV_RE.finditer(sql))
    if dbt_env:
        hits.append(("H008", "hint",
                     f"dbt 編譯期的環境資訊:{'、'.join(dbt_env)[:120]}——開發與正式分流"
                     f"本來就會用到,但它決定了編譯出來的 SQL,而執行驗證驗的是編譯後的"
                     f"結果。請確認分流的內容"))
    if _TIME_BOMB_RE.search(sql):
        hits.append(("H009", "hint",
                     "現在時間與寫死的日期比較:行為會在某個日期之後改變,審查與執行驗證"
                     "都是在今天跑的,驗不到那之後的行為。請確認是刻意的"))
    return hits


def _text_rules(sql: str):
    hits = []
    m = _SECRET_RE.search(sql)
    if m:
        hits.append(("R004", "blocker",
                     f"疑似硬編碼憑證/密碼/金鑰於 SQL:{_secret_kind(m)}——"
                     f"一律禁止,應改用參數/密鑰管理服務"))
    if _PW_COMPARE_RE.search(sql):
        hits.append(("H004", "hint",
                     "疑似以明碼字串比對密碼欄位:應以雜湊比對,且不得將密碼寫進 SQL"))
    elif _CRED_COL_RE.search(sql):
        hits.append(("H004", "hint",
                     "查詢選取了憑證/密碼/卡號類敏感欄位:請確認此存取的正當性與最小必要"))
    probes = _env_probes(sql)
    if probes:
        hits.append(("R005", "major",
                     f"規則 SQL 讀取執行環境:{'、'.join(probes[:5])}——規則不需要知道自己跑在"
                     f"哪個資料庫/主機/身分下;讀取環境的寫法可以讓規則在執行驗證的沙盒裡照規格跑、"
                     f"到正式環境才做別的事,執行驗證會因此通過。請移除,或說明正當用途"))
    hits += _dangerous_primitives(sql)
    hits += _env_hints(sql)
    if _MASKED_KEY_RE.search(sql):
        hits.append(("H005", "hint",
                     "輸出的 account_id/主鍵疑似被遮罩:若規格要求明碼輸出(供下游部門"
                     "依帳號作業),遮罩會使其失去作用——請核對規格的輸出欄位遮罩要求"))
    return hits


def run_rules(sql: str) -> str:
    """執行 rule-base 檢查(組織既有規則的掛載點),回傳命中清單。"""
    hits = []
    parse_error = None
    try:
        statements = _parse(sql)
    except Exception as e:
        # 解析失敗時 AST 規則無法執行,但文字層規則(R004 明碼憑證等)不需要 AST,
        # 照樣要跑 —— 否則一段解析不了的 SQL 會連明碼密碼都一起放過。
        # 這條訊息會變成 finding 的內文、貼回 MR 留言,所以**只留位置**
        # (見 parse_error_text:sqlglot 的訊息夾著出錯位置附近的原文)
        statements, parse_error = [], parse_error_text(e)
    for st in statements:
        if st is None:
            continue
        for code, severity, fn in RULES:
            try:
                msg = fn(st)
            except Exception:
                continue
            if msg:
                hits.append({"rule": code, "severity": severity, "message": msg,
                             "statement": st.sql(dialect=DIALECT)[:200]})
    # 文字層資安規則(R004 hardcode 憑證、R005 讀取執行環境、H004 異常查詢)
    for code, severity, msg in _text_rules(sql):
        hits.append({"rule": code, "severity": severity, "message": msg,
                     "statement": ""})
    # H003(檔案層):涉及規則編號 → 必須與核定規格逐項核對
    for code in sorted(set(re.findall(r"\bR-\d{2,4}\b", sql))):
        hits.append({"rule": "H003", "severity": "hint",
                     "message": f"檔案涉及規則 {code}:必須與任務附上的核定規格(spec)逐項核對——"
                                f"比較運算子(達/以上=含=>=;超過=不含=>)、時間窗、"
                                f"通報粒度、豁免條件、**輸出欄位的遮罩/明碼**(規格說明碼者"
                                f"不得遮罩、說遮罩者必須遮罩);不符即為 major「實作與核定規格不符」"})
    if parse_error:
        return json.dumps({"error": parse_error, "hits": hits}, ensure_ascii=False)
    return json.dumps(hits, ensure_ascii=False)
