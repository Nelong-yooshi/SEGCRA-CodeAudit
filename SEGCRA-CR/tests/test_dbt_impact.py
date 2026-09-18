"""dbt_impact 的確定性測試 — 規格對應(Issue #7 第 2 點)與 macro 反查(第 3 點)。

不呼叫 LLM、不連資料庫、不執行任何樣板,可進 CI。

驗的五件事:
  1. **以 dbt 官方的依賴資料為標準答案**:tests/dbt_impact_reference/generate.py 以
     `dbt parse` 產生 tests/fixtures/dbt_manifest/impact_deps.json。dbt 判定受影響的
     model,我們必須全部判定為「確定受影響」(不能漏);我們判定為確定的,除了已記錄的
     例外,也不能比 dbt 多(不能亂報)。
  2. 無法靜態確定時(動態呼叫、覆寫內建、hook、解析失敗、上限…)一律 needs_human,
     並保守列出可能受影響的 model。
  3. 規格對應只回傳實際存在的規格,不自行擇一,不接受不安全的路徑。
  4. **資安**:不執行樣板、路徑白名單、惡意輸入不會卡住、資源上限、輸出有上限。
  5. 隨機輸入下不變條件永遠成立。

測試資料全為去識別化的假名。
"""
import ast
import importlib.util
import json
import pathlib
import random
import time

import jinja2
import pytest

from orchestrator import dbt_impact
from orchestrator.dbt_builtin_macros import BUILTIN_MACROS
from orchestrator.dbt_impact import (
    MAX_REASON_CHARS, MAX_REASONS, ImpactReport, SpecMatch, _identifier_tokens,
    analyze_macro_impact, analyze_macro_impact_isolated, normalize_path, resolve_spec,
    resolve_specs,
)

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
IMPACT_PROJECT = PKG_ROOT / "tests" / "dbt_impact_reference"
DEPS = json.loads((PKG_ROOT / "tests" / "fixtures" / "dbt_manifest" / "impact_deps.json")
                  .read_text(encoding="utf-8"))
SAMPLE_CODE = PKG_ROOT / "examples" / "sample" / "RETAIL_M1_code"

_spec = importlib.util.spec_from_file_location("dbt_impact_reference_generate",
                                               IMPACT_PROJECT / "generate.py")
impact_reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(impact_reference)
FILES = impact_reference.project_files()
PROJECT = "segcra_impact"
ALL_MODELS = sorted(DEPS["models"])


def _analyze(changed, files=None, **kwargs):
    files = FILES if files is None else files
    kwargs.setdefault("base_files", {p: files[p] for p in changed if p in files})
    return analyze_macro_impact(files, changed, **kwargs)


def _certain(report: ImpactReport) -> set:
    return {m.path for m in report.affected_models if m.certain}


def _all(report: ImpactReport) -> set:
    return {m.path for m in report.affected_models}


def _assert_report_invariants(report: ImpactReport, model_paths) -> None:
    assert isinstance(report, ImpactReport)
    assert {m.path for m in report.affected_models} <= set(model_paths)
    assert len(report.uncertain) <= MAX_REASONS + 1
    for text in report.uncertain:
        assert len(text) <= MAX_REASON_CHARS + 1
        assert all(ch.isprintable() for ch in text)
    for model in report.affected_models:
        assert model.chain, "每個受影響的 model 都要說明原因"
        assert all(len(c) <= MAX_REASON_CHARS + 1 and all(ch.isprintable() for ch in c)
                   for c in model.chain)
    if report.all_models_possibly_affected or not all(m.certain for m in report.affected_models):
        assert report.needs_human, "有保守推定的結果就必須交人工確認"


# ---------------------------------------------------------------- dbt 標準答案
def _dbt_expected(changed_file: str) -> tuple[set, bool]:
    """依 dbt manifest 的依賴,推算改了 changed_file 會影響哪些 model、會不會碰到 hook。"""
    closure = {n for n, m in DEPS["macros"].items() if m["path"] == changed_file}
    grew = True
    while grew:
        grew = False
        for name, macro in DEPS["macros"].items():
            deps = {d.split(".", 1)[1] for d in macro["macros"] if d.startswith(PROJECT + ".")}
            if name not in closure and deps & closure:
                closure.add(name)
                grew = True

    def hits(deps):
        return {d.split(".", 1)[1] for d in deps if d.startswith(PROJECT + ".")} & closure

    models = {p for p, m in DEPS["models"].items() if hits(m["macros"])}
    hook = any(hits(op["macros"]) for op in DEPS["operations"].values())
    return models, hook


# dbt 自己記不到、但我們刻意判為確定受影響的情況(比 dbt 保守),逐一記錄理由:
#   ZETA 以字串 'flag_small' 交給 call_by_name 動態呼叫;dbt 只記錄 call_by_name。
EXTRA_CERTAIN = {
    "macros/registry/rules.sql": {"models/ZETA.sql"},
    "macros/logic/flags.sql": {"models/ZETA.sql"},
}
MACRO_FILES = sorted({m["path"] for m in DEPS["macros"].values()})


def test_manifest_fixture_matches_reference_project():
    """標準答案要與目前的對照專案一致(改了專案卻沒重跑 generate.py 時會失敗)。"""
    assert DEPS["adapter"] == "sqlserver"
    assert set(DEPS["models"]) == {p for p in FILES if p.startswith("models/") and p.endswith(".sql")}
    assert set(MACRO_FILES) == {p for p in FILES if p.startswith("macros/")}


@pytest.mark.parametrize("macro_file", MACRO_FILES)
def test_impact_covers_dbt_dependencies(macro_file):
    """dbt 判定受影響的 model 必須全部被判為「確定受影響」;hook 必須觸發全部 model。"""
    expected, hook = _dbt_expected(macro_file)
    report = _analyze([macro_file])
    missing = expected - _certain(report)
    assert not missing, f"漏判:{sorted(missing)}"
    if hook:
        assert report.all_models_possibly_affected
    _assert_report_invariants(report, ALL_MODELS)


@pytest.mark.parametrize("macro_file", MACRO_FILES)
def test_impact_does_not_overclaim_certainty(macro_file):
    """判為「確定」的 model 不可比 dbt 多,除非是已記錄理由的例外。"""
    expected, _ = _dbt_expected(macro_file)
    report = _analyze([macro_file])
    assert _certain(report) == expected | EXTRA_CERTAIN.get(macro_file, set())


@pytest.mark.parametrize("path, dbt_macros", sorted(
    (p, {d.split(".", 1)[1] for d in m["macros"]}) for p, m in DEPS["models"].items()))
def test_every_direct_dependency_is_seen(path, dbt_macros):
    """逐一 model 對照 dbt 記錄的直接依賴:改那個 macro 的檔案時,這個 model 必須受影響。"""
    for name in dbt_macros:
        if name not in DEPS["macros"]:      # dbt 內建 macro(例如 is_incremental)不在專案裡
            continue
        report = _analyze([DEPS["macros"][name]["path"]])
        assert path in _certain(report), f"{path} 依賴 {name},但未被判為受影響"


# ---------------------------------------------------------------- 各種寫法
def test_registry_change_reaches_models_through_macros():
    """改規則註冊表(比照 config.sql)→ 透過條件 macro 影響所有使用它的 model。"""
    report = _analyze(["macros/registry/rules.sql"])
    by_path = {m.path: m for m in report.affected_models}
    chain = by_path["models/mrt_ALPHA.sql"].chain
    assert chain[0].startswith("get_rules(")
    assert "flag_large" in chain
    assert chain[-1] == "models/mrt_ALPHA.sql"


def test_dispatch_implementations_are_linked():
    report = _analyze(["macros/logic/format.sql"])
    assert set(report.changed_macros) == {"default__fmt_amount", "sqlserver__fmt_amount"}
    assert "models/BETA.sql" in _certain(report)


def test_macro_in_post_hook_string_is_linked():
    assert "models/GAMMA.sql" in _certain(_analyze(["macros/hooks/audit.sql"]))


def test_macro_passed_as_value_is_linked():
    assert "models/intermediate/int_DELTA.sql" in _certain(_analyze(["macros/logic/flags.sql"]))


def test_project_hook_marks_all_models():
    report = _analyze(["macros/hooks/run_end.sql"])
    assert report.all_models_possibly_affected
    assert _all(report) == set(ALL_MODELS)
    assert any("dbt_project.yml" in r for r in report.uncertain)


@pytest.mark.parametrize("macro_file", ["macros/global/schema_name.sql",
                                        "macros/materializations/noop.sql"])
def test_builtin_override_marks_all_models(macro_file):
    """覆寫 dbt 內建(dbt 執行時自己會呼叫,專案內找不到呼叫點)→ 全部 model。"""
    report = _analyze([macro_file])
    assert report.all_models_possibly_affected
    assert _all(report) == set(ALL_MODELS)
    assert report.needs_human


def test_dynamic_call_is_uncertain_and_conservative():
    """專案有動態呼叫時,任何 macro 變更都要交人工,並列出經過動態呼叫的 model。"""
    report = _analyze(["macros/unused/dead.sql"])
    assert report.unreferenced_macros == ("dead_macro",)
    assert report.needs_human
    zeta = {m.path: m for m in report.affected_models}["models/ZETA.sql"]
    assert not zeta.certain


def test_unreferenced_macro_without_dynamic_calls_is_clean():
    files = {p: t for p, t in FILES.items()
             if p not in ("models/ZETA.sql", "macros/logic/dynamic.sql")}
    report = _analyze(["macros/unused/dead.sql"], files=files)
    assert report.unreferenced_macros == ("dead_macro",)
    assert report.affected_models == ()
    assert not report.needs_human


@pytest.mark.parametrize("model", [
    # hook 字串在執行時才拼出來:靜態看不到呼叫哪個 macro
    '{% set name = "dead" ~ "_macro" %}{% set hook = "{{ " ~ name ~ "() }}" %}'
    '{{ config(post_hook=hook) }}SELECT 1',
    '{{ config(post_hook="{{ " ~ var("hook_macro") ~ "() }}") }}SELECT 1',
    '{% set hook = ["{{ dead", "_macro() }}"] | join("") %}{{ config(post_hook=hook) }}SELECT 1',
])
def test_hook_string_built_at_runtime_is_uncertain(model):
    files = dict(STATIC_FILES, **{"models/HOOKED.sql": model})
    report = _analyze(["macros/unused/dead.sql"], files=files)
    assert report.needs_human
    hooked = {m.path: m for m in report.affected_models}.get("models/HOOKED.sql")
    assert hooked is not None and not hooked.certain
    _assert_report_invariants(report, _models_of(files))


# ---------------------------------------------------------------- 繞過手法(靜態看不到 macro 呼叫)
# 專案內沒有任何動態呼叫的版本:用來確認「只因為新加的那一段」而判為不確定
STATIC_FILES = {p: t for p, t in FILES.items()
                if p not in ("models/ZETA.sql", "macros/logic/dynamic.sql")}
DEAD = "macros/unused/dead.sql"


def _models_of(files) -> list:
    return sorted(p for p in files if p.startswith("models/") and p.endswith(".sql"))


# 每一種寫法都可能讓 dbt 執行時呼叫到 dead_macro,但靜態分析看不到名稱或看不出呼叫
EVASIVE_MODELS = {
    # hook 在執行期才組出來(dbt 會再渲染一次 hook 字串)
    "拆開大括號": '{{ config(post_hook="{" ~ "{ dead_" ~ "macro() }" ~ "}") }}SELECT 1',
    "以 %c 產生大括號": '{{ config(post_hook=("%c%c" % (123, 123)) ~ " dead_" ~ "macro() "'
                  ' ~ ("%c%c" % (125, 125))) }}SELECT 1',
    "hook 來自 var": '{{ config(post_hook=var("h")) }}SELECT 1',
    "hook 清單含變數": '{{ config(pre_hook=["select 1", h]) }}SELECT 1',
    "hook 字典含變數": '{{ config(post_hook={"sql": h, "transaction": false}) }}SELECT 1',
    "config(**kwargs)": '{% set d = {"post_hook": h} %}{{ config(**d) }}SELECT 1',
    "config(變數)": '{% set d = {} %}{{ config(d) }}SELECT 1',
    "config 字典參數含變數": '{{ config({"post-hook": h}) }}SELECT 1',
    "config 字典鍵為變數": '{{ config({k: "x"}) }}SELECT 1',
    "call 區塊": '{% call config(post_hook=h) %}{% endcall %}SELECT 1',
    "config.set hook": '{{ config.set("post_hook", h) }}SELECT 1',
    "config.set 鍵為變數": '{{ config.set(k, "x") }}SELECT 1',
    # config 物件被取別名或傳出去後再設定 hook
    "config 別名": '{% set c = config %}{{ c(post_hook=h) }}SELECT 1',
    "config 當參數傳出": '{{ helper(config) }}SELECT 1',
    "config 未知屬性": '{{ config.model }}SELECT 1',
    "if 內的同名變數不算遮蔽": '{% if false %}{% set config = {} %}{% endif %}\n{% set c = config %}\n'
                    '{{ c(post_hook=h) }}',
    "for 內的同名變數不算遮蔽": '{% for i in [1] %}{% set config = {} %}{% endfor %}\n'
                     '{% set c = config %}\n{{ c(post_hook=h) }}',
    "等號右邊跨行讀到原本的 config": '{% set config = [\n config\n] %}\n{% set c = config[0] %}\n'
                         '{{ c(post_hook=h) }}',
    "同一行先取別名再遮蔽": '{% set c = config %}{% set config = {} %}{{ c(post_hook=h) }}',
    # 經由 context / builtins / 樣板參照取用任意 macro
    "context 別名": '{% set c = context %}{% set f = c.get(n) %}{{ f() }}',
    "context.get 取 config": '{% set c = context.get("config") %}{{ c(post_hook=h) }}',
    "context.items 走訪": '{% for k, v in context.items() %}{% if k == n %}{{ v() }}{% endif %}{% endfor %}',
    "context.config": '{{ context.config(post_hook=h) }}',
    "context[\'config\']": '{{ context["config"](post_hook=h) }}',
    "builtins 別名": '{% set b = builtins %}{% set f = b.get(n) %}{{ f() }}',
    "self 傳出": '{{ helper(self) }}SELECT 1',
    "私有屬性": '{% set g = ref.__globals__ %}{% set f = g.get(n) %}{{ f() }}',
    # 經由 macro 物件(dbt 的 macro 物件有公開的 .context)或 Jinja 的屬性取用
    "macro.context": '{% set f = flag_small.context.get(n) %}{{ f() }}',
    "macro['context']": '{% set f = flag_small["context"].get(n) %}{{ f() }}',
    "macro[執行期名稱]": '{% set f = flag_small["con" ~ "text"].get(n) %}{{ f() }}',
    "macro 別名[執行期名稱]": '{% set m = flag_small %}{% set m2 = m %}{% set f = m2[k].get(n) %}{{ f() }}',
    # 多層別名(走訪順序不定,單次掃描無法保證追完)
    "macro 多層別名": "".join(f"{{% set a{i + 1} = a{i} %}}" for i in range(8))
                   .replace("a0", "flag_small") + "{% set f = a8[k].get(n) %}{{ f() }}",
    "selectattr(context)": '{% set g = [flag_small]|selectattr("context")|list %}',
    "rejectattr(私有)": '{{ [x]|rejectattr("__class__")|list }}',
    "macro 參數預設值取別名": "{% macro m(config=config) %}{% set c = config %}{{ c(post_hook=h) }}"
                          "{% endmacro %}{{ m() }}",
    "ref[執行期名稱]": '{% set g = ref[k] %}{% set f = g.get(n) %}{{ f() }}',
    "attr filter 私有屬性": '{% set g = ref|attr("__globals__") %}{% set f = g.get(n) %}{{ f() }}',
    "attr filter 名稱為變數": '{% set f = (flag_small|attr(a)).get(n) %}{{ f() }}',
    "map(attribute=context)": '{% set g = [flag_small]|map(attribute="context")|first %}'
                              '{% set f = g.get(n) %}{{ f() }}',
    "map 轉交 attr": '{% set g = [flag_small]|map("attr", "context")|first %}'
                   '{% set f = g.get(n) %}{{ f() }}',
    "sum(attribute=變數)": '{{ [flag_small]|sum(attribute=a) }}',
    "join(attribute=私有)": '{{ x|join(",", attribute="__class__") }}',
    # 屬性名稱以位置參數傳入(Jinja 3.1 的參數順序)
    "sort 位置參數": '{{ [x]|sort(false, false, "__class__") }}',
    "unique 位置參數": '{{ [flag_small]|unique(false, "context")|list }}',
    "min 位置參數": '{{ [flag_small]|min(false, a) }}',
    "join 位置參數": '{{ [flag_small]|join(",", "context") }}',
    "sum 位置參數": '{{ [x]|sum(a) }}',
    "groupby 位置參數": '{{ [flag_small]|groupby("context")|list }}',
}


@pytest.mark.parametrize("name", sorted(EVASIVE_MODELS))
def test_evasive_model_constructs_are_uncertain(name):
    files = dict(STATIC_FILES, **{"models/H.sql": EVASIVE_MODELS[name]})
    report = _analyze([DEAD], files=files)
    assert report.needs_human, name
    hooked = {m.path: m for m in report.affected_models}.get("models/H.sql")
    assert hooked is not None and not hooked.certain, name
    _assert_report_invariants(report, _models_of(files))


def test_evasive_construct_inside_macro_reaches_calling_model():
    files = dict(STATIC_FILES, **{
        "macros/cfg.sql": '{% macro my_cfg() %}{{ config(post_hook="{" ~ "{ x() }}") }}{% endmacro %}',
        "models/H.sql": "{{ my_cfg() }}SELECT 1",
    })
    report = _analyze([DEAD], files=files)
    assert report.needs_human
    assert "models/H.sql" in _all(report) - _certain(report)


EVASIVE_YML = {
    "樣板內動態取用": "models:\n  +post-hook: \"{{ context['dead_' ~ 'macro']() }}\"\n",
    "\\x 跳脫藏住樣板與名稱": 'models:\n  +post-hook: "\\x7b\\x7b dead\\x5fmacro() \\x7d\\x7d"\n',
    "\\u 跳脫": 'models:\n  +post-hook: "\\u007b\\u007b dead_macro() }}"\n',
    "\\U 跳脫": 'models:\n  +post-hook: "\\U0000007b\\U0000007b dead_macro() }}"\n',
    "行尾反斜線接行": 'models:\n  +post-hook: "{{ dead_\\\n    macro() }}"\n',
    # 大括號與名稱都以接行拆開:原始文字中沒有 {{ 可解析,名稱也對不上,只能靠跳脫檢查
    "接行拆開大括號與名稱": 'models:\n  +post-hook: "{\\\n{ dead_\\\n    macro() }}"\n',
    "樣板無法解析": "models:\n  +post-hook: \"{{ (context['dead_' ~ 'macro']() \"\n",
    "self 樣板參照": "models:\n  +post-hook: \"{{ self._TemplateReference__context.get('x') }}\"\n",
}


@pytest.mark.parametrize("yml_path", ["dbt_project.yml", "models/sub/_props.yml"])
@pytest.mark.parametrize("name", sorted(EVASIVE_YML))
def test_evasive_yml_is_uncertain(name, yml_path):
    files = dict(STATIC_FILES, **{"models/sub/H.sql": "SELECT 1"})
    files[yml_path] = files.get(yml_path, "") + EVASIVE_YML[name]
    report = _analyze([DEAD], files=files)
    assert report.needs_human
    assert "models/sub/H.sql" in _all(report) - _certain(report)
    if yml_path == "dbt_project.yml":
        assert report.all_models_possibly_affected


# 常見的正常寫法不可因上述檢查而誤報(否則每個 macro MR 都得交人工)
ORDINARY_MODELS = {
    "一般 config": "{{ config(materialized='table', tags=['a']) }}SELECT 1",
    "hook 寫死": '{{ config(post_hook="{{ audit_hook() }}") }}SELECT 1',
    "hook 清單寫死": '{{ config(post_hook=["grant select on t to r", "{{ audit_hook() }}"]) }}SELECT 1',
    "hook 字典寫死": '{{ config(post_hook={"sql": "{{ audit_hook() }}", "transaction": false}) }}SELECT 1',
    "config 字典參數": '{{ config({"materialized": "view", "post-hook": "{{ audit_hook() }}"}) }}SELECT 1',
    "config.get": "{% if config.get('materialized') == 'table' %}1{% endif %}SELECT 1",
    "set_sql_header": "{% call set_sql_header(config) %}SET x = 1;{% endcall %}SELECT 1",
    "註冊表(同名區域變數)": "{% set config = {\n 'A': {'t': '1'}\n} %}\n{{ return(config) }}",
    "macro 內註冊表": "{% macro get_cfg2() %}\n{% set config = {'A': 1} %}\n"
                   "{{ return(config['A']) }}\n{% endmacro %}{{ get_cfg2() }}",
    "macro 參數名為 config": "{% macro takes(config) %}{{ config['a'] }}{% endmacro %}{{ takes({'a': 1}) }}",
    "context 取用已知 macro": "{{ context['flag_small']('ALPHA') }}{{ context.flag_large('ALPHA') }}"
                           "{{ builtins['ref']('BETA') }}SELECT 1",
    "dispatch 指定套件": "{{ adapter.dispatch('fmt_amount', 'segcra_impact')('a') }}SELECT 1",
    "is_incremental": "{% if is_incremental() %}WHERE 1=1{% endif %}SELECT 1",
    "註冊表取值": "{% set cfg = get_rules()[rule_name] %}{{ cfg['large'] }}{{ cfg[k] }}SELECT 1",
    "字典取值": "{% set rules = {'a': 1} %}{{ rules[k] }}{{ rules['a'] }}SELECT 1",
    "依屬性排序": "{% for c in cols|sort(attribute='name') %}{{ c.name }}{% endfor %}SELECT 1",
    "map 屬性": "{{ cols|map(attribute='name')|join(', ') }}SELECT 1",
    "map filter": "{{ cols|map('upper')|join(', ') }}SELECT 1",
    "selectattr": "{{ cols|selectattr('is_numeric')|list }}SELECT 1",
    "索引與查詢結果": "{{ x[0] }}{% if execute %}{{ run_query('select 1').columns[0].values() }}{% endif %}"
                "SELECT 1",
    "join 分隔符為底線": "{{ ['a', 'b']|join('_') }}{{ cols|join(', ', 'name') }}SELECT 1",
    "位置參數的一般用法": "{{ names|sort(true) }}{{ vals|sum }}{{ rows|groupby('region')|list }}"
                   "{{ xs|unique(false, 'name')|list }}{{ xs|max(false, 'amount') }}SELECT 1",
}


@pytest.mark.parametrize("name", sorted(ORDINARY_MODELS))
def test_ordinary_model_constructs_are_not_flagged(name):
    files = dict(STATIC_FILES, **{"models/H.sql": ORDINARY_MODELS[name]})
    report = _analyze([DEAD], files=files)
    assert not report.needs_human, report.uncertain
    assert report.affected_models == ()


ORDINARY_YML = {
    "hook 寫死": 'models:\n  segcra_impact:\n    +post-hook: "{{ audit_hook() }}"\n',
    "跨段 if": "on-run-end:\n  - \"{% if target.name == 'prod' %}grant select on x to y{% endif %}\"\n",
    "flow mapping 與 env_var": "models:\n  segcra_impact:\n    +persist_docs: {relation: true, columns: true}\n"
                               "    +schema: \"{{ env_var('DBT_SCHEMA', 'dbo') }}\"\n",
    "正規表達式中的反斜線": "models:\n  - name: BETA\n    columns:\n      - name: c\n        tests:\n"
                  "          - accepted_values:\n              values: ['^\\d+$', '\\s\\.']\n",
    "Windows 路徑與不完整的跳脫": "models:\n  segcra_impact:\n    +meta: {share: 'C:\\Users\\x\\data', "
                            "note: '\\xyz \\u12 \\U1234'}\n",
}


@pytest.mark.parametrize("yml_path", ["dbt_project.yml", "models/_props.yml"])
@pytest.mark.parametrize("name", sorted(ORDINARY_YML))
def test_ordinary_yml_is_not_flagged(name, yml_path):
    files = dict(STATIC_FILES)
    files[yml_path] = files.get(yml_path, "") + ORDINARY_YML[name]
    report = _analyze([DEAD], files=files)
    assert not report.needs_human, report.uncertain
    assert report.affected_models == ()


def test_model_only_change_does_not_trigger_macro_analysis():
    """只改 model:與 macro 反查無關的不確定因素不可觸發 needs_human。"""
    files = dict(FILES, **{"macros/broken.sql": "{% macro broken( %}"})
    report = _analyze(["models/EPSILON.sql"], files=files)
    assert report.changed_models == ("models/EPSILON.sql",)
    assert report.changed_macros == ()
    assert not report.needs_human


def test_deleted_macro_is_found_through_base_files():
    """macro 被刪除後,呼叫它的 model 會編譯失敗 → 必須透過變更前內容找出來。"""
    files = dict(FILES)
    base = {"macros/logic/flags.sql": files["macros/logic/flags.sql"]}
    files["macros/logic/flags.sql"] = "{# flag_large 與 flag_small 都被刪掉了 #}"
    report = analyze_macro_impact(files, ["macros/logic/flags.sql"], base_files=base)
    assert set(report.changed_macros) == {"flag_large", "flag_small"}
    assert {"models/mrt_ALPHA.sql", "models/GAMMA.sql"} <= _certain(report)


def test_deleted_macro_file_without_base_fails_closed():
    files = {p: t for p, t in FILES.items() if p != "macros/logic/flags.sql"}
    report = analyze_macro_impact(files, ["macros/logic/flags.sql"])
    assert report.all_models_possibly_affected
    assert report.needs_human


def test_missing_base_files_is_uncertain():
    report = analyze_macro_impact(FILES, ["macros/hooks/audit.sql"])
    assert "models/GAMMA.sql" in _certain(report)
    assert any("變更前" in r for r in report.uncertain)


def test_base_files_missing_one_changed_file_fails_closed():
    """有給 base_files 卻缺了被改的 macro 檔:不可當成新增檔,否則改名前的 macro
    名稱不會被納入,還在呼叫舊名的 model 會靜靜漏判。"""
    new = {"macros/logic.sql": "{% macro new_name() %}1{% endmacro %}",
           "models/mrt_X.sql": "SELECT {{ old_name() }}"}   # 仍在呼叫改名前的 old_name
    report = analyze_macro_impact(new, ["macros/logic.sql"],
                                  base_files={"models/mrt_X.sql": "SELECT 1"})
    assert report.needs_human
    assert report.all_models_possibly_affected
    assert any("未被宣告為新增檔" in r for r in report.uncertain)
    assert "models/mrt_X.sql" in _all(report)


def test_declared_added_file_needs_no_base_content():
    """明確宣告是新增檔時,沒有變更前內容是正常的,不該標為不確定。"""
    files = {"macros/logic.sql": "{% macro new_one() %}1{% endmacro %}",
             "models/mrt_X.sql": "SELECT 1"}
    report = analyze_macro_impact(files, ["macros/logic.sql"], base_files={},
                                  added_paths=["macros/logic.sql"])
    assert not report.needs_human
    assert report.affected_models == ()


def test_renamed_macro_is_traced_when_base_is_complete():
    """對照組:base_files 完整時,呼叫舊名稱的 model 要被找出來。"""
    old = {"macros/logic.sql": "{% macro old_name() %}1{% endmacro %}"}
    new = {"macros/logic.sql": "{% macro new_name() %}1{% endmacro %}",
           "models/mrt_X.sql": "SELECT {{ old_name() }}"}
    report = analyze_macro_impact(new, ["macros/logic.sql"], base_files=old)
    assert set(report.changed_macros) == {"old_name", "new_name"}
    assert "models/mrt_X.sql" in _certain(report)
    assert not report.needs_human


# ---------------------------------------------------------------- 找不到 model 時不可判成沒有影響
@pytest.mark.parametrize("files, kwargs, expect_reason", [
    # model 不在 model 目錄下(例如範例專案原本的結構)
    ({"macros/logic.sql": "{% macro f() %}1{% endmacro %}",
      "code/mrt_X.sql": "SELECT {{ f() }}"}, {}, "不在 model / macro 目錄內"),
    # 呼叫端只傳了 macro 檔,一個 model 都沒傳
    ({"macros/logic.sql": "{% macro f() %}1{% endmacro %}"}, {}, "找不到任何 model"),
    # model_dirs 設錯
    ({"macros/logic.sql": "{% macro f() %}1{% endmacro %}",
      "models/mrt_X.sql": "SELECT {{ f() }}"},
     {"model_dirs": ("nonexistent",)}, "不在 model / macro 目錄內"),
])
def test_macro_change_without_models_is_not_silently_clean(files, kwargs, expect_reason):
    report = analyze_macro_impact(files, ["macros/logic.sql"],
                                  base_files={"macros/logic.sql": files["macros/logic.sql"]},
                                  **kwargs)
    assert report.needs_human, "找不到 model 時不可判成沒有影響"
    assert any(expect_reason in r for r in report.uncertain), report.uncertain


@pytest.mark.parametrize("model_dirs", [("",), (".",), "", "."])
def test_project_root_can_be_the_model_dir(model_dirs):
    """範例專案的 model 就放在根目錄;macros/ 仍然要被當成 macro。"""
    files = {"macros/logic.sql": "{% macro f() %}1{% endmacro %}",
             "mrt_X.sql": "SELECT {{ f() }}"}
    report = analyze_macro_impact(files, ["macros/logic.sql"], base_files=files,
                                  model_dirs=model_dirs)
    assert _certain(report) == {"mrt_X.sql"}
    assert not report.needs_human


def test_renamed_macro_marks_callers_of_old_name():
    files = dict(FILES)
    base = {"macros/hooks/audit.sql": files["macros/hooks/audit.sql"]}
    files["macros/hooks/audit.sql"] = "{% macro audit_hook_v2() %}SELECT 1{% endmacro %}"
    report = analyze_macro_impact(files, ["macros/hooks/audit.sql"], base_files=base)
    assert "audit_hook" in report.changed_macros
    assert "models/GAMMA.sql" in _certain(report)


def test_unparsable_changed_macro_fails_closed():
    files = dict(FILES, **{"macros/hooks/audit.sql": "{% macro audit_hook( %}"})
    report = analyze_macro_impact(files, ["macros/hooks/audit.sql"], base_files={})
    assert report.all_models_possibly_affected


def test_unparsable_model_is_included_when_macros_change():
    files = dict(FILES, **{"models/BROKEN.sql": "SELECT {{ oops( }}"})
    report = _analyze(["macros/hooks/audit.sql"], files=files)
    broken = {m.path: m for m in report.affected_models}["models/BROKEN.sql"]
    assert not broken.certain
    assert report.needs_human


@pytest.mark.parametrize("path", ["dbt_project.yml", "models/sources.yml",
                                  "seeds/codes.csv", "snapshots/s.sql"])
def test_dbt_config_changes_are_uncertain(path):
    report = _analyze([path])
    assert path in report.other_changed
    assert report.needs_human


def test_unrelated_file_changes_are_not_flagged():
    report = _analyze(["README.md", "docs/notes.md"])
    assert report.other_changed == ("README.md", "docs/notes.md")
    assert not report.needs_human


def test_duplicate_macro_definition_is_uncertain():
    files = dict(FILES, **{"macros/dup.sql": "{% macro get_rules() %}{% endmacro %}"})
    report = _analyze(["macros/registry/rules.sql"], files=files)
    assert any("重複定義" in r for r in report.uncertain)


def test_report_is_deterministic():
    a = _analyze(["macros/registry/rules.sql", "macros/logic/format.sql"])
    b = _analyze(["macros/logic/format.sql", "macros/registry/rules.sql"])
    assert a == b


def test_reasons_follow_sorted_path_order():
    """說明順序不可依賴 set 的走訪順序(每個行程的雜湊種子不同,同一行程內比對抓不到)。"""
    changed = [f"models/cfg_{i:02d}.yml" for i in range(40)]
    report = _analyze(list(reversed(changed)))
    listed = [r for r in report.uncertain if "cfg_" in r]
    assert listed == sorted(listed)
    assert len(listed) == len(changed)


def test_needs_human_follows_each_flag():
    assert not ImpactReport().needs_human
    assert ImpactReport(all_models_possibly_affected=True).needs_human
    assert ImpactReport(uncertain=("x",)).needs_human


# ---------------------------------------------------------------- 範例專案(RETAIL_M1)
SAMPLE_FILES = {
    ("models/" + p.name if p.parent == SAMPLE_CODE else p.relative_to(SAMPLE_CODE).as_posix()):
        p.read_bytes().decode("utf-8")
    for p in SAMPLE_CODE.rglob("*.sql")
}


@pytest.mark.parametrize("macro_file", ["macros/anomaly/config.sql", "macros/anomaly/logic.sql"])
def test_sample_config_and_logic_reach_model(macro_file):
    """Issue #7 驗收:只改 macro / config 註冊表的 MR,能對應到受影響的 model。"""
    report = _analyze([macro_file], files=SAMPLE_FILES)
    assert _certain(report) == {"models/mrt_RETAIL_M1.sql"}
    assert not report.needs_human


def test_sample_unused_macro_is_unreferenced():
    report = _analyze(["macros/global/math_utils.sql"], files=SAMPLE_FILES)
    assert report.unreferenced_macros == ("safe_divide",)
    assert report.affected_models == ()


def test_sample_builtin_override_marks_all_models():
    """範例中的 make_temp_relation 覆寫了 dbt 內建:dbt 執行 incremental model 時會呼叫。"""
    assert "make_temp_relation" in BUILTIN_MACROS
    report = _analyze(["macros/global/override_temp_relation.sql"], files=SAMPLE_FILES)
    assert report.all_models_possibly_affected
    assert _all(report) == {"models/mrt_RETAIL_M1.sql"}


def test_sample_macro_change_maps_to_spec():
    """反查到的 model 再對應規格:mrt_RETAIL_M1 → specs/RETAIL_M1.md。"""
    report = _analyze(["macros/anomaly/config.sql"], files=SAMPLE_FILES)
    matches = resolve_specs([m.path for m in report.affected_models],
                            ["specs/RETAIL_M1.md", "specs/R-201.md"])
    assert [(s.model_path, s.spec_path, s.method) for s in matches] == \
           [("models/mrt_RETAIL_M1.sql", "specs/RETAIL_M1.md", "path")]


# ---------------------------------------------------------------- 規格對應
SPECS = ["specs/RETAIL_M1.md", "specs/ALPHA.md", "specs/R-201.md", "specs/R-305.md",
         "specs/mrt_BOTH.md", "specs/BOTH.md", "specs/0042_sample.md"]


@pytest.mark.parametrize("model, codes, spec, method", [
    ("models/mrt_RETAIL_M1.sql", (), "specs/RETAIL_M1.md", "path"),       # Issue #7 範例
    ("mrt_RETAIL_M1.sql", (), "specs/RETAIL_M1.md", "path"),              # 不在子目錄
    ("models/sub/mrt_ALPHA.sql", (), "specs/ALPHA.md", "path"),
    ("models/mrt_BOTH.sql", (), "specs/mrt_BOTH.md", "path"),             # 完整檔名優先
    ("models/0042_sample.sql", (), "specs/0042_sample.md", "path"),         # 數字開頭的檔名
    ("models/UNKNOWN.sql", ("R-201",), "specs/R-201.md", "rule_code"),    # R 編號備援
    ("models/UNKNOWN.sql", "R-201", "specs/R-201.md", "rule_code"),       # 傳單一字串
    ("models/UNKNOWN.sql", ("R-999",), None, "none"),                     # R 編號沒有對應檔
    ("models/UNKNOWN.sql", (), None, "none"),
    ("models/UNKNOWN.sql", ("R-201", "R-305"), None, "ambiguous"),        # 不自行擇一
    ("models/UNKNOWN.sql", ("R-201", "R-201"), "specs/R-201.md", "rule_code"),
])
def test_resolve_spec(model, codes, spec, method):
    match = resolve_spec(model, SPECS, codes)
    assert (match.spec_path, match.method) == (spec, method)


def test_path_mapping_wins_but_conflict_is_noted():
    match = resolve_spec("models/mrt_ALPHA.sql", SPECS, ("R-305",))
    assert match.spec_path == "specs/ALPHA.md"
    assert any("R-305" in n for n in match.notes)


def test_case_only_match_is_not_used():
    """GitLab 路徑區分大小寫:只有大小寫不同時不採用,但要提示人工確認。"""
    match = resolve_spec("models/mrt_retail_m1.sql", SPECS)
    assert match.spec_path is None
    assert any("大小寫" in n for n in match.notes)


def test_earlyjob_is_not_guessed():
    """earlyjob model 該對應哪份規格尚未確認:不自行猜測。"""
    match = resolve_spec("models/mrt_RETAIL_M1_earlyjob.sql", SPECS)
    assert match.spec_path is None


@pytest.mark.parametrize("bad_code", ["R-1", "R-12345", "r-201", "R-20a", "R-２０１", " R-201",
                                      "R-201/../x", "../R-201", 201, None])
def test_invalid_rule_codes_are_ignored(bad_code):
    assert resolve_spec("models/UNKNOWN.sql", SPECS + [f"specs/{bad_code}.md"],
                        (bad_code,)).spec_path is None


@pytest.mark.parametrize("model", [
    "models/../../config/gitlab.env", "/etc/passwd", "models\\x.sql", "C:/x.sql",
    "models/x.sql:stream", "models//x.sql", "./models/x.sql", "models/x\u202e.sql",
    "models/x\x00.sql", "", None, 42, "a" * 2000,
])
def test_unsafe_model_paths_are_rejected(model):
    match = resolve_spec(model, SPECS)
    assert match.spec_path is None
    assert all(ch.isprintable() for ch in match.model_path)


@pytest.mark.parametrize("model", ["models/x-y.sql", "models/x.y.sql", "models/表.sql",
                                   "models/x.txt", "models/.sql"])
def test_unusual_model_names_do_not_map(model):
    assert resolve_spec(model, SPECS + ["specs/x-y.md", "specs/表.md"]).spec_path is None


def test_only_existing_specs_are_returned():
    """絕不回傳清單以外的路徑(不自行拼路徑去讀檔)。"""
    assert resolve_spec("models/mrt_RETAIL_M1.sql", []).spec_path is None
    assert resolve_spec("models/mrt_RETAIL_M1.sql", ["specs/../specs/RETAIL_M1.md"]).spec_path is None


@pytest.mark.parametrize("specs_dir", ["../specs", "/specs", "specs\\x", ""])
def test_unsafe_specs_dir_is_rejected(specs_dir):
    assert resolve_spec("models/mrt_RETAIL_M1.sql", SPECS, specs_dir=specs_dir).spec_path is None


def test_spec_match_is_frozen():
    match = resolve_spec("models/mrt_RETAIL_M1.sql", SPECS)
    assert isinstance(match, SpecMatch)
    with pytest.raises(Exception):
        match.spec_path = "specs/other.md"


# ---------------------------------------------------------------- 資安
@pytest.mark.parametrize("path", [
    "", "/abs/x.sql", "macros/../x.sql", "macros//x.sql", "./macros/x.sql", "macros\\x.sql",
    "C:/macros/x.sql", "macros/x.sql:ads", "macros/x\x00.sql", "macros/x\u202e.sql",
    "macros/x\u200b.sql", "macros/x\n.sql", "a" * 1025, None, 3, b"macros/x.sql",
])
def test_normalize_path_rejects_unsafe(path):
    assert normalize_path(path) is None


@pytest.mark.parametrize("path", ["macros/x.sql", "models/sub dir/中文.sql", "a", "a/b/c.yml"])
def test_normalize_path_accepts_ordinary(path):
    assert normalize_path(path) == path


def test_unsafe_changed_path_fails_closed():
    report = _analyze(["macros/../../config/gitlab.env"])
    assert report.all_models_possibly_affected
    assert report.rejected_paths == 1
    assert all("gitlab.env" not in r for r in report.uncertain), "不合法路徑不可原樣寫進說明"


def test_unsafe_project_file_paths_are_skipped_when_macros_change():
    files = dict(FILES, **{"macros/../evil.sql": "{% macro x() %}{% endmacro %}"})
    report = _analyze(["macros/hooks/audit.sql"], files=files)
    assert report.rejected_paths == 1
    assert report.all_models_possibly_affected


@pytest.mark.parametrize("payload", [
    "{{ ''.__class__.__mro__[1].__subclasses__() }}",
    "{% for i in range(10**12) %}{% endfor %}",
    "{{ 9 ** 9 ** 9 ** 9 }}",
    "{% include '../../config/gitlab.env' %}",
    "{{ env_var('PATH') }}",
])
def test_templates_are_never_executed(payload):
    """只解析不執行:這些樣板若被執行會丟 SecurityError、卡住或讀檔;分析必須立即正常結束。"""
    files = dict(FILES, **{"macros/evil.sql": "{% macro evil() %}" + payload + "{% endmacro %}",
                           "models/USES_EVIL.sql": "SELECT {{ evil() }}"})
    t = time.monotonic()
    report = _analyze(["macros/evil.sql"], files=files)
    assert time.monotonic() - t < 5
    assert "models/USES_EVIL.sql" in _certain(report)


def test_analysis_does_not_touch_filesystem(tmp_path, monkeypatch):
    """分析只看傳入的內容:即使路徑與磁碟上的檔案同名,也不會去讀。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "real.sql").write_text("{% macro from_disk() %}{% endmacro %}",
                                                   encoding="utf-8")
    report = analyze_macro_impact({}, ["macros/real.sql"], base_files={})
    assert "from_disk" not in report.changed_macros
    assert report.all_models_possibly_affected


@pytest.mark.parametrize("source", [
    "{% materialization noop, default %}",                   # 缺結束標籤
    "{% materialization noop, default",                      # 標籤本身也沒結束
    "{% test not_negative(model, column_name) %} SELECT 1",
    "{% snapshot s %}",
    "{% docs d %}",
    "{% materialization %}{% endmaterialization %}",
])
def test_unterminated_dbt_tags_do_not_hang(source):
    """自訂標籤解析器必須在檔案結尾停止(不檢查 eof 會無窮迴圈)。"""
    files = dict(FILES, **{"macros/bad.sql": source})
    t = time.monotonic()
    report = _analyze(["macros/bad.sql"], files=files)
    assert time.monotonic() - t < 5
    assert report.needs_human


def test_deep_nesting_fails_closed():
    deep = "{{ " + "(" * 5000 + "1" + ")" * 5000 + " }}"
    files = dict(FILES, **{"macros/deep.sql": "{% macro d() %}" + deep + "{% endmacro %}"})
    report = _analyze(["macros/deep.sql"], files=files)
    assert report.all_models_possibly_affected


def test_nested_template_strings_are_depth_limited():
    """字串中的樣板裡再藏字串樣板……超過深度即視為無法解析(保守)。"""
    inner = "{{ audit_hook() }}"
    for _ in range(dbt_impact.MAX_NESTED_TEMPLATE_DEPTH + 2):
        inner = "{{ config(post_hook=" + json.dumps(inner) + ") }}"
    files = dict(FILES, **{"models/NESTED.sql": inner})
    report = _analyze(["macros/hooks/audit.sql"], files=files)
    nested = {m.path: m for m in report.affected_models}["models/NESTED.sql"]
    assert not nested.certain
    assert report.needs_human


@pytest.mark.parametrize("limit, value", [
    ("MAX_FILES", 3), ("MAX_FILE_CHARS", 50), ("MAX_TOTAL_CHARS", 500), ("MAX_AST_NODES", 50),
])
def test_resource_limits_fail_closed(monkeypatch, limit, value):
    monkeypatch.setattr(dbt_impact, limit, value)
    report = _analyze(["macros/registry/rules.sql"])
    assert report.all_models_possibly_affected
    assert report.needs_human
    assert any("上限" in r for r in report.uncertain)


@pytest.mark.parametrize("limit, value, which, message", [
    ("MAX_FILE_CHARS", 30, "files", "單一檔案超過"),
    ("MAX_FILE_CHARS", 30, "base", "變更前的單一檔案超過"),
    ("MAX_TOTAL_CHARS", 300, "files", "檔案合計超過"),
    ("MAX_TOTAL_CHARS", 300, "base", "變更前的檔案合計超過"),
])
def test_each_size_limit_is_enforced_on_its_own(monkeypatch, limit, value, which, message):
    """每個上限各自生效:只讓其中一側(變更後/變更前)超過,確認是該檢查擋下。"""
    monkeypatch.setattr(dbt_impact, limit, value)
    small = {"models/A.sql": "SELECT 1", "macros/m.sql": "{% macro m() %}{% endmacro %}"}
    if limit == "MAX_TOTAL_CHARS":
        big = {f"macros/p{i}.sql": "{# " + "x" * 20 + " #}" for i in range(20)}
    else:
        big = {"macros/p.sql": "{# " + "x" * 40 + " #}"}
    files, base = (dict(small, **big), small) if which == "files" else (small, dict(small, **big))
    report = analyze_macro_impact(files, ["macros/m.sql"], base_files=base)
    assert report.all_models_possibly_affected and report.needs_human
    hits = [r for r in report.uncertain if message in r]
    assert hits, report.uncertain
    if which == "files":
        assert not any("變更前" in r for r in hits)


def test_reason_count_and_length_are_capped():
    changed = [f"models/cfg_{i}_{'x' * 400}.yml" for i in range(MAX_REASONS + 50)]
    report = _analyze(changed)
    assert len(report.uncertain) == MAX_REASONS + 1
    assert all(len(r) <= MAX_REASON_CHARS + 1 for r in report.uncertain)


def test_vulnerable_jinja_fails_closed(monkeypatch):
    monkeypatch.setattr(jinja2, "__version__", "3.1.5")
    report = _analyze(["macros/registry/rules.sql"])
    assert report.all_models_possibly_affected


@pytest.mark.parametrize("model_dirs, macro_dirs", [
    (("../models",), ("macros",)), (("models",), ("/macros",)), ((), ("macros",)),
    (("models",), ()), (("models",), ("macros\\x",)),
])
def test_unsafe_directory_settings_fail_closed(model_dirs, macro_dirs):
    report = _analyze(["macros/registry/rules.sql"], model_dirs=model_dirs, macro_dirs=macro_dirs)
    assert report.all_models_possibly_affected


@pytest.mark.parametrize("piece, repeat", [("a_b ", 250_000), ("{{", 500_000), ("\u4e2d", 1_000_000)])
def test_identifier_tokenizer_is_linear(piece, repeat):
    text = piece * repeat
    t = time.monotonic()
    _identifier_tokens(text)
    assert time.monotonic() - t < 3


def test_large_yml_hook_scan_is_fast():
    yml = "on-run-end:\n" + "".join(f"  - \"{{{{ hook_{i}() }}}}\"\n" for i in range(20_000))
    files = dict(FILES, **{"dbt_project.yml": yml})
    t = time.monotonic()
    _analyze(["macros/hooks/audit.sql"], files=files)
    assert time.monotonic() - t < 10


def test_isolated_matches_in_process():
    changed = ["macros/registry/rules.sql"]
    base = {p: FILES[p] for p in changed}
    assert analyze_macro_impact_isolated(FILES, changed, base_files=base) == \
           analyze_macro_impact(FILES, changed, base_files=base)


def test_isolated_failure_fails_closed():
    """子行程來不及完成(逾時)時,結果必須是「全部 model 可能受影響」。"""
    report = analyze_macro_impact_isolated(FILES, ["macros/registry/rules.sql"], timeout_s=0.0001)
    assert report.all_models_possibly_affected
    assert report.needs_human
    assert any("逾時" in r for r in report.uncertain)


@pytest.mark.parametrize("module, allowed", [
    ("dbt_impact.py", {"dataclasses", "jinja2", ".dbt_builtin_macros", ".dbt_render", ".isolation"}),
    ("dbt_builtin_macros.py", set()),
])
def test_module_imports_are_allowlisted(module, allowed):
    """處理不可信內容的模組不得悄悄獲得網路、指令執行、檔案讀寫等能力。"""
    imported = set()
    for node in ast.walk(ast.parse((PKG_ROOT / "orchestrator" / module).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or "").split(".")[0])
    assert imported == allowed


def test_impact_module_never_reads_files_or_renders():
    """以語法樹檢查(不用子字串比對,避免 `macros.setdefault` 這類誤判或換個寫法就漏判):
    不讀寫檔案、不執行程式碼、不渲染樣板。"""
    tree = ast.parse((PKG_ROOT / "orchestrator" / "dbt_impact.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"open", "eval", "exec", "compile", "__import__", "input", "breakpoint"}
    assert not attrs & {"read_text", "read_bytes", "write_text", "write_bytes", "open", "unlink",
                        "mkdir", "rmdir", "system", "popen", "render", "generate", "from_string",
                        "get_template", "make_module", "module"}


def test_builtin_macro_list_is_names_only():
    """內建清單只含名稱(不含程式碼),且都是合法識別字。"""
    assert len(BUILTIN_MACROS) > 400
    assert all(isinstance(n, str) and n.replace("_", "").isalnum() and n.isascii()
               for n in BUILTIN_MACROS)
    header = (PKG_ROOT / "orchestrator" / "dbt_builtin_macros.py").read_text(encoding="utf-8")[:600]
    assert "Apache" in header and "MIT" in header, "來源授權需正確註明"


# ---------------------------------------------------------------- 隨機輸入
FUZZ_MACRO_BODIES = [
    "{{ get_rules() }}", "{{ flag_large('A') }}", "{{ adapter.dispatch('fmt_amount')('x') }}",
    "{{ context[name]() }}", "{{ context['flag_small'] }}", "{% set f = flag_small %}{{ f() }}",
    "{{ return(1) }}", "{{ segcra_impact.get_rules() }}", "{{ config(post_hook=\"{{ audit_hook() }}\") }}",
    "{% if x %}", "{% endif %}", "{{ (", "}}", "{% materialization m, default %}",
    "{% endmaterialization %}", "\u202e", "\x00", "中文", "{{ '{{ oops' }}", "{% do x.append(1) %}",
]


@pytest.mark.parametrize("seed", range(6))
def test_fuzz_invariants(seed):
    """隨機產生 macro / model 與變更清單:不丟例外、輸出受限、保守結果必交人工、結果可重現。"""
    rng = random.Random(seed)
    for _ in range(25):
        files = {}
        for i in range(rng.randint(0, 5)):
            body = "".join(rng.choice(FUZZ_MACRO_BODIES) for _ in range(rng.randint(0, 4)))
            files[f"macros/m{i}.sql"] = f"{{% macro mac_{i}() %}}{body}{{% endmacro %}}"
        for i in range(rng.randint(0, 5)):
            body = "".join(rng.choice(FUZZ_MACRO_BODIES + [f"{{{{ mac_{j}() }}}}" for j in range(5)])
                           for _ in range(rng.randint(0, 4)))
            files[f"models/M{i}.sql"] = "SELECT " + body
        if rng.random() < 0.3:
            files["dbt_project.yml"] = "on-run-end: '{{ mac_0() }}'"
        candidates = list(files) + ["macros/gone.sql", "../x", "README.md", "models/a.yml"]
        changed = rng.sample(candidates, k=rng.randint(0, min(4, len(candidates))))
        base = {p: files[p] for p in changed if p in files and rng.random() < 0.7}
        report = analyze_macro_impact(files, changed, base_files=base)
        model_paths = [p for p in files if p.startswith("models/") and p.endswith(".sql")]
        _assert_report_invariants(report, model_paths)
        assert report == analyze_macro_impact(files, list(reversed(changed)), base_files=base)
