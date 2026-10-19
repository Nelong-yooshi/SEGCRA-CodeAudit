"""以增量模式展開:is_incremental() 為 True 時的分支,以及 {{ this }}。

預設照 dbt compile(is_incremental() 回 False),`{% if is_incremental() %}` 裡的 SQL 不在輸出裡,
規則層掃不到增量模型最常放刪改舊資料語句的那一段。呼叫端可以再以 incremental=True 展開一次;
RenderResult.incremental_checked 說明有沒有必要。
"""
import pytest

from orchestrator.dbt_relations import project_info_from_files
from orchestrator.dbt_render import (_RELATION_NOTICE, _RELATION_UNCERTAIN_NOTICE, render_model,
                                     render_model_isolated)

DB = "DBT_PLACEHOLDER"
MODEL = """{{ config(materialized='incremental') }}
select id, amount from {{ ref('stg_txn') }}
{% if is_incremental() %}
where id > (select max(id) from {{ this }})
{% endif %}"""


def _render(src=MODEL, path="models/mrt_daily.sql", **kw):
    return render_model(path, source=src, **{"database": DB, **kw})


# ─────────────────── 兩種模式 ───────────────────

def test_預設照compile_增量分支不在輸出裡():
    r = _render()
    assert r.ok and "max(id)" not in r.sql
    assert r.incremental_checked is True


def test_增量模式_分支展開_this是這個model自己的表():
    r = _render(incremental=True)
    assert r.ok, r.error
    assert f'where id > (select max(id) from "{DB}"."dbo"."mrt_daily")' in r.sql


def test_this的字串操作與屬性都是真的值():
    """字串內容就是表名:用濾器、串接、format 都不會安靜地變成空字串。"""
    src = ("{% if is_incremental() %}{{ this | upper }}|{{ this ~ '_x' }}|{{ this | length }}|"
           "{{ this.database }}.{{ this.schema }}.{{ this.identifier }}.{{ this.name }}.{{ this.table }}"
           "{% endif %}")
    r = _render(src, incremental=True)
    full = f'"{DB}"."dbo"."mrt_daily"'
    assert r.ok, r.error
    assert r.sql == (f"{full.upper()}|{full}_x|{len(full)}|"
                     f"{DB}.dbo.mrt_daily.mrt_daily.mrt_daily")


def test_預設模式不提供this():
    """非增量模式照舊:{{ this }} 未定義,大聲失敗(不猜值)。"""
    r = _render("select * from {{ this }}")
    assert not r.ok and "this" in r.error


def test_沒有database時_用到this才失敗():
    r = _render("{% if is_incremental() %}select * from {{ this }}{% endif %}",
                incremental=True, database=None)
    assert not r.ok and "database" in r.error
    plain = _render("{% if is_incremental() %}select 1{% endif %}", incremental=True, database=None)
    assert plain.ok and plain.sql == "select 1"


def test_model名稱不合規時增量模式失敗():
    r = _render(incremental=True, path="models/mrt-daily.sql")
    assert not r.ok


def test_this的私有屬性與內部物件拿不到():
    r = _render("{% if is_incremental() %}{{ this.__class__ }}{% endif %}", incremental=True)
    assert not r.ok


# ─────────────────── 要不要展開第二次 ───────────────────

def test_沒有呼叫is_incremental時不用展開第二次():
    r = _render("select id from {{ ref('stg_txn') }}")
    assert r.ok and r.incremental_checked is False


def test_macro裡呼叫is_incremental也算():
    files = {"macros/inc.sql": "{% macro since() %}{% if is_incremental() %}where 1=1{% endif %}"
                               "{% endmacro %}"}
    r = _render("select 1 {{ since() }}", macro_files=files)
    assert r.ok and r.incremental_checked is True
    r = _render("select 1 {{ since() }}", macro_files=files, incremental=True)
    assert r.ok and r.sql == "select 1 where 1=1"


def test_macro檔最外層呼叫的不算():
    """dbt 不執行 macro 檔最外層的程式碼;載入時呼叫到的不代表 model 用了。"""
    files = {"macros/top.sql": "{% set x = is_incremental() %}{% macro m() %}1{% endmacro %}"}
    r = _render("select {{ m() }}", macro_files=files)
    assert r.ok and r.incremental_checked is False


# ─────────────────── 表名提醒 ───────────────────

def test_沒有專案資訊時_增量模式帶表名提醒():
    r = _render("{% if is_incremental() %}select 1{% endif %}", incremental=True)
    assert r.ok and r.relation_notice == _RELATION_NOTICE


def test_專案資訊說這個model表名無法確定時_帶無法確定的提醒():
    files = {"dbt_project.yml": "name: shop\nversion: '1.0'\n",
             "models/stg_txn.sql": "select 1 as id",
             "models/mrt_daily.sql": MODEL,
             "models/props.yml": ("version: 2\nmodels:\n  - name: mrt_daily\n"
                                  "    config:\n      alias: daily_v2\n")}
    info = project_info_from_files(files, other_ref_names=(), root_files_checked=True)
    assert "mrt_daily" in info.uncertain_models
    r = _render(incremental=True, project=info)
    assert r.ok and r.relation_notice == _RELATION_UNCERTAIN_NOTICE
    plain = _render(project=info)
    assert plain.ok and plain.relation_notice is None      # 非增量模式沒有用到 this


# ─────────────────── 隔離子行程 ───────────────────

def test_隔離展開也接受增量模式():
    r = render_model_isolated("models/mrt_daily.sql", source=MODEL, database=DB, incremental=True)
    assert r.ok and "max(id)" in r.sql and r.incremental_checked is True


def test_有macro檔時_表名提醒也照樣記():
    """macro 載入完會把「用到 ref()」的紀錄歸零;增量模式的 this 要在歸零之後才記。"""
    files = {"macros/noop.sql": "{% macro noop() %}{% endmacro %}"}
    r = _render("{% if is_incremental() %}select 1{% endif %}", incremental=True, macro_files=files)
    assert r.ok and r.relation_notice == _RELATION_NOTICE
