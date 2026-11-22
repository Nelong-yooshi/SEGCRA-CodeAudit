"""dbt 的 run_started_at——不呼叫 LLM、不連資料庫。

dbt-core 1.12:`run_started_at` 是這次執行開始的時間,帶 UTC 時區的 datetime
(`tracking.User.run_started_at = datetime.now(tz=pytz.utc)`)。用到它的 model 原本展開失敗。

  1. 型別與格式跟 dbt 一樣:`.strftime()`、`.date()`、`.year` 可用,直接輸出是
     `YYYY-MM-DD HH:MM:SS.ffffff+00:00`
  2. 一份審查共用一個值(一般、增量兩次展開與插記號那次的原文才會相同);用真實時間,
     不是固定值(固定值的話樣板分得出自己在被審查)
  3. 呼叫端給的值要帶 UTC 時區;macro 不能叫這個名字;時間值只放行結果跟 dbt 相同的屬性與方法
     (時區物件是 pytz 的 UTC,跟這裡不同),其他一律展開失敗;看主機時區或現在時間的寫法也是
  4. Jinja 標記裡用到 → H008 提醒(用 Jinja 的 lexer 找,字串裡的不算);SQL 裡同名的欄位不算
  5. 執行驗證不用它展開的 SQL:用到時不送沙盒
"""
from __future__ import annotations

import asyncio
import contextvars
import datetime
import re

import pytest

from orchestrator import dbt_hidden_writes as hw
from orchestrator import dbt_render, pipeline
from orchestrator.config import DBT_DATABASE_PLACEHOLDER
from orchestrator.dbt_render import render_model, render_model_isolated, start_review_invocation
from toolbox.sqltools import run_rules

DB = DBT_DATABASE_PLACEHOLDER
_DBT_STR = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}\+00:00")


def _render(src, **kwargs):
    return render_model("models/m.sql", source=src, **kwargs)


def _value(src="{{ run_started_at }}", **kwargs) -> str:
    r = _render(src, **kwargs)
    assert r.ok, r.error
    return r.sql


# ─────────────────── 1. 型別與格式 ───────────────────

def test_直接輸出的格式跟dbt一樣():
    assert _DBT_STR.fullmatch(_value())


@pytest.mark.parametrize("src,pattern", [
    ("{{ run_started_at.strftime('%Y-%m-%d') }}", r"\d{4}-\d{2}-\d{2}"),
    ("{{ run_started_at.date() }}", r"\d{4}-\d{2}-\d{2}"),
    ("{{ run_started_at.year }}", r"\d{4}"),
    ("{{ run_started_at.tzname() }}", r"UTC"),
    ("{{ run_started_at - run_started_at }}", r"0:00:00"),
    ("{{ run_started_at.isoformat() }}", r"\d{4}-\d{2}-\d{2}T[\d:.]+\+00:00"),
])
def test_常見的用法(src, pattern):
    assert re.fullmatch(pattern, _value(src)), src


def test_是真實的時間_不是固定值():
    before = datetime.datetime.now(datetime.timezone.utc)
    got = datetime.datetime.fromisoformat(_value("{{ run_started_at.isoformat() }}"))
    after = datetime.datetime.now(datetime.timezone.utc)
    assert before <= got <= after


# ─────────────────── 2. 一份審查共用一個值 ───────────────────

def _in_review(fn):
    """在一份審查裡跑(另一份 context,不把這份審查的值留給後面的測試)。"""
    def run():
        start_review_invocation()
        return fn()
    return contextvars.copy_context().run(run)


def test_同一份審查裡的展開共用一個值():
    def work():
        a = render_model_isolated("models/m.sql", source="{{ run_started_at }}")
        b = render_model_isolated("models/m.sql", source="{{ run_started_at }}", incremental=True)
        return a, b, dbt_render._REVIEW_RUN_STARTED_AT.get()
    a, b, started = _in_review(work)
    assert a.ok and b.ok, (a.error, b.error)
    assert a.sql == b.sql == datetime.datetime.fromisoformat(started).strftime(
        "%Y-%m-%d %H:%M:%S.%f+00:00")
    assert dbt_render._REVIEW_RUN_STARTED_AT.get() is None      # 沒有留給後面的測試


def test_呼叫端指定的值照用():
    assert _value(run_started_at="2026-01-02T03:04:05.000006+00:00") == \
        "2026-01-02 03:04:05.000006+00:00"


def test_同一份審查裡用到它的樣板_插記號那次不會被當成輸出不同():
    """插記號那次跟第一次的展開時間不同的話,輸出會差在時間上,被誤報成「無法確認」。
    走真正的子行程展開(審查的值由 render_model_isolated 帶進去)。"""
    src = "SELECT '{{ run_started_at }}' AS t;\nDELETE FROM t WHERE id = 1"
    def work():
        clean = render_model_isolated("models/m.sql", source=src, database=DB)
        assert clean.ok, clean.error

        def render(marked, inc):
            return render_model_isolated("models/m.sql", source=marked, database=DB,
                                         incremental=inc)
        return hw.find_hidden_writes(src, {False: clean.sql}, render)
    assert _in_review(work) == {}


# ─────────────────── 3. 呼叫端的值、輸出、保留字 ───────────────────

@pytest.mark.parametrize("value", [
    "2026-01-02T03:04:05",               # 沒有時區
    "2026-01-02T03:04:05+08:00",         # 不是 UTC
    "not a time",
    "",
])
def test_呼叫端給的值要帶UTC時區(value):
    r = _render("{{ run_started_at }}", run_started_at=value)
    assert not r.ok and "run_started_at" in r.error


def _fails(src, **kwargs):
    r = _render(src, **kwargs)
    assert not r.ok and "沒有照實模擬" in r.error, (src, r.sql if r.ok else r.error)


# dbt 的時區物件是 pytz 的 UTC,這裡是 datetime.timezone.utc:下面每一列 dbt 跟這裡的結果不同
# (dbt 那欄是 review 照 datetime.now(tz=pytz.utc) 實測),所以一律展開失敗,不能展開成功卻跟 dbt 不同
@pytest.mark.parametrize("src", [
    "{% if run_started_at.tzinfo.zone is defined %}DELETE FROM t WHERE id = 1{% endif %}SELECT 1",  # dbt 進
    "{% if run_started_at.tzinfo.utc is defined %}DELETE FROM t WHERE id = 1{% endif %}SELECT 1",   # dbt 不進
    "{{ run_started_at.dst() }}",                                 # dbt 0:00:00
    "{{ run_started_at.timetuple().tm_isdst }}",                  # dbt 0
    "{{ run_started_at | pprint }}",                              # dbt tzinfo=<UTC>
    "{{ [run_started_at] | pprint }}",
    "{{ run_started_at.tzinfo }}",
    "{{ run_started_at['tzinfo'] }}",
    "{{ run_started_at | attr('tzinfo') }}",
    "{% if run_started_at.foo is defined %}x{% endif %}",         # 沒有照實模擬的屬性:is defined 也不行
])
def test_跟dbt結果不同的寫法_展開失敗(src):
    _fails(src)


# 從 run_started_at 拿得到的 min / max / date() 是一般的 datetime / date:它們的 now() 這類方法
# 給的是審查機的現在時間,每次展開都不同(dbt 給的是部署端主機的值,審查機算不出來)
@pytest.mark.parametrize("src", [
    "{{ run_started_at.min.now() }}",
    "{{ run_started_at.max.now() }}",
    "{{ run_started_at.min.today() }}",
    "{{ run_started_at.min.utcnow() }}",
    "{{ run_started_at.date().today() }}",
    "{{ run_started_at.min.fromtimestamp(0) }}",
    "{{ run_started_at.min.replace(tzinfo=run_started_at.tzinfo).astimezone() }}",
    "{% set t = run_started_at.min %}{{ t.now() }}",
    "{{ [run_started_at] | map(attribute='min') | list }}",
    "{{ run_started_at.astimezone() }}",                          # 換成主機的時區
    "{{ run_started_at.now() }}",
    "{{ run_started_at.today() }}",
    "{{ run_started_at.utcnow() }}",
    "{{ run_started_at.fromtimestamp(0) }}",
    "{{ (run_started_at + (run_started_at - run_started_at)).astimezone() }}",   # 先運算再換
    "{{ run_started_at.replace(year=2030).astimezone() }}",
    "{{ (run_started_at - run_started_at).min }}",
    "{{ run_started_at.time().min }}",
])
def test_看主機時區或現在時間的寫法_展開失敗(src):
    _fails(src)
    _fails(src, run_started_at="2026-01-02T03:04:05+00:00")     # 一份審查裡指定了值也一樣


def test_每次展開都不同的寫法_展開失敗而不是記成無法確認():
    """`run_started_at.min.now()` 每次展開都不同:插記號那次跟第一次比對時會說「樣板會因為內容改變輸出」。
    現在它直接展開失敗(交人工);只用 run_started_at 本身的,一份審查裡兩次展開的結果相同。"""
    _fails("SELECT '{{ run_started_at.min.now() }}' AS t;\nDELETE FROM t WHERE id = 1")
    ctx = contextvars.copy_context()

    def twice():
        start_review_invocation()
        src = "SELECT '{{ run_started_at }}' AS t;\nDELETE FROM t WHERE id = 1"
        return [render_model_isolated("models/m.sql", source=src) for _ in range(2)]
    a, b = ctx.run(twice)
    assert a.ok and b.ok and a.sql == b.sql


@pytest.mark.parametrize("src, pattern", [
    ("{{ run_started_at.month }}/{{ run_started_at.day }} {{ run_started_at.hour }}:{{ run_started_at.minute }}",
     r"\d{1,2}/\d{1,2} \d{1,2}:\d{1,2}"),
    ("{{ run_started_at.weekday() }}{{ run_started_at.isoweekday() }}", r"\d{2}"),
    ("{{ run_started_at.date().isoformat() }}", r"\d{4}-\d{2}-\d{2}"),
    ("{{ run_started_at.replace(day=1).strftime('%Y-%m-%d') }}", r"\d{4}-\d{2}-01"),
    ("{{ run_started_at.utcoffset() }}", r"0:00:00"),
    ("{{ (run_started_at - run_started_at).total_seconds() }}", r"0\.0"),
    ("{{ (run_started_at - run_started_at).days }}", r"0"),
    ("{{ run_started_at.time().hour }}", r"\d{1,2}"),
    ("{{ run_started_at.timestamp() | int > 0 }}", r"True"),
])
def test_照實模擬的屬性與方法照常可用(src, pattern):
    assert re.fullmatch(pattern, _value(src)), src


def test_macro不能叫這個名字(tmp_path):
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "m.sql").write_text(
        "{% macro run_started_at() %}x{% endmacro %}", encoding="utf-8")
    r = render_model("models/m.sql", code_root=tmp_path, source="SELECT 1")
    assert not r.ok and "run_started_at" in r.error


# ─────────────────── 4. H008 提醒 ───────────────────

def _rules(sql) -> dict:
    import json
    out = json.loads(run_rules(sql))
    return {h["rule"]: h["message"] for h in (out["hits"] if isinstance(out, dict) else out)}


@pytest.mark.parametrize("src", [
    "{% if run_started_at.year >= 2027 %}DELETE FROM t{% endif %}SELECT 1",
    "SELECT 1 WHERE '{{ run_started_at.strftime('%Y') }}' > '2027'",
    "{%- set y = run_started_at.year -%}SELECT {{ y }}",
])
def test_Jinja裡用到run_started_at給H008(src):
    rules = _rules(src)
    assert "H008" in rules and "run_started_at" in rules["H008"], rules
    assert "{%" not in rules["H008"] and "{{" not in rules["H008"]    # 只印關鍵字,不印整段 Jinja


@pytest.mark.parametrize("src", [
    "{% if " + "x == 1 and " * 30 + "run_started_at.year > 2026 %}DELETE FROM t{% endif %}SELECT 1",
    "{% if {'a': 1}['a'] == 1 and run_started_at.year > 2026 %}DELETE FROM t{% endif %}SELECT 1",
    "{% if run_started_at.year > 2026 %}SELECT 1",                 # 樣板 lex 得過、解析不過也算
])
def test_長條件或中間有右大括號也找得到(src):
    """用 Jinja 的 lexer 逐個標記找:條件多長、中間有沒有 `}` 都找得到。"""
    assert "run_started_at" in _rules(src).get("H008", "")


@pytest.mark.parametrize("src", [
    "{% if " + "x == 1 and " * 30 + "target.name == 'prod' %}DELETE FROM t{% endif %}SELECT 1",
    "{% if {'a': 1}['a'] == 1 and target.name == 'prod' %}DELETE FROM t{% endif %}SELECT 1",
])
def test_target_也一樣(src):
    assert "target.name" in _rules(src).get("H008", "")


@pytest.mark.parametrize("src", [
    "{{ config(unique_key='run_started_at') }}SELECT run_started_at FROM job_log",   # Jinja 字串裡的欄位名
    "{{ config(alias='target.name') }}SELECT 1",
    "{{ x.run_started_at }}SELECT 1",                              # 別的物件的屬性
    "{{ foo.target.name }}SELECT 1",
])
def test_字串裡或別的物件的屬性不算(src):
    assert "H008" not in _rules(src)


def test_lex_不過的樣板_出現在_Jinja_開頭之後就照樣給():
    src = "{{ 'a }}\n{% if run_started_at.year > 2026 %}x"           # 字串沒有結尾:lex 不過
    assert "run_started_at" in _rules(src).get("H008", "")


def test_run_started_at_的提醒寫出要確認跟日期有關的行為():
    msg = _rules("{% if run_started_at.year >= 2027 %}DELETE FROM t{% endif %}SELECT 1")["H008"]
    assert "日期" in msg and "分流" not in msg


def test_target_的提醒照舊寫分流():
    msg = _rules("{% if target.name == 'prod' %}DELETE FROM t{% endif %}SELECT 1")["H008"]
    assert "分流" in msg and "run_started_at" not in msg


@pytest.mark.parametrize("sql", [
    "SELECT run_started_at FROM job_log",
    "SELECT j.run_started_at FROM job_log j",
    "{{ config(materialized='view') }}SELECT run_started_at FROM job_log",
])
def test_SQL裡同名的欄位不算(sql):
    assert "H008" not in _rules(sql)


def test_預掃用展開前的原文給提醒(monkeypatch):
    """展開成功時 H008 來自原文(展開後就看不到 Jinja 了)。"""
    monkeypatch.setattr(pipeline, "render_model_isolated", render_model)

    class _Hub:
        async def call_json(self, name, args):
            return []           # 展開後的 SQL 交給規則層的結果:空的,命中只會來自原文

        async def call(self, name, args):
            return ""

    src = "{% if run_started_at.year >= 2027 %}DELETE FROM t WHERE id = 1{% endif %}SELECT 1 AS a"
    entry = asyncio.run(pipeline.prescan(
        _Hub(), [{"path": "models/m.sql", "full_content": src}],
        {"enabled": True, "database": DB}))[0]
    assert "H008" in {h["rule"] for h in entry["rules"]}


# ─────────────────── 5. 執行驗證不用它 ───────────────────

def _exec(model_sql, macro_sql=None):
    from orchestrator.dbt_exec import render_for_exec
    from tests.test_dbt_exec import INFO, SPEC
    kwargs = {"project": INFO}
    if macro_sql is not None:
        kwargs["macro_files"] = {"macros/m.sql": macro_sql}
    return render_for_exec("models/mrt_BIG.sql", model_sql, SPEC, DB, kwargs)


SOURCE_MODEL = ("SELECT account_no, amount FROM {{ source('raw','transactions') }}\n"
                "WHERE txn_date = CAST('{{ run_started_at.strftime(\"%Y-%m-%d\") }}' AS DATE)\n"
                "  AND created_at < GETDATE()")


def test_用到_run_started_at_的_model_不送沙盒():
    from orchestrator.dbt_exec import RUN_STARTED_AT_UNVERIFIED
    assert _exec(SOURCE_MODEL) == (None, RUN_STARTED_AT_UNVERIFIED)


def test_macro_裡用到也一樣():
    from orchestrator.dbt_exec import RUN_STARTED_AT_UNVERIFIED
    macro = "{% macro today_str() %}{{ run_started_at.strftime('%Y-%m-%d') }}{% endmacro %}"
    model = ("SELECT account_no FROM {{ source('raw','transactions') }} "
             "WHERE txn_date = CAST('{{ today_str() }}' AS DATE)")
    assert _exec(model, macro) == (None, RUN_STARTED_AT_UNVERIFIED)


def test_沒呼叫寫到它的_macro_照常執行():
    macro = ("{% macro today_str() %}{{ run_started_at.strftime('%Y-%m-%d') }}{% endmacro %}"
             "{% macro amount_col() %}amount{% endmacro %}")
    model = "SELECT account_no, {{ amount_col() }} FROM {{ source('raw','transactions') }}"
    sql, reason = _exec(model, macro)
    assert reason is None and "[transactions]" in sql


def test_沒用到_run_started_at_的_model_照常執行():
    sql, reason = _exec("SELECT account_no, amount FROM {{ source('raw','transactions') }}")
    assert reason is None and "[transactions]" in sql


def test_規則層的展開照舊用審查開始的時間():
    r = _render("SELECT '{{ run_started_at.strftime(\"%Y\") }}' AS y")
    assert r.ok and r.run_started_at_used and re.fullmatch(r"SELECT '\d{4}' AS y", r.sql)


@pytest.mark.parametrize("cls, name", [
    (datetime.datetime, "strptime"), (datetime.datetime, "fromisoformat"), (datetime.datetime, "combine"),
    (datetime.date, "fromisoformat"), (datetime.time, "fromisoformat"),
])
def test_時間的類別只放行結果固定的建構方法(cls, name):
    """之後提供 modules.datetime 時用同一道防線:建構方法的結果不看主機的時區與現在時間。"""
    dbt_render._guard_time_access(cls, name)


@pytest.mark.parametrize("cls, name", [
    (datetime.datetime, "now"), (datetime.datetime, "today"), (datetime.datetime, "utcnow"),
    (datetime.datetime, "fromtimestamp"), (datetime.datetime, "min"), (datetime.datetime, "max"),
    (datetime.date, "today"), (datetime.date, "fromtimestamp"), (datetime.timedelta, "max"),
    (datetime.timezone, "utc"), (datetime.tzinfo, "utcoffset"),
])
def test_時間的類別擋下看主機時間的方法與時區(cls, name):
    with pytest.raises(dbt_render.DbtRenderError, match="沒有照實模擬"):
        dbt_render._guard_time_access(cls, name)


def test_env_var_的提醒只印名稱_不印括號與參數():
    msg = _rules("SELECT '{{ env_var(\"DB_ENV\") }}' AS e")["H008"]
    assert "env_var" in msg and "env_var(" not in msg and "DB_ENV" not in msg


def test_date_沒有照實模擬的屬性_is_defined_也展開失敗():
    """`date()` 拿到的是 date:它的放行清單沒有時分秒,問 `is defined` 也不回答。"""
    _fails("{% if run_started_at.date().hour is defined %}x{% endif %}")


def test_只在_macro_檔最外層用到的不算(tmp_path):
    """dbt 不執行 macro 檔最外層的程式碼:那裡寫到 run_started_at,不代表 model 用了。"""
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "a.sql").write_text(
        "{% macro ts() %}{{ run_started_at }}{% endmacro %}{% macro one() %}1{% endmacro %}",
        encoding="utf-8")
    # 後載入的檔在最外層呼叫前一個檔的 macro(載入時就會跑到)
    (tmp_path / "macros" / "b.sql").write_text("{% set loaded_at = ts() %}", encoding="utf-8")
    r = render_model("models/m.sql", code_root=tmp_path, source="SELECT {{ one() }} AS n")
    assert r.ok and not r.run_started_at_used
