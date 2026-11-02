#!/usr/bin/env python3
"""把存下來的報告重跑一次「判斷校準」,量它到底改了什麼——不呼叫模型、不連資料庫。

  python eval/replay_calibration.py eval/_reports_701_702       # 重放那個目錄裡的報告
  python eval/replay_calibration.py d/ --detail                 # 連每一條變動都列出來

**為什麼需要這支程式**:校準的單元測試與突變點全綠,不代表在實際的報告上沒有問題
(實測曾讓有缺陷的報告大量被放寬)。單元測試量不到那件事——它
量的是「每個判斷自己對不對」,而誤判是從「真實報告的用字」長出來的:規格範本每份都有的
「待補」標題,剛好跟 system prompt 規定的「實作與核定規格不符」在字詞上重疊。那種事只有
拿真實報告對照才會現形。

所以這支程式是**送審前的門檻**:改動校準之後,先看三個數字有沒有往反方向走。

  1. **通過數**(decision == auto_approved):校準只該讓決策變嚴,不該變寬。
  2. **抓到的預期缺陷數**(對照 golden 的 expected):**不能變少**。只看誤報數會把
     「少報」當成進步,那正是上一版犯的錯。
  3. **乾淨 case 的誤報數**:不能變多。

「改前」怎麼來:
  * 報告是校準之前存的(`_calibration` 不存在)→ 原樣就是改前。
  * 報告是校準之後存的 → 用 `report["_calibration"]` 把它**還原**:拿掉 `_needs_human`、
    把 `downgraded` 記的嚴重度改回 `from`。加註只動內文,不影響分數與決策,所以不必還原。
    這也是 `_calibration` 要逐筆記下來的理由之一。

兩側都用同一份 `apply_rubric` / `apply_policy` 重算,所以差異一定來自那三道校準。
"""
import argparse
import copy
import json
import sys
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator.config import load_config                                   # noqa: E402
from orchestrator.pipeline import (apply_policy, apply_rubric,                # noqa: E402
                                   calibrate_with_execution, enforce_claim_evidence,
                                   enforce_spec_pending)

import run_eval                                                              # noqa: E402

GOLDEN_DIR = PKG_ROOT / "eval" / "golden"
SPECS_DIR = PKG_ROOT / "specs"


def restore_precalibration(report: dict) -> dict:
    """把校準過的報告還原成校準前的狀態(見模組 docstring)。"""
    out = copy.deepcopy(report)
    calib = out.pop("_calibration", None) or {}
    out.pop("_needs_human", None)
    for entry in calib.get("downgraded", []):
        want_title, want_file = entry.get("title"), entry.get("file")
        for f in out.get("findings", []):
            if f.get("title") == want_title and (not want_file or f.get("file") == want_file):
                f["severity"] = entry.get("from", f.get("severity"))
                break
    return out


# 校準之前存的報告沒有 `_source` 欄位(那是跟著這次改動才加的),於是程式自己補的
# finding 會被當成模型的指控拿去對照——重放的結果就會比實際情況誇大。這裡用舊的標題
# 啟發式把它們補標回去。**這個相容處理只存在重放工具裡**,管線一律看欄位。
_PROGRAM_TITLE_HINTS = ("執行驗證", "無規格可驗", "疑似提示注入攻擊", "部分變更內容無法取得",
                        "模型輸出的格式不正確", "預掃無法解析", "dbt 樣板展開失敗",
                        "展開後的表名未經驗證", "審查模型嘗試呼叫未開放的寫入工具")


def tag_program_findings(report: dict) -> dict:
    """舊報告補上 `_source`:標題是 `[代碼] …` 或命中固定標題的,視為程式補的。"""
    findings = report.get("findings") or []
    if any(f.get("_source") for f in findings if isinstance(f, dict)):
        return report            # 已經有欄位了,不要覆蓋
    for f in findings:
        if not isinstance(f, dict):
            continue
        title = str(f.get("title") or "")
        if title.startswith("[") or any(h in title for h in _PROGRAM_TITLE_HINTS):
            f["_source"] = "program"
    return report


def skip_reason(report: dict) -> str:
    """校準為什麼對這份報告沒有作用(沒有的話回空字串)。

    印出來是為了讓「沒有變動」看得出是**資料不足**還是**校準沒生效**——兩者完全不同,
    而看不出差別的話,重放的綠燈就沒有意義。"""
    se = report.get("_spec_exec") or {}
    if not se:
        return "報告裡沒有 _spec_exec(執行驗證沒跑)"
    if se.get("no_spec"):
        return "沒有規格可對照(no_spec)"
    if not (se.get("conditions") or []):
        return "執行驗證沒有拆出條件"
    if not (se.get("case_results") or []):
        return "執行驗證沒有案例結果(測資或沙盒失敗)"
    return ""


def spec_text_for(report: dict) -> str:
    """報告裡記的規則碼 → 本機規格原文。讀檔不呼叫模型。"""
    code = (report.get("_spec_exec") or {}).get("spec_code")
    if not code:
        return ""
    p = SPECS_DIR / f"{code}.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def calibrated(report: dict, policy: dict, mr: dict) -> dict:
    out = copy.deepcopy(report)
    out = calibrate_with_execution(out)
    out = enforce_spec_pending(out, spec_text_for(out))
    out = enforce_claim_evidence(out)
    return apply_policy(apply_rubric(out), mr, policy)


# 「抓到」在這支工具裡要看嚴重度。`run_eval.match_expected` 只比對檔案與行號——那量的是
# 「模型有沒有提到這個位置」,拿它當閘門會對我們要防的壞法**完全失明**:把 major 壓成
# info,位置還在,它照樣算「抓到」,可是決策層已經放行了。所以這裡要求命中的那條
# finding 的嚴重度**擋得住自動放行**(info/minor 擋不住)。
_BLOCKING = {"major", "blocker"}


def caught(exp: dict, findings: list[dict]) -> bool:
    return any(run_eval.match_expected(exp, [f]) and f.get("severity") in _BLOCKING
               for f in findings)


def scored(report: dict, case: dict, policy: dict, mr: dict) -> dict:
    """同一份 rubric/policy 重算,再計分。"""
    r = apply_policy(apply_rubric(copy.deepcopy(report)), mr, policy)
    golden = case.get("_golden") or {}
    expected = case.get("expected", [])
    asserted = golden.get("expect_findings") or []
    findings = r.get("findings", [])
    hits = sum(caught(e, findings) for e in expected)
    noise = [f for f in findings
             if f.get("severity") in run_eval.FP_SEVERITIES
             and not any(run_eval.match_expected(e, [f]) for e in expected)
             and not any(run_eval.finding_matches(spec, f) for spec in asserted)]
    return {"decision": r.get("decision"), "score": r.get("score"),
            "hits": hits, "expected": len(expected), "noise": len(noise),
            "severities": sorted(f.get("severity", "?") for f in findings),
            "needs_human": list(r.get("_needs_human") or []),
            "report": r}


# 決策的嚴格程度。校準只該讓它**往嚴的方向**走;往寬走的唯一正當理由是
# `_calibration["downgraded"]` 記下的那種(有執行證據的降級),其餘都要亮紅燈。
_STRICTNESS = {"auto_approved": 0, "needs_human": 1, "blocked": 2}


def looser(before: str | None, after: str | None) -> bool:
    return _STRICTNESS.get(after, 1) < _STRICTNESS.get(before, 1)


def _sev_changes(before: dict, after: dict) -> list[str]:
    out = []
    for b, a in zip(before["report"].get("findings", []), after["report"].get("findings", [])):
        if b.get("severity") != a.get("severity"):
            out.append(f"{b.get('severity')} → {a.get('severity')}:{str(a.get('title'))[:40]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("reports", help="--dump-reports 存下來的目錄")
    ap.add_argument("--detail", action="store_true", help="列出每一條嚴重度變動與加註")
    args = ap.parse_args()

    d = Path(args.reports)
    files = sorted(p for p in d.glob("mr_*.json"))
    if not files:
        print(f"{d} 底下沒有 mr_*.json", file=sys.stderr)
        return 2
    policy = load_config().policy
    agg = {"before": {"pass": 0, "hits": 0, "noise": 0},
           "after": {"pass": 0, "hits": 0, "noise": 0},
           "expected": 0, "clean": 0, "cases": 0}
    rows, moved, unexplained = [], [], []

    for p in files:
        mr_id = p.stem.removeprefix("mr_")
        gp = GOLDEN_DIR / f"mr_{mr_id}.json"
        case = json.loads(gp.read_text(encoding="utf-8")) if gp.exists() else {}
        stored = json.loads(p.read_text(encoding="utf-8"))
        if stored.get("error"):
            print(f"‼ {p.name} 是失敗的紀錄({stored['error']}),跳過")
            continue
        mr = {"files": case.get("files", [])}
        base = tag_program_findings(restore_precalibration(stored))
        why = skip_reason(base)
        if why:
            print(f"  mr_{mr_id}:校準不適用({why})")
        before = scored(base, case, policy, mr)
        after = scored(calibrated(base, policy, mr), case, policy, mr)
        agg["cases"] += 1
        agg["expected"] += before["expected"]
        agg["clean"] += 1 if not before["expected"] else 0
        for k, v in (("before", before), ("after", after)):
            agg[k]["pass"] += 1 if v["decision"] == "auto_approved" else 0
            agg[k]["hits"] += v["hits"]
            agg[k]["noise"] += v["noise"]
        rows.append((mr_id, before, after))
        if before["decision"] != after["decision"]:
            tag = ""
            if looser(before["decision"], after["decision"]):
                # 放寬一定要有「執行證據推翻了什麼」的紀錄,不然就是沒有依據的放寬
                if (after["report"].get("_calibration") or {}).get("downgraded"):
                    tag = "(放寬,有執行證據)"
                else:
                    tag = "(放寬,**沒有執行證據**)"
                    unexplained.append(mr_id)
            moved.append(f"mr_{mr_id}:{before['decision']} → {after['decision']}{tag}")

    print(f"\n重放 {agg['cases']} 份報告(其中 {agg['clean']} 份沒有預期缺陷 = 乾淨 case)\n")
    print("| | 改前 | 改後 |")
    print("|---|---|---|")
    print(f"| 自動放行 | {agg['before']['pass']} | {agg['after']['pass']} |")
    print(f"| 抓到的預期缺陷 | {agg['before']['hits']}/{agg['expected']} | "
          f"{agg['after']['hits']}/{agg['expected']} |")
    print(f"| 誤報 | {agg['before']['noise']} | {agg['after']['noise']} |")

    bad = []
    if agg["after"]["hits"] < agg["before"]["hits"]:
        bad.append(f"**抓到的缺陷變少了({agg['before']['hits']} → {agg['after']['hits']})**"
                   f" —— 這就是上一版的壞法,校準不該讓偵測能力下降")
    if agg["after"]["pass"] > agg["before"]["pass"]:
        bad.append(f"**自動放行變多了({agg['before']['pass']} → {agg['after']['pass']})**"
                   f" —— 校準只該讓決策變嚴")
    if agg["after"]["noise"] > agg["before"]["noise"]:
        bad.append(f"誤報變多了({agg['before']['noise']} → {agg['after']['noise']})")
    if unexplained:
        bad.append(f"**{len(unexplained)} 份報告的決策被放寬,但 `_calibration` 裡沒有對應的"
                   f"降級紀錄**(mr_{', mr_'.join(unexplained)})—— 校準只該讓決策變嚴,"
                   f"唯一的例外是有執行證據的降級")

    if moved:
        print("\n決策變動:")
        for m in moved:
            print("  -", m)
    if args.detail:
        for mr_id, before, after in rows:
            ch = _sev_changes(before, after)
            nh = [x for x in after["needs_human"] if x not in before["needs_human"]]
            if ch or nh:
                print(f"\nmr_{mr_id}:")
                for c in ch:
                    print("   嚴重度", c)
                for n in nh:
                    print("   要人看:", n)
    print()
    for b in bad:
        print("‼", b)
    if not bad:
        print("✓ 三個數字都沒有往反方向走")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
