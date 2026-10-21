"""change_view — 把變更檔「變更後的完整內容」整理成 prompt 的一段。

只給 diff 時,模型逐項核對規格對照的其實只有 diff 的那幾行:條件寫在離改動處超過幾行的
地方,就會被報成「遺漏」。

這裡只負責排版,不取檔、不連網:
  * 每一行前面加行號,這次新增或修改的行以 `>` 標記(由 diff 的 hunk 算出來)
  * 刪掉的行在完整內容裡看不到:在刪除的位置插一行以 `-` 開頭的說明,寫明刪了幾行(不放
    被刪的內容,那在 diff 裡)。改寫時刪掉的比新增的多也一樣(例如三個條件改成一個)
  * 行號對不上內容時不標記——mock 的 fixture、佔位的 diff 都可能與內容不一致,標錯行比不標更糟
  * 超過預算時只放改動所在的語句(往前後找到以分號結尾的行),略過的部分寫明行號範圍;
    刪除的位置也算改動所在。找不到分號時(dbt model 通常整個檔只有一句、不寫分號)改成
    前後各留 FALLBACK_CONTEXT 行,不一定是完整的語句或 CTE
  * 沒有任何檔案有完整內容時回傳空字串:prompt 不多這一段

內容來自待審的 MR,是不可信的文字。每一行都以行號開頭,所以內容裡的 ``` 不會出現在行首、
關不掉外層的程式碼區塊。
"""
import re

from .config import estimate_tokens

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
# 超過預算時,每個改動的行往前後最多找這麼多行來湊出完整的語句
MAX_STATEMENT_SPAN = 60
# 沒有分號可以當邊界時(dbt model 通常整個檔只有一句),改動處前後各留這麼多行
FALLBACK_CONTEXT = 15

HEADER = """

## 變更後的完整內容
以下是變更檔在這次 MR 之後的完整內容。行號在左,這次新增或修改的行以 `>` 標記;\
以 `-` 開頭、沒有行號的那一行表示這個位置有行被刪掉(刪掉的內容見上面的 diff)。
- 判斷程式有沒有某個條件、欄位或過濾時,以這裡的完整內容為準,不要只看上面的 diff:\
diff 只顯示改動處前後幾行,條件可能寫在沒有顯示的地方。
- 回報「缺少 / 遺漏某條件」之前,先在這裡確認它真的不存在;finding 的行號請用這裡的行號。
- 沒有標 `>` 的行不是這次改的:只用來確認條件與上下文,不要另外回報那些行的寫法。
  (標題註明「未標記改動的行」的檔案例外:那些檔案哪幾行是這次改的,請對照上面的 diff。)"""


def _matches(lines: list[str], no: int, raw: str) -> bool:
    return 1 <= no <= len(lines) and lines[no - 1].rstrip() == raw[1:].rstrip()


def changes(diff, text: str) -> tuple[set[int], dict[int, str]] | None:
    """(diff 新增的行在新檔裡的行號, {新檔的行號: 插在那一行前面的刪除說明})。
    說明的位置是 len(lines) + 1 時,刪除在檔尾。

    刪除只在「刪掉的比新增的多」時寫說明:只刪不增的看不到任何 `>`;改寫時三個條件改成一個,
    `>` 只標出那一個。刪掉的不比新增的多時,`>` 已經標出改動的位置。

    看不懂、或新增 / 上下文的行對不上 text 的內容時回 None(不標記):刪除的位置只能靠
    上下文定位,上下文對不上就說不準刪在哪裡。"""
    if not isinstance(diff, str) or not diff:
        return None
    lines = text.splitlines()
    marked: set[int] = set()
    notes: dict[int, str] = {}
    current = None
    block = None                        # 進行中的一段連續 +/- 行:[開始的行號, 刪掉幾行, 新增幾行]

    def close():
        if block and block[1] > block[2]:
            start, deleted, added = block
            note = (f"這裡刪掉了 {deleted} 行" if not added
                    else f"下面 {added} 行取代了原本的 {deleted} 行")
            notes[start] = f"{notes[start]};{note}" if start in notes else note

    for raw in diff.splitlines():
        m = _HUNK.match(raw)
        if m:
            close()
            block = None
            current = int(m.group(1))
            if m.group(2) == "0":
                current += 1            # `+N,0`:這一段在新檔沒有任何行,N 是刪除處的前一行
            continue
        # 第一段 @@ 之前的 ---/+++ 是檔頭;段落裡以 --- / +++ 開頭的是內容(刪掉的 SQL 註解
        # `-- x`、新增的 `++x`),不能跳過,否則後面的行號全部錯開
        if current is None or raw.startswith("\\"):
            continue
        if raw.startswith(("+", "-")):
            if block is None:
                if not 1 <= current <= len(lines) + 1:
                    return None
                block = [current, 0, 0]
            if raw.startswith("-"):
                block[1] += 1           # 被刪掉的行不佔新檔的行號
                continue
            if not _matches(lines, current, raw):
                return None             # diff 與內容對不上:寧可不標
            marked.add(current)
            block[2] += 1
            current += 1
            continue
        close()
        block = None
        if not _matches(lines, current, raw):
            return None
        current += 1                    # 上下文
    close()
    return (marked, notes) if current is not None else None


def _numbered(lines: list[str], keep: list[tuple[int, int]], marked: set[int],
              notes: dict[int, str] | None = None) -> str:
    """keep 是要顯示的行號範圍(含頭尾、已排序且不重疊);範圍之間寫明略過了哪幾行。
    notes 是刪除的說明,插在它的行號那一行前面(檔尾的放在最後一行後面)。"""
    notes = notes or {}
    width = len(str(len(lines)))

    def note_row(no):
        return f"- {'':>{width}} | ({notes[no]},原本的內容見上面的 diff)"

    out, last = [], 0
    for start, end in keep:
        if start > last + 1:
            out.append(f"  {'':>{width}} | …(第 {last + 1}–{start - 1} 行略過)")
        for no in range(start, end + 1):
            if no in notes:
                out.append(note_row(no))
            out.append(f"{'>' if no in marked else ' '} {no:>{width}} | {lines[no - 1]}")
        last = end
    if last == len(lines) and last + 1 in notes:
        out.append(note_row(last + 1))
    if last < len(lines):
        out.append(f"  {'':>{width}} | …(第 {last + 1}–{len(lines)} 行略過)")
    return "\n".join(out)


def _statement_ranges(lines: list[str], marked: set[int]) -> list[tuple[int, int]]:
    """每個改動的行所在的語句:往前找到上一個以分號結尾的行之後,往後找到下一個以分號結尾的行。
    找不到分號(在 MAX_STATEMENT_SPAN 行之內)就改用前後各 FALLBACK_CONTEXT 行——dbt model
    通常不寫分號,所以實際上多半是這一種,不一定涵蓋完整的語句或 CTE。"""
    ranges = []
    for no in sorted(marked):
        start = end = None
        for back in range(no - 1, max(0, no - 1 - MAX_STATEMENT_SPAN), -1):
            if lines[back - 1].rstrip().endswith(";"):
                start = back + 1
                break
        else:
            if no - 1 - MAX_STATEMENT_SPAN <= 0:
                start = 1               # 一路找到檔頭都沒有分號:語句從第一行開始
        for fwd in range(no, min(len(lines), no + MAX_STATEMENT_SPAN) + 1):
            if lines[fwd - 1].rstrip().endswith(";"):
                end = fwd
                break
        else:
            if no + MAX_STATEMENT_SPAN >= len(lines):
                end = len(lines)        # 一路找到檔尾都沒有分號:語句到最後一行
        if start is None or end is None:
            start = max(1, no - FALLBACK_CONTEXT)
            end = min(len(lines), no + FALLBACK_CONTEXT)
        ranges.append((start, end))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def file_view(path: str, text: str, diff, budget_tokens: int) -> tuple[str, int]:
    """回傳(這個檔案的那一段, 用掉的 token 估計)。放不進預算時逐步縮小:
    完整內容 → 只放改動所在的語句 → 只寫一行說明。
    每一行都是這次新增的(新檔)時回傳空字串:完整內容與 diff 相同,不重複放。"""
    lines = text.splitlines()
    found = changes(diff, text)
    marked, notes = found if found is not None else (None, {})
    if marked is not None and lines and len(marked) == len(lines) and not notes:
        return "", 0
    note = "" if marked is not None else "(無法由 diff 對應到行號,未標記改動的行)"
    title = f"### {path}{note}"
    if not lines:
        block = f"{title}\n(空檔案)"
        return block, estimate_tokens(block)
    # 內容本身就超過預算時,加了行號只會更大:不必先排好整份(檔案可能很大)
    if estimate_tokens(text) <= budget_tokens:
        whole = f"{title}\n```\n{_numbered(lines, [(1, len(lines))], marked or set(), notes)}\n```"
        if estimate_tokens(whole) <= budget_tokens:
            return whole, estimate_tokens(whole)
    # 改動所在:新增的行,以及刪除處前後的那兩行(刪在檔頭 / 檔尾時只有一行)
    anchors = (marked or set()) | {no for pos in notes for no in (pos - 1, pos)
                                   if 1 <= no <= len(lines)}
    if anchors:
        part = (f"{title}(內容超出預算,只放改動所在的語句)\n```\n"
                f"{_numbered(lines, _statement_ranges(lines, anchors), marked or set(), notes)}\n```")
        if estimate_tokens(part) <= budget_tokens:
            return part, estimate_tokens(part)
    block = f"### {path}\n(完整內容超出 context 預算,已略過;請以上面的 diff 為準,並說明沒有看到完整內容)"
    return block, estimate_tokens(block)


def build_full_text_section(files: list[dict], texts: dict, budget_tokens: int) -> str:
    """files 是 MR 的變更檔(同 build_diff_section);texts 是 {路徑: 變更後的完整內容}。
    沒有任何檔案有完整內容、或預算不是正數時回傳空字串。"""
    if not isinstance(budget_tokens, int) or isinstance(budget_tokens, bool) or budget_tokens <= 0:
        return ""
    parts, left = [], budget_tokens
    for f in files:
        path = f.get("path") if isinstance(f, dict) else None
        text = texts.get(path) if isinstance(path, str) else None
        if not isinstance(text, str):
            continue
        block, used = file_view(path, text, f.get("diff"), left)
        if block:
            parts.append(block)
        left = max(0, left - used)
    if not parts:
        return ""
    return HEADER + "\n\n" + "\n\n".join(parts)
