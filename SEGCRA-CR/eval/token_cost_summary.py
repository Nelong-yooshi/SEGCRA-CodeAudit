"""評測報告的主審查 token 成本統計,以及兩批報告(例如 main 與關掉全文的對照組)的對照。

背景:把變更檔的完整內容放進 prompt 之後,黑盒量到主審查讀入的 prompt 大約翻倍、生成多約兩成。
要判斷這筆開銷值不值,得用「端點回報、回填進報告的真實 token 數」來比,不用字元估計,也不用
時間(評測機負載會晃,時間不可比)。時間看 stage_time_summary.py,token 看這支。

    python eval/token_cost_summary.py <報告目錄或檔> [<第二批> ...]

讀 run_eval.py --dump-reports 存的報告(或任何 review_mr 回傳的報告 JSON)。每給一個路徑算成
一組;給兩組以上時並排對照、以第一組為基準算差值(main 放第一個、對照組放第二個,就看得出
全文省下多少讀入、生成差多少)。

用的是報告 `_output_meta` 的回填值,**只含主審查**(測資生成、仲裁、風格基準各自呼叫模型,
不在這裡;見 pipeline._output_meta_record):

    prompt_tokens            主審查最後一次呼叫讀入的 token —— 對應黑盒說的「主審查 prompt 中位數」
    completion_tokens        主審查最後一次呼叫生成的 token
    prompt_tokens_total      主審查這次 run_agent 每一輪的加總(有工具呼叫時每輪重讀整串對話,
                             所以遠大於最後一次)—— 讀入的實際成本
    completion_tokens_total  同上,生成側

沒有回填的份數(舊報告沒有 `_output_meta`,或端點那幾輪沒回報用量)另外列出,不混進真實 token
的統計——把沒量到的當成 0 加進去會把開銷算少。`rounds_without_usage` 不是 0 的份數也標出來:
那幾份有輪次沒回報用量,`*_total` 是低估的。

「生成的去向」只能粗分,原因寫在這裡免得誤讀:端點不回報「思考」用掉多少 token,報告也沒分開
記。這支只把「生成總量」和「落在報告文字裡的量(摘要 + 模型自己那幾條 finding,用 len/3 估)」
並列,差額是沒落在報告文字裡的生成(思考 + 工具輪的中間生成 + 被後處理取代掉的 finding),
**不等於**思考本身。要再往下拆,得端點先分項回報用量。

這支只用標準庫,方便搬到只有報告 JSON 的機器上單獨跑。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

# 跟 orchestrator.pipeline.PROGRAM_SOURCE 一致:程式補的 finding 的 _source。算「模型寫了
# 多少字」時要把這些剔掉——它們是確定性後處理補的,不是模型生成的。
PROGRAM_SOURCE = "program"


def est_tokens(text: str) -> int:
    """粗估 token 數,和 orchestrator.config.estimate_tokens 一樣用 len/3。內聯一份,讓這支
    不必 import orchestrator 也能跑。只拿來估「報告文字」的量;真實讀入/生成一律用端點回報的
    數字,不用這個估計。"""
    return len(text) // 3


def _reports(paths):
    """把每個路徑(目錄或單檔)底下的報告 JSON 讀出來。判斷條件跟 stage_time_summary 一樣:
    是 dict 且有 findings 才當成報告,跳過 result.json、fingerprint.json 這類。"""
    for p in paths:
        p = Path(p)
        for f in (sorted(p.glob("*.json")) if p.is_dir() else [p]):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and "findings" in data:
                yield f.stem, data


def _q(values, q):
    """第 q 分位(跟 stage_time_summary 同一個實作:排序後取索引,不內插)。"""
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def _finding_text(f: dict) -> str:
    """一條 finding 裡模型寫的字:標題、說明、建議。"""
    return " ".join(str(f.get(k, "")) for k in ("title", "detail", "suggestion"))


def _is_int(value) -> bool:
    """是整數,且不是 bool(bool 是 int 的子類,會被 isinstance 當成 int)。"""
    return isinstance(value, int) and not isinstance(value, bool)


def metrics(report: dict) -> dict:
    """單份報告的主審查 token 指標。回填齊全時 prompt/gen 是端點回報的真實值,否則記成 None
    (不進真實 token 的統計);報告文字量不靠回填,一律能算。"""
    om = report.get("_output_meta")
    om = om if isinstance(om, dict) else {}
    prompt_last, gen_last = om.get("prompt_tokens"), om.get("completion_tokens")
    prompt_total, gen_total = om.get("prompt_tokens_total"), om.get("completion_tokens_total")
    # 單次與加總都要是數字才算「有回填」;任一缺就當這份沒量到
    backfilled = all(_is_int(v) for v in (prompt_last, gen_last, prompt_total, gen_total))
    # 有幾輪端點沒回報用量——那幾輪沒加進 *_total,所以這份的 total 偏低,要標出來
    rounds_missing = om.get("rounds_without_usage") or 0

    findings = [f for f in (report.get("findings") or []) if isinstance(f, dict)]
    model_f = [f for f in findings if f.get("_source") != PROGRAM_SOURCE]
    prog_f = [f for f in findings if f.get("_source") == PROGRAM_SOURCE]
    summary = report.get("summary") or ""
    # 「落在報告文字裡的生成」只算模型寫的:摘要 + 模型自己那幾條 finding 的文字
    body_tokens = est_tokens(summary + " " + " ".join(_finding_text(f) for f in model_f))
    details = [len(str(f.get("detail", ""))) for f in model_f]

    return {
        "backfilled": backfilled,
        "incomplete": bool(rounds_missing),      # total 不完整(有輪沒回報用量)
        "prompt_last": prompt_last if backfilled else None,
        "gen_last": gen_last if backfilled else None,
        "prompt_total": prompt_total if backfilled else None,
        "gen_total": gen_total if backfilled else None,
        "report_body_tokens": body_tokens,
        # 生成總量減掉落在報告文字裡的,下限 0;是「沒落在報告文字裡的生成」,不是思考本身
        "gen_outside_report": max(gen_total - body_tokens, 0) if backfilled else None,
        "n_findings": len(findings),
        "n_model": len(model_f),
        "n_program": len(prog_f),
        "detail_chars_avg": statistics.mean(details) if details else 0.0,
    }


def _agg(values):
    """一串數字的份數、中位數、P90、總和;過濾掉 None(沒回填的那幾份)。全是 None 時回 None。"""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {"n": len(vals), "p50": statistics.median(vals),
            "p90": _q(vals, 0.9), "sum": sum(vals)}


# 真實 token(需要回填)與報告文字(一律可算)兩類指標的欄位名
_REAL_KEYS = ("prompt_last", "gen_last", "prompt_total", "gen_total", "gen_outside_report")
_TEXT_KEYS = ("report_body_tokens", "n_findings", "detail_chars_avg")


def _summarize_reports(reports) -> dict:
    """一串報告 dict 的彙總。跟路徑分開,好讓測試直接餵報告、不必落地成檔。"""
    rows = [metrics(r) for r in reports]
    out = {
        "reports": len(rows),
        "backfilled": sum(1 for m in rows if m["backfilled"]),
        "incomplete": sum(1 for m in rows if m["incomplete"]),
    }
    for key in _REAL_KEYS + _TEXT_KEYS:
        out[key] = _agg([m[key] for m in rows])
    return out


def summarize(paths) -> dict:
    """一組路徑底下所有報告的彙總。"""
    return _summarize_reports(r for _, r in _reports(paths))


def _fmt(agg, key="p50"):
    return "—" if not agg else f"{agg[key]:,.0f}"


def _print_group(label: str, s: dict) -> None:
    print(f"\n[{label}] 報告 {s['reports']} 份;有回填 token 的 {s['backfilled']} 份", end="")
    if s["incomplete"]:
        print(f";其中 {s['incomplete']} 份有輪次沒回報用量(total 偏低)", end="")
    print()
    if not s["backfilled"]:
        print("  沒有任何一份有回填 token——真實讀入/生成無法統計(舊報告或端點沒回報用量)")
    else:
        for key, name in (("prompt_last", "主審查 prompt(單次)"), ("gen_last", "主審查生成(單次)"),
                          ("prompt_total", "讀入 加總"), ("gen_total", "生成 加總"),
                          ("gen_outside_report", "生成未落在報告文字")):
            a = s[key]
            print(f"  {name:20s} 中位數 {_fmt(a):>10}  P90 {_fmt(a,'p90'):>10}  總和 {_fmt(a,'sum'):>12}")
    b = s["report_body_tokens"]
    print(f"  {'報告文字(估計)':18s} 中位數 {_fmt(b):>10}  P90 {_fmt(b,'p90'):>10}  總和 {_fmt(b,'sum'):>12}")
    print(f"  finding 數 中位數 {_fmt(s['n_findings'])};說明平均字數 中位數 {_fmt(s['detail_chars_avg'])}")


def _print_compare(labels, groups) -> None:
    """兩組以上時,以第一組為基準,對幾個關鍵指標的總和並排 + 差值。"""
    print("\n=== 對照(基準 = 第一組)===")
    base = groups[0]
    for key, name in (("prompt_total", "讀入 加總"), ("gen_total", "生成 加總"),
                      ("prompt_last", "prompt單次中位"), ("report_body_tokens", "報告文字"),
                      ("gen_outside_report", "生成未落報告")):
        use = "p50" if key == "prompt_last" else "sum"
        base_v = base[key][use] if base[key] else None
        row = [name.ljust(14)]
        for g in groups:
            a = g[key]
            cell = "—" if not a else f"{a[use]:,.0f}"
            if a and base_v and g is not base:
                diff = a[use] - base_v
                cell += f" ({diff:+,.0f}, {diff / base_v:+.0%})"
            row.append(cell)
        print("  " + " | ".join(row))
    print("  " + " | ".join(["(每組)".ljust(14)] + list(labels)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+", help="每個路徑算成一組(目錄或單一報告 JSON)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    labels = [Path(p).name or str(p) for p in args.paths]
    groups = [summarize([p]) for p in args.paths]
    # 空組要分清兩種原因:路徑打錯 vs 真的沒有報告。不然下面只會印「0 份、
    # 沒有回填 token」,很容易被誤讀成「這批真的跑出空報告」而下錯結論。警告走
    # stderr,不污染給人看/轉貼的統計本體。
    for p, s in zip(args.paths, groups):
        if not Path(p).exists():
            print(f"⚠ 路徑不存在:{p}(當成空組——是不是打錯了?)", file=sys.stderr)
        elif s["reports"] == 0:
            print(f"⚠ {p} 底下找不到任何報告 JSON(要有 findings 欄位才算);"
                  f"這組是空的,不是「舊報告沒回報用量」", file=sys.stderr)
    for label, s in zip(labels, groups):
        _print_group(label, s)
    if len(groups) > 1:
        _print_compare(labels, groups)


if __name__ == "__main__":
    main()
