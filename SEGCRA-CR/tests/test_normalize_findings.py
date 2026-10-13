"""模型輸出的格式不正確時,審查不可以中斷(#21)。

模型輸出的 JSON 解析得過、但欄位型別不對時,後處理以前會丟例外:MR 收不到結果;
在寫回留言時才出錯的話,還會停在「留言貼了一半、沒有 label 也沒有 commit status」。

三組測試:
1. normalize_findings / enforce_format 本身(每一種型別錯誤怎麼處理、算不算格式錯誤)
2. 不可以讓決策變寬鬆、不可以被模型關掉、原本正常的輸入行為不變
3. 整條 review_mr:格式錯誤的輸出要能一路走到寫完 label 與 commit status
"""
import asyncio
import copy
import inspect
import json

import pytest

from orchestrator import pipeline, spec_exec
from orchestrator.config import load_config
from orchestrator.pipeline import (_FORMAT_NO_TITLE, _FORMAT_TITLE, apply_policy, apply_rubric,
                                   enforce_format, normalize_findings, sanitize_findings,
                                   validate_citations)
from toolbox import gitlab

FILE = "sql/maintenance/x.sql"


def _fd(**over) -> dict:
    fd = {"file": FILE, "line": 3, "severity": "minor", "title": "標題", "detail": "說明",
          "suggestion": "建議", "citations": []}
    fd.update(over)
    return fd


def _norm(findings, **top) -> dict:
    return normalize_findings({"summary": "總評", "findings": findings, **top})


def _chain(report: dict) -> dict:
    """後處理鏈裡與格式有關的幾步,順序與 review_mr 相同。"""
    report = normalize_findings(report)
    report = validate_citations(report, "R-201")
    report = sanitize_findings(report)
    report = enforce_format(report)
    return apply_rubric(report)


def _format_findings(report: dict) -> list[dict]:
    return [f for f in report["findings"] if f["title"] == _FORMAT_TITLE]


# ─────────────────── 1. 每一種型別錯誤 ───────────────────

@pytest.mark.parametrize("bad", ["一段文字", None, 5, ["x"], True],
                         ids=["字串", "null", "數字", "清單", "布林"])
def test_finding不是物件_丟掉並記錄(bad):
    r = _norm([bad, _fd()])
    assert [f["title"] for f in r["findings"]] == ["標題"]
    assert r["_format_issues"] == {"fixed": 0, "dropped": 1, "findings_not_a_list": False}


@pytest.mark.parametrize("sev", [[], ["major"], {"a": 1}, 3, True, 1.5],
                         ids=["空清單", "清單", "物件", "數字", "布林", "浮點數"])
def test_severity不是字串_丟掉並記錄(sev):
    r = _norm([_fd(severity=sev)])
    assert r["findings"] == []
    assert r["_format_issues"]["dropped"] == 1


@pytest.mark.parametrize("title, expect", [
    (["第一段", "第二段"], "第一段\n第二段"), (5, "5"), ({"a": 1}, "{'a': 1}"),
    ([], _FORMAT_NO_TITLE), ([1, 2], "[1, 2]"), (False, "False"),
], ids=["字串清單", "數字", "物件", "空清單", "數字清單", "布林"])
def test_title型別不對_修正後保留並記錄(title, expect):
    r = _norm([_fd(title=title, severity="blocker")])
    assert [(f["title"], f["severity"]) for f in r["findings"]] == [(expect, "blocker")]
    assert r["_format_issues"] == {"fixed": 1, "dropped": 0, "findings_not_a_list": False}


@pytest.mark.parametrize("key", ["detail", "suggestion"])
@pytest.mark.parametrize("value, expect", [(["a", "b"], "a\nb"), (7, "7"), ({"k": "v"}, "{'k': 'v'}")],
                         ids=["字串清單", "數字", "物件"])
def test_detail與suggestion型別不對_轉成文字並記錄(key, value, expect):
    r = _norm([_fd(**{key: value})])
    assert r["findings"][0][key] == expect and isinstance(r["findings"][0][key], str)
    assert r["_format_issues"]["fixed"] == 1


@pytest.mark.parametrize("file", [["a.sql"], 5, {"p": 1}, True], ids=["清單", "數字", "物件", "布林"])
def test_file型別不對_補空字串並記錄(file):
    r = _norm([_fd(file=file)])
    assert r["findings"][0]["file"] == "" and r["_format_issues"]["fixed"] == 1


@pytest.mark.parametrize("line", ["abc", [], [3], {"n": 3}, "12.5", float("nan"), float("inf"),
                                  "9" * 5000],
                         ids=["文字", "空清單", "清單", "物件", "小數字串", "NaN", "Infinity", "超長數字"])
def test_line無法轉成整數_設成0並記錄(line):
    r = _norm([_fd(line=line)])
    assert r["findings"][0]["line"] == 0 and r["_format_issues"]["fixed"] == 1


@pytest.mark.parametrize("line, expect", [(7, 7), ("12", 12), (" 12 ", 12), (12.0, 12), (12.9, 12),
                                          (0, 0), (-1, -1)],
                         ids=["整數", "數字字串", "含空白", "浮點數", "浮點數取整", "零", "負數"])
def test_line原本就能用的寫法_照舊接受且不算格式錯誤(line, expect):
    r = _norm([_fd(line=line)])
    assert r["findings"][0]["line"] == expect and type(r["findings"][0]["line"]) is int
    assert "_format_issues" not in r


@pytest.mark.parametrize("cites", ["某規格", {"source": "x"}, 5, True], ids=["字串", "物件", "數字", "布林"])
def test_citations不是清單_清空並記錄(cites):
    r = _norm([_fd(citations=cites)])
    assert r["findings"][0]["citations"] == [] and r["_format_issues"]["fixed"] == 1


@pytest.mark.parametrize("item", ["specs/R-201.md", None, 5, ["x"], {"source": 5}, {"source": ["a"]}],
                         ids=["字串", "null", "數字", "清單", "source 是數字", "source 是清單"])
def test_citations裡格式不對的項目_只丟那一項並記錄(item):
    good = {"source": "specs/R-201.md", "article": "§2"}
    r = _norm([_fd(citations=[item, good])])
    assert r["findings"][0]["citations"] == [good] and r["_format_issues"]["fixed"] == 1


@pytest.mark.parametrize("bad", [None, "沒有發現", {"a": 1}, 0, 5, False],
                         ids=["null", "字串", "物件", "零", "數字", "False"])
def test_findings不是清單_當作沒有finding並記錄(bad):
    r = _norm(bad)
    assert r["findings"] == []
    assert r["_format_issues"] == {"fixed": 0, "dropped": 0, "findings_not_a_list": True}


def test_一條finding有多處錯誤_只算一條():
    r = _norm([_fd(title=["a"], detail=5, line="x", file=[], citations="x")])
    assert r["_format_issues"]["fixed"] == 1


def test_修正與丟掉分開計數():
    r = _norm([_fd(title=5), _fd(line="x"), "字串", _fd(severity=[]), _fd()])
    assert r["_format_issues"] == {"fixed": 2, "dropped": 2, "findings_not_a_list": False}
    assert len(r["findings"]) == 3


@pytest.mark.parametrize("summary, expect", [(None, ""), (5, "5"), (["a", "b"], "a\nb"), ("原文", "原文")],
                         ids=["null", "數字", "字串清單", "字串"])
def test_summary一律是字串(summary, expect):
    """寫回 commit status 時會對 summary 做切片,不是字串就會中斷。"""
    r = normalize_findings({"summary": summary, "findings": []})
    assert r["summary"] == expect and "_format_issues" not in r


def test_沒有summary欄位_補空字串():
    assert normalize_findings({"findings": []})["summary"] == ""


# ─────────────────── 2a. 原本正常的輸入,行為不變 ───────────────────

def test_格式正確的報告_完全不變():
    report = {"summary": "總評", "score": 90, "verdict": "approve", "findings": [
        _fd(), _fd(title="另一條", severity="info",
                   citations=[{"source": "specs/R-201.md", "article": "§2"}])]}
    before = copy.deepcopy(report)
    after = normalize_findings(report)
    assert after == before and "_format_issues" not in after
    assert _format_findings(_chain(copy.deepcopy(before))) == []


@pytest.mark.parametrize("key", ["title", "detail", "suggestion", "file", "line", "citations"])
@pytest.mark.parametrize("how", ["缺漏", "null"])
def test_欄位缺漏或null_當成空值且不算格式錯誤(key, how):
    """模型常把沒有的建議寫成 null、MR 層級的 finding 不寫 file;這些以前就不會出錯,
    算成格式錯誤的話幾乎每個 MR 都會多一條 major。"""
    fd = _fd()
    if how == "缺漏":
        del fd[key]
    else:
        fd[key] = None
    r = _norm([fd])
    assert "_format_issues" not in r
    assert r["findings"][0][key] == {"line": 0, "citations": []}.get(key, "")


def test_沒有findings欄位_當成空清單且不算格式錯誤():
    r = normalize_findings({"summary": "x"})
    assert r["findings"] == [] and "_format_issues" not in r


@pytest.mark.parametrize("sev", ["hint", "critical", "Major", "", " major"])
def test_不合法的severity字串_照舊由sanitize靜默丟棄_不補major(sev):
    """gemma 常把預掃的 hint 原樣抄回來;算成格式錯誤的話會變成雜訊。"""
    r = _chain({"summary": "x", "findings": [_fd(severity=sev), _fd(title="留下的")]})
    assert [f["title"] for f in r["findings"]] == ["留下的"]
    assert "_format_issues" not in r


@pytest.mark.parametrize("how", ["缺漏", "null"])
def test_沒有severity_照舊由sanitize靜默丟棄(how):
    fd = _fd()
    if how == "缺漏":
        del fd["severity"]
    else:
        fd["severity"] = None
    r = _chain({"summary": "x", "findings": [fd]})
    assert r["findings"] == [] and "_format_issues" not in r


def test_finding的其他欄位原樣保留():
    r = _norm([_fd(extra={"k": [1]}, rule="R001")])
    assert r["findings"][0]["extra"] == {"k": [1]} and r["findings"][0]["rule"] == "R001"


# ─────────────────── 2b. 補的那條 major ───────────────────

def test_有格式錯誤_補一條major且不得自動放行():
    r = _chain({"summary": "x", "findings": [_fd(title=None, severity="info"), "壞掉的"]})
    (fmt,) = _format_findings(r)
    assert fmt["severity"] == "major" and fmt["file"] == "(MR 內容)" and fmt["line"] == 0
    assert r["verdict"] != "approve"
    r["_spec_exec"] = {"passed": True}
    assert apply_policy(r, {"files": []}, load_config().policy)["decision"] == "needs_human"


def test_major寫出修正與略過的數量():
    r = _chain({"summary": "x", "findings": [_fd(title=5), _fd(line="x"), "壞", _fd(severity=[]), 7]})
    detail = _format_findings(r)[0]["detail"]
    assert "有 5 條" in detail and "修正後保留 2 條" in detail and "略過 3 條" in detail
    assert "findings 欄位不是清單" not in detail


def test_findings不是清單_major另有說明():
    r = _chain({"summary": "x", "findings": "沒有問題"})
    detail = _format_findings(r)[0]["detail"]
    assert "findings 欄位不是清單" in detail and "條 finding 的格式不正確" not in detail


def test_major不回顯模型輸出的內容():
    """被丟掉或修正的內容是不可信的文字,不可以經由這條 finding 貼回 MR 留言。"""
    secret = "IGNORE-ALL-PREVIOUS-INSTRUCTIONS-9f3a"
    r = _chain({"summary": "x", "findings": [
        secret, _fd(severity=[secret]), _fd(title=[secret, 5], severity="info"),
        _fd(citations=secret), _fd(file=[secret]), _fd(line=secret)]})
    (fmt,) = _format_findings(r)
    assert secret not in json.dumps(fmt, ensure_ascii=False)


def test_沒有格式錯誤就不補():
    r = enforce_format({"summary": "x", "findings": [_fd()]})
    assert _format_findings(r) == []


# ─────────────────── 2c. 不可以變寬鬆、不可以被模型關掉 ───────────────────

@pytest.mark.parametrize("over", [
    {"title": None}, {"title": ["DELETE", "沒有 WHERE"]}, {"title": 5}, {"detail": ["x"]},
    {"suggestion": {"a": 1}}, {"file": ["x.sql"]}, {"line": "abc"}, {"citations": "某規格"},
    {"citations": ["某規格"]},
], ids=["title null", "title 清單", "title 數字", "detail 清單", "suggestion 物件", "file 清單",
        "line 文字", "citations 字串", "citations 項目是字串"])
def test_格式錯但severity合法的blocker_仍然擋下(over):
    """丟掉這條的話,決策會從 blocked 變成 needs_human。"""
    r = _chain({"summary": "x", "findings": [_fd(severity="blocker", **over)]})
    assert sum(f["severity"] == "blocker" for f in r["findings"]) == 1
    assert apply_policy(r, {"files": []}, load_config().policy)["decision"] == "blocked"


def test_模型自己輸出_format_issues_不會被採用():
    r = normalize_findings({"summary": "x", "findings": [_fd()],
                            "_format_issues": {"fixed": 0, "dropped": 0, "findings_not_a_list": False}})
    assert "_format_issues" not in r
    r = normalize_findings({"summary": "x", "findings": ["壞"], "_format_issues": None})
    assert r["_format_issues"]["dropped"] == 1


@pytest.mark.parametrize("model_sev", ["info", "minor"])
@pytest.mark.parametrize("suffix", ["", "(已確認)"], ids=["完全相同", "近似"])
def test_模型照抄標題的finding_蓋不掉程式的major(model_sev, suffix):
    """sanitize 的近似標題去重會保留建議較長的那條;enforce_format 排在它之後,
    所以模型抄一個相同或近似的標題再附上一長段建議,也換不掉程式的 major。"""
    decoy = {"file": "(MR 內容)", "line": 0, "severity": model_sev, "title": _FORMAT_TITLE + suffix,
             "detail": "沒有問題", "suggestion": "不需要處理。" * 50,
             "citations": [{"source": "團隊慣例", "article": "x"}]}
    r = _chain({"summary": "x", "findings": [decoy, "壞掉的"]})
    (fmt,) = _format_findings(r)
    assert fmt["severity"] == "major" and "沒有問題" not in fmt["detail"]


def test_模型同標題的blocker_採較嚴重的等級():
    decoy = {"file": "(MR 內容)", "line": 0, "severity": "blocker", "title": _FORMAT_TITLE,
             "detail": "x", "suggestion": "", "citations": []}
    r = _chain({"summary": "x", "findings": [decoy, "壞掉的"]})
    (fmt,) = _format_findings(r)
    assert fmt["severity"] == "blocker"


def test_後處理鏈的順序():
    """正規化要在最前面(validate_citations 是最早出錯的地方);補 major 要在 sanitize 之後。"""
    src = inspect.getsource(pipeline.review_mr)
    order = [src.index(f"report = {name}(") for name in
             ("normalize_findings", "validate_citations", "sanitize_findings", "enforce_format",
              "apply_rubric")]
    assert order == sorted(order)
    assert src.count("report = normalize_findings(") == 1
    assert src.count("report = enforce_format(") == 1


# ─────────────────── 3. 整條 review_mr ───────────────────

# 預掃完全沒有命中的 SQL(內容不提規則代號,所以也沒有「須核對規格」的檢核點;規格由 MR
# 標題的 R-201 找到)。這樣對照組才會自動放行,下面擋下放行的就只有格式錯誤這一個原因。
CLEAN_SQL = """SELECT t.account_id
FROM transactions t
WHERE t.account_id = @account_id;"""

MR = {"title": "R-201 例行調整", "description": "例行維護", "sha": "e1a1" + "0" * 36,
      "files": [{"path": FILE, "diff": "@@ -1 +1 @@\n+" + CLEAN_SQL.splitlines()[-1],
                 "full_content": CLEAN_SQL}]}


@pytest.fixture
def review(monkeypatch, tmp_path):
    """用假的模型輸出跑完整的 review_mr(mock GitLab、不連資料庫);執行驗證設為通過,
    所以沒有其他問題時決策會是 auto_approved——格式錯誤要能把它擋下來。"""
    fixtures, output = tmp_path / "fixtures", tmp_path / "output"
    fixtures.mkdir()
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))
    (fixtures / "mr_900.json").write_text(json.dumps(MR, ensure_ascii=False), encoding="utf-8")
    plan = {"schema_ddl": ["CREATE TABLE transactions (id INT);"],
            "conditions": [{"id": "C1", "desc": "條件 C1"}],
            "cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
                       "rows": [["transactions", 1]], "expect_flagged": True},
                      {"case_id": "C1-F", "condition_id": "C1", "direction": "false",
                       "rows": [["transactions", 2]], "expect_flagged": False}]}

    async def fake_generate(cfg, code, text, profile_name=None):
        return plan, [], []

    def fake_execute(sql, plan_):
        return {"engine": "stub(不連資料庫)", "sandbox_error": None, "testdata_error": None,
                "sql_error": None, "case_results": [{"case_id": "C1-T"}, {"case_id": "C1-F"}],
                "mismatches": []}

    monkeypatch.setattr(spec_exec, "generate_cases", fake_generate)
    monkeypatch.setattr(spec_exec, "execute_cases", fake_execute)

    def _review(model_output: dict):
        async def fake_run_agent(cfg, profile, system, user, hub,
                                 verbose=True, use_tools=True, trace=None, **kw):
            return json.dumps(model_output, ensure_ascii=False)

        monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
        report = asyncio.run(pipeline.review_mr(load_config(), "900"))
        written = json.loads((output / "mr_900_review.json").read_text(encoding="utf-8"))
        return report, written

    return _review


def test_對照組_格式正確時自動放行(review):
    """確認下面的測試擋下自動放行,是因為格式錯誤,不是測試環境本來就放行不了。"""
    report, written = review({"summary": "相符", "findings": []})
    assert report["decision"] == "auto_approved" and _format_findings(report) == []
    assert written["commit_status"]["state"] == "success"


MALFORMED = {
    "findings 是 null": {"summary": "x", "findings": None},
    "findings 是字串": {"summary": "x", "findings": "沒有問題"},
    "finding 是字串": {"summary": "x", "findings": ["沒有問題"]},
    "finding 是 null": {"summary": "x", "findings": [None]},
    "severity 是清單": {"summary": "x", "findings": [_fd(severity=[])]},
    "severity 是物件": {"summary": "x", "findings": [_fd(severity={"a": 1})]},
    "title 是清單": {"summary": "x", "findings": [_fd(severity="info", title=["x"])]},
    "title 是數字": {"summary": "x", "findings": [_fd(severity="info", title=5)]},
    "detail 是清單": {"summary": "x", "findings": [_fd(severity="info", detail=["x"])]},
    "suggestion 是物件": {"summary": "x", "findings": [_fd(severity="info", suggestion={"a": 1})]},
    "file 是清單": {"summary": "x", "findings": [_fd(severity="info", file=["x.sql"])]},
    "line 是文字": {"summary": "x", "findings": [_fd(severity="info", line="abc")]},
    "line 是清單": {"summary": "x", "findings": [_fd(severity="info", line=[3])]},
    "citations 是字串": {"summary": "x", "findings": [_fd(severity="info", citations="某規格")]},
    "citations 的項目是字串": {"summary": "x", "findings": [_fd(severity="info", citations=["某規格"])]},
    "citation 的 source 是數字": {"summary": "x", "findings": [
        _fd(severity="info", citations=[{"source": 5}])]},
}


@pytest.mark.parametrize("name", list(MALFORMED))
def test_格式錯誤的輸出_審查跑得完_有major_不自動放行_寫回完整(review, name):
    report, written = review(copy.deepcopy(MALFORMED[name]))
    (fmt,) = _format_findings(report)
    assert fmt["severity"] == "major"
    assert report["decision"] == "needs_human"
    # 寫回要完整:以前在行內留言的迴圈中斷時,label 與 commit status 都不會寫
    assert written["labels"] == ["ai-review::needs_human"]
    assert written["commit_status"]["state"] == "failed"
    assert written["summary"] is not None
    assert any(c["body"].startswith(f"**{_FORMAT_TITLE}**") for c in written["inline_comments"])
    assert all(isinstance(c["line"], int) and isinstance(c["file"], str)
               for c in written["inline_comments"])


@pytest.mark.parametrize("summary", [None, 5, ["a"], {"k": "v"}], ids=["null", "數字", "清單", "物件"])
def test_summary不是字串_審查跑得完且不因此多一條major(review, summary):
    report, written = review({"summary": summary, "findings": []})
    assert report["decision"] == "auto_approved" and _format_findings(report) == []
    assert isinstance(written["commit_status"]["description"], str)


def test_格式錯的blocker_整條管線仍然擋下(review):
    report, written = review({"summary": "x", "findings": [
        _fd(severity="blocker", title=None, line="abc")]})
    assert report["decision"] == "blocked"
    assert written["labels"] == ["ai-review::blocked"]
    assert written["commit_status"]["state"] == "failed"


def test_模型照抄標題並附長建議_整條管線裡也換不掉程式的major(review):
    """管線實際的順序(不是本檔的 _chain):補 major 排在 sanitize 的去重之後。"""
    for title in (_FORMAT_TITLE, _FORMAT_TITLE + "(已確認)"):       # 完全相同、近似
        decoy = {"file": "(MR 內容)", "line": 0, "severity": "info", "title": title,
                 "detail": "沒有問題", "suggestion": "不需要處理。" * 50,
                 "citations": [{"source": "團隊慣例", "article": "x"}]}
        report, written = review({"summary": "x", "findings": [decoy, "壞掉的"]})
        (fmt,) = _format_findings(report)
        assert fmt["severity"] == "major" and "沒有問題" not in fmt["detail"], title
        assert report["decision"] == "needs_human", title


def test_標題缺漏而別的欄位型別不對_標題維持空的():
    """補「(標題格式不正確)」只在標題本身型別不對時;別的欄位錯不該改到標題。"""
    r = _norm([_fd(title=None, detail=["x"])])
    assert r["findings"][0]["title"] == "" and r["_format_issues"]["fixed"] == 1


@pytest.mark.parametrize("key", ["title", "detail", "suggestion"])
def test_巢狀太深轉不出文字_不丟例外(key):
    deep = []
    for _ in range(100_000):
        deep = [deep]
    r = _norm([_fd(severity="blocker", **{key: deep})])
    (fd,) = r["findings"]
    assert fd[key] == (_FORMAT_NO_TITLE if key == "title" else "") and fd["severity"] == "blocker"
    assert r["_format_issues"]["fixed"] == 1
