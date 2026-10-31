"""回寫 MR:`diff_refs` 只取一次;行內討論失敗時分清楚「被拒絕」與「不知道有沒有建立」。

- 回寫前取一次 MR(`get_mr_refs`,不取檔案內容),同一份結果比對 head、也拿來開行內討論,
  不再每條 finding 重取。
- 行內討論被 GitLab 拒絕(4xx,例如行號對不上 diff):照舊退成一般留言。
- 連線中斷、逾時、5xx:GitLab 可能已經建立了討論、只是回應沒送到,**不改貼**(免得同一條
  出現兩次),記下來、在總結列出。

GitLab 的回應用替換 `httpx.request` 模擬(位址用 .invalid,token 用測試值),`_api` 照樣
`raise_for_status`,所以狀態碼的分流走的是真的程式。
"""
import asyncio
import json

import httpx
import pytest

from orchestrator import pipeline
from toolbox import gitlab

BASE, HEAD, START = "a" * 40, "b" * 40, "c" * 40
REFS = {"base_sha": BASE, "head_sha": HEAD, "start_sha": START}
SECRET = "ZZ_HOST_192.0.2.10:443_ZZ"


class FakeGitLab:
    """記下每個請求;`discussions` 決定開行內討論時怎麼回:狀態碼,或要丟的例外。"""

    def __init__(self, discussions=201, mr_status=200, diff_refs=REFS, notes=201):
        self.calls = []
        self.discussions, self.mr_status, self.diff_refs, self.notes = (
            discussions, mr_status, diff_refs, notes)

    def __call__(self, method, url, **kw):
        path = url.split("/api/v4/projects/1", 1)[1]
        self.calls.append((method, path))
        req = httpx.Request(method, url)
        if method == "GET" and path == "/merge_requests/7":
            return httpx.Response(self.mr_status, json={"sha": HEAD, "diff_refs": self.diff_refs},
                                  request=req)
        if method == "POST" and path.endswith("/discussions"):
            if isinstance(self.discussions, Exception):
                raise self.discussions
            return httpx.Response(self.discussions, json={"id": "d"}, request=req)
        if method == "POST" and path.endswith("/notes"):
            if isinstance(self.notes, Exception):
                raise self.notes
            return httpx.Response(self.notes, json={"id": 1}, request=req)
        return httpx.Response(404, json={}, request=req)

    def count(self, method, suffix):
        return sum(1 for m, p in self.calls if m == method and p.endswith(suffix))


@pytest.fixture
def real(monkeypatch):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", "https://gitlab.invalid")
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", "1")
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", "test-write-token")

    def install(fake):
        monkeypatch.setattr(httpx, "request", fake)
        return fake
    return install


def _post(**kw):
    args = {"mr_id": "7", "file": "sql/a.sql", "line": 3, "body": "內容", "severity": "major"}
    args.update(kw)
    return json.loads(gitlab.post_inline_comment(**args))


# ─────────────────── get_mr_refs ───────────────────

def test_get_mr_refs_只取_MR_不取檔案(real):
    fake = real(FakeGitLab())
    got = json.loads(gitlab.get_mr_refs("7"))
    assert got == {"sha": HEAD, **REFS}
    assert fake.calls == [("GET", "/merge_requests/7")]


def test_get_mr_refs_diff_refs_是_null_時三個欄位是_None(real):
    real(FakeGitLab(diff_refs=None))
    got = json.loads(gitlab.get_mr_refs("7"))
    assert got["base_sha"] is None and got["head_sha"] is None and got["start_sha"] is None


def test_get_mr_refs_不接受不合法的_MR_編號(real):
    real(FakeGitLab())
    with pytest.raises(ValueError):
        gitlab.get_mr_refs("7/../../../other")


# ─────────────────── post_inline_comment ───────────────────

def test_帶了_diff_refs_就不再取_MR(real):
    fake = real(FakeGitLab())
    assert _post(**REFS) == {"ok": True}
    assert fake.count("GET", "/merge_requests/7") == 0
    assert fake.count("POST", "/discussions") == 1 and fake.count("POST", "/notes") == 0


@pytest.mark.parametrize("given", [
    {},                                                      # 沒帶
    {"base_sha": BASE, "head_sha": HEAD},                    # 沒帶齊
    {"base_sha": BASE, "head_sha": HEAD, "start_sha": "main"},   # 不是完整的 commit 編號
])
def test_沒帶齊或不合法時自己取一次(real, given):
    fake = real(FakeGitLab())
    assert _post(**given) == {"ok": True}
    assert fake.count("GET", "/merge_requests/7") == 1 and fake.count("POST", "/discussions") == 1


@pytest.mark.parametrize("status", [400, 403, 404, 422, 429])
def test_被_GitLab_拒絕時照舊退成一般留言(real, status):
    """4xx 代表 GitLab 收到、明確拒絕(例如行號對不上 diff):一定沒有建立,改貼不會重複。"""
    fake = real(FakeGitLab(discussions=status))
    assert _post(**REFS) == {"ok": True}
    assert fake.count("POST", "/notes") == 1


@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout(SECRET),
    httpx.ConnectError(SECRET),
    httpx.RemoteProtocolError(SECRET),
    500, 502, 503,
])
def test_不知道有沒有建立時不改貼_回報沒有確定貼上(real, failure):
    fake = real(FakeGitLab(discussions=failure))
    assert _post(**REFS) == {"ok": False, "uncertain": True}
    assert fake.count("POST", "/notes") == 0


@pytest.mark.parametrize("fake_kw", [
    {"mr_status": 500},                     # 取 MR 失敗
    {"diff_refs": None},                    # GitLab 還沒算好 diff_refs
])
def test_拿不到位置時直接貼一般留言(real, fake_kw):
    """還沒送出任何行內討論,貼一般留言不會重複。"""
    fake = real(FakeGitLab(**fake_kw))
    assert _post() == {"ok": True}
    assert fake.count("POST", "/discussions") == 0 and fake.count("POST", "/notes") == 1


def test_沒有行號時照舊直接貼一般留言(real):
    fake = real(FakeGitLab())
    assert _post(line=0, **REFS) == {"ok": True}
    assert fake.calls == [("POST", "/merge_requests/7/notes")]


def test_get_mr_refs_不開放給審查模型():
    from orchestrator.tool_hub import MODEL_TOOLS
    assert "gitlab__get_mr_refs" not in MODEL_TOOLS


# ─────────────────── pipeline:判斷有沒有貼上、總結列出沒貼上的 ───────────────────

@pytest.mark.parametrize("result,expected", [
    ('{"ok": true}', True),
    ('{"ok": false, "uncertain": true}', False),
    ("Error executing tool gitlab__post_inline_comment: ReadTimeout", False),   # 工具丟例外
    ('{"ok": "true"}', False),
    ("[]", False),
    ("", False),
])
def test_有沒有確定貼上(result, expected):
    assert pipeline._posted(result) is expected


def test_總結列出沒有確定貼上的_最多列二十條():
    assert pipeline._unposted_section([]) == ""
    few = [{"file": "sql/a.sql", "line": 3, "title": "門檻寫錯"}]
    text = pipeline._unposted_section(few)
    assert "有 1 條意見沒有確定貼上" in text and "`sql/a.sql:3` 門檻寫錯" in text
    many = [{"file": f"f{i}.sql", "line": i, "title": f"t{i}"} for i in range(25)]
    text = pipeline._unposted_section(many)
    assert "有 25 條" in text and "`f19.sql:19` t19" in text and "f20.sql" not in text
    assert "其餘 5 條" in text


def test_回寫用取回的_diff_refs_沒貼上的記在報告與總結(monkeypatch, tmp_path):
    """走整條 review_mr(mock 模式):第一條 finding 回「沒有確定貼上」,其餘照常。"""
    from orchestrator.config import load_config
    fixtures, output = tmp_path / "fixtures", tmp_path / "out"
    fixtures.mkdir()
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))
    (fixtures / "mr_7.json").write_text(json.dumps({
        "title": "t", "description": "", "sha": HEAD, "diff_refs": REFS,
        "files": [{"path": "sql/a.sql", "diff": "@@ -1 +1 @@\n+DELETE FROM t",
                   "full_content": "DELETE FROM t"}]}, ensure_ascii=False), encoding="utf-8")

    async def run_agent(cfg, profile, system, user, hub, meta=None, **kw):
        return json.dumps({"summary": "ok", "findings": []})
    monkeypatch.setattr(pipeline, "run_agent", run_agent)

    seen, summaries = [], []
    real_post = gitlab.post_inline_comment

    def post(**kw):
        seen.append({k: kw.get(k) for k in ("base_sha", "head_sha", "start_sha")})
        if len(seen) == 1:
            return json.dumps({"ok": False, "uncertain": True})
        return real_post(**kw)

    def summary(mr_id, body, score, verdict):
        summaries.append(body)
        return json.dumps({"ok": True})
    from orchestrator import tool_hub
    groups = dict(tool_hub.TOOL_GROUPS)
    groups["gitlab"] = [post if f is gitlab.post_inline_comment else
                        summary if f is gitlab.post_summary else f for f in groups["gitlab"]]
    post.__name__, summary.__name__ = "post_inline_comment", "post_summary"
    monkeypatch.setattr(tool_hub, "TOOL_GROUPS", groups)

    report = asyncio.run(pipeline.review_mr(load_config(), "7"))
    assert seen and all(s == REFS for s in seen)            # 用取回的 diff_refs
    assert report["_writeback"] == {"unposted": 1}
    first = report["findings"][0]
    assert "有 1 條意見沒有確定貼上" in summaries[0] and first["title"] in summaries[0]


@pytest.mark.parametrize("got", [["x"], "head", 3, None])
def test_取回的_MR_形狀不對時當作沒有資料_不中斷回寫(got):
    class Hub:
        async def call_json(self, *a, **k):
            return got
    now = asyncio.run(pipeline._current_mr(Hub(), "7"))
    assert now == {}
    assert pipeline._superseded(now, HEAD) is False
