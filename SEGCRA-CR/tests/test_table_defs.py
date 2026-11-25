"""table_defs:specs/tables/ 的表定義補進執行驗證用的規格。"""
import json

import pytest

import orchestrator.pipeline as pipeline
import orchestrator.spec_exec as spec_exec
from orchestrator import spec_index, table_defs
from orchestrator.dbt_exec import spec_tables
from orchestrator.table_defs import TRIM_THRESHOLD, augment, load, parse_file
from tests.test_dbt_affected import CFG, FRAGMENT, PLAIN_SQL, _Loader, _project, review  # noqa: F401
from tests.test_dbt_affected import _fresh_cache, _summarize_in_process  # noqa: F401

PATH = "specs/tables/dbo.txn_log.md"

# SSMS「Script Table as → CREATE To」的輸出長相(表名、欄位皆為虛構)
SSMS = """USE [SAMPLE_DW]
GO

/****** Object:  Table [dbo].[txn_log]    Script Date: 2026/11/24 ******/
SET ANSI_NULLS ON
GO

SET QUOTED_IDENTIFIER ON
GO

CREATE TABLE [dbo].[txn_log](
\t[txn_id] [bigint] IDENTITY(1,1) NOT NULL,
\t[acct_no] [varchar](20) NOT NULL,
\t[txn_date] [varchar](8) NULL,
\t[amt] [decimal](18, 2) NOT NULL,
\t[memo] [ntext] NULL,
\t[rate] [real] NULL,
\t[upd_time] [datetime2](7) NULL,
\t[flag] [char](1) NOT NULL CONSTRAINT [DF_x] DEFAULT ('N'),
\t[order] [int] NULL,
\t[total]  AS ([amt]*(2)),
\t[shape] [geography] NULL,
 CONSTRAINT [PK_txn_log] PRIMARY KEY CLUSTERED
(
\t[txn_id] ASC
)WITH (PAD_INDEX = OFF, STATISTICS_NORECOMPUTE = OFF) ON [PRIMARY]
) ON [PRIMARY] TEXTIMAGE_ON [PRIMARY]
GO

ALTER TABLE [dbo].[txn_log] ADD  CONSTRAINT [DF_txn_log_flag]  DEFAULT ('N') FOR [flag]
GO
"""

COLUMNS = (("txn_id", "BIGINT"), ("acct_no", "VARCHAR(20)"), ("txn_date", "VARCHAR(8)"),
           ("amt", "DECIMAL(18,2)"), ("memo", "NTEXT"), ("rate", "REAL"),
           ("upd_time", "DATETIME2(7)"), ("flag", "CHAR(1)"), ("order", "INT"))


# ─────────────────── 解析一個檔 ───────────────────

@pytest.mark.parametrize("wrap", [
    lambda s: s,
    lambda s: "```sql\n" + s + "\n```\n",
    lambda s: "# txn_log\n\n說明文字\n\n```sql\n" + s + "```\n",
    lambda s: "﻿" + s.replace("\n", "\r\n"),
])
def test_ssms_output_is_parsed(wrap):
    table, reason = parse_file(PATH, wrap(SSMS))
    assert reason is None
    assert (table.schema, table.name, table.source) == ("dbo", "txn_log", PATH)
    assert table.columns == COLUMNS, "型別要保留 SQL Server 原本的寫法(ntext、real 不能被改掉)"
    assert table.computed == ("total",) and table.unsupported == ("shape",)


@pytest.mark.parametrize("decl, want", [
    ("decimal(18)", "DECIMAL(18)"), ("numeric( 10 , 0 )", "NUMERIC(10,0)"), ("float(53)", "FLOAT(53)"),
    ("varchar(MAX)", "VARCHAR(MAX)"), ("[nvarchar](max)", "NVARCHAR(MAX)"), ("time(3)", "TIME(3)"),
    ("datetime", "DATETIME"), ("uniqueidentifier", "UNIQUEIDENTIFIER"), ("varbinary(16)", "VARBINARY(16)"),
])
def test_type_arguments_kept(decl, want):
    table, reason = parse_file(PATH, f"CREATE TABLE txn_log([a] {decl})")
    assert reason is None and table.columns == (("a", want),)


def test_without_schema_means_dbo():
    text = "CREATE TABLE txn_log (acct_no VARCHAR(20), amt MONEY)"
    table, reason = parse_file(PATH, text)
    assert reason is None and table.columns == (("acct_no", "VARCHAR(20)"), ("amt", "MONEY"))


@pytest.mark.parametrize("path, text, why", [
    ("specs/tables/txn_log.md", SSMS, "檔名"),
    ("specs/tables/dbo.txn-log.md", SSMS, "檔名"),
    ("specs/tables/sub/dbo.txn_log.md", SSMS, "檔名"),
    ("specs/tables/dbo.txn_log.sql", SSMS, "檔名"),
    (PATH, "# 只有說明", "找不到 CREATE TABLE"),
    (PATH, SSMS + "\nCREATE TABLE [dbo].[x]([a] int)\n", "不只一句"),
    ("specs/tables/dbo.other.md", SSMS, "表名跟檔名不同"),
    ("specs/tables/rpt.txn_log.md", SSMS, "表名跟檔名不同"),
    (PATH, "CREATE TABLE [SAMPLE_DW].[dbo].[txn_log]([a] int)", "資料庫名"),
    (PATH, "CREATE TABLE [dbo].[txn_log]([金額] int)", "英數字"),
    (PATH, "CREATE TABLE [dbo].[txn_log]([a] int", "括號"),
    (PATH, "CREATE TABLE [dbo].[txn_log]", "欄位清單"),
    (PATH, "CREATE TABLE [dbo].[txn_log](CONSTRAINT [PK] PRIMARY KEY ([a]))", "沒有欄位"),
    (PATH, "x" * (table_defs.MAX_FILE_CHARS + 1), "太大"),
])
def test_files_not_adopted(path, text, why):
    table, reason = parse_file(path, text)
    assert table is None and why in reason


def test_comments_and_strings_do_not_break_parsing():
    """註解裡的 CREATE TABLE 不算;字串常值裡的 -- 與括號不影響切欄位。"""
    text = ("-- CREATE TABLE fake(a int)\n/* CREATE TABLE fake2(b int) */\n"
            "CREATE TABLE [dbo].[txn_log]([note] varchar(10) DEFAULT ('--(,'), [b] int)")
    table, reason = parse_file(PATH, text)
    assert reason is None and table.columns == (("note", "VARCHAR(10)"), ("b", "INT"))


@pytest.mark.parametrize("decl", ["[a] int(5)", "[a] char(max)", "[a] datetime2(9)",
                                  "[a] varchar(abc)", "[a] MyType", "[a] timestamp",
                                  "[a] rowversion", "[a] xml", "[a] sql_variant", "[a]"])
def test_unsupported_types(decl):
    table, reason = parse_file(PATH, f"CREATE TABLE txn_log({decl}, [b] int)")
    assert reason is None and table.unsupported == ("a",) and table.columns == (("b", "INT"),)


# ─────────────────── 讀整個目錄 ───────────────────

def test_load_marks_ambiguous_and_skips_deleted():
    files = {"specs/tables/dbo.t.md": "CREATE TABLE dbo.t(a int)",
             "specs/tables/rpt.t.md": "CREATE TABLE rpt.t(a int)",
             "specs/tables/dbo.gone.md": "",
             "specs/tables/sub/dbo.x.md": "CREATE TABLE dbo.x(a int)",
             "specs/R-1.md": "# 規格"}
    defs, notes = load(files)
    assert defs == {"t": table_defs.AMBIGUOUS} and notes == []


def test_load_keeps_failures_by_table_name():
    defs, notes = load({"specs/tables/dbo.bad.md": "CREATE TABLE dbo.other(a int)",
                        "specs/tables/nodot.md": "CREATE TABLE nodot(a int)"})
    assert "表名跟檔名不同" in defs["bad"]
    assert len(notes) == 1 and "specs/tables/nodot.md" in notes[0]


def test_load_limits_file_count():
    files = {f"specs/tables/dbo.t{i}.md": f"CREATE TABLE dbo.t{i}(a int)"
             for i in range(table_defs.MAX_TABLE_FILES + 3)}
    defs, notes = load(files)
    assert len(defs) == table_defs.MAX_TABLE_FILES and "超過上限" in notes[0]


# ─────────────────── 補進規格 ───────────────────

SPEC_FENCED = ("# R\n\n## 資料表定義\n```sql\nCREATE TABLE acct(acct_no VARCHAR(20));\n```\n\n"
               "## 需求\n- x\n")
SPEC_RAW = "# R\n\n## 資料表定義\nCREATE TABLE acct(acct_no VARCHAR(20));\n\n## 需求\n- x\n"
SPEC_NONE = "# R\n\n## 需求\n- x\n"
# 段落只寫了說明文字(部署端整理規格時常留「待補」):補上的表要讀得到,說明文字不刪
SPEC_PENDING = "# R\n\n## 資料表定義()\n\n待補(欄位與型別向部署端確認)\n\n## 需求\n- x\n"
SPEC_EMPTY = "# R\n\n## 資料表定義\n\n## 需求\n- x\n"
SQL = "SELECT t.acct_no, t.amt FROM dbo.txn_log t JOIN acct a ON a.acct_no = t.acct_no"


def _defs():
    return load({PATH: SSMS})[0]


@pytest.mark.parametrize("spec", [SPEC_FENCED, SPEC_RAW, SPEC_NONE, SPEC_PENDING, SPEC_EMPTY])
def test_augment_adds_table_where_spec_tables_reads_it(spec):
    text, record = augment(spec, [SQL], _defs(), spec_tables(spec))
    tables = spec_tables(text)
    assert tables["txn_log"] == "[txn_log]"
    assert ("acct" in tables) == (spec in (SPEC_FENCED, SPEC_RAW)), "規格原本的表不能不見"
    assert "## 需求\n- x" in text
    assert spec != SPEC_PENDING or "待補(欄位與型別向部署端確認)" in text
    assert record["tables"][0]["table"] == "txn_log" and record["tables"][0]["kept"] == 9
    assert "IDENTITY" not in text and "NOT NULL" not in text and "DEFAULT" not in text
    assert "PRIMARY" not in text and "SAMPLE_DW" not in text and "Script Date" not in text
    assert "[order] INT" in text and "[memo] NTEXT" in text


def test_spec_definition_wins():
    spec = SPEC_FENCED.replace("acct(acct_no", "txn_log(acct_no")
    text, record = augment(spec, [SQL], _defs(), spec_tables(spec))
    assert text == spec and record is None


def test_unused_tables_are_not_added():
    text, record = augment(SPEC_NONE, ["SELECT 1 FROM acct"], _defs(), {})
    assert text == SPEC_NONE and record is None


def test_used_computed_or_unsupported_column_not_adopted():
    for col in ("total", "shape"):
        text, record = augment(SPEC_NONE, [SQL + f" WHERE t.{col} > 0"], _defs(), {})
        assert text == SPEC_NONE and record["tables"] == []
        assert col in record["notes"][0] and "沒有採用" in record["notes"][0]


def test_failed_definition_noted_only_when_used():
    defs, _ = load({"specs/tables/dbo.bad.md": "CREATE TABLE dbo.other(a int)"})
    assert augment(SPEC_NONE, ["SELECT 1 FROM acct"], defs, {}) == (SPEC_NONE, None)
    text, record = augment(SPEC_NONE, ["SELECT a FROM bad"], defs, {})
    assert text == SPEC_NONE and "表名跟檔名不同" in record["notes"][0]


def test_ambiguous_noted_when_used():
    defs, _ = load({"specs/tables/dbo.t.md": "CREATE TABLE dbo.t(a int)",
                    "specs/tables/rpt.t.md": "CREATE TABLE rpt.t(a int)"})
    text, record = augment(SPEC_NONE, ["SELECT a FROM rpt.t"], defs, {})
    assert text == SPEC_NONE and "不只一個定義檔" in record["notes"][0]


def _wide(n=TRIM_THRESHOLD + 5):
    cols = ", ".join(f"[c{i}] int" for i in range(n))
    return load({"specs/tables/dbo.wide.md": f"CREATE TABLE dbo.wide({cols})"})[0]


def test_wide_table_keeps_only_columns_named_in_sql():
    text, record = augment(SPEC_NONE, ["SELECT w.c3, COUNT(*) FROM wide w WHERE w.c10 > 1 -- c20"],
                           _wide(), {})
    (t,) = record["tables"]
    assert (t["columns"], t["kept"]) == (TRIM_THRESHOLD + 5, 3), "註解裡的名字也留(寧多不漏)"
    assert "[c3] INT, [c10] INT, [c20] INT" in text
    assert "c4" in t["dropped"] and "c3" not in t["dropped"]


@pytest.mark.parametrize("sql", ["SELECT * FROM wide WHERE c1 > 0",
                                 "SELECT DISTINCT TOP (5) * FROM wide WHERE c1 > 0",
                                 "SELECT w.*, w.c1 FROM wide w", "SELECT w . * FROM wide w WHERE w.c2 = 1"])
def test_star_disables_trimming(sql):
    """SQL 裡也出現了欄位名:沒有 * 的話會只留那幾欄,有 * 就要全部留。"""
    _, record = augment(SPEC_NONE, [sql], _wide(), {})
    assert record["tables"][0]["kept"] == TRIM_THRESHOLD + 5


def test_narrow_table_is_not_trimmed():
    _, record = augment(SPEC_NONE, ["SELECT c1 FROM wide"], _wide(TRIM_THRESHOLD), {})
    assert record["tables"][0]["kept"] == TRIM_THRESHOLD


def test_nothing_without_spec_or_defs():
    assert augment(None, [SQL], _defs(), {}) == (None, None)
    assert augment(SPEC_NONE, [SQL], {}, {}) == (SPEC_NONE, None)


# ─────────────────── 管線 ───────────────────

def test_table_versions_in_mr():
    mr = {"files": [
        {"path": "specs/tables/dbo.a.md", "full_content": "CREATE TABLE dbo.a(x int)"},
        {"path": "specs/tables/dbo.b.md", "diff": "@@ -1,2 +0,0 @@\n-a\n-b"},
        {"path": "specs/tables/dbo.c.md", "diff": "@@ -1 +1 @@\n+x"},                # 沒有完整內容
        {"path": "specs/tables/sub/dbo.d.md", "full_content": "x"},
        {"path": "specs/R-1.md", "full_content": "# R"}, "odd"]}
    assert pipeline._table_versions_in_mr(mr) == {"specs/tables/dbo.a.md": "CREATE TABLE dbo.a(x int)",
                                                  "specs/tables/dbo.b.md": ""}


def _with_index(**kw):
    spec_index.use_index(spec_index.build_index({PATH: SSMS, **kw}))


def test_with_repo_tables_only_in_dbt_mode():
    mr = {"files": [{"path": "models/P01/x.sql", "full_content": SQL}]}
    spec_index.use_index(None)
    assert pipeline._with_repo_tables(SPEC_NONE, mr) == (SPEC_NONE, None)
    _with_index()
    try:
        text, record = pipeline._with_repo_tables(SPEC_NONE, mr)
    finally:
        spec_index.use_index(None)
    assert "CREATE TABLE [txn_log]" in text and record["tables"][0]["source"] == PATH


def test_with_repo_tables_reads_diff_when_no_full_content():
    mr = {"files": [{"path": "models/P01/x.sql", "diff": "@@ -0,0 +1 @@\n+" + SQL}]}
    _with_index(**{"specs/tables/nodot.md": "CREATE TABLE nodot(a int)"})
    try:
        text, record = pipeline._with_repo_tables(SPEC_NONE, mr)
    finally:
        spec_index.use_index(None)
    assert "CREATE TABLE [txn_log]" in text and "nodot.md" in record["notes"][0]


def _sql_error_report(column):
    return {"findings": [{"file": "m.sql", "line": 0, "severity": "major",
                          "title": "執行驗證:SQL 無法在測資上執行",
                          "detail": f"執行錯誤(語法/欄位):(207, b\"Invalid column name '{column}'.\")",
                          "suggestion": "x", "citations": [], "_source": "program"}]}


@pytest.mark.parametrize("message", ["Invalid column name '{c}'", "無效的資料行名稱 '{c}'"])
def test_missing_trimmed_column_is_not_blamed_on_sql(message):
    report = _sql_error_report("c4")
    report["findings"][0]["detail"] = message.format(c="C4")
    record = {"tables": [{"table": "wide", "dropped": ["c4", "c5"]}], "notes": []}
    (f,) = pipeline.enforce_repo_tables(report, record)["findings"]
    assert f["severity"] == "major" and f["title"] == "執行驗證無法完成(測資表少了欄位)"
    assert "不是 SQL 的錯" in f["detail"]
    assert report["_repo_tables"] is record


def test_missing_column_that_was_never_defined_stays_sql_error():
    report = _sql_error_report("nope")
    record = {"tables": [{"table": "wide", "dropped": ["c4"]}], "notes": []}
    (f,) = pipeline.enforce_repo_tables(report, record)["findings"]
    assert f["title"] == "執行驗證:SQL 無法在測資上執行"


def test_model_written_finding_is_not_rewritten():
    report = _sql_error_report("c4")
    report["findings"][0]["_source"] = "model"
    record = {"tables": [{"table": "wide", "dropped": ["c4"]}], "notes": []}
    (f,) = pipeline.enforce_repo_tables(report, record)["findings"]
    assert f["title"] == "執行驗證:SQL 無法在測資上執行"


def test_notes_become_one_info_finding_and_record_is_overwritten():
    report = {"findings": [], "_repo_tables": {"tables": "模型自己寫的"}}
    pipeline.enforce_repo_tables(report, {"tables": [], "notes": ["甲", "乙"]})
    (f,) = report["findings"]
    assert (f["severity"], f["title"]) == ("info", "執行驗證的表定義(specs/tables/)")
    assert "甲;乙" in f["detail"]
    assert pipeline.enforce_repo_tables({"findings": [], "_repo_tables": 1}, None) == \
        {"findings": [], "_repo_tables": None}


def test_spec_change_finding_says_table_files_are_used():
    mr = {"files": [{"path": "specs/tables/dbo.txn_log.md", "diff": "@@ -1 +1 @@\n+x",
                     "full_content": SSMS},
                    {"path": "models/P01/x.sql", "diff": "@@ -1 +1 @@\n+x"}]}
    _with_index()
    try:
        report = pipeline.enforce_spec_change({"findings": []}, mr)
    finally:
        spec_index.use_index(None)
    detail = report["findings"][0]["detail"]
    assert "1 個表定義檔(specs/tables/)被修改" in detail and "審查沒有用到它們" not in detail
    assert "不會自動放行" in detail


def test_real_index_takes_tables_from_the_same_download(monkeypatch):
    """真實模式:表定義跟規格在同一包 specs/ 裡,不多發請求。"""
    from tests.test_spec_index import SHA, _tgz
    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", "test-write-token")
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", "test-read-token")
    calls = []

    def download(sha, path, *, max_bytes):
        calls.append(path)
        return _tgz({"specs/a.md": "# a", PATH: SSMS, "specs/tables/x.sql": "CREATE TABLE x(a int)",
                     "specs/tables/sub/dbo.y.md": "CREATE TABLE dbo.y(a int)"})
    gitlab.pin_review_ref(SHA)
    try:
        idx = spec_index.load_index({}, real_mode=True, download=download)
    finally:
        gitlab.pin_review_ref(None)
    assert calls == ["specs"] and set(idx.tables) == {PATH} and set(idx.specs) == {"a"}


def test_review_index_overlays_tables_changed_in_mr(tmp_path, monkeypatch):
    """mock 模式:這個 MR 改過、刪掉的表定義,以 MR 的版本為準。"""
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "dbo.old.md").write_text("CREATE TABLE dbo.old(a int)", encoding="utf-8")
    monkeypatch.setattr(spec_exec, "SPECS_DIR", tmp_path)
    mr = {"files": [{"path": PATH, "full_content": SSMS, "diff": "@@ -0,0 +1 @@\n+x"},
                    {"path": "specs/tables/dbo.old.md", "diff": "@@ -1 +0,0 @@\n-x"}]}
    idx = pipeline._load_spec_index(mr, {"enabled": True}, None)
    assert set(idx.tables) == {PATH}


def test_mock_index_reads_local_tables(tmp_path):
    (tmp_path / "tables").mkdir()
    (tmp_path / "tables" / "dbo.txn_log.md").write_text(SSMS, encoding="utf-8")
    (tmp_path / "tables" / "dbo.gone.md").write_text("CREATE TABLE dbo.gone(a int)", encoding="utf-8")
    idx = spec_index.load_index({}, real_mode=False, local_dir=tmp_path,
                                table_versions={"specs/tables/dbo.gone.md": ""})
    assert set(idx.tables) == {PATH}


# ─────────────────── 整條 review_mr ───────────────────

def test_review_feeds_table_definitions_to_test_generation(review, monkeypatch):
    """規格沒有資料表定義、specs/tables/ 有:測資生成拿到的規格要有那張表;報告記下來源。"""
    (spec_exec.SPECS_DIR / "tables").mkdir()
    (spec_exec.SPECS_DIR / "tables" / "dbo.transactions.md").write_text(
        "CREATE TABLE [dbo].[transactions]([account_no] [varchar](20) NOT NULL, [amount] [money])",
        encoding="utf-8")
    seen = {}

    async def capture(cfg, code, text, profile_name=None):
        seen["text"] = text
        return ({"schema_ddl": ["CREATE TABLE transactions (id INT);"], "conditions": [],
                 "cases": []}, [], [])
    monkeypatch.setattr(spec_exec, "generate_cases", capture)
    cfg = {**CFG, "project_dir": "dbt_sub"}
    files = [{"path": "dbt_sub/models/P01/x.sql", "diff": FRAGMENT, "full_content": PLAIN_SQL}]
    report, _ = review(files, _Loader(_project(PLAIN_SQL)), cfg_dbt=cfg)
    assert "CREATE TABLE [transactions] ([account_no] VARCHAR(20), [amount] MONEY);" in seen["text"]
    assert report["_repo_tables"]["tables"][0]["source"] == "specs/tables/dbo.transactions.md"
    assert "transactions" not in json.dumps(report.get("_spec_exec", {}).get("spec_code"))
