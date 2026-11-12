"""執行驗證用的展開:ref() 由規格裡同名的測資表代替上游 model——不呼叫 LLM、不連資料庫。

上游 model 要在沙盒裡有東西可以讀。這裡用規格「資料表定義」裡跟被引用 model 同名的表(alias 算過
之後)代替它的輸出,不動執行驗證(spec_exec)。上游 model 自己的邏輯不在這次執行,所以代替了
哪些 model 要記下來(報告會寫出來)。不猜表名:表名無法確定、規格沒有那張表、對到兩次,一律展開
失敗,執行驗證照舊回「尚未支援」交人工。
"""
from __future__ import annotations

import pytest

from orchestrator.config import DBT_DATABASE_PLACEHOLDER as DB
from orchestrator.dbt_render import DbtProjectInfo, render_model

TABLES = {"transactions": "[transactions]", "stg_a": "[stg_a]", "stg_alias": "[stage].[stg_alias]",
          "dup": None}


def _info(**kw) -> DbtProjectInfo:
    base = dict(project_name="sample",
                ref_names=frozenset({"mrt_BIG", "stg_a", "stg_b", "stg_dup", "code_seed",
                                     "stg_other"}),
                sources={("raw", "transactions"): (None, None), ("raw", "stg_a"): (None, None)},
                refs_complete=True, sources_complete=True,
                naming={"mrt_BIG": {"alias": None, "schema": None, "database": None},
                        "stg_a": {"alias": None, "schema": None, "database": None},
                        "stg_b": {"alias": "stg_alias", "schema": "custom", "database": None},
                        "stg_dup": {"alias": "dup", "schema": None, "database": None},
                        "stg_other": {"alias": "not in spec", "schema": None, "database": None}},
                uncertain_models=frozenset({"code_seed"}))
    base.update(kw)
    return DbtProjectInfo(**base)


def _render(src, info=None, tables=TABLES):
    return render_model("models/mrt_BIG.sql", source=src, database=DB,
                        project=info if info is not None else _info(), sandbox_tables=tables)


# ─────────────────── 對得到的 ───────────────────

def test_ref_由同名的測資表代替():
    r = _render("SELECT a FROM {{ ref('stg_a') }}")
    assert r.ok, r.error
    assert r.sql == "SELECT a FROM [stg_a]"
    assert r.stand_ins == ["stg_a"]


def test_用_alias_算過之後的表名_不看自訂_schema():
    """沙盒只有規格建的表:綱要用不到,自訂 schema 要不要串接不知道也沒關係。"""
    r = _render("SELECT a FROM {{ ref('stg_b') }}", _info(schema_concat=None))
    assert r.ok, r.error
    assert r.sql == "SELECT a FROM [stage].[stg_alias]" and r.stand_ins == ["stg_b"]


def test_指定本專案的_ref_也算():
    r = _render("SELECT a FROM {{ ref('sample', 'stg_a') }}")
    assert r.ok and r.sql == "SELECT a FROM [stg_a]"


def test_同一個_model_引用兩次只記一次():
    r = _render("SELECT a FROM {{ ref('stg_a') }} x JOIN {{ ref('stg_a') }} y ON x.a = y.a")
    assert r.ok and r.stand_ins == ["stg_a"]


def test_跟_source_一起用_依出現順序記():
    r = _render("SELECT a FROM {{ source('raw', 'transactions') }} t "
                "JOIN {{ ref('stg_b') }} b ON 1 = 1 JOIN {{ ref('stg_a') }} a ON 1 = 1")
    assert r.ok, r.error
    assert r.stand_ins == ["stg_b", "stg_a"]


def test_表名物件只給名稱():
    r = _render("{% set s = ref('stg_b') %}{{ s.identifier }} {{ s.name }} {{ s.table }}")
    assert r.ok and r.sql == "stg_alias stg_alias stg_alias"


def test_沒用到_ref_時沒有代替():
    r = _render("SELECT a FROM {{ source('raw', 'transactions') }}")
    assert r.ok and r.stand_ins == []


def test_一般展開不受影響():
    """沒有 sandbox_tables 時(規則層用的展開)照 dbt 的表名,不記代替。"""
    r = render_model("models/mrt_BIG.sql", source="SELECT a FROM {{ ref('stg_a') }}",
                     database=DB, project=_info())
    assert r.ok and r.sql == f'SELECT a FROM "{DB}"."dbo"."stg_a"' and r.stand_ins == []


# ─────────────────── 不猜表名 ───────────────────

@pytest.mark.parametrize("src, why", [
    ("{{ ref('code_seed') }}", "表名無法確定"),                  # seed / snapshot
    ("{{ ref('stg_other') }}", "表名無法確定"),                  # alias 不是英數與底線
    ("{{ ref('nope') }}", "不存在"),
    ("{{ ref('pkg', 'stg_a') }}", "其他套件"),
    ("{{ ref('stg_a', version=2) }}", "關鍵字參數"),
    ("{{ ref() }}", "參數個數"),
    ("{{ ref('a', 'b', 'c') }}", "參數個數"),
    ("{{ ref('stg_dup') }}", "不只一張"),
    ("{{ ref('mrt_BIG') }}", "沒有 ref() 指向的表"),            # 規格沒有那張表
    ("{{ source('raw', 'stg_a') }} {{ ref('stg_a') }}", "同一張表"),
    ("{{ ref('stg_a').database }}", "沒有 database"),
    ("{{ ref('stg_a').schema }}", "沒有 schema"),
    ("{{ ref('bad name') }}", "表"),
])
def test_展開失敗的情況(src, why):
    r = _render(src)
    assert not r.ok and why in r.error, (src, r.error)


def test_專案層級無法確定時不對():
    r = _render("{{ ref('stg_a') }}", _info(global_uncertain="自訂 generate_alias_name"))
    assert not r.ok and "表名無法確定" in r.error


def test_沒有專案資訊時不對():
    r = render_model("models/mrt_BIG.sql", source="{{ ref('stg_a') }}", database=DB,
                     sandbox_tables=TABLES)
    assert not r.ok and "沒有 dbt 專案資訊" in r.error


def test_失敗時不留代替的紀錄():
    r = _render("{{ ref('stg_a') }} {{ ref('nope') }}")
    assert not r.ok and r.stand_ins == []


def test_this_照舊不支援():
    r = _render("{{ this }}")
    assert not r.ok and "{{ this }}" in r.error


def test_macro_檔載入時不對():
    """macro 檔最外層 dbt 不執行:在那裡呼叫 ref() 不算用到,也不記代替。"""
    files = {"macros/m.sql": "{% set x = ref('stg_b') %}{% macro m() %}1{% endmacro %}"}
    r = render_model("models/mrt_BIG.sql", source="SELECT a FROM {{ ref('stg_a') }}", database=DB,
                     project=_info(), sandbox_tables=TABLES, macro_files=files)
    assert r.ok, r.error
    assert r.stand_ins == ["stg_a"]
