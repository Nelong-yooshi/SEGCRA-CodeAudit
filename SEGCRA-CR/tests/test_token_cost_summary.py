"""eval/token_cost_summary.py 的統計邏輯 —— #44 全文開銷評估用的那支,不呼叫 LLM。

這支的數字會拿去判斷「全文這筆 token 開銷值不值」,所以它自己得準:
  1. 只認主審查回填的真實 token;沒回填的那幾份不能當成 0 混進去(會把開銷算少)。
  2. 「模型寫了多少字」要把程式補的 finding 剔掉——那是確定性後處理加的,不是模型生成的。
  3. bool 不能被當成數字(JSON 裡的 true/false 若落到 token 欄位,isinstance(x, int) 會中)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import token_cost_summary as tcs  # noqa: E402


def _om(p_last=100, g_last=50, p_tot=400, g_tot=120, rounds=0):
    return {"prompt_tokens": p_last, "completion_tokens": g_last,
            "prompt_tokens_total": p_tot, "completion_tokens_total": g_tot,
            "rounds_without_usage": rounds}


def _report(summary="", findings=None, om=None):
    r = {"summary": summary, "findings": findings or []}
    if om is not None:
        r["_output_meta"] = om
    return r


def test_回填齊全_抓到主審查的單次與加總():
    m = tcs.metrics(_report(om=_om(p_last=120, g_last=60, p_tot=500, g_tot=180)))
    assert m["backfilled"] is True
    assert (m["prompt_last"], m["gen_last"]) == (120, 60)
    assert (m["prompt_total"], m["gen_total"]) == (500, 180)


def test_程式補的finding不算進模型寫的字():
    model = {"title": "門檻與規格不符", "detail": "第 20 行條件寫錯", "suggestion": "改成 >=",
             "_source": "model"}
    program = {"title": "[R001] DELETE 無 WHERE", "detail": "x" * 999, "_source": tcs.PROGRAM_SOURCE}
    m = tcs.metrics(_report(summary="摘要", findings=[model, program], om=_om()))
    # 報告文字只該含摘要與「模型那條」的字;程式補的那條(即使很長)不算
    assert m["report_body_tokens"] == tcs.est_tokens("摘要" + " " + tcs._finding_text(model))
    assert (m["n_findings"], m["n_model"], m["n_program"]) == (2, 1, 1)
    # 說明平均字數也只看模型那條
    assert m["detail_chars_avg"] == len("第 20 行條件寫錯")


def test_沒有output_meta的舊報告_不進真實token統計但文字量照算():
    m = tcs.metrics(_report(summary="摘要", findings=[{"title": "甲", "_source": "model"}]))
    assert m["backfilled"] is False
    assert m["prompt_last"] is None and m["gen_total"] is None and m["gen_outside_report"] is None
    assert m["report_body_tokens"] == tcs.est_tokens("摘要" + " " + tcs._finding_text({"title": "甲"}))
    assert m["n_findings"] == 1


def test_有輪次沒回報用量_標成total不完整():
    assert tcs.metrics(_report(om=_om(rounds=2)))["incomplete"] is True
    assert tcs.metrics(_report(om=_om(rounds=0)))["incomplete"] is False


def test_bool不能被當成回填的token數():
    # JSON 的 true 落到 token 欄位時,isinstance(True, int) 會是 True;要擋掉,否則這份會被
    # 當成有回填、把 True(=1)加進總量
    om = _om()
    om["prompt_tokens_total"] = True
    assert tcs.metrics(_report(om=om))["backfilled"] is False


def test_生成未落報告的量下限是0():
    # 生成總量很小、報告文字很長時,差額不應該是負數
    big = {"title": "說明" * 200, "_source": "model"}
    m = tcs.metrics(_report(summary="", findings=[big], om=_om(g_tot=5)))
    assert m["gen_outside_report"] == 0


def test_summarize彙總與分位():
    reports = [
        _report(om=_om(p_tot=100, g_tot=10)),
        _report(om=_om(p_tot=300, g_tot=30)),
        _report(om=_om(p_tot=500, g_tot=50)),
        _report(findings=[{"title": "x", "_source": "model"}]),   # 無回填
    ]
    s = tcs._summarize_reports(reports)
    assert s["reports"] == 4 and s["backfilled"] == 3
    assert s["prompt_total"]["sum"] == 900         # 無回填那份沒被加成 0
    assert s["prompt_total"]["n"] == 3
    assert s["prompt_total"]["p50"] == 300
    assert s["gen_total"]["sum"] == 90


def test_reports跳過沒有findings的json(tmp_path):
    (tmp_path / "mr_1.json").write_text('{"summary":"s","findings":[],"_output_meta":%s}'
                                        % tcs.json.dumps(_om()), encoding="utf-8")
    (tmp_path / "result.json").write_text('{"recall": 0.9}', encoding="utf-8")   # 不是報告
    (tmp_path / "broken.json").write_text("{ not json", encoding="utf-8")
    names = [name for name, _ in tcs._reports([tmp_path])]
    assert names == ["mr_1"]


def test_兩組各自彙總_互不影響():
    a = tcs._summarize_reports([_report(om=_om(p_tot=100))])
    b = tcs._summarize_reports([_report(om=_om(p_tot=900))])
    assert a["prompt_total"]["sum"] == 100
    assert b["prompt_total"]["sum"] == 900
