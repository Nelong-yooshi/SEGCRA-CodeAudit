"""dbt 樣板自己的 Jinja 語法錯誤:dbt 解析專案時一樣失敗,整個專案編譯失敗 → blocker。

哪些算、哪些不算,照 dbt-core 1.12 的實測(dbt parse / dbt compile):

- 整個專案失敗:區塊沒閉合、運算式寫錯、不認得的標籤、不認得的濾器、重複定義的 block、
  model 檔裡的 `{% snapshot %}`、docs 區塊裡的語法錯誤;model、analysis、singular test 的
  目錄都一樣
- dbt 照常通過:model 檔裡的 `{% docs %}` / `{% test %}` / `{% materialization %}` 區塊
  (編譯出的 SQL 不含區塊內容)、呼叫不存在的 macro(到 compile 才失敗)

展開器對 `{% test %}` / `{% materialization %}` 仍然中止展開(區塊裡的 SQL 會在別的地方執行,
丟掉就逃過預掃),但不算語法錯誤:照舊是「展開失敗」的 major。snapshot 目錄的檔一律不報
blocker:展開器把它當 model 展開,`{% snapshot %}` 本身就會失敗,分不出真的語法錯誤。
"""
import asyncio

import pytest

from orchestrator import dbt_context, dbt_project, pipeline
from orchestrator.config import DBT_DATABASE_PLACEHOLDER
from orchestrator.dbt_context import prepare_dbt_context
from orchestrator.dbt_render import DbtBlockInModel, render_model
from tests.test_dbt_affected import CFG, _Hub, _Loader, _mr

DB = DBT_DATABASE_PLACEHOLDER


# ─────────────────── 展開器:語法錯誤與其他失敗分得開 ───────────────────

@pytest.mark.parametrize("source,line", [
    ("{% if true %}\nselect 1 as a\n", 1),
    ("select {{ 1 + 1 } as a\n", 1),
    ("select 1\n{% foo %}\n", 2),
    ("select '{{ 'x' | no_such_filter }}' as a\n", 1),
    ("{% block b %}1{% endblock %}{% block b %}2{% endblock %}\nselect 1\n", 1),
    ("{% snapshot s %}select 1{% endsnapshot %}\n", 1),
    ("{% docs d1 %}{% if true %}x{% enddocs %}\nselect 1\n", 1),
])
def test_dbt_一樣解析不了的是語法錯誤(source, line):
    r = render_model("m.sql", source=source, database=DB)
    assert not r.ok and r.syntax_error and r.error_line == line, (r.error, r.error_line)


def test_語法錯誤的行號算上開頭被去掉的空行():
    r = render_model("m.sql", source="\n\n  \nselect 1\n{% foo %}\n", database=DB)
    assert r.syntax_error and r.error_line == 5


@pytest.mark.parametrize("source", [
    "select {{ 1 // 0 }} as a\n",                     # 執行時的錯誤
    "select {{ no_such_macro() }} as a\n",            # dbt 也是到 compile 才失敗
    "{% test t1(model) %}select 1{% endtest %}\nselect 1 as a\n",
    "{% materialization m1, default %}select 1{% endmaterialization %}\nselect 1 as a\n",
])
def test_其他失敗不是語法錯誤(source):
    r = render_model("m.sql", source=source, database=DB)
    assert not r.ok and not r.syntax_error and r.error_line == 0, r.error


@pytest.mark.parametrize("tag", ["test t1(model)", "materialization m1, default"])
def test_model_裡的_test_與_materialization_照樣中止展開(tag):
    name = tag.split()[0]
    source = "{% " + tag + " %}SELECT secret{% end" + name + " %}\nSELECT 1\n"
    r = render_model("m.sql", source=source, database=DB)
    assert not r.ok and r.error.startswith("DbtBlockInModel:")
    assert "dbt 解析得過" in r.error and "secret" not in r.error
    assert issubclass(DbtBlockInModel, Exception)


def test_model_裡的_docs_照_dbt_丟掉():
    r = render_model("m.sql", source="{% docs d1 %}說明 DOCS_BODY{% enddocs %}\nselect 1 as a\n",
                     database=DB)
    assert r.ok, r.error
    assert r.sql.strip() == "select 1 as a" and "DOCS_BODY" not in r.sql


def test_成功時沒有語法錯誤():
    r = render_model("m.sql", source="select {{ 1 + 1 }} as a\n", database=DB)
    assert r.ok and not r.syntax_error and r.error_line == 0


# ─────────────────── 預掃:只有 dbt 整份解析的目錄才記 ───────────────────

@pytest.fixture(autouse=True)
def _fresh_project(monkeypatch):
    monkeypatch.setattr(dbt_context, "_summarize_isolated", dbt_context._summarize)
    dbt_project.clear_cache()

    def in_process(path, *, timeout_s=None, **kwargs):
        return render_model(path, **kwargs)
    monkeypatch.setattr(pipeline, "render_model_isolated", in_process)
    yield
    dbt_project.clear_cache()


BAD = "{% if true %}\nselect 1 as a\n"
PROJECT_YML = ("name: sample\nmodel-paths: ['models']\nanalysis-paths: ['analyses']\n"
               "test-paths: ['tests']\nsnapshot-paths: ['snapshots']\nmacro-paths: ['macros']\n")


def _prescan(path, text, prefix=""):
    files = {"dbt_project.yml": PROJECT_YML, "macros/m.sql": "{% macro m() %}1{% endmacro %}",
             "models/ok.sql": "select 1 as a\n", path: text}
    mr = _mr([prefix + path])
    mr["files"][0]["full_content"] = text
    cfg = {**CFG, "project_dir": prefix.rstrip("/")}
    ctx = prepare_dbt_context(mr, cfg, real_mode=True, load=_Loader(files))
    [entry] = [e for e in asyncio.run(pipeline.prescan(_Hub(), mr["files"], cfg, ctx))
               if e["path"] == prefix + path]
    return entry


@pytest.mark.parametrize("path", ["models/bad.sql", "models/sub/bad.sql", "analyses/bad.sql",
                                  "tests/bad.sql"])
def test_model_analysis_singular_test_的語法錯誤記下行號(path):
    entry = _prescan(path, BAD)
    assert entry["dbt_syntax_line"] == 1 and entry.get("dbt_render_error")


def test_snapshot_目錄的檔不記語法錯誤():
    entry = _prescan("snapshots/snap.sql",
                     "{% snapshot snap %}\nselect 1 as a\n{% endsnapshot %}\n")
    assert entry.get("dbt_render_error") and "dbt_syntax_line" not in entry


@pytest.mark.parametrize("text", ["{% test t1(model) %}select 1{% endtest %}\nselect 1 as a\n",
                                  "select {{ 1 // 0 }} as a\n"])
def test_不是語法錯誤的展開失敗不記(text):
    entry = _prescan("models/bad.sql", text)
    assert entry.get("dbt_render_error") and "dbt_syntax_line" not in entry


def test_專案不在_repo_根目錄時照樣認得目錄():
    entry = _prescan("models/bad.sql", BAD, prefix="dbt/")
    assert entry["dbt_syntax_line"] == 1


def test_不知道目錄設定時不記():
    assert pipeline._parsed_whole_by_dbt("models/bad.sql", None) is False
    assert pipeline._parsed_whole_by_dbt("models/bad.sql", dbt_context.DbtContext(error="x")) is False


# ─────────────────── finding ───────────────────

def _entry(**kw):
    return {"path": "models/bad.sql", "rules": [], "lint": [],
            "dbt_render_error": "TemplateSyntaxError: unexpected '}' SECRET_TEXT", **kw}


def test_語法錯誤補_blocker_寫出檔名與行號_不帶錯誤訊息():
    report = pipeline.enforce_dbt_render_failure({"findings": []}, [_entry(dbt_syntax_line=3)])
    [fd] = report["findings"]
    assert fd["severity"] == "blocker" and fd["title"] == pipeline._TEMPLATE_SYNTAX_TITLE
    assert fd["file"] == "models/bad.sql" and fd["line"] == 3
    assert "第 3 行附近" in fd["detail"] and "SECRET_TEXT" not in fd["detail"]


def test_行號不知道時不寫行號():
    [fd] = pipeline.enforce_dbt_render_failure({"findings": []}, [_entry(dbt_syntax_line=0)])["findings"]
    assert fd["severity"] == "blocker" and fd["line"] == 0 and "行附近" not in fd["detail"]


@pytest.mark.parametrize("line", [None, True, "3"])
def test_沒有語法錯誤的行號時照舊補_major(line):
    entry = _entry() if line is None else _entry(dbt_syntax_line=line)
    [fd] = pipeline.enforce_dbt_render_failure({"findings": []}, [entry])["findings"]
    assert fd["severity"] == "major" and fd["title"] == pipeline._RENDER_FAIL_TITLE


def test_語法錯誤時決策是擋下():
    report = pipeline.enforce_dbt_render_failure(
        {"summary": "x", "score": 100, "findings": []}, [_entry(dbt_syntax_line=1)])
    mr = {"files": [{"path": "models/bad.sql", "diff": "@@ -0,0 +1 @@\n+select 1"}]}
    policy = {"auto_approve": {"max_diff_lines": 30, "min_score": 0,
                               "allowed_severities": ["info", "major"], "forbid_pending_hints": True}}
    out = pipeline.apply_policy(pipeline.apply_rubric(report), mr, policy)
    assert out["decision"] == "blocked", out.get("decision")
