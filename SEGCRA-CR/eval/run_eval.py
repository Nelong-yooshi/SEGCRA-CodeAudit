#!/usr/bin/env python3
"""golden set 評測 — 對 eval/golden/ 每個 case 跑完整審查管線,比對標準答案。

用法:
  python eval/run_eval.py                     # 跑全部 case
  python eval/run_eval.py --profile fast      # 換模型
  python eval/run_eval.py --layer injection   # 只跑某一層
  python eval/run_eval.py --case 101          # 只跑單一 case
  python eval/run_eval.py --dry-run           # 不呼叫 LLM(只驗確定性層)
  python eval/run_eval.py --json out.json     # 另存機器可讀結果

golden case = mock MR fixture + 兩段標準答案:
  expected  逐條應被抓到的問題 → 量 recall / precision(沿用 POC 的比對語意)
  _golden   層級與行為斷言     → 證明「哪一道防線真的啟動」、決策落在哪一態

格式與擴充方式見 eval/README.md。離開碼:全部斷言通過 0,否則 1(供 CI 用)。
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PKG_ROOT = EVAL_DIR.parent
GOLDEN_DIR = EVAL_DIR / "golden"

# 必須在 import pipeline 之前設定:toolbox/gitlab.py 在 module 載入時就把這兩個路徑固定住。
# 指向 golden/ 之後,review_mr(cfg, "101") 讀的就是 eval/golden/mr_101.json——
# 標準答案與受審內容永遠是同一個檔,不會像分開放時那樣悄悄對不上。
os.environ["FIXTURES_DIR"] = str(GOLDEN_DIR)
os.environ["REVIEW_OUTPUT"] = str(EVAL_DIR / "_output")

sys.path.insert(0, str(PKG_ROOT))

from orchestrator.config import load_config  # noqa: E402
from orchestrator.pipeline import review_mr  # noqa: E402

# Windows 主控台預設 cp950,中文輸出會炸;能改就改成 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - 舊版 Python / 特殊終端
        pass

# 計入 precision 分母的嚴重度。info/hint 不計:
# 管線補位的 H 系列檢核點、S 系列風格彙總都是 info,它們是「負責任的提問」,
# 不影響放行決策,算成誤報會低估 precision(POC 的 GOLDEN_SET.md 記為待補項)。
FP_SEVERITIES = {"blocker", "major", "minor"}

# dry-run 在 LLM 之前就 return,只有確定性前處理會動;這兩層的 case 才驗得動。
# 其餘層(注入強制、spec_exec、三態決策)都在 LLM 之後,dry-run 一律略過而非判失敗。
#
# 注意:層別只是粗篩,同一層內仍有需要模型的 case。例如 binding 有兩種:
#   抑制型(bind-reversal-netted,宣告 suppress)→ 預掃層確定性濾除,dry-run 驗得到;
#   規範型(bind-no-sysdate,無 suppress)      → 只是注入 context,要靠 LLM 讀了照做。
# 後者請在 case 的 _golden 標 "needs_llm": true,dry-run 會一併略過。
DRY_RUN_LAYERS = {"rule-base", "binding"}


# ─────────────────────────── 比對工具 ───────────────────────────

def match_expected(exp: dict, findings: list[dict]) -> bool:
    """expected 逐條比對:同檔 + 行號落在 line_range ±2 內即算命中(沿用 POC 語意)。"""
    lo, hi = exp.get("line_range", [0, 10 ** 9])
    return any(
        f.get("file") == exp["file"]
        and lo - 2 <= int(f.get("line") or 0) <= hi + 2
        for f in findings
    )


def finding_matches(spec: dict, f: dict) -> bool:
    """_golden 的 finding 選擇器:severity / file / 標題關鍵詞皆須符合(有給才比)。

    關鍵詞用 `title_contains`(單一)或 `title_contains_any`(任一命中即可)。
    **優先用 any 版**:findings 的文字是模型生成的,同一個問題可能寫「注入」也可能寫
    「規避」,把標準答案綁死在單一措辭會產生假失敗——防線是好的,只是用詞不同。
    """
    if "severity" in spec and f.get("severity") != spec["severity"]:
        return False
    if "file" in spec and f.get("file") != spec["file"]:
        return False
    haystack = f"{f.get('title', '')}\n{f.get('detail', '')}"
    needles = spec.get("title_contains_any")
    if needles:
        return any(n in haystack for n in needles)
    needle = spec.get("title_contains")
    if needle:
        return needle in haystack
    return True


def spec_desc(spec: dict) -> str:
    bits = []
    for k in ("severity", "title_contains", "file"):
        if k in spec:
            bits.append(str(spec[k]))
    if "title_contains_any" in spec:
        bits.append("|".join(spec["title_contains_any"]))
    return "/".join(bits) or "(任意)"


def read_signal(report: dict, key: str):
    """把好記的訊號名對到報告裡的稽核欄位。新增訊號時只改這裡。"""
    if key == "injection_hit":
        return bool(report.get("_injection_scan"))
    if key == "spec_exec_passed":
        return report.get("_spec_exec", {}).get("passed")
    if key == "spec_found":
        return bool(report.get("_spec_exec", {}).get("spec_code"))
    if key == "citations_removed":
        return bool(report.get("_removed_citations"))
    if key == "pending_hints":
        # decision=blocked 時管線提早 return,不會寫 _policy_signals
        return report.get("_policy_signals", {}).get("pending_hints")
    raise KeyError(f"未知的訊號名稱: {key}(可用: injection_hit / spec_exec_passed / "
                   f"spec_found / citations_removed / pending_hints)")


# ─────────────────────────── 斷言 ───────────────────────────

def check_golden(g: dict, report: dict, skip_post_llm: bool = False) -> list[tuple[str, bool, str]]:
    """跑 _golden 的所有斷言,回傳 [(名稱, 是否通過, 失敗說明)]。

    skip_post_llm=True(dry-run)時略過決策與訊號類斷言——那些機制在 LLM 之後才執行,
    dry-run 根本沒跑到,判它失敗只會製造假紅燈。
    """
    out: list[tuple[str, bool, str]] = []
    findings = report.get("findings", [])

    # ① 最終三態決策(門檻調校的主要觀測值);可給單一值或允許清單
    if "expect_decision" in g and not skip_post_llm:
        want = g["expect_decision"]
        want = [want] if isinstance(want, str) else list(want)
        got = report.get("decision")
        out.append(("decision", got in want, f"期望 {' 或 '.join(want)},實得 {got}"))

    # ② 防線訊號:證明「這道機制真的啟動了」,而不是 LLM 碰巧提到
    for key, want in ({} if skip_post_llm else (g.get("expect_signals") or {})).items():
        try:
            got = read_signal(report, key)
        except KeyError as e:
            out.append((f"signal:{key}", False, str(e)))
            continue
        out.append((f"signal:{key}", got == want, f"期望 {want},實得 {got}"))

    # ③ 必須出現的 finding
    for spec in g.get("expect_findings") or []:
        ok = any(finding_matches(spec, f) for f in findings)
        out.append((f"expect_finding[{spec_desc(spec)}]", ok,
                    "" if ok else "找不到符合條件的 finding"))

    # ④ 必須「不」出現的 finding —— binding「講過的不再提」只能靠這個驗
    for spec in g.get("forbid_findings") or []:
        hits = [f for f in findings if finding_matches(spec, f)]
        out.append((f"forbid_finding[{spec_desc(spec)}]", not hits,
                    "" if not hits else f"不該出現卻出現:{hits[0].get('title', '')[:50]}"))

    # ⑤ 乾淨案例:不該出現的嚴重度(量「會不會亂吼」)
    forbid_sev = set(g.get("forbid_severities") or [])
    if forbid_sev:
        bad = [f for f in findings if f.get("severity") in forbid_sev]
        out.append(("forbid_severities", not bad,
                    "" if not bad else
                    f"出現 {len(bad)} 條:{bad[0].get('severity')}/{bad[0].get('title', '')[:40]}"))

    # ⑥ 引用白名單:findings 引用的來源必須都在允許清單內(抗捏造法規/欄位)。
    #    validate_citations 應已剔除白名單外的引用,這裡驗「確實沒有漏網的」。
    if "allowed_citations" in g:
        allowed = set(g["allowed_citations"])
        stray = [(f.get("title", "")[:30], c)
                 for f in findings for c in (f.get("citations") or [])
                 if c not in allowed]
        out.append(("allowed_citations", not stray,
                    "" if not stray else
                    f"出現白名單外的引用 {len(stray)} 筆,首筆:{stray[0][1]}(於「{stray[0][0]}」)"))
    return out


# ─────────────────────────── 主流程 ───────────────────────────

def load_cases(layer: str | None, only: str | None) -> list[tuple[str, dict]]:
    cases = []
    for p in sorted(GOLDEN_DIR.glob("mr_*.json")):
        case = json.loads(p.read_text(encoding="utf-8"))
        mr_id = p.stem.removeprefix("mr_")
        if only and mr_id != only:
            continue
        if layer and (case.get("_golden") or {}).get("layer") != layer:
            continue
        cases.append((mr_id, case))
    return cases


async def run(args) -> int:
    cfg = load_config()
    cases = load_cases(args.layer, args.case)
    if not cases:
        print(f"golden set 為空或篩選後無 case(目錄: {GOLDEN_DIR})")
        return 1

    if args.dry_run:
        def _dry_ok(case):
            g = case.get("_golden") or {}
            return g.get("layer") in DRY_RUN_LAYERS and not g.get("needs_llm")

        skipped = [c for c, case in cases if not _dry_ok(case)]
        cases = [(c, case) for c, case in cases if _dry_ok(case)]
        print(f"⚠  --dry-run:不呼叫 LLM,只驗確定性前處理層 "
              f"({'/'.join(sorted(DRY_RUN_LAYERS))})。")
        if skipped:
            print(f"   略過 {len(skipped)} 個需要 LLM 之後階段的 case:"
                  f"{', '.join('mr_' + c for c in skipped)}\n")
        if not cases:
            print("   篩選後無可跑的 case。")
            return 0

    tp = fp = fn = 0
    rows, layer_stat, decisions, failures, gaps, errors = [], {}, {}, [], [], []

    for mr_id, case in cases:
        golden = case.get("_golden") or {}
        layer = golden.get("layer", "(未標層)")
        # 單一 case 的例外不得中斷整輪:一輪要跑數小時,經 SSH tunnel 的長連線
        # 偶發斷線(httpx ReadError → APIConnectionError)是常態,不能讓它清空前面的成果。
        try:
            report = await review_mr(cfg, mr_id, args.profile, dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001 - 蒐集所有失敗原因,不預設種類
            errors.append((mr_id, layer, f"{type(e).__name__}: {e}"))
            st = layer_stat.setdefault(layer, {"pass": 0, "fail": 0, "gap": 0, "error": 0})
            st["error"] += 1
            rows.append({"case": mr_id, "layer": layer, "error": f"{type(e).__name__}: {e}"})
            print(f"‼ mr_{mr_id} [{layer}] 執行失敗:{type(e).__name__}(已跳過,續跑下一個)")
            continue
        findings = report.get("findings", [])
        expected = case.get("expected", [])

        hits = sum(match_expected(e, findings) for e in expected)
        # 誤報只算「實質等級、且既對不上 expected 也不是 _golden 指名該出現的」finding。
        # 不含管線補位的 info/hint(那是負責任的提問,不影響放行決策)。
        asserted = golden.get("expect_findings") or []
        noise = [f for f in findings
                 if f.get("severity") in FP_SEVERITIES
                 and not any(match_expected(e, [f]) for e in expected)
                 and not any(finding_matches(spec, f) for spec in asserted)]
        tp += hits
        fn += len(expected) - hits
        fp += len(noise)

        checks = check_golden(golden, report, skip_post_llm=args.dry_run)
        bad = [c for c in checks if not c[1]]
        # known_gap:已知且已記錄成因的防線缺口。斷言照跑照顯示,但不列為失敗、
        # 不影響離開碼——避免為了讓套件變綠而刪掉暴露問題的 case。
        is_gap = bool(golden.get("known_gap"))
        decision = report.get("decision", "-")
        decisions[decision] = decisions.get(decision, 0) + 1
        st = layer_stat.setdefault(layer, {"pass": 0, "fail": 0, "gap": 0, "error": 0})
        st["gap" if (bad and is_gap) else ("fail" if bad else "pass")] += 1

        rows.append({
            "case": mr_id, "layer": layer, "intent": golden.get("intent", ""),
            "known_gap": is_gap, "expected": len(expected), "hit": hits,
            "noise": len(noise), "decision": decision,
            "checks": len(checks), "failed": len(bad),
            "failures": [{"name": n, "why": w} for n, _, w in bad],
        })
        if bad and is_gap:
            gaps.append((mr_id, layer, bad, golden.get("gap_note", "")))
        elif bad:
            failures.append((mr_id, layer, bad))

        mark = "⚠" if (bad and is_gap) else ("✓" if not bad else "✗")
        print(f"{mark} mr_{mr_id} [{layer}] recall {hits}/{len(expected)} "
              f"誤報 {len(noise)} 決策 {decision} 斷言 {len(checks) - len(bad)}/{len(checks)}"
              + ("  (已知缺口)" if bad and is_gap else ""))

    # ── 彙總 ──
    print("\n" + "=" * 62)
    print("逐層結果(斷言)")
    for layer, st in sorted(layer_stat.items()):
        total = st["pass"] + st["fail"] + st["gap"] + st.get("error", 0)
        notes = []
        if st["gap"]:
            notes.append(f"已知缺口 {st['gap']}")
        if st.get("error"):
            notes.append(f"執行失敗 {st['error']}")
        suffix = f"  ({', '.join(notes)})" if notes else ""
        print(f"  {layer:<18} {st['pass']}/{total} 通過{suffix}")

    print("\n決策分布(門檻是否擾人的觀測值)")
    for d, n in sorted(decisions.items()):
        print(f"  {d:<18} {n}")

    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    print("\n問題偵測(expected 逐條比對)")
    print(f"  TP={tp}  FN={fn}  FP={fp}")
    print(f"  precision={precision:.2f}" if precision is not None else "  precision=n/a")
    print(f"  recall   ={recall:.2f}" if recall is not None else "  recall   =n/a")

    if gaps:
        print("\n" + "=" * 62)
        print("已知缺口(不計入失敗,但必須持續可見)")
        for mr_id, layer, bad, note in gaps:
            print(f"\n  ⚠ mr_{mr_id} [{layer}]")
            for name, _, why in bad:
                print(f"    - {name}: {why}")
            if note:
                print(f"    成因/修補方向:{note}")

    if failures:
        print("\n" + "=" * 62)
        print("未通過的斷言")
        for mr_id, layer, bad in failures:
            print(f"\n  mr_{mr_id} [{layer}]")
            for name, _, why in bad:
                print(f"    ✗ {name}: {why}")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "profile": args.profile, "dry_run": args.dry_run,
            "precision": precision, "recall": recall,
            "tp": tp, "fp": fp, "fn": fn,
            "layers": layer_stat, "decisions": decisions,
            "known_gaps": [{"case": c, "layer": l, "note": n} for c, l, _, n in gaps],
            "errors": [{"case": c, "layer": l, "why": w} for c, l, w in errors],
            "cases": rows,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n機器可讀結果已寫入 {args.json}")

    return 1 if (failures or errors) else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=None, help="模型 profile(見 config/models.yaml)")
    ap.add_argument("--layer", default=None, help="只跑某一層(injection/rule-base/binding/…)")
    ap.add_argument("--case", default=None, help="只跑單一 case id(如 101)")
    ap.add_argument("--dry-run", action="store_true", help="不呼叫 LLM,只驗確定性層")
    ap.add_argument("--json", default=None, help="另存機器可讀結果的路徑")
    sys.exit(asyncio.run(run(ap.parse_args())))
