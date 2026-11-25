"""寫回 GitLab 的兩個細節:重審時舊的 ai-review:: 標籤要拿掉、建議欄位的排版。"""
import json

import pytest

from orchestrator import pipeline
from toolbox import gitlab


# ─────────────────── 標籤 ───────────────────

@pytest.mark.parametrize("label, removed", [
    ("ai-review::blocked", {"ai-review::auto_approved", "ai-review::needs_human"}),
    ("ai-review::needs_human", {"ai-review::auto_approved", "ai-review::blocked"}),
    ("ai-review::auto_approved", {"ai-review::needs_human", "ai-review::blocked"}),
])
def test_set_label_removes_other_review_labels_in_same_request(monkeypatch, label, removed):
    """scoped label 互斥要付費版;沒有授權時舊的結論會留著,同時掛兩個看不出哪個是現在的。"""
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    sent = []
    monkeypatch.setattr(gitlab, "_api", lambda method, path, **kw: sent.append((method, path, kw)))
    assert json.loads(gitlab.set_label("7", label)) == {"ok": True}
    ((method, path, kw),) = sent
    assert (method, path) == ("PUT", "/merge_requests/7")
    assert kw["json"]["add_labels"] == label
    assert set(kw["json"]["remove_labels"].split(",")) == removed


def test_other_labels_are_not_touched(monkeypatch):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    sent = []
    monkeypatch.setattr(gitlab, "_api", lambda method, path, **kw: sent.append(kw))
    gitlab.set_label("7", "team::dba")
    assert sent == [{"json": {"add_labels": "team::dba"}}]


def test_retry_resends_the_same_removal(monkeypatch):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    calls = []

    def flaky(method, path, **kw):
        calls.append(kw["json"])
        if len(calls) == 1:
            raise RuntimeError("暫時失敗")
    monkeypatch.setattr(gitlab, "_api", flaky)
    monkeypatch.setattr(gitlab, "_send_with_retry", _retry)
    gitlab.set_label("7", "ai-review::blocked")
    assert len(calls) == 2 and calls[0] == calls[1] and "remove_labels" in calls[0]


def _retry(send, attempts):
    for _ in range(attempts):
        try:
            send()
            return None
        except Exception:      # noqa: BLE001
            continue
    return "failed"


# ─────────────────── 建議欄位 ───────────────────

def _comment(suggestion):
    return pipeline._format_comment({"title": "t", "detail": "d", "suggestion": suggestion})


@pytest.mark.parametrize("code", [
    "SELECT a FROM t WHERE x >= 100",
    "  where amount >= 100000",
    "AND t.amount >= 100000",
    "t.amount >= 100000",
    "[amount] >= 100000",
    "{{ config(materialized='table') }}",
    "-- 改成含等\nWHERE x >= 1",
    "DECLARE @start_date DATE = '2026-06-01';",
])
def test_code_goes_into_a_sql_block(code):
    body = _comment(code)
    assert f"建議修改:\n```sql\n{code}\n```" in body


@pytest.mark.parametrize("prose", [
    "這段 SQL 的 GROUP BY 少了 account_no,請補上後再送審。",
    "請確認門檻是否為「達 10 萬」(含等)。",
    "規格寫的是達,程式寫成超過。",
])
def test_prose_is_a_paragraph_not_a_code_block(prose):
    body = _comment(prose)
    assert body.endswith(f"建議修改:\n{prose}") and "```" not in body


def test_suggestion_with_its_own_fence_is_not_wrapped_again():
    """模型自己帶了 ```sql 的,原樣放:外面再包一層,GitLab 上會變成區塊裡又有反引號。"""
    for text in ("改成:\n```sql\nWHERE x >= 1\n```",
                 "WHERE x > 1 改成含等:\n```sql\nWHERE x >= 1\n```"):     # 第一行像程式也一樣
        body = _comment(text)
        assert body.endswith(f"建議修改:\n{text}") and body.count("```") == 2


def test_fence_is_longer_than_backticks_inside():
    code = "SELECT `a` FROM t -- ``x``"
    body = _comment(code)
    assert f"建議修改:\n```sql\n{code}\n```" in body
    code4 = "SELECT a FROM t -- ````"
    assert "`````sql\n" + code4 + "\n`````" in _comment(code4)


def test_no_suggestion_no_section():
    assert "建議修改" not in pipeline._format_comment({"title": "t", "detail": "d"})
