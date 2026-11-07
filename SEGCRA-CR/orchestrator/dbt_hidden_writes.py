"""dbt 樣板裡沒有經過規則檢查的寫入語句。

規則層看的是展開後的 SQL。樣板可以拿展開器跟 dbt 真正執行時的差異當條件,讓寫入語句在審查時
不出現、實際執行時照跑(例如 `{% if invocation_id != '...' %}`)。差異可以減少,不能保證沒有,
所以這裡不靠「展開器跟 dbt 一樣」,改看樣板原文:

* **固定文字裡的寫入關鍵字**:每一個前面插一個隨機記號,用同一支展開器再展開一次(一般模式;
  樣板有呼叫 `is_incremental()` 時增量模式也一次)。**記號後面要緊接著那段固定文字**(從關鍵字到
  下一個 Jinja 標記為止;不分大小寫,`{% filter upper %}` 會改大小寫;結尾的空白不算,`-%}` 會
  修掉)才算展開到。只看記號有沒有出現不夠:樣板可以先把語句收進變數,再只印出前面的記號
  (`{{ w[:-22] }}`、`{{ w | replace(...) }}`)。哪一次都沒有展開到 → 那一句這次審查沒有展開到,
  不論用什麼條件藏。macro 產生的語句不帶記號,拿來掩護也沒用。
* **插記號的那次展開,去掉記號後必須跟第一次的展開結果相同**:不同代表樣板會因為記號改變輸出
  (例如檢查自己的內容、偵測到記號才印出完整語句),這時無法確認,一律報。
  比對前遮掉 uuid 格式的字串(`invocation_id` 每次展開都不同),只差在那裡的兩份輸出結構相同。
* **Jinja 標記裡的字串**(展開結果裡看不到,規則層沒有檢查到):
  * dbt 會拿去當 SQL 執行的位置——`pre_hook` / `post_hook`(含 `pre-hook` / `post-hook`)、
    `sql_header` 的值,`run_query(...)` 的參數——字串裡有寫入關鍵字就報;
  * 其他位置的字串要**像一句寫入語句**才報(`DELETE FROM`、`INSERT INTO`、`UPDATE x SET`、
    `MERGE INTO`、`DROP TABLE`…,見 _STATEMENT):`{% set q = 'DELETE FROM t' %}` 之後可能交給
    `run_query(q)`。`incremental_strategy='merge'`、`materialized` 的值、`log()` 的訊息不報。
  標記裡的名稱不算(`{% if execute %}` 的 execute 是 dbt 的變數)。
* Jinja 註解不算:dbt 執行時也不會輸出。
* 關鍵字拆開寫(`DEL{{ '' }}ETE`)也找得到:固定文字接起來再找一次。
* **沒有展開到的分支裡有輸出**(不只看關鍵字):`{% if %}` / `{% elif %}` / `{% else %}`、`{% for %}`
  裡面的每一個輸出——`{{ … }}` 與非空白的固定文字——也插記號(`{{ … }}` 的記號接在運算式前面:
  `{{ '記號' ~ (…) }}`,不動前後的空白)。哪一次展開都沒有出現 → 記成無法確認。關鍵字可以用樣板
  組出來(`{{ 'DEL' ~ 'ETE' }}`、`{% set k = 'DEL' + 'ETE' %}{{ k }}`),靜態看不出來;對準的是
  「這段輸出審查時沒有產生過」,不論它會產生什麼。分支外面的輸出只要那個 model / macro 有展開就一定
  產生,不用插。用 `return()` 回傳的 macro 不輸出文字(dbt 丟掉它的輸出),記號看不出分支有沒有
  走到:分支裡有輸出時直接記成無法確認。

MR 改到的 macro 檔(find_hidden_writes_in_macro):同樣的方法,從呼叫它的 model 展開(把 macro 檔換成
插了記號的版本);只看 macro 與自訂物化的區塊裡面。沒有 model 呼叫、或呼叫它的 model 都展開失敗時
記成無法確認。

找不到的:有展開到的輸出裡、在 Jinja 裡用運算組出來的關鍵字(`'ETELED' | reverse`,或
`{{ 'DEL' ~ 'ETE' if 條件 else '' }}` 這種寫在同一個運算式裡的條件)——靜態看不出運算結果,
這次展開的結果規則層看得到,但換個環境值就不同;以及 MR 沒改到的 macro 檔裡的。審查模型在
diff 裡看得到這些原文。

結果只有關鍵字(固定集合)與行號,不帶原文。
"""
from __future__ import annotations

import bisect
import re
import secrets

WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "TRUNCATE", "ALTER", "CREATE",
                  "EXEC", "EXECUTE", "GRANT", "REVOKE")
# 前後不能接識別字的字元:`updated_at`、`@delete`、`#drop_tmp` 都不是關鍵字
_KEYWORD_RE = re.compile(r"(?<![A-Za-z0-9_@#$])(" + "|".join(WRITE_KEYWORDS) + r")(?![A-Za-z0-9_@#$])",
                         re.IGNORECASE)
# 標記字串裡「像一句寫入語句」:關鍵字後面接著該有的下一個字。每一條都從關鍵字的位置 match,
# 量詞之間有固定的分隔,沒有巢狀的重複(不會回溯爆炸)
_OBJECTS = (r"(?:TABLE|VIEW|SCHEMA|DATABASE|INDEX|PROCEDURE|PROC|FUNCTION|TRIGGER|SYNONYM|"
            r"SEQUENCE|TYPE|USER|LOGIN|ROLE)\b")
_TOP = r"(?:TOP\s*\([^)]*\)\s*)?"
_STATEMENT = re.compile(
    r"DELETE\s+" + _TOP + r"(?:FROM\b|\S+\s+(?:WHERE|FROM|OUTPUT)\b)"
    r"|INSERT\s+" + _TOP + r"(?:INTO\b|\S+\s*(?:\(|VALUES\b|SELECT\b|EXEC))"
    r"|UPDATE\s+" + _TOP + r"\S+\s+SET\b"
    r"|MERGE\s+(?:INTO\b|\S+\s+(?:(?:AS\s+)?\w+\s+)?USING\b)"
    r"|(?:DROP|ALTER)\s+" + _OBJECTS +
    r"|TRUNCATE\s+TABLE\b"
    r"|CREATE\s+(?:(?:OR\s+ALTER|UNIQUE|CLUSTERED|NONCLUSTERED)\s+)*" + _OBJECTS +
    r"|EXEC(?:UTE)?\s*(?:\(|@|\[|(?:sp|xp)_|\w+\.\w)"
    r"|(?:GRANT|REVOKE)\s+\w+(?:\s*,\s*\w+)*\s+(?:ON|TO|FROM)\b",
    re.IGNORECASE)
# dbt 會拿去當 SQL 執行的設定
# 整個字串就是一句 `EXEC 名稱`(字串裡其他地方出現 exec 加一個字太常見,例如 log 訊息)
_WHOLE_EXEC = re.compile(r"EXEC(?:UTE)?\s+[\w.\[\]]+\s*;?", re.IGNORECASE)
_EXEC_KEYS = frozenset({"pre_hook", "post_hook", "pre-hook", "post-hook", "sql_header"})
_KEY_RE = re.compile(r"([A-Za-z_][\w-]*)\s*[=:]")
_STRING_KEY_RE = re.compile(r"\s*:")
_RUN_QUERY_RE = re.compile(r"\brun_query\s*\(")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TAG_PAIRS = {"{{": "}}", "{%": "%}", "{#": "#}"}

# 固定文字裡的寫入關鍵字超過這個數量就不逐一插記號,直接記成無法確認(交人工)
MAX_MARKED_KEYWORDS = 500
# 分支裡的輸出超過這個數量就不逐一插記號,直接記成無法確認(交人工)
MAX_BRANCH_OUTPUTS = 2000

UNREACHED = "unreached"
IN_TAGS = "in_tags"
UNVERIFIED = "unverified"
UNREACHED_OUTPUT = "unreached_output"

# 分支的開頭與結尾(`{%- if`、`{%+ for` 也算)。elif / else 在同一層裡,不改變層數
_BRANCH_OPEN = re.compile(r"\{%[-+]?\s*(?:if|for)\b")
_BRANCH_CLOSE = re.compile(r"\{%[-+]?\s*end(?:if|for)\b")
_RETURN_CALL = re.compile(r"(?<![\w.])return\s*\(")


def _segments(text: str):
    """把樣板切成 (種類, 起點, 終點):literal(固定文字)、tag(`{{ }}`、`{% %}`)、comment(`{# #}`)。
    線性時間(不用正規表達式,理由同 dbt_render.is_dbt_template)。沒有結尾的標記算到檔尾。"""
    start, i, n = 0, 0, len(text)
    while i < n:
        closer = _TAG_PAIRS.get(text[i:i + 2])
        if closer is None:
            i += 1
            continue
        if i > start:
            yield "literal", start, i
        kind = "comment" if closer == "#}" else "tag"
        end = text.find(closer, i + 2)
        stop = n if end < 0 else end + 2
        yield kind, i, stop
        start = i = stop
    if start < n:
        yield "literal", start, n


def _strings(text: str, start: int, end: int):
    """標記內部(start..end)的字串常值範圍。沒有結尾的字串算到標記結尾。"""
    i = start
    while i < end:
        quote = text[i]
        if quote not in "'\"":
            i += 1
            continue
        j = i + 1
        while j < end and text[j] != quote:
            j += 2 if text[j] == "\\" else 1
        yield i + 1, min(j, end)
        i = j + 1


def occurrences(text: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """回傳 (固定文字裡的寫入關鍵字, 標記字串裡的寫入關鍵字),各是依位置排序的
    [(在原文的位置, 大寫關鍵字)]。

    固定文字每一段各找一次,再把各段接起來(去掉中間的標記與註解)找一次:`DEL{{ '' }}ETE`
    拆開寫也找得到,位置記在第一段的開頭。兩次都找是因為接起來會吃掉邊界:`x{{ y }}DELETE`
    接成 `xDELETE` 就不像關鍵字了。"""
    segments = list(_segments(text))
    found: dict[int, str] = {}
    in_tags = []
    pieces = [(s, e) for kind, s, e in segments if kind == "literal"]
    for s, e in pieces:
        for m in _KEYWORD_RE.finditer(text, s, e):
            found[m.start()] = m.group(1).upper()
    starts, total = [], 0
    for s, e in pieces:
        starts.append(total)
        total += e - s
    joined = "".join(text[s:e] for s, e in pieces)
    for m in _KEYWORD_RE.finditer(joined):
        k = bisect.bisect_right(starts, m.start()) - 1
        found.setdefault(pieces[k][0] + m.start() - starts[k], m.group(1).upper())
    for kind, s, e in segments:
        if kind == "tag":
            in_tags.extend(_tag_writes(text, s, e))
    return sorted(found.items()), in_tags


def _tag_writes(text: str, start: int, end: int):
    """一個標記裡、字串常值中的寫入關鍵字:會被執行的位置全部算,其他位置只算像一句寫入語句的。

    位置看字串前面最近的「名稱 =」或「名稱 :」(名稱寫成字串、後面接 `:` 的也算,例如
    `{'post-hook': ...}`);找名稱時不看字串內容(字串裡的 `a = 1` 不是設定)。串列裡後面的
    字串沿用同一個名稱(`post_hook=['…', '…']`)。`run_query(` 還沒閉合時也算會被執行。"""
    pos, key, masked = start + 2, None, []
    for a, b in _strings(text, start, end):
        gap = text[pos:a - 1]
        masked.append(gap)
        for km in _KEY_RE.finditer(gap):
            key = km.group(1).lower()
        pos = b + 1
        if _STRING_KEY_RE.match(text, pos, end):           # 字串本身是名稱
            key = text[a:b].lower()
            masked.append("''")
            continue
        executed = key in _EXEC_KEYS or _inside_run_query("".join(masked))
        masked.append("''")
        for m in _KEYWORD_RE.finditer(text, a, b):
            if executed or _STATEMENT.match(text, m.start(), b) or \
                    _WHOLE_EXEC.fullmatch(text[a:b].strip()):
                yield m.start(), m.group(1).upper()


def _inside_run_query(masked: str) -> bool:
    last = None
    for last in _RUN_QUERY_RE.finditer(masked):
        pass
    if last is None:
        return False
    rest = masked[last.end():]
    return rest.count("(") >= rest.count(")")


def _marker(token: str, n: int, kind: str = "W") -> str:
    """kind:W 是固定文字裡的寫入關鍵字,O 是分支裡的輸出。"""
    return f"/*__SEGCRA_{kind}{token}_{n}__*/"


def _marker_re(token: str) -> re.Pattern:
    # 不分大小寫:`{% filter upper %}` 會把記號轉成大寫。group(1) 是種類、group(2) 是編號
    return re.compile(r"/\*__SEGCRA_([WO])" + re.escape(token) + r"_(\d+)__\*/", re.IGNORECASE)


def _expression(text: str, s: int, e: int) -> tuple[int, int] | None:
    """`{{ … }}`(s..e)裡運算式的範圍,去掉 `{{-` / `-}}` 的修空白記號。沒有結尾或內容是空的回 None。"""
    if not text.startswith("{{", s) or e - s < 4 or not text.startswith("}}", e - 2):
        return None
    a = s + 2 + (text[s + 2] in "-+")
    b = e - 2 - (text[e - 3] in "-+")
    return (a, b) if a < b and text[a:b].strip() else None


def output_points(text: str) -> list[tuple[int, int | None]]:
    """分支(`{% if %}` / `{% elif %}` / `{% else %}`、`{% for %}` 的內容)裡的輸出,依位置排序:
    [(起點, 終點)]。`{{ … }}` 是運算式的範圍;非空白的固定文字只有起點(第一個非空白字元),
    終點是 None。分支外面的不算。"""
    depth, points = 0, []
    for kind, s, e in _segments(text):
        if kind == "literal":
            body = text[s:e]
            lead = len(body) - len(body.lstrip())
            if depth and lead < len(body):
                points.append((s + lead, None))
        elif text.startswith("{{", s):         # 註解(`{#`)對不上這裡與下面兩個,不用另外排除
            inner = _expression(text, s, e)
            if depth and inner is not None:
                points.append(inner)
        elif _BRANCH_OPEN.match(text, s):
            depth += 1
        elif _BRANCH_CLOSE.match(text, s):
            depth = max(0, depth - 1)
    return points


def mark(text: str, found: list[tuple[int, str]], token: str,
         outputs: list[tuple[int, int | None]] = ()) -> str:
    """在每個固定文字的關鍵字前面插記號(第 n 個關鍵字插 _marker(token, n)),
    分支裡的第 m 個輸出也插(_marker(token, m, "O"))。固定文字的輸出插在它前面;
    `{{ … }}` 寫成 `{{ '記號' ~ (…) }}`。同一個位置先插輸出的、再插關鍵字的。"""
    edits = []
    for m, (a, b) in enumerate(outputs):
        if b is None:
            edits.append((a, 0, _marker(token, m, "O")))
        else:
            edits.append((a, 0, f"'{_marker(token, m, 'O')}' ~ ("))
            edits.append((b, 2, ")"))
    for n, (pos, _) in enumerate(found):
        edits.append((pos, 1, _marker(token, n)))
    out, prev = [], 0
    for pos, _, piece in sorted(edits, key=lambda x: (x[0], x[1])):
        out.append(text[prev:pos])
        out.append(piece)
        prev = pos
    out.append(text[prev:])
    return "".join(out)


def _tails(text: str, found: list[tuple[int, str]]) -> list[str]:
    """每個關鍵字從它的位置到所在那段固定文字的結尾(下一個 Jinja 標記之前),去掉結尾的空白
    (`{%-` 會修掉前面的空白)。拆開寫的關鍵字(`DEL{{ '' }}ETE`)只有第一段。"""
    ends = sorted(e for kind, _, e in _segments(text) if kind == "literal")
    return [text[pos:ends[bisect.bisect_right(ends, pos)]].rstrip() for pos, _ in found]


def _inside(found: list[tuple[int, str]], n: int, tail: str) -> int:
    """第 n 個關鍵字那段裡,後面還夾著幾個關鍵字(各自前面會插記號)。"""
    pos = found[n][0]
    return sum(1 for p, _ in found[n + 1:] if p < pos + len(tail))


def _lines(text: str, found: list[tuple[int, str]]) -> list[dict]:
    breaks = [i for i, ch in enumerate(text) if ch == "\n"]
    return [{"line": bisect.bisect_left(breaks, pos) + 1, "keyword": kw} for pos, kw in found]


def _too_many(literal: list, outputs: list = ()) -> str | None:
    """固定文字裡的寫入關鍵字或分支裡的輸出太多時不逐一插記號,回原因(記成無法確認)。"""
    if len(literal) > MAX_MARKED_KEYWORDS:
        return f"固定文字裡的寫入關鍵字超過 {MAX_MARKED_KEYWORDS} 個,沒有逐一確認"
    if len(outputs) > MAX_BRANCH_OUTPUTS:
        return f"分支裡的輸出超過 {MAX_BRANCH_OUTPUTS} 處,沒有逐一確認"
    return None


class _Probe:
    """一份樣板原文插了記號的版本,以及比對「記號後面接著那段固定文字」要用的資料。"""

    def __init__(self, source: str, literal: list[tuple[int, str]], token: str,
                 outputs: list[tuple[int, int | None]] = ()):
        self.literal, self.outputs = literal, list(outputs)
        self.pattern = _marker_re(token)
        self.marked = mark(source, literal, token, self.outputs)
        self.outputs_reached: set[int] = set()
        self.tails = tails = _tails(source, literal)
        # 記號後面那段可能夾著後面關鍵字的記號:比對的範圍要放寬這麼多,去掉記號後再比
        self.room = [len(tails[n]) + len(_marker(token, len(literal))) * _inside(literal, n, tails[n])
                     for n in range(len(literal))]

    def reached(self, result, clean_sql: str) -> set[int] | str:
        """插了記號那次的展開結果裡,展開到的關鍵字編號;無法確認時回原因(固定文字)。"""
        if not getattr(result, "ok", False) or not isinstance(getattr(result, "sql", None), str):
            return "加了檢查記號後展開失敗,無法確認寫入語句有沒有展開到"
        sql, pattern = result.sql, self.pattern
        if _UUID_RE.sub("", pattern.sub("", sql)) != _UUID_RE.sub("", clean_sql):
            return "加了檢查記號後展開結果不同,樣板會因為內容改變輸出,無法確認"
        reached: set[int] = set()
        for m in pattern.finditer(sql):
            n = int(m.group(2))
            if m.group(1).upper() == "O":
                # 分支裡的輸出:記號出現就是那段有產生過(沒有走到的分支印不出這次才產生的記號)
                if n < len(self.outputs):
                    self.outputs_reached.add(n)
            elif n < len(self.literal) and pattern.sub("", sql[m.end():m.end() + self.room[n]]).lower() \
                    .startswith(self.tails[n].lower()):
                reached.add(n)
        return reached

    def unreached_outputs(self, source: str) -> list[dict]:
        """哪一次展開都沒有產生過的分支輸出:[{line}],同一行只列一次。"""
        breaks = [i for i, ch in enumerate(source) if ch == "\n"]
        lines = [bisect.bisect_left(breaks, a) + 1
                 for m, (a, _) in enumerate(self.outputs) if m not in self.outputs_reached]
        return [{"line": line} for line in dict.fromkeys(lines)]


def _return_macro_outputs(source: str, blocks: list[tuple[int, int]],
                          outputs: list[tuple[int, int | None]]) -> tuple[list, list]:
    """把分支輸出分成 (用 return() 的 macro 裡的, 其他的)。前者記號看不出有沒有走到。"""
    returning = [(s, e) for s, e in blocks if _RETURN_CALL.search(source, s, e)]
    inside = [p for p in outputs if _within(returning, p[0])]
    return inside, [p for p in outputs if not _within(returning, p[0])]


def _return_macro_reason(source: str, points: list) -> str:
    breaks = [i for i, ch in enumerate(source) if ch == "\n"]
    lines = list(dict.fromkeys(bisect.bisect_left(breaks, a) + 1 for a, _ in points))
    shown = "、".join(str(n) for n in lines[:10]) + (f" 等 {len(lines)} 行" if len(lines) > 10 else "")
    return (f"用 return() 回傳的 macro 的分支裡有輸出(第 {shown} 行),從展開結果看不出分支有沒有走到,"
            "無法確認")


def find_hidden_writes(source: str, clean: dict, render, *, token: str | None = None) -> dict:
    """樣板原文有寫入關鍵字、而規則層看不到時回 {種類: 內容};都看得到時回 {}。

    clean   {是否增量模式(bool): 那次展開後交給規則層的 SQL};一般模式一定要有
    render  render(加了記號的原文, 是否增量模式) → 有 .ok、.sql 的展開結果(同一支展開器、同樣的專案)

    回傳的種類:
      UNREACHED   [{line, keyword}]:固定文字裡、哪一次展開都沒有出現的
      IN_TAGS     [{line, keyword}]:寫在 Jinja 標記字串裡的
      UNREACHED_OUTPUT  [{line}]:分支裡的輸出、哪一次展開都沒有產生過的
      UNVERIFIED  原因(固定文字):加了記號後展開失敗、輸出不同,或關鍵字、分支輸出太多
    """
    if not isinstance(source, str):
        return {}
    literal, in_tags = occurrences(source)
    out: dict = {}
    if in_tags:
        out[IN_TAGS] = _lines(source, in_tags)
    # model 檔裡定義的 macro 不能用 return()(展開器當成展開失敗),這裡不用分開看
    outputs = output_points(source)
    if not literal and not outputs:
        return out
    reason = _too_many(literal, outputs)
    if reason:
        out[UNVERIFIED] = reason
        return out
    probe = _Probe(source, literal, token or secrets.token_hex(8), outputs)
    reached: set[int] = set()
    for incremental, clean_sql in clean.items():
        got = probe.reached(render(probe.marked, incremental), clean_sql)
        if isinstance(got, str):
            out[UNVERIFIED] = got
            return out
        reached |= got
    unreached = [occ for n, occ in enumerate(literal) if n not in reached]
    if unreached:
        out[UNREACHED] = _lines(source, unreached)
    missed = probe.unreached_outputs(source)
    if missed:
        out[UNREACHED_OUTPUT] = missed
    return out


# ------------------------------------------------------------------ macro 檔
# dbt 會執行的區塊:macro(被 model、hook、其他 macro 呼叫)與自訂物化(被設定成這種物化的 model 執行)。
# 區塊外面的內容 dbt 不執行(載入 macro 檔時只取定義)
_BLOCK_START = re.compile(r"\{%-?\s*(macro|materialization)\b")
_BLOCK_END = re.compile(r"\{%-?\s*end(macro|materialization)\b")


def macro_blocks(text: str) -> list[tuple[int, int]]:
    """macro 檔裡 `{% macro %}…{% endmacro %}` 與 `{% materialization %}…{% endmaterialization %}`
    的範圍(含開頭與結尾的標記)。沒有結尾的區塊算到檔尾。"""
    blocks, start = [], None
    for kind, s, e in _segments(text):
        if kind != "tag":
            continue
        if start is None and _BLOCK_START.match(text, s):
            start = s
        elif start is not None and _BLOCK_END.match(text, s):
            blocks.append((start, e))
            start = None
    if start is not None:
        blocks.append((start, len(text)))
    return blocks


def _within(blocks: list[tuple[int, int]], pos: int) -> bool:
    k = bisect.bisect_right([s for s, _ in blocks], pos) - 1
    return k >= 0 and pos < blocks[k][1]


# MR 改到的 macro 檔最多從幾個呼叫它的 model 展開來確認(每個 model 一般模式兩次,
# 有呼叫 is_incremental() 時增量模式再兩次)
MAX_MACRO_MODELS = 3
MODELS_CHECKED = "models_checked"


def find_hidden_writes_in_macro(source: str, models, render, *, token: str | None = None,
                                max_models: int = MAX_MACRO_MODELS) -> dict:
    """MR 改到的 macro 檔:寫入關鍵字從呼叫它的 model 展開時有沒有出現。做法同 find_hidden_writes
    (固定文字插記號、記號後面要接著那段固定文字、插了記號的輸出去掉記號後要跟沒插記號時相同),
    只看 macro 與自訂物化的區塊裡面(區塊外 dbt 不執行)。

    models  呼叫它的 model(依優先順序);依序展開,全部關鍵字都展開到就停,最多 max_models 個
    render  render(macro 檔的內容, model, 是否增量模式) → 有 .ok、.sql、.incremental_checked 的展開結果
            (同一支展開器、同樣的專案,只把這個 macro 檔換成給的內容)

    回傳的種類同 find_hidden_writes,另外 MODELS_CHECKED:展開確認過的 model 數。沒有 model 呼叫、
    或呼叫它的 model 都展開失敗時記成無法確認。"""
    if not isinstance(source, str):
        return {}
    blocks = macro_blocks(source)
    found, tagged = occurrences(source)
    literal = [occ for occ in found if _within(blocks, occ[0])]
    in_tags = [occ for occ in tagged if _within(blocks, occ[0])]
    out: dict = {}
    if in_tags:
        out[IN_TAGS] = _lines(source, in_tags)
    returning, outputs = _return_macro_outputs(
        source, blocks, [p for p in output_points(source) if _within(blocks, p[0])])
    if returning:
        out[UNVERIFIED] = _return_macro_reason(source, returning)
        return out
    if not literal and not outputs:
        return out
    reason = _too_many(literal, outputs)
    if reason:
        out[UNVERIFIED] = reason
        return out
    probe = _Probe(source, literal, token or secrets.token_hex(8), outputs)
    reached: set[int] = set()
    checked = 0
    for model in list(models)[:max_models]:
        clean = render(source, model, False)
        if not getattr(clean, "ok", False):
            continue                        # 這個 model 本身展開失敗(另有 finding),換下一個
        modes = [(False, clean)]
        if getattr(clean, "incremental_checked", False):
            modes.append((True, render(source, model, True)))
        for incremental, base in modes:
            if not getattr(base, "ok", False) or not isinstance(getattr(base, "sql", None), str):
                continue
            got = probe.reached(render(probe.marked, model, incremental), base.sql)
            if isinstance(got, str):
                out[UNVERIFIED] = got
                return out
            reached |= got
        checked += 1
        if len(reached) == len(literal) and len(probe.outputs_reached) == len(outputs):
            break
    if not checked:
        out[UNVERIFIED] = ("沒有呼叫這個 macro 檔的 model 可以展開(沒有 model 呼叫,或呼叫它的 model "
                           "都展開失敗),無法確認寫入語句有沒有展開到")
        return out
    unreached = [occ for n, occ in enumerate(literal) if n not in reached]
    if unreached:
        out[UNREACHED] = _lines(source, unreached)
    missed = probe.unreached_outputs(source)
    if missed:
        out[UNREACHED_OUTPUT] = missed
    if unreached or missed:
        out[MODELS_CHECKED] = checked
    return out
