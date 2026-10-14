"""change_view — 把變更檔「變更後的完整內容」整理成 prompt 的一段(#38 第 1 點)。

只給 diff 時,模型逐項核對規格對照的其實只有 diff 的那幾行:條件寫在離改動處超過幾行的
地方,就會被報成「遺漏」。實測只改兩行註解的 MR 被報三條 major「缺少某某過濾」,而那三個
條件就在同一個 WHERE 裡。

這裡只負責排版,不取檔、不連網:
  * 每一行前面加行號,這次新增或修改的行以 `>` 標記(由 diff 的 hunk 算出來)
  * 行號對不上內容時不標記——mock 的 fixture、佔位的 diff 都可能與內容不一致,標錯行比不標更糟
  * 超過預算時只放改動所在的語句(往前後找到以分號結尾的行),略過的部分寫明行號範圍
  * 沒有任何檔案有完整內容時回傳空字串:prompt 與之前逐字相同

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
以下是變更檔在這次 MR 之後的完整內容。行號在左,這次新增或修改的行以 `>` 標記。
- 判斷程式有沒有某個條件、欄位或過濾時,以這裡的完整內容為準,不要只看上面的 diff:\
diff 只顯示改動處前後幾行,條件可能寫在沒有顯示的地方。
- 回報「缺少 / 遺漏某條件」之前,先在這裡確認它真的不存在;finding 的行號請用這裡的行號。
- 沒有標 `>` 的行不是這次改的:既有的寫法有疑慮可以提,但要說明那不是這次的變更。
  (標題註明「未標記改動的行」的檔案例外:那些檔案哪幾行是這次改的,請對照上面的 diff。)"""


def changed_lines(diff, text: str) -> set[int] | None:
    """diff 新增的行在新檔裡的行號。看不懂、或行號對不上 text 的內容時回 None(不標記)。"""
    if not isinstance(diff, str) or not diff:
        return None
    lines = text.splitlines()
    marked: set[int] = set()
    current = None
    for raw in diff.splitlines():
        m = _HUNK.match(raw)
        if m:
            current = int(m.group(1))
            continue
        if current is None or raw.startswith(("+++", "---", "\\")):
            continue
        if raw.startswith("+"):
            if not (1 <= current <= len(lines)) or lines[current - 1].rstrip() != raw[1:].rstrip():
                return None             # diff 與內容對不上:寧可不標
            marked.add(current)
            current += 1
        elif raw.startswith("-"):
            continue                    # 被刪掉的行不佔新檔的行號
        else:
            current += 1                # 上下文
    return marked if current is not None else None


def _numbered(lines: list[str], keep: list[tuple[int, int]], marked: set[int]) -> str:
    """keep 是要顯示的行號範圍(含頭尾、已排序且不重疊);範圍之間寫明略過了哪幾行。"""
    width = len(str(len(lines)))
    out, last = [], 0
    for start, end in keep:
        if start > last + 1:
            out.append(f"  {'':>{width}} | …(第 {last + 1}–{start - 1} 行略過)")
        for no in range(start, end + 1):
            out.append(f"{'>' if no in marked else ' '} {no:>{width}} | {lines[no - 1]}")
        last = end
    if last < len(lines):
        out.append(f"  {'':>{width}} | …(第 {last + 1}–{len(lines)} 行略過)")
    return "\n".join(out)


def _statement_ranges(lines: list[str], marked: set[int]) -> list[tuple[int, int]]:
    """每個改動的行所在的語句:往前找到上一個以分號結尾的行之後,往後找到下一個以分號結尾的行。
    找不到分號(在 MAX_STATEMENT_SPAN 行之內)就改用前後各 FALLBACK_CONTEXT 行。"""
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
    marked = changed_lines(diff, text)
    if marked is not None and lines and len(marked) == len(lines):
        return "", 0
    note = "" if marked is not None else "(無法由 diff 對應到行號,未標記改動的行)"
    title = f"### {path}{note}"
    if not lines:
        block = f"{title}\n(空檔案)"
        return block, estimate_tokens(block)
    # 內容本身就超過預算時,加了行號只會更大:不必先排好整份(檔案可能很大)
    if estimate_tokens(text) <= budget_tokens:
        whole = f"{title}\n```\n{_numbered(lines, [(1, len(lines))], marked or set())}\n```"
        if estimate_tokens(whole) <= budget_tokens:
            return whole, estimate_tokens(whole)
    if marked:
        part = (f"{title}(內容超出預算,只放改動所在的語句)\n```\n"
                f"{_numbered(lines, _statement_ranges(lines, marked), marked)}\n```")
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
        text = texts.get(f.get("path"))
        if not isinstance(text, str):
            continue
        block, used = file_view(f["path"], text, f.get("diff"), left)
        if block:
            parts.append(block)
        left = max(0, left - used)
    if not parts:
        return ""
    return HEADER + "\n\n" + "\n\n".join(parts)
