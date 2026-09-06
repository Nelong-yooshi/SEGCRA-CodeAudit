#!/usr/bin/env python3
"""autopin — 讓「使用者講定的規範」透過機制自動被定住(GitLab MR = 鎖,無需新 UI)。

觸發(自動起草 binding → 開 MR 到知識庫 repo,人一鍵 merge 即生效):
  from-comment "<審查人員的一句話指示>"   AI 判斷是否為常設規範,是則起草 + 開 MR
  from-history                      同型 finding 被駁回≥2次 → auto-consolidated binding + 開 MR
維運:
  seed                              把本地 memory/knowledge/ 上傳到 repo main(初始化基準)
  merge <iid>                       合併 MR(模擬人按下 merge)
  sync                              把 repo main 拉回 memory/knowledge/(合併後生效)

  .venv/bin/python scripts/autopin.py <subcommand> [args] [--profile fast]
"""
import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))
from orchestrator.config import load_config                           # noqa: E402
from orchestrator.knowledge_governance import (                       # noqa: E402
    KnowledgeRepo, draft_binding, propose_binding)
from toolbox.knowledge_store import Item                              # noqa: E402

KB_LOCAL = PKG_ROOT / "memory" / "knowledge"
HISTORY = PKG_ROOT / "memory" / "review_history" / "history.jsonl"


async def _propose_from_text(cfg, repo, text, author, scope, profile):
    item = await draft_binding(cfg, text, profile, default_scope=scope)
    if not item:
        print(f"  ✗ 「{text[:30]}…」判定為非常設規範,不起草(避免亂 pin)")
        return None
    mr = propose_binding(repo, item, evidence=f"{author} 於討論中明確指示:「{text[:80]}」")
    print(f"  ✓ 起草 binding「{item.id}」→ MR !{mr['iid']}  {mr['web_url']}")
    return mr


def _cluster_reject(records, threshold=2):
    """同 tag(或標題)被 rejected ≥ threshold 次 → 一個抑制型 binding 候選。"""
    by_tag = defaultdict(list)
    for r in records:
        if r.get("disposition") == "rejected":
            by_tag[r.get("tag") or r.get("title", "")].append(r)
    return {k: v for k, v in by_tag.items() if len(v) >= threshold}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["from-comment", "from-history",
                                    "seed", "merge", "sync"])
    ap.add_argument("arg", nargs="?", help="指令參數(留言文字 / MR iid)")
    ap.add_argument("--scope", default="anomaly-rules")
    ap.add_argument("--author", default="審查人員")
    ap.add_argument("--profile", default="fast")
    args = ap.parse_args()

    repo = KnowledgeRepo()
    repo.ensure_project()
    scope = [s.strip() for s in args.scope.split(",") if s.strip()]

    if args.cmd == "seed":
        repo.seed_from_local(KB_LOCAL)
        print(f"已上傳 {len(repo.list_md())} 個知識項到 repo main")
        return

    if args.cmd == "merge":
        print("state:", repo.merge_mr(int(args.arg)).get("state"))
        return

    if args.cmd == "sync":
        pulled = repo.sync_to_local(KB_LOCAL)
        print(f"已從 repo main 同步 {len(pulled)} 個知識項到 {KB_LOCAL}")
        return

    cfg = load_config()

    if args.cmd == "from-comment":
        await _propose_from_text(cfg, repo, args.arg, args.author, scope, args.profile)

    elif args.cmd == "from-history":
        recs = [json.loads(l) for l in HISTORY.read_text(encoding="utf-8").splitlines()
                if l.strip()]
        clusters = _cluster_reject(recs)
        print(f"達門檻(rejected≥2)的誤報群:{len(clusters)}")
        for tag, group in clusters.items():
            notes = "; ".join(sorted({r.get("note", "") for r in group if r.get("note")}))
            title = sorted({r.get("title", "") for r in group})[0]
            item = Item(id=f"auto-suppress-{str(tag).lower()}", tier="binding",
                        source="auto-consolidated", scope=scope, suppress=[tag],
                        conflict_key=f"{str(tag).lower()}-applicability",
                        tags=["誤報收斂"], ts=int(time.time()),
                        text=f"{notes};此環境下審查不應以「{title}」作為 finding。")
            mr = propose_binding(repo, item,
                                 evidence=f"同型 finding 被人工駁回 {len(group)} 次(tag={tag})")
            print(f"  ✓ auto-consolidated「{item.id}」→ MR !{mr['iid']}  {mr['web_url']}")


if __name__ == "__main__":
    asyncio.run(main())
