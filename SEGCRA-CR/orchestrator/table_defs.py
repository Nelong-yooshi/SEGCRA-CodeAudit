"""table_defs — 部署端放在 `specs/tables/` 的表定義,補進執行驗證用的規格「資料表定義」。

執行驗證要先在沙盒建出規則讀的表(spec_exec.execute_cases 依規格「資料表定義」建表)。
部署端新增或修改表時,把 SSMS「Script Table as → CREATE To」的輸出存一份成
`specs/tables/<schema>.<表名>.md`(一張表一個檔;可以包在 ```sql 圍欄裡,也可以不包)。
用 `.md`:`specs/` 底下的 `.md` 本來就被當成規格——預掃略過、不會被拿去執行、
MR 改到時列為「修改了規格」交人工。

**原文不進沙盒、也不進 prompt**:只解析出每個欄位的名稱與型別,重新組成
`CREATE TABLE [表] ([欄] 型別, ...)`。NOT NULL、DEFAULT、主鍵、外鍵、CHECK、UNIQUE、
IDENTITY(會擋住測資明確給值)、檔案群組等選項一律不帶;註解與其他語句一律丟掉。
型別保留 SQL Server 原本的寫法(不經 sqlglot 重組:它會把 ntext 寫成 VARCHAR(MAX)、
real 寫成 FLOAT,Unicode 與精度就變了)。

不採用、而是記一條說明的情況(不猜):
  * 檔名不是 `<schema>.<表名>.md`、檔裡沒有或不只一句 CREATE TABLE、表名與檔名不同
  * 名稱不是英數字與底線(跟規格資料表定義的表名同一個標準)
  * 規則用到計算欄、或用到不支援的型別(timestamp / rowversion、xml、geography、自訂型別……)
  * 同名的表有兩個 schema 的定義

欄位很多的表(超過 TRIM_THRESHOLD 欄)只留**名稱在規則 SQL 裡以完整單字出現過**的欄位:
測資每一列要依序給每一欄的值,90 多欄的表會讓測資生成的輸出過長。用文字比對而不是語法樹,
寧可多留也不漏;SQL 裡有 `SELECT *` 或 `別名.*` 時不裁。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TABLES_DIR = "specs/tables"
TRIM_THRESHOLD = 30
MAX_TABLE_FILES = 500
MAX_FILE_CHARS = 200_000
MAX_NOTES = 20

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FILE = re.compile(r"specs/tables/([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\.md")
_FENCE = re.compile(r"^[ \t]*```[^\n]*\n(.*?)^[ \t]*```", re.M | re.S)
_GO = re.compile(r"^[ \t]*GO(?:[ \t]+\d+)?[ \t]*;?[ \t]*$", re.I | re.M)
_CREATE = re.compile(r"\bCREATE\s+TABLE\b", re.I)
_TYPES = frozenset({
    "BIGINT", "INT", "SMALLINT", "TINYINT", "BIT", "DECIMAL", "NUMERIC", "MONEY", "SMALLMONEY",
    "FLOAT", "REAL", "DATE", "DATETIME", "DATETIME2", "SMALLDATETIME", "DATETIMEOFFSET", "TIME",
    "CHAR", "VARCHAR", "NCHAR", "NVARCHAR", "TEXT", "NTEXT", "BINARY", "VARBINARY",
    "UNIQUEIDENTIFIER"})
_TYPE_ARGS = re.compile(r"\(\s*(?:\d{1,4}|MAX)\s*(?:,\s*\d{1,2}\s*)?\)", re.I)
# 型別可以帶的參數:精度與小數位數、長度(MAX 只有變動長度的型別可以)、時間精度。
# 其他型別帶了參數(例如 INT(5))SQL Server 本來就不接受,當成不支援,不幫它拿掉
_ARGS_OK = {
    "DECIMAL": r"\(\d{1,2}(,\d{1,2})?\)", "NUMERIC": r"\(\d{1,2}(,\d{1,2})?\)",
    "FLOAT": r"\(\d{1,2}\)", "DATETIME2": r"\([0-7]\)", "DATETIMEOFFSET": r"\([0-7]\)",
    "TIME": r"\([0-7]\)", "CHAR": r"\(\d{1,4}\)", "NCHAR": r"\(\d{1,4}\)",
    "BINARY": r"\(\d{1,4}\)", "VARCHAR": r"\((\d{1,4}|MAX)\)", "NVARCHAR": r"\((\d{1,4}|MAX)\)",
    "VARBINARY": r"\((\d{1,4}|MAX)\)"}
_NOT_COLUMN = frozenset({"CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "INDEX", "PERIOD"})
_STAR = re.compile(r"\bSELECT\s+(?:DISTINCT\s+|ALL\s+|TOP\s*\(?\s*\d+\s*\)?\s*(?:PERCENT\s+)?)*\*"
                   r"|\b[A-Za-z_][A-Za-z0-9_]*\s*\.\s*\*", re.I)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

AMBIGUOUS = None


@dataclass(frozen=True)
class TableDef:
    schema: str
    name: str
    source: str                               # specs/tables/<schema>.<表名>.md
    columns: tuple = ()                       # ((欄名, 型別), ...),照定義的順序
    computed: tuple = ()                      # 計算欄的名稱
    unsupported: tuple = ()                   # 型別不支援的欄位名稱


# ------------------------------------------------------------------ 解析一個檔
def _strip_comments(text: str) -> str:
    """拿掉 /* */ 與 -- 註解(字串常值與方括號裡的不算)。"""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'" or ch == "[":
            end = "'" if ch == "'" else "]"
            j = i + 1
            while j < n:
                if text[j] == end:
                    if end == "'" and j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _matching_paren(text: str, start: int) -> int:
    """text[start] 是 '(',回傳對應的 ')' 位置;找不到回 -1。字串常值與方括號裡的括號不算。"""
    depth, i, n = 0, start, len(text)
    while i < n:
        ch = text[i]
        if ch in "'[":
            end = "'" if ch == "'" else "]"
            j = text.find(end, i + 1)
            if j < 0:
                return -1
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top(text: str) -> list[str]:
    """以最外層的逗號切開(括號、字串常值、方括號裡的逗號不算)。"""
    parts, depth, cur, i, n = [], 0, [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'[":
            end = "'" if ch == "'" else "]"
            j = text.find(end, i + 1)
            j = n - 1 if j < 0 else j
            cur.append(text[i:j + 1])
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _ident(token: str) -> str | None:
    """`[name]` 或 `name` → name;不是英數字與底線回 None。"""
    name = token[1:-1] if token.startswith("[") and token.endswith("]") else token
    return name if _IDENT.fullmatch(name) else None


def _qualified(text: str) -> list[str] | None:
    """`[dbo].[txn]` / `dbo.txn` / `txn` → 各段名稱;任何一段不合法回 None。"""
    parts = re.findall(r"\[[^\]]*\]|[^.\s\[\]]+", text)
    names = [_ident(p) for p in parts]
    return names if names and all(names) else None


def _column(item: str):
    """欄位定義 → ("col", 名稱, 型別) / ("computed", 名稱) / ("unsupported", 名稱) /
    ("skip",)(表層級的限制)/ ("bad", 原因)。"""
    m = re.match(r"(\[[^\]]*\]|[A-Za-z_][A-Za-z0-9_]*)\s*(.*)", item, re.S)
    if not m:
        return ("bad", "看不出欄位名稱")
    head = m.group(1)
    if head.upper() in _NOT_COLUMN:
        return ("skip",)
    name = _ident(head)
    if name is None:
        return ("bad", "欄位名稱不是英數字與底線")
    rest = m.group(2).strip()
    if re.match(r"AS\b", rest, re.I):
        return ("computed", name)
    t = re.match(r"(\[[A-Za-z0-9_]+\]|[A-Za-z_][A-Za-z0-9_]*)\s*", rest)
    if not t:
        return ("unsupported", name)
    base = t.group(1).strip("[]").upper()
    after = rest[t.end():]
    args = ""
    a = _TYPE_ARGS.match(after)
    if a:
        args = re.sub(r"\s+", "", a.group(0)).upper()
    elif after.startswith("("):
        return ("unsupported", name)
    if base not in _TYPES or (args and not re.fullmatch(_ARGS_OK.get(base, "$^"), args)):
        return ("unsupported", name)
    return ("col", name, base + args)


def parse_file(path: str, text: str) -> tuple[TableDef | None, str | None]:
    """一個表定義檔 → (TableDef, None) 或 (None, 不採用的原因)。"""
    m = _FILE.fullmatch(path or "")
    if not m:
        return None, "檔名不是 specs/tables/<schema>.<表名>.md"
    schema, name = m.groups()
    if not isinstance(text, str) or len(text) > MAX_FILE_CHARS:
        return None, "檔案太大"
    body = text.lstrip("﻿")
    blocks = _FENCE.findall(body)
    if blocks:
        body = "\n".join(blocks)
    body = _strip_comments(body)
    creates = [c for c in _CREATE.finditer(body)]
    if len(creates) != 1:
        return None, ("檔裡找不到 CREATE TABLE" if not creates else "檔裡不只一句 CREATE TABLE")
    batch_end = _GO.search(body, creates[0].end())
    stmt = body[creates[0].end():batch_end.start() if batch_end else len(body)]
    open_at = stmt.find("(")
    if open_at < 0:
        return None, "CREATE TABLE 沒有欄位清單"
    close_at = _matching_paren(stmt, open_at)
    if close_at < 0:
        return None, "CREATE TABLE 的括號沒有成對"
    names = _qualified(stmt[:open_at].strip())
    if names is None or len(names) > 2:
        return None, "表名不是英數字與底線,或帶了資料庫名"
    t_schema, t_name = (names if len(names) == 2 else ["dbo", names[0]])
    if (t_schema.lower(), t_name.lower()) != (schema.lower(), name.lower()):
        return None, "CREATE TABLE 的表名跟檔名不同"
    columns, computed, unsupported = [], [], []
    for item in _split_top(stmt[open_at + 1:close_at]):
        got = _column(item)
        if got[0] == "bad":
            return None, got[1]
        if got[0] == "col":
            columns.append((got[1], got[2]))
        elif got[0] == "computed":
            computed.append(got[1])
        elif got[0] == "unsupported":
            unsupported.append(got[1])
    if not columns and not computed and not unsupported:
        return None, "CREATE TABLE 沒有欄位"
    return TableDef(t_schema, t_name, path, tuple(columns), tuple(computed), tuple(unsupported)), None


def load(files: dict) -> tuple[dict, list[str]]:
    """{路徑: 內容} → ({表名(小寫): TableDef、AMBIGUOUS 或不採用的原因(字串)}, 說明)。
    只看 specs/tables/ 直接一層的 .md。解析不了的檔以檔名的表名記下原因:規則有用到那張表
    時才寫進報告(用不到的表不必每次都提醒);檔名本身不合格式的,記在第二個回傳值。"""
    defs: dict = {}
    notes: list[str] = []
    paths = sorted(p for p in files if isinstance(p, str) and p.startswith(TABLES_DIR + "/")
                   and p.endswith(".md") and p.count("/") == 2)
    if len(paths) > MAX_TABLE_FILES:
        notes.append(f"specs/tables/ 有 {len(paths)} 個檔,超過上限 {MAX_TABLE_FILES},後面的沒有讀")
        paths = paths[:MAX_TABLE_FILES]
    for path in paths:
        text = files[path]
        if not (isinstance(text, str) and text):
            continue                              # 這個 MR 刪掉的
        table, reason = parse_file(path, text)
        if table is None:
            m = _FILE.fullmatch(path)
            if m is None:
                notes.append(f"{_shown(path)}:{reason},沒有採用")
            else:
                key = m.group(2).lower()
                defs[key] = f"{_shown(path)}:{reason},沒有採用" if key not in defs else AMBIGUOUS
            continue
        key = table.name.lower()
        defs[key] = table if key not in defs else AMBIGUOUS
    return defs, notes


def _shown(path) -> str:
    return path if isinstance(path, str) and re.fullmatch(r"[A-Za-z0-9_./-]{1,300}", path) \
        else "(含特殊字元的檔名)"


# ------------------------------------------------------------------ 補進規格
_TABLES_HEADING = re.compile(r"^##[ \t]*資料表定義[^\n]*$", re.M)
_NEXT_HEADING = re.compile(r"^#{1,2}[ \t]", re.M)
_SPEC_FENCE = re.compile(r"^```", re.M)


def _ddl(table: TableDef, columns) -> str:
    cols = ", ".join(f"[{c}] {t}" for c, t in columns)
    return f"CREATE TABLE [{table.name}] ({cols});"


def _insert(spec_text: str, ddl: list[str]) -> str:
    """把 DDL 放進規格的「資料表定義」段:段落用圍欄就加一個圍欄,沒用就直接接在段尾;
    沒有這一段就在最後加一段。"""
    m = _TABLES_HEADING.search(spec_text)
    if m is None:
        return (spec_text.rstrip("\n") + "\n\n## 資料表定義(由 specs/tables/ 補上)\n\n```sql\n"
                + "\n".join(ddl) + "\n```\n")
    rest = spec_text[m.end():]
    nxt = _NEXT_HEADING.search(rest)
    end = m.end() + (nxt.start() if nxt else len(rest))
    section = spec_text[m.end():end]
    if _SPEC_FENCE.search(section):
        added = "\n```sql\n" + "\n".join(ddl) + "\n```\n"
    else:
        added = "\n" + "\n".join(ddl) + "\n"
    return spec_text[:end].rstrip("\n") + "\n" + added + ("\n" if nxt else "") + spec_text[end:]


def augment(spec_text, sql_texts, defs: dict, spec_tables: dict) -> tuple:
    """回傳 (補過的規格, 紀錄)。紀錄 None = 沒有用到 specs/tables/。

    sql_texts   要執行驗證的 SQL(或 dbt 樣板)原文;只用來看用到哪些表與欄位
    spec_tables 規格本身的資料表定義(dbt_exec.spec_tables 的結果):已經定義的表以規格為準
    """
    if not (isinstance(spec_text, str) and spec_text and defs):
        return spec_text, None
    joined = "\n".join(t for t in sql_texts if isinstance(t, str))
    words = {w.lower() for w in _WORD.findall(joined)}
    star = _STAR.search(joined) is not None
    ddl, tables, notes = [], [], []
    for key in sorted(defs):
        if key not in words:
            continue
        table = defs[key]
        if key in spec_tables:
            continue                              # 規格自己有定義:以規格為準
        if table is AMBIGUOUS:
            notes.append(f"表 {key} 在 specs/tables/ 有不只一個定義檔,沒有採用")
            continue
        if isinstance(table, str):
            notes.append(table)                   # 這張表的定義檔解析不了
            continue
        used_bad = [c for c in table.computed + table.unsupported if c.lower() in words]
        if used_bad:
            notes.append(f"表 {table.name}({table.source})的欄位 {'、'.join(used_bad)} 是計算欄或"
                         "不支援的型別,規則用到了它,定義不完整,沒有採用")
            continue
        columns = list(table.columns)
        dropped: list[str] = []
        if len(columns) > TRIM_THRESHOLD and not star:
            kept = [(c, t) for c, t in columns if c.lower() in words]
            if kept:
                dropped = [c for c, _ in columns if c.lower() not in words]
                columns = kept
        if not columns:
            notes.append(f"表 {table.name}({table.source})沒有可以建的欄位,沒有採用")
            continue
        ddl.append(_ddl(table, columns))
        tables.append({"table": table.name, "source": table.source, "columns": len(table.columns),
                       "kept": len(columns), "dropped": dropped})
    if not ddl and not notes:
        return spec_text, None
    record = {"tables": tables, "notes": notes[:MAX_NOTES]}
    return (_insert(spec_text, ddl) if ddl else spec_text), record
