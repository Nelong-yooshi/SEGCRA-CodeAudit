#!/usr/bin/env python3
"""量「規則命中一律補報」改了什麼——不呼叫模型、不連資料庫。

  python eval/measure_rule_backfill.py                 # 用 eval/_reports_* 裡存下來的報告

**為什麼要量**:一律補報把判斷從「猜模型講過沒有」換成「照實說」,代價是報告會變長。
要知道長多少、以及近似標題去重接得住多少,不能只看單元測試。

怎麼量:報告檔裡沒有存預掃結果,所以拿 golden case 的檔案內容重跑一次規則層,得到
那次審查真正的命中;模型那一側用報告裡存下來的 findings。兩種做法各跑一次 enforce_rules:

  舊:_coverage(模型報告, 關鍵詞表) → 只補「沒對應到」的那幾處
  新:全部補

輸出三個數字:補報數、去重後的報告長度、以及被去重併掉的條數。

**樣本很小**(只有存下來的那幾份報告)。完整的數字要等評測跑一輪;這支程式就是
那時候拿來對照的工具。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator import pipeline  # noqa: E402
from toolbox.sqltools import run_rules  # noqa: E402

# 改之前的關鍵詞表(快照,只給這支程式對照用;程式裡已經拿掉了)
OLD_KEYWORDS = {
    "R001": ["WHERE", "全表", "整表", "整張表", "purge"],
    "R002": ["SELECT *", "明列欄位", "star"],
    "R003": ["NOT IN", "NULL 陷阱", "NOT EXISTS"],
    "R004": ["憑證", "密碼", "金鑰", "硬編碼", "hardcode", "credential", "secret"],
    "R005": ["執行環境", "DB_NAME", "HOST_NAME", "SUSER_NAME", "SERVERNAME", "sys."],
    "R006": ["OPENROWSET", "OPENDATASOURCE", "OPENQUERY", "BULK INSERT", "linked server",
             "四段式"],
    "R007": ["xp_cmdshell", "作業系統指令", "作業系統命令"],
    "R008": ["GRANT", "REVOKE", "EXECUTE AS", "ALTER ROLE", "授權", "權限授予"],
    "R009": ["動態 SQL", "sp_executesql", "字串串接", "組出來再執行"],
    "R010": ["run_query", "編譯期", "compile"],
}


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


def _old_backfill(report: dict, pre: list[dict]) -> int:
    """舊做法補了幾條:只補「模型沒對應到」的那幾處。"""
    before = list(report.get("findings", []))
    added = 0
    for entry in pre:
        by_code: dict[str, list[dict]] = {}
        for h in entry.get("rules", []):
            if str(h.get("rule", "")).startswith("R"):
                by_code.setdefault(h["rule"], []).append(h)
        for code, hits in by_code.items():
            missing, _ = pipeline._coverage(before, entry["path"],
                                            OLD_KEYWORDS.get(code, [code]), hits)
            added += len(missing)
    return added


def main(dirs) -> int:
    rows = []
    for d in dirs:
        for f in sorted(pathlib.Path(d).glob("mr_*.json")):
            report = json.loads(f.read_text(encoding="utf-8"))
            golden = ROOT / "eval" / "golden" / f.name
            if not golden.exists() or "findings" not in report:
                continue
            pre = _prescan(json.loads(golden.read_text(encoding="utf-8")))
            hits = sum(len([h for h in e["rules"] if str(h.get("rule", "")).startswith("R")])
                       for e in pre)
            old = _old_backfill(report, pre)
            new_report = pipeline.enforce_rules(json.loads(json.dumps(report)), pre)
            new = len(new_report["findings"]) - len(report.get("findings", []))
            kept = len(pipeline.sanitize_findings(
                json.loads(json.dumps(new_report)))["findings"])
            rows.append((f.name, len(report.get("findings", [])), hits, old, new,
                         len(new_report["findings"]), kept))
    if not rows:
        print("找不到可以對照的報告(需要 eval/golden 裡有同名的 case)")
        return 1
    head = ("case", "模型報的", "R 命中", "舊補報", "新補報", "合計", "去重後")
    print("  ".join(f"{h:>8}" for h in head))
    for r in rows:
        print("  ".join(f"{str(v):>8}" for v in r))
    print("\n小計:舊補報 {} 條、新補報 {} 條;去重併掉 {} 條".format(
        sum(r[3] for r in rows), sum(r[4] for r in rows),
        sum(r[5] - r[6] for r in rows)))
    return 0


def golden_ceiling() -> int:
    """整個 golden set 的上限:模型一條都沒涵蓋時,一律補報最多會多出幾條。

    報告檔只有兩份,對照出來的差異不具代表性。這個數字不需要模型輸出就算得出來,
    它是「報告最多會變長多少」的上界 —— 實際只會更少(模型本來就會報到一些)。
    """
    import collections
    total, per_case, multi = 0, [], 0
    for g in sorted((ROOT / "eval" / "golden").glob("*.json")):
        case = json.loads(g.read_text(encoding="utf-8"))
        pre = _prescan(case)
        codes = collections.Counter(
            (e["path"], h["rule"]) for e in pre for h in e["rules"]
            if str(h.get("rule", "")).startswith("R"))
        n = sum(codes.values())
        total += n
        per_case.append((g.name, n))
        multi += sum(1 for v in codes.values() if v > 1)
    hit_cases = [c for c in per_case if c[1]]
    print(f"golden set {len(per_case)} 個案例,{len(hit_cases)} 個有 R 命中,共 {total} 處")
    print(f"同一個檔案同一條規則命中多處的組合:{multi} 組"
          f"(舊做法在這種情況最多只補到「命中數 - 模型報的數量」)")
    for name, n in sorted(hit_cases, key=lambda x: -x[1])[:8]:
        print(f"  {name:22s} {n} 處")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--golden":
        raise SystemExit(golden_ceiling())
    raise SystemExit(main(args or [str(ROOT / "eval" / "_reports_701_702")]))
