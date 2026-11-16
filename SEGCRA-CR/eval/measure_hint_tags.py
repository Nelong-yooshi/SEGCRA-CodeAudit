#!/usr/bin/env python3
"""量「檢核點與規格待補改成看模型標的格式」改了什麼——不呼叫模型、不連資料庫。

  python eval/measure_hint_tags.py <報告目錄>...     # run_eval.py --dump-reports 存下來的 mr_*.json

**為什麼要量**:檢核點從「同檔的 finding 有沒有提到關鍵詞」改成「標題有沒有帶代碼」,
模型沒照格式寫的會多補一條「模型未回應」、多一次人工;規格待補改成看「規格未載明:
<項目名>」。這兩件事的代價取決於**模型照格式寫的比例**,單元測試量不到,要拿真的報告看。

怎麼量:報告檔裡沒有存預掃結果,所以拿 golden case 的檔案內容重跑一次規則層,得到那次
審查真正的檢核點;模型那一側用報告裡存下來、不是程式補的 findings(`_source` 不是
`program`)。對每份報告輸出:

  * 檢核點幾處、舊做法(關鍵詞)與新做法(代碼)各補幾條「模型未回應」
  * 會不會因此改變決策:舊做法沒有待確認、新做法有 → 這份報告會從可能自動放行變成交人工
  * 模型標了代碼的 finding 幾條、標了「規格未載明:」的幾條
  * 規格待補:照格式且在清單裡 / 照格式但不在清單裡 / 沒照格式、靠用字比對命中,各幾條

規格是用檔案內容裡第一個 `R-數字` 到 `specs/` 找同名檔,跟正式路徑的找法不完全一樣
(正式路徑另外看 dbt 的檔名對應),所以規格待補那幾欄是近似值。

**改 prompt 之前存下來的報告**,模型當然沒有照新格式寫:那時候的數字是「模型一條都沒照
格式寫」的上界(新做法補最多、待補全走用字比對)。要知道實際的比例,得用改了 prompt 之後
跑的評測報告。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator import pipeline  # noqa: E402
from toolbox.sqltools import run_rules  # noqa: E402

# 改之前的關鍵詞表(快照,只給這支程式對照用;程式裡已經拿掉了)
OLD_HINT_KEYWORDS = {
    "H001": ["沖正", "退匯", "淨額"],
    "H002": ["粒度", "聯名", "重複通報", "多筆通報"],
    "H003": ["規格", "核定"],
    "H004": ["敏感欄位", "憑證", "密碼", "正當", "最小必要", "明碼"],
    "H005": ["遮罩", "account_id", "主鍵", "明碼輸出"],
}
_ANY_HINT_TAG = re.compile(r"\[H\d{3}\]")
_SPEC_CODE = re.compile(r"R-\d+")


def _prescan(case: dict) -> list[dict]:
    """拿 golden case 的檔案內容重跑規則層,得到那次審查真正的命中。"""
    out = []
    for f in case.get("files", []):
        sql = f.get("full_content") or f.get("diff") or ""
        if not sql:
            continue
        res = json.loads(run_rules(sql))
        hits = res.get("hits") if isinstance(res, dict) else res
        out.append({"path": f.get("path", ""), "rules": hits or []})
    return out


def _hints(pre: list[dict]) -> int:
    return sum(1 for e in pre for h in e["rules"] if h.get("severity") == "hint")


def _old_pending(findings: list[dict], pre: list[dict]) -> int:
    """舊做法補了幾條「模型未回應」:同檔的 finding 提到關鍵詞就算回應。"""
    added = 0
    for entry in pre:
        by_code: dict[str, list[dict]] = {}
        for h in entry["rules"]:
            if h.get("severity") == "hint":
                by_code.setdefault(h.get("rule", ""), []).append(h)
        for code, hits in by_code.items():
            missing, _ = pipeline._coverage(findings, entry["path"],
                                            OLD_HINT_KEYWORDS.get(code, [code]),
                                            [{**h, "severity": "info"} for h in hits])
            added += len(missing)
    return added


def _new_pending(findings: list[dict], pre: list[dict]) -> int:
    out = pipeline.enforce_hints({"findings": json.loads(json.dumps(findings))}, pre)
    return sum(1 for f in out["findings"] if "檢核點待人工確認" in str(f.get("title", "")))


def _spec_text(case: dict) -> str | None:
    for f in case.get("files", []):
        for code in _SPEC_CODE.findall(f.get("full_content") or f.get("diff") or ""):
            path = ROOT / "specs" / f"{code}.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
    return None


def _pending_counts(findings: list[dict], spec: str | None) -> dict:
    """「spec-pending」同時包含照格式在清單裡的與沒照格式、靠用字命中的,用標題分開。"""
    out = pipeline.enforce_spec_pending({"findings": json.loads(json.dumps(findings))}, spec)
    counts = {"tagged": sum(1 for f in findings if pipeline._pending_tag(f) is not None),
              "listed": 0, "unlisted": 0, "fallback": 0}
    for a in out.get("_calibration", {}).get("annotated", []):
        if a.get("by") == "spec-pending-unlisted":
            counts["unlisted"] += 1
        elif a.get("by") == "spec-pending":
            tagged = pipeline._pending_tag({"title": a.get("title", "")}) is not None
            counts["listed" if tagged else "fallback"] += 1
    return counts


def main(dirs) -> int:
    rows = []
    for d in dirs:
        for path in sorted(pathlib.Path(d).glob("mr_*.json")):
            report = json.loads(path.read_text(encoding="utf-8"))
            golden = ROOT / "eval" / "golden" / path.name
            if not golden.exists() or "findings" not in report:
                continue
            case = json.loads(golden.read_text(encoding="utf-8"))
            model = [f for f in report.get("findings", [])
                     if isinstance(f, dict) and f.get("_source") != pipeline.PROGRAM_SOURCE]
            pre = _prescan(case)
            old, new = _old_pending(model, pre), _new_pending(model, pre)
            pc = _pending_counts(model, _spec_text(case))
            tags = sum(1 for f in model if _ANY_HINT_TAG.search(str(f.get("title", ""))))
            rows.append((path.name, _hints(pre), old, new, "是" if old == 0 and new else "",
                         tags, pc["tagged"], pc["listed"], pc["unlisted"], pc["fallback"]))
    if not rows:
        print("找不到可以對照的報告(需要 eval/golden 裡有同名的 case)")
        return 1
    head = ("case", "檢核點", "舊補", "新補", "改交人工", "標代碼", "標待補",
            "待補在清單", "待補不在", "用字命中")
    print("  ".join(f"{h:>8}" for h in head))
    for r in rows:
        print("  ".join(f"{str(v):>8}" for v in r))
    print(f"\n小計:檢核點 {sum(r[1] for r in rows)} 處;舊做法補 {sum(r[2] for r in rows)} 條、"
          f"新做法補 {sum(r[3] for r in rows)} 條;因此改成交人工的報告 "
          f"{sum(1 for r in rows if r[4])} 份(共 {len(rows)} 份)")
    print(f"模型標代碼 {sum(r[5] for r in rows)} 條、標「規格未載明:」{sum(r[6] for r in rows)} 條"
          f"(其中在清單裡 {sum(r[7] for r in rows)}、不在 {sum(r[8] for r in rows)});"
          f"沒照格式、靠用字比對命中 {sum(r[9] for r in rows)} 條")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:]))
