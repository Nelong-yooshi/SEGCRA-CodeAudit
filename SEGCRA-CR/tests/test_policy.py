"""決策閘門 apply_policy / 評分 apply_rubric 的確定性測試 — 不呼叫 LLM、不連資料庫。

為什麼值得單獨測:決策是「這個 MR 能不能自動合入」的最後一道閘門,它**完全是確定性
邏輯**(刻意不採用模型自評分數,見 apply_policy 的 docstring)。但在此之前,它只能靠
golden case 端到端驗——那要先等 13-20 分鐘的模型呼叫才走得到這裡,而且同一個閘門
往往被多個條件同時擋住,分不出是哪一條在起作用。

這裡改成直接餵合成的 report/mr,**一次只翻動一個條件**,證明每一道閘門各自都會動。

驗的東西:
  1. 六個自動放行條件逐一隔離:diff 大小、新規則檔、執行驗證、分數、severity 白名單、
     未回應的檢核點。
  2. blocker 的優先權:有 blocker 一律 blocked,而且**提早 return、不寫
     _policy_signals**(run_eval.py 對這個狀態有特別處理,行為變了會壞掉)。
  3. policy 缺席時的 fail-safe:設定檔沒給 policy → needs_human,不是放行。
  4. 佔位 diff 退回 full_content:diff 是「(generated)」這種佔位符時,變更量改算
     full_content 行數,否則一整條新規則會被當成 0 行小改而自動放行。
  5. apply_rubric 的扣分與 verdict 換算,以及「裁決權不給模型」(模型自評分數只留在
     _model_score,不參與決策)。
"""
import pytest

from orchestrator.pipeline import apply_policy, apply_rubric

# 與 config/models.yaml 的 policy 區塊同構;測試不讀設定檔,避免改 yaml 就讓測試變色
POLICY = {
    "auto_approve": {
        "max_diff_lines": 30,
        "min_score": 95,
        "allowed_severities": ["info"],
        "forbid_pending_hints": True,
    },
    "block": {"min_blockers": 1},
}


def _report(**over):
    """一份「六個條件全部符合」的報告:不翻動任何東西就應該 auto_approved。"""
    base = {
        "score": 100,
        "findings": [],
        "_spec_exec": {"passed": True},
    }
    base.update(over)
    return base


def _mr(**over):
    """對應的 MR:小改、不在 sql/rules/ 底下(不觸發新規則檔條件)。"""
    base = {
        "files": [{
            "path": "sql/maintenance/cleanup.sql",
            "diff": "@@ -1,2 +1,3 @@\n context\n+SELECT 1;\n+SELECT 2;",
            "full_content": "SELECT 1;\nSELECT 2;",
        }],
    }
    base.update(over)
    return base


def test_基準情境_六個條件全符合時自動放行():
    """先確立基準線:不翻動任何條件時必須是 auto_approved。

    沒有這個基準,底下每個「翻一個條件就不放行」的測試都證明不了因果——
    有可能它本來就不會放行。
    """
    r = apply_policy(_report(), _mr(), POLICY)
    assert r["decision"] == "auto_approved"
    assert r["_policy_signals"]["spec_exec_passed"] is True


# ─────────────────── 六個自動放行條件,一次翻一個 ───────────────────

def test_閘門_diff_行數超過門檻():
    mr = _mr(files=[{
        "path": "sql/maintenance/big.sql",
        "diff": "@@\n" + "\n".join(f"+line{i}" for i in range(31)),
        "full_content": "x",
    }])
    r = apply_policy(_report(), mr, POLICY)
    assert r["decision"] == "needs_human"
    assert r["_policy_signals"]["change_lines"] == 31


def test_閘門_新規則檔一律至少人工過目():
    """sql/rules/ 下新建的檔案(diff 以 @@ -0,0 開頭)不得自動放行,再小也一樣。"""
    mr = _mr(files=[{
        "path": "sql/rules/r999_new.sql",
        "diff": "@@ -0,0 +1,2 @@\n+SELECT 1;\n+SELECT 2;",
        "full_content": "SELECT 1;\nSELECT 2;",
    }])
    r = apply_policy(_report(), mr, POLICY)
    assert r["decision"] == "needs_human"
    assert r["_policy_signals"]["new_rule"] is True


@pytest.mark.parametrize("spec_exec", [
    {"passed": False},          # 執行驗證判定不通過
    {},                         # 根本沒跑到(沒有 passed 欄位)
    {"passed": "true"},         # 字串 "true" 不算通過(用 is True 嚴格比對)
])
def test_閘門_執行驗證是硬條件(spec_exec):
    r = apply_policy(_report(_spec_exec=spec_exec), _mr(), POLICY)
    assert r["decision"] == "needs_human"
    assert r["_policy_signals"]["spec_exec_passed"] is False


def test_閘門_分數低於門檻():
    """min_score 單獨生效的證明。

    注意:這個組合(分數 94 但 severity 只有 info)目前**端到端跑不出來**——
    apply_rubric 只對 blocker/major/minor 扣分,info 不扣分,所以分數掉到 95 以下時
    一定已經有 minor 以上的 finding,severity 白名單那關也會同時被擋。
    兩個條件在現行 rubric 下是耦合的,只有在這一層才分得開。
    這也正是單元測試的價值:閘門各自的契約可以獨立驗證,不受上游耦合影響。
    """
    r = apply_policy(_report(score=94), _mr(), POLICY)
    assert r["decision"] == "needs_human"

    # 邊界:剛好等於門檻要放行(min_score 是「>=」不是「>」)
    r = apply_policy(_report(score=95), _mr(), POLICY)
    assert r["decision"] == "auto_approved"


def test_閘門_severity_白名單():
    """白名單是 [info],出現任何 minor 就不放行——一條純排版的 minor 就夠。"""
    r = apply_policy(
        _report(findings=[{"severity": "minor", "title": "排版"}]), _mr(), POLICY)
    assert r["decision"] == "needs_human"
    assert r["_policy_signals"]["severities"] == ["minor"]

    # info 在白名單內,不影響放行
    r = apply_policy(
        _report(findings=[{"severity": "info", "title": "風格彙總"}]), _mr(), POLICY)
    assert r["decision"] == "auto_approved"


def test_閘門_未回應的檢核點():
    """標題含「檢核點待人工確認」= 模型沒回應 hint,管線補的 info,不得自動放行。"""
    findings = [{"severity": "info", "title": "[H002] 檢核點待人工確認(模型未回應)"}]
    r = apply_policy(_report(findings=findings), _mr(), POLICY)
    assert r["decision"] == "needs_human"
    assert r["_policy_signals"]["pending_hints"] is True

    # 關掉這個條件就會放行,證明是這一條在擋
    policy = {**POLICY, "auto_approve": {**POLICY["auto_approve"],
                                         "forbid_pending_hints": False}}
    r = apply_policy(_report(findings=findings), _mr(), policy)
    assert r["decision"] == "auto_approved"


# ─────────────────── blocker 與 fail-safe ───────────────────

def test_blocker_優先且不寫_policy_signals():
    """有 blocker 直接 blocked 並提早 return。

    _policy_signals 不會被寫入——run_eval.py 的 auto_approve_blockers() 與
    read_signal("pending_hints") 都對這個狀態有特別處理,行為改了那邊會壞。
    """
    r = apply_policy(
        _report(findings=[{"severity": "blocker", "title": "疑似提示注入"}]),
        _mr(), POLICY)
    assert r["decision"] == "blocked"
    assert "_policy_signals" not in r


def test_policy_缺席時_fail_safe_不放行():
    """設定檔沒給 policy(或給空的)→ needs_human,絕不是自動放行。

    這是「設定壞掉時往安全邊倒」的保證:少了門檻設定,寧可打擾人,不可誤放。
    """
    for policy in ({}, None):
        r = apply_policy(_report(), _mr(), policy)
        assert r["decision"] == "needs_human"


# ─────────────────── 佔位 diff 退回 full_content ───────────────────

def test_佔位_diff_退回算_full_content_行數():
    """diff 是「(generated)」佔位符時,變更量改算 full_content 行數。

    沒有這個退路的話,一整條新規則會因為 diff 看起來 0 行而被當成小改自動放行。
    """
    mr = _mr(files=[{
        "path": "sql/maintenance/gen.sql",
        "diff": "(generated)\n+SELECT 1;",          # 有 + 行,但含 generated 字樣
        "full_content": "\n".join(f"line{i}" for i in range(40)),
    }])
    r = apply_policy(_report(), mr, POLICY)
    assert r["_policy_signals"]["change_lines"] == 40   # 退回算 full_content
    assert r["decision"] == "needs_human"               # 40 > max_diff_lines 30


def test_沒有_diff_時也退回_full_content():
    mr = _mr(files=[{
        "path": "sql/maintenance/nodiff.sql",
        "diff": "",
        "full_content": "SELECT 1;\nSELECT 2;\nSELECT 3;",
    }])
    r = apply_policy(_report(), mr, POLICY)
    assert r["_policy_signals"]["change_lines"] == 3


# ─────────────────── 評分 ───────────────────

@pytest.mark.parametrize("counts,expected_score", [
    ({}, 100),                                    # 無 finding
    ({"info": 3}, 100),                           # info 不扣分
    ({"minor": 2}, 90),                           # minor 每條 -5
    ({"major": 2}, 70),                           # major 每條 -15
    ({"blocker": 1}, 59),                         # blocker -40,且封頂 59
    ({"blocker": 1, "major": 3}, 15),             # 100-40-45=15,未觸及封頂
    ({"blocker": 3}, 0),                          # 不會變負數
])
def test_rubric_扣分(counts, expected_score):
    findings = [{"severity": sev} for sev, n in counts.items() for _ in range(n)]
    assert apply_rubric({"findings": findings})["score"] == expected_score


@pytest.mark.parametrize("findings,verdict", [
    ([], "approve"),
    ([{"severity": "minor"}], "approve"),                       # 95 分、無 major
    ([{"severity": "major"}], "needs_changes"),                 # 有 major 不得 approve
    ([{"severity": "blocker"}], "needs_changes"),               # 59 分 → 不到 reject
    ([{"severity": "blocker"}, {"severity": "major"}] * 2, "reject"),
])
def test_rubric_verdict(findings, verdict):
    assert apply_rubric({"findings": list(findings)})["verdict"] == verdict


def test_模型自評分數不參與決策():
    """模型自己給的分數只被保留在 _model_score,實際分數由 rubric 重算。

    實測同一個 MR 模型自評會逐輪漂移(50 vs 10),所以裁決權不交給模型。
    """
    r = apply_rubric({"score": 100, "findings": [{"severity": "major"}]})
    assert r["_model_score"] == 100     # 模型說 100
    assert r["score"] == 85             # 管線算 85,以這個為準
