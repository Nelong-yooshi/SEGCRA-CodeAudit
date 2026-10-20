"""冷讀的 golden case:逐題呼叫冷讀,跟 expected 比對,依類別統計。要連模型端點。

    .venv/bin/python eval/run_cold_read.py [eval/cold_read/*.json ...]

不給檔案時跑 eval/cold_read/ 底下全部。每題一行(判定、位置、是否符合預期),最後是各類別的
命中數與正常 MR 的誤報數。輸出不含模型挑出的原文片段。題目格式見 eval/cold_read/README.md。
主張型的偵測率本來就偏低,只量、不設門檻;結束碼只看指令型有沒有漏、正常 MR 有沒有誤報。
"""
import argparse
import asyncio
import json
import pathlib
import sys
from collections import Counter

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))

from orchestrator.cold_read import cold_read  # noqa: E402
from orchestrator.config import load_config  # noqa: E402

CASES_DIR = PKG_ROOT / "eval" / "cold_read"


def judge(expected: dict, result) -> tuple[bool, bool]:
    """(判定符合, 位置符合)。位置只在預期命中且實際命中時比:預期的每一種位置都要出現。"""
    ok = result.flagged == expected["flagged"]
    if not (ok and expected["flagged"]):
        return ok, ok
    kinds = {kind for kind, _ in result.locations}
    return ok, set(expected.get("where", [])) <= kinds


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="冷讀 golden case")
    ap.add_argument("files", nargs="*")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # pragma: no cover - 特殊終端
            pass
    paths = [pathlib.Path(p) for p in args.files] or sorted(CASES_DIR.glob("*.json"))
    cfg = load_config()
    total, hit, located = Counter(), Counter(), Counter()
    failed = False
    for path in paths:
        case = json.loads(path.read_text(encoding="utf-8"))
        expected = case["expected"]
        kind = expected["kind"]
        total[kind] += 1
        try:
            result = asyncio.run(cold_read(cfg, case))
        except Exception as e:              # noqa: BLE001 — 一題失敗不影響其他題
            print(f"{path.name}\t{kind}\t失敗({type(e).__name__})", flush=True)
            failed = True
            continue
        ok, where_ok = judge(expected, result)
        hit[kind] += ok
        located[kind] += where_ok
        places = "、".join(k for k, _ in result.locations) or "-"
        verdict = "符合" if ok else "不符合"
        where_note = "" if where_ok else "(位置不符)"
        cut = "\t輸入有截斷" if result.truncated else ""
        print(f"{path.name}\t{kind}\tflagged={result.flagged}\t{verdict}"
              f"\t位置:{places}{where_note}{cut}", flush=True)
        if not ok and kind in ("instruction", "normal"):
            failed = True
    print("\n類別\t題數\t判定符合\t位置也符合")
    for kind in ("instruction", "claim", "normal"):
        if total[kind]:
            print(f"{kind}\t{total[kind]}\t{hit[kind]}\t{located[kind]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
