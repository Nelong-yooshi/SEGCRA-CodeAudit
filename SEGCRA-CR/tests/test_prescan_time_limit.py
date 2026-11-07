"""預掃的時間上限:工具呼叫在背景執行緒跑、lint 的總預算、預掃整段的上限。

工具都是同步函式。直接在事件迴圈裡呼叫時,外層的 `asyncio.wait_for` 要等它跑完才插得進來:
5 個慢檔、每個 lint 各有 60 秒上限,預掃最壞就是 5 分鐘,整份審查的上限也等它。

卡住的情境用 `time.sleep`(同步卡住),不用 `await asyncio.sleep`:後者會讓出事件迴圈,
沒有這些處理時測試照樣通過。
"""
import asyncio
import json
import threading
import time

import pytest

from orchestrator import pipeline, tool_hub
from orchestrator.tool_hub import MODEL_TOOLS, ToolHub
from toolbox import gitlab, sqltools


def _run(coro):
    return asyncio.run(coro)


def _codes(lint):
    return [v.get("code") for v in lint]


# ─────────────────── ToolHub:唯讀的在背景執行緒,寫入類留在事件迴圈 ───────────────────

def test_唯讀工具同步卡住時外層上限準時中止():
    hub = ToolHub()

    def slow(sql):
        time.sleep(2)
        return "[]"

    hub._functions["sqltools__lint"] = slow

    async def main():
        t0 = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(hub.call("sqltools__lint", {"sql": "x"}), 0.3)
        return time.monotonic() - t0

    took = _run(main())
    assert took < 1.5, f"上限 0.3 秒,卻等了 {took:.1f} 秒"


def _thread_of(hub, name, args):
    async def main():
        loop_thread = threading.get_ident()
        return loop_thread, await hub.call(name, args)

    return _run(main())


def test_唯讀工具不在事件迴圈的執行緒上跑():
    hub = ToolHub()
    hub._functions["gitlab__get_file"] = lambda path, ref="": str(threading.get_ident())
    loop_thread, ran_on = _thread_of(hub, "gitlab__get_file", {"path": "a.sql"})
    assert "gitlab__get_file" in MODEL_TOOLS
    assert ran_on != str(loop_thread)


@pytest.mark.parametrize("name", ["gitlab__set_commit_status", "gitlab__post_summary",
                                  "gitlab__post_inline_comment", "gitlab__set_label",
                                  "memory__record_feedback"])
def test_寫入類留在事件迴圈裡直接呼叫(name):
    """丟到背景執行緒的話,外層逾時後它仍會做完:webhook 已把 commit status 設成失敗,
    還在跑的「設成成功」在那之後才送到,就把失敗蓋掉了。"""
    hub = ToolHub()
    hub._functions[name] = lambda **kw: str(threading.get_ident())
    loop_thread, ran_on = _thread_of(hub, name, {})
    assert name not in MODEL_TOOLS
    assert ran_on == str(loop_thread)


def test_本地工具照同一個規則():
    hub = ToolHub(servers=[])
    hub.register_local("load_skill", lambda name: str(threading.get_ident()), "x", {})
    hub.register_local("write_something", lambda: str(threading.get_ident()), "x", {})

    async def main():
        loop_thread = str(threading.get_ident())
        return (loop_thread, await hub.call("load_skill", {"name": "a"}),
                await hub.call("write_something", {}))

    loop_thread, read_on, write_on = _run(main())
    assert read_on != loop_thread
    assert write_on == loop_thread


def test_背景執行緒讀得到審查釘住的_commit():
    """釘住的 commit 存在 ContextVar 裡,新開的執行緒不會繼承;讀到預設值就是在審別的版本。"""
    hub = ToolHub()
    hub._functions["gitlab__get_file"] = lambda path, ref="": str(gitlab.review_ref())

    async def main():
        gitlab.pin_review_ref("abc123")
        return await hub.call("gitlab__get_file", {"path": "a.sql"})

    assert _run(main()) == "abc123"


def test_背景執行緒裡的例外照樣變成錯誤字串():
    hub = ToolHub()

    def broken(sql):
        raise ValueError("boom")

    hub._functions["sqltools__run_rules"] = broken
    out = _run(hub.call("sqltools__run_rules", {"sql": "x"}))
    assert out == "Error executing tool sqltools__run_rules: boom"


def test_本地工具的例外照樣往外丟():
    hub = ToolHub(servers=[])

    def broken(name):
        raise KeyError("no such skill")

    hub.register_local("load_skill", broken, "x", {})
    with pytest.raises(KeyError):
        _run(hub.call("load_skill", {"name": "a"}))


def test_截斷照舊():
    hub = ToolHub(tool_result_max_chars=10)
    hub._functions["gitlab__get_file"] = lambda path, ref="": "x" * 30
    out = _run(hub.call("gitlab__get_file", {"path": "a.sql"}))
    assert out.startswith("x" * 10 + "\n…(截斷,原長 30 字元)")
    assert _run(hub.call("gitlab__get_file", {"path": "a.sql"}, truncate=False)) == "x" * 30


# ─────────────────── lint 的總預算 ───────────────────

def _files(n):
    return [{"path": f"sql/f{i}.sql", "full_content": "SELECT a FROM t\n"} for i in range(n)]


def _hub_with_lint(fn):
    hub = ToolHub()
    hub._functions["sqltools__lint"] = fn
    return hub


def test_lint_總預算用完後剩下的檔案不跑(monkeypatch):
    """每次 0.4 秒、預算 1 秒:前兩個跑完(剩約 0.2 秒),第三個跑到一半不等,第四個不跑。"""
    calls = []

    def slow(sql):
        calls.append(sql)
        time.sleep(0.4)
        return json.dumps([{"line": 1, "code": "LT01", "description": "x"}])

    monkeypatch.setattr(pipeline, "LINT_TOTAL_SECONDS", 1.0)
    t0 = time.monotonic()
    pre = _run(pipeline.prescan(_hub_with_lint(slow), _files(4)))
    took = time.monotonic() - t0
    assert [_codes(e["lint"]) for e in pre] == [
        ["LT01"], ["LT01"], [sqltools.LINT_INCOMPLETE_CODE], [sqltools.LINT_INCOMPLETE_CODE]]
    assert len(calls) == 3
    assert took < 1.6, f"lint 總預算 1 秒,預掃卻花了 {took:.1f} 秒"


def test_沒跑完的那一筆寫出總預算的秒數(monkeypatch):
    monkeypatch.setattr(pipeline, "LINT_TOTAL_SECONDS", 0)
    pre = _run(pipeline.prescan(_hub_with_lint(lambda sql: "[]"), _files(1)))
    assert pre[0]["lint"] == [{"line": 0, "code": sqltools.LINT_INCOMPLETE_CODE,
                               "description": "整份 MR 的 lint 總時間(0 秒)用完了,這份內容沒有 lint 結果"}]


def test_預算一開始就是_0_時一次都不跑(monkeypatch):
    def must_not_run(sql):
        raise AssertionError("預算用完了還在跑 lint")

    monkeypatch.setattr(pipeline, "LINT_TOTAL_SECONDS", 0)
    pre = _run(pipeline.prescan(_hub_with_lint(must_not_run), _files(2)))
    assert [_codes(e["lint"]) for e in pre] == [[sqltools.LINT_INCOMPLETE_CODE]] * 2


def test_預算內的檔案_lint_結果照舊(monkeypatch):
    many = [{"line": i, "code": "LT01", "description": "x"} for i in range(20)]
    monkeypatch.setattr(pipeline, "LINT_TOTAL_SECONDS", 100)
    pre = _run(pipeline.prescan(_hub_with_lint(lambda sql: json.dumps(many)), _files(1)))
    assert pre[0]["lint"] == many[:15]
    pre = _run(pipeline.prescan(_hub_with_lint(lambda sql: "{}"), _files(1)))
    assert pre[0]["lint"] == []


def test_預算用完的檔案在報告記未完整(monkeypatch):
    monkeypatch.setattr(pipeline, "LINT_TOTAL_SECONDS", 0)
    pre = _run(pipeline.prescan(_hub_with_lint(lambda sql: "[]"), _files(1)))
    report = pipeline.enforce_lint_incomplete({"findings": []}, pre)
    fd = report["findings"][0]
    assert fd["stage"] == "lint" and fd["file"] == "sql/f0.sql"
    assert "整份 MR 的 lint 總時間 0 秒用完" in fd["detail"]


def test_預設值():
    assert pipeline.PRESCAN_TIMEOUT_SECONDS == 600
    assert pipeline.LINT_TOTAL_SECONDS == 300


# ─────────────────── 預掃整段的上限 ───────────────────

def _slow_second_file(seconds):
    def lint(sql):
        if "slow" in sql:
            time.sleep(seconds)
        return "[]"
    return lint


_THREE_FILES = [{"path": "sql/a.sql", "full_content": "DELETE FROM t\n"},
                {"path": "sql/b.sql", "full_content": "DELETE FROM slow\n"},
                {"path": "sql/c.sql", "full_content": "SELECT 1\n"}]


def _prescan_with_limit(hub, files, deadline):
    report = {"findings": []}
    pre = _run(pipeline._run_prescan(report, hub, files, None, None, [], {}, deadline))
    return report, pre


def test_預掃逾時時保留做完的檔案(monkeypatch):
    """第二個檔的 lint 卡住:第一個檔整份保留;第二個檔的規則命中保留、lint 記成沒跑完;
    第三個檔沒做到。"""
    monkeypatch.setattr(pipeline, "PRESCAN_TIMEOUT_SECONDS", 0.5)
    t0 = time.monotonic()
    report, pre = _prescan_with_limit(_hub_with_lint(_slow_second_file(3)), _THREE_FILES,
                                      time.monotonic() + 1000)
    took = time.monotonic() - t0
    assert took < 2, f"預掃上限 0.5 秒,卻等了 {took:.1f} 秒"
    assert [e["path"] for e in pre] == ["sql/a.sql", "sql/b.sql"]
    assert all(any(h.get("rule") == "R001" for h in e["rules"]) for e in pre), pre
    assert pre[0]["lint"] == []
    assert _codes(pre[1]["lint"]) == [sqltools.LINT_INCOMPLETE_CODE]
    assert "預掃整段的時間上限到了" in pre[1]["lint"][0]["description"]
    [fd] = report["findings"]
    assert fd["stage"] == pipeline._PRESCAN_STAGE and fd["severity"] == "major"
    assert fd["title"].endswith("(逾時)")
    assert "逾時前做完規則層的 2 個檔案(共 3 個)結果仍在報告內" in fd["detail"]
    assert "這一環節的結果不在本報告內" not in fd["detail"]
    lint = pipeline.enforce_lint_incomplete({"findings": []}, pre)["findings"]
    assert [f["file"] for f in lint] == ["sql/b.sql"]


def test_預掃的上限不超過整份的截止時間(monkeypatch):
    monkeypatch.setattr(pipeline, "PRESCAN_TIMEOUT_SECONDS", 100)
    monkeypatch.setattr(pipeline, "MIN_STAGE_SECONDS", 0.1)
    t0 = time.monotonic()
    report, pre = _prescan_with_limit(_hub_with_lint(_slow_second_file(3)), _THREE_FILES,
                                      time.monotonic() + 0.5)
    assert time.monotonic() - t0 < 2
    [fd] = report["findings"]
    assert "被整份審查的截止時間縮短了" in fd["detail"] and "100 秒" in fd["detail"]


def test_剩的時間不夠時預掃照跑_至少有最少秒數(monkeypatch):
    """預掃是確定性的結果:截止時間已經過了也要跑,上限是 MIN_STAGE_SECONDS。"""
    monkeypatch.setattr(pipeline, "PRESCAN_TIMEOUT_SECONDS", 100)
    monkeypatch.setattr(pipeline, "MIN_STAGE_SECONDS", 0.5)
    t0 = time.monotonic()
    report, pre = _prescan_with_limit(_hub_with_lint(_slow_second_file(3)), _THREE_FILES,
                                      time.monotonic() - 10)
    assert time.monotonic() - t0 < 2
    assert [e["path"] for e in pre] == ["sql/a.sql", "sql/b.sql"]
    [fd] = report["findings"]
    assert fd["stage"] == pipeline._PRESCAN_STAGE and fd["title"].endswith("(逾時)")


def test_預掃出錯時記一條並用空的預掃(monkeypatch):
    async def broken(hub, files, *a, out=None, **kw):
        out.append({"path": "sql/a.sql", "rules": [], "lint": []})
        raise RuntimeError("SECRET")

    monkeypatch.setattr(pipeline, "prescan", broken)
    report, pre = _prescan_with_limit(ToolHub(), _THREE_FILES, time.monotonic() + 1000)
    assert pre == []
    [fd] = report["findings"]
    assert fd["stage"] == pipeline._PRESCAN_STAGE and "RuntimeError" in fd["detail"]
    assert "SECRET" not in json.dumps(fd, ensure_ascii=False)


def test_預掃沒逾時時結果與直接呼叫相同(monkeypatch):
    hub = _hub_with_lint(lambda sql: "[]")
    report, pre = _prescan_with_limit(hub, _THREE_FILES, time.monotonic() + 1000)
    assert report == {"findings": []}
    assert pre == _run(pipeline.prescan(hub, _THREE_FILES))
    assert all(e["lint"] == [] for e in pre)


# ─────────────────── 真的走 review_mr ───────────────────

def test_review_mr_預掃卡住時照常產出報告並交人工(monkeypatch, tmp_path):
    """第二個檔的 lint 同步卡 5 秒、預掃上限 1 秒:第一個檔的 R001 照常報出,決策交人工。"""
    from orchestrator.config import load_config

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "mr_952.json").write_text(json.dumps({
        "title": "調整查詢", "description": "例行維護", "sha": "e1a3" + "0" * 36,
        "files": [{"path": "sql/a.sql", "diff": "@@ -0,0 +1 @@\n+DELETE FROM t",
                   "full_content": "DELETE FROM t\n"},
                  {"path": "sql/b.sql", "diff": "@@ -0,0 +1 @@\n+SELECT slow FROM t",
                   "full_content": "SELECT slow FROM t\n"}],
    }, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))
    monkeypatch.setattr(pipeline, "PRESCAN_TIMEOUT_SECONDS", 1)
    lint = _slow_second_file(5)
    lint.__name__ = "lint"
    monkeypatch.setitem(tool_hub.TOOL_GROUPS, "sqltools",
                        [sqltools.parse_ast, lint, sqltools.run_rules])

    async def fake_run_agent(cfg, profile, system, user, hub, verbose=True,
                             use_tools=False, trace=None, meta=None):
        return json.dumps({"summary": "沒問題", "score": 100, "findings": []},
                          ensure_ascii=False)

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    t0 = time.monotonic()
    report = _run(pipeline.review_mr(load_config(), "952"))
    took = time.monotonic() - t0
    assert took < 4.5, f"預掃上限 1 秒,整份審查卻花了 {took:.1f} 秒"
    assert any(f.get("stage") == pipeline._PRESCAN_STAGE for f in report["findings"]), \
        report["findings"]
    assert any(f.get("file") == "sql/a.sql" and f.get("severity") == "blocker"
               for f in report["findings"]), report["findings"]
    assert report["decision"] != "auto_approved", report["decision"]


# ─────────────────── 卡在呼叫到的 macro ───────────────────

def test_卡在呼叫到的_macro_時_掃完的那個命中還在_說明寫對(monkeypatch):
    """變更的 model 呼叫兩個 macro:第一個掃完(讀取環境的寫法,H008),第二個卡住。
    掃完的那個命中要在結果裡;說明要寫「變更的檔案都做完了,卡在呼叫到的 macro」。

    卡住的記號寫在 Jinja 註解裡:只出現在 macro 的原文(預掃掃的是原文),不出現在展開結果,
    model 自己那一輪的規則層不會跟著卡住。"""
    from orchestrator import dbt_context, dbt_project
    from orchestrator.dbt_render import render_model
    from tests import test_dbt_context as DBT

    dbt_project.clear_cache()
    monkeypatch.setattr(dbt_context, "_summarize_isolated", dbt_context._summarize)
    monkeypatch.setattr(pipeline, "render_model_isolated",
                        lambda path, *, timeout_s=None, **kw: render_model(path, **kw))
    project = {**DBT.PROJECT,
               "macros/a_env.sql": ("{% macro a_env() %}{% if target.name == 'prod' %}1"
                                    "{% else %}2{% endif %}{% endmacro %}"),
               "macros/b_slow.sql": "{% macro b_slow() %}{# SLOW_MACRO #}3{% endmacro %}",
               "models/mrt_big.sql": "SELECT {{ a_env() }} AS a, {{ b_slow() }} AS b FROM t\n"}
    mr = DBT._mr([DBT._f("models/mrt_big.sql")])
    ctx = DBT._ctx(files=project, mr=mr)

    def run_rules(sql):
        if "SLOW_MACRO" in sql:
            time.sleep(5)
        return sqltools.run_rules(sql)
    hub = ToolHub()
    hub._functions["sqltools__run_rules"] = run_rules
    hub._functions["sqltools__lint"] = lambda sql: "[]"
    monkeypatch.setattr(pipeline, "PRESCAN_TIMEOUT_SECONDS", 3)
    try:
        report = {"findings": []}
        pre = _run(pipeline._run_prescan(report, hub, mr["files"], DBT.CFG, ctx, [], {},
                                         time.monotonic() + 1000))
    finally:
        dbt_project.clear_cache()
    called = {e["path"]: e for e in pre if e.get("called_macro")}
    assert [h["rule"] for h in called["macros/a_env.sql"]["rules"]] == ["H008"], called
    assert "macros/b_slow.sql" not in called
    assert [e["path"] for e in pre if not e.get("called_macro")] == ["models/mrt_big.sql"]
    [fd] = report["findings"]
    assert "變更的 1 個檔案都做完了" in fd["detail"] and "卡在掃它們呼叫到的 macro" in fd["detail"]
    assert "逾時前做完規則層" not in fd["detail"]


def test_最後一個檔的_lint_卡住時_說明不寫成卡在_macro(monkeypatch):
    """變更檔都已經放進結果(規則層做完、lint 還在跑),但還沒進到呼叫到的 macro 那一段。"""
    monkeypatch.setattr(pipeline, "PRESCAN_TIMEOUT_SECONDS", 0.5)
    files = [{"path": "sql/a.sql", "full_content": "DELETE FROM slow\n"}]
    report, pre = _prescan_with_limit(_hub_with_lint(_slow_second_file(3)), files,
                                      time.monotonic() + 1000)
    assert [e["path"] for e in pre] == ["sql/a.sql"]
    [fd] = report["findings"]
    assert "逾時前做完規則層的 1 個檔案(共 1 個)" in fd["detail"]
    assert "卡在掃它們呼叫到的 macro" not in fd["detail"]
