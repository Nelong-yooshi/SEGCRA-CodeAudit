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


def lint(sql: str) -> str:
    """以 sqlfluff 檢查風格與已知反模式,回傳違規清單。"""
    import sqlfluff

    try:
        results = sqlfluff.lint(sql, dialect=DIALECT)
    except Exception as e:
        return json.dumps({"error": f"lint failed: {e}"}, ensure_ascii=False)
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
_SECRET_RE = re.compile(
    r"""(?ix)
    (password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token
       |token|credential|conn(ection)?[_-]?string)
    \s*[:=]\s*['"][^'"]{3,}['"]
    | ://[^:/\s'"]+:[^@/\s'"]+@          # user:pass@host 連線字串
    """)
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
_ENV_FUNC_RE = re.compile(
    r"(?i)(?<![\w.@\[])(DB_NAME|DB_ID|HOST_NAME|HOST_ID|SUSER_NAME|SUSER_SNAME|SUSER_ID"
    r"|ORIGINAL_LOGIN|APP_NAME|SERVERPROPERTY|DATABASEPROPERTYEX)\s*\(")
_ENV_VAR_RE = re.compile(r"(?i)@@(SERVERNAME|SERVICENAME|VERSION|SPID)\b"
                         r"|(?<![\w.@\[])SYSTEM_USER\b")
_ENV_CATALOG_RE = re.compile(
    r"(?i)(?<![\w.])\[?sys\]?\s*\.\s*\[?(databases|objects|tables|schemas|columns|servers"
    r"|sysdatabases|sysobjects|dm_\w+)\b"
    r"|(?<![\w.])\[?INFORMATION_SCHEMA\]?\s*\.")


def _env_probes(sql: str) -> list[str]:
    """回傳 SQL 裡讀取執行環境的寫法(去重、保留出現順序)。"""
    found = []
    for rx in (_ENV_FUNC_RE, _ENV_VAR_RE, _ENV_CATALOG_RE):
        for m in rx.finditer(sql):
            token = " ".join(m.group(0).split()).rstrip("(").strip()
            if token.upper() not in (t.upper() for t in found):
                found.append(token)
    return found


def _text_rules(sql: str):
    hits = []
    m = _SECRET_RE.search(sql)
    if m:
        hits.append(("R004", "blocker",
                     f"疑似硬編碼憑證/密碼/金鑰於 SQL:「{m.group(0)[:40]}」——"
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
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))      # 去掉 sqlglot 錯誤訊息裡的終端機色碼
        statements, parse_error = [], f"parse failed: {' '.join(msg.split())[:200]}"
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
