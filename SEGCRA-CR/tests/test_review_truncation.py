"""主審查模型的輸出被截斷時,審查結果要看得出來(#26 第 1 點)。

回應被輸出長度上限切斷時,`extract_json` 的修補退路會把殘缺的 JSON 補成合法物件:
後半的 finding 不見了,報告卻一切正常。這裡驗的是「截斷 → 一定有一條 major、不會自動放行、
也不會讓整次審查中斷」,以及沒有截斷時行為完全不變。

整條 `review_mr` 跑真的,只換掉會呼叫模型與要連資料庫的點(做法同
`test_pipeline_integration.py`);假的 `run_agent` 會照 #18 的約定回填 `meta`。
"""
import asyncio
import copy
import json

import pytest

from orchestrator import pipeline, spec_exec
from orchestrator.config import load_config
from toolbox import gitlab

LIMIT = load_config().profile(None).max_output_tokens
TITLE = pipeline._TRUNCATED_TITLE

CLEAN_SQL = """-- R-201: 單日 ATM 現金提領累計達 100000(含)通報
SELECT t.account_id, SUM(t.amount) AS total_amount
FROM transactions t
WHERE t.tx_type = 'WITHDRAW' AND t.channel = 'ATM'
  AND t.tx_time >= @start_date AND t.tx_time < @end_date
GROUP BY t.account_id
HAVING SUM(t.amount) >= 100000;"""

# 預掃一定會命中 R001(DELETE 無 WHERE)
PURGE_SQL = """-- R-201 相關的清理腳本
DELETE FROM transactions;"""

PATH = "sql/maintenance/x.sql"

# 模型在檔案上回應了檢核點:沒有截斷時這份報告會自動放行,是「截斷會擋下放行」的對照組
RESPONDED = {"file": PATH, "line": 1, "severity": "info",
             "title": "已與核定規格 R-201 逐項核對",
             "detail": "門檻 >= 100000 與規格「達」一致;時間窗與通報粒度相符。",
             "suggestion": "", "citations": []}
CLEAN_REPORT = {"score": 100, "verdict": "approve", "summary": "實作與核定規格相符。",
                "findings": [RESPONDED]}

# 三條 finding,最嚴重的排在最後:截斷時最先消失的就是它
THREE = {"score": 40, "verdict": "needs_changes", "summary": "有三個問題。", "findings": [
    {"file": PATH, "line": 2, "severity": "minor", "title": "欄位別名不一致",
     "detail": "total_amount 與其他報表的命名不同。", "suggestion": "", "citations": []},
    {"file": PATH, "line": 4, "severity": "major", "title": "缺少通路條件的說明",
     "detail": "channel 的取值沒有註解。", "suggestion": "", "citations": []},
    {"file": PATH, "line": 7, "severity": "blocker", "title": "門檻與核定規格不符",
     "detail": "規格寫「達 100000」,這裡的比較方向需要確認。", "suggestion": "",
     "citations": []}]}
THREE_TEXT = json.dumps(THREE, ensure_ascii=False)


def _mr(sql: str = CLEAN_SQL, path: str = PATH) -> dict:
    return {"title": "R-201 例行調整", "description": "例行維護", "sha": "e1a1" + "0" * 36,
            "files": [{"path": path, "diff": "@@ -1 +1 @@\n+" + sql.splitlines()[-1],
                       "full_content": sql}]}


@pytest.fixture
def review(monkeypatch, tmp_path):
    """回傳 `review(raw, meta, mr=...)`:raw 是模型輸出的原文,meta 是 run_agent 回填的內容
    (None 表示這個端點什麼都沒回報)。回傳 (report, capture, output 目錄)。"""
    fixtures, output = tmp_path / "fixtures", tmp_path / "output"
    fixtures.mkdir()
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

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

    async def fake_arbitrate(cfg, spec_text, sql, mm, profile_name=None):
        return {"who_is_wrong": "sql", "reason": "不會走到"}

    monkeypatch.setattr(spec_exec, "generate_cases", fake_generate)
    monkeypatch.setattr(spec_exec, "execute_cases", fake_execute)
    monkeypatch.setattr(spec_exec, "arbitrate", fake_arbitrate)

    def _review(raw, meta, mr=None):
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)
        (fixtures / "mr_900.json").write_text(
            json.dumps(mr or _mr(), ensure_ascii=False), encoding="utf-8")
        seen = {}

        async def fake_run_agent(cfg, profile, system, user, hub, **kw):
            seen["meta_passed"] = "meta" in kw
            if meta is not None and kw.get("meta") is not None:
                kw["meta"].update(meta)
            return raw

        monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
        capture = {}
        report = asyncio.run(pipeline.review_mr(load_config(), "900", capture=capture))
        assert seen["meta_passed"], "主審查的呼叫沒有把 meta 傳給 run_agent"
        return report, capture, output

    return _review


def _cut_findings(report):
    return [f for f in report["findings"] if f.get("title") == TITLE]


def _others(report):
    return [f for f in report["findings"] if f.get("title") != TITLE]


STOP = {"finish_reason": "stop", "completion_tokens": 500}
LENGTH = {"finish_reason": "length", "completion_tokens": LIMIT}


# ─────────────────── 對照組:沒有截斷時行為不變 ───────────────────

def test_沒有截斷時不補任何東西而且照常自動放行(review):
    report, capture, _ = review(CLEAN_REPORT, STOP)
    assert _cut_findings(report) == []
    assert report["decision"] == "auto_approved", report.get("_policy_signals")
    assert capture["output_meta"] == STOP
    assert report["_output_meta"] == {"finish_reason": "stop", "completion_tokens": 500,
                                      "max_output_tokens": LIMIT, "truncated": False}


def test_端點什麼都沒回報時當成沒有截斷(review):
    report, capture, _ = review(CLEAN_REPORT, None)
    assert _cut_findings(report) == []
    assert report["decision"] == "auto_approved"
    assert capture["output_meta"] == {}
    assert report["_output_meta"] == {"finish_reason": None, "completion_tokens": None,
                                      "max_output_tokens": LIMIT, "truncated": False}


@pytest.mark.parametrize("meta", [
    {"finish_reason": None, "completion_tokens": None},
    {"finish_reason": "stop", "completion_tokens": LIMIT - 1},
    {"finish_reason": "eos", "completion_tokens": 10},            # 沒看過的值:不報
    {"finish_reason": "<b>length</b>", "completion_tokens": 10},
    {"finish_reason": ["length"], "completion_tokens": 10},       # 不是字串也不能出錯
    {"finish_reason": "stop", "completion_tokens": str(LIMIT)},   # 不是整數的用量不採計
    {"finish_reason": "stop", "completion_tokens": True},
    {"finish_reason": "stop", "completion_tokens": float(LIMIT)},
])
def test_不算截斷的回報(review, meta):
    report, _, _ = review(CLEAN_REPORT, meta)
    assert _cut_findings(report) == []
    assert report["decision"] == "auto_approved"


def test_沒有截斷卻解析不出來時照舊明確失敗(review):
    with pytest.raises(RuntimeError, match="無法解析"):
        review("我想了一下,這個 MR 看起來沒問題。", STOP)
    with pytest.raises(RuntimeError, match="無法解析"):
        review("我想了一下,這個 MR 看起來沒問題。", None)


# ─────────────────── 截斷:一定看得到、不會自動放行 ───────────────────

def test_截斷時補一條_major_並擋下自動放行(review):
    report, _, _ = review(CLEAN_REPORT, LENGTH)
    cut = _cut_findings(report)
    assert len(cut) == 1
    assert cut[0]["severity"] == "major" and cut[0]["file"] == "(MR 內容)"
    assert str(LIMIT) in cut[0]["detail"]
    assert report["decision"] == "needs_human"
    # 已解析出來的 finding 保留
    assert RESPONDED["title"] in [f["title"] for f in _others(report)]
    assert report["_output_meta"] == {"finish_reason": "length", "completion_tokens": LIMIT,
                                      "max_output_tokens": LIMIT, "truncated": True}


def test_加這一步不會改到其他_finding(review):
    """同一份輸出,有沒有截斷只差那一條 major:其他步驟補的、保留的內容逐欄相同。"""
    mr = _mr(PURGE_SQL, "sql/maintenance/purge.sql")
    normal, _, _ = review(THREE, STOP, mr)
    cut, _, _ = review(THREE, LENGTH, mr)
    assert _others(cut) == normal["findings"]
    assert len(_cut_findings(cut)) == 1


@pytest.mark.parametrize("meta", [
    {"finish_reason": "stop", "completion_tokens": LIMIT},        # 端點沒有照規格回 length
    {"finish_reason": None, "completion_tokens": LIMIT},
    {"finish_reason": "stop", "completion_tokens": LIMIT + 7},
    {"finish_reason": "length", "completion_tokens": None},
    {"finish_reason": "length"},
])
def test_用量達到上限或回報_length_都算截斷(review, meta):
    report, _, _ = review(CLEAN_REPORT, meta)
    assert len(_cut_findings(report)) == 1
    assert report["decision"] == "needs_human"


def test_後半的_finding_被切掉時前半保留並轉人工(review):
    """切在第三條(blocker)中間:修補後只剩前兩條,決策原本會從 blocked 悄悄變寬鬆。"""
    cut_at = THREE_TEXT.index("門檻與核定規格不符") + 3
    report, _, _ = review(THREE_TEXT[:cut_at], LENGTH)
    titles = [f["title"] for f in _others(report)]
    assert "欄位別名不一致" in titles and "缺少通路條件的說明" in titles
    assert len(_cut_findings(report)) == 1
    assert report["decision"] != "auto_approved"


def test_截斷到解析不出來時不中斷而且規則命中仍在(review):
    """切在第一個 } 之前,extract_json 回 None;以前會丟例外,MR 收不到任何結果。"""
    raw = THREE_TEXT[:THREE_TEXT.index("}")]
    assert pipeline.extract_json(raw) is None
    report, _, output = review(raw, LENGTH, _mr(PURGE_SQL, "sql/maintenance/purge.sql"))
    assert len(_cut_findings(report)) == 1
    assert report["summary"] == pipeline._TRUNCATED_SUMMARY
    assert any("R001" in f["title"] for f in report["findings"]), "規則命中跟著模型輸出一起不見了"
    assert report["decision"] in ("needs_human", "blocked")
    # 結果有寫回去(留言、總結、status 都在)
    written = "".join(p.read_text(encoding="utf-8") for p in output.iterdir()
                      if p.is_file() and "raw" not in p.name)
    assert TITLE in written


@pytest.mark.parametrize("fraction", [0.02, 0.15, 0.3, 0.45, 0.6, 0.75, 0.9, 0.995])
def test_切在各種位置整條管線都走得完(review, fraction):
    raw = THREE_TEXT[:max(1, int(len(THREE_TEXT) * fraction))]
    report, _, _ = review(raw, LENGTH)
    assert len(_cut_findings(report)) == 1
    assert report["decision"] in ("needs_human", "blocked")


@pytest.mark.parametrize("text", [THREE_TEXT,
                                  "```json\n" + json.dumps(THREE, ensure_ascii=False, indent=2)
                                  + "\n```"])
def test_切在每一個字元位置都不出錯而且都有那條_major(text):
    """不走整條管線(太慢),但逐一走過會讀 finding 內容的那幾步。"""
    for cut_at in range(1, len(text) + 1):
        report = pipeline._parse_model_output(text[:cut_at], LENGTH, LIMIT)
        assert report, f"切在 {cut_at}:截斷時不該回 None"
        report = pipeline.validate_citations(report, "R-201")
        report = pipeline.sanitize_findings(report)
        report = pipeline.enforce_truncation(report, LENGTH, LIMIT)
        report = pipeline.apply_rubric(report)
        assert len(_cut_findings(report)) == 1, f"切在 {cut_at}"
        assert report["verdict"] != "approve", f"切在 {cut_at}"


# ─────────────────── 這條 major 不能被模型關掉或擠掉 ───────────────────

def test_模型自己輸出同標題的_info_會被程式的取代(review):
    fake = {"file": "(MR 內容)", "line": 0, "severity": "info", "title": TITLE,
            "detail": "輸出完整,沒有截斷。", "suggestion": "", "citations": []}
    report, _, _ = review({**CLEAN_REPORT, "findings": [RESPONDED, fake]}, LENGTH)
    cut = _cut_findings(report)
    assert len(cut) == 1 and cut[0]["severity"] == "major"
    assert "輸出完整" not in cut[0]["detail"]
    assert report["decision"] == "needs_human"


def test_模型那條比較嚴重時採較嚴重的等級(review):
    fake = {"file": "(MR 內容)", "line": 0, "severity": "blocker", "title": TITLE,
            "detail": "x", "suggestion": "", "citations": []}
    report, _, _ = review({**CLEAN_REPORT, "findings": [RESPONDED, fake]}, LENGTH)
    cut = _cut_findings(report)
    assert len(cut) == 1 and cut[0]["severity"] == "blocker"


def test_近似標題加上很長的建議也擠不掉(review):
    """sanitize_findings 的近似標題去重會留下建議較長的那條;程式的要排在它之後才補。"""
    near = {"file": "(MR 內容)", "line": 0, "severity": "info",
            "title": TITLE + "嗎", "detail": "沒有。", "suggestion": "不需要處理。" * 60,
            "citations": []}
    report, _, _ = review({**CLEAN_REPORT, "findings": [RESPONDED, near]}, LENGTH)
    cut = _cut_findings(report)
    assert len(cut) == 1 and cut[0]["severity"] == "major"
    assert report["decision"] == "needs_human"


def test_補的文字不回顯模型輸出也不回顯端點的字串(review):
    marker = "ZZ_MODEL_TEXT_ZZ"
    raw = json.dumps({**CLEAN_REPORT, "summary": marker}, ensure_ascii=False)
    report, _, _ = review(raw[:raw.index(marker) + 5], {"finish_reason": "length",
                                                       "completion_tokens": LIMIT})
    cut = _cut_findings(report)[0]
    text = json.dumps(cut, ensure_ascii=False)
    assert "ZZ_MO" not in text and "length" not in text


# ─────────────────── 判斷本身 ───────────────────

@pytest.mark.parametrize("meta, limit, expected", [
    ({"finish_reason": "length"}, 8192, True),
    ({"finish_reason": "length"}, None, True),
    ({"finish_reason": "stop", "completion_tokens": 8192}, 8192, True),
    ({"finish_reason": "stop", "completion_tokens": 8191}, 8192, False),
    ({"completion_tokens": 8192}, 8192, True),
    ({"completion_tokens": 0}, 0, False),             # 上限沒設定(0)時不拿用量來判斷
    ({"completion_tokens": 5}, -1, False),
    ({"completion_tokens": 5}, None, False),
    ({"completion_tokens": 5}, "5", False),
    ({"completion_tokens": True}, 1, False),
    ({"completion_tokens": 5}, True, False),
    ({}, 8192, False),
])
def test_截斷的判斷(meta, limit, expected):
    assert pipeline._output_truncated(meta, limit) is expected


def test_沒有截斷時只多一個稽核欄位():
    report = pipeline.enforce_truncation(copy.deepcopy(THREE), STOP, LIMIT)
    assert report.pop("_output_meta")["truncated"] is False
    assert report == THREE
    assert pipeline._parse_model_output(THREE_TEXT, STOP, LIMIT) == THREE
    assert pipeline._parse_model_output("不是 JSON", STOP, LIMIT) is None


def test_上限不是整數時說明裡不寫數字():
    report = pipeline.enforce_truncation({"findings": []}, {"finish_reason": "length"}, None)
    assert "None" not in report["findings"][0]["detail"]
    assert "上限" in report["findings"][0]["detail"]


# ─────────────────── 程式補的文字不能被當成模型的回應 ───────────────────

def test_空報告不會讓檢核點被當成已回應(review):
    """enforce_hints 以報告全文含關鍵字判斷模型有沒有回應;空報告的摘要是程式寫的,
    而且在 enforce_hints 之前就在報告裡——它若含關鍵字,檢核點會被悄悄吞掉。"""
    normal, _, _ = review({"summary": "x", "findings": []}, STOP)
    hints = [f["title"] for f in normal["findings"] if "模型未回應" in f["title"]]
    assert hints, "對照組沒有任何檢核點,這個測試驗不到東西"
    raw = THREE_TEXT[:THREE_TEXT.index("}")]
    report, _, _ = review(raw, LENGTH)
    assert [f["title"] for f in report["findings"] if "模型未回應" in f["title"]] == hints


def test_程式寫的摘要不含任何檢核點的關鍵字():
    for code, words in pipeline._HINT_KEYWORDS.items():
        assert not [w for w in [code, *words] if w in pipeline._TRUNCATED_SUMMARY], code


def test_突變點登記在工具的進入點之前():
    """mutate.py 以腳本執行時,寫在 `if __name__ == "__main__":` 之後的突變點不會被登記,
    `--only "截斷:"` 會回報 0 個突變而且看起來一切正常。"""
    import pathlib
    text = (pathlib.Path(__file__).parent / "tools" / "mutate.py").read_text(encoding="utf-8")
    guard = text.index('if __name__ == "__main__":')
    assert 0 < text.index('"截斷:') < guard
    assert "MUTANTS.update(" not in text[guard:]


# ─────────────────── 報告裡的稽核紀錄 ───────────────────

def test_blocked_的報告也有稽核紀錄(review):
    """評測靠存下來的報告統計截斷次數;被擋下的 MR 也要算得到。"""
    report, _, _ = review(THREE, LENGTH)
    assert report["decision"] == "blocked"
    assert report["_output_meta"]["truncated"] is True


def test_模型自己輸出的同名欄位會被蓋掉(review):
    forged = {**CLEAN_REPORT, "_output_meta": {"finish_reason": "stop", "truncated": False,
                                               "note": "ZZ_FORGED_ZZ"}}
    report, _, _ = review(forged, LENGTH)
    assert report["_output_meta"]["truncated"] is True
    assert "ZZ_FORGED_ZZ" not in json.dumps(report["_output_meta"], ensure_ascii=False)


@pytest.mark.parametrize("reason, recorded", [
    ("stop", "stop"), ("length", "length"), ("tool_calls", "tool_calls"),
    ("content_filter", "content_filter"), ("function_call", "function_call"),
    (None, None),
    ("eos", "other"), ("<script>alert(1)</script>", "other"), ("", "other"),
    (["length"], "other"), ({"a": 1}, "other"), (7, "other"), ("STOP", "other"),
])
def test_端點回的結束原因只記認得的值(reason, recorded):
    record = pipeline._output_meta_record({"finish_reason": reason}, LIMIT)
    assert record["finish_reason"] == recorded


@pytest.mark.parametrize("used, limit, expected", [
    (500, 8192, (500, 8192)), (0, 8192, (0, 8192)),
    ("500", 8192, (None, 8192)), (True, 8192, (None, 8192)), (5.0, 8192, (None, 8192)),
    ([1], 8192, (None, 8192)), (500, None, (500, None)), (500, "8192", (500, None)),
])
def test_稽核紀錄的數字只收整數(used, limit, expected):
    record = pipeline._output_meta_record({"completion_tokens": used}, limit)
    assert (record["completion_tokens"], record["max_output_tokens"]) == expected
    json.dumps(record)      # 一定能序列化(評測會把整份報告寫成檔)


def test_稽核紀錄不會被當成模型對檢核點的回應():
    """enforce_hints / enforce_style 以報告全文含關鍵字判斷;這份紀錄的欄位名與可能的值
    都不含它們的關鍵字(紀錄也排在它們之後才寫)。"""
    text = json.dumps([pipeline._output_meta_record({"finish_reason": r, "completion_tokens": 1}, 2)
                       for r in (*pipeline._KNOWN_FINISH, None, "x")], ensure_ascii=False)
    for code, words in pipeline._HINT_KEYWORDS.items():
        assert not [w for w in [code, *words] if w in text], code
    assert "逗號" not in text
