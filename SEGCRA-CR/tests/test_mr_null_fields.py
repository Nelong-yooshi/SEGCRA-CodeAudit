"""MR 的欄位在、值是 null 時照常審查 —— 不呼叫 LLM、不連 GitLab。

GitLab 對沒填說明的 MR 回 `"description": null`。`.get("description", "")` 只在鍵不存在時給預設值,
拿到的是 None,抽規則碼時 `" ".join` 丟 TypeError,整份審查失敗(結束碼 5、不能合併)。
三層各自防:取 MR 的地方(真實 GitLab)、review_mr 拿到 MR 之後(任何來源)、抽規則碼本身。

另外兩件同一次實測看到的:剩的時間不夠、沒呼叫模型時,總結不能寫成「呼叫失敗」;
「審查未完整」的 finding 要有檔名(整份 MR 層級的用「(MR 內容)」)。
"""
import asyncio
import json

import pytest

from orchestrator import pipeline, spec_exec
from tests.test_review_truncation import CLEAN_REPORT, _mr, review  # noqa: F401
from toolbox import gitlab

SHA = "a" * 40


# ─────────────────── 1. 取 MR(真實 GitLab) ───────────────────

def _real_mr(monkeypatch, **fields):
    mr = {"iid": 5, "title": "標題", "description": "說明", "sha": SHA,
          "diff_refs": {"base_sha": "b" * 40, "head_sha": SHA, "start_sha": "b" * 40}, **fields}
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "_api", lambda method, path, **kw: mr)
    monkeypatch.setattr(gitlab, "_api_pages", lambda path: ([], False))
    return json.loads(gitlab.get_mr_diff("5"))


@pytest.mark.parametrize("field", ["description", "title", "sha"])
def test_GitLab_回_null_的文字欄位給空字串(monkeypatch, field):
    got = _real_mr(monkeypatch, **{field: None})
    assert got[field] == ""


def test_有值的照用(monkeypatch):
    got = _real_mr(monkeypatch)
    assert (got["title"], got["description"], got["sha"]) == ("標題", "說明", SHA)


def test_不是字串的也給空字串(monkeypatch):
    assert _real_mr(monkeypatch, description=123)["description"] == ""


# ─────────────────── 2. 抽規則碼 ───────────────────

@pytest.mark.parametrize("mr", [
    {"title": "R-201 調整", "description": None, "files": []},
    {"title": None, "description": "R-201 調整", "files": []},
    {"title": "x", "description": "x", "files": [{"path": None, "diff": "R-201"}]},
    {"title": "R-201 調整", "description": "x", "files": [{"path": "sql/a.sql", "diff": None}]},
])
def test_抽規則碼時_值是_None_的欄位當成空字串(mr):
    assert spec_exec.extract_rule_codes(mr) == ["R-201"]


# ─────────────────── 3. review_mr:任何來源的 MR ───────────────────

def test_沒填說明的_MR_照常審完(review):
    """mock 模式的 MR 直接讀 fixture(不經過真實 GitLab 那層),驗的是 review_mr 自己的處理。"""
    mr = dict(_mr(), description=None)
    report, _, _ = review(CLEAN_REPORT, {"prompt_tokens": 1, "completion_tokens": 1}, mr=mr)
    assert report["decision"] in ("auto_approved", "needs_human", "blocked")
    assert not any(f.get("stage") for f in report["findings"]), report["findings"]


def test_沒填標題與說明時_prompt_裡不會出現_None(monkeypatch, review):
    seen = []
    real = pipeline.USER_TEMPLATE

    class Spy(str):
        def format(self, *a, **kw):
            seen.append((kw.get("title"), kw.get("description")))
            return real.format(*a, **kw)
    monkeypatch.setattr(pipeline, "USER_TEMPLATE", Spy(real))
    review(CLEAN_REPORT, {}, mr=dict(_mr(), description=None, title=None))
    assert seen == [("", "")]


# ─────────────────── 4. 沒呼叫模型時的總結、審查未完整的檔名 ───────────────────

def test_剩的時間不夠時_總結不寫成呼叫失敗():
    out = pipeline._parse_model_output(None, {"failed": pipeline._NO_TIME_KIND}, 100)
    assert "時間不夠" in out["summary"] and "失敗" not in out["summary"]


def test_真的呼叫失敗時_總結照舊():
    out = pipeline._parse_model_output(None, {"failed": "timeout"}, 100)
    assert out["summary"] == pipeline._FAILED_SUMMARY


@pytest.mark.parametrize("kw", [{"exc": RuntimeError()}, {"timeout": 30}, {"reason": "沒有端點"}])
def test_審查未完整的_finding_有檔名(kw):
    assert pipeline._incomplete_finding("執行驗證", **kw)["file"] == "(MR 內容)"


def test_審查未完整的_finding_標成程式補的(monkeypatch):
    assert pipeline._is_program_finding(pipeline._incomplete_finding("冷讀", RuntimeError()))


def test_MR_內容的檔名只有一份定義():
    """去重、校準靠檔名分辨程式補的 finding:各處要用同一個常數,不另寫字面字串。"""
    import pathlib
    src = pathlib.Path(pipeline.__file__).parent
    hits = [(p.name, n) for p in sorted(src.glob("*.py"))
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if '"(MR 內容)"' in line]
    assert [name for name, _ in hits] == ["text_fields.py"], hits


# ─────────────────── 5. 模型的一條不能換掉程式補的「審查未完整」 ───────────────────

def _copycat(title: str) -> dict:
    """模型照抄程式的標題、建議寫得比較長:去重比長短時會贏。"""
    return {"file": "(MR 內容)", "line": 0, "severity": "info", "title": title,
            "detail": "已確認沒有問題。", "suggestion": "這一環節已經完成,不需要人工確認。" * 5,
            "citations": []}


def _stages(report):
    return [f.get("stage") for f in report["findings"] if f.get("stage")]


def _cold_read_times_out(monkeypatch):
    async def hang(cfg, mr, texts):
        await asyncio.sleep(3600)
    monkeypatch.setattr(pipeline, "_cold_read", hang)
    monkeypatch.setattr(pipeline, "COLD_READ_TIMEOUT_SECONDS", 0.2)
    return "[冷讀] " + pipeline.INCOMPLETE_TITLE_PREFIX + "(逾時)", "冷讀"


def _prescan_raises(monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("預掃壞了")
    monkeypatch.setattr(pipeline, "prescan", boom)
    stage = pipeline._PRESCAN_STAGE
    return "[" + stage + "] " + pipeline.INCOMPLETE_TITLE_PREFIX, stage


@pytest.mark.parametrize("setup", [_cold_read_times_out, _prescan_raises])
@pytest.mark.parametrize("claims_program", [False, True])
def test_模型照抄未完整的標題也擋不掉(monkeypatch, review, setup, claims_program):
    """模型自己帶 `_source`、`stage` 也一樣:那兩個欄位只由程式寫。"""
    title, stage = setup(monkeypatch)
    fake = _copycat(title)
    if claims_program:
        fake.update(_source=pipeline.PROGRAM_SOURCE, stage=None)
    report, _, _ = review(dict(CLEAN_REPORT, findings=CLEAN_REPORT["findings"] + [fake]), {})
    assert report["decision"] == "needs_human", report["findings"]
    assert stage in _stages(report)
    assert pipeline.review_incomplete(report)


@pytest.mark.parametrize("setup", [_cold_read_times_out, _prescan_raises])
def test_對照組_模型沒有照抄時一樣交人工(monkeypatch, review, setup):
    _, stage = setup(monkeypatch)
    report, _, _ = review(CLEAN_REPORT, {})
    assert report["decision"] == "needs_human" and stage in _stages(report)


def _program(title, severity="minor"):
    return {"file": "a.sql", "line": 1, "severity": severity, "title": title, "detail": "",
            "suggestion": "", "citations": [], "_source": pipeline.PROGRAM_SOURCE}


def _model(title, severity="info", suggestion="很長的建議" * 20):
    return {"file": "a.sql", "line": 1, "severity": severity, "title": title, "detail": "",
            "suggestion": suggestion, "citations": [{"code": "R-201"}]}


@pytest.mark.parametrize("program_first", [True, False])
def test_去重_程式的留下_相近又不比它嚴重的模型那條丟掉(program_first):
    prog, model = _program("[R002] 使用 SELECT *"), _model("[R002] 使用 SELECT *", "minor")
    out = pipeline.sanitize_findings({"findings": [prog, model] if program_first else [model, prog]})
    assert out["findings"] == [prog]


@pytest.mark.parametrize("program_first", [True, False])
def test_去重_相近但比程式的嚴重時兩條都留(program_first):
    """程式那條在前時,模型那條(建議較長)也不能把它換掉。"""
    prog, model = _program("[R002] 使用 SELECT *"), _model("[R002] 使用 SELECT *", "blocker")
    out = pipeline.sanitize_findings({"findings": [prog, model] if program_first else [model, prog]})
    assert sorted(f["severity"] for f in out["findings"]) == ["blocker", "minor"]
    assert prog in out["findings"]


def test_去重_兩條程式的不互相去重():
    a, b = _program("[冷讀] 審查未完整"), _program("[冷讀] 審查未完整(逾時)")
    assert pipeline.sanitize_findings({"findings": [a, b]})["findings"] == [a, b]


def test_去重_模型之間照舊留較完整的那條():
    short, long_ = _model("欄位別名不一致", suggestion=""), _model("欄位別名不一致")
    assert pipeline.sanitize_findings({"findings": [short, long_]})["findings"] == [long_]


def test_去重_不同檔案的不算相近():
    prog = _program("[R002] 使用 SELECT *")
    other = dict(_model("[R002] 使用 SELECT *", "minor"), file="b.sql")
    assert pipeline.sanitize_findings({"findings": [other, prog]})["findings"] == [other, prog]


def test_模型自己帶的程式欄位在整理時拿掉():
    fd = dict(_model("x"), _source=pipeline.PROGRAM_SOURCE, stage="冷讀")
    out = pipeline.normalize_findings({"summary": "", "findings": [fd]})["findings"][0]
    assert "_source" not in out and "stage" not in out
