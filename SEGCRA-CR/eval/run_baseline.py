#!/usr/bin/env python3
"""baseline 跑批與彙總 — 在**不可重現**的系統上量出可信的數字。

方法書:`eval/BASELINE.md`(為什麼這樣設計、每個判定規則的理由都在那裡)。
這支程式只負責執行方法書:跑批、驗結果有效、分級、彙總、比對。

核心前提(已實測,不是假設):**跑批已經固定 temperature=0 + seed=42,跑兩次結果
還是不一定一樣。**

量測模式是有的:`run_eval.py` 帶 `LLM_TEMPERATURE=0` / `LLM_SEED=42` 的預設,
正式審查不受影響(`models.yaml` 維持 0.1、不帶 seed)。而 `run_baseline.py` 正是
透過 `run_eval.py` 跑每個 case 的,所以跑批自動吃到這組設定。

問題是**那樣還是會翻盤**(`mr_406` 跨 6 小時:18 案例/0 缺口/通過 → 11 案例/5 缺口/
不通過)。seed 固定的是「同輸入下的取樣」,不是「輸入」;管線是多輪 agent loop,
後一輪送出去的 prompt 含前一輪的工具結果,那裡面有會變的東西。輸入一變,
同一個 seed 也救不了。確切機制尚未證實,驗法見 BASELINE.md §1/§7。

也就是說,**已經做到「固定 seed」這一步了,而它不夠**,所以下面這套方法仍然需要:

  * 單次結果的差異**無法判讀**——不知道是改壞了,還是它本來就會晃
  * 「跑三次取平均」也不夠:平均會丟掉最關鍵的資訊,也就是
    **哪些案例本來就穩、哪些本來就會晃**。把每次都一樣的 case 和會翻盤的 case
    平均在一起,等於用同一把尺量兩種東西

所以分兩階段,先量尺再量東西:

  階段一 `--stage1 --runs 5`
      什麼都不改,連續跑 N 次,量「測量工具本身的抖動」。產出**分級表**:
      穩定 / 邊界 / 不穩。這張表本身就是交付物——它直接回答
      「這套測試能信到什麼程度」,並且把「哪幾個 case 的綠燈是假的」講明白。

  階段二 (預設) `--classify-from <上一份 baseline>`
      依分級決定每個 case 跑幾次(穩定 1 / 邊界 3 / 不穩 5),
      硬閘門只放在「穩定案例」與「聚合指標」兩處,不放在會晃的個別案例上。

用法:
  # 階段一:量噪音(最貴的一次,之後沿用)
  python eval/run_baseline.py --stage1 --runs 5 --tag noise

  # 階段二:依分級自適應取樣,並與上一份比對
  python eval/run_baseline.py --classify-from eval/baselines/2026-09-18-57c0371-noise \
                              --compare      eval/baselines/2026-09-18-57c0371-noise

  # 只重新彙總(不跑任何 case),例如改了分級門檻想重看一次
  python eval/run_baseline.py --summarize-only eval/baselines/<dir>

  # 配對交錯:評估「某一項改動」時用,見 BASELINE.md「配對交錯」一節
  python eval/run_baseline.py --runs 3 --partner /path/to/other-worktree

離開碼:0 沒有判定為回歸;1 有 🔴 回歸或環境不可比;2 跑批本身失敗(環境問題)。
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PKG_ROOT = EVAL_DIR.parent
GOLDEN_DIR = EVAL_DIR / "golden"
BASELINES = EVAL_DIR / "baselines"
sys.path.insert(0, str(PKG_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover
        pass

# 自適應取樣:N 由「階段一量到的變異」決定,不是固定 3。
# 固定 N=3 會同時「浪費在穩定案例上」又「不夠用在不穩案例上」。
RUNS_BY_GRADE = {"stable": 1, "boundary": 3, "unstable": 5}

# 分級門檻。判準寫在這裡而不是散在程式裡,因為它是**方法的一部分**,
# 改門檻等於改方法 —— 改了就要在 BASELINE.md 說明並重跑階段一。
BOUNDARY_GAP_SPREAD = 2      # 覆蓋缺口數的極差在此之內算「小範圍」
BOUNDARY_CASE_SPREAD = 3     # 測資案例數的極差在此之內算「小範圍」


# ─────────────────────── 跑一個 case ───────────────────────

def case_ids(only: str | None) -> list[str]:
    """case id 一律是去掉 `mr_` 的那一段(`406`),因為 `run_eval.py --case` 吃的是這個。

    但**人記得的是檔名**(`mr_406.json`),文件裡的例子也多半寫 `mr_406`。
    照那樣打會篩不到任何 case、直接以「golden set 為空」離開——訊息還會指向
    golden 目錄,看起來像目錄壞了,而不是參數寫法不對。所以兩種寫法都收。
    """
    ids = sorted(p.stem.removeprefix("mr_") for p in GOLDEN_DIR.glob("mr_*.json"))
    if only:
        wanted = {c.strip().removeprefix("mr_") for c in only.split(",") if c.strip()}
        ids = [i for i in ids if i in wanted]
    return ids


# 基礎設施失敗的特徵字樣。**管線遇到這些會優雅降級**——照樣跑完、照樣產出一份
# 看起來正常的結果,只是把原因寫進 spec_exec 的降級訊息裡。那是管線該有的行為
# (正式審查不該因為端點抖一下就整個炸掉),但對跑批來說是陷阱:結果檔通過了
# 每一項既有檢查,於是環境失敗被計成「品質退步」。
#
# 這不是假設,是實測撞到的:2026-09-23 凌晨端點中斷,某一次跑批的結果是
# spec_exec 案例數 0、缺口 1,gap_detail 寫著
# 「測資生成失敗(LLM 呼叫失敗:APIConnectionError: Connection error.)」——
# 頂層沒有 error、errors 清單是空的,所以舊的 result_ok 判它有效。
_INFRA_FAILURE = re.compile(
    r"APIConnectionError|APITimeoutError|ConnectError|Connection error"
    r"|LLM 呼叫失敗|連不到|連線失敗"
    r"|沙盒不可用|無法連線執行驗證沙盒|未設定 SANDBOX_MSSQL_PASSWORD|沙盒執行異常"
    r"|Timeout|timed out|逾時")


def infra_failure(row: dict) -> str | None:
    """這一列是不是「環境壞了」而不是「品質不好」?是的話回傳原因。

    刻意只認**明確的基礎設施特徵**。像「測資生成失敗(兩次皆無合法輸出)」這種
    就不算——那是模型沒產出合法輸出,是真的品質問題,誤判成環境失敗會把真實的
    退步藏起來,比漏抓更糟。
    """
    gaps = ((row.get("spec_exec") or {}).get("gap_detail")) or []
    for g in gaps:
        if _INFRA_FAILURE.search(str(g)):
            return str(g)[:200]
    return None


def result_ok(path: Path, mr_id: str) -> bool:
    """**檔案存在不等於跑成功。**

    上一輪就是在這裡吃到虧:`run_percase.sh` 只檢查 `case_<id>.json` 存不存在,
    結果 4 個「連不到本機 Ollama:timed out」的錯誤紀錄被計成成功,
    32 個裡宣稱 30 個成功、實際只有 26 個(FINDINGS「發現十一」)。
    所以這裡驗內容:JSON 解得開、有這個 case 的那一列、那一列沒有 error,
    **而且那一列不是基礎設施失敗降級來的**(見 infra_failure)。

    > **已知限制**:沙盒中途掛掉偵測不到。沙盒失敗只進 findings,不進
    > `coverage_gaps`,所以結果列裡看不到。起飛前檢查會在開跑前擋掉沙盒不可用,
    > 但跑到一半才掛的情況目前只能靠人看 REPORT.md 的異常數字。
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if d.get("errors"):
        return False
    rows = [r for r in d.get("cases", []) if str(r.get("case")) == mr_id]
    if len(rows) != 1 or rows[0].get("error"):
        return False
    return not infra_failure(rows[0])


def run_one(mr_id: str, run_idx: int, out: Path, args, cwd: Path) -> dict:
    """跑單一 case,結果與完整報告都落地。

    per-case 一個子行程,不是一個行程跑完 32 個。理由:
      * **隔離**——一個 case 卡死或炸掉只影響它自己,前面幾小時的成果不會沒了
      * **可續跑**——已有有效結果的直接跳過(跑批十幾小時,中斷是常態)
      * **卡死可處理**——子行程才殺得掉;同一個行程裡的 async 呼叫卡住就只能整個等
    """
    jpath = out / "cases" / f"mr_{mr_id}.run{run_idx}.json"
    rpath = out / "reports" / f"run{run_idx}"
    lpath = out / "logs" / f"mr_{mr_id}.run{run_idx}.log"
    for p in (jpath.parent, rpath, lpath.parent):
        p.mkdir(parents=True, exist_ok=True)

    if args.resume and result_ok(jpath, mr_id):
        return {"status": "skipped", "attempts": 0, "seconds": 0.0}

    cmd = [sys.executable, "eval/run_eval.py", "--case", mr_id,
           "--json", str(jpath), "--dump-reports", str(rpath)]
    if args.profile:
        cmd += ["--profile", args.profile]

    env = dict(os.environ)
    # 單次 LLM 呼叫上限。實測合理上限約 750 秒,預設的 3600 秒等於「卡死就等一小時」。
    # 收到 900 秒會讓病態的呼叫早一點失敗,由下面的重試決定要不要再給一次機會。
    env.setdefault("LLM_TIMEOUT", str(args.llm_timeout))
    if getattr(args, "dump_prompts", False):
        # 每個 (case, run) 一個目錄,diff 的時候才對得起來是哪一次
        ppath = out / "prompts" / f"{mr_id}_run{run_idx}"
        ppath.mkdir(parents=True, exist_ok=True)
        env["SEGCRA_DUMP_PROMPTS"] = str(ppath)

    last = ""
    for attempt in range(1, args.attempts + 1):
        t0 = time.monotonic()
        try:
            with lpath.open("a", encoding="utf-8") as log:
                log.write(f"\n=== attempt {attempt} @ {time.strftime('%H:%M:%S')} ===\n")
                log.flush()
                subprocess.run(cmd, cwd=cwd, env=env, stdout=log,
                               stderr=subprocess.STDOUT, timeout=args.case_timeout)
            took = time.monotonic() - t0
        except subprocess.TimeoutExpired:
            took = time.monotonic() - t0
            last = f"逾時 {args.case_timeout}s(第 {attempt} 次)"
            print(f"    ⏱ {last}")
            continue
        if result_ok(jpath, mr_id):
            return {"status": "ok", "attempts": attempt, "seconds": round(took, 1)}
        last = _why_failed(jpath, mr_id)
        print(f"    ✗ 第 {attempt} 次無有效結果:{last}")
    # 重試用盡 → 記為「環境失敗」,**不是**「測試失敗」。
    # 兩者在報告裡必須分開,否則環境問題會被讀成品質退步。
    return {"status": "env_failure", "attempts": args.attempts, "seconds": 0.0,
            "why": last}


def _why_failed(jpath: Path, mr_id: str) -> str:
    if not jpath.exists():
        return "沒有產出結果檔"
    try:
        d = json.loads(jpath.read_text(encoding="utf-8"))
    except Exception as e:
        return f"結果檔壞掉({type(e).__name__})"
    if d.get("errors"):
        return "; ".join(f"{e.get('case')}: {e.get('why')}" for e in d["errors"])[:200]
    rows = [r for r in d.get("cases", []) if str(r.get("case")) == mr_id]
    if not rows:
        return "結果檔裡沒有這個 case"
    if rows[0].get("error"):
        return str(rows[0]["error"])[:200]
    # 降級來的環境失敗要講明白是環境,不要只說「原因不明」——報告上
    # 「環境失敗」與「測試失敗」分在兩節,講錯了整段就讀錯了
    infra = infra_failure(rows[0])
    return f"(基礎設施失敗,非品質問題){infra}" if infra else "(原因不明)"


# ─────────────────────── 從結果檔抽觀測值 ───────────────────────

def observe(jpath: Path, mr_id: str) -> dict | None:
    """把一次跑的結果壓成可比較的觀測值。

    刻意把「判定」與「數值」分開:
      verdict —— 這次的**結論**(斷言有沒有過、決策落在哪、執行驗證通不通)
      numbers —— 支撐結論的**數字**(驗了幾個案例、缺幾個角、抓到幾條、誤報幾條)

    分開的理由:有的 case 判定每次一樣但數字會晃(那還能當閘門),
    有的判定會翻但數字只差一點(那只能看分布)。混在一起就分不出這兩種。
    """
    if not result_ok(jpath, mr_id):
        return None
    d = json.loads(jpath.read_text(encoding="utf-8"))
    r = next(r for r in d["cases"] if str(r.get("case")) == mr_id)
    se = r.get("spec_exec") or {}
    return {
        "verdict": {"passed": r.get("failed", 0) == 0,
                    "decision": r.get("decision"),
                    "spec_exec_passed": se.get("passed")},
        "numbers": {"se_cases": se.get("cases"), "se_gaps": se.get("coverage_gaps"),
                    "se_dropped": se.get("dropped_cases"),
                    "hit": r.get("hit"), "expected": r.get("expected"),
                    "noise": r.get("noise")},
        "known_gap": bool(r.get("known_gap")),
        "layer": r.get("layer"),
        "failures": [f.get("name") for f in r.get("failures", [])],
    }


def _spread(vals: list) -> int:
    nums = [v for v in vals if isinstance(v, (int, float))]
    return (max(nums) - min(nums)) if nums else 0


def grade(obs: list[dict]) -> dict:
    """依 N 次觀測分級。**只有分級完成後,品質數字才有意義。**"""
    verdicts = [json.dumps(o["verdict"], sort_keys=True) for o in obs]
    numbers = [json.dumps(o["numbers"], sort_keys=True) for o in obs]
    verdict_stable = len(set(verdicts)) == 1
    numbers_stable = len(set(numbers)) == 1
    gap_spread = _spread([o["numbers"]["se_gaps"] for o in obs])
    case_spread = _spread([o["numbers"]["se_cases"] for o in obs])

    if verdict_stable and numbers_stable:
        g = "stable"
    elif gap_spread <= BOUNDARY_GAP_SPREAD and case_spread <= BOUNDARY_CASE_SPREAD:
        g = "boundary"
    else:
        g = "unstable"
    return {
        "grade": g, "runs": len(obs),
        # verdict_stable 要單獨留著:「判定穩定但數值會晃」的 case 仍可當硬閘門,
        # 只是不能拿它的數字去比 delta。合成一個等級會把這件事蓋掉。
        "verdict_stable": verdict_stable, "numbers_stable": numbers_stable,
        "pass_rate": f"{sum(o['verdict']['passed'] for o in obs)}/{len(obs)}",
        "gap_spread": gap_spread, "case_spread": case_spread,
        "se_cases": [o["numbers"]["se_cases"] for o in obs],
        "se_gaps": [o["numbers"]["se_gaps"] for o in obs],
        "decisions": sorted({str(o["verdict"]["decision"]) for o in obs}),
        "known_gap": obs[0]["known_gap"], "layer": obs[0]["layer"],
    }


# ─────────────────────── 彙總與回歸判定 ───────────────────────

def aggregate(cases: dict) -> dict:
    """聚合指標——**比單一案例穩定**,所以硬閘門放這裡才有意義。

    個別案例會晃,紅燈就會變成大家習慣忽略的雜訊;聚合指標的噪音區間小得多,
    它超出區間才值得叫人。
    """
    graded = [c for c in cases.values() if c.get("grade")]
    if not graded:
        return {}
    by_grade = {g: sum(1 for c in graded if c["grade"] == g)
                for g in ("stable", "boundary", "unstable")}
    # 通過率用「每次跑」當分母,不是用「每個 case」——否則跑 5 次的不穩案例
    # 會被算成一票,權重反而比穩定案例低
    passes = sum(int(c["pass_rate"].split("/")[0]) for c in graded)
    total = sum(int(c["pass_rate"].split("/")[1]) for c in graded)
    gaps = [g for c in graded for g in c["se_gaps"] if isinstance(g, int)]
    return {
        "cases": len(graded), "by_grade": by_grade,
        "runs": total, "passes": passes,
        "pass_rate": round(passes / total, 3) if total else None,
        # 可當硬閘門 = 判定每次相同 **而且**不是不穩級。
        # 只看 verdict_stable 不夠:一個數值大幅跳動的 case 也可能「每次都一樣地失敗」,
        # 那個一致性是巧合(或只是它一直壞著),不是可以用來判回歸的穩定性。
        "gate_eligible": sum(1 for c in graded
                             if c["verdict_stable"] and c["grade"] != "unstable"),
        "coverage_gaps_median": statistics.median(gaps) if gaps else None,
        "coverage_gaps_total": sum(gaps) if gaps else None,
    }


def judge(new: dict, old: dict) -> list[dict]:
    """回歸判定。規則明確到可以直接寫進 CI 說明,理由見 BASELINE.md。

    最重要的一條:**翻盤不等於回歸。** 對一個**量過、而且量到會晃**的 case,
    單次翻盤是噪音的正常表現,拿它當紅燈會讓所有人學會忽略紅燈。
    所以邊界案例單次翻盤只補跑、不判定;硬閘門只放在量過、而且量到穩定的 case 上。
    """
    rows = []
    for mr_id, n in sorted(new.get("cases", {}).items()):
        o = (old.get("cases") or {}).get(mr_id)
        if not o:
            rows.append({"case": mr_id, "verdict": "⚪", "why": "舊 baseline 沒有這個 case"})
            continue
        np_, nt = (int(x) for x in n["pass_rate"].split("/"))
        op, ot = (int(x) for x in o["pass_rate"].split("/"))
        was, now = op / ot, np_ / nt
        base_grade = o["grade"]
        if base_grade == "stable":
            if now < 1.0 and was == 1.0:
                rows.append({"case": mr_id, "verdict": "🔴",
                             "why": f"穩定案例從 {o['pass_rate']} 變 {n['pass_rate']}"})
            else:
                rows.append({"case": mr_id, "verdict": "✓", "why": "穩定案例維持"})
        elif base_grade == "boundary":
            if nt < RUNS_BY_GRADE["boundary"]:
                rows.append({"case": mr_id, "verdict": "⚪",
                             "why": f"邊界案例只跑了 {nt} 次,補到 "
                                    f"{RUNS_BY_GRADE['boundary']} 次再看分布"})
            elif was > 0 and now == 0:
                rows.append({"case": mr_id, "verdict": "🟡",
                             "why": f"邊界案例通過率 {o['pass_rate']} → {n['pass_rate']},"
                                    "明顯下降,需人工判讀"})
            else:
                rows.append({"case": mr_id, "verdict": "⚪",
                             "why": f"邊界案例分布 {o['pass_rate']} → {n['pass_rate']}"})
        else:
            rows.append({"case": mr_id, "verdict": "⚪",
                         "why": "不穩案例,只記錄不判定(待修測資生成)"})

    na, oa = new.get("aggregate") or {}, old.get("aggregate") or {}
    if na.get("pass_rate") is not None and oa.get("pass_rate") is not None:
        # 噪音區間:用舊 baseline 每個 case 的通過率離散度當尺,而不是憑感覺訂門檻。
        # 沒有量過噪音就訂門檻,等於在猜。
        drop = oa["pass_rate"] - na["pass_rate"]
        band = _noise_band(old)
        rows.append({"case": "(聚合)通過率", "verdict": "🟡" if drop > band else "✓",
                     "why": f"{oa['pass_rate']} → {na['pass_rate']}"
                            f"(噪音區間 ±{band:.3f})"})
    return rows


def _noise_band(old: dict) -> float:
    """噪音區間 = 舊 baseline 裡「同一份程式碼跑多次」的通過率變異。

    只有跑過 2 次以上的 case 才提供資訊(跑 1 次的沒有變異可言)。
    量不到就退回 0.05——但那是猜的,報告裡要標明。
    """
    rates = []
    for c in (old.get("cases") or {}).values():
        p, t = (int(x) for x in c["pass_rate"].split("/"))
        if t > 1:
            rates.append(p / t)
    if len(rates) < 2:
        return 0.05
    return max(0.02, round(statistics.pstdev(rates), 3))


# ─────────────────────── 報告 ───────────────────────

GRADE_LABEL = {"stable": "穩定", "boundary": "邊界", "unstable": "不穩"}
GRADE_USE = {
    "stable": "可當硬閘門:翻盤即視為回歸",
    "boundary": "只看分布位移,**不當硬閘門**",
    "unstable": "只當觀測指標;先修測資生成,修好前不列入評分",
}


def write_report(out: Path, summary: dict, rulings: list[dict] | None) -> None:
    fp = summary.get("fingerprint") or {}
    agg = summary.get("aggregate") or {}
    L = [f"# baseline 報告 — {out.name}", ""]
    L += ["> 方法與判定規則見 [BASELINE.md](../../BASELINE.md)。",
          "> **指紋不同的兩份 baseline 一律不准互比**,指紋見 `fingerprint.yaml`。", ""]

    L += ["## 環境", "", "| 項目 | 值 |", "|---|---|"]
    code = fp.get("code") or {}
    params = fp.get("params") or {}
    def _v(x):
        """指紋缺項時印「未記錄」而不是 None——None 看起來像量到了一個空值。"""
        return "未記錄" if x in (None, "", {}) else x

    L += [f"| 程式碼 | {code.get('branch')}@{code.get('sha')}"
          f"{'(**有未 commit 的改動**)' if code.get('dirty') else ''} |",
          # 記端點的雜湊而非位址:REPORT.md 會進公開 repo,而「還是不是同一個端點」
          # 是這一列唯一需要回答的問題,雜湊就夠了
          f"| 模型端點 | {_v((fp.get('endpoint') or {}).get('id'))} "
          f"(API 版本 {_v((fp.get('endpoint') or {}).get('api_version'))}) |",
          f"| 模型 digest | {_v((fp.get('model') or {}).get('digest'))} |",
          f"| seed 實測是否生效 | **{_v(params.get('seed_effective'))}** |",
          f"| context 實測 | {_v(params.get('num_ctx_effective'))} |",
          f"| 沙盒 | {_v((fp.get('sandbox') or {}).get('engine'))} |",
          f"| 跑批耗時 | {_v(summary.get('wall_hours'))} 小時 |", ""]

    # 「沒有指紋」和「量到 seed 無效」是兩件不同的事,不能用同一句話帶過:
    # 前者代表這份 baseline 連環境都沒記錄(更嚴重,完全不可當基準),
    # 後者代表環境記錄完整、而且已知不可重現(可用,只是數字要當抽樣讀)。
    seed_eff = params.get("seed_effective")
    if not fp:
        L += ["> ⛔ **這份 baseline 沒有環境指紋**(跑批時略過了起飛前檢查),",
              "> 無法確認它跑在什麼環境上 → **不可當回歸基準**,只能當一次性觀測。", ""]
    elif seed_eff is False:
        L += ["> `seed` 實測未生效 → **連單次模型呼叫都不可重現**。",
              "> 底下每個數字都要當成「一次抽樣」,不是「這個系統的值」。", ""]
    elif seed_eff is not True:
        # 「沒量到」與「量到無效」是兩件事,不能用同一句話帶過——把前者講成後者,
        # 就是在報告裡放一個我們其實沒有的結論。這正是上一輪 seed 誤判的成因。
        L += [f"> ⚠ **`seed` 這一項沒有量到**(指紋記的是 `{seed_eff}`)。",
              "> 這**不等於** seed 無效,只代表這輪沒有跑模型探針——",
              "> 要嘛是 `--skip-preflight`、要嘛是 `--offline`/`--quick` 模式的指紋。",
              "> 在補量之前,底下的數字只能當一次性觀測,不要當回歸基準。", ""]
    else:
        # seed 生效不等於這份 baseline 可重現,別讓讀的人自己接錯這一步:
        # 可重現的是單次呼叫,不是整條多輪 agent loop 的管線。
        L += ["> `seed` 實測生效 → **單次模型呼叫**是可重現的。",
              "> 但**整條管線不是**:它是多輪 agent loop,後一輪送出去的 prompt 含有",
              "> 前一輪的工具結果,那裡面有會變的東西。所以底下的數字仍然要當成抽樣讀",
              "> (詳見 `eval/BASELINE.md` §1)。", ""]

    L += ["## 分級表", "",
          "這張表本身就是交付物:它回答「這套測試能信到什麼程度」,",
          "也把**哪幾個 case 的綠燈是假的**講明白。", "",
          "| case | 層 | 分級 | 通過 | 執行驗證案例數 | 覆蓋缺口 | 決策 | 怎麼用 |",
          "|---|---|---|---|---|---|---|---|"]
    for mr_id, c in sorted((summary.get("cases") or {}).items()):
        note = GRADE_USE[c["grade"]]
        if c["grade"] != "stable" and c["verdict_stable"]:
            note = "判定每次相同、數值會晃 → 判定可當閘門,數字不可比 delta"
        L.append(f"| mr_{mr_id} | {c['layer']} | {GRADE_LABEL[c['grade']]}"
                 f"{'(已知缺口)' if c['known_gap'] else ''} | {c['pass_rate']} | "
                 f"{c['se_cases']} | {c['se_gaps']} | {'/'.join(c['decisions'])} | {note} |")
    L.append("")

    if summary.get("env_failures"):
        L += ["## 環境失敗(**不是**測試失敗)", "",
              "重試用盡仍拿不到有效結果的 case。列在這裡而不是算進通過率——",
              "把環境問題算成品質退步,是上一輪最大的誤判來源。", ""]
        for mr_id, why in sorted(summary["env_failures"].items()):
            L.append(f"- mr_{mr_id}:{why}")
        L.append("")

    if agg:
        L += ["## 聚合指標", "",
              "硬閘門放在「穩定案例」與「聚合指標」兩處——聚合指標的噪音區間小得多,",
              "它超出區間才值得叫人;把紅燈綁在會晃的個別案例上,紅燈就會被習慣性忽略。",
              "",
              "| 指標 | 值 |", "|---|---|",
              f"| 納入分級的 case | {agg['cases']} |",
              f"| 分級分布 | 穩定 {agg['by_grade']['stable']} / "
              f"邊界 {agg['by_grade']['boundary']} / 不穩 {agg['by_grade']['unstable']} |",
              f"| 可當硬閘門的 case | {agg['gate_eligible']} |",
              f"| 總跑次 | {agg['runs']} |",
              f"| 通過率(以跑次為分母) | {agg['pass_rate']} |",
              f"| 覆蓋缺口中位數 | {agg['coverage_gaps_median']} |", ""]

    if rulings:
        L += ["## 與上一份的比對", "",
              f"對照:`{summary.get('compared_to')}`", "",
              "| case | 判定 | 說明 |", "|---|---|---|"]
        for r in rulings:
            L.append(f"| {r['case']} | {r['verdict']} | {r['why']} |")
        L += ["", "判定符號:🔴 回歸(擋下) / 🟡 疑似回歸(人工判讀) / "
              "⚪ 不判定 / ⚫ 不可比 / ✓ 維持", ""]

    (out / "REPORT.md").write_text("\n".join(L), encoding="utf-8")


# ─────────────────────── 主流程 ───────────────────────

def summarize(out: Path, args, meta: dict) -> dict:
    """從落地的結果檔重建彙總——**不重跑任何 case**。

    彙總是純函式,所以改了分級門檻想重看一次,用 `--summarize-only` 幾毫秒就好。
    這和 `run_eval.py --from-reports` 是同一個原則:把「慢且會飄的收集」與
    「快且確定的判讀」徹底分開。
    """
    cases: dict[str, dict] = {}
    for mr_id in case_ids(args.case):
        obs = []
        for run_idx in range(1, args.runs + 1):
            o = observe(out / "cases" / f"mr_{mr_id}.run{run_idx}.json", mr_id)
            if o:
                obs.append(o)
        if obs:
            cases[mr_id] = grade(obs)

    fp_path = out / "fingerprint.yaml"
    fingerprint = {}
    if fp_path.exists():
        try:
            import yaml
            fingerprint = yaml.safe_load(fp_path.read_text(encoding="utf-8")) or {}
        except Exception:
            fingerprint = {}

    summary = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "dir": out.name, "fingerprint": fingerprint,
               "cases": cases, "aggregate": aggregate(cases), **meta}
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def plan_runs(args) -> dict[str, int]:
    """決定每個 case 要跑幾次。

    階段一:全部跑 `--runs` 次(要量的就是變異本身,不能自適應)。
    階段二:依上一份的分級自適應——沒有分級記錄的 case 一律當「不穩」處理,
    寧可多跑,也不要拿一個沒量過噪音的 case 去當閘門。
    """
    ids = case_ids(args.case)
    if args.stage1 or not args.classify_from:
        return {i: args.runs for i in ids}
    prev = json.loads((Path(args.classify_from) / "summary.json")
                      .read_text(encoding="utf-8"))
    graded = prev.get("cases") or {}
    return {i: RUNS_BY_GRADE.get((graded.get(i) or {}).get("grade"),
                                 RUNS_BY_GRADE["unstable"]) for i in ids}


def main(args) -> int:
    if args.summarize_only:
        out = Path(args.summarize_only)
        # 重新彙總時 runs 要涵蓋目錄裡實際存在的最大 run 編號,否則會漏讀
        found = [int(p.stem.rsplit("run", 1)[-1])
                 for p in (out / "cases").glob("mr_*.run*.json")]
        args.runs = max(found or [1])
        summary = summarize(out, args, {"mode": "summarize-only",
                                        "compared_to": args.compare})
        # --compare 在這裡也要生效。回歸判定只讀兩份 summary.json,不需要模型;
        # 若只在「重跑」那條路支援,等於要花十幾小時才能重新判一次回歸,
        # 而這套工具存在的理由正是不要那樣。
        rulings = None
        if args.compare:
            old = json.loads((Path(args.compare) / "summary.json").read_text(encoding="utf-8"))
            rulings = judge(summary, old)
        write_report(out, summary, rulings)
        print(f"已重新彙總 {out}(未重跑任何 case)")
        if rulings and any(r["verdict"] == "🔴" for r in rulings):
            print("🔴 有穩定案例翻盤 → 判定為回歸。")
            return 1
        return 0

    per_case = plan_runs(args)
    if not per_case:
        print(f"golden set 為空或篩選後無 case(目錄:{GOLDEN_DIR})")
        return 2

    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PKG_ROOT,
                         capture_output=True, text=True).stdout.strip() or "nogit"
    name = args.out or f"{time.strftime('%Y-%m-%d')}-{sha}" + (f"-{args.tag}" if args.tag else "")
    out = Path(name) if Path(name).is_absolute() else BASELINES / name
    out.mkdir(parents=True, exist_ok=True)

    # ① 起飛前檢查:任一項 fail 就不要開跑。這是整套方法裡性價比最高的一步——
    #    上一輪在環境失敗上白燒了 4-6 小時,全部都是兩分鐘內驗得出來的問題。
    fp_out = out / "fingerprint.yaml"
    if not args.skip_preflight:
        cmd = [sys.executable, "eval/preflight.py", "--out", str(fp_out)]
        if args.compare:
            prev_fp = Path(args.compare) / "fingerprint.yaml"
            if prev_fp.exists():
                cmd += ["--compare", str(prev_fp)]
        print("── 起飛前檢查 ──")
        rc = subprocess.run(cmd, cwd=PKG_ROOT).returncode
        if rc != 0:
            print("\n⛔ 起飛前檢查沒過,跑批中止。"
                  "(確定要照跑請加 --skip-preflight,但那輪的數字不可當基準)")
            return 2
    else:
        print("⚠ 已略過起飛前檢查 —— 這輪沒有環境指紋,**不可當回歸基準**")

    total = sum(per_case.values())
    print(f"\n── 跑批:{len(per_case)} 個 case、共 {total} 次 "
          f"({'階段一:量噪音' if args.stage1 else '階段二:自適應取樣'})──")
    t0 = time.monotonic()
    env_failures, timings = {}, {}
    done = 0
    for mr_id, n in per_case.items():
        for run_idx in range(1, n + 1):
            done += 1
            print(f"[{done}/{total}] mr_{mr_id} run{run_idx}", flush=True)
            r = run_one(mr_id, run_idx, out, args, cwd=PKG_ROOT)
            timings.setdefault(mr_id, []).append(r["seconds"])
            if r["status"] == "env_failure":
                env_failures[mr_id] = r.get("why", "")
            elif r["status"] == "skipped":
                print("    ↷ 已有有效結果,跳過(--resume)")
            else:
                print(f"    ✓ {r['seconds']}s(第 {r['attempts']} 次成功)")
            if args.partner:
                _run_partner(mr_id, run_idx, out, args)

    wall = round((time.monotonic() - t0) / 3600, 2)
    meta = {"mode": "stage1" if args.stage1 else "stage2",
            "runs_planned": per_case, "wall_hours": wall,
            "env_failures": env_failures, "timings": timings,
            "compared_to": args.compare, "profile": args.profile}
    summary = summarize(out, args, meta)

    rulings = None
    if args.compare:
        old = json.loads((Path(args.compare) / "summary.json").read_text(encoding="utf-8"))
        rulings = judge(summary, old)
    write_report(out, summary, rulings)

    print(f"\n── 完成:{wall} 小時 ──")
    agg = summary.get("aggregate") or {}
    if agg:
        print(f"分級:穩定 {agg['by_grade']['stable']} / 邊界 "
              f"{agg['by_grade']['boundary']} / 不穩 {agg['by_grade']['unstable']}"
              f";通過率 {agg['pass_rate']}(以 {agg['runs']} 次跑為分母)")
    if env_failures:
        print(f"⚠ {len(env_failures)} 個 case 是**環境失敗**(未計入通過率):"
              f"{', '.join('mr_' + k for k in env_failures)}")
    print(f"報告:{out / 'REPORT.md'}")

    if rulings and any(r["verdict"] == "🔴" for r in rulings):
        print("\n🔴 有穩定案例翻盤 → 判定為回歸。")
        return 1
    return 0


def _run_partner(mr_id: str, run_idx: int, out: Path, args) -> None:
    """配對交錯:同一個 case 在另一份工作樹上緊接著再跑一次。

    用途是評估**某一項改動**(例如「有 blocker 時跳過 spec_exec」這種會改變輸出的
    加速手段)。為什麼要交錯而不是先跑完 A 再跑完 B:上游的推論速度與負載會隨時間
    飄(實測同一個呼叫 53s → 681s),先後跑會讓「時間」變成第二個變因,
    分不出差異是改動造成的還是環境漂移造成的。交錯之後,漂移對兩邊的影響大致相同。
    """
    pout = out / "partner"
    jpath = pout / "cases" / f"mr_{mr_id}.run{run_idx}.json"
    rpath = pout / "reports" / f"run{run_idx}"
    lpath = pout / "logs" / f"mr_{mr_id}.run{run_idx}.log"
    for p in (jpath.parent, rpath, lpath.parent):
        p.mkdir(parents=True, exist_ok=True)
    if args.resume and result_ok(jpath, mr_id):
        return
    cmd = [sys.executable, "eval/run_eval.py", "--case", mr_id,
           "--json", str(jpath), "--dump-reports", str(rpath)]
    if args.profile:
        cmd += ["--profile", args.profile]
    env = dict(os.environ)
    env.setdefault("LLM_TIMEOUT", str(args.llm_timeout))
    print(f"    ⇄ partner mr_{mr_id} run{run_idx}", flush=True)
    try:
        with lpath.open("a", encoding="utf-8") as log:
            subprocess.run(cmd, cwd=Path(args.partner), env=env, stdout=log,
                           stderr=subprocess.STDOUT, timeout=args.case_timeout)
    except subprocess.TimeoutExpired:
        print(f"    ⏱ partner 逾時 {args.case_timeout}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="baseline 跑批與彙總(方法見 eval/BASELINE.md)")
    ap.add_argument("--stage1", action="store_true",
                    help="階段一:量噪音。所有 case 都跑 --runs 次並產出分級表")
    ap.add_argument("--runs", type=int, default=5,
                    help="階段一每個 case 跑幾次(預設 5)")
    ap.add_argument("--classify-from", default=None, metavar="DIR",
                    help="階段二:讀這份 baseline 的分級,自適應決定每個 case 跑幾次")
    ap.add_argument("--compare", default=None, metavar="DIR",
                    help="與這份 baseline 比對(指紋不同會直接判不可比)")
    ap.add_argument("--summarize-only", default=None, metavar="DIR",
                    help="只重新彙總既有結果,不跑任何 case")
    ap.add_argument("--case", default=None,
                help="只跑指定 case,逗號分隔;`406` 與 `mr_406` 都收")
    ap.add_argument("--profile", default=None, help="模型 profile")
    ap.add_argument("--tag", default=None, help="目錄名後綴(如 noise / after-speedup)")
    ap.add_argument("--out", default=None, help="自訂輸出目錄名或絕對路徑")
    ap.add_argument("--partner", default=None, metavar="PATH",
                    help="配對交錯:另一份工作樹,每個 case 兩邊交錯跑(見 BASELINE.md)")
    ap.add_argument("--attempts", type=int, default=2,
                    help="單一 case 最多嘗試幾次(預設 2;用盡記為環境失敗)")
    ap.add_argument("--case-timeout", type=float, default=2400,
                    help="單一 case 的牆鐘上限秒數(預設 2400)")
    ap.add_argument("--llm-timeout", type=float, default=900,
                    help="傳給子行程的 LLM_TIMEOUT(預設 900;實測合理上限約 750s)")
    ap.add_argument("--no-resume", dest="resume", action="store_false",
                    help="不續跑,已有的有效結果也重跑")
    ap.add_argument("--dump-prompts", action="store_true",
                    help="把每一次實際送出的 prompt 存進 prompts/<case>_run<n>/。"
                         "用來分辨「跑兩次不一樣」是取樣還是輸入差異(見 BASELINE.md §7)。"
                         "預設關閉:prompt 原文很大,而且含受審的 SQL")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="略過起飛前檢查(那輪不可當回歸基準)")
    sys.exit(main(ap.parse_args()))
