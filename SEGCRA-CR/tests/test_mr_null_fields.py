"""MR 的欄位在、值是 null 時照常審查 —— 不呼叫 LLM、不連 GitLab。

GitLab 對沒填說明的 MR 回 `"description": null`。`.get("description", "")` 只在鍵不存在時給預設值,
拿到的是 None,抽規則碼時 `" ".join` 丟 TypeError,整份審查失敗(結束碼 5、不能合併)。
三層各自防:取 MR 的地方(真實 GitLab)、review_mr 拿到 MR 之後(任何來源)、抽規則碼本身。

另外兩件同一次實測看到的:剩的時間不夠、沒呼叫模型時,總結不能寫成「呼叫失敗」;
「審查未完整」的 finding 要有檔名(整份 MR 層級的用「(MR 內容)」)。
"""
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

