"""抽出 MR 檔案裡「由寫程式的人取名」的識別字,給注入掃描用。

同一句指示寫在註解、字串、路徑裡會被注入掃描擋下;寫成欄位別名、CTE 名稱、macro 名稱就掃不到,
而審查模型在 diff 裡讀得到它。這裡把這些名稱抽出來,交給注入掃描另外看(security.py,
怎麼拆字、怎麼比對由它決定)。

每筆是 `{"kind": 種類, "name": 原文, "line": 行號}`(拿不到可信的行號時不給 `line`):

  * SQL:`column_alias`(欄位別名,含 T-SQL 的 `別名 = 運算式`)、`cte_name`、`table_alias`
  * Jinja:`macro_name`、`macro_param`、`set_var`(`{% set %}` 的變數名稱)

**內容由 MR 決定**,所以:

  * **線性時間**:不建語法樹(Jinja 與 sqlglot 的解析在主行程沒有時間上限,見 pipeline 的
    `_macros_used_by_tree`),只做一次由左到右的掃描。正規式都沒有巢狀的重複;沒收尾的
    `{{`、`/*`、字串一律塗到檔尾就停,不會每個位置重掃一次
  * **有上限**:每個檔案最多 `MAX_IDENTIFIERS` 筆(同種類同名稱只記第一次);超過的不收,
    回傳的略過筆數讓掃描端照實報「有略過」,不靜默截斷
  * 認不出種類的寫法寧可多收(多收只是多比對一次,漏掉才會讓指示混過去)
"""
from __future__ import annotations

import bisect
import re

MAX_IDENTIFIERS = 300

COLUMN_ALIAS, CTE_NAME, TABLE_ALIAS = "column_alias", "cte_name", "table_alias"
MACRO_NAME, MACRO_PARAM, SET_VAR = "macro_name", "macro_param", "set_var"
KINDS = (COLUMN_ALIAS, CTE_NAME, TABLE_ALIAS, MACRO_NAME, MACRO_PARAM, SET_VAR)

# ── Jinja ──
# 參數清單不要求 `)` 收尾:比對不會失敗,沒收尾的 `{% macro a(` 直接吃到檔尾、只算一次;
# 要求收尾的話,很多個沒收尾的開頭每一個都掃到檔尾才失敗,就變成平方時間
_MACRO_RE = re.compile(r"\{%-?\s*macro\s+(\w+)\s*\(([^)]*)")
_SET_RE = re.compile(r"\{%-?\s*set\s+(\w+(?:\s*,\s*\w+)*)")
_PARAM_RE = re.compile(r"(?:^|,)\s*(\w+)")
_JINJA_OPEN = re.compile(r"\{[{%#]")
_JINJA_CLOSE = {"{": "}}", "%": "%}", "#": "#}"}

# ── SQL 的詞 ──(每個分支都一定往前吃至少一個字元;沒收尾的註解、字串、識別字吃到檔尾)
_TOKEN = re.compile(r"""
    (?P<space>\s+)
  | (?P<comment>--[^\n]*|/\*(?:[^*]|\*(?!/))*(?:\*/)?)
  | (?P<string>[nN]?'(?:[^']|'')*'?)
  | (?P<bracket>\[(?:[^\]]|\]\])*\]?)
  | (?P<quoted>"(?:[^"]|"")*"?|`[^`]*`?)
  | (?P<var>@@?\w+|\#\#?\w+)
  | (?P<number>\d+(?:\.\d+)?)
  | (?P<word>[^\W\d]\w*)
  | (?P<punct>.)
""", re.VERBOSE | re.DOTALL)

_IDENT = ("word", "bracket", "quoted")
# 子句的開頭:決定接下來看到的別名是欄位的還是表的
_SELECT_CLAUSE = {"SELECT"}
_TABLE_CLAUSE = {"FROM", "JOIN", "APPLY", "UPDATE", "INTO", "USING"}
# WHEN、OVER 不算:它們在 SELECT 清單裡(`CASE WHEN … END 別名`、`ROW_NUMBER() OVER (…) 別名`),
# 當成子句的話後面省略 AS 的別名就認不出來
_OTHER_CLAUSE = {"WHERE", "GROUP", "ORDER", "HAVING", "ON", "SET", "VALUES", "UNION", "EXCEPT",
                 "INTERSECT", "WITH", "OUTPUT", "MERGE", "DELETE", "INSERT", "PARTITION",
                 "QUALIFY", "WINDOW", "OPTION"}
# 不會是別名的保留字(大寫比對)。沒列到的字被當成別名只是多收,不影響安全
_RESERVED = (_SELECT_CLAUSE | _TABLE_CLAUSE | _OTHER_CLAUSE | {
    "AS", "AND", "OR", "NOT", "IN", "IS", "NULL", "LIKE", "BETWEEN", "EXISTS", "CASE", "THEN",
    "ELSE", "END", "BY", "ASC", "DESC", "DISTINCT", "ALL", "TOP", "PERCENT", "TIES", "INNER",
    "LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "NATURAL", "LIMIT", "OFFSET", "FETCH", "NEXT",
    "ROWS", "ROW", "ONLY", "FIRST", "LAST", "RANGE", "PRECEDING", "FOLLOWING", "UNBOUNDED",
    "CURRENT", "PIVOT", "UNPIVOT", "FOR", "TABLESAMPLE", "WITHIN", "COLLATE", "ESCAPE", "ANY",
    "SOME", "DEFAULT", "TRUE", "FALSE", "CAST", "WHEN", "OVER", "CONVERT", "MATCHED", "TARGET", "SOURCE",
    "NOLOCK", "READUNCOMMITTED", "HOLDLOCK", "TABLE", "VIEW", "CREATE", "ALTER", "DROP",
    "TRUNCATE", "EXEC", "EXECUTE", "DECLARE", "BEGIN", "IF", "WHILE", "RETURN", "GO", "USE"})
# 運算式的結尾:後面直接接一個名稱時,那個名稱是別名(省略 AS 的寫法)
_EXPR_END_WORDS = {"END", "NULL", "TRUE", "FALSE"}


def extract(source, *, with_lines: bool = True,
            limit: int = MAX_IDENTIFIERS) -> tuple[list[dict], int]:
    """回傳 (識別字清單, 超過上限沒收的筆數)。source 不是字串時回 ([], 0)。"""
    if not isinstance(source, str) or not source:
        return [], 0
    out = _Collector(limit, with_lines)
    _jinja_names(source, out)
    # 塗掉 Jinja 之後位置會往前移,行號要用塗過的那份算(換行照留,行數跟原始檔一樣)
    _sql_names(_blank_jinja(source), out)
    return out.items, out.skipped


class _Collector:
    def __init__(self, limit: int, with_lines: bool):
        self.items: list[dict] = []
        self.skipped = 0
        self._seen: set = set()
        self._limit = limit
        self._with_lines = with_lines
        self._newlines: list = []

    def text(self, text: str) -> None:
        """接下來的位置(offset)是在這份文字裡量的。"""
        self._newlines = [m.start() for m in _NEWLINE.finditer(text)] if self._with_lines else []

    def add(self, kind: str, name: str, offset: int) -> None:
        if not name or (kind, name) in self._seen:
            return
        if len(self.items) >= self._limit:
            self.skipped += 1
            return
        self._seen.add((kind, name))
        item = {"kind": kind, "name": name}
        if self._with_lines:
            item["line"] = bisect.bisect_right(self._newlines, offset - 1) + 1
        self.items.append(item)


_NEWLINE = re.compile("\n")


def _jinja_names(source: str, out: _Collector) -> None:
    out.text(source)
    for m in _MACRO_RE.finditer(source):
        out.add(MACRO_NAME, m.group(1), m.start(1))
        for p in _PARAM_RE.finditer(m.group(2)):
            out.add(MACRO_PARAM, p.group(1), m.start(2) + p.start(1))
    for m in _SET_RE.finditer(source):
        for part in re.finditer(r"\w+", m.group(1)):
            out.add(SET_VAR, part.group(0), m.start(1) + part.start())


def _blank_jinja(source: str) -> str:
    """Jinja 的區段換成不影響 SQL 掃描的內容,**換行照留**(行號跟原始檔一樣)。

    `{{ … }}` 換成一個名稱 `x`(它在 SQL 裡就是一個值或表,`FROM {{ ref('a') }} t` 的 `t`
    才認得出是表的別名);`{% … %}`、`{# … #}` 換成空白。沒收尾的塗到檔尾。"""
    parts, pos = [], 0
    while True:
        m = _JINJA_OPEN.search(source, pos)
        if m is None:
            parts.append(source[pos:])
            break
        parts.append(source[pos:m.start()])
        close = source.find(_JINJA_CLOSE[m.group(0)[1]], m.end())
        end = len(source) if close < 0 else close + 2
        block = source[m.start():end]
        filler = "x" if m.group(0) == "{{" else " "
        parts.append(filler + "\n" * block.count("\n"))
        pos = end
    return "".join(parts)


def _ident_text(kind: str, text: str) -> str:
    if kind == "bracket":
        return text[1:-1].replace("]]", "]") if text.endswith("]") else text[1:]
    if kind == "quoted":
        q = text[0]
        body = text[1:-1] if len(text) > 1 and text.endswith(q) else text[1:]
        return body.replace(q * 2, q)
    return text


def _tokens(sql: str):
    for m in _TOKEN.finditer(sql):
        kind = m.lastgroup
        if kind in ("space", "comment", "string"):
            # 字串照樣是一個值(運算式的結尾);註解與空白不算詞
            if kind == "string":
                yield ("string", m.group(0), m.start())
            continue
        yield (kind, m.group(0), m.start())


def _sql_names(sql: str, out: _Collector) -> None:
    out.text(sql)
    toks = list(_tokens(sql))
    clause = None              # 這一層括號裡最近的子句
    stack: list = []           # 括號外層的子句
    for i, (kind, text, offset) in enumerate(toks):
        upper = text.upper() if kind == "word" else ""
        prev = toks[i - 1] if i > 0 else None
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if kind == "punct":
            if text == "(":
                stack.append(clause)
                clause = None
            elif text == ")":
                clause = stack.pop() if stack else None
            continue
        if upper in _SELECT_CLAUSE or upper in _TABLE_CLAUSE or upper in _OTHER_CLAUSE:
            clause = upper
            continue
        if kind not in _IDENT or upper in _RESERVED:
            continue
        name = _ident_text(kind, text)
        if _is_cte(toks, i, clause):            # 先判斷:`名稱 (欄位…) AS (` 後面接的也是括號
            out.add(CTE_NAME, name, offset)
            continue
        if nxt is not None and nxt[1] in (".", "("):
            continue                            # 限定名稱的前段或函式呼叫,不是取名
        if prev is not None and prev[0] == "word" and prev[1].upper() == "AS":
            # 只認 SELECT 清單與 FROM / JOIN 那一層:括號裡的 `CAST(x AS INT)` 不是取名
            if clause in _SELECT_CLAUSE or clause in _TABLE_CLAUSE:
                out.add(TABLE_ALIAS if clause in _TABLE_CLAUSE else COLUMN_ALIAS, name, offset)
            continue
        if clause in _SELECT_CLAUSE and nxt is not None and nxt[1] == "=" \
                and _starts_select_item(toks, i):
            out.add(COLUMN_ALIAS, name, offset)  # T-SQL:SELECT 別名 = 運算式
            continue
        if prev is not None and _ends_expression(prev) and clause in (_SELECT_CLAUSE | _TABLE_CLAUSE):
            out.add(TABLE_ALIAS if clause in _TABLE_CLAUSE else COLUMN_ALIAS, name, offset)


def _is_cte(toks: list, i: int, clause) -> bool:
    """`WITH 名稱 [(欄位…)] AS (` 或接在前一個 CTE 後面的 `, 名稱 AS (`。"""
    if clause != "WITH":
        return False
    prev = toks[i - 1] if i > 0 else None
    if prev is None or not (prev[1] == "," or prev[1].upper() == "WITH"):
        return False
    j = i + 1
    if j < len(toks) and toks[j][1] == "(":            # 欄位清單:找到對應的右括號
        depth = 0
        while j < len(toks):
            depth += toks[j][1] == "("
            depth -= toks[j][1] == ")"
            j += 1
            if depth == 0:
                break
    return (j + 1 < len(toks) and toks[j][1].upper() == "AS" and toks[j + 1][1] == "(")


def _starts_select_item(toks: list, i: int) -> bool:
    prev = toks[i - 1] if i > 0 else None
    if prev is None:
        return False
    if prev[1] == "," or prev[1].upper() in ("SELECT", "DISTINCT", "ALL"):
        return True
    # SELECT TOP 10 別名 = …
    before = toks[i - 2] if i > 1 else None
    return before is not None and before[1].upper() == "TOP"


def _ends_expression(tok) -> bool:
    kind, text = tok[0], tok[1]
    if kind in ("number", "string", "var") or (kind == "punct" and text == ")"):
        return True
    if kind == "word":
        upper = text.upper()
        return upper in _EXPR_END_WORDS or upper not in _RESERVED
    return kind in ("bracket", "quoted")
