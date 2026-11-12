"""dbt 的失敗原因要分得清楚——不呼叫 LLM、不連資料庫。

1. macro 反查(dbt_impact)的「無法解析」分開寫原因:Jinja 語法錯誤(dbt 一樣解析不了,
   整個專案失敗)與巢狀太深(分析用的遞迴上限,dbt 不一定失敗)。寫成同一句的話,看的人
   分不出是該修樣板,還是分析做不到。macro 檔、model、snapshot、變更前後的比對都一樣。
2. macro 檔載入失敗(dbt_render.build_env)只記一份:分類(`segcra_macro_issues`)。回傳的
   說明文字(`macro_problems`)由分類產生,兩份不會對不上;說明不帶錯誤訊息(可能含樣板內容)。
"""
from __future__ import annotations

import pytest

from orchestrator import dbt_impact, dbt_render
from orchestrator.dbt_impact import SCAN_SYNTAX, SCAN_TOO_DEEP, analyze_macro_impact
from orchestrator.dbt_render import (
    MACRO_LOAD_FAILED, MACRO_SYNTAX, MACRO_TOO_DEEP, MACRO_UNSUPPORTED, build_env,
    macro_problem_text,
)

GOOD_MACRO = "{% macro amt() %}amount > 1000{% endmacro %}"
BROKEN = "{% macro broken( %}"
FILES = {
    "macros/a.sql": GOOD_MACRO,
    "models/m.sql": "SELECT * FROM t WHERE {{ amt() }}",
}


def _deep(monkeypatch, marker: str):
    """含 marker 的內容掃描時丟 RecursionError(真的寫出那麼深的樣板會讓測試很慢)。"""
    real = dbt_impact._Scanner.scan

    def scan(self, source, depth=0):
        if marker in source:
            raise RecursionError("too deep")
        return real(self, source, depth)

    monkeypatch.setattr(dbt_impact._Scanner, "scan", scan)


def _text(report) -> str:
    parts = list(report.uncertain)
    for m in report.affected_models:
        parts += list(m.chain)
    return "\n".join(parts)


# ─────────────────── 1. macro 反查分開寫原因 ───────────────────

def test_掃描器回傳原因():
    scanner = dbt_impact._Scanner()
    assert scanner.try_scan(GOOD_MACRO)[1] is None
    assert scanner.try_scan(BROKEN) == (None, SCAN_SYNTAX)


def test_掃描器把巢狀太深分開(monkeypatch):
    _deep(monkeypatch, "DEEP")
    assert dbt_impact._Scanner().try_scan("{{ DEEP }}") == (None, SCAN_TOO_DEEP)


def test_沒有改到的macro檔語法錯誤():
    files = dict(FILES, **{"macros/z.sql": BROKEN})
    report = analyze_macro_impact(files, ["macros/a.sql"], base_files={"macros/a.sql": "x"})
    assert report.all_models_possibly_affected
    assert f"macro 檔 macros/z.sql {SCAN_SYNTAX}" in _text(report)
    assert "無法解析" not in _text(report)


def test_沒有改到的macro檔巢狀太深(monkeypatch):
    _deep(monkeypatch, "DEEP")
    files = dict(FILES, **{"macros/z.sql": "{% macro z() %}{{ DEEP }}{% endmacro %}"})
    report = analyze_macro_impact(files, ["macros/a.sql"], base_files={"macros/a.sql": "x"})
    assert f"macro 檔 macros/z.sql {SCAN_TOO_DEEP}" in _text(report)
    assert SCAN_SYNTAX not in _text(report)


@pytest.mark.parametrize("label,new,old,why", [
    ("變更後", BROKEN, GOOD_MACRO, SCAN_SYNTAX),
    ("變更前", GOOD_MACRO, BROKEN, SCAN_SYNTAX),
])
def test_改到的macro檔_變更前後各自寫原因(label, new, old, why):
    files = dict(FILES, **{"macros/a.sql": new})
    report = analyze_macro_impact(files, ["macros/a.sql"], base_files={"macros/a.sql": old})
    assert f"macro 檔 macros/a.sql({label}){why},無法得知變更了哪些 macro" in _text(report)


def test_改到的macro檔變更後巢狀太深(monkeypatch):
    _deep(monkeypatch, "DEEP")
    files = dict(FILES, **{"macros/a.sql": "{% macro amt() %}{{ DEEP }}{% endmacro %}"})
    report = analyze_macro_impact(files, ["macros/a.sql"], base_files={"macros/a.sql": GOOD_MACRO})
    assert f"macro 檔 macros/a.sql(變更後){SCAN_TOO_DEEP}" in _text(report)


@pytest.mark.parametrize("deep", [False, True])
def test_model解析不了_寫出原因(monkeypatch, deep):
    if deep:
        _deep(monkeypatch, "DEEP")
    body = "SELECT {{ DEEP }}" if deep else "SELECT {{ oops( }}"
    why = SCAN_TOO_DEEP if deep else SCAN_SYNTAX
    files = dict(FILES, **{"models/bad.sql": body})
    report = analyze_macro_impact(files, ["macros/a.sql"], base_files={"macros/a.sql": "x"})
    bad = {m.path: m for m in report.affected_models}["models/bad.sql"]
    assert not bad.certain
    assert bad.chain == (f"model {why},無法確認它呼叫了哪些 macro",)
    assert any(u.startswith(f"model models/bad.sql {why}") for u in report.uncertain),         report.uncertain


@pytest.mark.parametrize("deep", [False, True])
def test_snapshot解析不了_寫出原因(monkeypatch, deep):
    if deep:
        _deep(monkeypatch, "DEEP")
    body = "{% snapshot s %}{{ DEEP }}{% endsnapshot %}" if deep else "{% snapshot s %}{{ x( }}"
    why = SCAN_TOO_DEEP if deep else SCAN_SYNTAX
    files = dict(FILES, **{"snapshots/s.sql": body})
    report = analyze_macro_impact(files, ["macros/a.sql"], base_files={"macros/a.sql": "x"},
                                  snapshot_dirs=("snapshots",))
    assert any(u.startswith(f"snapshot snapshots/s.sql {why}") for u in report.uncertain), \
        report.uncertain


# ─────────────────── 2. macro 載入失敗只記一份 ───────────────────

def _write(tmp_path, name, text):
    d = tmp_path / "macros"
    d.mkdir(exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")


def test_說明文字由分類產生_一筆失敗只有一筆(tmp_path):
    _write(tmp_path, "good.sql", GOOD_MACRO)
    _write(tmp_path, "bad.sql", "{% macro broken() %} {% endfor %}")
    _write(tmp_path, "worse.sql", "{% macro w( %}")
    env, _, _, problems = build_env(tmp_path)
    assert len(problems) == len(env.segcra_macro_issues) == 2
    assert problems == [macro_problem_text(i) for i in env.segcra_macro_issues]


def test_說明文字寫出路徑_原因與行號(tmp_path):
    _write(tmp_path, "bad.sql", "\n\n{% macro broken() %} {% endfor %}")
    _, _, _, problems = build_env(tmp_path)
    assert problems == ["macros/bad.sql:Jinja 語法錯誤(第 3 行)"]


@pytest.mark.parametrize("kind,word", [
    (MACRO_SYNTAX, "語法錯誤"),
    (MACRO_UNSUPPORTED, "不認得的濾器"),
    (MACRO_TOO_DEEP, "巢狀太深"),
    (MACRO_LOAD_FAILED, "載入失敗"),
])
def test_每一種分類都有說明(kind, word):
    text = macro_problem_text({"path": "macros/x.sql", "kind": kind, "line": 0})
    assert text.startswith("macros/x.sql:") and word in text
    assert "第" not in text                     # 沒有行號時不寫


def test_說明文字不帶錯誤訊息裡的樣板內容(tmp_path):
    _write(tmp_path, "bad.sql", "{% ZZ_SECRET_TAG_ZZ %}")
    _, _, _, problems = build_env(tmp_path)
    assert problems and "ZZ_SECRET_TAG_ZZ" not in "".join(problems)


def test_展開結果的macro_problems跟build_env的一致(tmp_path):
    _write(tmp_path, "good.sql", GOOD_MACRO)
    _write(tmp_path, "bad.sql", "{% macro broken() %} {% endfor %}")
    r = dbt_render.render_model(tmp_path / "models" / "m.sql", code_root=tmp_path,
                                source="SELECT * FROM t WHERE {{ amt() }}")
    assert r.ok, r.error
    assert r.macro_problems == ["macros/bad.sql:Jinja 語法錯誤(第 1 行)"]
