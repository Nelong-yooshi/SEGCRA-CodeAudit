"""#45:內層任何一個環節出事,外層都要照常產出審查結果——不呼叫 LLM、不連資料庫。

原則(issue 原文):外層不能因為內層出事而跟著出事。每個環節各自隔離、各自有時間
上限;失敗或逾時就降級成一條 finding,其他環節照跑,整份審查照常產出,
而且**不得自動放行**。

在正式環境,原本的行為是「MR 送進來之後審查器沒有任何回應:沒擋、沒放、
也沒人知道」——那是最糟的失敗型態。
"""
import asyncio
import json

import pytest

from orchestrator import pipeline


def _report(findings=()):
    return {"summary": "x", "findings": list(findings), "score": 100}


# ─────────────────── 單一環節丟例外 ───────────────────



def test_每個後處理呼叫都有被包到():
    """掃 pipeline 的原始碼,確認每一個 `report = X(report, ...)` 的 X 都包過隔離。

    為什麼要掃原始碼:清單靠人工維護就是會漏。原本是在檔案最後用 globals() 把函式
    換掉,配一份人工清單,而 enforce_format(#32 加的)就這樣漏掉了——漏掉的那個
    環節丟例外時,整份審查照舊中斷,而且是靜默的。現在改用裝飾器(看定義就知道包過),
    這條測試再從另一邊確認沒有新的呼叫處被漏掉(#33 之後還會加 enforce_dbt_impact)。
    """
    import inspect
    import re

    src = inspect.getsource(pipeline)
    called = set(re.findall(r"^\s*report = (\w+)\(report\b", src, re.M))
    assert called, "連一個後處理呼叫都掃不到,這條測試自己壞了"
    missing = sorted(n for n in called if n not in pipeline._ISOLATED_STAGES)
    assert missing == [], f"這些後處理沒有包進環節隔離:{missing}"


def test_登記簿與實際包裝一致():
    """_ISOLATED_STAGES 是裝飾器自己登記的,不是人工清單——抽查它跟函式上的標記一致。"""
    for name, stage in pipeline._ISOLATED_STAGES.items():
        fn = getattr(pipeline, name)
        assert getattr(fn, "__wrapped_stage__", None) == stage, name


def test_環節丟例外時報告照出並記下是哪個環節():
    """包裝過的環節吃到例外:回傳進來的 report、補一條 major 說明是哪個環節。

    只測一次通用的包裝,不逐一列舉環節:包的是同一個 `_isolated`,列舉等於把同一件事
    測十幾遍;而「每個環節都有包到」已經由掃原始碼與登記簿那兩條測試保證——人工清單
    會漏(這份原本就少了後來加的 6 個),那兩條不會。
    """
    stage = "某個環節"
    def boom(report, *a, **kw):
        raise RuntimeError("內部爆掉:這段訊息不可以出現在報告裡")

    wrapped = pipeline._isolated(stage, boom)
    report = _report([{"severity": "minor", "title": "模型原本的發現"}])
    out = wrapped(report, "其他參數")
    assert out is report, "要沿用進來的 report,不可以吃掉前面環節的結果"
    inc = pipeline.review_incomplete(out)
    assert len(inc) == 1 and stage in inc[0], inc
    assert any(f.get("title") == "模型原本的發現" for f in out["findings"]), \
        "其他環節的 finding 要留著"


def test_環節失敗的那條是major():
    """major 會讓 auto_approve 的條件不成立(allowed_severities 只許 info)。"""
    wrapped = pipeline._isolated("某個環節",
                                 lambda r, *a, **k: (_ for _ in ()).throw(ValueError()))
    out = wrapped(_report())
    # 認的是 stage 欄位,不是標題開頭(標題是給人看的文字,改寫它不該讓判斷失效)
    bad = [f for f in out["findings"] if f.get("stage")]
    assert bad and all(f["severity"] == "major" for f in bad)


def test_不回顯例外訊息():
    """例外訊息可能帶著模型輸出或 MR 內容,而報告會貼回 GitLab(#45 明訂不得回顯)。"""
    secret = "SELECT * FROM 客戶資料 WHERE 身分證='A123456789'"

    def boom(report, *a, **kw):
        raise RuntimeError(secret)

    out = pipeline._isolated("注入防線", boom)(_report())
    blob = json.dumps(out, ensure_ascii=False)
    assert secret not in blob, "例外訊息被寫進報告了"
    assert "RuntimeError" in blob, "要寫出例外**類型**,不然查不出是什麼問題"


def test_正常時行為完全不變():
    """格式正確、各環節都正常的審查,輸出要跟改之前完全相同(#45 的完成條件)。"""
    def ok(report, *a, **kw):
        report["findings"].append({"severity": "info", "title": "正常"})
        return report

    out = pipeline._isolated("評分", ok)(_report())
    assert pipeline.review_incomplete(out) == []
    assert [f["title"] for f in out["findings"]] == ["正常"]


# ─────────────────── 執行驗證的時間上限 ───────────────────

def test_執行驗證逾時要中止並記一條(monkeypatch):
    """沙盒的單一查詢有 120 秒上限,但整段原本沒有——實測一題光在沙盒就
    跑了 38 分鐘,整題超過 1 小時。"""
    async def never_returns(*a, **kw):
        await asyncio.sleep(999)

    monkeypatch.setattr(pipeline, "SPEC_EXEC_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(pipeline, "run_spec_exec", never_returns)

    async def run():
        report = _report()
        try:
            await asyncio.wait_for(pipeline.run_spec_exec(None, None, None, None, None),
                                   timeout=pipeline.SPEC_EXEC_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            report["findings"].append(
                pipeline._incomplete_finding("執行驗證",
                                             timeout=pipeline.SPEC_EXEC_TIMEOUT_SECONDS))
        return report

    out = asyncio.run(run())
    inc = pipeline.review_incomplete(out)
    assert len(inc) == 1 and "逾時" in inc[0], inc
    assert "1 秒" in out["findings"][0]["detail"]


def test_上限可以用環境變數設定():
    """issue 寫「例如 20 分鐘,可設定」。預設 1200 秒。"""
    assert pipeline.SPEC_EXEC_TIMEOUT_SECONDS == 1200


# ─────────────────── 決策不得因為沒跑完而放行 ───────────────────

def test_有未完成的環節時不得自動放行():
    """#45:不得 auto_approved,也不得把「沒跑完」當成「沒問題」。"""
    report = _report()
    report["findings"].append(pipeline._incomplete_finding("執行驗證", timeout=1200))
    mr = {"files": [{"path": "m.sql", "diff": "@@ -0,0 +1 @@\n+SELECT 1"}]}
    policy = {"auto_approve": {"max_diff_lines": 30, "min_score": 95,
                               "allowed_severities": ["info"],
                               "forbid_pending_hints": True}}
    out = pipeline.apply_policy(report, mr, policy)
    assert out["decision"] != "auto_approved", out.get("decision")


def test_決策閘門自己壞掉時一律交人工(monkeypatch):
    """決策留空比誤判更危險:呼叫端會以為沒問題。"""
    report = _report()
    broken = pipeline._isolated("決策閘門",
                                lambda r, *a, **k: (_ for _ in ()).throw(KeyError("policy")))
    out = broken(report)
    assert not out.get("decision"), "這個情境下閘門沒跑完,所以沒有決策"
    # review_mr 會在這裡補上 needs_human(見 pipeline.review_mr 的決策段)
    assert pipeline.review_incomplete(out), "至少要留下「未完整」的記錄"


# ─────────────────── 真的走 review_mr 的逾時路徑 ───────────────────
#
# 上面那個逾時測試自己重寫了 asyncio.wait_for,所以把 pipeline 裡的上限拿掉也不會紅
# (突變驗證時那個突變點**存活**了)。這一組驅動真正的 review_mr。

def _mr_fixture(tmp_path, mr_id="950"):
    import json as _json
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / f"mr_{mr_id}.json").write_text(_json.dumps({
        "title": "R-201 調整", "description": "例行維護",
        "sha": "e1a1" + "0" * 36,
        "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+SELECT 1",
                   "full_content": "SELECT account_id FROM txn_log"}],
    }, ensure_ascii=False), encoding="utf-8")
    return fixtures


def _review_with_hanging_spec_exec(monkeypatch, tmp_path, timeout_seconds):
    """讓 run_spec_exec 永遠不回來,其餘環節都用假的,跑真正的 review_mr。"""
    import json as _json

    from orchestrator import pipeline as pl
    from orchestrator.config import load_config
    from toolbox import gitlab

    fixtures = _mr_fixture(tmp_path)
    output = tmp_path / "out"
    output.mkdir(exist_ok=True)
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))
    monkeypatch.setattr(pl, "SPEC_EXEC_TIMEOUT_SECONDS", timeout_seconds)

    model_report = _json.dumps({"summary": "沒問題", "score": 100, "findings": []},
                               ensure_ascii=False)

    async def fake_run_agent(cfg, profile, system, user, hub, verbose=True,
                             use_tools=True, trace=None, meta=None):
        return model_report

    async def hangs(*a, **kw):
        await asyncio.sleep(60)

    monkeypatch.setattr(pl, "run_agent", fake_run_agent)
    monkeypatch.setattr(pl, "run_spec_exec", hangs)
    return asyncio.run(pl.review_mr(load_config(), "950"))


def test_review_mr_的執行驗證逾時會中止並照常產出報告(monkeypatch, tmp_path):
    """整份審查照常產出,只有執行驗證那一段沒有結果。"""
    report = _review_with_hanging_spec_exec(monkeypatch, tmp_path, 1)
    inc = pipeline.review_incomplete(report)
    assert any("執行驗證" in t and "逾時" in t for t in inc), inc
    assert report.get("decision"), "報告還是要有決策"
    assert "summary" in report and "score" in report, "報告本體要完整"


def test_執行驗證逾時時不得自動放行(monkeypatch, tmp_path):
    report = _review_with_hanging_spec_exec(monkeypatch, tmp_path, 1)
    assert report["decision"] != "auto_approved", report["decision"]


def test_執行驗證逾時時沒有_spec_exec欄位(monkeypatch, tmp_path):
    """沒跑完就不該有結果欄位——有欄位會讓判讀的人以為跑過了。"""
    report = _review_with_hanging_spec_exec(monkeypatch, tmp_path, 1)
    assert "_spec_exec" not in report


# ───────── 時間上限要擋得住**同步**卡住的沙盒(#53 review 第 1 點) ─────────
#
# 原本的測試是用 `await asyncio.sleep` 模擬卡住 —— 那種卡法 `wait_for` 中斷得了,
# 所以測試會過,但真實的 `execute_cases` 是同步的 pymssql 呼叫:它在跑的時候事件迴圈
# 整個卡住,`wait_for` 插不進去。老闆實測:上限 1 秒、沙盒卡 6 秒,結果跑了 6 秒、
# 沒有逾時、結果照常回傳。所以這裡一律用 time.sleep 卡。

def test_同步卡住的沙盒也要在上限內中止(monkeypatch):
    """`execute_cases` 換成 time.sleep(3)、上限 1 秒:await 要在 1 秒多一點回來。"""
    import time as _t
    from orchestrator import spec_exec

    monkeypatch.setattr(spec_exec, "execute_cases",
                        lambda sql, plan, deadline=None: _t.sleep(3))

    async def main():
        t0 = _t.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(spec_exec._in_thread(spec_exec.execute_cases,
                                                        "SELECT 1", {}, None),
                                   timeout=1)
        return _t.monotonic() - t0

    took = asyncio.run(main())
    assert took < 2.5, f"同步卡住時 await 沒有準時回來(等了 {took:.1f} 秒)"


def test_背景執行緒是daemon且不是預設執行緒池():
    """不能用 asyncio.to_thread(預設執行緒池):`asyncio.run` 結束時會等池子裡的工作
    做完,等於逾時又被拖回去(跟 #33 的 _wait_in_thread 同一個理由)。"""
    import threading

    from orchestrator import spec_exec

    seen = {}

    def slow(*a):
        cur = threading.current_thread()
        seen["daemon"] = cur.daemon
        seen["name"] = cur.name
        return "done"

    assert asyncio.run(spec_exec._in_thread(slow, 1)) == "done"
    assert seen["daemon"] is True, "非 daemon 的執行緒會讓程式結束時卡住等它"
    assert seen["name"] != "MainThread"


def test_背景執行緒帶得到contextvars():
    """審查用的 ref 釘在 ContextVar 裡(#31):不把 context 帶進執行緒,背景那邊讀到的
    是預設值,於是「依 commit 取回程式碼」會抓到別的版本。"""
    from toolbox import gitlab

    from orchestrator import spec_exec

    gitlab.pin_review_ref("cafe1234")
    try:
        assert asyncio.run(spec_exec._in_thread(gitlab.review_ref)) == "cafe1234"
    finally:
        gitlab.pin_review_ref(None)


# ───────── 未完成是硬條件、逾時要說是哪個階段(#53 review 第 5、7 點) ─────────

POLICY_LOOSE = {"auto_approve": {"max_diff_lines": 999, "min_score": 0,
                                 "allowed_severities": ["info", "minor", "major"],
                                 "forbid_pending_hints": False},
                "block": {"min_blockers": 1}}


def test_有環節沒完成就不得自動放行_即使設定放寬():
    """原本是靠設定檔的 allowed_severities 只許 info 擋著(未完成的那條是 major)。
    設定一放寬,沒跑完的審查就會被當成「沒問題」自動放行——所以要寫死在程式裡。"""
    mr = {"files": [{"path": "a.sql", "diff": "@@ -1 +1 @@\n+SELECT 1;",
                     "full_content": "SELECT 1;"}]}
    base = {"score": 100, "findings": [], "_spec_exec": {"passed": True}}
    # 對照:什麼都沒缺的時候,這份寬鬆設定會放行
    assert pipeline.apply_policy(dict(base), mr, POLICY_LOOSE)["decision"] == "auto_approved"

    report = {**base, "findings": [pipeline._incomplete_finding("沙盒執行", timeout=1200)]}
    out = pipeline.apply_policy(report, mr, POLICY_LOOSE)
    assert out["decision"] == "needs_human"
    assert out["_policy_signals"]["incomplete"], out["_policy_signals"]


def test_逾時的階段名稱():
    """整段只算一個環節時,報告上只寫「執行驗證未完成」,而測資生成/沙盒/仲裁的修法
    完全不同。問不出來(例外發生在階段之間)就記整段,不要猜。"""
    assert pipeline._spec_exec_stage({"stage": "沙盒執行"}) == "執行驗證/沙盒執行"
    assert pipeline._spec_exec_stage({"stage": None}) == "執行驗證"
    assert pipeline._spec_exec_stage({}) == "執行驗證"


def test_逾時的finding要帶階段欄位與標題():
    f = pipeline._incomplete_finding("執行驗證/測資生成", timeout=1200)
    assert f["stage"] == "執行驗證/測資生成"
    assert f["title"].startswith("[執行驗證/測資生成]") and "逾時" in f["title"]
    assert f["severity"] == "major"
    assert "1200" in f["detail"]


# ───────── dbt 專案的取回也要隔離(#53 review 指出的、測試掃不到的那一處) ─────────
#
# `prepare_dbt_context` 自己有 catch-all,但讀 `dbt_project.yml` 判斷專案佈局的那一步
# (`_head_layout`)在那之外。而它不是 `report = X(report, ...)` 的形狀,所以
# `test_每個後處理呼叫都有被包到` 掃原始碼看不到它 —— 只能明寫這兩條。

def test_同步環節丟例外時記一條並回預設值():
    """`_step` 是給「不吃 report、回傳別的東西」的步驟用的(取回 dbt 專案、注入掃描):
    那種形狀 `@_isolated_stage` 套不上、掃原始碼的測試也看不到,而它們丟例外一樣會讓
    整份審查沒有輸出。"""
    def boom(a, b):
        raise RuntimeError("內部爆掉:這段訊息不可以出現在報告裡")

    report = _report()
    sentinel = object()
    assert pipeline._step(report, "某個同步環節", boom, sentinel, 1, 2) is sentinel
    inc = pipeline.review_incomplete(report)
    assert len(inc) == 1 and "某個同步環節" in inc[0], inc
    assert "爆掉" not in json.dumps(report, ensure_ascii=False), "不得回顯例外訊息"


def test_同步環節正常時原樣回傳():
    report = _report()
    assert pipeline._step(report, "x", lambda a: a * 2, None, 21) == 42
    assert pipeline.review_incomplete(report) == []


def test_dbt_專案取回失敗時當成取回失敗_不是不適用(monkeypatch):
    """預設值刻意是**標了 error 的 DbtContext**,不是 None:None 代表「不適用」,
    會被當成這個 MR 沒有 dbt 要處理,等於把失敗變成靜默放寬。"""
    def boom(mr, dbt_cfg):
        raise RuntimeError("讀 dbt_project.yml 爆掉")

    monkeypatch.setattr(pipeline, "prepare_dbt_context", boom)
    report = _report()
    ctx = pipeline._step(report, "dbt 專案取回", pipeline.prepare_dbt_context,
                         pipeline.DbtContext(prefix="", error="取回 dbt 專案時發生非預期錯誤"),
                         {"files": []}, {"enabled": True})
    assert ctx is not None and ctx.error
    assert pipeline.review_incomplete(report)


def test_dry_run_也要看得到環節未完整(monkeypatch):
    """`--dry-run` 這條路最常被用來檢查「確定性那半有沒有跑成」,所以**環節失敗一定要
    看得到**。那幾條 finding(預掃、dbt 取回、注入掃描)在 report 存在之前就產生了,
    原本只在正式路徑被併進報告,dry-run 直接 return 就把它們丟掉了 —— 看起來像一切正常。
    """
    class _Hub:
        async def call_json(self, name, args):
            return {}

        async def call(self, name, args):
            return ""

    stub = [pipeline._incomplete_finding("注入掃描", RuntimeError("x")),
            pipeline._incomplete_finding("dbt 專案取回", RuntimeError("y"))]
    report = asyncio.run(pipeline._dry_run_report(
        _Hub(), "900", {"files": []}, [], None, None, incomplete=stub))
    inc = pipeline.review_incomplete(report)
    assert len(inc) == 2, inc
    assert any("注入掃描" in t for t in inc) and any("dbt 專案取回" in t for t in inc)
    # 分數要反映那兩條 major,否則「看起來正常」只是換個地方發生
    assert report["score"] < 100


def test_dry_run_沒有未完整時行為不變(monkeypatch):
    class _Hub:
        async def call_json(self, name, args):
            return {}

        async def call(self, name, args):
            return ""

    report = asyncio.run(pipeline._dry_run_report(
        _Hub(), "900", {"files": []}, [], None, None))
    assert report["findings"] == [] and report["score"] == 100
