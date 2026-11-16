"""eval/measure_hint_tags.py:舊做法(關鍵詞)與新做法(標題的代碼)各補幾條 —— 不呼叫模型。

拿真的 golden case 重跑規則層,配一份假的報告:模型的回應只有關鍵詞、沒有代碼時,
舊做法算回應過、新做法補「模型未回應」,而且這份報告會從可能自動放行變成交人工。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import measure_hint_tags as m  # noqa: E402


def _case_with_h003():
    for g in sorted((ROOT / "eval" / "golden").glob("mr_*.json")):
        case = json.loads(g.read_text(encoding="utf-8"))
        pre = m._prescan(case)
        for e in pre:
            if [h for h in e["rules"] if h.get("rule") == "H003"] and m._hints(pre) == 1:
                return g.name, e["path"]
    raise AssertionError("golden set 裡應該有只命中一處 H003 的案例")


def _write(tmp_path, name, findings):
    (tmp_path / name).write_text(json.dumps({"findings": findings}, ensure_ascii=False),
                                 encoding="utf-8")


def test_只有關鍵詞的回應_舊做法算回應過_新做法補報(tmp_path, capsys):
    name, path = _case_with_h003()
    _write(tmp_path, name, [{"file": path, "severity": "info", "title": "已與核定規格逐項核對"}])
    assert m.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "舊做法補 0 條、新做法補 1 條;因此改成交人工的報告 1 份" in out, out


def test_標了代碼的回應_兩種做法都不補(tmp_path, capsys):
    name, path = _case_with_h003()
    _write(tmp_path, name, [{"file": path, "severity": "info",
                             "title": "[H003] 已與核定規格逐項核對"}])
    assert m.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "舊做法補 0 條、新做法補 0 條;因此改成交人工的報告 0 份" in out, out
    assert "模型標代碼 1 條" in out


def test_程式補的_finding_不算模型的回應(tmp_path, capsys):
    """存下來的報告已經有程式補的「[H003] 檢核點待人工確認」;不濾掉的話新做法會把它當成回應。"""
    name, path = _case_with_h003()
    _write(tmp_path, name, [{"file": path, "severity": "info", "_source": "program",
                             "title": "[H003] 檢核點待人工確認(模型未回應)", "detail": "x"}])
    assert m.main([str(tmp_path)]) == 0
    assert "新做法補 1 條" in capsys.readouterr().out


def test_規格待補_分開算照格式的與靠用字命中的():
    spec = (ROOT / "specs" / "R-140.md").read_text(encoding="utf-8")
    findings = [{"file": "a.sql", "severity": "info", "title": "規格未載明:幣別", "detail": "x"},
                {"file": "a.sql", "severity": "info", "title": "規格未載明:退匯", "detail": "x"},
                {"file": "a.sql", "severity": "major", "title": "門檻",
                 "detail": "外幣交易未依幣別折算"}]
    assert m._pending_counts(findings, spec) == {"tagged": 2, "listed": 1, "unlisted": 1,
                                                 "fallback": 1}


def test_找不到報告時回報而不是當成零():
    assert m.main([str(ROOT / "eval" / "不存在的目錄")]) == 1
