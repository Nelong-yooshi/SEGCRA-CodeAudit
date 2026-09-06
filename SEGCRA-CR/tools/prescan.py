#!/usr/bin/env python3
"""prescan — 確定性 SQL 預掃 CLI(不經 LLM、不碰 GitLab)。

給桌邊 IDE agent(如 Copilot agent mode)照 instructions 跑的確定性檢查入口:
行程內呼叫 toolbox.sqltools 的 run_rules(rule-base:R 規則 + H 檢核點)與
lint(sqlfluff),輸出人類可讀結果。

用法:
  python tools/prescan.py path/to/change.sql

離開碼:0 = 無 blocker/major;1 = 有 blocker 或 major(可直接當 gate 用)。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from toolbox.sqltools import lint, run_rules   # noqa: E402

_SEV_ORDER = {"blocker": 0, "major": 1, "minor": 2, "hint": 3}
_SEV_MARK = {"blocker": "✖ BLOCKER", "major": "▲ MAJOR", "minor": "· minor",
             "hint": "? hint(待確認檢核點)"}


def main():
    ap = argparse.ArgumentParser(
        description="確定性 SQL 預掃(rule-base + lint),不經 LLM。")
    ap.add_argument("sql_file", help="要檢查的 SQL 檔路徑")
    ap.add_argument("--no-lint", action="store_true", help="只跑 rule-base,略過 lint")
    args = ap.parse_args()

    path = Path(args.sql_file)
    if not path.is_file():
        sys.exit(f"找不到檔案:{path}")
    sql = path.read_text(encoding="utf-8")

    print(f"== 預掃:{path} ==")
    hits = json.loads(run_rules(sql))
    if isinstance(hits, dict) and hits.get("error"):
        print(f"\n[rule-base] SQL 解析失敗:{hits['error']}")
        sys.exit(1)

    print(f"\n[rule-base] 命中 {len(hits)} 項")
    gate = 0
    for h in sorted(hits, key=lambda h: _SEV_ORDER.get(h.get("severity"), 9)):
        sev = h.get("severity", "")
        if sev in ("blocker", "major"):
            gate = 1
        print(f"  {_SEV_MARK.get(sev, sev):<24} [{h.get('rule')}] {h.get('message')}")
        if h.get("statement"):
            print(f"      ↳ {h['statement'][:120]}")

    if not args.no_lint:
        issues = json.loads(lint(sql))
        if isinstance(issues, dict) and issues.get("error"):
            print(f"\n[lint] 失敗:{issues['error']}")
        else:
            print(f"\n[lint] {len(issues)} 項(風格/反模式定位線索,不擋 gate)")
            for it in issues[:20]:
                print(f"  L{it.get('line')}: {it.get('code')} {it.get('description')}")
            if len(issues) > 20:
                print(f"  …(其餘 {len(issues) - 20} 項略)")

    print(f"\n結果:{'未通過(有 blocker/major,請修正後重跑)' if gate else '通過(無 blocker/major)'}")
    sys.exit(gate)


if __name__ == "__main__":
    main()
