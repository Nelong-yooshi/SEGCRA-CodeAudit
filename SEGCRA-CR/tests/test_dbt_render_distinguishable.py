"""展開器跟真正執行時分得出來的地方,樣板可以拿來把語句藏在只有真正執行時才會進的分支。

  * invocation_id:dbt 每次執行都不同,展開器也每份審查不同(固定值可以拿來比對);同一份審查裡的
    展開共用一個值(兩種模式的同一句才不會被算成兩句)
  * this / ref() / source() 取屬性:只放行照實模擬的 database、schema、identifier、name、table,
    其他屬性與索引(dbt 的 Relation 有沒有、是什麼都跟字串不同)一律展開失敗、交人工
  * this / ref() / source() 的型別判斷:dbt 是 Relation 物件,展開器是字串(或給不出值的物件),
    `is string`、`is mapping`… 的結果不一定一致。逐個模擬模擬不完,所以 defined / undefined
    以外的 `is` 判斷一律展開失敗、交人工
  * 給不出值的 this:repr 也失敗(pprint 不會輸出記憶體位址)
"""
import asyncio
import re

import pytest

from orchestrator import dbt_render, pipeline
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


@pytest.fixture
def _isolated_in_process(monkeypatch):
    """render_model_isolated 的子行程換成同行程:要測的是它帶上這份審查的值,不是子行程本身。"""
    monkeypatch.setattr(dbt_render, "run_isolated",
                        lambda func, args, kwargs, **kw: func(*args, **kwargs))


def _ids_in_one_review(start: bool):
    async def review():
        if start:
            dbt_render.start_review_invocation()
        return [dbt_render.render_model_isolated("models/m.sql", source="{{ invocation_id }}",
                                                 database=DB).sql for _ in range(3)]
    return asyncio.run(review())


def test_一份審查裡的展開共用一個_invocation_id(_isolated_in_process):
    ids = _ids_in_one_review(True)
    assert len(set(ids)) == 1 and UUID4.fullmatch(ids[0])
    # 下一份審查是另一個值
    assert _ids_in_one_review(True)[0] != ids[0]


def test_沒有開始一份審查時每次展開都不同(_isolated_in_process):
    assert len(set(_ids_in_one_review(False))) == 3


def test_同時跑的審查各自一個值(_isolated_in_process):
    """每份審查各在自己的執行緒(webhook 的寫法);一條設了不會影響另一條。"""
    import threading
    seen = {}

    def work(name, start):
        seen[name] = _ids_in_one_review(start)
    threads = [threading.Thread(target=work, args=("a", True)),
               threading.Thread(target=work, args=("b", False))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(seen["a"])) == 1 and len(set(seen["b"])) == 3


def test_呼叫端指定的_invocation_id_優先(_isolated_in_process):
    given = "12345678-1234-4123-8123-123456789abc"

    async def review():
        dbt_render.start_review_invocation()
        return dbt_render.render_model_isolated("models/m.sql", source="{{ invocation_id }}",
                                                database=DB, invocation_id=given).sql
    assert asyncio.run(review()) == given


@pytest.mark.parametrize("bad", ["segcra00", "12345678-1234-1123-8123-123456789abc",
                                 "x' }}{{ 1/0 }}", 5, ""])
def test_指定的_invocation_id_格式不對就展開失敗(bad):
    """這個值會原樣出現在展開結果裡,只收 uuid4 的寫法;錯誤訊息不回顯它。"""
    r = _render("{{ invocation_id }}", invocation_id=bad)
    assert not r.ok and "invocation_id 的格式不對" in r.error
    if str(bad):
        assert str(bad) not in r.error


INCREMENTAL_AUDIT = ("{{ config(materialized='incremental') }}\n"
                     "UPDATE audit_log SET run_id = '{{ invocation_id }}';\n"
                     "select 1 as x\n{% if is_incremental() %} where 1 = 1 {% endif %}")


def test_語句裡有_invocation_id_時_兩種模式的同一句只算一次(_isolated_in_process):
    """預掃以一般、增量各展開一次,用(規則、語句原文)合併命中;兩次的值不同就會變成兩句。"""
    import json

    from toolbox import sqltools

    class Hub:
        async def call_json(self, name, args):
            if name == "sqltools__run_rules":
                return json.loads(sqltools.run_rules(args["sql"]))
            return []

    async def review():
        dbt_render.start_review_invocation()
        return await pipeline.prescan(Hub(), [{"path": "models/m.sql",
                                                "full_content": INCREMENTAL_AUDIT}],
                                      {"enabled": True, "database": DB}, None, None)
    (entry,) = asyncio.run(review())
    assert [(h["rule"], h.get("branch")) for h in entry["rules"] if h["rule"] == "R001"] == \
        [("R001", None)]


def test_review_mr_在處理_MR_之前就設好這份審查的_invocation_id(monkeypatch):
    """後面的展開(預掃、受影響 model)才會共用同一個值。"""
    from orchestrator.config import load_config
    calls = []
    monkeypatch.setattr(dbt_render, "start_review_invocation", lambda: calls.append(1))

    async def stop(*a, **kw):
        raise RuntimeError("停在這裡")
    monkeypatch.setattr(pipeline.ToolHub, "__aenter__", stop)
    with pytest.raises(RuntimeError, match="停在這裡"):
        asyncio.run(pipeline.review_mr(load_config(), "1"))
    assert calls == [1]


# ─────────────────── 取屬性與索引 ───────────────────

@pytest.mark.parametrize("use", [
    "{% if this.startswith is not defined %}A{% endif %}",     # dbt 的 Relation 沒有字串方法
    "{% if this.keys is defined %}A{% endif %}",               # dbt 的 Relation 是 mapping
    "{% if ref('stg_txn').split is not defined %}A{% endif %}",
    "{% if source('core', 'txn').upper is defined %}A{% endif %}",
    "{{ this['x'] }}", "{{ this[0] }}", "{{ this.__class__ }}",
    "{% if this | attr('keys') is defined %}A{% endif %}",     # attr 濾器不經過 getattr
    "{{ [this] | map(attribute='keys') | list }}",
    "{{ [this] | sort(attribute='keys') | list }}",
    "{{ [this] | selectattr('keys') | list }}",
])
def test_對表名物件取沒有照實模擬的屬性或索引就展開失敗(use):
    r = _render(use)
    assert not r.ok and "沒有照實模擬的屬性" in r.error


@pytest.mark.parametrize("use,out", [
    ("{{ this.database }}.{{ this.schema }}.{{ this.identifier }}", f"{DB}.dbo.mrt_daily"),
    ("{{ this.name }}|{{ this.table }}", "mrt_daily|mrt_daily"),
    ("{{ this | attr('name') }}", "mrt_daily"),
    ("{{ [this] | map(attribute='identifier') | join }}", "mrt_daily"),
    ("{{ ref('stg_txn').identifier }}", "stg_txn"),
    ("{{ (this | string).startswith('\"') }}", "True"),     # 轉成一般字串後就是字串
    ("{{ this['identifier'] }}", "mrt_daily"),               # 取索引會退回同名屬性
    ("{% set d = {'a': 1} %}{{ d.keys() | list }}", "['a']"),  # 一般物件照常
])
def test_照實模擬的屬性與一般物件照常(use, out):
    r = _render(use)
    assert r.ok, r.error
    assert r.sql == out


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


@pytest.mark.parametrize("use,out", [
    ("{% if (this | string) is string %}S{% endif %}", "S"),
    ("{{ (this | string).replace('\"', '') }}", f"{DB}.dbo.mrt_daily"),
    ("{{ this | string }}", f'"{DB}"."dbo"."mrt_daily"'),
])
def test_this_經過_string_濾器是一般字串_跟_dbt_一樣(use, out):
    """dbt 的 Relation 經過 string 就是一般字串;判斷與字串方法的結果兩邊相同,不需要擋。
    (Jinja 內建的濾器遇到字串子類別會原樣回傳,轉完仍是表名物件。)"""
    r = _render(use)
    assert r.ok, r.error
    assert r.sql == out


def test_給不出值的_this_經過_string_濾器照樣失敗():
    r = _render("{{ this | string }}", incremental=False, path="models/mrt-daily.sql")
    assert not r.ok and "object at" not in r.error


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
