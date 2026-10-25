"""冷讀的 golden case:逐題呼叫冷讀,跟 expected 比對,依類別統計。要連模型端點。

    .venv/bin/python eval/run_cold_read.py [eval/cold_read/*.json ...]
    .venv/bin/python eval/run_cold_read.py --normal <公開題的 JSON ...>

不給檔案時跑 eval/cold_read/ 底下全部。每題一行(判定、位置、是否符合預期、秒數、沒看完的原因),
最後是各類別的命中數、正常 MR 的誤報數,以及時間與「沒看完」「輸出被截斷」的次數。輸出不含模型挑出的
原文片段。題目格式見 eval/cold_read/README.md。
主張型的偵測率本來就偏低,只量、不設門檻;結束碼只看指令型有沒有漏、正常 MR 有沒有誤報。

    --normal    題目沒有冷讀的 expected(fixtures 格式沒有 expected,`eval/golden/` 格式的 expected
                是 findings 清單)時,一律當成正常 MR(不該被標)。有冷讀 expected 的題目照它比對
"""
import argparse
import asyncio
import json
import pathlib
import statistics
import sys
from collections import Counter
from time import monotonic

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))

from orchestrator.cold_read import OUTPUT_TRUNCATED, ColdReadError, cold_read  # noqa: E402
from orchestrator.config import load_config  # noqa: E402

CASES_DIR = PKG_ROOT / "eval" / "cold_read"
KINDS = ("instruction", "claim", "normal")
NORMAL = {"kind": "normal", "flagged": False, "where": []}


def cold_expected(case: dict):
    """題目裡冷讀的預期;不是冷讀格式(沒有,或 `eval/golden/` 的 findings 清單)時回 None。"""
    expected = case.get("expected")
    if (isinstance(expected, dict) and expected.get("kind") in KINDS
            and isinstance(expected.get("flagged"), bool)):
        return expected
    return None


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
    ap.add_argument("--normal", action="store_true",
                    help="題目沒有冷讀的 expected 時一律當成正常 MR(量誤報用)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # pragma: no cover - 特殊終端
            pass
    paths = [pathlib.Path(p) for p in args.files] or sorted(CASES_DIR.glob("*.json"))
    cfg = load_config()
    total, hit, located = Counter(), Counter(), Counter()
    seconds, unread, output_cut, errors = [], Counter(), 0, 0
    failed = False
    for path in paths:
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"{path.name}\t讀不起來({type(e).__name__})", flush=True)
            failed = True
            continue
        if not isinstance(case, dict):
            print(f"{path.name}\t格式不對(不是 JSON 物件)", flush=True)
            failed = True
            continue
        expected = cold_expected(case)
        if expected is None and args.normal:
            expected = NORMAL
        if expected is None:
            print(f"{path.name}\t沒有冷讀的 expected(公開題請加 --normal)", flush=True)
            failed = True
            continue
        kind = expected["kind"]
        total[kind] += 1
        start = monotonic()
        try:
            result = asyncio.run(cold_read(cfg, case))
        except Exception as e:              # noqa: BLE001 — 一題失敗不影響其他題
            seconds.append(monotonic() - start)
            cut_output = isinstance(e, ColdReadError) and str(e) == OUTPUT_TRUNCATED
            output_cut += cut_output
            errors += not cut_output
            reason = "輸出被截斷" if cut_output else f"失敗({type(e).__name__})"
            print(f"{path.name}\t{kind}\t{reason}\t{seconds[-1]:.0f}s", flush=True)
            failed = True
            continue
        seconds.append(monotonic() - start)
        ok, where_ok = judge(expected, result)
        hit[kind] += ok
        located[kind] += where_ok
        places = "、".join(k for k, _ in result.locations) or "-"
        verdict = "符合" if ok else "不符合"
        where_note = "" if where_ok else "(位置不符)"
        cut = ""
        if result.truncated:
            why = result.unread or "輸入超過預算"
            unread[why] += 1
            cut = f"\t沒看完:{why}"
        print(f"{path.name}\t{kind}\tflagged={result.flagged}\t{verdict}"
              f"\t位置:{places}{where_note}\t{seconds[-1]:.0f}s{cut}", flush=True)
        if not ok and kind in ("instruction", "normal"):
            failed = True
    print("\n類別\t題數\t判定符合\t位置也符合")
    for kind in KINDS:
        if total[kind]:
            print(f"{kind}\t{total[kind]}\t{hit[kind]}\t{located[kind]}")
    if seconds:
        print(f"\n時間(秒):中位數 {statistics.median(seconds):.0f}、最長 {max(seconds):.0f}")
    print(f"沒看完:{sum(unread.values())} 題" + "".join(f"\n  {k}:{v}" for k, v in unread.items()))
    print(f"輸出被截斷:{output_cut} 題;其他失敗:{errors} 題")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
