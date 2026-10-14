"""只改 dbt 設定(config()、dbt_project.yml、屬性檔)的變更,要有一條 finding 請人確認。

這類變更不會改變展開出來的 SQL,規則層和沙盒都看不出差別:拿掉排程用的 tag、incremental 改成
table、拿掉 unique_key,效果都很大。用變更後的完整內容加上 diff 反推變更前的內容,逐項比較設定。
"""
import asyncio
import difflib
import json

import pytest

from orchestrator import dbt_config_change, pipeline, spec_exec
from orchestrator.config import load_config
from orchestrator.dbt_config_change import WATCHED, ConfigChange, config_changes
from orchestrator.pipeline import (_DBT_CONFIG_OTHER_TITLE, _DBT_CONFIG_RISKS, _DBT_CONFIG_TITLE,
                                   enforce_dbt_config_change)
from toolbox import gitlab

MODEL = """{{ config(
    materialized='incremental',
    unique_key='id',
    tags=['daily', 'core'],
) }}

select id, amount
from {{ ref('txn_log') }}
where amount > 0
{% if is_incremental() %}
  and dt > (select max(dt) from {{ this }})
{% endif %}
"""
PROJECT = """name: sample
models:
  sample:
    staging:
      +materialized: view
      +tags:
        - daily
    marts:
      +materialized: incremental
      description: |
        materialized: table 這一行是說明文字
"""
PROPERTIES = """version: 2
models:
  - name: mrt_sample
    description: 樣本
    config:
      materialized: incremental
      tags: ['daily']
    columns:
      - name: id
        tags: ['pii']
"""


def _diff(old, new, context=3):
    lines = difflib.unified_diff(old.splitlines(True), new.splitlines(True), "a", "b", n=context)
    return "".join(ln for ln in lines if not ln.startswith(("---", "+++")))


def _file(path, old, new, full=True, context=3):
    f = {"path": path, "diff": _diff(old, new, context)}
    if full:
        f["full_content"] = new
    return f


def _changes(path, old, new, **kw):
    return config_changes([_file(path, old, new, **kw)])


def _keys(path, old, new, **kw):
    found = _changes(path, old, new, **kw)
    return [(c.keys, c.other, c.unknown) for c in found]


# ─────────────────── model 的 config() ───────────────────

@pytest.mark.parametrize("full", [True, False])
@pytest.mark.parametrize("new, keys", [
    (MODEL.replace("'daily', ", ""), ("tags",)),
    (MODEL.replace("'incremental'", "'table'"), ("materialized",)),
    (MODEL.replace("    unique_key='id',\n", ""), ("unique_key",)),
    (MODEL.replace("unique_key='id'", "unique_key=['id', 'dt']"), ("unique_key",)),
    (MODEL.replace("tags=", "incremental_strategy='append',\n    tags="), ("incremental_strategy",)),
    (MODEL.replace("tags=", "enabled=false,\n    tags="), ("enabled",)),
    (MODEL.replace("tags=", "full_refresh=true,\n    tags="), ("full_refresh",)),
    (MODEL.replace("tags=", 'post_hook="delete from x",\n    tags='), ("post_hook",)),
    (MODEL.replace("tags=", "alias='mrt_other',\n    tags="), ("alias",)),
    (MODEL.replace("'incremental'", "'table'").replace("'daily', ", ""), ("tags", "materialized")),
])
def test_改到受關注的設定(new, keys, full):
    assert _keys("models/mrt_sample.sql", MODEL, new, full=full) == [(keys, False, False)]


@pytest.mark.parametrize("new", [
    MODEL.replace("'incremental'", '"incremental"'),                         # 引號
    MODEL.replace("tags=['daily', 'core'],", "tags=[\n      'daily',\n      'core'\n    ],"),
    MODEL.replace("{{ config(", "{{- config("),
    MODEL.replace("amount > 0", "amount > 1"),                               # 只改 SQL
    MODEL.replace("select id", "-- 說明\nselect id"),
])
def test_寫法不同但設定沒變_或只改SQL_不算(new):
    assert _changes("models/mrt_sample.sql", MODEL, new) == []


def test_字典寫法與參數寫法相同_不算():
    old = "{{ config(materialized='table') }}\nselect 1"
    new = "{{ config({'materialized': 'table'}) }}\nselect 1"
    assert _changes("models/m.sql", old, new) == []


def test_整個config拿掉():
    new = MODEL.split(") }}\n", 1)[1]
    assert _keys("models/mrt_sample.sql", MODEL, new) == [
        (("tags", "materialized", "unique_key"), False, False)]


def test_原本沒有config_新加上去():
    old = "select id\nfrom {{ ref('txn_log') }}\n"
    new = "{{ config(materialized='table') }}\n" + old
    assert _keys("models/m.sql", old, new) == [(("materialized",), False, False)]


def test_改到其他設定_只標其他():
    new = MODEL.replace("tags=", "persist_docs={'relation': true},\n    tags=")
    assert _keys("models/mrt_sample.sql", MODEL, new) == [((), True, False)]


def test_同一個檔案有兩個config呼叫():
    old = "{{ config(materialized='table') }}\n{{ config(tags=['daily']) }}\nselect 1\n"
    new = old.replace("['daily']", "[]")
    assert _keys("models/m.sql", old, new) == [(("tags",), False, False)]


@pytest.mark.parametrize("old, new", [
    ("{{ config(materialized='incr' ~ 'emental') }}\nselect 1\n",
     "{{ config(materialized='ta' ~ 'ble') }}\nselect 1\n"),
    ("{{ config(**cfg) }}\nselect 1\n", "{{ config(**other) }}\nselect 1\n"),
    ("{% set m = 'table' %}{{ config(materialized=m) }}\nselect 1\n",
     "{% set m = 'table' %}{{ config(materialized=m, **x) }}\nselect 1\n"),
])
def test_看不懂的寫法有變_算看不出改了什麼(old, new):
    assert _keys("models/m.sql", old, new) == [((), False, True)]


@pytest.mark.parametrize("old", [
    "{% set m = 'incremental' %}\n{{ config(materialized=m) }}\nselect 1\n",
    "{%- set cfg = {'materialized': 'incremental'} -%}\n{{ config(**cfg) }}\nselect 1\n",
    "{% set m = 'incremental' %}\n{{ config(materialized=('x' if target.name == 'p' else m)) }}\n"
    "select 1\n",
])
def test_參數用的變數在set那一行被改到_算看不出改了什麼(old):
    new = old.replace("'incremental'", "'table'")
    assert _keys("models/m.sql", old, new) == [((), False, True)]


def test_參數用的變數沒被改到_不算():
    old = "{% set m = 'incremental' %}\n{{ config(materialized=m) }}\nselect m.id from t m\n"
    assert _changes("models/m.sql", old, old.replace("m.id", "m.amount")) == []


def test_參數名稱與函式名稱不算變數():
    old = ("{% set materialized = 1 %}{% set var = 2 %}\n"
           "{{ config(materialized=var('m'), target_schema=target.schema) }}\nselect 1\n")
    new = old.replace("= 1", "= 3").replace("= 2", "= 4")
    assert _changes("models/m.sql", old, new) == []


def test_看不懂的寫法沒變_不算():
    old = "{{ config(materialized='incr' ~ 'emental') }}\nselect 1\n"
    assert _changes("models/m.sql", old, old.replace("select 1", "select 2")) == []


def test_config_set有變_算看不出改了什麼():
    new = MODEL.replace("select id", "{% do config.set('materialized', 'table') %}\nselect id")
    assert _keys("models/mrt_sample.sql", MODEL, new) == [((), False, True)]


def test_macro裡的config也算():
    old = "{% macro std_config() %}{{ config(materialized='incremental') }}{% endmacro %}\n"
    new = old.replace("incremental", "table")
    assert _keys("macros/std_config.sql", old, new) == [(("materialized",), False, False)]


def test_沒有樣板標記的SQL_不算():
    old = "SELECT dbo.config(materialized) FROM t;\n"
    assert _changes("sql/a.sql", old, old.replace("materialized", "unique_key")) == []


def test_行號是變更後第一個config的位置():
    old = "-- 說明\n\n{{ config(materialized='table') }}\nselect 1\n"
    found = _changes("models/m.sql", old, old.replace("table", "view"))
    assert [c.line for c in found] == [3]


def test_新增或刪除整個檔案_不算():
    assert _changes("models/m.sql", "", MODEL) == []
    assert _changes("models/m.sql", MODEL, "") == []


# ─────────────────── 沒有完整內容、diff 對不上 ───────────────────

def _long_model(**changes):
    body = "\n".join(f"  , col_{i}" for i in range(20))
    cfg = "{{ config(\n    materialized='incremental',\n" + body.replace("  , ", "    # ") + \
        "\n    unique_key='id',\n) }}\nselect 1\n"
    for old, new in changes.items():
        cfg = cfg.replace(old, new)
    return cfg


def test_只有diff_config的開頭不在範圍內_改到的行有設定名稱_算看不出改了什麼():
    old = _long_model()
    new = old.replace("unique_key='id'", "unique_key='dt'")
    assert _keys("models/m.sql", old, new, full=False, context=1) == [((), False, True)]
    assert _keys("models/m.sql", old, new, full=True, context=1) == [(("unique_key",), False, False)]


def test_只有diff_改到的是同名的TSQL變數_不算():
    old = "{{ config(materialized='table') }}\n" + "\n".join(f"select {i}" for i in range(20)) + \
          "\nSET @unique_key = 1 {# 說明 #}\n"
    new = old.replace("@unique_key = 1", "@unique_key = 2")
    assert _changes("models/m.sql", old, new, full=False, context=1) == []


def test_只有diff_改到一般SQL的行_不算():
    old = "{{ config(materialized='table') }}\n" + "\n".join(f"select {i}" for i in range(20)) + \
          "\nwhere enabled = 1\n"
    new = old.replace("enabled = 1", "enabled = 0")
    assert _changes("models/m.sql", old, new, full=False, context=1) == []


def test_diff與完整內容對不上_退回只看diff():
    f = _file("models/mrt_sample.sql", MODEL, MODEL.replace("'incremental'", "'table'"))
    f["full_content"] = "完全不同的內容\n" * 30
    assert [(c.keys, c.line) for c in config_changes([f])] == [(("materialized",), 0)]


def test_有取回的完整內容時_優先用它():
    new = MODEL.replace("'incremental'", "'table'")
    f = _file("models/mrt_sample.sql", MODEL, new, full=False)
    f["full_content"] = "不是這份\n"
    assert [c.line for c in config_changes([f], head_text=lambda p: new)] == [1]


def test_內容太大_不反推_只看diff(monkeypatch):
    monkeypatch.setattr(dbt_config_change, "MAX_TEXT_CHARS", 10)
    found = _changes("models/mrt_sample.sql", MODEL, MODEL.replace("'incremental'", "'table'"))
    assert [(c.keys, c.line) for c in found] == [(("materialized",), 0)]


@pytest.mark.parametrize("diff", [None, "", "(generated)", "@@ 壞掉 @@\n+x", "@@ -1,5 +1,5 @@\n+x\n"])
def test_diff格式不對_不算_也不出錯(diff):
    assert config_changes([{"path": "models/m.sql", "diff": diff, "full_content": MODEL}]) == []


def test_特製的大檔案_不會拖住審查():
    """每一行都改、而且都在同一層的 4 萬行 yml:逐行往上找上層鍵是平方時間(要十幾分鐘)。
    config.set 寫了幾萬次的 model 也一樣。"""
    import time

    n = 40_000
    new = "models:\n" + "".join(f"    k{i}: v\n" for i in range(n))
    diff = (f"@@ -1,{n + 1} +1,{n + 1} @@\n models:\n" + "".join(f"-    k{i}: w\n" for i in range(n))
            + "".join(f"+    k{i}: v\n" for i in range(n)))
    sets = "{% do config.set('a', 1) %}\n" * n
    model_old, model_new = "{{ config(a=1) }}\n" + sets, "{{ config(a=2) }}\n" + sets
    started = time.monotonic()
    config_changes([{"path": "dbt_project.yml", "diff": diff, "full_content": new},
                    _file("models/m.sql", model_old, model_new)])
    assert time.monotonic() - started < 10


def test_處理中出錯_算看不出改了什麼(monkeypatch):
    def boom(*a):
        raise RecursionError
    monkeypatch.setattr(dbt_config_change, "_file_change", boom)
    assert config_changes([{"path": "models/m.sql"}]) == [ConfigChange("models/m.sql", unknown=True)]


def test_輸入不是清單或缺路徑_不出錯():
    assert config_changes(None) == [] and config_changes([None, {"diff": "x"}]) == []


# ─────────────────── dbt_project.yml 與屬性檔 ───────────────────

@pytest.mark.parametrize("full", [True, False])
@pytest.mark.parametrize("new, keys", [
    (PROJECT.replace("        - daily\n", "        - weekly\n"), ("tags",)),
    (PROJECT.replace("        - daily\n", ""), ("tags",)),
    (PROJECT.replace("+materialized: incremental", "+materialized: table"), ("materialized",)),
    (PROJECT.replace("      +materialized: view\n", "      +materialized: view\n      +post-hook: x\n"),
     ("post_hook",)),
])
def test_dbt_project_yml(new, keys, full):
    assert _keys("dbt_project.yml", PROJECT, new, full=full, context=1) == [(keys, False, False)]


def test_dbt_project_yml_多行說明文字裡的字_不算():
    new = PROJECT.replace("table 這一行", "view 這一行")
    assert _changes("dbt_project.yml", PROJECT, new) == []


def test_dbt_project_yml_資料夾名稱剛好是設定名稱_不算():
    old = "models:\n  sample:\n    schema:\n      description: a\n"
    assert _changes("dbt_project.yml", old, old.replace(": a", ": b")) == []


def test_屬性檔的config():
    new = PROPERTIES.replace("tags: ['daily']", "tags: []")
    assert _keys("models/schema.yml", PROPERTIES, new) == [(("tags",), False, False)]


def test_屬性檔的config_寫成一行():
    old = PROPERTIES.replace("    config:\n      materialized: incremental\n      tags: ['daily']\n",
                             "    config: {materialized: incremental, tags: ['daily']}\n")
    new = old.replace("incremental", "table")
    assert _keys("models/schema.yml", old, new) == [(("materialized",), False, False)]


@pytest.mark.parametrize("new", [
    PROPERTIES.replace("tags: ['pii']", "tags: []"),                 # 欄位的 tag
    PROPERTIES.replace("description: 樣本", "description: 新說明"),
])
def test_屬性檔_欄位與說明_不算(new):
    assert _changes("models/schema.yml", PROPERTIES, new) == []


def test_屬性檔_config的其他設定():
    new = PROPERTIES.replace("      tags: ['daily']\n", "      tags: ['daily']\n      persist_docs: {}\n")
    assert _keys("models/schema.yml", PROPERTIES, new) == [((), True, False)]


@pytest.mark.parametrize("full", [True, False])
def test_不是dbt的yml_不算(full):
    old = "deploy:\n  tags:\n    - runner-a\n  script: ./deploy.sh\n"
    assert _changes(".gitlab-ci.yml", old, old.replace("runner-a", "runner-b"), full=full) == []


def test_在dbt專案裡的yml_沒有models區段也看():
    """取回的專案裡的檔案(head_text 有內容)就是 dbt 的檔案,不必靠內容判斷。"""
    old = "snapshots:\n  sample:\n    +tags: ['daily']\n"
    f = _file("snapshot_props.yml", old, old.replace("'daily'", "'weekly'"), full=False)
    head = old.replace("'daily'", "'weekly'")
    assert [c.keys for c in config_changes([f], head_text=lambda p: head)] == [("tags",)]


def test_dbt_project_yml_models以外的區段_不算():
    old = PROJECT + "vars:\n  tags: daily\n"
    assert _changes("dbt_project.yml", old, old.replace("tags: daily", "tags: weekly")) == []


def test_dbt_project_yml_跨行的清單():
    old = "models:\n  sample:\n    +tags: [\n      'daily',\n      'core'\n    ]\n"
    new = old.replace("      'core'\n", "")
    assert _keys("dbt_project.yml", old, new) == [(("tags",), False, False)]


def test_dbt_project_yml_清單項目與上一層同縮排():
    old = "models:\n  sample:\n    +tags:\n    - daily\n    - core\n"
    assert _keys("dbt_project.yml", old, old.replace("    - core\n", "")) == [
        (("tags",), False, False)]


def test_多個檔案各自列出():
    files = [_file("models/a.sql", MODEL, MODEL.replace("'incremental'", "'table'")),
             _file("models/b.sql", MODEL, MODEL.replace("amount > 0", "amount > 1")),
             _file("dbt_project.yml", PROJECT, PROJECT.replace("        - daily\n", ""), context=1)]
    assert [(c.path, c.keys) for c in config_changes(files)] == [
        ("models/a.sql", ("materialized",)), ("dbt_project.yml", ("tags",))]


# ─────────────────── 補的 finding ───────────────────

def test_每個受關注的設定都有風險說明():
    assert set(_DBT_CONFIG_RISKS) == set(WATCHED)


def _mr(*files):
    return {"files": list(files)}


def test_受關注的設定_major_不回顯設定值():
    new = MODEL.replace("'incremental'", "'zz_marker_value'").replace("'daily', ", "")
    report = enforce_dbt_config_change({"findings": []},
                                       _mr(_file("models/mrt_sample.sql", MODEL, new)))
    [f] = report["findings"]
    assert (f["file"], f["line"], f["severity"], f["title"]) == (
        "models/mrt_sample.sql", 1, "major", _DBT_CONFIG_TITLE)
    assert "tags、materialized" in f["detail"] and "zz_marker" not in json.dumps(f, ensure_ascii=False)


def test_同一類風險只寫一次():
    new = MODEL.replace("tags=", "enabled=false,\n    tags=").replace("'daily', ", "")
    [f] = enforce_dbt_config_change({"findings": []},
                                    _mr(_file("models/m.sql", MODEL, new)))["findings"]
    assert f["detail"].count(_DBT_CONFIG_RISKS["tags"]) == 1


def test_看不出改了什麼_也是major():
    new = MODEL.replace("select id", "{% do config.set('materialized', 'table') %}\nselect id")
    [f] = enforce_dbt_config_change({"findings": []},
                                    _mr(_file("models/m.sql", MODEL, new)))["findings"]
    assert f["severity"] == "major" and "看不出" in f["detail"]


def test_只改到其他設定_info():
    new = MODEL.replace("tags=", "persist_docs={'relation': true},\n    tags=")
    [f] = enforce_dbt_config_change({"findings": []},
                                    _mr(_file("models/m.sql", MODEL, new)))["findings"]
    assert (f["severity"], f["title"], f["suggestion"]) == ("info", _DBT_CONFIG_OTHER_TITLE, "")


def test_模型輸出同標題的info_蓋不掉major():
    new = MODEL.replace("'incremental'", "'table'")
    model = {"file": "models/m.sql", "line": 0, "severity": "info", "title": _DBT_CONFIG_TITLE,
             "detail": "沒問題", "suggestion": "", "citations": []}
    report = enforce_dbt_config_change({"findings": [model]},
                                       _mr(_file("models/m.sql", MODEL, new)))
    assert [f["severity"] for f in report["findings"]] == ["major"]
    assert report["findings"][0]["detail"] != "沒問題"


def test_用取回的dbt專案內容比較():
    """真實 GitLab 模式的檔案沒有 full_content,完整內容來自取回的 dbt 專案。"""
    class _Ctx:
        @staticmethod
        def head_text(path):
            return new

    new = MODEL.replace("'incremental'", "'table'")
    f = _file("models/mrt_sample.sql", MODEL, new, full=False)
    [finding] = enforce_dbt_config_change({"findings": []}, _mr(f), _Ctx())["findings"]
    assert finding["line"] == 1


def test_沒改到設定_報告不變():
    report = {"findings": [{"title": "x"}]}
    new = MODEL.replace("amount > 0", "amount > 1")
    assert enforce_dbt_config_change(report, _mr(_file("models/m.sql", MODEL, new))) == {
        "findings": [{"title": "x"}]}


# ─────────────────── 整條管線 ───────────────────

@pytest.fixture
def review(monkeypatch, tmp_path):
    fixtures, output = tmp_path / "fixtures", tmp_path / "output"
    fixtures.mkdir()
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    async def fake_generate(cfg, code, text, profile_name=None):
        return {"schema_ddl": [], "conditions": [], "cases": []}, [], []

    monkeypatch.setattr(spec_exec, "generate_cases", fake_generate)
    monkeypatch.setattr(spec_exec, "execute_cases", lambda sql, plan: {
        "engine": "stub", "sandbox_error": None, "testdata_error": None, "sql_error": None,
        "case_results": [], "mismatches": []})

    async def fake_run_agent(cfg, profile, system, user, hub, **kw):
        return json.dumps({"summary": "x", "findings": []}, ensure_ascii=False)

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)

    def _review(files, dry_run=False):
        mr = {"title": "調整設定", "description": "例行維護", "sha": "e1a1" + "0" * 36,
              "files": files}
        (fixtures / "mr_900.json").write_text(json.dumps(mr, ensure_ascii=False), encoding="utf-8")
        return asyncio.run(pipeline.review_mr(load_config(), "900", dry_run=dry_run))

    return _review


@pytest.mark.parametrize("dry_run", [False, True])
def test_整條管線_只改config_有那條finding_不得自動放行(review, dry_run):
    new = MODEL.replace("'daily', ", "")
    report = review([_file("models/mrt_sample.sql", MODEL, new)], dry_run=dry_run)
    assert [(f["file"], f["severity"]) for f in report["findings"]
            if f["title"] == _DBT_CONFIG_TITLE] == [("models/mrt_sample.sql", "major")]
    if not dry_run:
        assert report["decision"] != "auto_approved"
