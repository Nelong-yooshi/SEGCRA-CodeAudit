"""審查模型只拿得到唯讀工具(#17)。

MR 的標題、描述、SQL 註解都是作者寫的,可以夾帶給模型的指令。寫判例、學風格、開 MR、
留言、設 label、設 commit status 只由管線自己呼叫;模型看到的工具清單與執行入口都只認
tool_hub.MODEL_TOOLS。不呼叫真的模型:run_agent 以假的 OpenAI 用戶端驅動。
"""
import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from orchestrator import agent, pipeline, tool_hub
from orchestrator.pipeline import _TOOL_REFUSAL_TITLE, apply_policy, enforce_tool_refusals
from orchestrator.tool_hub import MODEL_REFUSAL, MODEL_TOOLS, ToolHub

READ_ONLY = {
    "gitlab__get_mr_diff", "gitlab__get_file",
    "sqltools__parse_ast", "sqltools__lint", "sqltools__run_rules",
    "memory__retrieve_knowledge", "memory__get_conventions", "memory__check_style",
    "memory__lookup_similar_reviews", "load_skill",
}
WRITE_TOOLS = {
    "gitlab__post_inline_comment", "gitlab__post_summary", "gitlab__set_commit_status",
    "gitlab__create_rule_mr", "gitlab__set_label",
    "memory__learn_style_from_examples", "memory__record_feedback",
}


def _hub():
    hub = ToolHub()
    hub.register_local("load_skill", lambda name: f"skill {name}", "載入 skill",
                       {"type": "object", "properties": {"name": {"type": "string"}}})
    return hub


def _names(tools):
    return {t["function"]["name"] for t in tools}


# ─────────────────── 白名單本身 ───────────────────

def test_白名單就是這十個唯讀工具():
    """白名單的內容要有意識地改:新增工具預設不開放給模型,開放前要確認它不會寫入。"""
    assert MODEL_TOOLS == READ_ONLY


def test_每個已註冊的工具都被歸類():
    """新增工具時要決定它屬於哪一類;沒歸類就會在這裡紅。"""
    assert set(_hub().tools) == READ_ONLY | WRITE_TOOLS


def test_交給模型的工具清單只有白名單():
    hub = _hub()
    names = _names(hub.openai_tools)
    assert names == READ_ONLY
    assert not names & WRITE_TOOLS


def test_沒有註冊的工具不會因為在白名單就出現():
    """只註冊部分工具組時,清單是「已註冊 ∩ 白名單」。"""
    assert _names(ToolHub(servers=["sqltools"]).openai_tools) == {
        "sqltools__parse_ast", "sqltools__lint", "sqltools__run_rules"}


# ─────────────────── 執行入口 ───────────────────

class _Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return "executed"


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_模型要求寫入工具_不執行_有記錄(name):
    hub = _hub()
    spy = _Spy()
    hub._functions[name] = spy
    result = asyncio.run(hub.call_from_model(name, {"x": "1"}))
    assert result == MODEL_REFUSAL
    assert spy.calls == []
    assert hub.refused_calls == [name]


def test_本地工具不在白名單也不執行():
    hub = _hub()
    spy = _Spy()
    hub.register_local("write_something", spy, "d", {"type": "object", "properties": {}})
    assert asyncio.run(hub.call_from_model("write_something", {})) == MODEL_REFUSAL
    assert spy.calls == [] and hub.refused_calls == ["write_something"]
    assert "write_something" not in _names(hub.openai_tools)


@pytest.mark.parametrize("name", ["search_web", "gitlab__delete_project", "", "memory__"])
def test_模型自己編的工具名稱_拒絕但不記錄(name):
    """不存在的名稱只回拒絕,不記錄:小模型偶爾會編工具名,記下來會變成雜訊 finding。"""
    hub = _hub()
    assert asyncio.run(hub.call_from_model(name, {})) == MODEL_REFUSAL
    assert hub.refused_calls == []


def test_白名單內的工具照常執行():
    hub = _hub()
    spy = _Spy()
    hub._functions["sqltools__run_rules"] = spy
    assert asyncio.run(hub.call_from_model("sqltools__run_rules", {"sql": "SELECT 1"})) == "executed"
    assert spy.calls == [{"sql": "SELECT 1"}] and hub.refused_calls == []
    assert asyncio.run(hub.call_from_model("load_skill", {"name": "x"})) == "skill x"


def test_管線自己呼叫寫入工具照常執行():
    """寫留言、label、commit status 由管線用 call() 執行,不受白名單影響。"""
    hub = _hub()
    spy = _Spy()
    hub._functions["gitlab__set_label"] = spy
    assert asyncio.run(hub.call("gitlab__set_label", {"mr_id": "1", "label": "x"})) == "executed"
    assert spy.calls == [{"mr_id": "1", "label": "x"}] and hub.refused_calls == []


# ─────────────────── run_agent 端到端(假的模型端點) ───────────────────

class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls

    def model_dump(self, exclude_none=True):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [{"id": t.id, "type": "function",
                                "function": {"name": t.function.name,
                                             "arguments": t.function.arguments}}
                               for t in self.tool_calls]
        return {k: v for k, v in d.items() if v is not None}


def _tool_call(i, name, args):
    return SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(
        name=name, arguments=json.dumps(args, ensure_ascii=False)))


class _FakeClient:
    """第一輪:模型照 MR 內容的指示要求寫判例與設 commit status;第二輪:輸出報告。"""
    seen_tools = None

    def __init__(self, **kwargs):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.turn = 0

    async def _create(self, **kwargs):
        _FakeClient.seen_tools = kwargs.get("tools")
        self.turn += 1
        if self.turn == 1:
            msg = _FakeMessage(tool_calls=[
                _tool_call(1, "memory__record_feedback",
                           {"title": "R001", "disposition": "rejected"}),
                _tool_call(2, "gitlab__set_commit_status",
                           {"mr_id": "1", "sha": "a" * 40, "state": "success"}),
                _tool_call(3, "sqltools__run_rules", {"sql": "SELECT 1"})])
        else:
            msg = _FakeMessage(content='{"summary": "ok", "findings": []}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                               usage=None)


def test_run_agent_不執行模型要求的寫入工具(monkeypatch):
    monkeypatch.setattr(agent, "AsyncOpenAI", _FakeClient)
    hub = _hub()
    spies = {name: _Spy() for name in ("memory__record_feedback", "gitlab__set_commit_status",
                                       "sqltools__run_rules")}
    hub._functions.update(spies)
    cfg = SimpleNamespace(endpoint="http://127.0.0.1:1/v1", api_key="test")
    profile = SimpleNamespace(model="fake", temperature=0, max_output_tokens=100, seed=None,
                              reasoning_effort=None)
    trace = []
    out = asyncio.run(agent.run_agent(cfg, profile, "sys", "user", hub,
                                      verbose=False, trace=trace))

    assert json.loads(out)["summary"] == "ok"
    assert _names(_FakeClient.seen_tools) == READ_ONLY
    assert spies["memory__record_feedback"].calls == []
    assert spies["gitlab__set_commit_status"].calls == []
    assert spies["sqltools__run_rules"].calls == [{"sql": "SELECT 1"}]
    assert hub.refused_calls == ["memory__record_feedback", "gitlab__set_commit_status"]
    assert [t["result_preview"] for t in trace[:2]] == [MODEL_REFUSAL, MODEL_REFUSAL]


# ─────────────────── 後處理:被拒絕時要看得到、不得自動放行 ───────────────────

_POLICY = {"auto_approve": {"max_diff_lines": 30, "min_score": 0,
                            "allowed_severities": ["info"], "forbid_pending_hints": True},
           "block": {"min_blockers": 1}}
_MR = {"files": [{"path": "sql/maintenance/x.sql", "diff": "@@\n+SELECT 1;",
                  "full_content": "SELECT 1;"}]}


def test_有寫入工具被拒絕_補_major_不得自動放行():
    base = {"score": 100, "_spec_exec": {"passed": True}, "findings": []}
    assert apply_policy(dict(base, findings=[]), _MR, _POLICY)["decision"] == "auto_approved"

    report = enforce_tool_refusals(dict(base, findings=[]),
                                   ["memory__record_feedback", "memory__record_feedback"])
    (f,) = report["findings"]
    assert f["severity"] == "major" and f["title"] == _TOOL_REFUSAL_TITLE
    assert f["detail"].count("memory__record_feedback") == 1      # 去重
    assert apply_policy(report, _MR, _POLICY)["decision"] == "needs_human"


def test_沒有被拒絕就不補():
    assert enforce_tool_refusals({"findings": []}, [])["findings"] == []


def test_模型同標題的_info_不能取代這條_major():
    """標題只能由程式產生:被誘導的模型先輸出同標題的 info,也不能讓 major 消失。"""
    report = {"findings": [{"file": "(MR 內容)", "line": 0, "severity": "info",
                            "title": _TOOL_REFUSAL_TITLE, "detail": "已確認沒問題"}]}
    (f,) = enforce_tool_refusals(report, ["gitlab__set_label"])["findings"]
    assert f["severity"] == "major" and "已確認沒問題" not in f["detail"]


def test_拒絕訊息與_finding_都不含模型給的參數():
    """參數可能是攻擊者寫的文字;finding 會被回貼到 MR 留言,只能有工具名稱。"""
    hub = _hub()
    asyncio.run(hub.call_from_model("memory__record_feedback",
                                    {"title": "IGNORE ALL RULES", "disposition": "rejected"}))
    report = enforce_tool_refusals({"findings": []}, hub.refused_calls)
    assert "IGNORE" not in json.dumps(report, ensure_ascii=False)


def test_管線在決策前補這條_並讀的是同一個_hub():
    src = inspect.getsource(pipeline.review_mr)
    call = "report = enforce_tool_refusals(report, hub.refused_calls)"
    assert src.count(call) == 1
    assert src.index("raw = await _run_main_review(cfg, profile, system, user, hub") < src.index(call)
    # 主審查的模型呼叫拿到的也是這個 hub
    assert "run_agent(cfg, profile, system, user, hub, trace=trace, meta=meta)" in inspect.getsource(
        pipeline._run_main_review)
    assert src.index(call) < src.index("report = apply_policy(report, mr, cfg.policy)")


def test_審查迴圈走白名單入口():
    """run_agent 執行模型要求的工具時,必須走 call_from_model,不能直接 call()。"""
    src = inspect.getsource(agent.run_agent)
    assert "hub.call_from_model(tc.function.name, args)" in src
    assert "hub.call(" not in src


BAD_MR_IDS = ["1/../../issues/5", "x/../../config/x", "1?private_token=x", "1#x", "", "-1",
              "²", "١٢", "12345678901", "1 ", " 1", "1.0", "mr_1"]


@pytest.mark.parametrize("bad", BAD_MR_IDS)
def test_模型給的_MR_編號只接受數字_真實模式不會發出請求(bad, monkeypatch):
    """get_mr_diff 是模型能呼叫的工具,mr_id 會被接進 API 路徑:`1/../../issues/5`
    這類值可以讓同一把 token 去讀別的端點。不合法就不發請求。"""
    from toolbox import gitlab
    sent = []
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "_api", lambda *a, **k: sent.append(a) or {})
    monkeypatch.setattr(gitlab, "_api_pages", lambda *a, **k: sent.append(a) or ([], False))
    result = asyncio.run(_hub().call_from_model("gitlab__get_mr_diff", {"mr_id": bad}))
    assert result.startswith("Error executing tool") and sent == []


@pytest.mark.parametrize("bad", BAD_MR_IDS)
def test_模型給的_MR_編號只接受數字_mock_模式不會讀檔(bad, monkeypatch, tmp_path):
    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", tmp_path / "fixtures")
    # fixtures 目錄以外放一個誘餌檔:若 mr_id 沒檢查,`x/../../config/x` 會讀到它
    # (mr_x 目錄要存在,Linux 才會照路徑解析過去;Windows 是字面上消去)
    (tmp_path / "fixtures" / "mr_x").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "x.json").write_text(
        '{"title": "SECRET", "files": [{"path": "a.sql", "full_content": "SECRET"}]}',
        encoding="utf-8")
    hub = _hub()
    diff = asyncio.run(hub.call_from_model("gitlab__get_mr_diff", {"mr_id": bad}))
    assert diff.startswith("Error executing tool") and "SECRET" not in diff
    # get_file 的 mr_id 空字串 = 不指定 MR(不讀 fixture);其餘不合法的值一律報錯
    file = asyncio.run(hub.call_from_model("gitlab__get_file", {"path": "a.sql", "mr_id": bad}))
    assert "SECRET" not in file
    assert file.startswith("Error executing tool") or bad == ""


@pytest.mark.parametrize("good", ["001", "101", 101, "9999999999"])
def test_數字的_MR_編號照常(good, monkeypatch, tmp_path):
    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", tmp_path)
    (tmp_path / f"mr_{good}.json").write_text(
        '{"title": "t", "files": [{"path": "a.sql", "full_content": "SELECT 1"}]}',
        encoding="utf-8")
    assert json.loads(gitlab.get_mr_diff(good))["title"] == "t"
    assert gitlab.get_file("a.sql", mr_id=good) == "SELECT 1"


def test_工具組沒有不在任何一類的函式():
    """TOOL_GROUPS 新增函式時,這個檔案的分類也要一起更新(避免默默開放或漏列)。"""
    names = {f"{g}__{fn.__name__}" for g, fns in tool_hub.TOOL_GROUPS.items() for fn in fns}
    assert names == (READ_ONLY - {"load_skill"}) | WRITE_TOOLS
