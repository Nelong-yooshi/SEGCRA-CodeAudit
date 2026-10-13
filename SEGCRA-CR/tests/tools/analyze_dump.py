#!/usr/bin/env python3
"""把 SEGCRA_DUMP_PROMPTS 的落檔整理成「模型到底照不照做」的表。

  python tests/tools/analyze_dump.py eval/_runs/dump_r1

測資生成只有真端點答得出來的問題是:**模型有沒有照「只補缺的那幾個」做**。
模擬驗不到(假模型照定義就是聽話的),覆蓋數也看不出來——46 個案例可能是乖乖
補出來的,也可能是模型每次都整份重寫、剛好湊到。這裡逐次呼叫比對:

  要補的那幾個「條件×方向」 vs 回應裡實際出現的那幾個

並標出三種不照做:
  * 多給:回了沒要的(不一定是壞事,但代表它沒在遵守批量,輸出會變大、容易截斷)
  * 漏給:要了沒回的
  * 改名:回了計畫裡不存在的 condition_id(#23 第 2 點的三條拒收規則之一,
          也是補缺模式最常見的失效方式——補進來的案例會被整批丟掉)
"""
import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from orchestrator.agent import extract_json  # noqa: E402

ASK_RE = re.compile(r"- 條件 (\S+?)\((.*?)\) 的 (true|false) 向")
HEAD_RE = re.compile(r"要補的\((\d+) 個,共缺 (\d+) 個\)")
RESP_RE = re.compile(r"\n===== response finish_reason=(\S+) completion_tokens=(\S+) "
                     r"chars=(\d+) =====\n")


def mode_of(req: str) -> str:
    # 判斷字串要避開規格內容:規格本文裡就有「骨架」兩個字
    if "先前已經產出部分測資計畫" in req:
        return "補缺"
    if "cases 一律留空" in req:
        return "骨架"
    if "超過長度上限被截斷" in req:
        return "整份(精簡)"
    if "無法解析" in req or "語法完全正確" in req:
        return "重試"
    return "整份"


def parse(path: pathlib.Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = RESP_RE.search(text)
    req, resp = (text[:m.start()], text[m.end():]) if m else (text, "")
    out = {"file": path.name, "mode": mode_of(req),
           "finish": m.group(1) if m else "(無回應)",
           "tokens": m.group(2) if m else "-",
           "chars": int(m.group(3)) if m else 0,
           "asked": [(a, d) for a, _, d in ASK_RE.findall(req)],
           "got": [], "repaired": None}
    head = HEAD_RE.search(req)
    out["batch"], out["missing"] = (head.group(1), head.group(2)) if head else ("-", "-")
    if resp.strip():
        meta: dict = {}
        plan = extract_json(resp, meta=meta)
        out["repaired"] = meta.get("repaired")
        if isinstance(plan, dict):
            for c in plan.get("cases") or []:
                if isinstance(c, dict) and c.get("condition_id") and c.get("direction"):
                    out["got"].append((str(c["condition_id"]), str(c["direction"])))
            out["conds"] = [c.get("id") for c in (plan.get("conditions") or [])
                            if isinstance(c, dict)]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dump_dir")
    a = ap.parse_args()
    files = sorted(pathlib.Path(a.dump_dir).glob("*.txt"))
    if not files:
        print(f"{a.dump_dir} 裡沒有落檔")
        return 1

    known: set[str] = set()      # 計畫裡存在的 condition_id(用來認「改名」)
    rows = []
    for f in files:
        r = parse(f)
        known |= {c for c in r.get("conds", []) if c}
        rows.append(r)

    print(f"{'#':>2} {'模式':<10} {'批量':>4} {'缺':>4} {'finish':<7} {'tok':>5} "
          f"{'要':>3} {'回':>3} {'多給':>4} {'漏給':>4} {'改名':>4} repaired")
    for i, r in enumerate(rows, 1):
        asked, got = set(r["asked"]), set(r["got"])
        renamed = {cid for cid, _ in got if known and cid not in known}
        extra = len(got - asked) if asked else len(got)
        print(f"{i:>2} {r['mode']:<10} {r['batch']:>4} {r['missing']:>4} "
              f"{r['finish']:<7} {str(r['tokens']):>5} "
              f"{len(asked) or '-':>3} {len(got):>3} {extra:>4} "
              f"{len(asked - got) if asked else '-':>4} {len(renamed):>4} "
              f"{r['repaired']}")

    fills = [r for r in rows if r["mode"] == "補缺"]
    if fills:
        tot_asked = sum(len(set(r["asked"])) for r in fills)
        tot_hit = sum(len(set(r["asked"]) & set(r["got"])) for r in fills)
        print(f"\n補缺 {len(fills)} 次:要了 {tot_asked} 個,其中 {tot_hit} 個真的回來了"
              f"({(100 * tot_hit // tot_asked) if tot_asked else 0}%)")
        print("「多給」不一定是壞事,但代表模型沒在遵守批量——輸出會變大、更容易截斷。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
