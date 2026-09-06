"""memory 工具模組 — 分層知識檢索 + 審查歷史(誤報收斂)+ 風格學習,行程內直接呼叫。

記憶分層(取代「慣例全部倒進 context」):
  binding   恆常規範,scope 內永遠注入、確定性強制(講過的規定不會因對話變長而遺忘)
  guideline/observed  大量知識,依 scope + 相關度「檢索」少量注入

工具:
  retrieve_knowledge  分層檢索(binding 全注入 + 相關 guideline top_k),含衝突解析
  get_conventions     只回傳 binding 恆常規範(向後相容;不再整包倒入)
  check_style         對 SQL 執行「已學會」的風格檢查(如前置逗號)
  learn_style_from_examples  從已合併範例學風格 → 寫成 observed 慣例
  lookup_similar_reviews / record_feedback  審查判例(誤報收斂迴路)
"""
import json
import os
import re
import time
from pathlib import Path

from . import knowledge_store as ks
from . import style_learn

PKG_ROOT = Path(__file__).resolve().parent.parent

HISTORY_FILE = Path(os.environ.get("MEMORY_HISTORY",
                                   PKG_ROOT / "memory" / "review_history" / "history.jsonl"))


def _scope_list(scope: str) -> list[str]:
    return [s.strip() for s in re.split(r"[,\s]+", scope) if s.strip()] if scope else []


def retrieve_knowledge(query: str, scope: str = "", top_k: int = 4) -> str:
    """分層檢索團隊知識。binding 恆常規範(scope 內)一律注入;guideline/observed
    依相關度取 top_k。scope 用逗號分隔(如 "anomaly-rules")。回傳 rendered(可直接
    放進 prompt)+ suppress_codes(要確定性濾除的檢核點)+ style_codes(已學會的風格檢查)
    + 衝突解析結果(高權威覆蓋低權威)。"""
    sc = _scope_list(scope)
    r = ks.retrieve(query, scope=sc, top_k=top_k)
    return json.dumps({
        "rendered": ks.render_for_prompt(r),
        "binding_ids": [it.id for it in r["binding"]],
        "retrieved_ids": [it.id for it in r["retrieved"]],
        "suppress_codes": sorted(ks.binding_suppress_codes(sc)),
        "style_codes": sorted(ks.active_style_codes(sc)),
        "suppressed_conflicts": r["suppressed"],
        "stats": r["stats"],
    }, ensure_ascii=False)


def get_conventions(scope: str = "") -> str:
    """取得團隊**恆常規範**(binding,必守)。注意:僅回傳恆常規範,不再整包倒入所有
    慣例——大量參考慣例請用 retrieve_knowledge 依變更內容檢索。"""
    sc = _scope_list(scope)
    survivors, _ = ks.resolve_conflicts(ks.load_items())
    binding = [it for it in survivors if it.tier == "binding" and ks._in_scope(it, sc)]
    return ks.render_for_prompt({"binding": binding, "retrieved": []}) or "(尚無恆常規範)"


def check_style(sql: str, scope: str = "") -> str:
    """對 SQL 執行「已學會且在此 scope 生效」的風格檢查(如團隊前置逗號習慣)。
    只檢查知識庫中確實學到、且未被更高權威覆蓋的風格碼。回傳命中清單。"""
    sc = _scope_list(scope)
    codes = ks.active_style_codes(sc)
    return json.dumps(style_learn.check_style(sql, active_codes=codes),
                      ensure_ascii=False)


def learn_style_from_examples(examples: list[str], persist: bool = True) -> str:
    """從「已合併成功的範例」學團隊風格(目前支援前置逗號)。學到就寫成 observed
    慣例(source=code-mined,權威最低,可被使用者明確指示覆蓋)。回傳學習結果。"""
    item = style_learn.mine_leading_comma(examples)
    if not item:
        return json.dumps({"learned": False,
                           "reason": "範例中前置逗號比例不足,不學(避免誤學雜訊)"},
                          ensure_ascii=False)
    if persist:
        ks.save_item(item)
    return json.dumps({"learned": True, "id": item.id, "code": item.code,
                       "tier": item.tier, "source": item.source,
                       "evidence": item.evidence, "text": item.text,
                       "persisted": persist}, ensure_ascii=False)


def _bigrams(s: str):
    s = re.sub(r"\s+", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def lookup_similar_reviews(text: str, top_k: int = 5) -> str:
    """以 finding 標題/SQL 片段查過去審查判例。回傳相似案例與人工處置
    (accepted/rejected)。曾被 rejected 的同型 finding 應降級或不報。"""
    if not HISTORY_FILE.exists():
        return json.dumps({"results": []}, ensure_ascii=False)
    q = _bigrams(text)
    scored = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        key = f"{rec.get('title','')} {rec.get('detail','')} {rec.get('sql','')}"
        overlap = len(q & _bigrams(key))
        if overlap:
            scored.append((overlap / max(len(q), 1), rec))
    scored.sort(key=lambda x: -x[0])
    return json.dumps(
        {"results": [{**r, "similarity": round(s, 3)} for s, r in scored[:top_k]]},
        ensure_ascii=False)


def record_feedback(title: str, disposition: str, detail: str = "",
                    sql: str = "", note: str = "", tag: str = "") -> str:
    """記錄一筆 finding 的人工處置。disposition: accepted | rejected。
    tag 填來源規則/檢核點代碼(如 H001),供學習迴路精準分群。"""
    if disposition not in ("accepted", "rejected"):
        return json.dumps({"error": "disposition 必須是 accepted 或 rejected"},
                          ensure_ascii=False)
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": int(time.time()), "title": title, "detail": detail,
           "sql": sql, "disposition": disposition, "note": note, "tag": tag}
    with HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return json.dumps({"ok": True}, ensure_ascii=False)

