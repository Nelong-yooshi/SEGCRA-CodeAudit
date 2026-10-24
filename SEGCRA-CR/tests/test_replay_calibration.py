"""重放工具自己的測試 — 不呼叫 LLM、不連資料庫。

**為什麼要測工具**:這支程式是「改動校準之後送審前的門檻」。它算錯的話,綠燈就是假的
——而假綠燈比沒有這支工具更糟(我們會以為量過了)。前例:#55 的第一版校準有 46 條單元
測試、33 個突變點全綠,真實報告上卻少抓 292 個植入的缺陷;這支工具就是補那個盲點的,
所以它自己的尺要先準。

驗三件事:
  1. **還原**:校準過的報告要還原得回校準前(拿掉 _needs_human、把降級改回去)。
  2. **三個數字**:自動放行、抓到的預期缺陷、誤報,各自算得對。
  3. **閘門**:抓到的缺陷變少、或自動放行變多時,一定要回非零(送審前擋下來)。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import replay_calibration as rc  # noqa: E402

POLICY = {"auto_approve": {"max_diff_lines": 999, "min_score": 0,
                           "allowed_severities": ["info", "minor", "major"],
                           "forbid_pending_hints": False},
          "block": {"min_blockers": 1}}


def _finding(title="實作與核定規格不符", severity="major", file="a.sql", detail="d", **over):
    f = {"file": file, "line": 1, "severity": severity, "title": title,
         "detail": detail, "suggestion": "", "citations": []}
    f.update(over)
    return f


# ─────────────────── 1. 還原 ───────────────────

def test_還原拿掉要人看的理由與降級():
    """校準之後存的報告,要還原得回校準前,否則「改前」那一欄是錯的。"""
    report = {"findings": [_finding(title="語法錯誤", severity="major")],
              "_needs_human": ["指控的「無法執行」已被沙盒否證"],
              "_calibration": {"annotated": [],
                               "downgraded": [{"title": "語法錯誤", "file": "a.sql",
                                               "from": "blocker", "to": "major",
                                               "by": "sandbox-ran-ok"}]}}
    out = rc.restore_precalibration(report)
    assert out["findings"][0]["severity"] == "blocker", "降級要改回去"
    assert "_needs_human" not in out and "_calibration" not in out
    # 不改到傳進來的那份(重放要拿同一份報告算兩次)
    assert report["findings"][0]["severity"] == "major"


def test_沒有校準紀錄的報告原樣就是改前():
    report = {"findings": [_finding()], "decision": "needs_human"}
    assert rc.restore_precalibration(report)["findings"][0]["severity"] == "major"


def test_加註不需要還原():
    """加註只動內文,不影響分數與決策;會影響決策的是 _needs_human,那個有拿掉。"""
    report = {"findings": [_finding(detail="原文(執行驗證未重現:…)")],
              "_calibration": {"annotated": [{"title": "實作與核定規格不符"}],
                               "downgraded": []}}
    out = rc.restore_precalibration(report)
    assert "執行驗證未重現" in out["findings"][0]["detail"]
    assert out["findings"][0]["severity"] == "major"


# ─────────────────── 2. 舊報告的相容處理 ───────────────────

def test_舊報告補上程式來源():
    """校準之前存的報告沒有 _source,程式補的 finding 會被當成模型的指控拿去對照,
    重放的結果就會比實際誇大。"""
    report = {"findings": [_finding(title="執行驗證失敗:實作與規格行為不符"),
                           _finding(title="[R002] SELECT * — 請明列欄位"),
                           _finding(title="實作與核定規格不符")]}
    out = rc.tag_program_findings(report)
    assert out["findings"][0]["_source"] == "program"
    assert out["findings"][1]["_source"] == "program"
    assert "_source" not in out["findings"][2], "模型寫的那條不可以被標成程式補的"


def test_已經有來源欄位的報告不被覆蓋():
    report = {"findings": [_finding(title="執行驗證失敗:x"),
                           _finding(title="模型的指控", _source="program")]}
    out = rc.tag_program_findings(report)
    assert "_source" not in out["findings"][0], "新報告以欄位為準,不要再套舊的啟發式"


@pytest.mark.parametrize("se,expect", [
    (None, "沒有 _spec_exec"),
    ({"no_spec": True}, "沒有規格"),
    ({"conditions": []}, "沒有拆出條件"),
    ({"conditions": [{"id": "C1", "desc": "x"}], "case_results": []}, "沒有案例結果"),
    ({"conditions": [{"id": "C1", "desc": "x"}],
      "case_results": [{"case_id": "a", "ok": True}]}, ""),
])
def test_校準不適用的原因要講得出來(se, expect):
    """「沒有變動」要分得出是**資料不足**還是**校準沒生效**——看不出差別的話,
    重放的綠燈沒有意義。"""
    report = {"findings": []}
    if se is not None:
        report["_spec_exec"] = se
    assert expect in rc.skip_reason(report)


# ─────────────────── 3. 三個數字與閘門 ───────────────────

def _dump(tmp_path, mr_id, report, golden=None):
    (tmp_path / f"mr_{mr_id}.json").write_text(json.dumps(report, ensure_ascii=False),
                                               encoding="utf-8")
    if golden is not None:
        (rc.GOLDEN_DIR / f"mr_{mr_id}.json").write_text(json.dumps(golden, ensure_ascii=False),
                                                        encoding="utf-8")


def test_算出三個數字(tmp_path, monkeypatch):
    """一份乾淨 case(沒有 expected)+ 一份有預期缺陷的,兩邊的數字都要算對。"""
    monkeypatch.setattr(rc, "load_config", lambda: type("C", (), {"policy": POLICY})())
    _dump(tmp_path, "901", {"findings": [_finding(severity="info")], "score": 100})
    _dump(tmp_path, "902", {"findings": [_finding(title="門檻寫錯 >= 應為 >")], "score": 100})
    monkeypatch.setattr(rc, "GOLDEN_DIR", tmp_path / "golden")
    (tmp_path / "golden").mkdir()
    (tmp_path / "golden" / "mr_902.json").write_text(json.dumps(
        {"expected": [{"file": "a.sql", "line_range": [1, 5]}], "_golden": {}}),
        encoding="utf-8")
    out = _run(rc, tmp_path, monkeypatch)
    assert "重放 2 份報告(其中 1 份沒有預期缺陷 = 乾淨 case)" in out
    assert "| 抓到的預期缺陷 | 1/1 | 1/1 |" in out
    assert "✓ 三個數字都沒有往反方向走" in out


def _run_code(mod, d, monkeypatch, *extra):
    """跑一次 main(),回傳 (輸出, 結束碼)。"""
    import contextlib
    import io
    monkeypatch.setattr(sys, "argv", ["replay", str(d), *extra])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = mod.main()
    return buf.getvalue(), code


def _run(mod, d, monkeypatch, *extra):
    return _run_code(mod, d, monkeypatch, *extra)[0]


def test_抓到的缺陷變少要擋下來(tmp_path, monkeypatch):
    """這就是上一版的壞法。重放的唯一職責就是在送審前讓它亮紅燈。"""
    monkeypatch.setattr(rc, "load_config", lambda: type("C", (), {"policy": POLICY})())
    monkeypatch.setattr(rc, "GOLDEN_DIR", tmp_path / "golden")
    (tmp_path / "golden").mkdir()
    (tmp_path / "golden" / "mr_903.json").write_text(json.dumps(
        {"expected": [{"file": "a.sql", "line_range": [1, 5]}],
         "_golden": {"expect_findings": []}}),
        encoding="utf-8")
    _dump(tmp_path, "903", {"findings": [_finding(title="門檻寫錯")], "score": 100})

    # 假的校準:把那條 major 壓成 info → 預期缺陷就對不上了
    def squash(report):
        for f in report.get("findings", []):
            f["severity"] = "info"
        return report

    monkeypatch.setattr(rc, "calibrate_with_execution", squash)
    out, code = _run_code(rc, tmp_path, monkeypatch)
    assert code == 1, "抓到的缺陷變少必須回非零"
    assert "抓到的缺陷變少了" in out, out


def _squash_all(report):
    """假的校準:把所有 finding 壓成 info,而且不留任何降級紀錄。"""
    for f in report.get("findings", []):
        f["severity"] = "info"
    return report


def test_自動放行變多要擋下來(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "load_config", lambda: type("C", (), {"policy": POLICY})())
    monkeypatch.setattr(rc, "GOLDEN_DIR", tmp_path / "golden")
    (tmp_path / "golden").mkdir()
    # 要有通過的執行驗證,auto_approved 才有可能(spec_exec 是硬條件)
    _dump(tmp_path, "904", {"findings": [_finding(severity="blocker")], "score": 100,
                            "_spec_exec": {"passed": True}})
    monkeypatch.setattr(rc, "calibrate_with_execution", _squash_all)
    out, code = _run_code(rc, tmp_path, monkeypatch)
    assert code == 1, out
    assert "自動放行變多了" in out, out


def test_放寬但沒有降級紀錄要擋下來(tmp_path, monkeypatch):
    """`blocked → needs_human` 也是放寬。唯一正當的放寬是「執行證據推翻了什麼」,
    那會留在 `_calibration["downgraded"]`;沒有紀錄的放寬就是沒有依據。"""
    monkeypatch.setattr(rc, "load_config", lambda: type("C", (), {"policy": POLICY})())
    monkeypatch.setattr(rc, "GOLDEN_DIR", tmp_path / "golden")
    (tmp_path / "golden").mkdir()
    _dump(tmp_path, "907", {"findings": [_finding(severity="blocker")], "score": 100})
    monkeypatch.setattr(rc, "calibrate_with_execution", _squash_all)
    out, code = _run_code(rc, tmp_path, monkeypatch)
    assert code == 1, out
    assert "沒有對應的降級紀錄" in out and "放寬,**沒有執行證據**" in out, out


def test_有降級紀錄的放寬不算退步(tmp_path, monkeypatch):
    """沙盒實際跑成功 → blocker 降 major 是正當的(仍然交人工)。"""
    monkeypatch.setattr(rc, "load_config", lambda: type("C", (), {"policy": POLICY})())
    monkeypatch.setattr(rc, "GOLDEN_DIR", tmp_path / "golden")
    (tmp_path / "golden").mkdir()
    _dump(tmp_path, "908", {"findings": [_finding(title="語法錯誤", severity="blocker")],
                            "score": 100})

    def legit(report):
        for f in report.get("findings", []):
            f["severity"] = "major"
        report.setdefault("_calibration", {"annotated": [], "downgraded": []})
        report["_calibration"]["downgraded"].append(
            {"title": "語法錯誤", "file": "a.sql", "from": "blocker", "to": "major",
             "by": "sandbox-ran-ok"})
        return report

    monkeypatch.setattr(rc, "calibrate_with_execution", legit)
    out, code = _run_code(rc, tmp_path, monkeypatch)
    assert code == 0, out
    assert "放寬,有執行證據" in out, out


def test_失敗的紀錄不列入計分(tmp_path, monkeypatch):
    """環境失敗的存檔拿來評分,等於把環境問題記成品質退步。"""
    monkeypatch.setattr(rc, "load_config", lambda: type("C", (), {"policy": POLICY})())
    monkeypatch.setattr(rc, "GOLDEN_DIR", tmp_path / "golden")
    (tmp_path / "golden").mkdir()
    _dump(tmp_path, "905", {"error": "APIConnectionError"})
    _dump(tmp_path, "906", {"findings": [_finding(severity="info")], "score": 100})
    out = _run(rc, tmp_path, monkeypatch)
    assert "跳過" in out and "重放 1 份報告" in out


def test_沒有報告時回非零(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["replay", str(tmp_path)])
    assert rc.main() == 2
