"""展開器跟真正執行時分得出來的地方,樣板可以拿來把語句藏在只有真正執行時才會進的分支。

  * invocation_id:dbt 每次執行都不同,展開器也每次不同(固定值可以拿來比對)
  * this / ref() / source() 的型別判斷:dbt 是 Relation 物件,展開器是字串(或給不出值的物件),
    `is string`、`is mapping`… 的結果不一定一致。逐個模擬模擬不完,所以 defined / undefined
    以外的 `is` 判斷一律展開失敗、交人工
  * 給不出值的 this:repr 也失敗(pprint 不會輸出記憶體位址)
"""
import asyncio
import re

import pytest

from orchestrator import pipeline
from orchestrator.config import DBT_DATABASE_PLACEHOLDER
from orchestrator.dbt_render import _Relation, render_model

DB = DBT_DATABASE_PLACEHOLDER
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
SOURCES = {"models/src.yml": "version: 2\nsources:\n  - name: core\n    tables:\n      - name: txn\n"}


def _render(src, path="models/mrt_daily.sql", **kw):
    return render_model(path, source=src, **{"database": DB, **kw})


# ─────────────────── invocation_id ───────────────────

def test_invocation_id_每次展開都不同_格式跟_dbt_一樣():
    a, b = _render("{{ invocation_id }}"), _render("{{ invocation_id }}")
    assert a.ok and b.ok and a.sql != b.sql
    assert UUID4.fullmatch(a.sql) and UUID4.fullmatch(b.sql)


def test_同一次展開裡_invocation_id_不變():
    r = _render("{{ invocation_id }}|{{ invocation_id }}")
    left, right = r.sql.split("|")
    assert left == right


def test_拿固定值比對_invocation_id_藏不住語句():
    src = "select 1 as x\n{% if invocation_id != 'segcra00' %}\n;DELETE FROM important_table\n{% endif %}"
    assert "DELETE FROM important_table" in _render(src).sql


def test_invocation_id_的濾器照常可用():
    """範例專案的 macro 用 `invocation_id | replace('-', '')` 組暫存表名。"""
    r = _render("{{ invocation_id | replace('-', '') }}")
    assert r.ok and re.fullmatch(r"[0-9a-f]{32}", r.sql)


# ─────────────────── 型別判斷 ───────────────────

RELATIONS = {
    "this": "this",
    "ref": "ref('stg_txn')",
    "source": "source('core', 'txn')",
}
TESTS = ["string", "sequence", "iterable", "mapping", "number", "none", "callable", "escaped",
         "lower", "upper", "sameas 'x'", "eq 'x'", "in ['x']", "boolean", "true", "false"]


@pytest.mark.parametrize("test", TESTS)
@pytest.mark.parametrize("rel", list(RELATIONS))
def test_對表名物件做型別判斷就展開失敗(rel, test):
    r = _render(f"{{% if {RELATIONS[rel]} is {test} %}};DELETE FROM t{{% endif %}}select 1")
    assert not r.ok and r.sql == ""
    assert f"is {test.split()[0]}" in r.error and "mrt_daily" not in r.error


@pytest.mark.parametrize("test", ["string", "callable", "escaped", "mapping", "none"])
def test_給不出值的_this_做型別判斷也展開失敗(test):
    r = _render(f"{{% if this is {test} %}}A{{% endif %}}select 1", path="models/mrt-daily.sql")
    assert not r.ok and "mrt-daily" not in r.error


@pytest.mark.parametrize("src", ["{% if 'x' is sameas this %}A{% endif %}",
                                 "{% if 'x' is in [ref('a')] %}A{% endif %}",
                                 "{{ ['a'] | select('eq', this) | list }}"])
def test_表名物件放在判斷的參數裡也擋(src):
    r = _render(src + "select 1")
    assert not r.ok and "判斷" in r.error


@pytest.mark.parametrize("src", [
    "{% if 'x' is in [[ref('a')]] %}A{% endif %}",                 # 多層
    "{% if 'x' is in {'k': this} %}A{% endif %}",                  # dict 的值
    "{% if 'x' is in (1, source('core', 'txn')) %}A{% endif %}",   # tuple
])
def test_容器裡的表名物件也擋(src):
    r = _render(src + "select 1", macro_files=None)
    assert not r.ok and "判斷" in r.error


def test_容器太大看不完時當成有():
    from orchestrator.dbt_render import _RELATION_SCAN_LIMIT, _holds_relation
    assert _holds_relation([list(range(_RELATION_SCAN_LIMIT + 1))])
    assert not _holds_relation([list(range(100)), {"a": (1, 2)}, "string"])


def test_自己包含自己的容器不會卡住():
    from orchestrator.dbt_render import _holds_relation
    loop = []
    loop.append(loop)
    assert _holds_relation([loop]) is True              # 看到上限就停,當成有


def test_in_運算子不是判斷_不擋():
    """`'x' in [ref('a')]` 是運算子,不是 `is` 判斷;結果跟 dbt 一樣是比字串內容。"""
    r = _render("{% if 'x' in [ref('a')] %}A{% endif %}select 1")
    assert r.ok and r.sql == "select 1"


def test_defined_與_undefined_照常():
    r = _render("{% if this is defined and ref('x') is not undefined %}D{% endif %}")
    assert r.ok and r.sql == "D"


@pytest.mark.parametrize("src,out", [
    ("{% if 1 is number %}N{% endif %}", "N"),
    ("{% if 'abc' is string %}S{% endif %}", "S"),
    ("{% if 'upper' is filter %}F{% endif %}", "F"),          # pass_environment 的判斷
    ("{% if 'string' is test %}T{% endif %}", "T"),
    ("{{ [1, 'a'] | select('string') | list }}", "['a']"),
    ("{% if (this ~ '') is string %}S{% endif %}", "S"),     # 串接的結果是一般字串
])
def test_一般值的型別判斷照常(src, out):
    r = _render(src)
    assert r.ok, r.error
    assert r.sql == out


def test_this_經過_string_濾器仍是表名物件_照樣擋():
    """jinja 的 string 濾器遇到已經是字串(子類別)的值會原樣回傳;dbt 會轉成一般字串。
    這裡寧可多擋:實務上很少這樣寫,擋下只是交人工。"""
    r = _render("{% if (this | string) is string %}S{% endif %}")
    assert not r.ok and "is string" in r.error


def test_ref_與_source_回傳表名物件_展開結果不變():
    r = _render("{{ ref('stg_txn') }}|{{ ref('stg_txn').identifier }}|{{ source('core', 'txn') }}|"
                "{{ source('core', 'txn').schema }}|{{ ref('stg_txn') | upper }}")
    assert r.ok, r.error
    assert r.sql == (f'"{DB}"."dbo"."stg_txn"|stg_txn|"{DB}"."dbo"."txn"|dbo|'
                     f'"{DB}"."DBO"."STG_TXN"')


def test_ref_回傳的是表名物件():
    from orchestrator.dbt_render import build_env
    env, _, _, _ = build_env(database=DB)
    assert isinstance(env.globals["ref"]("x"), _Relation)


# ─────────────────── 給不出值的 this ───────────────────

@pytest.mark.parametrize("use", ["{{ this | pprint }}", "{{ this() }}", "{{ this | e }}",
                                 "{{ this | forceescape }}"])
def test_給不出值的_this_任何使用都失敗_不輸出記憶體位址(use):
    r = _render(f"select 1 {use}", path="models/mrt-daily.sql")
    assert not r.ok and r.sql == ""
    assert "object at" not in r.error and "mrt-daily" not in r.error


# ─────────────────── 接進預掃 ───────────────────

@pytest.fixture
def _in_process(monkeypatch):
    monkeypatch.setattr(pipeline, "render_model_isolated", render_model)


class _Hub:
    async def call_json(self, name, args):
        return {"hits": []} if name == "sqltools__run_rules" else []


def test_預掃_型別判斷記成展開失敗_交人工(_in_process):
    files = [{"path": "models/m.sql", "full_content": "{% if this is string %}select 1{% endif %}"}]
    cfg = {"enabled": True, "database": DB}
    (entry,) = asyncio.run(pipeline.prescan(_Hub(), files, cfg, None, None))
    assert "is string" in entry["dbt_render_error"]
    report = pipeline.enforce_dbt_render_failure({"findings": []}, [entry])
    assert [f["severity"] for f in report["findings"]] == ["major"]
