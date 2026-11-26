"""macro 反查確定不了呼叫對象時,原因分開寫、寫出是哪個檔——不呼叫 LLM、不連資料庫。

yml 的 hook、`config()` 裡的樣板字串:語法錯誤、巢狀太深、真的動態呼叫,看的人要去查的東西不同。
外層接到 RecursionError 時寫出是反查這一步、當時在處理哪個檔。
"""
from orchestrator import dbt_impact
from orchestrator.dbt_impact import SCAN_DYNAMIC, SCAN_SYNTAX, SCAN_TOO_DEEP, analyze_macro_impact

MACRO = "{% macro amt() %}amount{% endmacro %}"
BASE = {"macros/a.sql": "{% macro amt() %}amt{% endmacro %}"}


def _report(extra: dict):
    files = {"macros/a.sql": MACRO, "models/m.sql": "SELECT {{ amt() }} FROM t", **extra}
    return analyze_macro_impact(files, ["macros/a.sql"], base_files=BASE)


def _text(report) -> str:
    reasons = [r for a in report.affected_models for r in a.chain]
    return "\n".join(list(report.uncertain) + reasons)


def test_yml_的樣板語法錯誤_寫成語法錯誤與檔名():
    text = _text(_report({"models/schema.yml": 'models:\n  - name: m\n    config:\n      post_hook: "{{ amt( }}"\n'}))
    assert "models/schema.yml" in text and SCAN_SYNTAX in text
    assert SCAN_DYNAMIC not in text


def test_yml_的真的動態呼叫_寫成動態呼叫與檔名():
    text = _text(_report({"models/schema.yml":
                          'models:\n  - name: m\n    config:\n      post_hook: "{{ context[var(\'x\')]() }}"\n'}))
    assert "models/schema.yml" in text and SCAN_DYNAMIC in text and SCAN_SYNTAX not in text


def test_model_的_config_字串語法錯誤_寫出_model_與原因():
    text = _text(_report({"models/n.sql": "{{ config(post_hook='{{ amt( }}') }}SELECT 1"}))
    assert "models/n.sql" in text and SCAN_SYNTAX in text


def test_外層接到_RecursionError_時寫出反查這一步與檔名(monkeypatch):
    def boom(self, text, depth=0):
        raise RecursionError
    monkeypatch.setattr(dbt_impact._Scanner, "scan_yml", boom)
    report = _report({"models/schema.yml": "models: []\n"})
    text = "\n".join(report.uncertain)
    assert "macro 反查這一步" in text and "models/schema.yml" in text
    assert SCAN_TOO_DEEP.lstrip("的") in text


def test_外層接到_RecursionError_而且不知道是哪個檔(monkeypatch):
    def boom(self):
        raise RecursionError
    monkeypatch.setattr(dbt_impact._Analysis, "run", boom)
    text = "\n".join(_report({}).uncertain)
    assert "不確定是哪個檔" in text
