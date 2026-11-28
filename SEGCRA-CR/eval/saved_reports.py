"""讀 run_eval.py --dump-reports 存下來的報告、對回 golden case 的共用部分 —— 不呼叫模型、不連資料庫。

measure_rule_backfill.py 與 measure_hint_tags.py 都用這裡的:

  * iter_saved(dirs):每份存下來的報告與同名的 golden case
  * model_findings(report):報告裡模型自己寫的 findings(濾掉程式補的,`_source` 是 `program`)。
    存下來的報告是整份審查跑完的結果,不濾掉的話,舊做法會把程式補的那條當成「模型講過了」
  * prescan(case):拿 golden case 的檔案內容重跑規則層,**套上跟正式路徑同一份的 binding 抑制**
    (pipeline.suppress_prescan;範圍照 pipeline._derive_scope)。報告檔沒有存預掃結果,
    而正式路徑在預掃後就把被抑制的檢核點濾掉了,模型看不到;不套的話那些檢核點會被算成「新補」
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator import pipeline  # noqa: E402
from toolbox.knowledge_store import binding_suppress_codes  # noqa: E402
from toolbox.sqltools import run_rules  # noqa: E402


def prescan(case: dict) -> list[dict]:
    """golden case 的檔案內容重跑規則層,再濾掉 binding 慣例抑制的檢核點(跟正式路徑同一份)。"""
    out = []
    for f in case.get("files", []):
        sql = f.get("full_content") or f.get("diff") or ""
        if not sql:
            continue
        res = json.loads(run_rules(sql))
        hits = res.get("hits") if isinstance(res, dict) else res
        out.append({"path": f.get("path", ""), "rules": hits or []})
    return pipeline.suppress_prescan(out, binding_suppress_codes(pipeline._derive_scope(case)))


def model_findings(report: dict) -> list[dict]:
    return [f for f in report.get("findings", [])
            if isinstance(f, dict) and f.get("_source") != pipeline.PROGRAM_SOURCE]


def mentions(findings: list[dict], keywords: list[str]) -> list[dict]:
    """舊做法:標題、內文或建議提到任一關鍵詞的 finding(summary、檔名不算)。"""
    return [f for f in findings
            if any(k in " ".join(str(f.get(x) or "") for x in ("title", "detail", "suggestion"))
                   for k in keywords)]


def iter_saved(dirs):
    """(檔名, 報告, golden case):只列 eval/golden 有同名 case、而且有 findings 的報告。"""
    for d in dirs:
        for path in sorted(pathlib.Path(d).glob("mr_*.json")):
            report = json.loads(path.read_text(encoding="utf-8"))
            golden = ROOT / "eval" / "golden" / path.name
            if not golden.exists() or "findings" not in report:
                continue
            yield path.name, report, json.loads(golden.read_text(encoding="utf-8"))
