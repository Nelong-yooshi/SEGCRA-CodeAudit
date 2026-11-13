"""dbt 的 run_started_at——不呼叫 LLM、不連資料庫。

dbt-core 1.12:`run_started_at` 是這次執行開始的時間,帶 UTC 時區的 datetime
(`tracking.User.run_started_at = datetime.now(tz=pytz.utc)`)。用到它的 model 原本展開失敗。

  1. 型別與格式跟 dbt 一樣:`.strftime()`、`.date()`、`.year` 可用,直接輸出是
     `YYYY-MM-DD HH:MM:SS.ffffff+00:00`
  2. 一份審查共用一個值(一般、增量兩次展開與插記號那次的原文才會相同);用真實時間,
     不是固定值(固定值的話樣板分得出自己在被審查)
  3. 呼叫端給的值要帶 UTC 時區;時區物件不能直接輸出;macro 不能叫這個名字
  4. 拿它當 Jinja 條件 → H008 提醒(樣板版的定時炸彈);SQL 裡同名的欄位不算
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


def test_時區物件不能直接輸出():
    r = _render("{{ run_started_at.tzinfo }}")
    assert not r.ok and "物件" in r.error


@pytest.mark.parametrize("src", [
    "{{ run_started_at.astimezone() }}",                         # 換成主機的時區
    "{{ run_started_at.now() }}",                                # 主機的現在時間
    "{{ run_started_at.today() }}",
    "{{ run_started_at.utcnow() }}",
    "{{ run_started_at.fromtimestamp(0) }}",                     # 沒給時區:主機的時區
    "{{ (run_started_at + (run_started_at - run_started_at)).astimezone() }}",   # 先運算再換
    "{{ run_started_at.replace(year=2030).astimezone() }}",
])
def test_看主機本地時區或現在時間的寫法_展開失敗(src):
    """得到的是審查機的值(dbt 執行時是部署端主機的值),而且每次展開都不同。不猜。"""
    r = _render(src)
    assert not r.ok and "主機的本地時間" in r.error, (src, r.error)
    # 呼叫端指定值的時候(一份審查裡的展開)也一樣
    r = _render(src, run_started_at="2026-01-02T03:04:05+00:00")
    assert not r.ok and "主機的本地時間" in r.error, (src, r.error)


@pytest.mark.parametrize("src, want", [
    ("{{ run_started_at.astimezone(run_started_at.tzinfo) }}", r"[\d\- :.]+\+00:00"),
    ("{{ run_started_at.fromtimestamp(0, run_started_at.tzinfo) }}", r"1970-01-01 00:00:00\+00:00"),
])
def test_有給時區的照常可用(src, want):
    assert re.fullmatch(want, _value(src)), src


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
