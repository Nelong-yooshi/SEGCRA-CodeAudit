"""評測工具自己的計分邏輯 — `eval/run_eval.py` 的比對與統計函式,不呼叫 LLM。

**為什麼這是最該測的一層**:這些函式是「尺」本身。`finding_matches` 是每一條
`_golden` 斷言的必經之路,`match_expected` 是所有 recall / precision 數字的來源。
它們有 bug,baseline 的**每一個數字都是錯的,而且不會有任何錯誤訊息**——套件照樣
全綠,人照樣拿那些數字去判斷「有沒有改壞」。

而且有前例:`allowed_citations` 的比對曾經拿 dict 跟字串比,讓 citations 層永遠不可能
通過(見 `eval/FINDINGS.md`「發現七」)。那個 bug 存在期間,那一層的數字全是假的,
沒有任何跡象。

eval/README 的「覆蓋缺口」清單列的是**管線的**檢測缺口,漏掉了「尺自己準不準」。
這個檔案補的就是那一塊。

驗的東西:
  1. `finding_matches` 的選擇器語意:severity / file / 關鍵詞,有給才比、給了就要準。
  2. `title_contains_any` 優先於 `title_contains`(README 建議一律用 any 版,
     因為 findings 的文字是模型生成的,綁死單一措辭會製造假失敗)。
  3. 關鍵詞比對範圍含 `detail`,不只 `title`——模型常把重點寫在說明裡。
  4. `match_expected` 的行號 ±2 容忍區間,含邊界與缺 `line` 欄位的情況。
  5. `is_style_finding`:sqlfluff 規則碼與中文關鍵詞都算,但別誤判正常 finding。
  6. `auto_approve_blockers` 的每一條原因,以及 `decision=blocked` 提早 return
     沒有 `_policy_signals` 的特例(管線真的會走到這個狀態)。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
from run_eval import (  # noqa: E402
    auto_approve_blockers, finding_matches, is_style_finding, match_expected, spec_desc,
)

POLICY = {
    "auto_approve": {
        "max_diff_lines": 30,
        "min_score": 95,
        "allowed_severities": ["info"],
        "forbid_pending_hints": True,
    },
    "block": {"min_blockers": 1},
}


def _f(**over):
    base = {"file": "sql/rules/r201.sql", "line": 10, "severity": "major",
            "title": "實作與核定規格不符", "detail": "閾值用了 >= 但規格寫「超過」"}
    base.update(over)
    return base


# ─────────────────── finding_matches:每條斷言的必經之路 ───────────────────

def test_沒給條件就一律符合():
    """空的選擇器 = 「任何 finding 都算」。forbid_findings 用它表達「這個檔案不得有
    任何 finding」,所以這個行為不能改。"""
    assert finding_matches({}, _f()) is True


@pytest.mark.parametrize("spec,expect", [
    ({"severity": "major"}, True),
    ({"severity": "minor"}, False),
    ({"file": "sql/rules/r201.sql"}, True),
    ({"file": "sql/rules/other.sql"}, False),
    ({"severity": "major", "file": "sql/rules/r201.sql"}, True),
    ({"severity": "major", "file": "sql/rules/other.sql"}, False),   # 兩個條件要同時成立
])
def test_severity_與_file_有給才比_給了就要準(spec, expect):
    assert finding_matches(spec, _f()) is expect


def test_關鍵詞會比對到_detail_不只_title():
    """模型常把重點寫在 detail 裡,只比 title 會漏掉真的有抓到的情況。"""
    assert finding_matches({"title_contains": "閾值"}, _f()) is True   # 只在 detail 裡
    assert finding_matches({"title_contains": "不符"}, _f()) is True   # 在 title 裡
    assert finding_matches({"title_contains": "沖正"}, _f()) is False


def test_any_版任一命中即可():
    """README 建議一律用 any 版:同一個問題模型可能寫「注入」也可能寫「規避」,
    綁死單一措辭會製造假失敗——防線是好的,只是用詞不同。"""
    spec = {"title_contains_any": ["沖正", "閾值", "粒度"]}
    assert finding_matches(spec, _f()) is True                        # 命中「閾值」
    assert finding_matches({"title_contains_any": ["沖正", "粒度"]}, _f()) is False


def test_any_版優先於單一版():
    """兩個都給時以 any 版為準(實作是先看 any)。

    釘住這個優先順序:如果哪天改成「兩個都要滿足」,既有 case 的斷言語意會悄悄變嚴。
    """
    spec = {"title_contains_any": ["閾值"], "title_contains": "絕對不會出現的字"}
    assert finding_matches(spec, _f()) is True


def test_空的關鍵詞清單視為沒給():
    """`title_contains_any: []` 會落回檢查 `title_contains`,兩個都空就是「任何都算」。
    寫 case 時誤留空清單不該變成「永遠不符合」。"""
    assert finding_matches({"title_contains_any": []}, _f()) is True


def test_缺欄位的_finding_不會炸():
    """模型輸出殘缺(少 title / detail / severity)時,比對要回 False 而不是丟例外
    ——否則一個殘缺的 finding 會讓整個評測中斷。"""
    assert finding_matches({"title_contains": "閾值"}, {}) is False
    assert finding_matches({"severity": "major"}, {}) is False


def test_spec_desc_產生可讀的斷言名稱():
    """斷言名稱會印在報告裡給人看,壞掉會讓失敗訊息無法判讀。"""
    assert "major" in spec_desc({"severity": "major"})
    assert "注入|規避" in spec_desc({"title_contains_any": ["注入", "規避"]})
    assert spec_desc({}) == "(任意)"


# ─────────────────── match_expected:recall / precision 的來源 ───────────────────

@pytest.mark.parametrize("line,expect", [
    (16, True),    # 正中
    (14, True),    # 下界 -2,剛好
    (18, True),    # 上界 +2,剛好
    (13, False),   # 超出下界
    (19, False),   # 超出上界
])
def test_行號容忍區間是正負二(line, expect):
    """沿用 PoC 語意:同檔 + 行號落在 line_range ±2 內即算命中。

    ±2 是為了容忍模型指到相鄰行(例如指到 HAVING 的上一行)。
    區間算錯會直接讓 recall 數字失真,而且不會有任何跡象。
    """
    exp = {"file": "sql/rules/r201.sql", "line_range": [16, 16]}
    assert match_expected(exp, [_f(line=line)]) is expect


def test_跨多行的_line_range():
    exp = {"file": "sql/rules/r201.sql", "line_range": [10, 20]}
    assert match_expected(exp, [_f(line=8)]) is True     # 10-2
    assert match_expected(exp, [_f(line=22)]) is True    # 20+2
    assert match_expected(exp, [_f(line=7)]) is False


def test_檔名必須相同():
    exp = {"file": "sql/rules/r201.sql", "line_range": [10, 10]}
    assert match_expected(exp, [_f(file="sql/rules/other.sql", line=10)]) is False


def test_預掃補的_finding_行號是零_對不上真實行號():
    """管線補位的 R/H 系列 finding 行號一律是 0(README 明確記載這件事)。

    所以 `expected` 不該用來標機械性規則命中——那些要用 `expect_findings`。
    這個測試把「行號 0 對不上 line_range」這個前提釘住。
    """
    exp = {"file": "sql/rules/r201.sql", "line_range": [16, 16]}
    assert match_expected(exp, [_f(line=0)]) is False
    # 但如果 expected 本來就標在檔頭附近,行號 0 會命中——這是刻意的
    assert match_expected({"file": "sql/rules/r201.sql", "line_range": [1, 1]},
                          [_f(line=0)]) is True


def test_沒有_line_欄位視為零():
    exp = {"file": "sql/rules/r201.sql", "line_range": [16, 16]}
    f = _f()
    del f["line"]
    assert match_expected(exp, [f]) is False


def test_沒有_line_range_時視為全檔():
    """`line_range` 省略時預設 [0, 10**9],代表「這個檔案有抓到就算」。"""
    assert match_expected({"file": "sql/rules/r201.sql"}, [_f(line=99999)]) is True


def test_多個_findings_任一命中即可():
    exp = {"file": "sql/rules/r201.sql", "line_range": [16, 16]}
    assert match_expected(exp, [_f(line=1), _f(line=16), _f(line=99)]) is True
    assert match_expected(exp, []) is False


# ─────────────────── is_style_finding:風格統計 ───────────────────

@pytest.mark.parametrize("title,detail,expect", [
    ("建議統一 SQL 風格格式", "", True),                       # 中文關鍵詞
    ("縮排不一致", "", True),
    ("排版建議", "", True),
    ("", "預掃顯示 LT02、LT01 縮排不一致", True),               # sqlfluff 規則碼
    ("CP01 關鍵字大小寫", "", True),
    ("實作與核定規格不符", "閾值用了 >=", False),               # 真問題,不是風格
    ("DELETE 沒有 WHERE", "會刪掉整張表", False),
])
def test_風格_finding_的識別(title, detail, expect):
    """這個判定只用於統計(README 說明它不影響任何判定),但統計錯了會讓

    「lint 的 severity 不一致」這個發現的數字失真——那是要拿去調 policy 的依據。
    """
    assert is_style_finding({"title": title, "detail": detail}) is expect


def test_規則碼要完整比對不能只看兩個字母():
    """`\\b(LT|CP|...)\\d{2}\\b` 要求字母後緊接兩位數字。

    否則像 "ST" 開頭的正常英文字(STATUS、STRING)會被誤判成風格碼。
    """
    assert is_style_finding({"title": "STATUS 欄位型別不符", "detail": ""}) is False
    assert is_style_finding({"title": "ST05 子查詢應改為 CTE", "detail": ""}) is True


# ─────────────────── auto_approve_blockers:調門檻的主要依據 ───────────────────

def _report(**over):
    base = {
        "decision": "needs_human",
        "score": 100,
        "_policy_signals": {"change_lines": 5, "new_rule": False,
                            "severities": ["info"], "pending_hints": False,
                            "spec_exec_passed": True},
    }
    base.update(over)
    return base


def test_全部條件符合時沒有任何阻礙原因():
    assert auto_approve_blockers(_report(), POLICY) == []


@pytest.mark.parametrize("signals,score,expect_keyword", [
    ({"change_lines": 31}, 100, "diff 行數"),
    ({"new_rule": True}, 100, "新規則檔"),
    ({"spec_exec_passed": False}, 100, "執行驗證未通過"),
    ({}, 94, "分數"),
    ({"severities": ["minor"]}, 100, "severity 白名單"),
    ({"pending_hints": True}, 100, "待確認檢核點"),
])
def test_每一條阻礙原因都列得出來(signals, score, expect_keyword):
    """調門檻的核心數據:知道是被哪一項擋掉,才知道該調哪一項。

    少列一條,那個門檻就永遠不會被檢討到。
    """
    ps = {**_report()["_policy_signals"], **signals}
    out = auto_approve_blockers(_report(score=score, _policy_signals=ps), POLICY)
    assert any(expect_keyword in o for o in out), f"沒列出「{expect_keyword}」:{out}"


def test_多個原因會全部列出():
    """可複選——不是列出第一個就停,否則調好一項才發現還有三項。"""
    ps = {"change_lines": 40, "new_rule": True, "severities": ["major"],
          "pending_hints": True, "spec_exec_passed": False}
    out = auto_approve_blockers(_report(score=10, _policy_signals=ps), POLICY)
    assert len(out) == 6, f"應該六項全中,實際 {out}"


def test_blocked_的特例_沒有_policy_signals():
    """`apply_policy` 遇到 blocker 會提早 return,**不會**寫 `_policy_signals`。

    這個狀態管線真的會走到(所有注入類 case 都是),處理錯會讓整個統計掛掉。
    tests/test_policy.py 有對應的測試釘住 apply_policy 那一側,兩邊要一起看。
    """
    out = auto_approve_blockers({"decision": "blocked"}, POLICY)
    assert out == ["有 blocker(直接擋下)"]


def test_dry_run_沒有決策時不列統計():
    """dry-run 根本沒跑到決策層,列統計會製造假數據。"""
    assert auto_approve_blockers({}, POLICY) == []
    assert auto_approve_blockers({"score": 100}, POLICY) == []


def test_有決策但缺_policy_signals_要明確講出來():
    """不該靜靜回空清單——那會被誤讀成「沒有任何阻礙」。"""
    out = auto_approve_blockers({"decision": "needs_human"}, POLICY)
    assert out == ["無 _policy_signals"]
