#!/usr/bin/env python3
"""量「檢核點與規格待補改成看模型標的格式」改了什麼——不呼叫模型、不連資料庫。

  python eval/measure_hint_tags.py <報告目錄>...     # run_eval.py --dump-reports 存下來的 mr_*.json

**為什麼要量**:檢核點從「同檔的 finding 有沒有提到關鍵詞」改成「標題開頭照格式寫
`[H003] 已核對:…`」,模型沒照格式寫的會多補一條、多一次人工;規格待補改成看「規格未載明:
<項目名>」。這兩件事的代價取決於**模型照格式寫的比例**,單元測試量不到,要拿真的報告看。

怎麼量(讀報告與重跑預掃見 saved_reports.py,binding 抑制跟正式路徑同一份):

  * 檢核點幾處(只算 hint,不算 R 規則)、舊做法(關鍵詞)與新做法(格式)各補幾條
  * 改交人工:舊做法沒有補、新做法有補,**而且除了檢核點以外沒有別的原因擋自動放行**
    (看存下來的 `_policy_signals` 配上設定檔的 policy;本來就會交人工或擋下的不算)。
    沒有 `_policy_signals` 的報告(舊報告)判斷不了,另外列出、不算進去
  * 模型照格式寫的檢核點回應幾條(「已核對」/「待確認」),標了「規格未載明:」的幾條
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
sys.path.insert(0, str(ROOT / "eval"))

from orchestrator import pipeline  # noqa: E402
from orchestrator.config import load_config  # noqa: E402
from run_eval import auto_approve_blockers  # noqa: E402
from saved_reports import iter_saved, mentions, model_findings, prescan  # noqa: E402

# 改之前的關鍵詞表(快照,只給這支程式對照用;程式裡已經拿掉了)
OLD_HINT_KEYWORDS = {
    "H001": ["沖正", "退匯", "淨額"],
    "H002": ["粒度", "聯名", "重複通報", "多筆通報"],
    "H003": ["規格", "核定"],
    "H004": ["敏感欄位", "憑證", "密碼", "正當", "最小必要", "明碼"],
    "H005": ["遮罩", "account_id", "主鍵", "明碼輸出"],
}
_SPEC_CODE = re.compile(r"R-\d+")
_HINTS_ONLY = "待確認檢核點"         # auto_approve_blockers 裡檢核點那一項的文字
NO_SIGNALS = "無 _policy_signals"


def _hint_hits(pre: list[dict]) -> int:
    return sum(1 for e in pre for h in e["rules"] if h.get("severity") == "hint")


def _by_code(entry: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for h in entry["rules"]:
        if h.get("severity") == "hint":
            out.setdefault(h.get("rule", ""), []).append({**h, "severity": "info"})
    return out


def _old_pending(findings: list[dict], pre: list[dict]) -> int:
    """舊做法補了幾條:同檔的 finding 提到關鍵詞就算回應。"""
    added = 0
    for entry in pre:
        for code, hits in _by_code(entry).items():
            said = mentions(findings, OLD_HINT_KEYWORDS.get(code, [code]))
            added += len(pipeline._coverage(said, entry["path"], hits)[0])
    return added


def _new_pending(findings: list[dict], pre: list[dict]) -> int:
    out = pipeline.enforce_hints({"findings": json.loads(json.dumps(findings))}, pre)
    return sum(1 for f in out["findings"]
               if f.get("_source") == pipeline.PROGRAM_SOURCE and "檢核點待人工確認" in str(f.get("title", "")))


def other_blockers(report: dict, policy: dict) -> list[str] | None:
    """除了檢核點以外,這份報告還有哪些原因不能自動放行。判斷不了(沒有 `_policy_signals`、
    也不是被擋下的)回 None。"""
    reasons = auto_approve_blockers(report, policy)
    if report.get("decision") == "blocked":
        return reasons
    ps = report.get("_policy_signals")
    if not ps or reasons == [NO_SIGNALS]:
        return None
    extra = [name for name, on in (("需要人工確認", ps.get("needs_human")), ("規格有改", ps.get("spec_changed")),
                                   ("有環節沒跑完", ps.get("incomplete")), ("規則命中", ps.get("rule_hits")),
                                   ("冷讀", ps.get("cold_read"))) if on]
    return [r for r in reasons if r != _HINTS_ONLY] + extra


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


def measure(report: dict, case: dict, policy: dict) -> dict:
    model = model_findings(report)
    pre = prescan(case)
    old, new = _old_pending(model, pre), _new_pending(model, pre)
    others = other_blockers(report, policy)
    replies = [pipeline._hint_reply(f) for f in model]
    return {"hints": _hint_hits(pre), "old": old, "new": new,
            # None:判斷不了(另外列出);True:只因為新做法多補的檢核點而改交人工
            "flipped": None if others is None else (old == 0 and new > 0 and not others),
            "checked": sum(1 for r in replies if r and r[1] == pipeline.HINT_CHECKED),
            "asked": sum(1 for r in replies if r and r[1] == pipeline.HINT_ASK),
            **_pending_counts(model, _spec_text(case))}


def main(dirs) -> int:
    policy = load_config().policy
    rows = [(name, measure(report, case, policy)) for name, report, case in iter_saved(dirs)]
    if not rows:
        print("找不到可以對照的報告(需要 eval/golden 裡有同名的 case)")
        return 1
    head = ("case", "檢核點", "舊補", "新補", "改交人工", "已核對", "待確認", "標待補",
            "待補在清單", "待補不在", "用字命中")
    keys = ("hints", "old", "new", "flipped", "checked", "asked", "tagged", "listed", "unlisted", "fallback")
    show = {True: "是", False: "", None: "?"}
    print("  ".join(f"{h:>8}" for h in head))
    for name, r in rows:
        print("  ".join(f"{str(v):>8}" for v in
                        [name] + [show[r[k]] if k == "flipped" else r[k] for k in keys]))
    total = lambda k: sum(r[k] for _, r in rows)
    unknown = [name for name, r in rows if r["flipped"] is None]
    print(f"\n小計:檢核點 {total('hints')} 處;舊做法補 {total('old')} 條、新做法補 {total('new')} 條;"
          f"只因為檢核點而改成交人工的報告 {sum(1 for _, r in rows if r['flipped'])} 份"
          f"(共 {len(rows)} 份)")
    if unknown:
        print(f"沒有 _policy_signals、判斷不了會不會改交人工的 {len(unknown)} 份(不算進上面):"
              + "、".join(unknown))
    print(f"模型照格式回應檢核點:已核對 {total('checked')} 條、待確認 {total('asked')} 條;"
          f"標「規格未載明:」{total('tagged')} 條(其中在清單裡 {total('listed')}、不在 {total('unlisted')});"
          f"沒照格式、靠用字比對命中 {total('fallback')} 條")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:]))
