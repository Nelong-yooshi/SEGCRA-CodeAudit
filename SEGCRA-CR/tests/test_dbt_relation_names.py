"""表名照 dbt 的規則算:ref() / {{ this }} 套用 alias 與自訂 schema(dbt_relations + dbt_render)。

預期值照 dbt 的原始碼推出(沒有在 CI 跑 dbt):
  * generate_alias_name(dbt-adapters `get_custom_alias.sql`):alias 有值(非空字串)用它
    去掉頭尾空白,否則用 model 名稱
  * generate_schema_name(dbt-adapters `get_custom_schema.sql`):沒有自訂 schema 用
    target.schema;有的話預設是 `<target.schema>_<自訂去掉頭尾空白>`
  * dbt-sqlserver 的 sqlserver__generate_schema_name(`macros/adapters/schema.sql`):旗標
    dbt_sqlserver_use_default_schema_concat 或同名 var 任一個是 true 才串接,否則自訂值原樣用;
    旗標沒寫時的預設值依版本不同(1.9.2 是 false、1.12 的預覽版是 true),所以由部署端設定補
  * 合併順序(dbt 文件 "Define configs"):dbt_project.yml(資料夾越深越優先)→ 屬性檔的
    config: → model 檔的 config()
有 dbt 的環境可以用 tests/dbt_reference/relation_names.py 拿 dbt parse 的 manifest 對照。

資料庫名一律是約定的假名(展開結果會出現在審查結果裡,不放正式資料庫名)。
"""
import importlib.util
import json
import pathlib

import pytest

from orchestrator import dbt_context
from orchestrator.dbt_relations import project_info_from_files
from orchestrator.dbt_render import DEFAULT_SCHEMA, model_identity, render_model, render_model_isolated

DB = "DBT_PLACEHOLDER"
PROJECT = "name: shop\nversion: '1.0'\n"


def _info(files: dict, project_yml: str = PROJECT):
    return project_info_from_files({"dbt_project.yml": project_yml, **files},
                                   other_ref_names=(), root_files_checked=True)


def _ref(info, name="stg_txn"):
    r = render_model("m.sql", source=f"select * from {{{{ ref('{name}') }}}}", database=DB,
                     project=info)
    assert r.ok, r.error
    return r


def _rel(schema, table):
    return f'select * from "{DB}"."{schema}"."{table}"'


MODEL = {"models/stg/stg_txn.sql": "select 1 as id"}


# ─────────────────── alias(generate_alias_name) ───────────────────

@pytest.mark.parametrize("config, expected", [
    ("", "stg_txn"),                                       # 沒設:model 名稱
    ("{{ config(alias='txn') }}", "txn"),
    ("{{ config(alias=\"txn\") }}", "txn"),
    ("{{ config(alias='  txn  ') }}", "txn"),              # | trim
    ("{{ config(alias='') }}", "stg_txn"),                 # 空字串不算有值
    ("{{ config(alias=None) }}", "stg_txn"),
    ("{{ config(materialized='view', alias='txn') }}", "txn"),
    ("{{ config(alias='a') }}{{ config(alias='txn') }}", "txn"),   # 後面的 config() 蓋過前面的
])
def test_檔內_config_的_alias(config, expected):
    info = _info({"models/stg/stg_txn.sql": f"{config} select 1"})
    assert "stg_txn" not in info.uncertain_models
    r = _ref(info)
    assert r.sql == _rel(DEFAULT_SCHEMA, expected) and r.relation_notice is None


def test_屬性檔_config_的_alias():
    info = _info(MODEL | {"models/p.yml": "models:\n  - name: stg_txn\n    config: {alias: txn}\n"})
    assert _ref(info).sql == _rel(DEFAULT_SCHEMA, "txn")


# ─────────────────── 合併順序 ───────────────────

PROJECT_ALIAS = PROJECT + "models:\n  shop:\n    +alias: from_project\n"


@pytest.mark.parametrize("files, project_yml, expected", [
    (MODEL, PROJECT_ALIAS, "from_project"),
    (MODEL | {"models/p.yml": "models:\n  - name: stg_txn\n    config: {alias: from_yml}\n"},
     PROJECT_ALIAS, "from_yml"),                                    # 屬性檔蓋過 dbt_project.yml
    ({"models/stg/stg_txn.sql": "{{ config(alias='from_sql') }} select 1",
      "models/p.yml": "models:\n  - name: stg_txn\n    config: {alias: from_yml}\n"},
     PROJECT_ALIAS, "from_sql"),                                    # model 檔蓋過屬性檔
])
def test_合併順序_model_檔_蓋過_屬性檔_蓋過_dbt_project(files, project_yml, expected):
    assert _ref(_info(files, project_yml)).sql == _rel(DEFAULT_SCHEMA, expected)


@pytest.mark.parametrize("project_yml, expected", [
    (PROJECT + "models:\n  +alias: top\n", "top"),                              # 整個 models 區塊
    (PROJECT + "models:\n  shop:\n    +alias: proj\n    stg:\n      +alias: folder\n", "folder"),
    (PROJECT + "models:\n  shop:\n    stg:\n      +alias: folder\n      stg_txn:\n        +alias: one\n",
     "one"),                                                                    # 指名 model
    (PROJECT + "models:\n  shop:\n    marts:\n      +alias: other\n", "stg_txn"),  # 別的資料夾
    (PROJECT + "models:\n  other_pkg:\n    +alias: x\n", "stg_txn"),               # 別的專案
    (PROJECT + "models:\n  shop:\n    alias: plain\n", "plain"),                  # 不加 + 也算
    (PROJECT + "models:\n  shop:\n    alias: same\n    +alias: same\n", "same"),
])
def test_dbt_project_的資料夾層級_越深越優先(project_yml, expected):
    info = _info(MODEL, project_yml)
    assert info.global_uncertain is None
    assert _ref(info).sql == _rel(DEFAULT_SCHEMA, expected)


@pytest.mark.parametrize("files, project_yml", [
    ({"models/stg/stg_txn.sql": "{{ config(schema=None) }} select 1"},
     PROJECT + "models:\n  shop:\n    +schema: marts\n"),                      # model 檔蓋 dbt_project
    (MODEL | {"models/p.yml": "models:\n  - name: stg_txn\n    config: {alias: null}\n"},
     PROJECT_ALIAS),                                                           # 屬性檔蓋 dbt_project
    ({"models/stg/stg_txn.sql": "{{ config(alias=None) }} select 1",
      "models/p.yml": "models:\n  - name: stg_txn\n    config: {alias: from_yml}\n"},
     PROJECT),                                                                 # model 檔蓋屬性檔
    (MODEL, PROJECT + "models:\n  shop:\n    +alias: proj\n    stg:\n      +alias: null\n"),
])
def test_明寫_None_想蓋掉上層的值時無法確定(files, project_yml):
    """dbt 碰到 None 是清掉還是保留上層的值沒有逐一查證:不猜,記成無法確定。"""
    info = _info(files, project_yml + "flags:\n  dbt_sqlserver_use_default_schema_concat: false\n")
    assert "stg_txn" in info.uncertain_models
    assert _ref(info).relation_notice


def test_名稱叫_schema_的子目錄不是設定():
    info = _info({"models/schema/stg_txn.sql": "select 1"},
                 PROJECT + "models:\n  shop:\n    schema:\n      +materialized: view\n")
    r = _ref(info)
    assert r.sql == _rel(DEFAULT_SCHEMA, "stg_txn") and r.relation_notice is None


def test_上層沒設時_None_就是沒設():
    info = _info({"models/stg/stg_txn.sql": "{{ config(alias=None, schema=None) }} select 1",
                  "models/p.yml": "models:\n  - name: stg_txn\n    config: {alias: null}\n"})
    r = _ref(info)
    assert r.sql == _rel(DEFAULT_SCHEMA, "stg_txn") and r.relation_notice is None


def test_每個設定各自合併():
    """schema 從 dbt_project.yml、alias 從 model 檔:兩個都要生效。"""
    project = PROJECT + "flags:\n  dbt_sqlserver_use_default_schema_concat: false\n" \
                        "models:\n  shop:\n    stg:\n      +schema: staging\n"
    info = _info({"models/stg/stg_txn.sql": "{{ config(alias='txn') }} select 1"}, project)
    assert _ref(info).sql == _rel("staging", "txn")


@pytest.mark.parametrize("project_yml", [
    PROJECT + "models:\n  shop:\n    alias: a\n    +alias: b\n",              # 兩種寫法值不同
    PROJECT + "models:\n  shop:\n    +alias: \"{{ var('a') }}\"\n",           # 樣板值
    PROJECT + "models:\n  shop:\n    +alias: [x]\n",                         # 不是字串
    PROJECT + "models:\n  shop:\n    +schema: {a: 1}\n",                     # + 的值是物件
])
def test_dbt_project_看不懂的寫法_這個_model_無法確定(project_yml):
    info = _info(MODEL, project_yml)
    assert "stg_txn" in info.uncertain_models
    r = _ref(info)
    assert r.sql == _rel(DEFAULT_SCHEMA, "stg_txn") and r.relation_notice


# ─────────────────── 自訂 schema(generate_schema_name 與 dbt-sqlserver 的旗標) ───────────────────

SCHEMA_SQL = {"models/stg/stg_txn.sql": "{{ config(schema='  marts  ') }} select 1"}


@pytest.mark.parametrize("extra, expected", [
    ("flags:\n  dbt_sqlserver_use_default_schema_concat: true\n", f"{DEFAULT_SCHEMA}_marts"),
    ("flags:\n  dbt_sqlserver_use_default_schema_concat: false\n", "marts"),
    ("vars:\n  dbt_sqlserver_use_default_schema_concat: true\n", f"{DEFAULT_SCHEMA}_marts"),
    # 旗標 false 而 var true:照 dbt-sqlserver 的判斷(旗標 or var)還是串接
    ("flags:\n  dbt_sqlserver_use_default_schema_concat: false\n"
     "vars:\n  dbt_sqlserver_use_default_schema_concat: true\n", f"{DEFAULT_SCHEMA}_marts"),
    # var false 不影響:看旗標
    ("flags:\n  dbt_sqlserver_use_default_schema_concat: true\n"
     "vars:\n  dbt_sqlserver_use_default_schema_concat: false\n", f"{DEFAULT_SCHEMA}_marts"),
])
def test_自訂_schema_照旗標決定要不要串接(extra, expected):
    info = _info(SCHEMA_SQL, PROJECT + extra)
    assert info.schema_concat_unset is False
    r = _ref(info)
    assert r.sql == _rel(expected, "stg_txn") and r.relation_notice is None


def test_專案沒寫旗標時_無法確定_照_target_schema_展開():
    info = _info(SCHEMA_SQL)
    assert info.schema_concat is None and info.schema_concat_unset is True
    r = _ref(info)
    assert r.sql == _rel(DEFAULT_SCHEMA, "stg_txn") and r.relation_notice


@pytest.mark.parametrize("extra", [
    "flags:\n  dbt_sqlserver_use_default_schema_concat: 'true'\n",    # 不是布林
    "vars:\n  dbt_sqlserver_use_default_schema_concat: 1\n",
    "vars:\n  shop:\n    dbt_sqlserver_use_default_schema_concat: true\n",   # 專案層級的 var
    "flags: [x]\n",
])
def test_旗標看不懂時_無法確定_而且不讓部署端預設蓋過(extra):
    info = _info(SCHEMA_SQL, PROJECT + extra)
    assert info.schema_concat is None and info.schema_concat_unset is False
    assert dbt_context._with_concat_default(info, True).schema_concat is None


@pytest.mark.parametrize("default, expected", [(True, f"{DEFAULT_SCHEMA}_marts"), (False, "marts")])
def test_專案沒寫旗標時_用部署端設定的預設(default, expected):
    info = dbt_context._with_concat_default(_info(SCHEMA_SQL), default)
    r = _ref(info)
    assert r.sql == _rel(expected, "stg_txn") and r.relation_notice is None


def test_變更前的專案資訊也補上同一個預設(monkeypatch):
    """改到 macro 時會下載變更前的專案比對受影響的 model;兩邊的表名要照同一套規則算。"""
    from tests.test_dbt_affected import CFG, MACRO_MR, _Loader, _project
    monkeypatch.setattr(dbt_context, "_summarize_isolated", dbt_context._summarize)
    loader = _Loader(_project("SELECT 1 AS id"), _project("SELECT 1 AS id"))
    ctx = dbt_context.prepare_dbt_context(MACRO_MR, dict(CFG, sqlserver_schema_concat=True),
                                          real_mode=True, load=loader)
    assert ctx.base_info is not None
    assert ctx.info.schema_concat is True and ctx.base_info.schema_concat is True


def test_部署端沒設預設時維持無法確定():
    info = dbt_context._with_concat_default(_info(SCHEMA_SQL), None)
    assert info.schema_concat is None


def test_沒有自訂_schema_時不管旗標都是_target_schema():
    """generate_schema_name:custom_schema_name 是 none 就回 target.schema,不需要知道旗標。"""
    info = _info({"models/stg/stg_txn.sql": "{{ config(alias='txn') }} select 1"})
    assert info.schema_concat is None
    r = _ref(info)
    assert r.sql == _rel(DEFAULT_SCHEMA, "txn") and r.relation_notice is None


# ─────────────────── this、資料庫、撞名、不合法的名稱 ───────────────────

def test_this_用同一套規則():
    src = "{{ config(alias='txn', schema='marts') }} select * from {{ this }}"
    info = _info({"models/stg/stg_txn.sql": src},
                 PROJECT + "flags:\n  dbt_sqlserver_use_default_schema_concat: false\n")
    r = render_model("models/stg/stg_txn.sql", source=src, database=DB, project=info)
    assert r.ok and r.sql.strip().endswith(f'"{DB}"."marts"."txn"') and r.relation_notice is None


def test_自訂_database_照樣用約定的假名():
    """資料庫名一律用呼叫端給的假名(展開結果會出現在審查結果裡,不放正式資料庫名)。"""
    info = _info({"models/stg/stg_txn.sql": "{{ config(database='REAL_PROD_DB', alias='txn') }} select 1"})
    r = _ref(info)
    assert r.sql == _rel(DEFAULT_SCHEMA, "txn") and "REAL_PROD_DB" not in r.sql


def test_算出來表名相同的_model_都無法確定():
    """dbt 碰到兩個 model 同一個表名會編譯失敗,不能當成確定。"""
    info = _info({"models/stg/a.sql": "{{ config(alias='same') }} select 1",
                  "models/stg/b.sql": "{{ config(alias='same') }} select 1",
                  "models/stg/c.sql": "select 1"})
    assert {"a", "b"} <= info.uncertain_models and "c" not in info.uncertain_models


def test_alias_跟別的_model_名稱相同也算撞名():
    info = _info({"models/stg/a.sql": "{{ config(alias='b') }} select 1",
                  "models/stg/b.sql": "select 1"})
    assert {"a", "b"} <= info.uncertain_models


@pytest.mark.parametrize("a, b", [
    ("alias='Same'", "alias='same'"),                               # SQL Server 不分大小寫
    ("alias=' same '", "alias='same'"),                             # dbt 會去掉頭尾空白
    ("alias='t', schema='Marts'", "alias='t', schema='marts'"),
    ("alias='t', schema=' marts'", "alias='t', schema='marts'"),
    ("alias='t', database='Other'", "alias='t', database='other'"),
])
def test_撞名比對不分大小寫_不看頭尾空白(a, b):
    info = _info({"models/stg/a.sql": f"{{{{ config({a}) }}}} select 1",
                  "models/stg/b.sql": f"{{{{ config({b}) }}}} select 1"})
    assert {"a", "b"} <= info.uncertain_models


def test_schema_不同就不算撞名():
    info = _info({"models/stg/a.sql": "{{ config(alias='t', schema='marts') }} select 1",
                  "models/stg/b.sql": "{{ config(alias='t', schema='stage') }} select 1"})
    assert not {"a", "b"} & info.uncertain_models


@pytest.mark.parametrize("config", ["{{ config(alias='my-table') }}", "{{ config(alias='a b') }}",
                                    "{{ config(schema='x;drop') }}"])
def test_名稱不合法時無法確定_照_model_名稱展開_不讓展開失敗(config):
    info = _info({"models/stg/stg_txn.sql": f"{config} select 1"},
                 PROJECT + "flags:\n  dbt_sqlserver_use_default_schema_concat: false\n")
    assert model_identity(info, "stg_txn", DEFAULT_SCHEMA) == ("stg_txn", DEFAULT_SCHEMA, False)
    r = _ref(info)
    assert r.sql == _rel(DEFAULT_SCHEMA, "stg_txn") and r.relation_notice


def test_屬性檔看不懂時整個專案無法確定():
    """看不懂的屬性檔裡可能就有某個 model 的 alias。"""
    info = _info(MODEL | {"models/bad.yml": "models: [\n"})
    assert info.global_uncertain
    assert _ref(info).relation_notice


def test_其他套件的_ref_照舊():
    """套件裡的 model 看不到:不套用本專案同名 model 的 alias,照舊記成無法確定。"""
    info = _info({"models/stg/stg_txn.sql": "{{ config(alias='txn') }} select 1",
                  "packages.yml": "packages: []\n"})
    r = render_model("m.sql", source="select * from {{ ref('other_pkg', 'stg_txn') }}",
                     database=DB, project=info)
    assert r.ok and r.sql == _rel(DEFAULT_SCHEMA, "stg_txn") and r.relation_notice


def test_子行程展開也帶得到算好的表名():
    """正式路徑在子行程展開(spawn);新欄位要能傳過去。"""
    info = _info({"models/stg/stg_txn.sql": "{{ config(alias='txn') }} select 1"})
    r = render_model_isolated("m.sql", source="select * from {{ ref('stg_txn') }}",
                              database=DB, project=info)
    assert r.ok and r.sql == _rel(DEFAULT_SCHEMA, "txn")


# ─────────────────── 設定檔 dbt.sqlserver_schema_concat ───────────────────

@pytest.mark.parametrize("value, expected", [(True, True), (False, False), (None, None)])
def test_設定檔的預設值(value, expected):
    from orchestrator.config import _load_dbt_section
    section = {"enabled": False, "database": ""}
    if value is not None:
        section["sqlserver_schema_concat"] = value
    got = _load_dbt_section({"dbt": section})
    assert got.get("sqlserver_schema_concat") == expected
    assert ("sqlserver_schema_concat" in got) is (value is not None)


@pytest.mark.parametrize("value", ["false", "true", 0, 1, []])
def test_設定檔的預設值只接受布林(value):
    from orchestrator.config import _load_dbt_section
    with pytest.raises(ValueError, match="sqlserver_schema_concat"):
        _load_dbt_section({"dbt": {"enabled": False, "database": "", "sqlserver_schema_concat": value}})


# ─────────────────── 對照專案(tests/dbt_reference/relation_names.py) ───────────────────

_spec = importlib.util.spec_from_file_location(
    "dbt_reference_relation_names",
    pathlib.Path(__file__).resolve().parent / "dbt_reference" / "relation_names.py")
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)


@pytest.mark.parametrize("concat", [True, False])
def test_對照專案_每個_model_都跟預期一致(concat):
    info = project_info_from_files(reference.project_files(concat), other_ref_names=(),
                                   root_files_checked=True)
    assert info.global_uncertain is None and info.schema_concat is concat
    want = reference.expected(concat)
    assert set(want) == set(info.ref_names)                 # 對照表沒有漏列任何 model
    for name, (schema, alias) in want.items():
        assert model_identity(info, name, reference.TARGET_SCHEMA) == (alias, schema, True), name


def test_拿_manifest_比對_一致與不一致(tmp_path, capsys):
    want = reference.expected(True)
    nodes = {f"model.shop.{n}": {"resource_type": "model", "name": n, "schema": s, "alias": a}
             for n, (s, a) in want.items()}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
    assert reference.check(path, True) == 0
    nodes["model.shop.plain"]["alias"] = "wrong"
    path.write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
    assert reference.check(path, True) == 1
    assert "plain" in capsys.readouterr().out


def test_寫出的對照專案就是測試用的那一份(tmp_path):
    reference.write(tmp_path, False)
    for rel, text in reference.project_files(False).items():
        assert (tmp_path / rel).read_text(encoding="utf-8") == text
    assert (tmp_path / "profiles.yml").exists()
