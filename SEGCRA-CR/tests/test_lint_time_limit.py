"""lint 的時間上限:sqlfluff 的耗時隨內容超線性成長,特製的內容幾千字元就要數十秒。

lint 由預掃與模型的工具呼叫同步執行,在審查的同一條執行緒上;沒有上限時,一份 MR 可以讓
整份審查停住,外層的 asyncio 時間上限也插不進來。現在 lint 在子行程跑、超過時間強制終止,
沒跑完的檔案在報告記一條「審查未完整」,決策交人工。

卡住的情境用真的慢輸入,不用 `asyncio.sleep` 模擬:卡住的是同步呼叫,`await` 的假卡住
會讓測試在沒有上限時照樣通過。
"""
import asyncio
import json
import threading
import time

import pytest

from orchestrator import pipeline
from toolbox import sqltools

# sqlfluff 處理反覆的 `/*,` 非常慢(超線性),3 萬字元遠超過測試給的上限
SLOW_SQL = "/*," * 10_000
INCOMPLETE = [{"line": 0, "code": sqltools.LINT_INCOMPLETE_CODE}]


def _codes(result: str) -> list:
    return [v.get("code") for v in json.loads(result)]


def _lint_with_deadline(sql: str, seconds: float):
    """在背景執行緒呼叫 lint,最多等 seconds 秒。

    lint 自己沒有上限時,直接呼叫會讓這條測試跟著卡好幾分鐘;這樣寫,沒有上限就是立刻失敗。"""
    box = {}

    def run():
        t0 = time.monotonic()
        box["result"] = sqltools.lint(sql)
        box["took"] = time.monotonic() - t0

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"lint 超過 {seconds} 秒還沒回來:沒有時間上限"
    return box["result"], box["took"]


# ─────────────────── sqltools.lint ───────────────────

def test_一般的_SQL_照常有_lint_結果():
    codes = _codes(sqltools.lint("SELECT a FROM t"))    # 檔尾沒有換行:LT12
    assert codes and sqltools.LINT_INCOMPLETE_CODE not in codes, codes


def test_同步卡住時在上限內中止並回固定的一筆(monkeypatch):
    monkeypatch.setattr(sqltools, "LINT_TIMEOUT_SECONDS", 2)
    result, took = _lint_with_deadline(SLOW_SQL, 30)
    assert _codes(result) == [sqltools.LINT_INCOMPLETE_CODE]
    assert took < 15, f"上限 2 秒,卻等了 {took:.1f} 秒"


def test_沒跑完時不是空清單():
    """空清單代表「檢查過、沒有違規」,不能拿來代表沒跑完。"""
    assert json.loads(sqltools._LINT_INCOMPLETE) != []
    assert _codes(sqltools._LINT_INCOMPLETE) == [sqltools.LINT_INCOMPLETE_CODE]


def test_超過字數上限的不跑(monkeypatch):
    def must_not_run(*a, **kw):
        raise AssertionError("超過字數上限的內容不該送進子行程")

    monkeypatch.setattr(sqltools, "run_isolated", must_not_run)
    result = sqltools.lint("x" * (sqltools.LINT_MAX_CHARS + 1))
    assert _codes(result) == [sqltools.LINT_INCOMPLETE_CODE]


def test_剛好在字數上限的照常跑(monkeypatch):
    calls = []

    def fake(fn, args, **kw):
        calls.append(len(args[0]))
        return "[]"

    monkeypatch.setattr(sqltools, "run_isolated", fake)
    assert sqltools.lint("x" * sqltools.LINT_MAX_CHARS) == "[]"
    assert calls == [sqltools.LINT_MAX_CHARS]


def test_子行程失敗時回固定的一筆_不回顯錯誤內容(monkeypatch):
    def failing(fn, args, *, timeout_s, max_memory_mb, on_failure):
        assert timeout_s == sqltools.LINT_TIMEOUT_SECONDS
        assert max_memory_mb == sqltools.LINT_MAX_MEMORY_MB
        return on_failure("IsolationError: 子行程異常結束 SECRET_PATH")

    monkeypatch.setattr(sqltools, "run_isolated", failing)
    result = sqltools.lint("select 1")
    assert _codes(result) == [sqltools.LINT_INCOMPLETE_CODE]
    assert "SECRET_PATH" not in result


def test_sqlfluff丟例外時當成沒跑完_不回顯例外內容(monkeypatch):
    """sqlfluff 對內容的問題回報成違規;會丟例外的是執行環境(例如方言設錯)。那時不能讓每個檔
    都靜靜地沒有 lint 結果。直接呼叫子行程裡跑的那個函式,才換得掉 sqlfluff。"""
    import sqlfluff

    def broken(sql, dialect):
        raise RuntimeError("SELECT password FROM secret_table")

    monkeypatch.setattr(sqlfluff, "lint", broken)
    result = sqltools._lint_in_child("select 1")
    assert _codes(result) == [sqltools.LINT_INCOMPLETE_CODE]
    assert "secret_table" not in result


def test_模型的工具呼叫也有上限(monkeypatch):
    """模型呼叫 sqltools__lint 走同一個函式:上限在 lint 本身,不在預掃。"""
    from orchestrator.tool_hub import ToolHub

    monkeypatch.setattr(sqltools, "LINT_TIMEOUT_SECONDS", 2)
    box = {}

    def run():
        box["result"] = asyncio.run(ToolHub().call_json("sqltools__lint", {"sql": SLOW_SQL}))

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(30)
    assert not worker.is_alive(), "模型的 lint 工具呼叫沒有時間上限"
    assert [v.get("code") for v in box["result"]] == [sqltools.LINT_INCOMPLETE_CODE]


def test_上限的預設值():
    assert sqltools.LINT_TIMEOUT_SECONDS == 60
    assert sqltools.LINT_MAX_CHARS == 100_000


# ─────────────────── 報告與決策 ───────────────────

def _entry(lint, path="sql/x.sql"):
    return {"path": path, "rules": [], "lint": lint}


def test_沒跑完的檔案補一條未完整():
    report = pipeline.enforce_lint_incomplete(
        {"findings": []}, [_entry(INCOMPLETE), _entry([], "sql/ok.sql")])
    assert len(report["findings"]) == 1
    fd = report["findings"][0]
    assert fd["file"] == "sql/x.sql" and fd["severity"] == "major" and fd["stage"] == "lint"
    assert pipeline.review_incomplete(report) == [fd["title"]]


def test_一般的_lint_結果不補():
    pre = [_entry([{"line": 1, "code": "LT01", "description": "x"}]), _entry([]),
           {"path": "README.md", "non_sql": "doc", "rules": [], "lint": []},
           {"path": "a.sql", "rules": [], "lint": {"error": "lint failed"}}]
    assert pipeline.enforce_lint_incomplete({"findings": []}, pre) == {"findings": []}


def test_那一筆排在其他違規後面也認得():
    lint = [{"line": 1, "code": "LT01"}] + INCOMPLETE
    report = pipeline.enforce_lint_incomplete({"findings": []}, [_entry(lint)])
    assert len(report["findings"]) == 1


def test_未完整的說明不回顯檔案內容():
    report = pipeline.enforce_lint_incomplete({"findings": []}, [_entry(INCOMPLETE)])
    fd = report["findings"][0]
    assert str(sqltools.LINT_TIMEOUT_SECONDS) in fd["detail"]
    assert "SELECT" not in json.dumps(fd, ensure_ascii=False)


def test_lint_沒跑完時不得自動放行():
    report = pipeline.enforce_lint_incomplete(
        {"summary": "x", "score": 100, "findings": []}, [_entry(INCOMPLETE)])
    mr = {"files": [{"path": "sql/x.sql", "diff": "@@ -0,0 +1 @@\n+SELECT 1"}]}
    policy = {"auto_approve": {"max_diff_lines": 30, "min_score": 0,
                               "allowed_severities": ["info", "major"],
                               "forbid_pending_hints": True}}
    out = pipeline.apply_policy(report, mr, policy)
    assert out["decision"] != "auto_approved", out.get("decision")


def test_dry_run_也看得到():
    class _Hub:
        async def call_json(self, name, args):
            return {}

        async def call(self, name, args):
            return ""

    report = asyncio.run(pipeline._dry_run_report(
        _Hub(), "900", {"files": []}, [_entry(INCOMPLETE)], None, None))
    assert any(f.get("stage") == "lint" for f in report["findings"]), report["findings"]


# ─────────────────── 真的走 review_mr ───────────────────

def test_review_mr_遇到_lint_卡住時照常產出報告並交人工(monkeypatch, tmp_path):
    """預掃與 lint 都跑真的,只換掉模型。上限 2 秒:整份審查要在合理時間內回來。"""
    from orchestrator.config import load_config
    from toolbox import gitlab

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "mr_951.json").write_text(json.dumps({
        "title": "調整查詢", "description": "例行維護", "sha": "e1a2" + "0" * 36,
        "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+SELECT 1",
                   "full_content": SLOW_SQL}],
    }, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))
    monkeypatch.setattr(sqltools, "LINT_TIMEOUT_SECONDS", 2)

    async def fake_run_agent(cfg, profile, system, user, hub, verbose=True,
                             use_tools=False, trace=None, meta=None):
        return json.dumps({"summary": "沒問題", "score": 100, "findings": []},
                          ensure_ascii=False)

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    box = {}

    def run():
        t0 = time.monotonic()
        box["report"] = asyncio.run(pipeline.review_mr(load_config(), "951"))
        box["took"] = time.monotonic() - t0

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(120)
    assert not worker.is_alive(), "lint 卡住時整份審查沒有回來"
    report = box["report"]
    assert any(f.get("stage") == "lint" and f.get("file") == "sql/x.sql"
               for f in report["findings"]), report["findings"]
    assert report["decision"] != "auto_approved", report["decision"]
