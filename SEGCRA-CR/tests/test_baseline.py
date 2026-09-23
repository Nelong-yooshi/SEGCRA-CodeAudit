"""baseline 方法的判定邏輯 — `eval/run_baseline.py` 與 `eval/preflight.py`,不呼叫 LLM。

**為什麼這一層一定要有測試**:baseline 是「判斷改動有沒有讓結果變糟」的依據,
而這些函式就是下那個判斷的人。它們錯了,後果不是少一條警告,是**整套回歸判定失效
而且沒有任何跡象**——分級全錯、紅燈不亮、或紅燈亂亮到大家學會忽略它。

這和 `test_eval_scoring.py` 是同一個道理(尺自己要先準),只是層次更上面一層:
那邊測「單輪的分數算得對不對」,這邊測「多輪之間的差異判讀得對不對」。

而且這些判定規則全部是純函式:給幾份結果檔就能驗,秒級、不用 GPU、不用沙盒。
方法書見 `eval/BASELINE.md`。

驗的東西:
  1. `result_ok`:**檔案存在不等於跑成功**(上一輪 4 個連線錯誤被計成成功的坑)
  2. `grade`:穩定 / 邊界 / 不穩三級的判準,含「判定穩定但數值會晃」這種混合情況
  3. `judge`:回歸判定表的每一列——尤其**邊界案例單次翻盤不得判成回歸**
  4. `_noise_band`:噪音區間來自量測,而不是寫死的門檻
  5. `plan_runs`:自適應取樣,含「沒有分級記錄就當不穩」的保守退路
  6. `compare_fingerprint`:哪些環境差異讓兩份 baseline 不可比,而 **code.sha 不算**
"""
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import preflight  # noqa: E402
import run_baseline as rb  # noqa: E402


# ─────────────────────── 測試用的結果檔 ───────────────────────

def _row(mr, *, passed=True, decision="needs_human", se_passed=True,
         se_cases=12, se_gaps=0, hit=1, expected=1, noise=0, known_gap=False):
    return {"case": mr, "layer": "spec_exec", "known_gap": known_gap,
            "expected": expected, "hit": hit, "noise": noise, "decision": decision,
            "spec_exec": {"passed": se_passed, "spec_code": "R-401", "conditions": 3,
                          "cases": se_cases, "coverage_gaps": se_gaps,
                          "dropped_cases": 0, "gap_detail": []},
            "checks": 4, "failed": 0 if passed else 1,
            "failures": [] if passed else [{"name": "spec_exec_passed", "why": "翻盤"}]}


def _write(base: Path, mr: str, run: int, row: dict | None, *, errors=None,
           raw: str | None = None):
    p = base / "cases" / f"mr_{mr}.run{run}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        p.write_text(raw, encoding="utf-8")
        return p
    p.write_text(json.dumps({"cases": [row] if row else [], "errors": errors or []},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _args(case: str, runs: int):
    return types.SimpleNamespace(case=case, runs=runs)


# ─────────────────────── 一、結果有效性 ───────────────────────

def test_檔案存在不等於跑成功(tmp_path):
    """上一輪最貴的一個 bug:`run_percase.sh` 只檢查檔案存不存在,

    4 個「連不到本機 Ollama:timed out」的錯誤紀錄被計成成功,
    32 個裡宣稱 30 個成功、實際只有 26 個(FINDINGS「發現十一」)。
    在一個會跑十幾小時的流程裡,這種錯誤會一路傳到最終報告都沒人發現。
    """
    err = _write(tmp_path, "405", 1,
                 {"case": "405", "layer": "spec_exec",
                  "error": "APIConnectionError: 連不到本機 Ollama:timed out"},
                 errors=[{"case": "405", "why": "APIConnectionError"}])
    assert err.exists(), "前提:檔案是真的存在的"
    assert rb.result_ok(err, "405") is False


def test_結果檔的各種壞法都要判無效(tmp_path):
    """壞法不只一種,而每一種在「只看存不存在」的世界裡都會被當成成功。"""
    assert rb.result_ok(tmp_path / "不存在.json", "401") is False

    empty = tmp_path / "cases" / "mr_401.run1.json"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("", encoding="utf-8")
    assert rb.result_ok(empty, "401") is False, "空檔(跑批被中斷)"

    broken = _write(tmp_path, "402", 1, None, raw='{"cases": [{"case"')
    assert rb.result_ok(broken, "402") is False, "截斷的 JSON(行程被 kill)"

    other = _write(tmp_path, "403", 1, _row("999"))
    assert rb.result_ok(other, "403") is False, "檔名與內容的 case 對不上"

    good = _write(tmp_path, "404", 1, _row("404"))
    assert rb.result_ok(good, "404") is True


def test_有效結果才會被納入觀測(tmp_path):
    """無效的那次不能當成「一次觀測」——否則 N=5 實際只有 3 次有效時,

    分級會拿 3 次的資料當成 5 次的信心水準用。
    """
    _write(tmp_path, "406", 1, _row("406"))
    _write(tmp_path, "406", 2, {"case": "406", "error": "timeout"},
           errors=[{"case": "406", "why": "timeout"}])
    assert rb.observe(tmp_path / "cases" / "mr_406.run1.json", "406") is not None
    assert rb.observe(tmp_path / "cases" / "mr_406.run2.json", "406") is None


# ─────────────────────── 二、分級 ───────────────────────

def test_每次都一樣才是穩定(tmp_path):
    for k in range(1, 6):
        _write(tmp_path, "601", k, _row("601", se_cases=15, se_gaps=0))
    g = rb.grade([rb.observe(tmp_path / "cases" / f"mr_601.run{k}.json", "601")
                  for k in range(1, 6)])
    assert g["grade"] == "stable"
    assert g["verdict_stable"] and g["numbers_stable"]
    assert g["pass_rate"] == "5/5"


def test_判定會翻但數值只差一點是邊界(tmp_path):
    """`mr_408` 這一類:測資生成的覆蓋度本來就會抖,缺口 0↔2 之間跳。

    這種案例**不能當硬閘門**——把紅燈綁在它身上,紅燈就會變成習慣忽略的雜訊。
    """
    shape = [(True, 11, 0), (False, 11, 1), (True, 12, 0), (False, 11, 2), (True, 12, 1)]
    for k, (p, c, gp) in enumerate(shape, 1):
        _write(tmp_path, "408", k, _row("408", passed=p, se_passed=p,
                                        se_cases=c, se_gaps=gp))
    g = rb.grade([rb.observe(tmp_path / "cases" / f"mr_408.run{k}.json", "408")
                  for k in range(1, 6)])
    assert g["grade"] == "boundary"
    assert g["verdict_stable"] is False
    assert g["gap_spread"] <= rb.BOUNDARY_GAP_SPREAD


def test_數值大幅跳動是不穩(tmp_path):
    """`mr_406` 的真實形狀:18 案例/0 缺口 ↔ 11 案例/5 缺口。

    這是我們**實際觀測到**的翻盤(跨 6 小時),也是目前最大的噪音來源。
    """
    shape = [(True, 18, 0), (False, 11, 5), (False, 11, 5), (True, 16, 2), (False, 11, 5)]
    for k, (p, c, gp) in enumerate(shape, 1):
        _write(tmp_path, "406", k, _row("406", passed=p, se_passed=p,
                                        se_cases=c, se_gaps=gp))
    g = rb.grade([rb.observe(tmp_path / "cases" / f"mr_406.run{k}.json", "406")
                  for k in range(1, 6)])
    assert g["grade"] == "unstable"
    assert g["pass_rate"] == "2/5"


def test_判定穩定但數值會晃要分得出來(tmp_path):
    """這種案例仍可當閘門(判定每次一樣),但**它的數字不能拿去比 delta**。

    兩軸合成單一等級會把這個區別蓋掉,所以 `verdict_stable` 必須單獨留著。
    """
    for k, c in enumerate([12, 14, 11, 13, 12], 1):
        _write(tmp_path, "601", k, _row("601", se_cases=c, se_gaps=0))
    g = rb.grade([rb.observe(tmp_path / "cases" / f"mr_601.run{k}.json", "601")
                  for k in range(1, 6)])
    assert g["verdict_stable"] is True, "判定每次相同"
    assert g["numbers_stable"] is False, "但數值會晃"
    assert g["grade"] != "stable", "所以不算全然穩定"


def test_決策不同也算判定不一致(tmp_path):
    """斷言可能都通過,但決策從 needs_human 變 blocked——那是**產品行為**變了,

    使用者感受完全不同。只看 pass/fail 會漏掉這種差異。
    """
    for k, dec in enumerate(["needs_human", "blocked", "needs_human"], 1):
        _write(tmp_path, "101", k, _row("101", decision=dec))
    g = rb.grade([rb.observe(tmp_path / "cases" / f"mr_101.run{k}.json", "101")
                  for k in range(1, 4)])
    assert g["verdict_stable"] is False
    assert g["decisions"] == ["blocked", "needs_human"]


# ─────────────────────── 三、聚合 ───────────────────────

def test_通過率以跑次為分母(tmp_path):
    """用「case 數」當分母的話,跑 5 次的不穩案例只算一票,權重反而比穩定案例低。

    那會讓聚合指標對噪音**更**敏感,和我們用它當閘門的理由正好相反。
    """
    for k in range(1, 6):                                   # 穩定,5 次全過
        _write(tmp_path, "601", k, _row("601"))
    for k in range(1, 6):                                   # 不穩,5 次全不過
        _write(tmp_path, "406", k, _row("406", passed=False, se_passed=False,
                                        se_cases=11 + k, se_gaps=5))
    summary = rb.summarize(tmp_path, _args("601,406", 5), {})
    agg = summary["aggregate"]
    assert agg["runs"] == 10 and agg["passes"] == 5
    assert agg["pass_rate"] == 0.5
    assert agg["gate_eligible"] == 1, "只有穩定那個能當硬閘門"


# ─────────────────────── 四、回歸判定 ───────────────────────

def _summary(tmp_path: Path, name: str, spec: dict) -> dict:
    """spec: {case_id: [(passed, se_cases, se_gaps), ...]}"""
    d = tmp_path / name
    for mr, runs in spec.items():
        for k, (p, c, g) in enumerate(runs, 1):
            _write(d, mr, k, _row(mr, passed=p, se_passed=p, se_cases=c, se_gaps=g))
    n = max(len(v) for v in spec.values())
    return rb.summarize(d, _args(",".join(spec), n), {})


def test_穩定案例翻盤判回歸(tmp_path):
    old = _summary(tmp_path, "old", {"601": [(True, 15, 0)] * 5})
    new = _summary(tmp_path, "new", {"601": [(False, 9, 3)]})
    rulings = {r["case"]: r for r in rb.judge(new, old)}
    assert rulings["601"]["verdict"] == "🔴"


def test_邊界案例單次翻盤不得判回歸(tmp_path):
    """**這是整張判定表最重要的一條。**

    在一個不可重現的系統上,單次翻盤是噪音的正常表現。拿它當紅燈,
    紅燈就會天天亮,然後所有人都學會忽略紅燈——那比沒有紅燈更糟。
    """
    old = _summary(tmp_path, "old", {"408": [(True, 11, 0), (False, 11, 1),
                                             (True, 12, 0), (False, 11, 2),
                                             (True, 12, 1)]})
    new = _summary(tmp_path, "new", {"408": [(False, 11, 2)]})
    rulings = {r["case"]: r for r in rb.judge(new, old)}
    assert rulings["408"]["verdict"] == "⚪"
    assert "補到" in rulings["408"]["why"], "要告訴人該怎麼辦,不是只說不判定"


def test_邊界案例通過率歸零判疑似回歸(tmp_path):
    """分布位移才是訊號。3/5 → 0/3 不是單點翻盤,是整條分布掉下去。"""
    old = _summary(tmp_path, "old", {"408": [(True, 11, 0), (False, 11, 1),
                                             (True, 12, 0), (False, 11, 2),
                                             (True, 12, 1)]})
    new = _summary(tmp_path, "new", {"408": [(False, 11, 3)] * 3})
    rulings = {r["case"]: r for r in rb.judge(new, old)}
    assert rulings["408"]["verdict"] == "🟡"


def test_不穩案例任何變化都只記錄(tmp_path):
    """它現在量不出品質,所以拿它判回歸只會製造假訊號。

    正確的處理是修測資生成(那才是收益最大的一件事),不是調統計手段。
    """
    old = _summary(tmp_path, "old", {"406": [(True, 18, 0), (False, 11, 5),
                                             (False, 11, 5), (True, 16, 2),
                                             (False, 11, 5)]})
    new = _summary(tmp_path, "new", {"406": [(False, 11, 5)] * 5})
    rulings = {r["case"]: r for r in rb.judge(new, old)}
    assert rulings["406"]["verdict"] == "⚪"
    assert "不判定" in rulings["406"]["why"]


def test_新增的_case_不判回歸(tmp_path):
    """舊 baseline 沒有的 case 沒有可比對象。判成回歸會讓「補 case」變成一件

    會被紅燈懲罰的事,那是反效果。
    """
    old = _summary(tmp_path, "old", {"601": [(True, 15, 0)] * 3})
    new = _summary(tmp_path, "new", {"601": [(True, 15, 0)], "412": [(False, 8, 1)]})
    rulings = {r["case"]: r for r in rb.judge(new, old)}
    assert rulings["412"]["verdict"] == "⚪"


def test_噪音區間來自量測而不是寫死(tmp_path):
    """門檻是量出來的:舊 baseline 裡跑過 2 次以上的 case 的通過率標準差。

    沒有量過噪音就訂門檻等於在猜,而猜錯的方向通常是「太敏感」→ 紅燈變雜訊。
    """
    wobbly = _summary(tmp_path, "wobbly", {
        "601": [(True, 15, 0)] * 4,                       # 4/4
        "408": [(True, 11, 0), (False, 11, 1)],           # 1/2
        "406": [(False, 11, 5)] * 4,                      # 0/4
    })
    steady = _summary(tmp_path, "steady", {
        "601": [(True, 15, 0)] * 4,
        "408": [(True, 11, 0)] * 2,
        "406": [(True, 11, 0)] * 4,
    })
    assert rb._noise_band(wobbly) > rb._noise_band(steady), (
        "本來就晃得厲害的 baseline,噪音區間要更寬,否則每輪都會叫")


def test_量不到噪音時要退回保守值(tmp_path):
    """階段二(每個穩定 case 只跑 1 次)的結果沒有變異可言,

    拿它當噪音來源會得到「區間 = 0」,然後任何變動都變成回歸。
    """
    single = _summary(tmp_path, "single", {"601": [(True, 15, 0)]})
    assert rb._noise_band(single) == 0.05


# ─────────────────────── 五、自適應取樣 ───────────────────────

def test_自適應取樣依分級決定次數(tmp_path, monkeypatch):
    prev = tmp_path / "noise"
    prev.mkdir(parents=True, exist_ok=True)
    (prev / "summary.json").write_text(json.dumps({"cases": {
        "601": {"grade": "stable", "pass_rate": "5/5"},
        "408": {"grade": "boundary", "pass_rate": "3/5"},
        "406": {"grade": "unstable", "pass_rate": "2/5"},
    }}, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(rb, "case_ids", lambda only: ["601", "408", "406", "412"])
    plan = rb.plan_runs(types.SimpleNamespace(
        stage1=False, classify_from=str(prev), runs=5, case=None))
    assert plan == {"601": 1, "408": 3, "406": 5,
                    # 沒有分級記錄的一律當不穩:寧可多跑,也不要拿一個沒量過噪音的
                    # case 去當閘門
                    "412": 5}


def test_階段一不自適應(monkeypatch):
    """階段一要量的就是變異本身,自適應會讓「穩定」變成自我實現的預言

    (只跑一次當然看不到變異)。
    """
    monkeypatch.setattr(rb, "case_ids", lambda only: ["601", "406"])
    plan = rb.plan_runs(types.SimpleNamespace(
        stage1=True, classify_from="ignored", runs=5, case=None))
    assert plan == {"601": 5, "406": 5}


# ─────────────────────── 六、環境指紋 ───────────────────────

def _fp(**over):
    base = {
        "code": {"sha": "abc1234", "dirty": False},
        "deps": {"sqlglot": "30.18.0", "jinja2": "3.1.6"},
        "endpoint": {"id": "0b1d2c3e4f5a", "api_version": "0.14.2"},
        "model": {"digest": {"gemma4:31b": "deadbeef"}},
        "params": {"seed_effective": False, "num_ctx_effective": ">= probe"},
        "sandbox": {"engine": "SQL Server 16.0.4215.2 (Developer Edition)"},
    }
    for k, v in over.items():
        base[k] = {**base[k], **v}
    return base


def test_相同環境可比():
    assert preflight.compare_fingerprint(_fp(), _fp()) == []


def test_程式碼改變不影響可比性():
    """**這是刻意的。** 程式碼改變正是回歸比較要量的東西;

    把 code.sha 列進不可比條件,等於永遠不能比,baseline 就失去全部用途。
    """
    assert preflight.compare_fingerprint(
        _fp(code={"sha": "9999999"}), _fp()) == []


@pytest.mark.parametrize("section,key,value", [
    ("model", "digest", {"gemma4:31b": "0badc0de"}),   # tag 一樣、權重被換掉
    ("endpoint", "id", "ffffffffffff"),
    ("endpoint", "api_version", "0.15.0"),
    ("params", "seed_effective", True),                # 可重現性的前提變了
    ("params", "num_ctx_effective", "< probe"),        # 模型看得到的長度變了
    ("sandbox", "engine", "SQL Server 17.0.0 (Developer Edition)"),
    ("deps", "sqlglot", "31.0.0"),                     # AST 預掃的解析器換版
    ("deps", "jinja2", "3.2.0"),
])
def test_環境條件改變就不可比(section, key, value):
    diffs = preflight.compare_fingerprint(_fp(**{section: {key: value}}), _fp())
    assert len(diffs) == 1
    assert f"{section}.{key}" in diffs[0]
    assert "——" in diffs[0], "要說明為什麼不可比,不是只丟一個欄位名"


def test_seed_從無效變有效也算不可比():
    """反直覺但正確:哪天量測模式做出來、seed 真的帶上去了,是**好事**,

    但它讓舊 baseline 的所有數字失去可比性(那些是在「沒有 seed」的條件下量的)。
    這個規則讓那件事自動被發現,不靠人記得。
    """
    diffs = preflight.compare_fingerprint(
        _fp(params={"seed_effective": True}), _fp())
    assert any("seed_effective" in d for d in diffs)


def test_指紋缺欄位不得靜默當成相同():
    """殘缺的指紋(例如略過了起飛前檢查的那輪)拿去比,

    最危險的失敗方式是「兩邊都是 None,判定相同」——那會讓不可比的兩份數字被拿去比。
    """
    diffs = preflight.compare_fingerprint(_fp(), {})
    assert len(diffs) >= 6, f"缺整份指紋卻只回報 {len(diffs)} 項差異"


# ─────────────────────── 七、起飛前檢查的結論 ───────────────────────

def test_任一項_fail_就中止():
    checks = [preflight.Check("a", "ok", ""), preflight.Check("b", "fail", "")]
    assert preflight.verdict(checks) == "abort"


def test_只有_warn_可以帶著限制起飛():
    """seed 無效是 warn 而不是 fail:如果它是 fail,我們永遠不能跑批。

    但它必須被記錄下來並寫進報告——那是「數字怎麼解讀」的前提。
    """
    checks = [preflight.Check("a", "ok", ""), preflight.Check("b", "warn", "")]
    assert preflight.verdict(checks) == "go-with-caveats"
    assert preflight.verdict([preflight.Check("a", "ok", "")]) == "go"


def test_端點識別碼只回答是不是同一個端點_不外洩位址():
    """指紋會跟著 baseline 目錄進公開 repo,所以記的是雜湊不是位址。

    兩件事都要成立:同一個端點要穩定給同一個 id(否則 `--compare` 會誤判不可比),
    而 id 裡不能出現主機名或埠號(否則等於沒雜湊)。
    `/v1` 有沒有、結尾斜線有沒有都算同一個端點——因為 `_base_url` 會先正規化。
    """
    a = preflight._endpoint_id("http://model-host.internal:9999/v1")
    assert a == preflight._endpoint_id("http://model-host.internal:9999/v1/")
    assert a == preflight._endpoint_id("http://model-host.internal:9999")
    assert a != preflight._endpoint_id("http://other-host.internal:9999")
    assert len(a) == 12 and all(c in "0123456789abcdef" for c in a)
    assert "model-host" not in a and "9999" not in a


@pytest.mark.parametrize("endpoint,expect", [
    ("http://h:11434/v1", "http://h:11434"),
    ("http://h:11434/v1/", "http://h:11434"),
    ("http://h:11434", "http://h:11434"),
])
def test_原生_API_的位址要從_v1_削回主機根(endpoint, expect):
    """`/api/version`、`/api/tags` 掛在根上,不在 `/v1` 底下。

    削錯的話所有端點檢查都會靜默失敗成「問不到」,而那正是我們要偵測的東西。
    """
    assert preflight._base_url(endpoint) == expect


def test_沙盒探針一正一反缺一不可():
    """只有正向案例的話,SQL 永遠回傳列也會通過,等於沒驗到比對邏輯。

    這個斷言釘住探針的形狀,不是釘住沙盒——沙盒要連線才驗得到。
    """
    dirs = {c["direction"] for c in preflight.PROBE_PLAN["cases"]}
    assert dirs == {"positive", "negative"}
    assert {c["expect_flagged"] for c in preflight.PROBE_PLAN["cases"]} == {True, False}


# ─────────────────────── 八、報告 ───────────────────────

def test_沒有指紋的_baseline_要標明不可當基準(tmp_path):
    """「沒有指紋」和「量到 seed 無效」是兩件不同的事:

    前者代表連環境都沒記錄(完全不可當基準),後者代表環境記錄完整、
    而且已知不可重現(可用,只是數字要當抽樣讀)。用同一句話帶過會誤導。
    """
    summary = _summary(tmp_path, "nofp", {"601": [(True, 15, 0)]})
    rb.write_report(tmp_path / "nofp", summary, None)
    text = (tmp_path / "nofp" / "REPORT.md").read_text(encoding="utf-8")
    assert "沒有環境指紋" in text and "不可當回歸基準" in text


def test_報告要點名哪幾個_case_的綠燈是假的(tmp_path):
    summary = _summary(tmp_path, "r", {
        "601": [(True, 15, 0)] * 5,
        "406": [(True, 18, 0), (False, 11, 5), (False, 11, 5),
                (True, 16, 2), (False, 11, 5)],
    })
    rb.write_report(tmp_path / "r", summary, None)
    text = (tmp_path / "r" / "REPORT.md").read_text(encoding="utf-8")
    assert "可當硬閘門" in text, "穩定案例要標明可以當閘門"
    assert "不列入評分" in text, "不穩案例要標明不列入評分"


# ─────────────────── 實跑一次才浮出來的三個問題 ───────────────────
#
# 下面三條都不是「想到某個邊界」想出來的,是把 preflight / run_baseline 真的跑過
# 一輪之後撞到的。單元測試本來全綠,因為它們測的是函式,而這三個問題
# 分別出在「參數寫法」「報告措辭」「CLI 分支提早 return」——都在函式之外。

def test_case_參數吃得下帶_mr_前綴的寫法(monkeypatch, tmp_path):
    """`--case mr_406` 要跟 `--case 406` 等價。

    case id 內部是去掉 `mr_` 的那一段,但人記得的是檔名,文件例子也寫 `mr_406`。
    照文件打卻篩不到任何 case,然後以「golden set 為空」離開——訊息指向 golden
    目錄,看起來像目錄壞了。實跑第一次就踩到這個。
    """
    g = tmp_path / "golden"
    g.mkdir()
    for n in ("406", "601"):
        (g / f"mr_{n}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(rb, "GOLDEN_DIR", g)
    assert rb.case_ids("mr_406") == ["406"]
    assert rb.case_ids("406") == ["406"]
    assert rb.case_ids("mr_406,601") == ["406", "601"]
    assert rb.case_ids(None) == ["406", "601"]


@pytest.mark.parametrize("seed_effective,expect_in,forbid", [
    (False,  "連單次模型呼叫都不可重現", "沒有量到"),
    ("未測", "沒有量到",                  "實測未生效"),
    (None,   "沒有量到",                  "實測未生效"),
    (True,   "單次模型呼叫**是可重現的",  "沒有量到"),
])
def test_沒量到_seed_不可以寫成量到_seed_無效(tmp_path, seed_effective, expect_in, forbid):
    """報告不准把「這輪沒跑模型探針」講成「量到 seed 無效」。

    這正是上一輪 seed 誤判的成因:拿一個我們其實沒有的結論當前提寫進文件,
    後面每一層推論都跟著錯。判斷式原本寫 `is not True`,於是 `未測` 會落進
    「實測未生效」那一句——同一個錯誤在同一個專案裡犯第二次。
    """
    fp = _fp(params={"seed_effective": seed_effective, "num_ctx_effective": "未測"})
    summary = {"cases": {}, "aggregate": {}, "fingerprint": fp}
    rb.write_report(tmp_path, summary, None)
    text = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
    assert expect_in in text
    assert forbid not in text


def _mini_baseline(root: Path, mr_id: str, passed: list[bool]) -> Path:
    """造一份最小的 baseline 目錄:N 次跑、每次通過與否由 passed 決定。"""
    (root / "cases").mkdir(parents=True, exist_ok=True)
    for n, ok in enumerate(passed, start=1):
        rec = {"cases": [{"case": mr_id, "layer": "injection",
                          "failed": 0 if ok else 1, "decision": "blocked",
                          "hit": 3, "expected": 3, "noise": 0,
                          "failures": [] if ok else [{"name": "decision"}],
                          "spec_exec": {"passed": ok, "cases": 15,
                                        "coverage_gaps": 0, "dropped_cases": 0}}],
                "errors": []}
        (root / "cases" / f"mr_{mr_id}.run{n}.json").write_text(
            json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return root


def test_summarize_only_也要吃_compare(monkeypatch, tmp_path):
    """重新判一次回歸不該需要重跑十幾小時。

    `--compare` 原本只掛在「重跑」那條路上,`--summarize-only` 在比對之前就
    `return 0` 了。但回歸判定只讀兩份 `summary.json`,完全不碰模型——
    只在重跑那條路支援,等於要花十幾小時才能重判一次,而這套工具存在的理由
    正是不要那樣。實跑時發現的:給了 `--compare` 卻靜默沒有比對,也沒有任何提示。
    """
    g = tmp_path / "golden"
    g.mkdir()
    (g / "mr_101.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(rb, "GOLDEN_DIR", g)

    before = _mini_baseline(tmp_path / "before", "101", [True, True, True])
    after = _mini_baseline(tmp_path / "after", "101", [False, False, False])
    for d in (before, after):
        rb.write_report(d, rb.summarize(d, types.SimpleNamespace(case=None, runs=3),
                                        {"mode": "t"}), None)
        (d / "summary.json").write_text(json.dumps(
            rb.summarize(d, types.SimpleNamespace(case=None, runs=3), {"mode": "t"}),
            ensure_ascii=False), encoding="utf-8")

    args = types.SimpleNamespace(summarize_only=str(after), compare=str(before),
                                 case=None, runs=3, stage1=False, classify_from=None)
    # 穩定案例三次全過 → 三次全掛,必須判回歸並以 1 離開
    assert rb.main(args) == 1
    assert "🔴" in (after / "REPORT.md").read_text(encoding="utf-8")

    # 對照:跟自己比不該判回歸
    args.compare = str(after)
    assert rb.main(args) == 0


def test_端點認不認_seed_與跑批有沒有帶_seed_是兩件事():
    """指紋必須分開記這兩項,混在一起正是上一輪 seed 誤判的成因。

      `seed_effective`  —— **端點**認不認 seed(探針明確帶 seed 測出來的)
      `seed_configured` —— **這輪跑批**實際帶了什麼 seed(從 profile 解析)

    兩者可以同時是「端點認 seed」+「跑批沒帶」:那時跑批一樣不可重現,
    但只看 `seed_effective: True` 會讓人以為它是。這個測試釘住兩欄都要在。
    """
    from dataclasses import dataclass

    @dataclass
    class _P:
        model: str = "m"
        num_ctx: int = 32768
        temperature: float = 0.1
        max_output_tokens: int = 8192

    @dataclass
    class _PSeeded(_P):
        seed: int = 42

    class _C:
        endpoint = "http://x/v1"

        def __init__(self, P):
            self.P = P

        def profile(self, name):
            return self.P()

    bare = preflight.build_fingerprint([], _C(_P), ["review"])
    seeded = preflight.build_fingerprint([], _C(_PSeeded), ["review"])

    # 沒有 seed 欄位時要講「不支援」,不是 None——None 讀起來像「有欄位但沒設」
    assert bare["params"]["seed_configured"]["review"] == "不支援"
    assert seeded["params"]["seed_configured"]["review"] == 42

    # 跑批設定從「沒帶 seed」變成「帶 seed=42」,兩份 baseline 就不可比
    diffs = preflight.compare_fingerprint(bare, seeded)
    assert any("seed_configured" in d for d in diffs), \
        "跑批的 seed 變了卻判成可比 —— 那會讓兩組不同條件下的數字被直接相減"


# ─────────────────── 優雅降級造成的假成功 ───────────────────

def _row_with_gap(*gaps) -> dict:
    return {"case": "406", "layer": "spec_exec", "failed": 1, "decision": "needs_human",
            "spec_exec": {"passed": False, "cases": 0, "coverage_gaps": len(gaps),
                          "dropped_cases": 0, "gap_detail": list(gaps)}}


@pytest.mark.parametrize("gap", [
    # 實際撞到的那一筆(2026-09-23 凌晨端點中斷)
    "測資生成失敗(LLM 呼叫失敗:APIConnectionError: Connection error.)",
    "測資生成失敗(LLM 呼叫失敗:APITimeoutError: timed out)",
    "無法連線執行驗證沙盒 127.0.0.1:1433:connection refused",
    "未設定 SANDBOX_MSSQL_PASSWORD(見 config/sandbox.env.example)",
])
def test_降級來的環境失敗不可以算成有效結果(tmp_path, gap):
    """管線遇到基礎設施失敗會**優雅降級**:照樣跑完、照樣產出看起來正常的結果。

    那是管線該有的行為(正式審查不該因為端點抖一下就整個炸掉),但對跑批是陷阱:
    結果檔通過了每一項既有檢查——JSON 解得開、有這個 case、那一列沒有 error、
    errors 清單是空的——於是環境失敗被計成「品質退步」。

    這正是 FINDINGS「發現十一」那個 bug 的下一層:上一輪擋掉了頂層的 error,
    但沒擋降級訊息裡的。實測撞到過,不是想像的邊界。
    """
    p = tmp_path / "mr_406.run1.json"
    p.write_text(json.dumps({"cases": [_row_with_gap(gap)], "errors": []},
                            ensure_ascii=False), encoding="utf-8")
    assert rb.result_ok(p, "406") is False
    why = rb._why_failed(p, "406")
    assert "基礎設施失敗" in why, "報告要分得出環境問題與品質問題,否則整段會讀錯"


@pytest.mark.parametrize("gap", [
    # 模型沒產出合法輸出 —— 那是真的品質問題,不是環境
    "測資生成失敗(兩次皆無合法輸出)",
    # 正常的覆蓋缺口
    "條件 C2 的反向案例沒有被覆蓋",
])
def test_真的品質問題不可以被誤判成環境失敗(tmp_path, gap):
    """誤判的方向很重要:把真實的退步藏進「環境失敗」那一節,比漏抓更糟——

    環境失敗不計入通過率,所以誤判等於讓一次真的品質退步從數字裡消失。
    """
    p = tmp_path / "mr_406.run1.json"
    p.write_text(json.dumps({"cases": [_row_with_gap(gap)], "errors": []},
                            ensure_ascii=False), encoding="utf-8")
    assert rb.result_ok(p, "406") is True


# ─────────────────── dirty 判定不該把跑批產物算進去 ───────────────────

@pytest.mark.parametrize("line", [
    "?? SEGCRA-CR/eval/baselines/",
    "?? eval/baselines/full-baseline/",
    " M SEGCRA-CR/eval/_output/case_406.json",
    'R  a.py -> SEGCRA-CR/eval/baselines/b.json',       # 重新命名取箭頭右邊
    ' M "SEGCRA-CR/eval/baselines/q w.json"',           # 含空白會被引號包起來
])
def test_跑批產物不算未commit的程式碼改動(line):
    """跑批把輸出寫進 repo,那是**產物**不是程式碼。

    不排掉的話,第二輪起每一份 baseline 都會因為前一輪的輸出而被自己標成 dirty,
    而 dirty 的 baseline 依設計「不該當成別人可以回頭對照的基準」——於是每一份
    都失格,那個警告就變成沒人看的雜訊。實跑時撞到過。

    注意這**不是**用 .gitignore 解決:baseline 目錄是交付物,要能 commit、
    能被別人拿去比對(BASELINE.md §10)。該修的是 dirty 的判準。
    """
    assert preflight._is_output(line) is True


@pytest.mark.parametrize("line", [
    " M SEGCRA-CR/orchestrator/agent.py",
    "?? SEGCRA-CR/eval/run_baseline.py",
    " M SEGCRA-CR/eval/golden/mr_406.json",
    "?? SEGCRA-CR/specs/R-140.md",
])
def test_真的程式碼改動仍然算dirty(line):
    """反方向更重要:漏判會讓「這輪跑的是沒人能重建的程式碼」這件事被藏起來。

    golden case 與規格檔也算——它們是模型看得到的輸入,改了就不是同一份測量。
    """
    assert preflight._is_output(line) is False
