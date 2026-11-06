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
#
# **一律補報**(#104):規則命中是程式判定的事實,不看模型有沒有提到。原本用關鍵詞猜
# 「模型已經講過同一件事」,而那會誤中——模型在別的 finding 裡提到 GRANT,R008 就整條
# 不補,代價是安靜地漏報。
#
# **補上的不會跟模型的去重**:正式路徑上 sanitize_findings 排在 enforce_rules 之前,
# 所以模型講過同一件事時報告上一律是兩條。也不該在補報之後再去重一次——去重留下的是
# 建議較長的那條、不看嚴重度,程式補的 blocker 會被模型的 info 換掉。

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


_POLICY = {"auto_approve": {"max_diff_lines": 30, "min_score": 95,
                            "allowed_severities": ["info"], "forbid_pending_hints": True},
           "block": {"min_blockers": 1}}
_R001 = {"rule": "R001", "severity": "blocker", "message": "DELETE 沒有 WHERE,將刪除整張表"}


@pytest.mark.parametrize("model,label", [
    ([], "模型什麼都沒報"),
    ([{"severity": "blocker", "file": FILE, "title": "DELETE 沒有 WHERE"}], "一模一樣的話"),
    ([{"severity": "info", "file": FILE, "title": "已確認 WHERE 條件無誤"}], "講反的 info"),
    ([{"severity": "blocker", "file": FILE, "title": "硬編碼密碼"}], "講別的事"),
])
def test_不論模型報了什麼都照補(model, label):
    """猜「模型已經講過」會誤中:講反的 info(「已確認 WHERE 條件無誤」)曾經讓
    R001 的 blocker 不補,執行驗證通過時全表 DELETE 就被自動放行(#16 review)。
    現在結構上不可能——規則命中一律補。"""
    report = enforce_rules({"summary": "WHERE 條件都檢查過了", "findings": list(model)},
                           _pre([_R001]))
    assert any(f["title"].startswith("[R001]") for f in report["findings"]), label


def test_確定性的_blocker_一定擋得下來():
    """接到決策閘門:不論模型怎麼寫,全表 DELETE 都是 blocked。"""
    from orchestrator.pipeline import apply_policy

    report = enforce_rules(
        {"score": 100, "_spec_exec": {"passed": True},
         "findings": [{"severity": "info", "file": FILE, "title": "已確認 WHERE 條件無誤"}]},
        _pre([_R001]))
    mr = {"files": [{"path": "sql/maintenance/x.sql", "diff": "@@\n+DELETE FROM t;"}]}
    assert apply_policy(report, mr, _POLICY)["decision"] == "blocked"


def test_模型講過同一件事時報告上是兩條():
    """**照正式路徑的順序**:review_mr 裡先 sanitize_findings、再 enforce_rules,
    所以補上的 R 命中從來不會跟模型的 finding 去重 —— 兩條都會在。

    先 enforce_rules 再手動去重的寫法驗不到正式路徑(那是上一版這條測試的錯)。
    """
    from orchestrator.pipeline import sanitize_findings

    model = [{"severity": "blocker", "file": FILE,
              "title": "DELETE 沒有 WHERE 條件,將影響全表", "detail": "",
              "suggestion": "加上 WHERE", "citations": ["R-100"]}]
    report = enforce_rules(sanitize_findings({"findings": list(model)}),
                           _pre([{"rule": "R001", "severity": "blocker",
                                  "message": "DELETE 沒有 WHERE 條件,將影響全表"}]))
    assert len(report["findings"]) == 2, "說法再接近也是兩條"


def test_模型的近似info不會換掉程式補的blocker():
    """不能為了少一條而在 enforce_rules 之後再去重:去重留下的是建議較長、引用較多
    的那條,不看嚴重度 —— 模型寫一條標題近似的 info 附一段建議,程式補的 blocker
    就會被換掉(#16 的那個洞)。"""
    from orchestrator.pipeline import apply_policy, sanitize_findings

    model = [{"severity": "info", "file": FILE,
              "title": "DELETE 沒有 WHERE 條件,將影響全表", "detail": "已確認沒問題",
              "suggestion": "這段是一次性清理,已與需求確認過,不需要 WHERE" * 3,
              "citations": ["R-100", "R-101"]}]
    report = enforce_rules(sanitize_findings(
        {"score": 100, "_spec_exec": {"passed": True}, "findings": list(model)}), _pre([_R001]))
    blockers = [f for f in report["findings"] if f["severity"] == "blocker"]
    assert len(blockers) == 1 and blockers[0]["title"].startswith("[R001]")
    mr = {"files": [{"path": "sql/maintenance/x.sql", "diff": "@@\n+DELETE FROM t;"}]}
    assert apply_policy(report, mr, _POLICY)["decision"] == "blocked"


def test_不認得的嚴重度照樣補():
    pre = _pre([{"rule": "R999", "severity": "critical", "message": "R999 新規則"}])
    for sev in ("major", "blocker"):
        report = enforce_rules(
            {"findings": [{"severity": sev, "file": FILE, "title": "R999 已處理"}]}, pre)
        assert len(report["findings"]) == 2, sev


def test_每個檔案各自補():
    """#16 review:兩個檔案都有 R001,模型只在 a.sql 報了 blocker,b.sql 的也要補。"""
    other = "sql/rules/r002_other.sql"
    pre = _pre([_R001]) + _pre([_R001], path=other)
    report = enforce_rules(
        {"findings": [{"severity": "blocker", "file": FILE, "title": "DELETE 沒有 WHERE"}]}, pre)
    assert [f["file"] for f in report["findings"][1:]] == [FILE, other]


def test_同檔多個命中都要補():
    """同一個檔案兩句 DML 都沒有 WHERE:兩條都要出現在報告上。"""
    pre = _pre([_R001, {"rule": "R001", "severity": "blocker",
                        "message": "UPDATE 沒有 WHERE 條件,將影響全表"}])
    report = enforce_rules({"findings": []}, pre)
    assert [f["title"][:6] for f in report["findings"]] == ["[R001]", "[R001]"]


# ---- 同一個檔案有多處相同規則的命中(#28 review)

def _hit(statement, message="DELETE 沒有 WHERE 條件,將影響全表", rule="R001", severity="blocker"):
    return {"rule": rule, "severity": severity, "message": message, "statement": statement}


def _finding(text, severity="blocker", file=FILE, field="detail"):
    base = {"severity": severity, "file": file, "title": "資料異動沒有條件", "detail": "",
            "suggestion": ""}
    return {**base, field: text}


TWO = [_hit("DELETE FROM ledger_a"), _hit("DELETE FROM ledger_b")]


def _added(report, before):
    return report["findings"][len(before):]


@pytest.mark.parametrize("n_hits, n_model", [(1, 0), (1, 1), (2, 0), (2, 1), (5, 2), (5, 5)])
def test_補的數量等於命中數(n_hits, n_model):
    """以前是「命中數減掉模型報的數量」,模型報得多就補得少——而「報得多」是用字猜的。"""
    hits = [_hit(f"DELETE FROM t{i}") for i in range(n_hits)]
    model = [_finding(f"第 {i} 個全表刪除") for i in range(n_model)]
    report = enforce_rules({"findings": list(model)}, _pre(hits))
    assert len(_added(report, model)) == n_hits


def test_沒有語句的命中也照補():
    """文字層規則(R004)的命中沒有語句;預掃結果也可能沒有 statement 這個欄位。"""
    hits = [{"rule": "R004", "severity": "blocker", "message": "疑似硬編碼憑證", "statement": ""},
            {"rule": "R004", "severity": "blocker", "message": "疑似硬編碼憑證"}]
    model = [_finding("這裡有硬編碼的密碼")]
    report = enforce_rules({"findings": list(model)}, _pre(hits))
    assert len(_added(report, model)) == 2


def test_不同規則分開補():
    hits = [_hit("DELETE FROM a"), _hit("SELECT * FROM b", "SELECT *", rule="R002",
                                        severity="minor")]
    report = enforce_rules({"findings": []}, _pre(hits))
    assert sorted(f["title"][:6] for f in report["findings"]) == ["[R001]", "[R002]"]


def test_多處命中時說明寫出數量_單一命中的說明不變():
    report = enforce_rules({"findings": [_finding("第一句沒有 WHERE")]},
                           _pre(list(TWO) + [_hit("DELETE FROM ledger_c")]))
    details = [f["detail"] for f in report["findings"][1:]]
    assert len(details) == 3
    assert all("共有 3 處命中 R001" in d for d in details)
    assert all("只對應到" not in d for d in details), "一律補報,沒有「對應到幾處」這回事"
    assert "第 1 條" in details[0] and "第 3 條" in details[2]

    single = enforce_rules({"findings": []}, _pre([_hit("DELETE FROM ledger_a")]))
    assert single["findings"][0]["detail"] == "DELETE 沒有 WHERE 條件,將影響全表"


def test_每一條都寫出自己那一句的行號():
    """一律補報之後,每一條補的就是某一處命中,行號直接取那一處的 —— 不再有
    「不知道模型講的是哪一句,所以不寫行號」的情況。"""
    hits = [dict(_hit("DELETE FROM ledger_a"), line=3),
            dict(_hit("DELETE FROM ledger_b"), line=9)]
    report = enforce_rules({"findings": [_finding("第一句沒有 WHERE")]}, _pre(hits))
    assert [f["line"] for f in _added(report, [_finding("x")])] == [3, 9]


def test_補的內容不回顯命中的語句():
    """語句是 MR 作者寫的文字,補的 finding 會被貼回 MR 留言。"""
    hits = [_hit("DELETE FROM zz_marker_one"), _hit("DELETE FROM zz_marker_two")]
    for model in ([], [_finding("第一句沒有 WHERE")]):
        report = enforce_rules({"findings": list(model)}, _pre(hits))
        for f in _added(report, model):
            assert "zz_marker" not in " ".join(str(v) for v in f.values())


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


_H001 = {"rule": "H001", "severity": "hint", "message": "沖正是否處理"}


@pytest.mark.parametrize("field", ["title", "detail", "suggestion"])
def test_同檔_finding_的標題內文建議都算回應(field):
    """#16 review:只認同一個檔案的 findings 裡,標題、內文、建議提到的回應。"""
    finding = {"severity": "info", "file": FILE, "title": "核對結果"}
    finding[field] = "已確認沖正資料於前置系統處理"
    report = enforce_hints({"findings": [finding]}, _pre([_H001]))
    assert len(report["findings"]) == 1


@pytest.mark.parametrize("severity", ["info", "minor", "major", "blocker"])
def test_檢核點的回應不限嚴重度(severity):
    """檢核點的回應本來就可以是 info(與 enforce_rules 要求同等嚴重度不同)。"""
    report = enforce_hints(
        {"findings": [{"severity": severity, "file": FILE, "title": "已確認沖正處理"}]},
        _pre([_H001]))
    assert len(report["findings"]) == 1


@pytest.mark.parametrize("report", [
    {"summary": "沖正已確認無誤", "findings": []},
    {"findings": [{"severity": "info", "file": "sql/rules/other.sql",
                   "title": "已確認沖正處理"}]},
    {"findings": [{"severity": "info", "file": FILE, "title": "核對結果",
                   "citations": [{"source": "沖正"}]}]},
], ids=["summary", "other-file", "other-field"])
def test_summary_別的檔案或其他欄位提到不算回應(report):
    """#16 review:以前比對報告全文,summary 寫一句、別的檔案或其他欄位提到關鍵詞,
    這個檔案的檢核點就算處理過、不再擋自動放行。"""
    out = enforce_hints(report, _pre([_H001]))
    assert any(f["title"].startswith("[H001]") for f in out["findings"])


def test_自己補的檢核點不能吞掉同檔的其他檢核點():
    """用真的預掃訊息:H005 的訊息含「規格」(H003 的關鍵詞),實際命中順序是
    H002 → H005 → H003。若拿自己補的來比對,H003 會被 H005 補的文字吞掉。"""
    import json

    from toolbox.sqltools import run_rules
    sql = ("-- R-201\nSELECT a.x, b.y, SUBSTRING(a.account_id, 1, 4) AS account_id\n"
           "FROM a JOIN b ON a.id = b.id\nGROUP BY a.x, b.y")
    hits = json.loads(run_rules(sql))
    assert [h["rule"] for h in hits] == ["H002", "H005", "H003"]
    report = enforce_hints({"findings": []}, _pre(hits))
    assert [f["title"][:6] for f in report["findings"]] == ["[H002]", "[H005]", "[H003]"]


# ---- 同一個檔案有多處相同的檢核點(#28 review:與規則同一類,而且會影響決策)

def _hint(statement, rule="H001", message="此規則聚合交易金額,但未見沖正/退匯處理"):
    return {"rule": rule, "severity": "hint", "message": message, "statement": statement}


def _reply(text, severity="info", file=FILE):
    return {"severity": severity, "file": file, "title": "檢核點回應", "detail": text,
            "suggestion": ""}


TWO_HINTS = [_hint("SELECT SUM(amount) FROM transactions WHERE tx_type = 'A'"),
             _hint("SELECT SUM(amount) FROM transactions WHERE tx_type = 'B'")]


def _pending(report):
    return [f for f in report["findings"] if "檢核點待人工確認" in f.get("title", "")]


def test_同檔兩處相同檢核點_模型只回應一處_另一處仍待確認():
    """以前同檔只要有一條提到關鍵詞,兩處 H001 都算回應過,不再擋自動放行。"""
    model = [_reply("第一段查詢的資料已是淨額,不需另外處理沖正")]
    report = enforce_hints({"findings": list(model)}, _pre(list(TWO_HINTS)))
    pending = _pending(report)
    assert len(pending) == 1
    assert pending[0]["severity"] == "info" and pending[0]["file"] == FILE
    assert pending[0]["title"] == "[H001] 檢核點待人工確認(模型未回應)"


@pytest.mark.parametrize("n_hints, n_replies, expected", [
    (1, 0, 1), (1, 1, 0), (1, 2, 0), (2, 0, 2), (2, 1, 1), (2, 2, 0), (4, 1, 3), (4, 6, 0),
])
def test_待確認的數量等於檢核點數減掉回應數(n_hints, n_replies, expected):
    hints = [_hint(f"SELECT SUM(amount) FROM transactions WHERE k = {i}") for i in range(n_hints)]
    model = [_reply(f"第 {i} 段:沖正已另外處理") for i in range(n_replies)]
    report = enforce_hints({"findings": list(model)}, _pre(hints))
    assert len(_pending(report)) == expected


def test_兩條回應講的是同一段查詢_另一段仍待確認():
    model = [_reply("SELECT SUM(amount) FROM transactions WHERE tx_type = 'A' 已排除沖正"),
             _reply("select sum(amount) from transactions where tx_type = 'A' 的退匯另有處理")]
    report = enforce_hints({"findings": list(model)}, _pre(list(TWO_HINTS)))
    assert len(_pending(report)) == 1


def test_檔案提到兩個規格代碼_只回應一次時另一個仍待確認():
    """H003 是每個規格代碼一處(沒有語句),照數量算。"""
    hints = [_hint("", rule="H003", message="檔案涉及規則 R-201:必須與核定規格逐項核對"),
             _hint("", rule="H003", message="檔案涉及規則 R-202:必須與核定規格逐項核對")]
    one = [_reply("已與核定規格 R-201 逐項核對")]
    report = enforce_hints({"findings": list(one)}, _pre(hints))
    assert len(_pending(report)) == 1
    two = one + [_reply("已與核定規格 R-202 逐項核對")]
    assert _pending(enforce_hints({"findings": list(two)}, _pre(hints))) == []


def test_不同代碼的檢核點分開計算():
    hints = list(TWO_HINTS) + [_hint("", rule="H003", message="檔案涉及規則 R-201")]
    model = [_reply("沖正已處理"), _reply("退匯已處理")]
    report = enforce_hints({"findings": list(model)}, _pre(hints))
    assert [f["title"][:6] for f in _pending(report)] == ["[H003]"]


def test_別的檔案的回應不抵這個檔案的檢核點():
    model = [_reply("沖正已處理"), _reply("沖正已處理", file="sql/rules/r002_other.sql")]
    report = enforce_hints({"findings": list(model)}, _pre(list(TWO_HINTS)))
    assert len(_pending(report)) == 1


def test_多處檢核點時說明寫出數量_單一檢核點的說明不變():
    report = enforce_hints({"findings": [_reply("沖正已處理")]}, _pre(list(TWO_HINTS)))
    detail = _pending(report)[0]["detail"]
    assert "共有 2 處命中 H001" in detail and "只對應到 1 處" in detail

    single = enforce_hints({"findings": []}, _pre([TWO_HINTS[0]]))
    assert _pending(single)[0]["detail"] == "此規則聚合交易金額,但未見沖正/退匯處理"


def test_不認得的嚴重度不算回應():
    """`_coverage` 只剩檢核點在用(規則已改成一律補報)。檢核點把每一處都當成 info 比,
    所以嚴重度幾乎不影響 —— 唯一還有作用的是**不認得的嚴重度**:那種 finding 不算回應,
    檢核點照樣待確認,寧可多擋一次。"""
    pre = _pre([_H001])
    weird = {"severity": "critical", "file": FILE, "title": "沖正已處理"}
    report = enforce_hints({"findings": [weird]}, pre)
    assert any("[H001]" in f["title"] for f in report["findings"])


def test_引用了語句的回應先對應():
    """兩處同樣的檢核點:一條回應引用了第二句、一條沒引用。先讓有引用的對上它那一句,
    沒引用的才去補剩下的 —— 反過來的話,沒引用的先占掉第二句,有引用的就沒得對,
    結果會多補一條「未回應」。"""
    hits = [_hint("SELECT a FROM t1"), _hint("SELECT b FROM t2")]
    # 引用的是**第一句**:沒引用的那條若先對應,就會占走第一句,有引用的那條反而
    # 對不到 —— 結果多補一條「未回應」,而那一處其實有人回應過。
    model = [{"severity": "info", "file": FILE, "title": "沖正處理已確認", "detail": "",
              "suggestion": ""},
             {"severity": "info", "file": FILE, "title": "沖正:`SELECT a FROM t1` 已確認",
              "detail": "", "suggestion": ""}]
    report = enforce_hints({"findings": list(model)}, _pre(hits))
    assert [f for f in report["findings"] if "[H001]" in f["title"]] == []


def test_補的檢核點不回顯命中的語句():
    hints = [_hint("SELECT SUM(amount) FROM transactions WHERE zz_marker = 1"),
             _hint("SELECT SUM(amount) FROM transactions WHERE zz_marker = 2")]
    for model in ([], [_reply("沖正已處理")]):
        report = enforce_hints({"findings": list(model)}, _pre(hints))
        for f in _pending(report):
            assert "zz_marker" not in " ".join(str(v) for v in f.values())


def test_後處理鏈上檢核點排在任何程式補報之前():
    """enforce_rules 補的 R004 內文含「憑證」「密碼」(H004 的關鍵詞);若排在前面,
    會被當成模型已回應 H004。檢核點只認模型自己的回應。"""
    import inspect

    from orchestrator import pipeline
    src = inspect.getsource(pipeline.review_mr)
    hints = src.index("report = enforce_hints(report, pre)")
    assert src.index("report = sanitize_findings(report)") < hints
    for call in ("enforce_rules", "enforce_parse", "enforce_style", "enforce_dbt_notice",
                 "enforce_dbt_render_failure", "enforce_injection", "enforce_unreviewable"):
        assert hints < src.index(f"report = {call}("), call


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


def test_模型同標題的_info_不能取代解析失敗的_major():
    """#16 review:這類標題只能由程式產生,先移除模型同標題的輸出再補程式自己的。"""
    from orchestrator.pipeline import _PARSE_FAIL_TITLE

    model = {"severity": "info", "file": FILE, "title": _PARSE_FAIL_TITLE, "detail": "沒問題"}
    report = enforce_parse({"findings": [model]}, _pre(parse_error="boom"))
    [f] = report["findings"]
    assert f["severity"] == "major" and f["detail"] != "沒問題"


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
