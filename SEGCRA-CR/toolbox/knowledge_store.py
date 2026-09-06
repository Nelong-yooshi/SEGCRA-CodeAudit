"""分層、可檢索的知識庫 — 取代「把慣例全部倒進 context」的作法。

為什麼要這樣:正式環境會有很多系統、很多人、大量知識,不可能每次審查都把所有
慣例整包塞進 prompt(context 會爆、也不真實)。正確作法是把知識分層:

  binding   恆常生效、程式強制。小量、永遠注入,**不受對話長度/時間影響**
            —— 使用者講定的規範不會因為「聊久了」被擠出視窗而遺忘、回到舊行為。
  guideline 大量參考知識。依 scope + 相關度「檢索」少量注入,不整包倒入。
  observed  從程式碼/歷史「觀察學到」的習慣(如團隊愛用前置逗號),權威最低,
            可被更高權威的明確指示覆蓋。

來源權威(同一主題衝突時,權威高者勝 —— 對應 Task 1 的分級):
  user-explicit     40  使用者回覆「直接明確表示」的規定(最高)
  user-authored     30  使用者「親手寫」的 skill / 慣例
  auto-consolidated 20  學習迴路自動草擬、且經人工核准
  code-mined        10  從多份正式程式碼「觀察」到的習慣(最低)

一個知識項 = 一個帶 YAML frontmatter 的 markdown 檔(人可讀可改、可 diff、可留人閘),
機器學到的項目也寫成同格式檔案(source=code-mined/auto-consolidated),完全同構。
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PKG_ROOT = Path(__file__).resolve().parent.parent
KB_DIR = Path(os.environ.get("MEMORY_KB", PKG_ROOT / "memory" / "knowledge"))

AUTHORITY = {
    "user-explicit": 40,
    "user-authored": 30,
    "auto-consolidated": 20,
    "code-mined": 10,
}
TIERS = ("binding", "guideline", "observed")


@dataclass
class Item:
    id: str
    text: str
    tier: str = "guideline"
    source: str = "user-authored"
    scope: list[str] = field(default_factory=lambda: ["*"])
    code: str | None = None          # 對應的確定性強制碼(binding/observed 用)
    suppress: list[str] = field(default_factory=list)  # 要抑制的預掃檢核點代碼
    conflict_key: str | None = None  # 同 key 者互斥,權威高者勝
    tags: list[str] = field(default_factory=list)
    evidence: int = 1                # 支持此項的範例數(observed 用)
    ts: int = 0

    @property
    def authority(self) -> int:
        return AUTHORITY.get(self.source, 0)


# ------------------------------------------------------------------ 載入
def _parse_md(path: Path) -> Item | None:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return None
    meta = yaml.safe_load(m.group(1)) or {}
    scope = meta.get("scope", ["*"])
    if isinstance(scope, str):
        scope = [scope]
    supp = meta.get("suppress", [])
    if isinstance(supp, str):
        supp = [supp]
    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    return Item(
        id=meta.get("id", path.stem), text=m.group(2).strip(),
        tier=meta.get("tier", "guideline"), source=meta.get("source", "user-authored"),
        scope=scope, code=meta.get("code"), suppress=supp,
        conflict_key=meta.get("conflict_key"), tags=tags,
        evidence=int(meta.get("evidence", 1)),
        ts=int(meta.get("ts", int(path.stat().st_mtime))),
    )


def load_items(kb_dir: Path | None = None) -> list[Item]:
    kb_dir = kb_dir or KB_DIR
    items = []
    for f in sorted(Path(kb_dir).glob("**/*.md")):
        it = _parse_md(f)
        if it:
            items.append(it)
    return items


def dumps(it: Item) -> str:
    """把知識項序列化成帶 frontmatter 的 markdown 字串(存檔與 GitLab commit 共用)。"""
    meta = {"id": it.id, "tier": it.tier, "source": it.source, "scope": it.scope}
    if it.code:
        meta["code"] = it.code
    if it.suppress:
        meta["suppress"] = it.suppress
    if it.conflict_key:
        meta["conflict_key"] = it.conflict_key
    if it.tags:
        meta["tags"] = it.tags
    meta["evidence"] = it.evidence
    meta["ts"] = it.ts or int(time.time())
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{fm}\n---\n{it.text}\n"


def save_item(it: Item, kb_dir: Path | None = None) -> Path:
    """把一個知識項寫回磁碟(機器學到的項目與人寫的同格式)。"""
    kb_dir = Path(kb_dir or KB_DIR)
    kb_dir.mkdir(parents=True, exist_ok=True)
    path = kb_dir / f"{it.id}.md"
    path.write_text(dumps(it), encoding="utf-8")
    return path


# ------------------------------------------------------------------ 檢索
def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _in_scope(it: Item, scope: list[str]) -> bool:
    if "*" in it.scope or not scope:
        return True
    return bool(set(it.scope) & set(scope))


def _score(query: str, it: Item) -> float:
    q = _bigrams(query)
    if not q:
        return 0.0
    hay = _bigrams(it.text + " " + " ".join(it.tags))
    return len(q & hay) / len(q)


def resolve_conflicts(items: list[Item]) -> tuple[list[Item], list[dict]]:
    """同一 conflict_key 只保留權威最高者(平手取較新);其餘標記為被壓制。
    這是 Task 1 分級的落地:user-explicit 覆蓋 code-mined 觀察到的習慣。"""
    by_key: dict[str, list[Item]] = {}
    singles = []
    for it in items:
        if it.conflict_key:
            by_key.setdefault(it.conflict_key, []).append(it)
        else:
            singles.append(it)
    kept, suppressed = list(singles), []
    for key, group in by_key.items():
        group.sort(key=lambda x: (x.authority, x.ts), reverse=True)
        kept.append(group[0])
        for loser in group[1:]:
            suppressed.append({"id": loser.id, "beaten_by": group[0].id,
                               "key": key, "loser_authority": loser.authority,
                               "winner_authority": group[0].authority})
    return kept, suppressed


def retrieve(query: str, scope: list[str] | None = None, top_k: int = 4,
             kb_dir: Path | None = None, items: list[Item] | None = None) -> dict:
    """分層檢索:
      - binding:scope 內全數注入(恆常、不看相關度分數 → 不會被擠掉/遺忘)
      - retrieved:guideline+observed 中,scope 內、相關度最高的 top_k
      衝突解析在合併後做:高權威項覆蓋低權威項。
    items 給定時直接用(供壓測大量合成項);否則從 kb_dir 載入。
    回傳 dict:{binding, retrieved, suppressed, stats}。"""
    scope = scope or []
    source = items if items is not None else load_items(kb_dir)
    items = [it for it in source if _in_scope(it, scope)]
    items, suppressed = resolve_conflicts(items)

    binding = [it for it in items if it.tier == "binding"]
    pool = [it for it in items if it.tier != "binding"]
    scored = sorted(((_score(query, it), it) for it in pool),
                    key=lambda x: -x[0])
    retrieved = [it for s, it in scored if s > 0][:top_k]
    return {
        "binding": binding,
        "retrieved": retrieved,
        "suppressed": suppressed,
        "stats": {"total_in_scope": len(items), "binding": len(binding),
                  "pool": len(pool), "returned": len(retrieved)},
    }


def _fmt(it: Item) -> str:
    tag = {"binding": "【恆常規範·必守】", "guideline": "【參考慣例】",
           "observed": "【觀察習慣】"}.get(it.tier, "")
    src = {"user-explicit": "使用者明確指示", "user-authored": "人工撰寫",
           "auto-consolidated": "學習迴路(已核准)", "code-mined": "程式碼觀察"}.get(
        it.source, it.source)
    return f"- {tag}[{it.id}·{src}] {it.text}"


def render_for_prompt(result: dict) -> str:
    """把檢索結果組成注入 prompt 的文字。binding 明確標記為必守、且說明其恆常性,
    讓模型知道這些不是可討論的參考,而是不得違反的規範。"""
    lines = []
    if result["binding"]:
        lines.append("# 團隊恆常規範(必守 — 不論對話多長/多久前講定,一律遵守)")
        lines += [_fmt(it) for it in result["binding"]]
    if result["retrieved"]:
        lines.append("\n# 本次相關的參考慣例(依變更內容檢索)")
        lines += [_fmt(it) for it in result["retrieved"]]
    return "\n".join(lines) if lines else "(無相關慣例)"


def active_style_codes(scope: list[str] | None = None,
                       kb_dir: Path | None = None) -> set[str]:
    """scope 內、通過衝突解析後仍存活、且帶確定性 code 的慣例(observed/binding)代碼。
    「學到才檢查」:若某風格慣例被更高權威的明確指示覆蓋,其 code 就不在此集合中。"""
    scope = scope or []
    survivors, _ = resolve_conflicts(load_items(kb_dir))
    return {it.code for it in survivors
            if it.code and _in_scope(it, scope)}


def binding_suppress_codes(scope: list[str] | None = None,
                           kb_dir: Path | None = None) -> set[str]:
    """scope 內所有 binding 項要抑制的預掃檢核點代碼(供 pipeline 確定性濾除)。
    這是「講過的規定不再重複報」的確定性保證 —— 不靠模型讀慣例後自律。
    先做衝突解析(高權威覆蓋低權威),存活者中的 binding 項才算數。"""
    scope = scope or []
    survivors, _ = resolve_conflicts(load_items(kb_dir))
    codes: set[str] = set()
    for it in survivors:
        if it.tier == "binding" and _in_scope(it, scope):
            codes.update(it.suppress)
    return codes
