"""ref() / source() 的精確度(orchestrator/dbt_relations.py + dbt_render 的 project 參數)。

PR #10 review 列的三個落差:ref() 不驗證 model 是否存在、source() 不讀 sources.yml、
不套用 alias / 自訂 schema。有專案資訊時:
  * 找得到的照 dbt 的規則展開(以真正的 dbt compile 標準答案驗證逐字一致)
  * 找不到、而且能確定不存在 → 展開失敗(dbt 一樣會失敗)
  * 無法確定(套件、缺 seed 名單、會改表名的設定、樣板值)→ 保留提醒,不冤枉開發者
  * 資料庫名一律是呼叫端給的約定假名,不採用 sources.yml 宣告的資料庫
"""
import importlib.util
import pathlib
import time

import pytest

from orchestrator import dbt_relations
from orchestrator.dbt_relations import project_info_from_files
from orchestrator.dbt_render import (DEFAULT_ADAPTER, DbtProjectInfo, render_model,
                                     render_model_isolated)

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
REFERENCE_PROJECT = PKG_ROOT / "tests" / "dbt_reference"
COMPILED_DIR = PKG_ROOT / "tests" / "fixtures" / "dbt_compiled"
DB = "DBT_PLACEHOLDER"

_spec = importlib.util.spec_from_file_location("dbt_reference_generate_rel",
                                               REFERENCE_PROJECT / "generate.py")
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
REFERENCE_SOURCES = reference.reference_sources()

BASE = {
    "dbt_project.yml": "name: shop\nversion: '1.0'\n",
    "models/stg/stg_txn.sql": "select 1 as id",
    "models/marts/mrt_daily.sql": "select * from {{ ref('stg_txn') }}",
    "models/sources.yml": ("version: 2\nsources:\n"
                           "  - name: raw\n    schema: dbo\n    tables:\n"
                           "      - name: T_TXN\n      - name: acct\n        identifier: T_ACCT\n"
                           "  - name: legacy\n    tables:\n      - name: T_OLD\n"),
}


def _info(files=None, other=(), **kw):
    return project_info_from_files(files if files is not None else BASE,
                                   other_ref_names=other, **{"root_files_checked": True, **kw})


def _render(src, project, **kw):
    return render_model("m.sql", source=src, database=DB, project=project, **kw)


# ------------------------------------------------ 與 dbt compile 逐字一致
@pytest.fixture(scope="module")
def reference_project(tmp_path_factory):
    """產生標準答案時餵給 dbt 的同一個專案,整理成檔案字典。"""
    root = tmp_path_factory.mktemp("dbt_ref")
    reference.build_project(root)
    files = {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
             for p in root.rglob("*") if p.is_file()}
    return root, files


@pytest.mark.parametrize("name", sorted(REFERENCE_SOURCES))
def test_matches_dbt_compile_with_project_info(reference_project, name):
    """有專案資訊時,展開結果仍要與 dbt 官方編譯結果逐字相同;而且這個專案沒有任何
    無法確定的地方(沒用套件、沒有 seed、沒有改表名的設定),所以**不該帶提醒**。"""
    root, files = reference_project
    info = project_info_from_files(files, other_ref_names=(), root_files_checked=True)
    assert info.refs_complete and info.sources_complete and info.global_uncertain is None
    r = render_model(f"{name}.sql", source=REFERENCE_SOURCES[name], code_root=root,
                     macro_dirs=("macros",), variables={"target_date": "2026-02-01"},
                     database="SAMPLE_DW", adapter=DEFAULT_ADAPTER, project=info)
    assert r.ok, r.error
    assert r.sql == (COMPILED_DIR / DEFAULT_ADAPTER / f"{name}.sql").read_text(encoding="utf-8")
    assert r.relation_notice is None


# ------------------------------------------------------------ 解析專案檔案
def test_models_and_sources_are_collected():
    info = _info()
    assert info.project_name == "shop"
    assert info.ref_names == {"stg_txn", "mrt_daily"}
    assert info.sources == {("raw", "T_TXN"): ("dbo", None), ("raw", "acct"): ("dbo", "T_ACCT"),
                            ("legacy", "T_OLD"): (None, None)}
    assert info.refs_complete and info.sources_complete


def test_seed_and_snapshot_names_come_from_caller():
    """打包只收 .sql / .yml,拿不到 seed 的 .csv:名單要由呼叫端提供。
    沒提供(None)時 ref() 找不到就不能判定不存在。"""
    assert not _info(other=None).refs_complete
    info = _info(other={"country_codes"})
    assert "country_codes" in info.ref_names and info.refs_complete


def test_only_model_paths_count():
    files = BASE | {"analyses/a.sql": "x", "macros/m.sql": "{% macro m() %}{% endmacro %}",
                    "models_old/z.sql": "x", "models/x.SQL": "x"}
    assert _info(files).ref_names == {"stg_txn", "mrt_daily", "x"}


def test_custom_model_paths():
    files = {"dbt_project.yml": "name: p\n", "transform/a.sql": "x", "models/b.sql": "x"}
    assert _info(files, model_paths=("transform/",)).ref_names == {"a"}


@pytest.mark.parametrize("pkg_file", ["packages.yml", "dependencies.yml"])
def test_packages_make_lookups_incomplete(pkg_file):
    """套件的 model / source 不在 repo 裡:找不到不代表不存在。"""
    info = _info(BASE | {pkg_file: "packages:\n  - package: dbt-labs/dbt_utils\n"})
    assert not info.refs_complete and not info.sources_complete


@pytest.mark.parametrize("checked", [False, None, "yes", 1])
def test_packages_unknown_unless_root_files_explicitly_checked(checked):
    """files 裡沒有 packages.yml,可能只是呼叫端沒傳進來:沒明確確認就不能斷定沒用套件,
    否則套件裡的 model 會被誤判成不存在、冤枉開發者。只接受真正的 True。"""
    info = project_info_from_files(BASE, other_ref_names=(), root_files_checked=checked)
    assert not info.refs_complete and not info.sources_complete
    r = _render("{{ ref('from_a_package') }}", info)
    assert r.ok and r.relation_notice


def test_missing_dbt_project_yml_is_incomplete():
    files = {k: v for k, v in BASE.items() if k != "dbt_project.yml"}
    info = _info(files)
    assert not info.refs_complete and not info.sources_complete


@pytest.mark.parametrize("bad", [
    "::: [",                                              # 不是 YAML
    "- a\n",                                              # 最上層不是物件
    "sources: raw\n",                                     # sources 不是清單
    "sources:\n  - schema: dbo\n",                        # 來源沒有名稱
    "sources:\n  - name: raw\n    tables: [1]\n",         # 表不是物件
    "sources:\n  - name: raw\n    schema: [x]\n",         # schema 不是字串
    "sources:\n  - name: '{{ var(\"s\") }}'\n    tables: [{name: t}]\n",   # 名稱是樣板
    "models:\n  - config: {alias: x}\n",                  # model 沒有名稱
], ids=["not-yaml", "top-list", "sources-str", "no-name", "table-int", "schema-list",
        "templated-name", "model-no-name"])
def test_unreadable_properties_make_lookups_incomplete(bad):
    """看不懂的屬性檔可能宣告了我們看不到的 source / model:往「無法確定」靠。"""
    info = _info(BASE | {"models/bad.yml": bad})
    assert not info.refs_complete and not info.sources_complete


def test_non_text_content_never_raises():
    info = _info(BASE | {"models/b.yml": b"\xff", 5: "x"})
    assert not info.sources_complete


@pytest.mark.parametrize("kwargs", [
    {"files": None}, {"files": []}, {"files": "x"},
    {"files": BASE, "model_paths": (1,)},
    {"files": BASE, "model_paths": "models"},              # 字串會被拆成字元
    {"files": BASE, "other_ref_names": "seed_a"},          # 字串會被拆成字元
    {"files": BASE, "other_ref_names": [1]},
], ids=["none", "list", "str", "bad-model-path", "model-paths-str", "names-str", "names-int"])
def test_wrong_input_shapes_give_fully_uncertain_default(kwargs):
    info = project_info_from_files(**{"other_ref_names": (), **kwargs})
    assert info == DbtProjectInfo()


@pytest.mark.parametrize("yml", [
    "models:\n  shop:\n    marts:\n      +schema: marts\n",
    "models:\n  shop:\n    +alias: x\n",
    "models:\n  +database: other\n",
    "seeds:\n  shop:\n    +schema: ref_data\n",
    "snapshots:\n  shop:\n    schema: snaps\n",
], ids=["schema", "alias", "database", "seeds", "snapshots"])
def test_dbt_project_name_settings_make_every_ref_uncertain(yml):
    info = _info(BASE | {"dbt_project.yml": "name: shop\n" + yml})
    assert info.global_uncertain


def test_folder_named_schema_is_not_a_setting():
    """目錄名稱剛好叫 schema(值是物件)不是設定。"""
    info = _info(BASE | {"dbt_project.yml": "name: shop\nmodels:\n  shop:\n    schema:\n      +materialized: view\n"})
    assert info.global_uncertain is None


@pytest.mark.parametrize("macro", [
    "{% macro generate_schema_name(custom, node) %}x{% endmacro %}",
    "{%- macro generate_alias_name(custom, node) -%}x{%- endmacro %}",
    "{% macro  generate_database_name (c, n) %}x{% endmacro %}",
])
def test_custom_generate_name_macros_make_every_ref_uncertain(macro):
    assert _info(BASE | {"macros/naming.sql": macro}).global_uncertain


@pytest.mark.parametrize("files, model", [
    ({"models/p.yml": "models:\n  - name: stg_txn\n    config:\n      alias: txn\n"}, "stg_txn"),
    ({"models/p.yml": "models:\n  - name: stg_txn\n    config: {schema: stg}\n"}, "stg_txn"),
    ({"models/p.yml": "models:\n  - name: stg_txn\n    versions: [{v: 1}]\n"}, "stg_txn"),
    ({"models/stg/stg_txn.sql": "{{ config(alias='txn') }} select 1"}, "stg_txn"),
    ({"models/stg/stg_txn.sql": "{{ config(\n  materialized='view',\n  schema='x') }}"}, "stg_txn"),
], ids=["yml-alias", "yml-schema", "versions", "infile-alias", "infile-schema-multiline"])
def test_per_model_name_settings(files, model):
    assert model in _info(BASE | files).uncertain_models


def test_config_without_name_settings_is_certain():
    info = _info(BASE | {"models/stg/stg_txn.sql": "{{ config(materialized='view') }} select 1"})
    assert "stg_txn" not in info.uncertain_models


def test_yaml_alias_bomb_is_linear():
    """YAML 別名可以讓同一段內容被引用極多次;走訪要記住走過的節點,不能指數爆炸。"""
    lines = ["name: shop", "models:", "  l0: &l0 {a: {+materialized: view}}"]
    for i in range(1, 30):
        lines.append(f"  l{i}: &l{i} [*l{i-1}, *l{i-1}, *l{i-1}, *l{i-1}]")
    start = time.monotonic()
    info = _info(BASE | {"dbt_project.yml": "\n".join(lines) + "\n"})
    assert time.monotonic() - start < 5
    assert info.global_uncertain is None


def test_properties_alias_bomb_is_bounded():
    """屬性檔也能用別名把「來源 × 表」放大成平方級(修正前 72 KB 就要 9 秒);
    超過處理量上限就判成無法確定,不能卡住審查。"""
    n = 6000
    lines = ["defs:", "  - &s", "    name: raw", "    tables:", "      - &t {name: t}"]
    lines += ["      - *t"] * (n - 1) + ["sources:"] + ["  - *s"] * n
    start = time.monotonic()
    info = _info(BASE | {"models/bomb.yml": "\n".join(lines) + "\n"})
    assert time.monotonic() - start < 5
    assert not info.refs_complete and not info.sources_complete


@pytest.mark.parametrize("limit, complete", [(5, True), (4, False)])
def test_property_entry_limit_is_exact(monkeypatch, limit, complete):
    """BASE 的 sources.yml 剛好 5 筆(2 個來源 + 3 張表):上限內照常,超過一筆就無法確定。"""
    monkeypatch.setattr(dbt_relations, "MAX_PROPERTY_ENTRIES", limit)
    info = _info()
    assert info.sources_complete is complete and info.refs_complete is complete


def test_property_entry_limit_is_shared_across_files(monkeypatch):
    """上限是所有屬性檔合計,不是每個檔各自計算——否則拆成很多檔就能繞過。"""
    monkeypatch.setattr(dbt_relations, "MAX_PROPERTY_ENTRIES", 6)
    info = _info(BASE | {"models/more.yml": "models:\n  - name: a\n  - name: b\n"})
    assert not info.sources_complete


def test_property_entry_limit_counts_models(monkeypatch):
    monkeypatch.setattr(dbt_relations, "MAX_PROPERTY_ENTRIES", 6)
    assert _info(BASE | {"models/more.yml": "models:\n  - name: a\n"}).sources_complete
    assert not _info(BASE | {"models/more.yml": "models:\n  - name: a\n  - name: b\n"}).sources_complete


class _NoIterDict(dict):
    def __iter__(self):
        raise AssertionError("不可走訪整個 config")


def test_name_key_check_does_not_walk_the_whole_config():
    """被別名共用的大 config 會被每個 model 各查一次:只能查固定的幾個鍵,不能走訪整個物件,
    否則工作量是 model 數 × config 大小。"""
    assert dbt_relations._has_name_key(_NoIterDict({"alias": "x"}))
    assert not dbt_relations._has_name_key(_NoIterDict({"materialized": "view"}))


def test_shared_config_with_alias_marks_every_model():
    yml = ("defs:\n  - &c {alias: x}\n"
           "models:\n  - {name: stg_txn, config: *c}\n  - {name: mrt_daily, config: *c}\n")
    assert _info(BASE | {"models/p.yml": yml}).uncertain_models == {"stg_txn", "mrt_daily"}


# ------------------------------------------------------------ 展開行為
def test_known_ref_is_exact_without_notice():
    r = _render("select * from {{ ref('stg_txn') }}", _info())
    assert r.ok, r.error
    assert r.sql == f'select * from "{DB}"."dbo"."stg_txn"'
    assert r.relation_notice is None


def test_ref_with_own_project_name():
    r = _render("{{ ref('shop', 'stg_txn') }}", _info())
    assert r.ok and r.relation_notice is None


def test_missing_ref_fails_when_certain():
    """dbt compile 會失敗的 MR:合進去會讓排程壞掉,要在審查時就擋下。"""
    r = _render("select * from {{ ref('stg_tx') }}", _info())
    assert not r.ok and "stg_tx" in r.error and "不存在" in r.error


@pytest.mark.parametrize("info", [
    _info(other=None),                                                     # 沒有 seed 名單
    _info(BASE | {"packages.yml": "packages: []\n"}),                      # 用了套件
    _info(BASE | {"models/bad.yml": "::: ["}),                             # 屬性檔看不懂
], ids=["no-seed-list", "packages", "unreadable"])
def test_missing_ref_is_only_uncertain_when_we_can_not_be_sure(info):
    r = _render("select * from {{ ref('maybe_a_seed') }}", info)
    assert r.ok, r.error
    assert r.relation_notice and "無法確定" in r.relation_notice


def test_other_package_ref():
    assert not _render("{{ ref('dbt_utils', 'x') }}", _info()).ok
    r = _render("{{ ref('dbt_utils', 'x') }}", _info(BASE | {"packages.yml": "packages: []\n"}))
    assert r.ok and r.relation_notice


def test_other_package_ref_with_a_name_we_also_have():
    """指定了別的套件,即使本專案剛好也有同名 model,也不是同一個東西:
    不可當成本專案的 model 而「確定」展開。"""
    r = _render("{{ ref('dbt_utils', 'stg_txn') }}", _info())
    assert not r.ok and "dbt_utils" in r.error
    r = _render("{{ ref('dbt_utils', 'stg_txn') }}",
                _info(BASE | {"packages.yml": "packages: []\n"}))
    assert r.ok and r.relation_notice


def test_declared_source_uses_sources_yml():
    r = _render("{{ source('raw', 'T_TXN') }} {{ source('raw', 'acct') }} "
                "{{ source('legacy', 'T_OLD') }}", _info())
    assert r.ok, r.error
    # schema 預設是來源名(dbt 的規則)、identifier 有宣告就用宣告的
    assert r.sql == (f'"{DB}"."dbo"."T_TXN" "{DB}"."dbo"."T_ACCT" "{DB}"."legacy"."T_OLD"')
    assert r.relation_notice is None


def test_source_database_in_yml_is_never_used():
    """資料庫名一律是約定的假名:sources.yml 宣告的正式資料庫名不可出現在展開結果裡。"""
    files = BASE | {"models/sources.yml": ("sources:\n  - name: raw\n    database: REAL_PROD_DB\n"
                                           "    schema: dbo\n    tables: [{name: T_TXN}]\n")}
    r = _render("{{ source('raw', 'T_TXN') }}", _info(files))
    assert r.ok and "REAL_PROD_DB" not in r.sql and r.sql == f'"{DB}"."dbo"."T_TXN"'


def test_undeclared_source_fails_when_certain():
    r = _render("{{ source('raw', 'T_NOPE') }}", _info())
    assert not r.ok and "raw.T_NOPE" in r.error


def test_undeclared_source_is_uncertain_with_packages():
    r = _render("{{ source('raw', 'T_NOPE') }}", _info(BASE | {"packages.yml": "packages: []\n"}))
    assert r.ok and r.relation_notice


def test_templated_source_values_are_uncertain_and_fall_back_to_defaults():
    files = BASE | {"models/sources.yml": ("sources:\n  - name: raw\n    schema: \"{{ var('s') }}\"\n"
                                           "    tables: [{name: T_TXN}]\n")}
    r = _render("{{ source('raw', 'T_TXN') }}", _info(files))
    assert r.ok and r.relation_notice
    assert r.sql == f'"{DB}"."raw"."T_TXN"'


_SCHEMA_YML = "sources:\n  - name: raw\n    schema: '{v}'\n    tables:\n      - name: T_TXN\n"
_IDENT_YML = "sources:\n  - name: raw\n    tables:\n      - name: T_TXN\n        identifier: '{v}'\n"


@pytest.mark.parametrize("template", [_SCHEMA_YML, _IDENT_YML], ids=["schema", "identifier"])
@pytest.mark.parametrize("value", ['dbo"; DROP TABLE x; --', "dbo.other", "1dbo", "a b"])
def test_malicious_schema_or_identifier_is_never_spliced(template, value):
    """sources.yml 來自待審 commit:宣告的名稱會被拼進 SQL 識別字,一律過白名單。"""
    yml = template.format(v=value)
    r = _render("{{ source('raw', 'T_TXN') }}", _info(BASE | {"models/sources.yml": yml}))
    assert not r.ok, r.sql
    assert "DROP" not in (r.sql or "")


def test_uncertain_model_ref_keeps_notice():
    info = _info(BASE | {"models/p.yml": "models:\n  - name: stg_txn\n    config: {alias: t}\n"})
    r = _render("{{ ref('stg_txn') }}", info)
    assert r.ok and r.relation_notice


def test_global_uncertainty_keeps_notice():
    info = _info(BASE | {"macros/n.sql": "{% macro generate_schema_name(c, n) %}x{% endmacro %}"})
    r = _render("{{ ref('stg_txn') }}", info)
    assert r.ok and r.relation_notice


def test_no_relations_no_notice():
    r = _render("select 1", _info(other=None))
    assert r.ok and r.relation_notice is None


def test_without_project_behaviour_is_unchanged():
    """沒給專案資訊:不驗證、source 固定 dbo、用到就提醒(與接線前逐字相同)。"""
    r = render_model("m.sql", source="{{ ref('ghost') }} {{ source('raw', 'T') }}", database=DB)
    assert r.ok and r.sql == f'"{DB}"."dbo"."ghost" "{DB}"."dbo"."T"'
    assert r.relation_notice and "sources.yml" in r.relation_notice


def test_macro_file_top_level_ref_does_not_fail(tmp_path):
    """dbt 不執行 macro 檔最外層的程式碼;那裡的 ref() 指到不存在的 model 不可讓展開失敗。"""
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "m.sql").write_text(
        "{% set t = ref('ghost') %}{% macro m() %}1{% endmacro %}", encoding="utf-8")
    r = render_model("m.sql", source="select {{ m() }}", code_root=tmp_path,
                     database=DB, project=_info())
    assert r.ok, r.error
    assert r.relation_notice is None


def test_macro_called_ref_to_missing_model_still_fails(tmp_path):
    """被 model 呼叫到的 macro 裡的 ref() 會在 dbt compile 時解析:不存在就失敗。"""
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "m.sql").write_text(
        "{% macro m() %}{{ ref('ghost') }}{% endmacro %}", encoding="utf-8")
    r = render_model("m.sql", source="select * from {{ m() }}", code_root=tmp_path,
                     database=DB, project=_info())
    assert not r.ok and "ghost" in r.error


def test_project_must_be_the_right_type():
    r = render_model("m.sql", source="select 1", database=DB, project={"ref_names": {"x"}})
    assert not r.ok and "DbtProjectInfo" in r.error


def test_isolated_render_accepts_project_info():
    """待審內容走子行程:DbtProjectInfo 必須能跨行程傳遞。"""
    r = render_model_isolated("m.sql", source="select * from {{ ref('stg_txn') }}",
                              database=DB, project=_info())
    assert r.ok, r.error
    assert r.relation_notice is None
    missing = render_model_isolated("m.sql", source="{{ ref('nope') }}", database=DB,
                                    project=_info())
    assert not missing.ok


def test_default_project_info_is_fully_uncertain():
    """DbtProjectInfo() 的預設值不能讓任何引用被判成「確定不存在」。"""
    info = DbtProjectInfo()
    assert not info.refs_complete and not info.sources_complete
    r = _render("{{ ref('x') }} {{ source('a', 'b') }}", info)
    assert r.ok and r.relation_notice
