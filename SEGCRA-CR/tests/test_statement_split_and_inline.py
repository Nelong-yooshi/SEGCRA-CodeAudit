"""切語句與貼行內留言的兩個邊界。

  * 切語句(`pipeline._rendered_starts`、`dbt_affected._statement_keys`)只略過「切不出語句」這種預期
    中的情況(解析錯誤、tokenizer 錯誤、巢狀太深);程式自己的錯往外丟,由環節隔離記成未完整,
    不默默變成「沒有行號」
  * 沒有對應行號的 finding(line <= 0)直接貼一般留言,開頭寫「無對應行號」,不拿第 0 行去開
    行內討論、再靠 API 失敗分流
"""
import pytest
import sqlglot.errors

from orchestrator import dbt_affected, pipeline
from toolbox import gitlab


# ─────────────────── 切語句 ───────────────────

@pytest.mark.parametrize("error", [sqlglot.errors.ParseError("x"), sqlglot.errors.TokenError("x"),
                                   RecursionError()])
@pytest.mark.parametrize("split", [lambda: pipeline._rendered_starts("SELECT 1"),
                                   lambda: dbt_affected._statement_keys("SELECT 1")])
def test_切不出語句時回_None(monkeypatch, error, split):
    def fail(sql):
        raise error
    monkeypatch.setattr(pipeline, "_parse", fail)
    monkeypatch.setattr(dbt_affected, "_parse", fail)
    assert split() is None


@pytest.mark.parametrize("split", [lambda: pipeline._rendered_starts("SELECT 1"),
                                   lambda: dbt_affected._statement_keys("SELECT 1")])
def test_程式自己的錯往外丟_不默默當成切不出來(monkeypatch, split):
    def bug(sql):
        raise AttributeError("程式的錯")
    monkeypatch.setattr(pipeline, "_parse", bug)
    monkeypatch.setattr(dbt_affected, "_parse", bug)
    with pytest.raises(AttributeError):
        split()


def test_真的切不出來的SQL照舊回_None():
    assert pipeline._rendered_starts("SELECT (") in (None, {})
    assert dbt_affected._statement_keys("SELECT (") in (None, [])


# ─────────────────── 行內留言 ───────────────────

@pytest.fixture
def api(monkeypatch):
    calls = []

    def fake(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        if method == "GET":
            return {"diff_refs": {"base_sha": "b", "head_sha": "h", "start_sha": "s"}}
        if path.endswith("/discussions") and kw.get("json", {}).get("position", {}).get("new_line") == 99:
            raise RuntimeError("行號不在 diff 裡")
        return {}
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "_api", fake)
    return calls


@pytest.mark.parametrize("line", [0, -1, True, None, "3"])
def test_沒有對應行號時直接貼一般留言(api, line):
    gitlab.post_inline_comment("12", "models/a.sql", line, "內容", "major")
    assert len(api) == 1
    method, path, body = api[0]
    assert (method, path) == ("POST", "/merge_requests/12/notes")
    assert body["body"].startswith("`models/a.sql`(無對應行號)")
    assert ":0`" not in body["body"] and "內容" in body["body"]


def test_有行號時開行內討論(api):
    gitlab.post_inline_comment("12", "models/a.sql", 5, "內容", "major")
    assert [(m, p) for m, p, _ in api] == [("GET", "/merge_requests/12"),
                                           ("POST", "/merge_requests/12/discussions")]
    assert api[1][2]["position"]["new_line"] == 5


def test_行號不在diff裡時退成一般留言_寫出行號(api):
    gitlab.post_inline_comment("12", "models/a.sql", 99, "內容", "major")
    assert api[-1][1] == "/merge_requests/12/notes"
    assert api[-1][2]["body"].startswith("`models/a.sql:99`")
