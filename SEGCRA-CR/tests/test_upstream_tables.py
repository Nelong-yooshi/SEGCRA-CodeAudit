"""執行驗證的上游表:沿 ref() 推出上游 SQL model 與 snapshot 的表結構,補進規格(upstream_tables)。

沙盒換成替身:describe 依查詢裡讀的表回固定的欄位,記下建了哪些表。真的 SQL Server 上的實跑
結果寫在 PR 說明(這份測試不連沙盒)。
"""
import re
import sys
import types

import pytest

import orchestrator.pipeline as pipeline
import orchestrator.upstream_tables as upstream_tables
from orchestrator import spec_exec
from orchestrator.dbt_exec import render_for_exec, spec_tables
from orchestrator.pipeline import (_UPSTREAM_TITLE, _dbt_exec_mr, _render_project,
                                   _with_upstream_tables, enforce_upstream_tables)
from orchestrator.upstream_tables import (SCD_ID_TYPE, SNAPSHOT_TIME_TYPE, Snapshot, UpstreamError,
                                          _config_kwargs, _snapshot_reason, build, find_snapshots)
from tests.test_dbt_affected import CFG, DB, _Loader, review  # noqa: F401 — review 是 fixture
from tests.test_dbt_exec import _ctx, _exec_mr, _files, _p02, run  # noqa: F401 — run 是 fixture
# 匯入就會套用:專案資訊在同一個行程整理、每個測試清快取
from tests.test_dbt_affected import _fresh_cache, _summarize_in_process  # noqa: F401
from tests.test_dbt_exec import in_process  # noqa: F401

SPEC = ("# BIG 規格\n\n## 資料表定義(執行驗證以此建表)\n```sql\n"
        "CREATE TABLE transactions(account_no INT, amount DECIMAL(14,2), tx_time DATETIME2);\n```\n\n"
        "## 需求項目\n1. 金額達 100000 通報\n")
STG = "SELECT account_no, amount FROM {{ source('raw', 'transactions') }}\n"
SNAP_TS = ("{% snapshot snap_acct %}\n{{ config(strategy='timestamp', unique_key='account_no', "
           "updated_at='tx_time', target_schema='snapshots') }}\n"
           "SELECT account_no, tx_time FROM {{ source('raw', 'transactions') }}\n{% endsnapshot %}\n")
SNAP_CHECK = ("{% snapshot snap_chk %}\n{{ config(strategy='check', unique_key='account_no', "
              "check_cols=['amount']) }}\nSELECT account_no, amount FROM {{ ref('stg_a') }}\n{% endsnapshot %}\n")
MART = ("SELECT s.account_no FROM {{ ref('stg_a') }} s JOIN {{ ref('snap_acct') }} n "
        "ON n.account_no = s.account_no WHERE n.dbt_valid_to IS NULL AND s.amount >= 100000\n")

# 替身沙盒:查詢讀哪張表就回哪一組欄位(最後一個對到的)
COLUMNS = {
    "[dbo].[transactions]": None,          # 由查詢的欄位清單決定(見 FakeSession.describe)
}
TYPES = {"account_no": "int", "amount": "decimal(14,2)", "tx_time": "datetime2(7)"}


class FakeSession:
    instances: list = []

    def __init__(self, describe=None, fail_on=None):
        self.executed: list = []
        self.described: list = []
        self.closed = False
        self._describe = describe
        self._fail_on = fail_on
        FakeSession.instances.append(self)

    def execute(self, sql):
        if self._fail_on and self._fail_on.lower() in sql.lower():
            raise RuntimeError("建表失敗")
        self.executed.append(sql)

    def describe(self, sql):
        self.described.append(sql)
        if self._describe is not None:
            return self._describe(sql)
        head = re.match(r"\s*SELECT\s+(.*?)\s+FROM\b", sql, re.S | re.I)
        cols = [c.strip().split(".")[-1].split()[-1] for c in head.group(1).split(",")]
        return [(c, TYPES.get(c, "int")) for c in cols]

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset():
    FakeSession.instances = []


def _project(extra):
    ctx = _ctx(["models/mrt_BIG.sql"], extra)
    return ctx, _render_project(ctx), ctx.project


def _build(extra, spec=SPEC, factory=FakeSession, source=None):
    ctx, kwargs, project = _project(extra)
    src = source if source is not None else extra.get("models/mrt_BIG.sql", "")
    return build("models/mrt_BIG.sql", src, spec, DB, kwargs, project, session_factory=factory)


FILES = {"models/stg_a.sql": STG, "snapshots/snap_acct.sql": SNAP_TS, "models/mrt_BIG.sql": MART}


# ─────────────────── 推出上游表 ───────────────────

def test_上游_model_與_snapshot_依相依順序推出表結構_補進規格(in_process):
    r = _build(FILES)
    assert r.reason is None
    assert [(t["name"], t["kind"]) for t in r.tables] == [("stg_a", "model"), ("snap_acct", "snapshot")]
    assert r.snapshots == (("snap_acct", "snap_acct"),)
    tables = spec_tables(r.spec_text)
    assert {"transactions", "stg_a", "snap_acct"} <= set(tables)
    assert "CREATE TABLE [dbo].[stg_a] ([account_no] int, [amount] decimal(14,2));" in r.spec_text
    # snapshot:查詢的欄位 + dbt 的四個管理欄位,時間欄位跟 updated_at 同型別
    assert ("CREATE TABLE [dbo].[snap_acct] ([account_no] int, [tx_time] datetime2(7), "
            f"[dbt_scd_id] {SCD_ID_TYPE}, [dbt_updated_at] datetime2(7), [dbt_valid_from] datetime2(7), "
            "[dbt_valid_to] datetime2(7));") in r.spec_text
    s = FakeSession.instances[0]
    assert s.closed
    assert s.executed[0].upper().startswith("CREATE TABLE TRANSACTIONS")     # 先建規格的表
    assert all("[dbo].[transactions]" in d for d in s.described)


def test_推出來之後_展開時上游由那張表代替_snapshot_也是(in_process):
    r = _build(FILES)
    ctx, kwargs, _ = _project(FILES)
    sql, reason, stand_ins = render_for_exec("models/mrt_BIG.sql", MART, r.spec_text, DB, kwargs,
                                             r.snapshots)
    assert reason is None
    assert "[dbo].[stg_a]" in sql and "[dbo].[snap_acct]" in sql
    assert stand_ins == ("stg_a", "snap_acct")


def test_snapshot_沒帶進展開時_照舊無法確定(in_process):
    r = _build(FILES)
    ctx, kwargs, _ = _project(FILES)
    sql, reason, _ = render_for_exec("models/mrt_BIG.sql", MART, r.spec_text, DB, kwargs)
    assert sql is None and "無法確定" in reason


def test_兩層上游_先建最上游(in_process):
    files = {"models/stg_a.sql": STG,
             "models/int_b.sql": "SELECT account_no FROM {{ ref('stg_a') }}\n",
             "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('int_b') }}\n"}
    r = _build(files)
    assert [t["name"] for t in r.tables] == ["stg_a", "int_b"]
    s = FakeSession.instances[0]
    assert "[dbo].[stg_a]" in s.described[1]                       # int_b 讀的是剛建好的 stg_a
    assert s.executed.index(next(x for x in s.executed if "[stg_a]" in x)) < \
        s.executed.index(next(x for x in s.executed if "[int_b]" in x))
    # 測資只放在要驗的 model 直接讀的 int_b;stg_a 只在推結構時用,不補進規格
    assert [t["direct"] for t in r.tables] == [False, True]
    assert set(spec_tables(r.spec_text)) == {"transactions", "int_b"}


# 部署端的原始表放在 specs/tables/;上游 model 讀它,要驗的 model 不讀它(現場那條規則就是這樣)
RAW_ONLY = ("```sql\nCREATE TABLE [dbo].[txn_raw](\n  [acct] [int] NOT NULL,\n  [amt] [decimal](14, 2) NULL,\n"
            "  [memo] [nvarchar](50) NULL\n) ON [PRIMARY]\n```\n")
SPEC_NO_RAW = SPEC.replace("CREATE TABLE transactions(account_no INT, amount DECIMAL(14,2), tx_time DATETIME2);",
                           "CREATE TABLE other_t(x INT);")
RAW_FILES = {"models/src.yml": ("version: 2\nsources:\n  - name: raw\n    tables:\n"
                                "      - name: transactions\n      - name: txn_raw\n"),
             "models/stg_a.sql": "SELECT acct, SUM(amt) AS total FROM {{ source('raw', 'txn_raw') }} GROUP BY acct\n",
             "models/mrt_BIG.sql": "SELECT acct FROM {{ ref('stg_a') }} WHERE total >= 100000\n"}


def _repo(files):
    from orchestrator import table_defs
    return table_defs.load(files)[0]


def test_上游讀的原始表只在_specs_tables_也推得出來_原始表不補進規格(in_process):
    ctx, kwargs, project = _project(RAW_FILES)

    def describe(sql):
        assert "[dbo].[txn_raw]" in sql
        return [("acct", "int"), ("total", "decimal(38,2)")]
    r = build("models/mrt_BIG.sql", RAW_FILES["models/mrt_BIG.sql"], SPEC_NO_RAW, DB, kwargs, project,
              session_factory=lambda: FakeSession(describe=describe),
              repo_defs=_repo({"specs/tables/dbo.txn_raw.md": RAW_ONLY}))
    assert r.reason is None and [t["name"] for t in r.tables] == ["stg_a"]
    s = FakeSession.instances[0]
    # 推結構的暫用資料庫裡用完整欄位建原始表(上游讀的欄位不一定是要驗的 model 讀的)
    assert any(x.startswith("CREATE TABLE [txn_raw] ([acct] INT, [amt] DECIMAL(14,2), [memo] NVARCHAR(50))")
               for x in s.executed), s.executed
    assert set(spec_tables(r.spec_text)) == {"other_t", "stg_a"}       # 原始表沒有補進規格


def test_規格的資料表定義只寫待補_要驗的_model_只讀上游_也推得出來(in_process):
    # 現場整理的真實規格:「## 資料表定義」底下只寫了「待補」,表結構全在 specs/tables/
    # 寫成一句說明(sqlglot 解析不了;只寫「待補」兩個字會被當成欄位名稱,解析得過)
    spec = "# 規則\n\n## 資料表定義\n\n待補,欄位與型別待部署端提供。\n\n## 需求項目\n1. 累計達 100000 通報\n"
    ctx, kwargs, project = _project(RAW_FILES)
    r = build("models/mrt_BIG.sql", RAW_FILES["models/mrt_BIG.sql"], spec, DB, kwargs, project,
              session_factory=lambda: FakeSession(describe=lambda sql: [("acct", "int"), ("total", "int")]),
              repo_defs=_repo({"specs/tables/dbo.txn_raw.md": RAW_ONLY}))
    assert r.reason is None
    assert set(spec_tables(r.spec_text)) == {"stg_a"}
    assert "待補" in r.spec_text                                     # 原本的說明留著
    ctx, kwargs, _ = _project(RAW_FILES)
    sql, reason, _ = render_for_exec("models/mrt_BIG.sql", RAW_FILES["models/mrt_BIG.sql"], r.spec_text,
                                     DB, kwargs, r.snapshots)
    assert reason is None and "[dbo].[stg_a]" in sql


def test_沒給_specs_tables_時_上游讀的原始表找不到_寫出原因(in_process):
    r = _build(RAW_FILES, spec=SPEC_NO_RAW)
    assert r.tables == [] and "上游 stg_a 展開不了" in r.reason and "txn_raw" in r.reason


def test_specs_tables_裡上游沒讀的表不建(in_process):
    ctx, kwargs, project = _project(RAW_FILES)
    repo = _repo({"specs/tables/dbo.txn_raw.md": RAW_ONLY,
                  "specs/tables/dbo.unused_t.md": "CREATE TABLE dbo.unused_t(a INT);"})
    build("models/mrt_BIG.sql", RAW_FILES["models/mrt_BIG.sql"], SPEC_NO_RAW, DB, kwargs, project,
          session_factory=FakeSession, repo_defs=repo)
    assert not any("unused_t" in x for x in FakeSession.instances[0].executed)


def test_直接讀的上游欄位很多時只留要驗的_model_讀得到的欄位(in_process):
    wide = [("account_no", "int")] + [(f"c{i:02d}", "int") for i in range(40)]
    files = {"models/stg_a.sql": STG, "models/mrt_BIG.sql": "SELECT account_no, c07 FROM {{ ref('stg_a') }}\n"}
    r = _build(files, factory=lambda: FakeSession(describe=lambda sql: wide))
    assert "CREATE TABLE [dbo].[stg_a] ([account_no] int, [c07] int);" in r.spec_text
    assert r.tables[0]["columns"] == 41 and len(r.tables[0]["dropped"]) == 39


def test_要驗的_model_有_select_星號時不裁欄位(in_process):
    wide = [(f"c{i:02d}", "int") for i in range(40)]
    # 也寫到 c07:沒有星號的話會裁成只剩 c07
    files = {"models/stg_a.sql": STG, "models/mrt_BIG.sql": "SELECT * FROM {{ ref('stg_a') }} WHERE c07 = 1\n"}
    r = _build(files, factory=lambda: FakeSession(describe=lambda sql: wide))
    assert r.tables[0]["dropped"] == [] and "[c39] int" in r.spec_text


def test_check_的_snapshot_沒設_updated_at_時間欄位是_snapshot_get_time_的型別(in_process):
    files = {"models/stg_a.sql": STG, "snapshots/s.sql": SNAP_CHECK,
             "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('snap_chk') }}\n"}
    r = _build(files)
    assert r.reason is None
    assert [t["name"] for t in r.tables] == ["stg_a", "snap_chk"]
    assert f"[dbt_valid_to] {SNAPSHOT_TIME_TYPE}" in r.spec_text


def test_規格已經有上游的表時不建(in_process):
    spec = SPEC.replace("CREATE TABLE transactions(",
                        "CREATE TABLE stg_a(account_no INT);\nCREATE TABLE transactions(")
    files = {"models/stg_a.sql": STG, "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('stg_a') }}\n"}
    r = _build(files, spec=spec)
    assert r.tables == [] and r.reason is None and r.spec_text == spec
    assert FakeSession.instances == []                              # 沒有連沙盒


def test_只用_source_的_model_不建(in_process):
    r = _build({"models/mrt_BIG.sql": STG})
    assert r.tables == [] and r.reason is None and FakeSession.instances == []


# ─────────────────── 推不出來:不補、寫出原因 ───────────────────

@pytest.mark.parametrize("extra, why", [
    ({"seeds/code_seed.csv": "a\n1\n",
      "models/mrt_BIG.sql": "SELECT a FROM {{ ref('code_seed') }}\n"}, "無法確定"),
    ({"models/a.sql": "SELECT x FROM {{ ref('b') }}\n", "models/b.sql": "SELECT x FROM {{ ref('a') }}\n",
      "models/mrt_BIG.sql": "SELECT x FROM {{ ref('a') }}\n"}, "繞回自己"),
    ({"models/stg_a.sql": "SELECT x FROM {{ source('raw', 'nope') }}\n",
      "models/mrt_BIG.sql": "SELECT x FROM {{ ref('stg_a') }}\n"}, "上游 stg_a 展開不了"),
])
def test_推不出來的情況(in_process, extra, why):
    r = _build(extra)
    assert r.tables == [] and r.spec_text == SPEC
    assert r.reason is None or why in r.reason
    if why != "無法確定":
        assert why in r.reason


def test_seed_上游_展開時說表名無法確定(in_process):
    extra = {"seeds/code_seed.csv": "a\n1\n", "models/mrt_BIG.sql": "SELECT a FROM {{ ref('code_seed') }}\n"}
    r = _build(extra)
    ctx, kwargs, _ = _project(extra)
    _, reason, _ = render_for_exec("models/mrt_BIG.sql", extra["models/mrt_BIG.sql"], r.spec_text, DB, kwargs)
    assert "無法確定" in reason


@pytest.mark.parametrize("config, why", [
    ("strategy='timestamp', unique_key='id', updated_at='tx_time', alias='not valid'", "alias 不是固定的表名"),
    ("strategy='timestamp', unique_key='id', updated_at='tx_time', alias=3", "alias 不是固定的表名"),
    ("strategy='timestamp', unique_key='id', updated_at='tx_time', hard_deletes='new_record'", "hard_deletes"),
    ("strategy='timestamp', unique_key='id', updated_at='tx_time', dbt_valid_to_current=\"'9999-12-31'\"",
     "dbt_valid_to_current"),
    ("strategy='timestamp', unique_key='id', updated_at='tx_time', "
     "snapshot_table_column_names={'dbt_valid_to': 'valid_to'}", "snapshot_table_column_names"),
    ("strategy='timestamp', unique_key='id'", "沒有寫 updated_at"),
    ("strategy=var('s'), unique_key='id'", "不是常值"),
    ("unique_key='id', updated_at='tx_time'", "strategy"),
    ("strategy='timestamp', unique_key='id', updated_at='cast(tx_time as date)'", "不是欄位名稱"),
])
def test_snapshot_設定無法確定時寫出原因(in_process, config, why):
    snap = ("{% snapshot snap_acct %}\n{{ config(" + config + ") }}\n"
            "SELECT account_no, tx_time FROM {{ source('raw', 'transactions') }}\n{% endsnapshot %}\n")
    r = _build({"snapshots/snap_acct.sql": snap,
                "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('snap_acct') }}\n"})
    assert r.tables == [] and r.spec_text == SPEC
    assert r.reason and "snap_acct" in r.reason and why in r.reason


def test_專案設定的_snapshots_區段設了會改欄位的設定時不建(in_process):
    files = dict(FILES)
    files["dbt_project.yml"] = "name: sample\nsnapshots:\n  sample:\n    +hard_deletes: new_record\n"
    r = _build(files)
    assert r.tables == [] and "hard_deletes" in r.reason


# 區塊裡只寫 unique_key,strategy 與 updated_at 放在專案設定或屬性檔(常見的共用寫法)
SNAP_BARE = ("{% snapshot snap_acct %}\n{{ config(unique_key='account_no') }}\n"
             "SELECT account_no, tx_time FROM {{ source('raw', 'transactions') }}\n{% endsnapshot %}\n")
BARE_FILES = {"snapshots/snap_acct.sql": SNAP_BARE,
              "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('snap_acct') }}\n"}


@pytest.mark.parametrize("project_yml", [
    "name: sample\nsnapshots:\n  sample:\n    +strategy: timestamp\n    +updated_at: tx_time\n"
    "    +target_schema: snapshots\n",
    "name: sample\nsnapshots:\n  sample:\n    +strategy: check\n    +target_schema: snapshots\n"
    "    +updated_at: tx_time\n",
    "name: sample\nsnapshots:\n  sample:\n    +target_schema: snapshots\n    +strategy: check\n"
    "    +updated_at: tx_time\n    other_dir:\n      +alias: x\n",            # 別的資料夾的設定不套
], ids=["timestamp", "check", "other-folder"])
def test_strategy_寫在專案設定的_snapshots_區段也照建(in_process, project_yml):
    r = _build({**BARE_FILES, "dbt_project.yml": project_yml})
    assert r.reason is None and [t["name"] for t in r.tables] == ["snap_acct"]
    assert "[dbt_valid_to] datetime2(7)" in r.spec_text                  # 跟 updated_at 同型別


def test_專案設定照資料夾一層層套_越深的蓋掉越淺的(in_process):
    files = {"snapshots/acct/snap_acct.sql": SNAP_BARE,
             "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('snap_acct') }}\n",
             "dbt_project.yml": ("name: sample\nsnapshots:\n  sample:\n    +strategy: timestamp\n"
                                 "    +updated_at: missing_col\n    acct:\n      +updated_at: tx_time\n")}
    r = _build(files)
    assert r.reason is None and "[dbt_valid_to] datetime2(7)" in r.spec_text


def test_區塊的_config_蓋掉專案設定(in_process):
    snap = SNAP_BARE.replace("config(unique_key='account_no')",
                             "config(unique_key='account_no', strategy='check')")
    files = {**BARE_FILES, "snapshots/snap_acct.sql": snap,
             "dbt_project.yml": "name: sample\nsnapshots:\n  sample:\n    +strategy: timestamp\n"}
    r = _build(files)
    assert r.reason is None and f"[dbt_valid_to] {SNAPSHOT_TIME_TYPE}" in r.spec_text


def test_strategy_寫在屬性檔也照建(in_process):
    files = {**BARE_FILES, "snapshots/props.yml": (
        "version: 2\nsnapshots:\n  - name: snap_acct\n    config:\n      strategy: timestamp\n"
        "      updated_at: tx_time\n")}
    r = _build(files)
    assert r.reason is None and [t["name"] for t in r.tables] == ["snap_acct"]


@pytest.mark.parametrize("where", ["block", "yml", "project"])
def test_snapshot_設了固定的_alias_表名就是_alias(in_process, where):
    files = dict(FILES)
    if where == "block":
        files["snapshots/snap_acct.sql"] = SNAP_TS.replace("target_schema='snapshots'",
                                                           "target_schema='snapshots', alias='acct_hist'")
    elif where == "yml":
        files["snapshots/props.yml"] = ("version: 2\nsnapshots:\n  - name: snap_acct\n"
                                        "    config:\n      alias: acct_hist\n")
    else:
        files["dbt_project.yml"] = "name: sample\nsnapshots:\n  sample:\n    +alias: acct_hist\n"
    r = _build(files)
    assert r.reason is None
    assert [(t["name"], t["table"]) for t in r.tables] == [("stg_a", "stg_a"), ("snap_acct", "acct_hist")]
    assert r.snapshots == (("snap_acct", "acct_hist"),)
    assert "CREATE TABLE [dbo].[acct_hist] (" in r.spec_text and "[snap_acct]" not in r.spec_text
    ctx, kwargs, _ = _project(files)
    sql, reason, stand_ins = render_for_exec("models/mrt_BIG.sql", MART, r.spec_text, DB, kwargs, r.snapshots)
    assert reason is None and "[dbo].[acct_hist]" in sql and stand_ins == ("stg_a", "snap_acct")


def test_用_yml_定義的_snapshot_不建(in_process):
    files = dict(FILES)
    files["snapshots/props.yml"] = ("version: 2\nsnapshots:\n  - name: snap_acct\n"
                                    "    relation: source('raw', 'transactions')\n"
                                    "    config:\n      strategy: check\n")
    r = _build(files)
    assert r.tables == [] and "yml 定義" in r.reason


@pytest.mark.parametrize("project_yml", [
    "name: sample\nsnapshots: [1, 2]\n",
    "name: sample\nsnapshots:\n  sample: 3\n",
    "name: sample\nsnapshots:\n  sample:\n    1: x\n",
], ids=["list", "scalar-node", "non-str-key"])
def test_專案設定看不懂時不建(in_process, project_yml):
    r = _build({**FILES, "dbt_project.yml": project_yml})
    assert r.tables == [] and "dbt_project.yml 的 snapshots: 區段看不懂" in r.reason


def test_屬性檔只寫說明時照建(in_process):
    files = dict(FILES)
    files["snapshots/props.yml"] = ("version: 2\nsnapshots:\n  - name: snap_acct\n"
                                    "    description: 帳戶快照\n")
    r = _build(files)
    assert r.reason is None and [t["name"] for t in r.tables] == ["stg_a", "snap_acct"]


def test_snapshot_的查詢自己有管理欄位名稱時不建(in_process):
    snap = SNAP_TS.replace("SELECT account_no, tx_time", "SELECT account_no, tx_time, tx_time AS dbt_valid_to")
    files = {"snapshots/snap_acct.sql": snap,
             "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('snap_acct') }}\n"}
    r = _build(files)
    assert r.tables == [] and "管理欄位" in r.reason


def test_snapshot_的_updated_at_不是輸出欄位時不建(in_process):
    snap = SNAP_TS.replace("SELECT account_no, tx_time", "SELECT account_no")
    r = _build({"snapshots/snap_acct.sql": snap,
                "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('snap_acct') }}\n"})
    assert r.tables == [] and "updated_at" in r.reason and "不是查詢輸出的欄位" in r.reason


def test_上游是增量_model_時不建(in_process):
    files = {"models/stg_a.sql": "SELECT account_no FROM {{ source('raw', 'transactions') }}\n"
                                 "{% if is_incremental() %} WHERE 1 = 1 {% endif %}\n",
             "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('stg_a') }}\n"}
    r = _build(files)
    assert r.tables == [] and "上游 stg_a 展開不了" in r.reason and "增量" in r.reason


@pytest.mark.parametrize("described, why", [
    ([], "沒有輸出欄位"),
    ([(None, "int")], "沒有名稱"),
    ([("a", "int"), ("A", "int")], "兩個叫"),
    ([("a", "xml")], "不能拿來建測資表"),
    ([("a", "sql_variant")], "不能拿來建測資表"),
])
def test_沙盒描述出來的欄位不能用時不建(in_process, described, why):
    files = {"models/stg_a.sql": STG, "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('stg_a') }}\n"}
    r = _build(files, factory=lambda: FakeSession(describe=lambda sql: described))
    assert r.tables == [] and r.spec_text == SPEC
    assert "stg_a" in r.reason and why in r.reason
    assert FakeSession.instances[0].closed


def test_沙盒描述失敗時寫出原因(in_process):
    def boom(sql):
        raise UpstreamError("Invalid column name 'zz'.")
    files = {"models/stg_a.sql": STG, "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('stg_a') }}\n"}
    r = _build(files, factory=lambda: FakeSession(describe=boom))
    assert r.tables == [] and "Invalid column name" in r.reason and FakeSession.instances[0].closed


def test_沒有沙盒時寫出原因_規格不動(in_process):
    def none():
        raise UpstreamError("未設定 SANDBOX_MSSQL_PASSWORD,沒有沙盒可以推上游表結構")
    r = _build(FILES, factory=none)
    assert r.tables == [] and r.spec_text == SPEC and "SANDBOX_MSSQL_PASSWORD" in r.reason


def test_規格的表在沙盒建不出來時不建(in_process):
    r = _build(FILES, factory=lambda: FakeSession(fail_on="CREATE TABLE TRANSACTIONS"))
    assert r.tables == [] and "規格的表在沙盒建不出來" in r.reason and FakeSession.instances[0].closed


def test_上游超過上限時不建(in_process, monkeypatch):
    monkeypatch.setattr(upstream_tables, "MAX_UPSTREAM", 1)
    r = _build(FILES)
    assert r.tables == [] and "超過 1 張" in r.reason


def test_上游超過層數上限時不建(in_process, monkeypatch):
    monkeypatch.setattr(upstream_tables, "MAX_DEPTH", 1)
    files = {"models/stg_a.sql": STG,
             "models/int_b.sql": "SELECT account_no FROM {{ ref('stg_a') }}\n",
             "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('int_b') }}\n"}
    r = _build(files)
    assert r.tables == [] and "超過 1 層" in r.reason


# ─────────────────── snapshot 設定的解析 ───────────────────

@pytest.mark.parametrize("body, want", [
    ("SELECT 1", {}),
    ("{{ config(strategy='check', check_cols='all', unique_key=\"id\") }} SELECT 1",
     {"strategy": "check", "check_cols": "all", "unique_key": "id"}),
    ("{{ config(strategy='check', note='a)b') }}", {"strategy": "check", "note": "a)b"}),
    ("{{ config(**x) }}", None),
    ("{{ config('timestamp') }}", None),
    ("{{ config(strategy='a') }}{{ config(unique_key='b') }}", None),
    ("{{ config(strategy='a' }}", None),
])
def test_config_的參數(body, want):
    assert _config_kwargs(body) == want


def test_check_不設_updated_at_用_snapshot_get_time():
    assert _snapshot_reason({"strategy": "check", "unique_key": "id", "check_cols": "all"}) == (None, None)
    assert _snapshot_reason({"strategy": "check", "updated_at": "u"}) == ("u", None)


def test_找_snapshot_只看_snapshot_目錄(in_process):
    _, _, project = _project({"snapshots/snap_acct.sql": SNAP_TS,
                              "models/fake.sql": SNAP_TS.replace("snap_acct", "fake_snap")})
    found = find_snapshots(project)
    assert set(found) == {"snap_acct"} and found["snap_acct"].updated_at == "tx_time"


def test_同名的_snapshot_不只一個時無法確定(in_process):
    _, _, project = _project({"snapshots/a.sql": SNAP_TS, "snapshots/b.sql": SNAP_TS})
    assert "不只一個" in find_snapshots(project)["snap_acct"].reason


# ─────────────────── 沙盒連線(假的 pymssql) ───────────────────

def _fake_pymssql(monkeypatch, rows, sent):
    class Cursor:
        def execute(self, sql, params=None):
            sent.append((sql, params))

        def fetchall(self):
            return rows

    class Con:
        def cursor(self):
            return Cursor()

        def close(self):
            sent.append(("close", None))

    fake = types.ModuleType("pymssql")
    fake.connect = lambda **kw: Con()
    monkeypatch.setitem(sys.modules, "pymssql", fake)
    monkeypatch.setattr(spec_exec, "_sandbox_params",
                        lambda: {"server": "x", "port": 1433, "user": "sa", "password": "pw"})
    monkeypatch.setattr(spec_exec, "_drop_stale_sandbox_dbs", lambda admin: None)


def test_沙盒連線_建暫用資料庫_描述查詢_用完刪掉(monkeypatch):
    sent: list = []
    _fake_pymssql(monkeypatch, [("a", "int", None)], sent)
    s = upstream_tables.SandboxSession()
    assert s.describe("SELECT 1 AS a") == [("a", "int")]
    s.close()
    sqls = [q for q, _ in sent]
    assert any(q.startswith("CREATE DATABASE [segcra_sbx_") for q in sqls)
    assert any("dm_exec_describe_first_result_set" in q for q in sqls)
    assert ("SELECT 1 AS a",) in [p for _, p in sent]                 # 查詢當參數傳,不拼進 SQL
    assert any(q.startswith("ALTER DATABASE [segcra_sbx_") and "DROP DATABASE" in q for q in sqls)


def test_沙盒描述回錯誤時丟出原因(monkeypatch):
    _fake_pymssql(monkeypatch, [(None, None, "Invalid object name 'x'.")], [])
    s = upstream_tables.SandboxSession()
    with pytest.raises(UpstreamError, match="Invalid object name"):
        s.describe("SELECT * FROM x")


def test_沒設沙盒密碼時不連線(monkeypatch):
    monkeypatch.setattr(spec_exec, "_sandbox_params", lambda: {"password": ""})
    with pytest.raises(UpstreamError, match="SANDBOX_MSSQL_PASSWORD"):
        upstream_tables.SandboxSession()


# ─────────────────── pipeline ───────────────────

def test_pipeline_推出上游表後_展開送進沙盒(in_process, monkeypatch):
    monkeypatch.setattr(upstream_tables, "SandboxSession", FakeSession)
    mr = _exec_mr(["models/mrt_BIG.sql"], FILES)
    ctx = _ctx(["models/mrt_BIG.sql"], FILES)
    text, record = _with_upstream_tables(SPEC, mr, ctx, CFG)
    assert [t["name"] for t in record["tables"]] == ["stg_a", "snap_acct"]
    assert record["snapshots"] == [("snap_acct", "snap_acct")] and record["reason"] is None
    out, rec = _dbt_exec_mr(mr, text, text, (), ctx, CFG, record["snapshots"])
    assert rec["ok"] is True and rec["stand_ins"] == ["stg_a", "snap_acct"]
    assert "[dbo].[snap_acct]" in out["files"][0]["full_content"]


def test_pipeline_把_specs_tables_的完整定義交給推結構(in_process, monkeypatch):
    import types as _t
    monkeypatch.setattr(pipeline.spec_index, "current_index",
                        lambda: _t.SimpleNamespace(error=None, tables={"specs/tables/dbo.txn_raw.md": RAW_ONLY}))
    monkeypatch.setattr(upstream_tables, "SandboxSession",
                        lambda: FakeSession(describe=lambda sql: [("acct", "int"), ("total", "int")]))
    mr = _exec_mr(["models/mrt_BIG.sql"], RAW_FILES)
    text, record = _with_upstream_tables(SPEC_NO_RAW, mr, _ctx(["models/mrt_BIG.sql"], RAW_FILES), CFG)
    assert record["reason"] is None and [t["name"] for t in record["tables"]] == ["stg_a"]
    assert any("[txn_raw]" in x for x in FakeSession.instances[0].executed)


def test_pipeline_不是_dbt_model_時不適用(in_process, monkeypatch):
    monkeypatch.setattr(upstream_tables, "SandboxSession", FakeSession)
    mr = {"files": [{"path": "sql/a.sql", "full_content": "SELECT 1"}]}
    assert _with_upstream_tables(SPEC, mr, _ctx(["models/mrt_BIG.sql"], FILES), CFG) == (SPEC, None)
    off = {**CFG, "enabled": False}
    assert _with_upstream_tables(SPEC, _exec_mr(["models/mrt_BIG.sql"], FILES),
                                 _ctx(["models/mrt_BIG.sql"], FILES), off) == (SPEC, None)


def test_整條審查_推出上游表_測資生成拿到補過的規格(run, monkeypatch):
    monkeypatch.setattr(upstream_tables, "SandboxSession", FakeSession)
    seen = {}
    real = spec_exec.generate_cases

    async def capture(cfg, code, text, profile_name=None):
        seen["text"] = text
        return await real(cfg, code, text, profile_name)
    monkeypatch.setattr(spec_exec, "generate_cases", capture)
    report, sqls = run(["models/mrt_BIG.sql"], {k: v for k, v in FILES.items()})
    assert sqls and "[dbo].[snap_acct]" in sqls[0]
    assert "CREATE TABLE [dbo].[snap_acct]" in seen["text"]
    assert [t["name"] for t in report["_upstream_tables"]["tables"]] == ["stg_a", "snap_acct"]
    note = next(f for f in report["findings"] if f["title"] == _UPSTREAM_TITLE)
    assert "snap_acct(snapshot)" in note["detail"] and "沒有在這次執行" in note["detail"]
    assert note["severity"] == "info"


def test_整條審查_推不出來時寫出原因_照舊交人工(run, monkeypatch):
    def none():
        raise UpstreamError("未設定 SANDBOX_MSSQL_PASSWORD,沒有沙盒可以推上游表結構")
    monkeypatch.setattr(upstream_tables, "SandboxSession", none)
    report, sqls = run(["models/mrt_BIG.sql"], {k: v for k, v in FILES.items()})
    assert sqls == []
    note = next(f for f in report["findings"] if f["title"] == _UPSTREAM_TITLE)
    assert "SANDBOX_MSSQL_PASSWORD" in note["detail"]
    assert report["decision"] == "needs_human"


def test_沙盒說的缺欄位是裁掉的欄位時_不說成_SQL_的錯():
    from orchestrator.pipeline import _SQL_RUN_TITLE
    report = {"findings": [{"title": _SQL_RUN_TITLE, "_source": "program", "severity": "major",
                            "detail": "Invalid column name 'c09'.", "file": "x", "line": 0}]}
    record = {"tables": [{"name": "stg_a", "kind": "model", "direct": True, "dropped": ["c09"]},
                         {"name": "deep", "kind": "model", "direct": False, "dropped": []}],
              "snapshots": [], "reason": None}
    # 模型自己寫的同標題 finding、缺的是沒裁掉的欄位:都不改寫
    report["findings"] += [{"title": _SQL_RUN_TITLE, "severity": "major", "file": "x", "line": 0,
                            "detail": "Invalid column name 'c09'."},
                           {"title": _SQL_RUN_TITLE, "_source": "program", "severity": "major", "file": "x",
                            "line": 0, "detail": "Invalid column name 'zz'."}]
    out = enforce_upstream_tables(report, record)
    f = out["findings"][0]
    assert f["title"] == "執行驗證無法完成(測資表少了欄位)" and f["severity"] == "major"
    assert [x["title"] for x in out["findings"][1:3]] == [_SQL_RUN_TITLE, _SQL_RUN_TITLE]
    note = next(x for x in out["findings"] if x["title"] == _UPSTREAM_TITLE)
    assert "stg_a(model)" in note["detail"] and "deep" not in note["detail"]
    assert "上游的上游" in note["detail"]


def test_紀錄_模型輸出的同名欄位蓋掉():
    report = {"findings": [], "_upstream_tables": {"tables": [{"name": "假的"}]}}
    out = enforce_upstream_tables(report, None)
    assert out["_upstream_tables"] is None and out["findings"] == []


# ─────────────────── 給測資生成的上游說明 ───────────────────

def _context(spec_text):
    head = spec_text.index(upstream_tables.CONTEXT_HEADING)
    return spec_text[head:]


def test_補上的規格最後附上上游說明_讀哪幾張_怎麼來_要一致(in_process):
    r = _build(FILES)
    ctx = _context(r.spec_text)
    assert "要驗的 model 直接讀這幾張表:snap_acct、stg_a。" in ctx
    assert "要一致" in ctx and "不是需求,不要拆成條件" in ctx
    assert "**stg_a**(dbt model)" in ctx and "**snap_acct**(dbt snapshot)" in ctx
    assert ctx.count("[dbo].[transactions]") == 2                  # 附的是展開後的 SQL
    # 說明段在資料表定義之後,不影響表的解析
    assert set(spec_tables(r.spec_text)) == {"transactions", "stg_a", "snap_acct"}
    assert r.spec_text.index("## 資料表定義") < r.spec_text.index("## 需求項目") \
        < r.spec_text.index(upstream_tables.CONTEXT_HEADING)


def test_說明只附直接讀的上游(in_process):
    files = {"models/stg_a.sql": STG,
             "models/int_b.sql": "SELECT account_no FROM {{ ref('stg_a') }}\n",
             "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('int_b') }}\n"}
    ctx = _context(_build(files).spec_text)
    assert "直接讀這幾張表:int_b。" in ctx
    assert "**int_b**" in ctx and "**stg_a**" not in ctx


def test_定義太長時截斷_整段超過上限時不再附(in_process, monkeypatch):
    monkeypatch.setattr(upstream_tables, "MAX_DEFINITION_CHARS", 10)
    ctx = _context(_build(FILES).spec_text)
    assert ctx.count("-- (以下省略)") == 2
    monkeypatch.setattr(upstream_tables, "MAX_DEFINITION_CHARS", 1500)
    base = len(_context_head())
    monkeypatch.setattr(upstream_tables, "MAX_CONTEXT_CHARS", base + 1)
    ctx = _context(_build(FILES).spec_text)
    assert "**stg_a**" not in ctx and "沒有附上" in ctx


def _context_head():
    return upstream_tables._context_section(["snap_acct", "stg_a"], [])


def test_沒有推出上游時不附說明(in_process):
    spec = SPEC.replace("tx_time DATETIME2);", "tx_time DATETIME2);\nCREATE TABLE stg_a(account_no INT, "
                        "amount DECIMAL(14,2));\nCREATE TABLE snap_acct(account_no INT, dbt_valid_to DATETIME2);")
    r = _build(FILES, spec=spec)
    assert upstream_tables.CONTEXT_HEADING not in r.spec_text
