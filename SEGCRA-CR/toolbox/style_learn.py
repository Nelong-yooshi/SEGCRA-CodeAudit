"""風格學習 — 從「已合併成功的範例」觀察團隊習慣,學成一條 observed 慣例,
並附一個確定性檢查,讓學到的風格有牙齒(不必賭模型會不會照做)。

示範標的:團隊的 SQL 以「前置逗號」(leading comma)撰寫欄位清單。沒人明講這條規則,
系統從多份已合併範例「觀察」出來 → 產生 observed(code-mined)慣例(權威最低,
可被使用者明確指示覆蓋)+ 一個確定性風格檢查 S-COMMA。
"""
from __future__ import annotations

import re
import time

from .knowledge_store import Item

# 抓每個 SELECT ... FROM 之間的欄位清單區塊(容忍換行、大小寫)
_SELECT_BLOCK = re.compile(r"\bselect\b(.*?)\bfrom\b", re.I | re.S)


def comma_style(sql: str) -> tuple[str, int, int]:
    """判定 SQL 欄位清單的逗號風格。回傳 (style, leading, trailing)。
    leading = 行首逗號的行數;trailing = 行尾逗號的行數。"""
    lead = trail = 0
    for m in _SELECT_BLOCK.finditer(sql):
        block = m.group(1)
        lines = [ln.strip() for ln in block.splitlines()]
        lines = [ln for ln in lines if ln and not ln.startswith("--")]
        for ln in lines:
            code = re.sub(r"--.*$", "", ln).strip()  # 去行內註解再看尾字
            if code.startswith(","):
                lead += 1
            if code.endswith(","):
                trail += 1
    if lead == 0 and trail == 0:
        return ("unknown", 0, 0)
    if lead > trail:
        return ("leading", lead, trail)
    if trail > lead:
        return ("trailing", lead, trail)
    return ("mixed", lead, trail)


def mine_leading_comma(examples: list[str], min_examples: int = 3,
                       min_ratio: float = 0.6) -> Item | None:
    """從已合併範例學「前置逗號」習慣。多數範例是 leading 才學(避免雜訊誤學)。"""
    styles = [comma_style(s)[0] for s in examples]
    considered = [s for s in styles if s in ("leading", "trailing")]
    lead = sum(1 for s in considered if s == "leading")
    if len(considered) < min_examples or lead / max(len(considered), 1) < min_ratio:
        return None
    return Item(
        id="style-leading-comma",
        text=("團隊 SQL 欄位清單採「前置逗號」(leading comma)風格:逗號置於"
              "下一行行首而非上一行行尾,便於增刪欄位與版本差異比對。"
              f"(自 {lead}/{len(considered)} 份已合併範例觀察學得)"),
        tier="observed", source="code-mined", scope=["sql-style"],
        code="S-COMMA", conflict_key="sql-comma-style",
        tags=["風格", "逗號", "leading-comma"], evidence=lead,
        ts=int(time.time()),
    )


# ------------------------------------------------------------ 確定性風格檢查
def _check_leading_comma(sql: str) -> dict | None:
    style, lead, trail = comma_style(sql)
    if style == "trailing" and trail >= 2:
        return {"rule": "S-COMMA", "severity": "info",
                "message": ("欄位清單使用行尾逗號,與團隊學得的前置逗號(leading "
                            f"comma)風格不符(行尾逗號 {trail} 處);建議改為逗號置"
                            "於下一行行首以符團隊慣例。")}
    return None


_CHECKS = {"S-COMMA": _check_leading_comma}


def check_style(sql: str, active_codes: set[str] | None = None) -> list[dict]:
    """對 SQL 執行「已學會且啟用」的風格檢查。active_codes 來自知識庫中帶 code
    的 observed 慣例(學到才檢查;被更高權威覆蓋的就不在其中)。"""
    hits = []
    for code, fn in _CHECKS.items():
        if active_codes is not None and code not in active_codes:
            continue
        hit = fn(sql)
        if hit:
            hits.append(hit)
    return hits
