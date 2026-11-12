"""回寫 MR:`diff_refs` 只取一次;每一步寫回都看結果,分清楚「被拒絕」與「不知道有沒有建立」。

- 回寫前取一次 MR(`get_mr_refs`,不取檔案內容),同一份結果比對 head、也拿來開行內討論,
  不再每條 finding 重取。
- 行內討論被 GitLab 拒絕(4xx,例如行號對不上 diff):照舊退成一般留言。
- 連線中斷、逾時、5xx:GitLab 可能已經建立了討論、只是回應沒送到,**不改貼**(免得同一條
  出現兩次),記下來、在總結列出(每一條都列)。
- 總結、標籤、commit status 也都看結果:總結不重貼;標籤與 commit status 重送不會重複,
  失敗重送;沒寫上的記在報告、印一行 log,commit status 或標籤沒寫上時總結寫明。
- webhook 審查未完成的通知:兩步各自看結果,log 不印例外訊息。

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
    """記下每個請求;`discussions` 決定開行內討論時怎麼回:狀態碼,或要丟的例外。
    `notes`、`label`、`status` 同樣;給清單時依序取用(最後一個重複用),模擬「失敗後重送成功」。"""

    def __init__(self, discussions=201, mr_status=200, diff_refs=REFS, notes=201,
                 label=200, status=201, discussion_list=None, resolve=200):
        self.calls = []
        self.sent = []                  # (method, path, json body):驗送出去的內容
        self.discussions, self.mr_status, self.diff_refs, self.notes = (
            discussions, mr_status, diff_refs, notes)
        self.label, self.status = label, status
        # discussion_list:列討論時回的清單(None = 回 404,模擬問不出來);resolve:標已解決的回應
        self.discussion_list, self.resolve = discussion_list, resolve

    @staticmethod
    def _reply(spec, req, n):
        spec = spec[min(n, len(spec) - 1)] if isinstance(spec, list) else spec
        if isinstance(spec, Exception):
            raise spec
        return httpx.Response(spec, json={"id": 1}, request=req)

    def __call__(self, method, url, **kw):
        path = url.split("/api/v4/projects/1", 1)[1]
        self.calls.append((method, path))
        self.sent.append((method, path, kw.get("json")))
        req = httpx.Request(method, url)
        if method == "GET" and path.endswith("/discussions"):
            if self.discussion_list is None:
                return httpx.Response(404, json={}, request=req)
            page = int((kw.get("params") or {}).get("page", 1))
            chunk = self.discussion_list[(page - 1) * 100:page * 100]
            return httpx.Response(200, json=chunk, request=req)
        if method == "PUT" and "/discussions/" in path:
            if isinstance(self.resolve, Exception):
                raise self.resolve
            return httpx.Response(self.resolve, json={"id": "d"}, request=req)
        if method == "GET" and path == "/merge_requests/7":
            return httpx.Response(self.mr_status, json={"sha": HEAD, "diff_refs": self.diff_refs},
                                  request=req)
        if method == "POST" and path.endswith("/discussions"):
            if isinstance(self.discussions, Exception):
                raise self.discussions
            return httpx.Response(self.discussions, json={"id": "d"}, request=req)
        if method == "POST" and path.endswith("/notes"):
            return self._reply(self.notes, req, self.count("POST", "/notes") - 1)
        if method == "PUT" and path == "/merge_requests/7":
            return self._reply(self.label, req, self.count("PUT", "/merge_requests/7") - 1)
        if method == "POST" and path.startswith("/statuses/"):
            return self._reply(self.status, req, sum(1 for m, p in self.calls
                                                     if m == "POST" and p.startswith("/statuses/")) - 1)
        return httpx.Response(404, json={}, request=req)

    def count(self, method, suffix):
        return sum(1 for m, p in self.calls if m == method and p.endswith(suffix))


@pytest.fixture
def real(monkeypatch):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", "https://gitlab.invalid")
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", "1")
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", "test-write-token")

    monkeypatch.setattr(gitlab, "RETRY_DELAY_SECONDS", 0)

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
    assert _post(**REFS) == {"ok": False, "failure": "uncertain"}
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
    ('{"ok": false, "failure": "uncertain"}', False),
    ("Error executing tool gitlab__post_inline_comment: ReadTimeout", False),   # 工具丟例外
    ('{"ok": "true"}', False),
    ("[]", False),
    ("", False),
])
def test_有沒有確定貼上(result, expected):
    assert pipeline._posted(result) is expected


def test_總結列出沒有確定貼上的_每一條都列():
    """看 MR 的人拿不到伺服器上的報告檔:超過二十條的部分也要列出檔名與行號。"""
    assert pipeline._unposted_section([]) == ""
    few = [{"file": "sql/a.sql", "line": 3, "title": "門檻寫錯"}]
    text = pipeline._unposted_section(few)
    assert "有 1 條意見沒有確定貼上" in text and "`sql/a.sql:3` 門檻寫錯" in text
    many = [{"file": f"f{i}.sql", "line": i, "title": f"title{i}"} for i in range(25)]
    text = pipeline._unposted_section(many)
    assert "有 25 條" in text and "- `f19.sql:19` title19" in text
    for i in range(20, 25):
        assert f"- `f{i}.sql:{i}`\n" in text + "\n" and f"title{i}" not in text
    assert text.count("\n- ") == 25 and "其餘" not in text


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
            return json.dumps({"ok": False, "failure": "uncertain"})
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


# ─────────────────── 寫回的結果:總結、標籤、commit status ───────────────────

@pytest.mark.parametrize("result,expected", [
    ('{"ok": true}', None),
    ('{"ok": true, "state": "failed"}', None),
    ('{"ok": false, "failure": "rejected"}', "rejected"),
    ('{"ok": false, "failure": "uncertain"}', "uncertain"),
    ('{"ok": false, "failure": "no_sha", "error": "缺 sha"}', "no_sha"),
    ('{"ok": false, "failure": "' + SECRET + '"}', "failed"),     # 不認得的種類不帶進報告
    ('{"error": "state 必須是 success/failed/pending"}', "failed"),
    ("Error executing tool gitlab__post_summary: ReadTimeout", "failed"),
    ('{"ok": "true"}', "failed"),
    ("[]", "failed"),
    ("", "failed"),
    (None, "failed"),
])
def test_寫上了沒有(result, expected):
    assert gitlab.write_failure(result) == expected


_UNCERTAIN = [httpx.ReadTimeout(SECRET), httpx.ConnectError(SECRET), 500, 503]


@pytest.mark.parametrize("failure", _UNCERTAIN)
def test_總結逾時或_5xx_不重貼(real, failure):
    fake = real(FakeGitLab(notes=failure))
    out = gitlab.post_summary("7", "內容", 80, "ok")
    assert json.loads(out) == {"ok": False, "failure": "uncertain"}
    assert fake.count("POST", "/notes") == 1
    assert SECRET not in out


def test_總結被拒絕時回報被拒絕(real):
    fake = real(FakeGitLab(notes=403))
    assert json.loads(gitlab.post_summary("7", "內容", 80, "ok")) == {"ok": False, "failure": "rejected"}
    assert fake.count("POST", "/notes") == 1


def test_總結貼上了(real):
    fake = real(FakeGitLab())
    assert json.loads(gitlab.post_summary("7", "內容", 80, "ok")) == {"ok": True}
    assert fake.count("POST", "/notes") == 1


@pytest.mark.parametrize("failure", _UNCERTAIN)
def test_標籤失敗重送一次(real, failure):
    fake = real(FakeGitLab(label=[failure, 200]))
    assert json.loads(gitlab.set_label("7", "ai-review::blocked")) == {"ok": True}
    assert fake.count("PUT", "/merge_requests/7") == 2


@pytest.mark.parametrize("failure", _UNCERTAIN)
def test_標籤重送後還是失敗時回報種類(real, failure):
    fake = real(FakeGitLab(label=failure))
    out = gitlab.set_label("7", "ai-review::blocked")
    assert json.loads(out) == {"ok": False, "failure": "uncertain"}
    assert fake.count("PUT", "/merge_requests/7") == gitlab.LABEL_ATTEMPTS == 2
    assert SECRET not in out


def test_標籤被拒絕時不重送(real):
    fake = real(FakeGitLab(label=403))
    assert json.loads(gitlab.set_label("7", "x")) == {"ok": False, "failure": "rejected"}
    assert fake.count("PUT", "/merge_requests/7") == 1


def _statuses(fake):
    return sum(1 for m, p in fake.calls if m == "POST" and p.startswith("/statuses/"))


@pytest.mark.parametrize("failure", _UNCERTAIN)
def test_commit_status_失敗重送(real, failure):
    fake = real(FakeGitLab(status=[failure, failure, 201]))
    out = gitlab.set_commit_status(HEAD, "failed", mr_id="7")
    assert json.loads(out) == {"ok": True, "state": "failed"}
    assert _statuses(fake) == gitlab.STATUS_ATTEMPTS == 3


@pytest.mark.parametrize("failure", _UNCERTAIN)
def test_commit_status_重送後還是失敗時回報種類(real, failure):
    fake = real(FakeGitLab(status=failure))
    out = gitlab.set_commit_status(HEAD, "success", mr_id="7")
    assert json.loads(out) == {"ok": False, "failure": "uncertain"}
    assert _statuses(fake) == 3
    assert SECRET not in out


def test_commit_status_被拒絕時不重送(real):
    fake = real(FakeGitLab(status=422))
    assert json.loads(gitlab.set_commit_status(HEAD, "success", mr_id="7")) == {
        "ok": False, "failure": "rejected"}
    assert _statuses(fake) == 1


def test_commit_status_缺_sha_時不送出_回報種類(real):
    fake = real(FakeGitLab())
    got = json.loads(gitlab.set_commit_status("", "success", mr_id="7"))
    assert got["ok"] is False and got["failure"] == "no_sha"
    assert fake.calls == []


def test_重送之間有等待(real, monkeypatch):
    waits = []
    monkeypatch.setattr(gitlab, "RETRY_DELAY_SECONDS", 0.25)
    monkeypatch.setattr(gitlab.time, "sleep", waits.append)
    real(FakeGitLab(status=500))
    gitlab.set_commit_status(HEAD, "failed", mr_id="7")
    assert waits == [0.25, 0.25]


# ─────────────────── review_mr:每一步回寫都接住結果 ───────────────────

def _review_with(monkeypatch, tmp_path, replies: dict, findings=()):
    """走整條 review_mr(mock 模式),把指定的寫回工具換成回固定結果的函式;記下呼叫順序。"""
    from orchestrator import tool_hub
    from orchestrator.config import load_config
    fixtures, output = tmp_path / "fixtures", tmp_path / "out"
    fixtures.mkdir()
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))
    (fixtures / "mr_7.json").write_text(json.dumps({
        "title": "t", "description": "", "sha": HEAD, "diff_refs": REFS,
        "files": [{"path": "sql/a.sql", "diff": "@@ -1 +1 @@\n+SELECT a FROM t WHERE id = 1",
                   "full_content": "SELECT a FROM t WHERE id = 1"}]}, ensure_ascii=False),
        encoding="utf-8")

    async def run_agent(cfg, profile, system, user, hub, meta=None, **kw):
        return json.dumps({"summary": "ok", "findings": list(findings)})
    monkeypatch.setattr(pipeline, "run_agent", run_agent)

    order, bodies = [], []

    def fake(name, reply):
        def fn(**kw):
            order.append(name)
            if name == "post_summary":
                bodies.append(kw["body"])
            if isinstance(reply, Exception):
                raise reply
            return reply
        fn.__name__ = name
        return fn
    groups = dict(tool_hub.TOOL_GROUPS)
    groups["gitlab"] = [fake(f.__name__, replies.get(f.__name__, json.dumps({"ok": True})))
                        if f.__name__ in ("post_summary", "set_label", "set_commit_status",
                                          "resolve_stale_comments", "post_inline_comment") else f
                        for f in groups["gitlab"]]
    monkeypatch.setattr(tool_hub, "TOOL_GROUPS", groups)
    report = asyncio.run(pipeline.review_mr(load_config(), "7"))
    return report, order, bodies


_FAILED = json.dumps({"ok": False, "failure": "uncertain"})


def test_commit_status_與標籤先設_總結最後貼(monkeypatch, tmp_path):
    report, order, bodies = _review_with(monkeypatch, tmp_path, {})
    # 先收掉上一輪的行內意見,再貼這一輪的;commit status 與標籤接著設,總結最後
    assert order[0] == "resolve_stale_comments", order
    assert order[-3:] == ["set_commit_status", "set_label", "post_summary"]
    assert report["_writeback"] == {"unposted": 0}
    assert "回寫沒有完成" not in bodies[0]


def test_收掉上一輪標不掉也不影響這次(monkeypatch, tmp_path):
    """舊意見留在上面只是雜訊,新的照樣要貼、決策照樣要寫回去。"""
    report, order, bodies = _review_with(monkeypatch, tmp_path,
                                         {"resolve_stale_comments": RuntimeError(SECRET)})
    assert order[-3:] == ["set_commit_status", "set_label", "post_summary"]
    assert report["_resolved_stale"] == 0
    assert "_writeback" in report


def test_收掉幾個會記進報告(monkeypatch, tmp_path):
    report, _, _ = _review_with(monkeypatch, tmp_path, {
        "resolve_stale_comments": json.dumps({"ok": True, "resolved": 3, "failed": 1})})
    assert report["_resolved_stale"] == 3


def test_commit_status_沒寫上時總結與_log_寫明(monkeypatch, tmp_path, capsys):
    report, order, bodies = _review_with(monkeypatch, tmp_path, {"set_commit_status": _FAILED})
    assert report["_writeback"] == {"unposted": 0, "commit_status": "uncertain"}
    assert "merge 閘門沒有寫上" in bodies[0] and "不確定有沒有寫上" in bodies[0]
    assert "回寫沒有完成:merge 閘門(commit status)" in capsys.readouterr().out


def test_標籤沒寫上時總結寫明(monkeypatch, tmp_path):
    report, _, bodies = _review_with(monkeypatch, tmp_path, {
        "set_label": json.dumps({"ok": False, "failure": "rejected"})})
    assert report["_writeback"] == {"unposted": 0, "label": "rejected"}
    assert "標籤 `ai-review::" in bodies[0] and "GitLab 拒絕了這個請求" in bodies[0]
    assert "merge 閘門沒有寫上" not in bodies[0]


def test_總結沒貼上時記在報告與_log(monkeypatch, tmp_path, capsys):
    report, _, _ = _review_with(monkeypatch, tmp_path, {"post_summary": _FAILED})
    assert report["_writeback"] == {"unposted": 0, "summary": "uncertain"}
    assert "回寫沒有完成:總結留言" in capsys.readouterr().out


def test_寫回工具丟例外時記成寫入工具出錯_不印例外訊息(monkeypatch, tmp_path, capsys):
    report, order, bodies = _review_with(monkeypatch, tmp_path, {
        "set_commit_status": RuntimeError(SECRET), "set_label": RuntimeError(SECRET)})
    assert report["_writeback"] == {"unposted": 0, "commit_status": "failed", "label": "failed"}
    assert order[-3:] == ["set_commit_status", "set_label", "post_summary"]
    assert SECRET not in capsys.readouterr().out and SECRET not in bodies[0]


def test_沒寫上的種類都有說明():
    assert set(pipeline._WRITE_FAILURE_TEXT) == set(gitlab.WRITE_FAILURES)


def test_dry_run_也記下有沒有寫上():
    class Hub:
        async def call_json(self, name, args):
            return {}

        async def call(self, name, args):
            if name == "gitlab__post_inline_comment" and args["body"].startswith("[R001]"):
                return _FAILED
            return _FAILED if name == "gitlab__post_summary" else json.dumps({"ok": True})

    hits = [{"rule": r, "severity": "major", "message": "x"} for r in ("R001", "R002")]
    entry = {"path": "sql/a.sql", "rules": hits, "lint": []}
    report = asyncio.run(pipeline._dry_run_report(Hub(), "7", {"files": []}, [entry], None, None))
    assert report["_writeback"] == {"unposted": 1, "summary": "uncertain"}


# ─────────────────── webhook:審查未完成的通知 ───────────────────

def _notify(monkeypatch, summary, status, head_sha=None, refs=None):
    import scripts.webhook_server as ws
    calls = []

    def post_summary(*a):
        calls.append("post_summary")
        if isinstance(summary, Exception):
            raise summary
        return summary

    def set_commit_status(sha, *a):
        calls.append(("set_commit_status", sha))
        if isinstance(status, Exception):
            raise status
        return status

    def get_mr_refs(mr_id):
        calls.append("get_mr_refs")
        return json.dumps(refs or {"sha": HEAD, **REFS})

    def must_not_run(*a, **k):
        raise AssertionError("只為了一個 sha 不該把每個檔案都取一遍")
    monkeypatch.setattr(gitlab, "post_summary", post_summary)
    monkeypatch.setattr(gitlab, "set_commit_status", set_commit_status)
    monkeypatch.setattr(gitlab, "get_mr_refs", get_mr_refs)
    monkeypatch.setattr(gitlab, "get_mr_diff", must_not_run)
    ws._notify_incomplete(7, "逾時", head_sha)
    return calls


_OK = json.dumps({"ok": True})


def test_通知_兩步都寫上(monkeypatch, capsys):
    calls = _notify(monkeypatch, _OK, _OK, head_sha=HEAD)
    out = capsys.readouterr().out
    assert calls == ["post_summary", ("set_commit_status", HEAD)]
    assert "已留言告知審查未完成" in out and "commit status 設為失敗" in out


def test_通知_沒有_sha_時只取_MR_的位置(monkeypatch):
    calls = _notify(monkeypatch, _OK, _OK)
    assert calls == ["post_summary", "get_mr_refs", ("set_commit_status", HEAD)]


@pytest.mark.parametrize("status", [
    json.dumps({"ok": False, "failure": "no_sha", "error": "缺 sha"}),
    json.dumps({"error": "state 必須是 success/failed/pending"}),
    _FAILED,
    RuntimeError(SECRET),
])
def test_通知_commit_status_沒設上時不印成已設為失敗(monkeypatch, capsys, status):
    _notify(monkeypatch, _OK, status, head_sha=HEAD)
    out = capsys.readouterr().out
    assert "commit status 設為失敗" not in out and "commit status 沒有設上" in out
    assert SECRET not in out


@pytest.mark.parametrize("summary", [_FAILED, RuntimeError(SECRET)])
def test_通知_留言沒貼上時照樣設_commit_status_不印例外訊息(monkeypatch, capsys, summary):
    calls = _notify(monkeypatch, summary, _OK, head_sha=HEAD)
    out = capsys.readouterr().out
    assert ("set_commit_status", HEAD) in calls
    assert "留言沒有貼上" in out and "已留言" not in out and SECRET not in out


# ─────────────────── 收掉上一輪的行內意見(重審不洗版) ───────────────────
#
# 總結只有一則(改寫上一則),但行內意見是每次 N 條重貼一遍 —— 不收掉的話同一行會疊
# 好幾輪的意見,看 MR 的人分不出哪些還有效。重審時把「上一輪我們開的、而且沒有人回過」
# 的討論標成已解決。

MARK = gitlab._INLINE_MARKER


def _disc(did="d1", body=None, resolvable=True, resolved=False, notes=None):
    body = f"{MARK}\n**[major]** 舊意見" if body is None else body
    if notes is None:
        notes = [{"id": 1, "body": body, "resolvable": resolvable, "resolved": resolved}]
    return {"id": did, "notes": notes}


def _resolved_ids(fake):
    return [p.rsplit("/", 1)[1] for m, p, _ in fake.sent if m == "PUT" and "/discussions/" in p]


def test_行內意見帶著記號(real):
    """沒有記號,下一輪就認不出哪些討論是我們開的。"""
    fake = real(FakeGitLab())
    assert _post(**REFS) == {"ok": True}
    body = next(j["body"] for m, p, j in fake.sent if m == "POST" and p.endswith("/discussions"))
    assert body.startswith(MARK)


def test_上一輪我們開的討論會被標成已解決(real):
    fake = real(FakeGitLab(discussion_list=[_disc("d1"), _disc("d2")]))
    out = json.loads(gitlab.resolve_stale_comments("7"))
    assert out == {"ok": True, "resolved": 2, "failed": 0}
    assert _resolved_ids(fake) == ["d1", "d2"]
    assert all(j == {"resolved": True} for m, p, j in fake.sent if m == "PUT")


@pytest.mark.parametrize("d,why", [
    (_disc(notes=[{"id": 1, "body": f"{MARK}\n舊意見", "resolvable": True, "resolved": False},
                  {"id": 2, "body": "我覺得這條不對", "resolvable": True, "resolved": False}]),
     "有人回覆過的是對話,標成已解決會把人的話收起來"),
    (_disc(body="請看一下這段"), "沒有記號的是別人開的討論"),
    (_disc(resolved=True), "已經解決的再送一次只是多一個請求"),
    (_disc(resolvable=False), "不可解決的討論(例如一般留言)標不動"),
    ({"id": "d9"}, "形狀不對(沒有 notes)"),
])
def test_這些討論不要動(real, d, why):
    fake = real(FakeGitLab(discussion_list=[d]))
    assert json.loads(gitlab.resolve_stale_comments("7"))["resolved"] == 0, why
    assert _resolved_ids(fake) == [], why


def test_標不掉只數一個數字_不影響其他的(real):
    """舊意見留在上面不影響這次審查(新的照樣貼),所以不可以讓它中斷回寫。"""
    fake = real(FakeGitLab(discussion_list=[_disc("d1"), _disc("d2")], resolve=403))
    out = json.loads(gitlab.resolve_stale_comments("7"))
    assert out == {"ok": True, "resolved": 0, "failed": 2}
    assert len(_resolved_ids(fake)) == 2, "每一個都試過"


def test_標不掉的例外也不往外丟(real):
    real(FakeGitLab(discussion_list=[_disc("d1")], resolve=httpx.ReadTimeout(SECRET)))
    out = gitlab.resolve_stale_comments("7")
    assert json.loads(out) == {"ok": True, "resolved": 0, "failed": 1}
    assert SECRET not in out


def test_問不出討論清單就不標(real):
    fake = real(FakeGitLab(discussion_list=None))
    assert json.loads(gitlab.resolve_stale_comments("7")) == {"ok": True, "resolved": 0,
                                                              "failed": 0}
    assert _resolved_ids(fake) == []


def test_滿頁會翻下一頁(real):
    """一頁 100 則。只看第一頁的話,討論多的 MR 會收不乾淨。"""
    fake = real(FakeGitLab(discussion_list=[_disc(f"d{i}") for i in range(150)]))
    assert json.loads(gitlab.resolve_stale_comments("7"))["resolved"] == 150
    pages = [p for m, p, _ in fake.sent if m == "GET" and p.endswith("/discussions")]
    assert len(pages) == 2


def test_翻頁有上限(real):
    """收尾有時間上限,不可以無上限追下去。"""
    fake = real(FakeGitLab(discussion_list=[_disc(f"d{i}") for i in range(100 * 8)]))
    json.loads(gitlab.resolve_stale_comments("7"))
    got = sum(1 for m, p, _ in fake.sent if m == "GET" and p.endswith("/discussions"))
    assert got == gitlab.MAX_DISCUSSION_PAGES


def test_不接受不合法的_MR_編號(real):
    real(FakeGitLab(discussion_list=[]))
    with pytest.raises(ValueError):
        gitlab.resolve_stale_comments("7/../../../other")
