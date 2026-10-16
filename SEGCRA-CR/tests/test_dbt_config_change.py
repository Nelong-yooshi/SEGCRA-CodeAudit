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
    lines = list(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "a", "b", n=context))
    # 只去掉開頭的 --- / +++ 兩行檔頭:刪掉的 SQL 註解行(`-` + `-- x`)也以 --- 開頭
    assert not lines or (lines[0].startswith("--- ") and lines[1].startswith("+++ "))
    return "".join(lines[2:])


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
    assert _changes("models/a.sql", old, old.replace("materialized", "unique_key")) == []


def test_行號是變更後第一個config的位置():
    old = "-- 說明\n\n{{ config(materialized='table') }}\nselect 1\n"
    found = _changes("models/m.sql", old, old.replace("table", "view"))
    assert [c.line for c in found] == [3]


def test_新增的model_列出它設定的受關注項目():
    [c] = _changes("models/m.sql", "", MODEL)
    assert (c.added, c.keys, c.other, c.unknown, c.line) == (True, ("tags", "materialized", "unique_key"),
                                                           False, False, 1)


def test_新增的model沒有config_或刪除檔案():
    assert _changes("models/m.sql", "", "select 1\n") == []
    [c] = _changes("models/m.sql", MODEL, "")
    assert c.deleted and not (c.keys or c.unknown)


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
    assert [(c.keys, c.line) for c in config_changes([f])] == [(("materialized",), 2)]


def test_有取回的完整內容時_優先用它():
    new = MODEL.replace("'incremental'", "'table'")
    f = _file("models/mrt_sample.sql", MODEL, new, full=False)
    f["full_content"] = "不是這份\n"
    assert [c.line for c in config_changes([f], head_text=lambda p: new)] == [1]


def test_內容太大_不反推_只看diff(monkeypatch):
    monkeypatch.setattr(dbt_config_change, "MAX_TEXT_CHARS", 10)
    found = _changes("models/mrt_sample.sql", MODEL, MODEL.replace("'incremental'", "'table'"))
    assert [(c.keys, c.line) for c in found] == [(("materialized",), 2)]


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
    assert config_changes([{"path": "models/m.sql"}]) == [
        ConfigChange("models/m.sql", unknown=True, error="RecursionError")]


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
    f = _file("snapshots/snapshot_props.yml", old, old.replace("'daily'", "'weekly'"), full=False)
    head = old.replace("'daily'", "'weekly'")
    assert [c.keys for c in config_changes([f], head_text=lambda p: head)] == [("tags",)]


@pytest.mark.parametrize("old, new", [
    (PROJECT + "vars:\n  start: '2024-01-01'\n", PROJECT + "vars:\n  start: '2025-01-01'\n"),
    (PROJECT + "on-run-end:\n  - \"delete from a\"\n", PROJECT + "on-run-end:\n  - \"delete from b\"\n"),
    ("profile: dev\n" + PROJECT, "profile: prod\n" + PROJECT),
], ids=["vars", "on-run-end", "profile"])
def test_dbt_project_yml_models以外的區段_算看不出改了什麼(old, new):
    """on-run-end 是會執行的 SQL、vars 會改變 var() 的值、profile 換了連線目標:都不能不報。"""
    assert _keys("dbt_project.yml", old, new) == [((), False, True)]


def test_屬性檔_models以外的區段_不算():
    """屬性檔裡 sources 這類區段不影響 model 的物化與排程(dbt_project.yml 不一樣,見上一個測試)。"""
    old = PROPERTIES + "sources:\n  - name: raw\n    loader: a\n"
    assert _changes("models/schema.yml", old, old.replace("loader: a", "loader: b")) == []


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
        layout, prefix = None, ""

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


# ─────────────────── 看不懂的寫法不可以安靜漏掉 ───────────────────

HOOKS = """models:
  sample:
    +post-hook: |
      delete from {{ this }}
      where dt < '2020-01-01'
    +materialized: table
"""


@pytest.mark.parametrize("old, new, expect", [
    # 跨行的 hook:改到中間那行(hook 裡的 SQL 哪裡都沒掃到)
    (HOOKS, HOOKS.replace("'2020-01-01'", "'2099-01-01'"), (("post_hook",), False, False)),
    # tab 縮排:YAML 不允許,層級無法判斷
    ("models:\n  sample:\n    +materialized: view\n",
     "models:\n  sample:\n\t+materialized: table\n", (("materialized",), False, True)),  # 刪掉的舊行
    # merge key 指向的預設值被改
    ("defaults: &d\n  materialized: view\nmodels:\n  sample:\n    <<: *d\n",
     "defaults: &d\n  materialized: table\nmodels:\n  sample:\n    <<: *d\n", ((), False, True)),
    # models 底下對不到已知的鍵(資料夾改名,設定套用的範圍就變了)
    ("models:\n  sample:\n    staging:\n      +materialized: view\n",
     "models:\n  sample:\n    stage:\n      +materialized: view\n", ((), False, True)),
], ids=["多行hook", "tab縮排", "merge-key", "資料夾改名"])
def test_yaml_看不懂或藏在多行裡的改動_不會安靜漏掉(old, new, expect):
    assert _keys("dbt_project.yml", old, new) == [expect]


def test_yaml_多行說明與欄位測試_仍然不報():
    old = "models:\n  - name: m\n    description: |\n      第一行\n      第二行\n"
    assert _changes("models/schema.yml", old, old.replace("第二行", "改過")) == []


MULTI_SET = """{% set cfg = {
    'materialized': 'incremental',
    'unique_key': 'id'
} %}
{{ config(**cfg) }}
select 1
"""
CONFIG_SET = """{% do config.set(
    'materialized',
    'incremental'
) %}
{{ config(tags=['daily']) }}
select 1
"""


@pytest.mark.parametrize("old, new", [
    (MULTI_SET, MULTI_SET.replace("'unique_key': 'id'", "'unique_key': 'dt'")),
    (MULTI_SET.replace("**cfg", "tags=['daily']"),
     MULTI_SET.replace("**cfg", "tags=['daily']").replace("'id'", "'dt'")),
    (CONFIG_SET, CONFIG_SET.replace("    'incremental'", "    'table'")),
], ids=["多行set被config用到", "多行set一律算", "多行config.set只改中間那行"])
@pytest.mark.parametrize("full", [True, False])
def test_sql_多行的set與config_set_只改中間那行_算看不出改了什麼(old, new, full):
    assert [c.unknown for c in _changes("models/m.sql", old, new, full=full, context=1)] == [True]


# ─────────────────── 用位置判斷是不是 dbt 檔 ───────────────────

@pytest.mark.parametrize("path, old", [
    ("docs/example/dbt_project.yml", PROJECT),
    (".gitlab-ci.yml", "models:\n  sample:\n    +materialized: view\n"),
    ("sql/templates/m.sql", MODEL),
], ids=["說明文件裡的範例", "頂層有models的CI設定", "一般SQL範本"])
def test_不在dbt目錄裡的檔案_不比較(path, old):
    new = old.replace("view", "table").replace("'incremental'", "'table'")
    assert _changes(path, old, new) == []


def test_專案在子目錄時_照專案目錄判斷():
    new = MODEL.replace("'incremental'", "'table'")
    f = _file("dbt/models/m.sql", MODEL, new)
    found = enforce_dbt_config_change({"findings": []}, _mr(f), None, {"project_dir": "dbt"})
    assert [x["file"] for x in found["findings"]] == ["dbt/models/m.sql"]
    root = enforce_dbt_config_change({"findings": []}, _mr(f), None, {"project_dir": ""})
    assert root["findings"] == []


def test_有取回的專案時_照它的目錄設定():
    """model-paths 改成 transform/ 時,models/ 底下的檔案不是 dbt 讀的。"""
    from orchestrator.dbt_context import DbtContext
    from orchestrator.dbt_project import DbtLayout, DbtProject

    project = DbtProject(ok=True, files={}, model_paths=("transform",), macro_paths=("macros",))
    ctx = DbtContext(prefix="", project=project, layout=DbtLayout(("transform", "macros")))
    new = MODEL.replace("'incremental'", "'table'")
    files = [_file("transform/m.sql", MODEL, new), _file("models/m.sql", MODEL, new)]
    found = enforce_dbt_config_change({"findings": []}, _mr(*files), ctx)["findings"]
    assert [x["file"] for x in found] == ["transform/m.sql"]


def test_副檔名大寫的sql_不是dbt的model():
    """dbt 只讀小寫的 .sql;接線(dbt_context、dbt_impact)也是這樣判斷。"""
    new = MODEL.replace("'incremental'", "'table'")
    assert _changes("models/MODEL.SQL", MODEL, new) == []


# ─────────────────── 新增、刪除整個檔案的 finding ───────────────────

def _one(files, ctx=None):
    [finding] = enforce_dbt_config_change({"findings": []}, _mr(*files), ctx)["findings"]
    return finding


@pytest.mark.parametrize("new, severity", [
    ("{{ config(materialized='incremental', full_refresh=true) }}\nselect 1\n", "major"),
    ("{{ config(materialized='table', post_hook='delete from x') }}\nselect 1\n", "major"),
    ("{{ config(**cfg) }}\nselect 1\n", "major"),
    ("{{ config(materialized='table') }}\nselect 1\n", "info"),
], ids=["full_refresh", "hook", "看不懂", "一般設定"])
def test_新增的model_高風險設定major_其他info(new, severity):
    finding = _one([_file("models/m.sql", "", new)])
    assert finding["severity"] == severity and finding["line"] == 1
    assert finding["title"] == (pipeline._DBT_CONFIG_TITLE if severity == "major"
                                else pipeline._DBT_CONFIG_ADDED_TITLE)
    assert "delete from x" not in finding["detail"]


def test_新增的屬性檔_列出設定():
    new = "version: 2\nmodels:\n  - name: m\n    config:\n      tags: ['daily']\n"
    finding = _one([_file("models/schema.yml", "", new)])
    assert finding["severity"] == "info" and "tags" in finding["detail"] and finding["line"] == 5


@pytest.mark.parametrize("path, old", [("models/m.sql", MODEL), ("models/schema.yml", PROPERTIES)])
def test_刪除model或屬性檔_info(path, old):
    finding = _one([_file(path, old, "", full=False)])
    assert (finding["severity"], finding["title"]) == ("info", pipeline._DBT_CONFIG_DELETED_TITLE)


# ─────────────────── 行號、出錯 ───────────────────

def test_yaml的finding_帶第一個改到的設定行():
    new = PROJECT.replace("+materialized: incremental", "+materialized: table")
    finding = _one([_file("dbt_project.yml", PROJECT, new)])
    assert finding["line"] == PROJECT.split("\n").index("      +materialized: incremental") + 1
    removed = PROJECT.replace("        - daily\n", "")
    assert _one([_file("dbt_project.yml", PROJECT, removed)])["line"] == 7     # 刪除處


def test_處理中出錯_finding寫出例外類型(monkeypatch):
    def boom(*a):
        raise KeyError("x")
    monkeypatch.setattr(dbt_config_change, "_file_change", boom)
    finding = _one([_file("models/m.sql", MODEL, MODEL.replace("'incremental'", "'table'"))])
    assert finding["severity"] == "major" and "非預期錯誤:KeyError" in finding["detail"]


def test_內附的fixture都不會走到出錯的那條路():
    """出錯時會記成「看不出改了什麼」;現有的 fixture 不該靠這條路得到結果。"""
    import glob

    from orchestrator.config import PKG_ROOT

    seen = 0
    for path in sorted(glob.glob(str(PKG_ROOT / "**" / "*.json"), recursive=True)):
        if ".venv" in path or "testdata_cache" in path:
            continue
        try:
            data = json.loads(open(path, encoding="utf-8").read())
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and isinstance(data.get("files"), list):
            seen += 1
            assert [c.path for c in config_changes(data["files"]) if c.error] == [], path
    assert seen >= 30


def test_新增的屬性檔_一行寫的config也列出():
    new = "version: 2\nmodels:\n  - name: m\n    config: {materialized: table, tags: ['daily']}\n"
    [c] = _changes("models/schema.yml", "", new)
    assert (c.added, c.keys, c.line) == (True, ("tags", "materialized"), 4)


def test_特製的大量跨行set_不會拖住審查():
    """改到的行與跨行的 {% set %} 兩兩比對會是平方時間。最壞的情況是改到的行都不在 set 裡
    (每一行都要跟每一段比過才知道不在):1 萬行 × 1 萬段。"""
    import time

    n = 10_000                                  # 約 41 萬字元:在 MAX_TEXT_CHARS 之內,會完整反推
    old = "{{ config(materialized='table') }}\n" + "".join(
        f"{{% set v{i} = [\n  {i}\n] %}}\nselect {i}\n" for i in range(n))
    new = old.replace("\nselect ", "\nselect 9")
    # 直接組 diff(difflib 處理這麼大的檔案本身就很慢):每個 set 後面那一行 SQL 都改了
    diff = "".join(f"@@ -{4 * i + 5} +{4 * i + 5} @@\n-select {i}\n+select 9{i}\n" for i in range(n))
    started = time.monotonic()
    found = config_changes([{"path": "models/m.sql", "diff": diff, "full_content": new}])
    assert time.monotonic() - started < 5 and found == []          # 只改 SQL,設定沒變


def test_hook的多行字串裡出現設定名稱_算hook():
    """跨行字串裡長得像「鍵:」的行是字串的內容,不是設定。"""
    old = HOOKS.replace("      where dt", "      materialized: x\n      where dt")
    new = old.replace("materialized: x", "materialized: y")
    assert _keys("dbt_project.yml", old, new) == [(("post_hook",), False, False)]


def test_跨行的大括號設定_逐項看():
    old = "version: 2\nmodels:\n  - name: m\n    config: {\n      materialized: incremental\n    }\n"
    new = old.replace("incremental", "table")
    assert _keys("models/schema.yml", old, new) == [(("materialized",), False, False)]


def test_資料夾名稱剛好是設定名稱_底下改了設定_算底下那個():
    old = "models:\n  sample:\n    schema:\n      +materialized: view\n"
    assert _keys("dbt_project.yml", old, old.replace("view", "table")) == [
        (("materialized",), False, False)]



# ─────────────────── 改名搬進 / 搬出 dbt 的目錄 ───────────────────

FULL_REFRESH = "{{ config(materialized='incremental', full_refresh=true) }}\nselect 1\n"


def test_從dbt目錄外改名搬進來_內容沒變_也算新增():
    """內容沒變時 diff 是空的;不另外判斷,帶著 full_refresh 的 model 就會安靜進來。"""
    f = {"path": "models/m.sql", "old_path": "scratch/m.sql", "diff": "", "full_content": FULL_REFRESH}
    [c] = config_changes([f])
    assert (c.added, c.keys) == (True, ("materialized", "full_refresh"))
    finding = _one([f])
    assert finding["severity"] == "major"


def test_搬進來但看不到內容_算看不出():
    f = {"path": "models/m.sql", "old_path": "scratch/m.sql", "diff": ""}
    assert config_changes([f]) == [ConfigChange("models/m.sql", unknown=True, added=True)]


def test_改名搬出dbt目錄_等於刪除():
    f = {"path": "archive/m.sql", "old_path": "models/m.sql", "diff": ""}
    finding = _one([f])
    assert (finding["severity"], finding["title"]) == ("info", pipeline._DBT_CONFIG_DELETED_TITLE)


def test_在dbt目錄裡改名_內容沒變_不算():
    f = {"path": "models/b.sql", "old_path": "models/a.sql", "diff": "", "full_content": FULL_REFRESH}
    assert config_changes([f]) == []


def test_刪掉的SQL註解行_在diff裡以三個減號開頭_照樣比對():
    """刪掉一行 `-- 說明` 在 diff 裡是 `--- 說明`;它是內容,不是檔頭。"""
    old = "-- 舊的寫法\n" + MODEL
    new = MODEL.replace("'incremental'", "'table'")
    diff = _diff(old, new)
    assert "\n--- 舊的寫法\n" in "\n" + diff
    assert _keys("models/mrt_sample.sql", old, new) == [(("materialized",), False, False)]
    assert _keys("models/mrt_sample.sql", old, new, full=False) == [(("materialized",), False, False)]
