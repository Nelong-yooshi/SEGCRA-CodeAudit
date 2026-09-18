"""確定性後處理鏈的測試 — sanitize_findings 與 enforce_* 系列,不呼叫 LLM。

為什麼值得單獨測:這條鏈是三明治架構的「下半片」,存在的理由就是**不賭模型自律**——
模型漏報的規則命中由管線補回、模型亂加的非法 severity 由管線清掉。但這些函式吃的是
「模型輸出」,用 golden case 驗的話,得先等模型真的產出那種輸出才碰得到分支,而模型
會不會產出那種輸出我們控制不了。改成直接餵合成報告,每個分支都走得到。

驗的東西:
  1. sanitize_findings:丟掉非法 severity(hint 是預掃內部代碼,模型會原樣 echo)、
     同檔近似標題去重且保留資訊較完整的那條。
  2. enforce_rules:預掃的 R 規則命中若模型沒提 → 確定性補回(DELETE 無 WHERE 這種
     機械性命中不能因為模型挑著報而靜默消失)。
  3. enforce_hints:H 系列檢核點沒被回應 → 補成 info,並且標題要能被決策閘門的
     pending_hints 判定抓到(兩邊用同一個字串,任一邊改了這裡會紅)。
  4. enforce_parse:預掃解析失敗 → 補 major。這條最重要:解析失敗代表 AST 規則整組
     沒跑,若不補,這個缺口不會出現在報告任何地方,MR 還可能因「沒有命中」被放行。
  5. enforce_style:已學會的 S- 系列風格命中沒被提 → 補 info。
  6. 共通行為:模型已經提過的就不重複補(用關鍵詞比對)。
"""
import pytest

from orchestrator.pipeline import (
    enforce_hints, enforce_parse, enforce_rules, enforce_style, sanitize_findings,
)

FILE = "sql/rules/r001_purge.sql"


def _pre(rules=None, parse_error=None, path=FILE):
    entry = {"path": path, "rules": rules or [], "lint": []}
    if parse_error:
        entry["parse_error"] = parse_error
    return [entry]


# ─────────────────── sanitize_findings ───────────────────

@pytest.mark.parametrize("severity", ["hint", "HINT", "critical", "warning", "", None])
def test_丟掉非法的_severity(severity):
    """hint 是預掃的內部代碼,不是合法的 finding severity。

    起因:模型傾向把預掃的 JSON 整包複製進 findings,連 severity=hint 一起 echo,
    造成報告裡出現管線不認得的等級,rubric 算分與 severity 白名單都會被弄亂。
    """
    report = sanitize_findings({"findings": [{"severity": severity, "title": "x"}]})
    assert report["findings"] == []


def test_合法的_severity_都保留():
    findings = [{"severity": s, "title": f"標題{i}", "file": f"f{i}.sql"}
                for i, s in enumerate(["blocker", "major", "minor", "info"])]
    report = sanitize_findings({"findings": list(findings)})
    assert len(report["findings"]) == 4


def test_同檔近似標題去重且保留資訊較完整者():
    """兩條標題幾乎一樣時只留一條,而且留「有修改建議 / 有引用」的那條。

    保留規則是 len(suggestion) + 50 * len(citations) 取大者——引用權重高,
    因為有引用代表模型真的去對過規格,那條的價值比較高。
    """
    thin = {"severity": "major", "file": FILE,
            "title": "DELETE 沒有 WHERE 條件", "suggestion": "", "citations": []}
    rich = {"severity": "major", "file": FILE,
            "title": "DELETE 沒有 WHERE 條件(整表刪除)",
            "suggestion": "補上 WHERE", "citations": [{"source": "specs/R-001.md"}]}

    for order in ([thin, rich], [rich, thin]):
        report = sanitize_findings({"findings": [dict(f) for f in order]})
        assert len(report["findings"]) == 1, f"順序 {[f['title'][:6] for f in order]} 沒去重"
        assert report["findings"][0]["citations"], "留錯了:應保留有引用的那條"


def test_不同檔案的相同標題不去重():
    """去重只在同一個檔案內做——兩個檔案各自有同樣的問題是兩個問題,不能合併。"""
    findings = [
        {"severity": "major", "file": "a.sql", "title": "SELECT * 應明列欄位"},
        {"severity": "major", "file": "b.sql", "title": "SELECT * 應明列欄位"},
    ]
    report = sanitize_findings({"findings": findings})
    assert len(report["findings"]) == 2


def test_標題差很多就不去重():
    findings = [
        {"severity": "major", "file": FILE, "title": "DELETE 沒有 WHERE 條件"},
        {"severity": "major", "file": FILE, "title": "時間窗邊界與核定規格不符"},
    ]
    report = sanitize_findings({"findings": findings})
    assert len(report["findings"]) == 2


def test_去重會忽略規則碼前綴與排版符號():
    """[R001] 這種前綴與 `**` 等排版符號不算差異——同一件事寫法不同而已。"""
    findings = [
        {"severity": "major", "file": FILE, "title": "[R001] DELETE 沒有 WHERE 條件"},
        {"severity": "major", "file": FILE, "title": "**DELETE 沒有 WHERE 條件**"},
    ]
    report = sanitize_findings({"findings": findings})
    assert len(report["findings"]) == 1


# ─────────────────── enforce_rules ───────────────────

def test_模型漏報的_R_規則命中要補回():
    """大型多問題 SQL 裡,模型會挑語意問題報而漏掉機械性規則命中。

    不補的話,DELETE 無 WHERE 這種確定性 blocker 會靜默消失。
    """
    pre = _pre([{"rule": "R001", "severity": "blocker",
                 "message": "DELETE 沒有 WHERE,將刪除整張表"}])
    report = enforce_rules({"findings": []}, pre)
    assert len(report["findings"]) == 1
    added = report["findings"][0]
    assert added["severity"] == "blocker"     # 沿用預掃給的等級,不降級
    assert "R001" in added["title"]
    assert added["file"] == FILE


def test_模型已經報過就不重複補():
    """用關鍵詞比對報告全文;模型用自己的話講同一件事也算報過。"""
    pre = _pre([{"rule": "R002", "severity": "minor", "message": "視圖使用 SELECT *"}])
    report = enforce_rules(
        {"findings": [{"severity": "minor", "file": FILE,
                       "title": "建議明列欄位而非 SELECT *"}]}, pre)
    assert len(report["findings"]) == 1, "模型已經報過了,不該重複補"


def test_hint_系列不由_enforce_rules_處理():
    """H 系列是檢核點,走 enforce_hints;enforce_rules 只管 R 系列。"""
    pre = _pre([{"rule": "H001", "severity": "hint", "message": "沖正/退匯是否已處理"}])
    report = enforce_rules({"findings": []}, pre)
    assert report["findings"] == []


# ─────────────────── enforce_hints ───────────────────

def test_沒回應的檢核點補成_info():
    pre = _pre([{"rule": "H002", "severity": "hint", "message": "通報粒度是否為聯名戶"}])
    report = enforce_hints({"findings": []}, pre)
    assert len(report["findings"]) == 1
    assert report["findings"][0]["severity"] == "info"


def test_檢核點標題要能被決策閘門認出來():
    """補出來的標題必須含「檢核點待人工確認」。

    apply_policy 的 pending_hints 就是比對這個字串;兩邊任一改了,自動放行的
    forbid_pending_hints 條件會靜默失效——這個測試就是把兩邊釘在一起。
    """
    from orchestrator.pipeline import apply_policy

    pre = _pre([{"rule": "H002", "severity": "hint", "message": "粒度"}])
    report = enforce_hints({"findings": [], "score": 100,
                            "_spec_exec": {"passed": True}}, pre)
    assert "檢核點待人工確認" in report["findings"][0]["title"]

    policy = {"auto_approve": {"max_diff_lines": 30, "min_score": 95,
                               "allowed_severities": ["info"],
                               "forbid_pending_hints": True},
              "block": {"min_blockers": 1}}
    mr = {"files": [{"path": "sql/maintenance/x.sql", "diff": "@@\n+SELECT 1;",
                     "full_content": "SELECT 1;"}]}
    decided = apply_policy(report, mr, policy)
    assert decided["_policy_signals"]["pending_hints"] is True
    assert decided["decision"] == "needs_human"


def test_模型回應過的檢核點不補():
    """H001 的判定關鍵詞是 沖正/退匯/淨額,報告裡提到任一個就算回應過。"""
    pre = _pre([{"rule": "H001", "severity": "hint", "message": "沖正是否處理"}])
    report = enforce_hints(
        {"findings": [{"severity": "info", "file": FILE,
                       "title": "已確認沖正資料於前置系統淨額"}]}, pre)
    assert len(report["findings"]) == 1


# ─────────────────── enforce_parse ───────────────────

def test_預掃解析失敗要補_major():
    """解析失敗 = AST 規則整組沒跑,不補的話這個缺口在報告裡完全看不到。

    而且必須是 major:major 不在 allowed_severities([info])內,所以這個檔案
    不可能被自動放行——這是「確定性檢查不完整就不准放行」的保證。
    """
    pre = _pre(parse_error="Expected table name but got '{{'")
    report = enforce_parse({"findings": []}, pre)
    assert len(report["findings"]) == 1
    assert report["findings"][0]["severity"] == "major"
    assert "dbt" in report["findings"][0]["detail"]   # 提示常見原因:樣板未展開


def test_同檔解析失敗不重複補():
    pre = _pre(parse_error="boom")
    report = enforce_parse({"findings": []}, pre)
    report = enforce_parse(report, pre)      # 再跑一次
    assert len(report["findings"]) == 1


def test_沒有解析錯誤就不補():
    report = enforce_parse({"findings": []}, _pre())
    assert report["findings"] == []


# ─────────────────── enforce_style ───────────────────

def test_學到的風格命中沒被提就補_info():
    """S- 系列是從團隊範例「學到」的風格(如前置逗號),讓它有牙齒而不必賭模型遵循。"""
    pre = _pre([{"rule": "S-COMMA", "severity": "info", "message": "團隊慣例使用前置逗號"}])
    report = enforce_style({"findings": []}, pre)
    assert len(report["findings"]) == 1
    assert "S-COMMA" in report["findings"][0]["title"]
    assert report["findings"][0]["severity"] == "info"


def test_模型已提到風格就不補():
    pre = _pre([{"rule": "S-COMMA", "severity": "info", "message": "前置逗號"}])
    report = enforce_style(
        {"findings": [{"severity": "info", "file": FILE, "title": "逗號位置建議調整"}]}, pre)
    assert len(report["findings"]) == 1


def test_非_S_系列不由_enforce_style_處理():
    pre = _pre([{"rule": "R002", "severity": "minor", "message": "SELECT *"}])
    report = enforce_style({"findings": []}, pre)
    assert report["findings"] == []
