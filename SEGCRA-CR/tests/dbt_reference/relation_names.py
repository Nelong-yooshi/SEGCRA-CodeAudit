"""表名對照:合成小專案 + 照 dbt 規則推出的預期表名 + 拿 dbt parse 的 manifest 驗證的腳本。

預期值(expected())照 dbt 的原始碼推出,沒有跑 dbt:
  * generate_alias_name(dbt-adapters `macros/get_custom_name/get_custom_alias.sql`)
  * generate_schema_name(dbt-adapters `macros/get_custom_name/get_custom_schema.sql`)與
    dbt-sqlserver 的 sqlserver__generate_schema_name(`macros/adapters/schema.sql`)
  * 設定的合併順序(dbt 文件 "Define configs")
tests/test_dbt_relation_names.py 驗我們算的跟 expected() 一致(CI 跑,不需要 dbt)。

有 dbt-sqlserver 的環境可以拿真的 dbt 驗 expected() 本身(不連資料庫,`dbt parse` 只讀檔):

    python tests/dbt_reference/relation_names.py write <空目錄> --concat true
    cd <空目錄> && dbt parse --profiles-dir .        # profile 的 target schema 是 dbo
    python tests/dbt_reference/relation_names.py check <空目錄>/target/manifest.json --concat true

--concat 對應 dbt_project.yml 的 flags.dbt_sqlserver_use_default_schema_concat(true / false 各跑一次)。
不一致時印出是哪個 model、dbt 算的與預期的各是什麼,結束碼 1。名稱全為假名。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

TARGET_SCHEMA = "dbo"

# 每一種寫法一個 model。路徑相對專案根目錄。
MODELS = {
    "models/stg/plain.sql": "select 1 as id",
    "models/stg/sql_alias.sql": "{{ config(alias='sql_alias_x') }} select 1 as id",
    "models/stg/sql_alias_trim.sql": "{{ config(alias='  trimmed_x  ') }} select 1 as id",
    "models/stg/sql_alias_empty.sql": "{{ config(alias='') }} select 1 as id",
    "models/stg/sql_schema.sql": "{{ config(schema='marts') }} select 1 as id",
    "models/stg/yml_alias.sql": "select 1 as id",
    "models/stg/sql_over_yml.sql": "{{ config(alias='from_sql') }} select 1 as id",
    "models/marts/folder_schema.sql": "select 1 as id",
    "models/marts/deep/deeper_wins.sql": "select 1 as id",
    "models/marts/named_in_project.sql": "select 1 as id",
}
PROPERTIES = (
    "version: 2\nmodels:\n"
    "  - name: yml_alias\n    config: {alias: yml_alias_x}\n"
    "  - name: sql_over_yml\n    config: {alias: from_yml}\n"
)


def project_files(concat: bool) -> dict:
    project = (
        "name: shop\nversion: '1.0'\nprofile: shop\n"
        f"flags:\n  dbt_sqlserver_use_default_schema_concat: {str(concat).lower()}\n"
        "  send_anonymous_usage_stats: false\n"
        "models:\n  shop:\n"
        "    marts:\n      +schema: reporting\n"
        "      deep:\n        +schema: deep_reporting\n"
        "      named_in_project:\n        +alias: named_x\n"
    )
    return {"dbt_project.yml": project, "models/props.yml": PROPERTIES, **MODELS}


def expected(concat: bool) -> dict:
    """{model: (schema, alias)}。自訂 schema 串接時是 `<target>_<自訂>`,否則自訂值。"""
    def custom(s):
        return f"{TARGET_SCHEMA}_{s}" if concat else s
    return {
        "plain": (TARGET_SCHEMA, "plain"),
        "sql_alias": (TARGET_SCHEMA, "sql_alias_x"),
        "sql_alias_trim": (TARGET_SCHEMA, "trimmed_x"),        # alias | trim
        "sql_alias_empty": (TARGET_SCHEMA, "sql_alias_empty"),  # 空字串不算有值
        "sql_schema": (custom("marts"), "sql_schema"),
        "yml_alias": (TARGET_SCHEMA, "yml_alias_x"),
        "sql_over_yml": (TARGET_SCHEMA, "from_sql"),            # model 檔蓋過屬性檔
        "folder_schema": (custom("reporting"), "folder_schema"),
        "deeper_wins": (custom("deep_reporting"), "deeper_wins"),
        "named_in_project": (custom("reporting"), "named_x"),
    }


PROFILE = (
    "shop:\n  target: ref\n  outputs:\n    ref:\n      type: sqlserver\n"
    "      driver: 'ODBC Driver 18 for SQL Server'\n      server: localhost\n      port: 1433\n"
    "      database: SAMPLE_DW\n      schema: dbo\n      user: placeholder\n      password: placeholder\n"
)


def write(root: pathlib.Path, concat: bool) -> None:
    for rel, text in {**project_files(concat), "profiles.yml": PROFILE}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def check(manifest_path: pathlib.Path, concat: bool) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    got = {n["name"]: (n["schema"], n["alias"]) for n in manifest["nodes"].values()
           if n.get("resource_type") == "model"}
    want = expected(concat)
    bad = [f"{name}:dbt 算的是 {got.get(name)},預期是 {value}"
           for name, value in sorted(want.items()) if got.get(name) != value]
    for line in bad:
        print(line)
    print(f"{len(want) - len(bad)} / {len(want)} 一致")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("dir")
    c = sub.add_parser("check")
    c.add_argument("manifest")
    for p in (w, c):
        p.add_argument("--concat", choices=("true", "false"), required=True)
    args = ap.parse_args()
    concat = args.concat == "true"
    if args.cmd == "write":
        write(pathlib.Path(args.dir), concat)
        return 0
    return check(pathlib.Path(args.manifest), concat)


if __name__ == "__main__":
    sys.exit(main())
