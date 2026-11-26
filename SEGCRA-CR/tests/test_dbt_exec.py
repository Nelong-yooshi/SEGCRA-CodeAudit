"""dbt model 的執行驗證:引用 source() / ref() 的 model 展開成沙盒用的 SQL 再執行。

  * 規格「資料表定義」的表名(spec_tables)
  * 展開模式:source() 指向規格建的測資表;ref() 由同名的測資表代替(細節見 test_dbt_exec_ref);
    {{ this }}、增量 model 不支援
  * pipeline:哪個檔案要展開、什麼情況照舊回「尚未支援」、原因寫進報告、
    展開後執行通過時不再補「dbt 的變更沒有經過執行驗證」
"""
import pytest

import orchestrator.dbt_exec as dbt_exec
import orchestrator.pipeline as pipeline
from orchestrator.dbt_exec import AMBIGUOUS, render_for_exec, spec_tables
from orchestrator.dbt_render import DbtProjectInfo, render_model, render_model_isolated
from orchestrator.pipeline import (_DBT_EXEC_NOTE_TITLE, _DBT_EXEC_STAND_IN_TITLE, _DBT_OTHER_MODELS_TITLE,
                                   _DBT_UNEXECUTED_TITLE, review_incomplete,
                                   _dbt_exec_mr,
                                   enforce_dbt_exec_note, enforce_dbt_unexecuted)
from orchestrator.dbt_affected import SpecForModels
from tests.test_dbt_affected import (CFG, DB, HEAD, _Loader, prepare_dbt_context, review,
                                     spec_exec)
# 匯入就會套用:專案資訊在同一個行程整理、每個測試清快取
from tests.test_dbt_affected import _fresh_cache, _summarize_in_process  # noqa: F401

SPEC = ("# BIG 規格\n\n## 資料表定義(執行驗證以此建表)\n```sql\n"
        "CREATE TABLE transactions(account_no INT, amount DECIMAL(14,2));\n```\n\n"
        "## 需求項目\n1. 金額達 100000 通報\n")


def _render(source, project=None, tables=None, **kw):
    """在同一個行程裡展開(快);正式路徑走 render_model_isolated,另有一個測試驗它。"""
    return render_model("models/mrt_BIG.sql", source=source, database=DB,
                        project=project, sandbox_tables=tables, **kw)


INFO = DbtProjectInfo(project_name="sample", ref_names=frozenset({"mrt_BIG", "stg_a"}),
                      sources={("raw", "transactions"): (None, None),
                               ("raw", "txn_alias"): ("stage", "Transactions"),
                               ("other", "transactions"): (None, None),
                               ("raw", "templated"): (None, None)},
                      uncertain_sources=frozenset({("raw", "templated")}),
                      refs_complete=True, sources_complete=True)
TABLES = {"transactions": "[dbo].[transactions]"}


# ─────────────────── 規格的資料表定義 ───────────────────

def test_資料表定義_取出表名與寫法():
    assert spec_tables(SPEC) == {"transactions": "[dbo].[transactions]"}


def test_資料表定義_帶_schema_照寫_同名不分大小寫當成同一張():
    text = ("## 資料表定義\n```sql\nCREATE TABLE dbo.Txn(a INT);\nCREATE TABLE rpt.TXN(a INT);\n"
            "CREATE TABLE [dbo].[ok_t] (a INT);\nCREATE TABLE stage.only_one(a INT);\n```\n")
    assert spec_tables(text) == {"txn": AMBIGUOUS, "ok_t": "[dbo].[ok_t]",
                                 "only_one": "[stage].[only_one]"}


def test_沒寫_schema_與寫了_dbo_是同一張表():
    text = "## 資料表定義\n```sql\nCREATE TABLE t(a INT);\nCREATE TABLE dbo.t(b INT);\n```\n"
    assert spec_tables(text) == {"t": "[dbo].[t]"}


def test_資料表定義_同一張表寫兩次_寫法相同不算重複():
    text = "## 資料表定義\n```sql\nCREATE TABLE t(a INT);\nCREATE TABLE t(b INT);\n```\n"
    assert spec_tables(text) == {"t": "[dbo].[t]"}


@pytest.mark.parametrize("ddl", [
    "CREATE TABLE db1.dbo.t(a INT);",            # 三段式(指定資料庫)
    "CREATE TABLE [we ird](a INT);",             # 不是英數與底線
    "CREATE VIEW v AS SELECT 1 AS a;",           # 不是表
    "CREATE TABLE (((;",                         # 解析不了
])
def test_資料表定義_不收的表(ddl):
    assert spec_tables(f"## 資料表定義\n```sql\n{ddl}\n```\n") == {}


def test_資料表定義_只看那一段():
    text = ("## 說明\n```sql\nCREATE TABLE before_t(a INT);\n```\n## 資料表定義\n```sql\n"
            "CREATE TABLE mine(a INT);\n```\n### 子標題\n```sql\nCREATE TABLE sub_t(a INT);\n```\n"
            "## 需求項目\n```sql\nCREATE TABLE after_t(a INT);\n```\n")
    assert spec_tables(text) == {"mine": "[dbo].[mine]", "sub_t": "[dbo].[sub_t]"}


def test_資料表定義_沒有程式碼區塊時看整段():
    assert spec_tables("## 資料表定義\nCREATE TABLE t(a INT);\n\n## 需求\nx\n") == {"t": "[dbo].[t]"}


@pytest.mark.parametrize("text", [None, 1, "", "# 規格\n沒有資料表定義\n",
                                  "## 資料表定義\n" + "CREATE TABLE t(a INT);\n" * 5000],
                         ids=["none", "int", "empty", "no-section", "too-big"])
def test_資料表定義_找不到或太大時是空的(text):
    assert spec_tables(text) == {}


# ─────────────────── 展開模式 ───────────────────

def test_source_展開成規格的表名():
    r = _render("SELECT account_no FROM {{ source('raw', 'transactions') }}", INFO, TABLES)
    assert r.ok and r.sql == "SELECT account_no FROM [dbo].[transactions]"
    assert r.relation_notice is None


def test_source_用宣告的_identifier_不分大小寫_不看來源的_schema():
    r = _render("SELECT 1 FROM {{ source('raw', 'txn_alias') }}", INFO, TABLES)
    assert r.ok and r.sql == "SELECT 1 FROM [dbo].[transactions]"


def test_規格帶_schema_時照規格的寫法():
    r = _render("SELECT 1 FROM {{ source('raw', 'transactions') }}", INFO,
                {"transactions": "[stage].[transactions]"})
    assert r.ok and r.sql == "SELECT 1 FROM [stage].[transactions]"


def test_表名物件只給名稱():
    src = "{% set s = source('raw', 'transactions') %}{{ s.identifier }} {{ s.name }} {{ s.table }}"
    r = _render(src, INFO, TABLES)
    assert r.ok and r.sql == "transactions transactions transactions"


@pytest.mark.parametrize("src, why", [
    ("{{ source('raw', 'nope') }}", "未在 sources.yml 宣告"),
    ("{{ source('raw', 'templated') }}", "無法確定"),
    ("{{ source('raw', 'transactions') }}{{ source('other', 'transactions') }}", "同一張表"),
    ("{{ ref('stg_a') }}", "表名無法確定"),               # 沒有表名紀錄(naming)
    ("{{ ref('sample', 'stg_a') }}", "表名無法確定"),
    ("{{ this }}", "尚未支援 {{ this }}"),
    ("{{ source('raw', 'transactions').database }}", "沒有 database"),
    ("{{ source('raw', 'transactions').schema }}", "沒有 schema"),
])
def test_展開失敗的情況(src, why):
    r = _render(src, INFO, TABLES)
    assert not r.ok and why in r.error


def test_規格沒有那張表():
    r = _render("{{ source('raw', 'transactions') }}", INFO, {"other_t": "[other_t]"})
    assert not r.ok and "規格的資料表定義沒有" in r.error


def test_規格有兩張同名的表():
    r = _render("{{ source('raw', 'transactions') }}", INFO, {"transactions": AMBIGUOUS})
    assert not r.ok and "不只一張" in r.error


def test_沒有專案資訊_source_無法確定():
    r = _render("{{ source('raw', 'transactions') }}", None, TABLES)
    assert not r.ok and "沒有 dbt 專案資訊" in r.error


def test_同一個來源用兩次沒關係():
    r = _render("{{ source('raw', 'transactions') }} {{ source('raw', 'transactions') }}",
                INFO, TABLES)
    assert r.ok and r.sql == "[dbo].[transactions] [dbo].[transactions]"


def test_this_有定義_沒用到就照常展開():
    r = _render("{% if this is defined %}SELECT 1 FROM {{ source('raw', 'transactions') }}{% endif %}",
                INFO, TABLES)
    assert r.ok and r.sql == "SELECT 1 FROM [dbo].[transactions]"


def test_macro_檔最外層的_source_與_ref_不讓展開失敗():
    """dbt 不執行 macro 檔最外層的程式碼:在那裡失敗會冤枉開發者。"""
    macros = {"macros/m.sql": "{% set a = ref('stg_a') %}{% set b = source('raw', 'x') %}"
                              "{% macro pick() %}{{ source('raw', 'transactions') }}{% endmacro %}"}
    r = _render("SELECT 1 FROM {{ pick() }}", INFO, TABLES, macro_files=macros)
    assert r.ok and r.sql == "SELECT 1 FROM [dbo].[transactions]"


def test_沒給_sandbox_tables_時照正式環境的表名():
    r = _render("SELECT 1 FROM {{ source('raw', 'transactions') }}", INFO, None)
    assert r.ok and r.sql == f'SELECT 1 FROM "{DB}"."raw"."transactions"'


def test_子行程隔離的展開也收得到():
    r = render_model_isolated("models/mrt_BIG.sql", database=DB, project=INFO,
                              source="SELECT 1 FROM {{ source('raw', 'transactions') }}",
                              sandbox_tables=TABLES)
    assert r.ok and r.sql == "SELECT 1 FROM [dbo].[transactions]"


# ─────────────────── render_for_exec ───────────────────

@pytest.fixture
def in_process(monkeypatch):
    def fake(path, *, timeout_s=None, **kw):
        return render_model(path, **kw)
    monkeypatch.setattr(dbt_exec, "render_model_isolated", fake)


def _for_exec(source, spec=SPEC, project=INFO, **kw):
    return render_for_exec("models/mrt_BIG.sql", source, spec, DB,
                           {"project": project, **kw} if project else {})


def test_render_for_exec_成功(in_process):
    assert _for_exec("SELECT 1 FROM {{ source('raw', 'transactions') }}") == (
        "SELECT 1 FROM [dbo].[transactions]", None, ())


@pytest.mark.parametrize("source, spec, project, why", [
    ("{{ source('raw', 'transactions') }}", SPEC, None, "沒有 dbt 專案資訊"),
    ("SELECT {{ 1 }} AS n", SPEC, None, "沒有 dbt 專案資訊"),        # 沒用到 source() 也一樣
    ("{{ source('raw', 'transactions') }}", "# 沒有資料表定義\n", INFO, "沒有可以解析的 CREATE TABLE"),
    ("{% if is_incremental() %}x{% endif %}{{ source('raw', 'transactions') }}", SPEC, INFO,
     "增量 model"),
    ("{{ ref('stg_a') }}", SPEC, INFO, "表名無法確定"),
    ("{{ '{{' }} {{ source('raw', 'transactions') }}", SPEC, INFO, "仍含樣板標記"),
    ("{% if false %}{{ source('raw', 'transactions') }}{% endif %}", SPEC, INFO, "沒有 SQL"),
])
def test_render_for_exec_不能展開的原因(in_process, source, spec, project, why):
    sql, reason, stand_ins = _for_exec(source, spec, project)
    assert sql is None and why in reason and stand_ins == ()


def test_render_for_exec_同名_macro(in_process):
    macros = {"macros/a.sql": "{% macro m() %}1{% endmacro %}",
              "macros/b.sql": "{% macro m() %}2{% endmacro %}"}
    sql, reason, _ = _for_exec("{{ source('raw', 'transactions') }}", macro_files=macros)
    assert sql is None and "同名的 macro" in reason


# ─────────────────── pipeline:哪個檔案要展開 ───────────────────

YML = ("version: 2\nsources:\n  - name: raw\n    tables:\n      - name: transactions\n")
MODEL = "SELECT account_no FROM {{ source('raw', 'transactions') }}\n"


def _files(extra=None):
    files = {"dbt_project.yml": "name: sample\n", "models/src.yml": YML,
             "models/mrt_BIG.sql": MODEL, "macros/m.sql": "{% macro m() %}1{% endmacro %}"}
    files.update(extra or {})
    return files


def _ctx(paths, extra=None):
    mr = {"sha": HEAD, "files": [{"path": p, "diff": "@@ -1 +1 @@\n+x"} for p in paths]}
    return prepare_dbt_context(mr, CFG, real_mode=True, load=_Loader(_files(extra)))


def _exec_mr(paths, contents=None):
    """交給執行驗證的 MR(_mr_for_spec_exec 之後的形狀:樣板檔帶完整內容)。"""
    files = []
    for p in paths:
        text = (contents or {}).get(p, _files().get(p))
        files.append({"path": p, "diff": "@@ -1 +1 @@\n+x", "full_content": text})
    return {"files": files}


def test_最後一個檔案是_model_展開後換掉內容(in_process):
    mr = _exec_mr(["models/mrt_BIG.sql"])
    out, record = _dbt_exec_mr(mr, SPEC, SPEC, (), _ctx(["models/mrt_BIG.sql"]), CFG)
    assert record == {"file": "models/mrt_BIG.sql", "ok": True, "reason": None, "stand_ins": []}
    assert out["files"][0]["full_content"] == "SELECT account_no FROM [dbo].[transactions]"
    assert mr["files"][0]["full_content"] == MODEL                  # 傳進來的 MR 不動


@pytest.mark.parametrize("paths, contents, cfg", [
    (["models/mrt_BIG.sql", "sql/plain.sql"], {"sql/plain.sql": "SELECT 1"}, CFG),   # 最後一個不是 model
    (["models/mrt_BIG.sql", "macros/m.sql"], None, CFG),                              # 最後一個是 macro
    (["models/mrt_BIG.sql"], {"models/mrt_BIG.sql": "SELECT 1"}, CFG),                # 不是樣板
    (["models/mrt_BIG.sql"], None, {**CFG, "enabled": False}),                        # 沒開 dbt
    (["models/mrt_BIG.sql"], None, None),
])
def test_不適用時照舊(in_process, paths, contents, cfg):
    mr = _exec_mr(paths, contents)
    out, record = _dbt_exec_mr(mr, SPEC, SPEC, (), _ctx(paths), cfg)
    assert out is mr and record is None


def test_最後一個有內容的才算_後面沒有內容的檔案不算(in_process):
    mr = _exec_mr(["models/mrt_BIG.sql"])
    mr["files"].append({"path": "sql/empty.sql", "diff": ""})
    out, record = _dbt_exec_mr(mr, SPEC, SPEC, (), _ctx(["models/mrt_BIG.sql"]), CFG)
    assert record["ok"] and out["files"][0]["full_content"].endswith("[dbo].[transactions]")


def test_diff_裡的樣板也算(in_process):
    mr = {"files": [{"path": "models/mrt_BIG.sql", "diff": "@@ -0,0 +1 @@\n+" + MODEL}]}
    out, record = _dbt_exec_mr(mr, SPEC, SPEC, (), _ctx(["models/mrt_BIG.sql"]), CFG)
    assert record["ok"] and out["files"][0]["full_content"] == "SELECT account_no FROM [dbo].[transactions]"


@pytest.mark.parametrize("ctx", [None, "error", "seed"])
def test_沒有專案或不在_model_目錄時照舊(in_process, ctx):
    from orchestrator.dbt_context import DbtContext
    path = "seeds/mrt_BIG.sql" if ctx == "seed" else "models/mrt_BIG.sql"
    if ctx is None:
        context = None
    elif ctx == "error":
        context = DbtContext(error="x")
    else:
        context = _ctx([path], {"dbt_project.yml": "name: sample\nseed-paths: [seeds]\n", path: MODEL})
    mr = _exec_mr([path], {path: MODEL})
    out, record = _dbt_exec_mr(mr, SPEC, SPEC, (), context, CFG)
    assert out is mr and record is None


def test_沒有規格時照舊(in_process):
    mr = _exec_mr(["models/mrt_BIG.sql"])
    out, record = _dbt_exec_mr(mr, None, None, (), _ctx(["models/mrt_BIG.sql"]), CFG)
    assert out is mr and record is None


def test_借來的規格要是這個_model_的(in_process):
    mr = _exec_mr(["models/mrt_BIG.sql"])
    mine = SpecForModels("BIG", SPEC, ("models/mrt_BIG.sql",))
    other = SpecForModels("OTHER", SPEC, ("models/mrt_OTHER.sql",))
    ctx = _ctx(["models/mrt_BIG.sql"])
    out, record = _dbt_exec_mr(mr, SPEC, None, (mine, other), ctx, CFG)
    assert record["ok"]
    out, record = _dbt_exec_mr(mr, SPEC, None, (other, mine), ctx, CFG)
    assert out is mr and not record["ok"] and "不是這個 model 的" in record["reason"]
    out, record = _dbt_exec_mr(mr, SPEC, None, (), ctx, CFG)
    assert out is mr and not record["ok"]


def test_資料庫名稱不是約定的假名(in_process):
    mr = _exec_mr(["models/mrt_BIG.sql"])
    out, record = _dbt_exec_mr(mr, SPEC, SPEC, (), _ctx(["models/mrt_BIG.sql"]),
                               {**CFG, "database": "PROD_DB"})
    assert out is mr and not record["ok"] and "約定的假名" in record["reason"]


def test_展開失敗時_MR_不動_記下原因(in_process):
    mr = _exec_mr(["models/mrt_BIG.sql"], {"models/mrt_BIG.sql": "SELECT 1 FROM {{ ref('x') }}"})
    out, record = _dbt_exec_mr(mr, SPEC, SPEC, (), _ctx(["models/mrt_BIG.sql"]), CFG)
    assert out is mr and record["file"] == "models/mrt_BIG.sql" and not record["ok"]
    assert "ref() 指向專案裡不存在" in record["reason"]


# ─────────────────── 後處理 ───────────────────

def test_沒展開的原因寫成_info():
    report = enforce_dbt_exec_note({"findings": []}, {"file": "models/a.sql", "ok": False,
                                                       "reason": "規格的資料表定義沒有 ref() 指向的表:stg_a"})
    (fd,) = report["findings"]
    assert (fd["severity"], fd["title"], fd["file"]) == ("info", _DBT_EXEC_NOTE_TITLE, "models/a.sql")
    assert "規格的資料表定義沒有 ref() 指向的表:stg_a" in fd["detail"]


@pytest.mark.parametrize("record", [None, {"file": "a", "ok": True, "reason": None},
                                    {"file": "a", "ok": True, "reason": "x"},
                                    {"file": "a", "ok": False, "reason": None}])
def test_展開成功或不適用時不寫(record):
    assert enforce_dbt_exec_note({"findings": []}, record)["findings"] == []


def test_模型同標題的_finding_被程式的取代():
    decoy = {"file": "models/a.sql", "line": 0, "severity": "info", "title": _DBT_EXEC_NOTE_TITLE,
             "detail": "其實有送進沙盒", "suggestion": "", "citations": []}
    report = enforce_dbt_exec_note({"findings": [decoy]},
                                   {"file": "models/a.sql", "ok": False, "reason": "原因"})
    (fd,) = report["findings"]
    assert "其實有送進沙盒" not in fd["detail"] and "原因" in fd["detail"]


def _passed():
    return {"findings": [], "_spec_exec": {"passed": True}}


def test_執行通過的就是唯一改到的_model_不補未執行():
    mr = {"files": [{"path": "models/mrt_BIG.sql", "diff": "+" + MODEL}]}
    ctx = _ctx(["models/mrt_BIG.sql"])
    report = enforce_dbt_unexecuted(_passed(), mr, ctx, CFG, "models/mrt_BIG.sql")
    assert report["findings"] == []
    # 沒有展開執行(executed_dbt 是 None)照舊補
    (fd,) = enforce_dbt_unexecuted(_passed(), mr, ctx, CFG)["findings"]
    assert fd["title"] == _DBT_UNEXECUTED_TITLE


@pytest.mark.parametrize("paths, extra_file", [
    (["models/mrt_BIG.sql", "macros/m.sql"], None),                  # 也改了 macro(有反查結果)
    (["models/mrt_BIG.sql", "models/src.yml"], None),                # 也改了屬性檔
    (["models/mrt_BIG.sql"], {"path": "models/other.sql", "diff": "+{{ x }}"}),   # 另一個樣板
])
def test_還改了其他_dbt_的東西時照舊補(paths, extra_file):
    files = [{"path": p, "diff": "+x"} for p in paths] + ([extra_file] if extra_file else [])
    mr = {"files": files}
    ctx = _ctx(paths)
    (fd,) = enforce_dbt_unexecuted(_passed(), mr, ctx, CFG, "models/mrt_BIG.sql")["findings"]
    assert fd["title"] == _DBT_UNEXECUTED_TITLE


def test_其他檔案是純_SQL_沒關係():
    mr = {"files": [{"path": "models/mrt_BIG.sql", "diff": "+" + MODEL},
                    {"path": "sql/plain.sql", "full_content": "SELECT 1"}]}
    ctx = _ctx(["models/mrt_BIG.sql"])
    assert enforce_dbt_unexecuted(_passed(), mr, ctx, CFG, "models/mrt_BIG.sql")["findings"] == []


@pytest.mark.parametrize("ctx", [None, "error"])
def test_沒有專案時照舊補(ctx):
    from orchestrator.dbt_context import DbtContext
    mr = {"files": [{"path": "models/mrt_BIG.sql", "diff": "+" + MODEL}]}
    context = DbtContext(error="x") if ctx else None
    (fd,) = enforce_dbt_unexecuted(_passed(), mr, context, CFG, "models/mrt_BIG.sql")["findings"]
    assert fd["title"] == _DBT_UNEXECUTED_TITLE


# ─────────────────── 整條 review_mr ───────────────────

@pytest.fixture
def run(review, monkeypatch):
    """review 的替身之外:規格換成有資料表定義的,記下送進沙盒的 SQL。"""
    (spec_exec.SPECS_DIR / "BIG.md").write_text(SPEC, encoding="utf-8")
    seen = {"sql": []}
    real_execute = spec_exec.execute_cases

    def capture(sql, plan_, deadline=None):
        seen["sql"].append(sql)
        return real_execute(sql, plan_)
    monkeypatch.setattr(spec_exec, "execute_cases", capture)

    def go(paths, extra=None, contents=None):
        files = [{"path": p, "diff": "@@ -0,0 +1 @@\n+" + (contents or {}).get(p, _files(extra).get(p, "x"))}
                 for p in paths]
        report, _ = review(files, _Loader(_files(extra)))
        return report, seen["sql"]
    return go


def _titles(report):
    return {f["title"] for f in report["findings"]}


def test_只引用_source_的_model_送進沙盒執行(run):
    report, sqls = run(["models/mrt_BIG.sql"])
    assert sqls == ["SELECT account_no FROM [dbo].[transactions]"]
    assert report["_spec_exec"]["passed"] is True
    assert report["_spec_exec"]["executed_file"] == "models/mrt_BIG.sql"
    assert report["_dbt_exec"] == {"file": "models/mrt_BIG.sql", "ok": True, "reason": None,
                                   "stand_ins": []}
    assert _DBT_UNEXECUTED_TITLE not in _titles(report)
    assert not any("尚未支援" in t for t in _titles(report))


REF_FILES = {"models/stg_a.sql": "SELECT 1 AS account_no",
             "models/mrt_BIG.sql": "SELECT account_no FROM {{ ref('stg_a') }}\n"}
# 規格多一張跟上游 model 同名的表:由它代替 stg_a 的輸出
SPEC_STG = SPEC.replace("CREATE TABLE transactions(", "CREATE TABLE stg_a(account_no INT);\n"
                        "CREATE TABLE transactions(")


def test_ref_指向的表規格裡沒有時_照舊交人工_並寫出原因(run):
    report, sqls = run(["models/mrt_BIG.sql"], REF_FILES)
    assert sqls == []
    assert report["_spec_exec"].get("dbt_template") is True
    note = next(f for f in report["findings"] if f["title"] == _DBT_EXEC_NOTE_TITLE)
    assert "規格的資料表定義沒有 ref() 指向的表:stg_a" in note["detail"]
    assert report["decision"] == "needs_human"


def test_ref_由規格裡同名的表代替_記下代替了哪些(in_process):
    mr = _exec_mr(["models/mrt_BIG.sql"], REF_FILES)
    out, record = _dbt_exec_mr(mr, SPEC_STG, SPEC_STG, (), _ctx(["models/mrt_BIG.sql"], REF_FILES), CFG)
    assert record == {"file": "models/mrt_BIG.sql", "ok": True, "reason": None, "stand_ins": ["stg_a"]}
    assert out["files"][0]["full_content"] == "SELECT account_no FROM [dbo].[stg_a]"


def test_ref_由測資代替時送進沙盒_並寫出上游的邏輯沒有執行(run):
    (spec_exec.SPECS_DIR / "BIG.md").write_text(SPEC_STG, encoding="utf-8")
    report, sqls = run(["models/mrt_BIG.sql"], REF_FILES)
    assert sqls == ["SELECT account_no FROM [dbo].[stg_a]"]
    assert report["_dbt_exec"]["stand_ins"] == ["stg_a"]
    note = next(f for f in report["findings"] if f["title"] == _DBT_EXEC_STAND_IN_TITLE)
    assert note["severity"] == "info" and "stg_a" in note["detail"]
    assert "沒有在這次執行" in note["detail"]
    assert _DBT_EXEC_NOTE_TITLE not in _titles(report)
    assert _DBT_UNEXECUTED_TITLE not in _titles(report)       # 上游沒改:通過的就是這個 model


def _passed_with(stand_ins, file="models/mrt_BIG.sql", ok=True, passed=True):
    return {"findings": [], "_spec_exec": {"passed": passed},
            "_dbt_exec": {"file": file, "ok": ok, "reason": None, "stand_ins": stand_ins}}


REF_MR = {"files": [{"path": p, "diff": "@@ -1 +1 @@\n+x", "full_content": REF_FILES[p]}
                    for p in ("models/stg_a.sql", "models/mrt_BIG.sql")]}


def test_改了由測資代替的上游_不管有沒有樣板都補未執行():
    ctx = _ctx(["models/stg_a.sql", "models/mrt_BIG.sql"], REF_FILES)
    (fd,) = enforce_dbt_unexecuted(_passed_with(["stg_a"]), REF_MR, ctx, CFG,
                                   "models/mrt_BIG.sql")["findings"]
    assert (fd["title"], fd["stage"]) == (_DBT_OTHER_MODELS_TITLE, "執行驗證")      # 待驗證
    assert "stg_a" in fd["detail"] and "由規格的測資代替的上游" in fd["detail"]


def test_上游是樣板時也寫出是哪個上游():
    files = dict(REF_FILES, **{"models/stg_a.sql": "SELECT account_no FROM {{ source('raw', 'transactions') }}"})
    mr = {"files": [{"path": p, "diff": "@@ -1 +1 @@\n+x", "full_content": files[p]}
                    for p in ("models/stg_a.sql", "models/mrt_BIG.sql")]}
    (fd,) = enforce_dbt_unexecuted(_passed_with(["stg_a"]), mr, _ctx(list(files), files), CFG,
                                   "models/mrt_BIG.sql")["findings"]
    assert "stg_a" in fd["detail"]


@pytest.mark.parametrize("report, executed", [
    (_passed_with(["stg_a"]), None),                                  # 跑的不是展開的 model
    (_passed_with(["stg_a"], file="models/other.sql"), "models/mrt_BIG.sql"),
    (_passed_with(["stg_a"], passed=False), "models/mrt_BIG.sql"),    # 執行驗證沒通過:它自己會說
    (_passed_with([]), "models/mrt_BIG.sql"),                         # 沒有代替
    (_passed_with(["stg_b"]), "models/mrt_BIG.sql"),                  # 代替的不是 MR 改到的
])
def test_上游的改動沒被執行_其他情況不補(report, executed):
    """不補「上游由測資代替」那一種;其餘照既有的判斷(例如跑的不是展開的 model 時照舊補一般的)。"""
    ctx = _ctx(["models/stg_a.sql", "models/mrt_BIG.sql"], REF_FILES)
    out = enforce_dbt_unexecuted(report, REF_MR, ctx, CFG, executed)
    assert not any("上游 model" in f["detail"] for f in out["findings"])


@pytest.mark.parametrize("path", [
    "sql/stg_a.sql",            # 不屬於 dbt 專案
    "analyses/stg_a.sql",       # dbt 會讀、但不是 model 的目錄
    "macros/stg_a.sql",
])
def test_同名但不在_model_目錄的檔案不算上游(path):
    files = dict(REF_FILES, **{path: "SELECT 1"})
    mr = {"files": [{"path": p, "diff": "@@ -1 +1 @@\n+x", "full_content": files[p]}
                    for p in (path, "models/mrt_BIG.sql")]}
    out = enforce_dbt_unexecuted(_passed_with(["stg_a"]), mr, _ctx([path, "models/mrt_BIG.sql"], files),
                                 CFG, "models/mrt_BIG.sql")
    assert not any("上游 model" in f["detail"] for f in out["findings"])


def test_由測資代替的_info():
    report = enforce_dbt_exec_note({"findings": []}, {"file": "models/a.sql", "ok": True,
                                                       "reason": None, "stand_ins": ["stg_a", "stg_b"]})
    (fd,) = report["findings"]
    assert (fd["severity"], fd["title"], fd["file"]) == ("info", _DBT_EXEC_STAND_IN_TITLE, "models/a.sql")
    assert "stg_a、stg_b" in fd["detail"]


def test_由測資代替的_info_最多列十個_不是字串的不列():
    names = [f"m{i}" for i in range(12)] + [None, 3]
    report = enforce_dbt_exec_note({"findings": []}, {"file": "a", "ok": True, "reason": None,
                                                       "stand_ins": names})
    detail = report["findings"][0]["detail"]
    assert "m9" in detail and "m10" not in detail and "共 12 個" in detail
    assert "None" not in detail


def test_模型同標題的代替說明_被程式的取代():
    decoy = {"file": "models/a.sql", "line": 0, "severity": "info", "title": _DBT_EXEC_STAND_IN_TITLE,
             "detail": "上游也驗過了", "suggestion": "", "citations": []}
    report = enforce_dbt_exec_note({"findings": [decoy]}, {"file": "models/a.sql", "ok": True,
                                                            "reason": None, "stand_ins": ["stg_a"]})
    (fd,) = report["findings"]
    assert "上游也驗過了" not in fd["detail"] and "stg_a" in fd["detail"]


def test_同時改了上游_model_時_通過也要補未執行(run):
    """上游 model 由測資代替,它自己的改動沒有被執行:MR 改到 dbt 的不只這個 model。"""
    (spec_exec.SPECS_DIR / "BIG.md").write_text(SPEC_STG, encoding="utf-8")
    report, sqls = run(["models/stg_a.sql", "models/mrt_BIG.sql"], REF_FILES)
    assert sqls == ["SELECT account_no FROM [dbo].[stg_a]"]
    assert _DBT_OTHER_MODELS_TITLE in _titles(report)
    assert _DBT_EXEC_STAND_IN_TITLE in _titles(report)
    assert report["decision"] == "needs_human" and _DBT_OTHER_MODELS_TITLE in review_incomplete(report)
    assert report["verdict"] != "approve"


@pytest.mark.parametrize("other", [
    "models/stg_x.sql",          # stg_a 引用它(間接的上游)
    "models/other_plain.sql",    # 跟這次無關
])
def test_改到的其他純_SQL_model_也記成待驗證_不會自動放行(run, other):
    """執行驗證只跑一個 model:MR 改到的其他 model 都沒有被執行,不管有沒有樣板。"""
    (spec_exec.SPECS_DIR / "BIG.md").write_text(SPEC_STG, encoding="utf-8")
    files = {**REF_FILES, other: "SELECT account_no FROM raw_table"}
    report, sqls = run([other, "models/mrt_BIG.sql"], files)
    assert sqls == ["SELECT account_no FROM [dbo].[stg_a]"]
    (fd,) = [f for f in report["findings"] if f["title"] == _DBT_OTHER_MODELS_TITLE]
    assert fd["stage"] == "執行驗證" and other.rsplit("/", 1)[-1][:-4] in fd["detail"]
    assert "其他 model" in fd["detail"]
    assert report["decision"] != "auto_approved" and report["verdict"] != "approve"


def test_同時改了_macro_時_通過也要補未執行(run):
    report, sqls = run(["macros/m.sql", "models/mrt_BIG.sql"])
    assert sqls == ["SELECT account_no FROM [dbo].[transactions]"]
    (fd,) = [f for f in report["findings"] if f["title"] == _DBT_UNEXECUTED_TITLE]
    assert fd["stage"] == "執行驗證"                    # 沒跑到:待驗證
    assert report["decision"] == "needs_human"


def test_模型自己輸出的_dbt_exec_被蓋掉(run, monkeypatch):
    import json

    async def fake_run_agent(cfg, profile, system, user, hub, **kw):
        return json.dumps({"summary": "x", "findings": [],
                           "_dbt_exec": {"file": "models/mrt_BIG.sql", "ok": True}})
    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    report, _ = run(["models/mrt_BIG.sql"],
                    {"models/mrt_BIG.sql": "SELECT 1 FROM {{ ref('stg_a') }}\n",
                     "models/stg_a.sql": "SELECT 1"})
    assert report["_dbt_exec"]["ok"] is False


# ─────────────────── golden set 的 dbt model(mr_413) ───────────────────

def test_golden_的_dbt_model_送進沙盒的是跟_mr_406_相同的_SQL(in_process, monkeypatch):
    """mr_413 跟 mr_406 是同一份正確的 R-140 SQL,只把表名換成 source():照 golden set 的專案
    (mock 模式從 eval/golden/dbt_archives/ 取回)展開後,送進沙盒的 SQL 要跟 mr_406 只差在表名
    換成規格「資料表定義」的寫法。沙盒的結果由評測(凍結測資)驗,這裡不連資料庫。"""
    import json

    from orchestrator.config import PKG_ROOT
    from toolbox import gitlab
    golden = PKG_ROOT / "eval" / "golden"
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", golden)
    mr = json.loads((golden / "mr_413.json").read_text(encoding="utf-8"))
    plain = json.loads((golden / "mr_406.json").read_text(encoding="utf-8"))["files"][0]["full_content"]
    spec = (PKG_ROOT / "specs" / "R-140.md").read_text(encoding="utf-8")
    cfg = {"enabled": True, "database": DB}
    ctx = prepare_dbt_context(mr, cfg, real_mode=False)
    assert ctx.error is None and ctx.project is not None
    exec_mr = pipeline._mr_for_spec_exec(mr, ctx)
    out, record = _dbt_exec_mr(exec_mr, spec, spec, (), ctx, cfg)
    assert record == {"file": "models/mrt_cash_inbound.sql", "ok": True, "reason": None,
                      "stand_ins": []}
    assert out["files"][-1]["full_content"] == plain.replace("FROM txn_log t", "FROM [dbo].[txn_log] t")


# ─────────────────── 上游由測資代替時,不符不判成 SQL 錯 ───────────────────

def _mismatch_run(monkeypatch, stand_ins):
    """上游 stg_a 過濾金額、mrt_BIG 只 SELECT:測資直接放進 [stg_a] 的「不應命中」資料列被原樣輸出,
    案例不符;仲裁看不到上游,判 sql。"""
    import asyncio
    from orchestrator.config import load_config
    plan = {"schema_ddl": ["CREATE TABLE stg_a (account_no INT);"],
            "conditions": [{"id": "C1", "desc": "金額達門檻"}],
            "cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
                       "rows": [["stg_a", 1]], "expect_flagged": True},
                      {"case_id": "C1-F", "condition_id": "C1", "direction": "false",
                       "rows": [["stg_a", 2]], "expect_flagged": False}]}

    async def generate(cfg, code, text, profile_name=None):
        return plan, [], []

    def execute(sql, plan_, deadline=None):
        mm = {"case_id": "C1-F", "condition_id": "C1", "direction": "false", "note": "金額未達",
              "expect_flagged": False, "actual_flagged": True}
        return {"engine": "stub", "sandbox_error": None, "testdata_error": None, "sql_error": None,
                "case_results": [{"case_id": "C1-T"}, {"case_id": "C1-F"}], "mismatches": [mm]}

    async def arbitrate(cfg, spec_text, sql, mm, profile_name=None):
        return {"who_is_wrong": "sql", "reason": "實作沒有過濾金額"}

    monkeypatch.setattr(spec_exec, "generate_cases", generate)
    monkeypatch.setattr(spec_exec, "execute_cases", execute)
    monkeypatch.setattr(spec_exec, "arbitrate", arbitrate)
    mr = {"files": [{"path": "models/mrt_BIG.sql", "full_content": "SELECT account_no FROM [dbo].[stg_a]"}]}
    return asyncio.run(spec_exec.run_spec_exec(load_config(), None, mr, "BIG", SPEC_STG,
                                               stand_ins=stand_ins))


def test_上游由測資代替時_不符記成待驗證_不說實作與規格不符(monkeypatch):
    result = _mismatch_run(monkeypatch, ("stg_a",))
    titles = {f["title"] for f in result["findings"]}
    assert "執行驗證失敗:實作與規格行為不符" not in titles
    (fd,) = [f for f in result["findings"] if f["title"] == spec_exec.STAND_IN_MISMATCH_TITLE]
    assert fd["stage"] == "執行驗證" and "stg_a" in fd["detail"]
    assert result["passed"] is False


def test_沒有代替時_不符照舊是實作與規格不符(monkeypatch):
    result = _mismatch_run(monkeypatch, ())
    (fd,) = [f for f in result["findings"] if f["title"] == "執行驗證失敗:實作與規格行為不符"]
    assert "stage" not in fd


def test_ref_指向的表規格裡沒有時_記成待驗證(run):
    report, sqls = run(["models/mrt_BIG.sql"], REF_FILES)
    (fd,) = [f for f in report["findings"] if f["title"] == spec_exec.DBT_NOT_RENDERED_TITLE]
    assert fd["stage"] == "執行驗證" and fd["title"] in review_incomplete(report)
    assert report["verdict"] != "approve" and report["decision"] != "auto_approved"


# ─────────────────── 名叫 ref 的 source 跟 ref() 分開 ───────────────────

def test_名叫_ref_的_source_跟_ref_對到同一張表時展開失敗(in_process):
    from orchestrator.dbt_render import render_model
    info = DbtProjectInfo(project_name="sample", ref_names=frozenset({"mrt_BIG", "stg_a"}),
                          sources={("ref", "stg_a"): (None, None), ("raw", "stg_a"): (None, None)},
                          naming={"stg_a": {}})
    tables = {"stg_a": "[dbo].[stg_a]"}
    for other in ("source('ref', 'stg_a')", "source('raw', 'stg_a')"):
        r = render_model("models/mrt_BIG.sql", database=DB, project=info, sandbox_tables=tables,
                         source=f"SELECT 1 FROM {{{{ {other} }}}} JOIN {{{{ ref('stg_a') }}}}")
        assert not r.ok and "都對到規格的同一張表" in r.error, (other, r.sql)


# ─────────────────── 同名的 CTE 不會蓋掉測資表 ───────────────────

@pytest.mark.parametrize("model", [
    "WITH stg_a AS (SELECT * FROM {{ ref('stg_a') }})\nSELECT account_no FROM stg_a",
    "WITH stg_a AS (SELECT 0 AS x WHERE 1=0),\nb AS (SELECT * FROM {{ ref('stg_a') }}) SELECT * FROM b",
    "WITH transactions AS (SELECT * FROM {{ source('raw', 'transactions') }})\nSELECT * FROM transactions",
])
def test_展開成兩段式_不會被同名的_CTE_蓋掉(in_process, model):
    """單段的 [stg_a] 在 CTE 裡會解成 CTE 自己(遞迴 CTE 錯誤 252),或讀到前面同名的 CTE;
    [dbo].[stg_a] 只會是表。"""
    info = DbtProjectInfo(project_name="sample", ref_names=frozenset({"mrt_BIG", "stg_a"}),
                          sources={("raw", "transactions"): (None, None)}, naming={"stg_a": {}},
                          refs_complete=True, sources_complete=True)
    sql, reason, _ = _for_exec(model, spec=SPEC_STG, project=info)
    assert reason is None, reason
    assert "FROM [dbo].[" in sql and "FROM [stg_a]" not in sql and "FROM [transactions]" not in sql


def test_代替了哪些交給執行驗證(run, monkeypatch):
    """執行驗證要知道哪些上游由測資代替,不符時才不會說成 SQL 錯。"""
    seen = {}
    real = pipeline.run_spec_exec

    async def spy(*a, **kw):
        seen["stand_ins"] = kw.get("stand_ins")
        return await real(*a, **kw)
    monkeypatch.setattr(pipeline, "run_spec_exec", spy)
    (spec_exec.SPECS_DIR / "BIG.md").write_text(SPEC_STG, encoding="utf-8")
    run(["models/mrt_BIG.sql"], REF_FILES)
    assert seen["stand_ins"] == ("stg_a",)
