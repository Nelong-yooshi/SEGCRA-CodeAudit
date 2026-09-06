#!/usr/bin/env python3
"""學習迴路:從審查歷史提煉「慣例/抑制/規則」草案 — 系統在使用過程中長知識。

輸入:memory/review_history/history.jsonl(record_feedback 累積的人工處置)
輸出:memory/proposals/PROPOSALS.md(草案;人工核准後才存成 memory/knowledge/ 知識項
      ——高合規要求場景要留人閘;source=auto-consolidated,權威低於人工撰寫、高於程式觀察)

規則:
- 同型 finding 被 rejected ≥ 2 次 → 草擬「抑制型 binding」知識項(誤報收斂)
- 同型 finding 被 accepted ≥ 3 次 → 起草「候選確定性規則」建議(升級進 rule-base/hint)

用法:.venv/bin/python scripts/consolidate_feedback.py
"""
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
HISTORY = PKG_ROOT / "memory" / "review_history" / "history.jsonl"
# 注意:草案絕不能放 conventions/(那裡整個目錄會被 get_conventions 讀進審查 context,
# 未核准的 [suppress:*] 會直接生效)——提案放獨立目錄,核准後人工移入
PROPOSALS = PKG_ROOT / "memory" / "proposals" / "PROPOSALS.md"

REJECT_THRESHOLD = 2
ACCEPT_THRESHOLD = 3


def _bigrams(s: str):
    s = re.sub(r"\s+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _cluster(records: list[dict], threshold: float = 0.3) -> list[list[dict]]:
    """分群:優先按 tag(規則/檢核點代碼)精準分群;無 tag 的退回標題相似度。"""
    by_tag: dict[str, list[dict]] = defaultdict(list)
    untagged = []
    for rec in records:
        (by_tag[rec["tag"]].append(rec) if rec.get("tag") else untagged.append(rec))
    clusters = list(by_tag.values())
    for rec in untagged:
        grams = _bigrams(rec.get("title", "") + rec.get("note", ""))
        for c in clusters:
            ref = _bigrams(c[0].get("title", "") + c[0].get("note", ""))
            if grams and len(grams & ref) / max(len(grams | ref), 1) >= threshold:
                c.append(rec)
                break
        else:
            clusters.append([rec])
    return clusters


def main():
    if not HISTORY.exists():
        sys.exit("尚無審查歷史(memory/review_history/history.jsonl)")
    records = [json.loads(l) for l in HISTORY.read_text(encoding="utf-8").splitlines()
               if l.strip()]
    proposals = []
    for cluster in _cluster(records):
        titles = {r["title"] for r in cluster}
        rejected = [r for r in cluster if r["disposition"] == "rejected"]
        accepted = [r for r in cluster if r["disposition"] == "accepted"]
        notes = "; ".join(sorted({r.get("note", "") for r in cluster if r.get("note")}))
        tag = cluster[0].get("tag") or "H0XX"
        if len(rejected) >= REJECT_THRESHOLD:
            # 草擬成「auto-consolidated」知識項(權威 20,介於人工撰寫與程式觀察之間)。
            # 人工核准後把此區塊存成 memory/knowledge/<id>.md 即生效。
            proposals.append(
                f"### 抑制型 binding 提案(rejected ×{len(rejected)})\n"
                f"同型 finding:{' / '.join(sorted(titles))}\n"
                f"核准後存成 `memory/knowledge/auto-suppress-{tag.lower()}.md`:\n"
                f"```markdown\n---\n"
                f"id: auto-suppress-{tag.lower()}\ntier: binding\n"
                f"source: auto-consolidated\nscope: [anomaly-rules]\n"
                f"suppress: [{tag}]\nconflict_key: {tag.lower()}-applicability\n"
                f"tags: [誤報收斂]\n---\n"
                f"{notes or '<補上環境事實>'};此環境下審查不應以"
                f"「{sorted(titles)[0]}」作為 finding。\n```\n")
        if len(accepted) >= ACCEPT_THRESHOLD:
            proposals.append(
                f"### 規則升級提案(accepted ×{len(accepted)})\n"
                f"- 同型 finding:{' / '.join(sorted(titles))}\n"
                f"- 建議:此問題反覆出現且皆被採納,評估寫成確定性規則(R/H 系列)"
                f"進 sqltools rule-base,零 LLM 成本攔截\n")
    if not proposals:
        print(f"歷史 {len(records)} 筆,尚無達門檻的提案"
              f"(rejected≥{REJECT_THRESHOLD} 或 accepted≥{ACCEPT_THRESHOLD})")
        return
    header = (f"# 慣例/規則提案(自動起草,{time.strftime('%Y-%m-%d')})\n\n"
              "> 由 consolidate_feedback.py 從審查歷史提煉。**人工核准後**才存成\n"
              "> memory/knowledge/ 知識項 或 sqltools rule-base;本檔案不會被審查管線讀取。\n\n")
    PROPOSALS.write_text(header + "\n".join(proposals), encoding="utf-8")
    print(f"產出 {len(proposals)} 條提案 → {PROPOSALS}")


if __name__ == "__main__":
    main()
