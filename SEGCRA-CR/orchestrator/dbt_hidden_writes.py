"""dbt 樣板裡沒有經過規則檢查的寫入語句。

規則層看的是展開後的 SQL。樣板可以拿展開器跟 dbt 真正執行時的差異當條件,讓寫入語句在審查時
不出現、實際執行時照跑(例如 `{% if invocation_id != '...' %}`)。差異可以減少,不能保證沒有,
所以這裡不靠「展開器跟 dbt 一樣」,改看樣板原文:

* **固定文字裡的寫入關鍵字**:每一個前面插一個隨機記號,用同一支展開器再展開一次(一般模式;
  樣板有呼叫 `is_incremental()` 時增量模式也一次)。哪一次的輸出都沒有那個記號 → 那一句這次審查
  沒有展開到,不論用什麼條件藏。macro 產生的語句不帶記號,拿來掩護也沒用。
* **插記號的那次展開,去掉記號後必須跟第一次的展開結果相同**:不同代表樣板會因為記號改變輸出
  (例如檢查自己的內容),「記號出現了」就不代表規則層看到了同一句,這時無法確認,一律報。
  比對前遮掉 uuid 格式的字串(`invocation_id` 每次展開都不同),只差在那裡的兩份輸出結構相同。
* **Jinja 標記裡的字串含寫入關鍵字**(例如 `post_hook="DELETE ..."`、`{% set q = 'DELETE ...' %}`):
  展開結果裡看不到,規則層沒有檢查到,一律報。標記裡的名稱不算(`{% if execute %}` 的 execute
  是 dbt 的變數)。
* Jinja 註解不算:dbt 執行時也不會輸出。
* 關鍵字拆開寫(`DEL{{ '' }}ETE`)也找得到:固定文字接起來再找一次。

找不到的:在 Jinja 裡用運算組出來的關鍵字(`'ETELED' | reverse`)——靜態看不出運算結果;
以及藏在 macro 檔裡的(這裡只看 MR 改到的 model)。審查模型在 diff 裡看得到這些原文。

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
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TAG_PAIRS = {"{{": "}}", "{%": "%}", "{#": "#}"}

# 固定文字裡的寫入關鍵字超過這個數量就不逐一插記號,直接記成無法確認(交人工)
MAX_MARKED_KEYWORDS = 500

UNREACHED = "unreached"
IN_TAGS = "in_tags"
UNVERIFIED = "unverified"


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
            for a, b in _strings(text, s, e):
                in_tags.extend((m.start(), m.group(1).upper())
                               for m in _KEYWORD_RE.finditer(text, a, b))
    return sorted(found.items()), in_tags


def _marker(token: str, n: int) -> str:
    return f"/*__SEGCRA_W{token}_{n}__*/"


def _marker_re(token: str) -> re.Pattern:
    # 不分大小寫:`{% filter upper %}` 會把記號轉成大寫
    return re.compile(r"/\*__SEGCRA_W" + re.escape(token) + r"_(\d+)__\*/", re.IGNORECASE)


def mark(text: str, found: list[tuple[int, str]], token: str) -> str:
    """在每個固定文字的關鍵字前面插記號(第 n 個關鍵字插 _marker(token, n))。"""
    out, prev = [], 0
    for n, (pos, _) in enumerate(found):
        out.append(text[prev:pos])
        out.append(_marker(token, n))
        prev = pos
    out.append(text[prev:])
    return "".join(out)


def _lines(text: str, found: list[tuple[int, str]]) -> list[dict]:
    breaks = [i for i, ch in enumerate(text) if ch == "\n"]
    return [{"line": bisect.bisect_left(breaks, pos) + 1, "keyword": kw} for pos, kw in found]


def find_hidden_writes(source: str, clean: dict, render, *, token: str | None = None) -> dict:
    """樣板原文有寫入關鍵字、而規則層看不到時回 {種類: 內容};都看得到時回 {}。

    clean   {是否增量模式(bool): 那次展開後交給規則層的 SQL};一般模式一定要有
    render  render(加了記號的原文, 是否增量模式) → 有 .ok、.sql 的展開結果(同一支展開器、同樣的專案)

    回傳的種類:
      UNREACHED   [{line, keyword}]:固定文字裡、哪一次展開都沒有出現的
      IN_TAGS     [{line, keyword}]:寫在 Jinja 標記字串裡的
      UNVERIFIED  原因(固定文字):加了記號後展開失敗、輸出不同,或關鍵字太多
    """
    if not isinstance(source, str):
        return {}
    literal, in_tags = occurrences(source)
    out: dict = {}
    if in_tags:
        out[IN_TAGS] = _lines(source, in_tags)
    if not literal:
        return out
    if len(literal) > MAX_MARKED_KEYWORDS:
        out[UNVERIFIED] = f"固定文字裡的寫入關鍵字超過 {MAX_MARKED_KEYWORDS} 個,沒有逐一確認"
        return out
    token = token or secrets.token_hex(8)
    pattern = _marker_re(token)
    marked = mark(source, literal, token)
    reached: set[int] = set()
    for incremental, clean_sql in clean.items():
        result = render(marked, incremental)
        if not getattr(result, "ok", False) or not isinstance(getattr(result, "sql", None), str):
            out[UNVERIFIED] = "加了檢查記號後展開失敗,無法確認寫入語句有沒有展開到"
            return out
        if _UUID_RE.sub("", pattern.sub("", result.sql)) != _UUID_RE.sub("", clean_sql):
            out[UNVERIFIED] = "加了檢查記號後展開結果不同,樣板會因為內容改變輸出,無法確認"
            return out
        reached.update(int(m.group(1)) for m in pattern.finditer(result.sql))
    unreached = [occ for n, occ in enumerate(literal) if n not in reached]
    if unreached:
        out[UNREACHED] = _lines(source, unreached)
    return out
