"""dbt_render 的確定性測試 — 不呼叫 LLM、不連資料庫,可進 CI。

驗的六件事:
  1. **與 dbt compile 逐字一致**:標準答案由 dbt 官方編譯產生
     (tests/dbt_reference/generate.py → tests/fixtures/dbt_compiled/<轉接器>/),
     不是我們自己寫的預期值,避免「自己改自己的考卷」。
  2. 行號對得回原始檔,而且走的是**哨兵**這條確定性路徑,不是 difflib 近似;
     行號不得超出檔案實際行數。
  3. **不猜值**:未定義的 dbt 內建、沒有預設值又沒被提供的 var、未設定 database
     卻用到 ref/source,一律硬失敗。靜靜產出錯的 SQL 比展開失敗危險得多。
  4. **資安**:沙箱逃逸(含已知 CVE 手法)、讀檔、環境變數、識別字注入、覆寫內建、
     偽造哨兵、資訊洩漏、資源耗盡,都必須被擋下。
  5. **隨機輸入**:以固定種子產生大量怪異樣板,驗證不變條件永遠成立。
  6. 失敗時收斂成 ok=False,不丟例外、不產出半套 SQL。

測試資料全為去識別化的假名(資料庫 SAMPLE_DW、表 T_SAMPLE_*)。
"""
import ast
import importlib.util
import json
import os
import pathlib
import random
import time

import jinja2
import pytest

from orchestrator import dbt_render
from orchestrator.dbt_render import (
    MAX_ERROR_CHARS, RenderResult, _count_lines, _jinja_version_ok, _lines_inside_tag,
    _map_from_difflib, _mask_tags, build_env, is_dbt_template, render_model,
    render_model_isolated,
)
from orchestrator.isolation import run_isolated

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
CODE_ROOT = PKG_ROOT / "examples" / "sample" / "RETAIL_M1_code"
MODEL = CODE_ROOT / "mrt_RETAIL_M1.sql"
REFERENCE_PROJECT = PKG_ROOT / "tests" / "dbt_reference"
COMPILED_DIR = PKG_ROOT / "tests" / "fixtures" / "dbt_compiled"
DBT_RENDER_PY = PKG_ROOT / "orchestrator" / "dbt_render.py"

# 對照原始碼的定義與產生標準答案的腳本共用(同一份,不會兩邊各寫一套)
_spec = importlib.util.spec_from_file_location("dbt_reference_generate",
                                               REFERENCE_PROJECT / "generate.py")
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)
REFERENCE_SOURCES = reference.reference_sources()

# 去識別化的資料庫名,與 tests/dbt_reference/profiles.yml 一致
SAMPLE_DB = "SAMPLE_DW"

# 原始檔裡這幾行是 macro 呼叫 / ref 呼叫,行號對應要指得回來
SRC_INWARD_LARGE = 70          # , SUM(CASE WHEN {{ is_inward_large('RETAIL_M1') }} ...
SRC_KIOSK_OUTWARD = 77         # , SUM(CASE WHEN {{ is_kiosk_outward_all('RETAIL_M1') }} ...
SRC_REF_TXN_LOG = 35           # FROM {{ ref('txn_log_net') }}
SRC_REF_EARLYJOB = 165         # FROM {{ ref('retail_m1_earlyjob') }} e
SRC_CONFIG_START = 12          # {{ config(  ← 跨行標記,續行是 13-16

# 這份 model 第 167 行是 `{{ var("target_date") }}`(無預設值),dbt 沒設定變數時
# 也會編譯失敗 → 渲染一律要明確帶入變數。與 generate.py 的 --vars 一致。
SAMPLE_VARS = {"target_date": "2026-02-01"}

# 巢狀迴圈:沙箱的 range 上限擋不住(每層都在上限內),只能靠行程逾時
RUNAWAY_LOOP = ("{% for i in range(100000) %}{% for j in range(100000) %}"
                "{% endfor %}{% endfor %}")


def _render(**kwargs) -> RenderResult:
    """以範例的標準設定展開;個別測試可覆寫任何參數。"""
    params = dict(model_path=MODEL, code_root=CODE_ROOT,
                  variables=SAMPLE_VARS, database=SAMPLE_DB)
    params.update(kwargs)
    return render_model(**params)


def _render_reference(name: str) -> RenderResult:
    """以產生標準答案時相同的條件展開(同一組 macro、var、database)。"""
    return _render(model_path=f"{name}.sql", source=REFERENCE_SOURCES[name])


@pytest.fixture(scope="module")
def result() -> RenderResult:
    r = _render()
    assert r.ok, f"展開失敗:{r.error}"
    return r


def _find(sql: str, needle: str) -> int:
    """回傳第一個含 needle 的渲染行號(1-based);找不到直接讓測試失敗。"""
    for idx, line in enumerate(sql.split("\n"), start=1):
        if needle in line:
            return idx
    pytest.fail(f"渲染輸出裡找不到 {needle!r}")


def _compiled_targets() -> list[pathlib.Path]:
    return sorted(p for p in COMPILED_DIR.iterdir() if p.is_dir())


def _write_macro(root: pathlib.Path, filename: str, text: str) -> pathlib.Path:
    (root / "macros").mkdir(parents=True, exist_ok=True)
    (root / "macros" / filename).write_text(text, encoding="utf-8")
    return root


def _assert_invariants(r: RenderResult, text: str) -> None:
    """任何輸入都必須成立的不變條件。"""
    assert isinstance(r.ok, bool)
    if not r.ok:
        assert r.sql == "" and r.line_map == {}
        assert r.error and len(r.error) <= MAX_ERROR_CHARS
        assert all(ch.isprintable() for ch in r.error), "錯誤訊息含控制字元"
        return
    assert r.error is None
    assert r.source_lines == _count_lines(text)
    assert r.rendered_lines == _count_lines(r.sql)
    assert "\r" not in r.sql, "Jinja 應把換行統一成 \\n"
    if r.map_method == "none":
        assert r.line_map == {}
    else:
        assert set(r.line_map) == set(range(1, r.rendered_lines + 1))
    assert all(0 <= v <= r.source_lines for v in r.line_map.values())
    if "__SEGCRA_" not in text:
        assert "__SEGCRA_" not in r.sql, "哨兵標記外洩到展開結果"


# ---------------------------------------------------- 與 dbt compile 逐字對照
def test_reference_fixtures_exist():
    """每個對照原始碼在每個轉接器底下都要有標準答案,否則對照測試等於沒跑。

    新增探測檔卻忘了重跑 generate.py 時,這條會失敗。
    """
    targets = _compiled_targets()
    assert targets, f"{COMPILED_DIR} 底下沒有任何 dbt 編譯結果,請執行 generate.py"
    assert len(REFERENCE_SOURCES) >= 2
    for t in targets:
        assert {p.stem for p in t.glob("*.sql")} == set(REFERENCE_SOURCES), \
            f"{t.name} 的標準答案與對照原始碼不一致"


@pytest.mark.parametrize(
    "target, name",
    [(t, n) for t in _compiled_targets() for n in REFERENCE_SOURCES],
    ids=lambda v: v.name if isinstance(v, pathlib.Path) else v)
def test_matches_dbt_compile(target, name):
    """展開結果必須與 dbt 官方編譯結果**逐字相同**(Issue #7 的驗收條件)。

    涵蓋範例 model 與各探測檔:
      probe_source       source() 的完整名稱格式
      probe_whitespace   檔案開頭空行、{%- -%} 空白控制、if/else 區塊、檔尾空行
      probe_strip        dbt 去的是「原始檔」頭尾空白,不是輸出的
      probe_control      do、continue、macro 預設參數、有預設值的 var、execute
      probe_incremental  is_incremental() 在 compile 期為 False
      probe_bom          帶 BOM 的檔案(dbt 會把 BOM 保留在輸出裡)
      probe_crlf         CRLF 換行的檔案(Windows 編輯器常見)
    不一致代表我們與 dbt 行為有落差;若是原始檔改了,請重跑 generate.py。
    """
    r = _render_reference(name)
    assert r.ok, r.error
    # 標準答案不含 \r(見 generate.py);以文字模式讀,不受 git 換行設定影響
    expected = (target / f"{name}.sql").read_text(encoding="utf-8")
    assert r.sql == expected


@pytest.mark.parametrize("name", list(REFERENCE_SOURCES))
def test_reference_line_maps_are_exact_and_in_range(name):
    """每個對照檔都要走哨兵路徑,且行號對應完整、不越界。"""
    text = REFERENCE_SOURCES[name]
    r = _render_reference(name)
    assert r.ok, r.error
    assert r.map_method == "sentinel"
    _assert_invariants(r, text)


@pytest.mark.parametrize("name", ["probe_whitespace", "probe_crlf"])
def test_leading_blank_lines_offset_line_numbers(name):
    """dbt 會去掉原始檔開頭空行再渲染 → 行號必須加回被去掉的行數。

    probe_whitespace 開頭有 2 行空白,展開後第 1 行其實是原始檔第 5 行。
    沒加回的話,所有留言都會往上偏 2 行。CRLF 版本必須得到相同的行號。
    """
    r = _render_reference(name)
    assert r.sql.split("\n")[0] == "SELECT 2 AS n"
    assert r.src_line(1) == 5
    assert r.src_line(_find(r.sql, "'many' AS label")) == 7
    assert r.src_line(_find(r.sql, '."txn_log_net"')) == 11


# ------------------------------------------------------------------ 展開正確性
def test_no_template_residue(result):
    """輸出不能還留著 Jinja 標記——留著就代表沒展開乾淨。"""
    assert "{{" not in result.sql
    assert "{%" not in result.sql
    assert not is_dbt_template(result.sql)


def test_macros_loaded(result):
    """macros/ 底下的 macro 都要被載到,而且範例本身不能有同名衝突。"""
    assert "get_config" in result.macros
    assert "is_inward_large" in result.macros
    assert result.macro_conflicts == []


def test_macro_body_expanded(result):
    """關鍵:門檻值與 op_code 白名單要真的展開出來,規則層才看得到。"""
    flat = " ".join(result.sql.split())
    assert "direction_flag = '0'" in flat
    assert "AND amount > 1000" in flat
    assert "AND op_code IN ('OP01','OP02','OP03')" in flat      # 入帳白名單
    assert "AND op_code IN ('OP11','OP12')" in flat             # 自助設備出帳
    assert "AND op_code IN ('OP21','OP22','OP23')" in flat      # 出帳白名單


def test_get_config_returns_dict():
    """dbt 的 {{ return(dict) }} 要回傳真的 dict,不是渲染後的字串。

    這是整個做法的成敗點:拿到字串的話 cfg['inward_codes'] 會直接炸。
    """
    env, _, _ = build_env(CODE_ROOT)
    cfg = env.globals["get_config"]()
    assert isinstance(cfg, dict)
    assert cfg["RETAIL_M1"]["inward_threshold"] == "1000"


def test_ref_renders_qualified_name(result):
    """ref() 比照正式環境展開成 "<database>"."dbo"."<表>"。"""
    assert f'FROM "{SAMPLE_DB}"."dbo"."txn_log_net"' in result.sql
    assert f'FROM "{SAMPLE_DB}"."dbo"."retail_m1_earlyjob" e' in result.sql


def test_schema_can_be_overridden():
    r = _render(source="SELECT * FROM {{ ref('t') }}", schema="stage")
    assert r.ok
    assert r.sql == f'SELECT * FROM "{SAMPLE_DB}"."stage"."t"'


def test_ref_two_argument_form():
    """ref('套件', 'model') 形式:表名取最後一個參數。"""
    r = _render(source="{{ ref('pkg', 't') }}")
    assert r.ok
    assert r.sql == f'"{SAMPLE_DB}"."dbo"."t"'


def test_target_is_readable():
    r = _render(source="{{ target.database }}.{{ target.schema }}")
    assert r.ok
    assert r.sql == f"{SAMPLE_DB}.dbo"


def test_render_is_deterministic():
    """同樣輸入必須逐字相同——否則沒辦法拿去跟 dbt compile 對照。

    哨兵權杖每次隨機,但行號對應不得因此改變。
    """
    a, b = _render(), _render()
    assert a.sql == b.sql
    assert a.line_map == b.line_map


def test_macro_name_conflict_is_reported(tmp_path):
    """同名 macro 只能擇一,結果可能與 dbt 不同 → 必須回報,不可靜靜吞掉。"""
    _write_macro(tmp_path, "a_first.sql", "{% macro dup() %}FIRST{% endmacro %}")
    _write_macro(tmp_path, "z_second.sql", "{% macro dup() %}SECOND{% endmacro %}")
    _, macros, conflicts = build_env(tmp_path)
    assert macros.count("dup") == 1, "同名不應在清單裡重複出現"
    assert len(conflicts) == 1
    assert "dup" in conflicts[0]
    assert "a_first.sql" in conflicts[0] and "z_second.sql" in conflicts[0]


# ------------------------------------------------------------------ 不猜值
def test_model_needs_vars_like_dbt_does():
    """不給變數時要跟 dbt 一樣失敗,不可用猜來的預設值硬渲染過去。

    本檔 L167 的 `var("target_date")` 沒有預設值。dbt 的 var() 看不到 L21 的
    `{% set %}`,所以 dbt 在沒有 --vars 時同樣編譯失敗。
    """
    r = _render(variables=None)
    assert not r.ok
    assert "target_date" in r.error


def test_var_without_default_fails_loudly():
    """沒有預設值又沒被提供的 var 必須硬失敗,不可渲染成 `WHERE d = None`。"""
    r = _render(source='SELECT * FROM t WHERE d = {{ var("nope") }}')
    assert not r.ok
    assert "nope" in r.error
    assert "None" not in r.sql


def test_var_with_default_is_used():
    r = _render(source='SELECT {{ var("nope", "FALLBACK") }}')
    assert r.ok
    assert r.sql == "SELECT FALLBACK"


def test_var_default_and_override():
    default = _render()
    assert "'2026-02-01'" in default.sql
    custom = _render(variables={"target_date": "2026-03-15"})
    assert "'2026-03-15'" in custom.sql
    assert "'2026-02-01'" not in custom.sql


def test_ref_without_database_fails_loudly():
    """沒給 database 卻用到 ref():dbt 由 profile 決定資料庫名,這裡同樣不猜。"""
    r = _render(database=None)
    assert not r.ok
    assert "database" in r.error


def test_this_is_not_silently_stubbed():
    """`{{ this }}` 未支援時必須失敗,不可輸出一個假的表名混進 SQL。"""
    r = _render(source="SELECT * FROM {{ this }}")
    assert not r.ok
    assert r.sql == ""


# ---------------------------------------------------------------------- 資安
# --- 沙箱逃逸 ---
# 這裡渲染的是**待審的 MR 內容**。用一般的 jinja2.Environment 的話,下列樣板
# 可以在審查機上取得任意程式執行(已實測:成功執行 os.getcwd())。
SSTI_PAYLOADS = [
    "{{ ''.__class__.__mro__ }}",
    "{{ ''.__class__.__mro__[1].__subclasses__() | length }}",
    "{{ config.__globals__ }}",
    "{{ ref.__globals__ }}",
    ("{% for c in ''.__class__.__mro__[1].__subclasses__() %}"
     "{% if c.__name__ == 'catch_warnings' %}"
     "{{ c()._module.__builtins__['__import__']('os').getcwd() }}"
     "{% endif %}{% endfor %}"),
    "{% do ''.__class__.__mro__[1].__subclasses__() %}",
    # CVE-2024-56326(間接取得 str.format)與 CVE-2025-27516(|attr 取得 format)的手法
    '{{ "{0.__class__}".format(1) }}',
    '{% set f = "{0.__class__.__mro__}".format %}{{ f(1) }}',
    '{% set f = "{0.__init__.__globals__}"|attr("format") %}{{ f(ref) }}',
    '{{ ("{0.__class__}"|attr("format"))(1) }}',
    '{% set fm = "{a.__class__}".format_map %}{{ fm({"a": 1}) }}',
    '{{ "%s"|format(ref.__globals__) }}',
]


@pytest.mark.parametrize("payload", SSTI_PAYLOADS)
def test_template_injection_is_blocked(payload):
    """惡意 MR 樣板不得取得任意程式執行或內部物件。"""
    r = _render(source=payload)
    assert not r.ok, f"樣板注入沒有被擋下:{payload!r} -> {r.sql!r}"
    assert r.sql == ""


def test_environment_is_sandboxed():
    from jinja2.sandbox import SandboxedEnvironment

    env, _, _ = build_env(CODE_ROOT)
    assert isinstance(env, SandboxedEnvironment)


def test_sandbox_does_not_break_legitimate_rendering(result):
    flat = " ".join(result.sql.split())
    assert "AND amount > 1000" in flat
    assert "AND op_code IN ('OP01','OP02','OP03')" in flat


# --- 沙箱本身的版本 ---
@pytest.mark.parametrize("version, ok", [
    ("3.1.4", False), ("3.1.5", False), ("3.0.3", False), ("2.11.3", False),
    ("3.1.6", True), ("3.1.10", True), ("3.2.0", True), ("4.0.0a1", True),
])
def test_jinja_version_gate(version, ok):
    """3.1.5 之前有 CVE-2024-56326、3.1.6 之前有 CVE-2025-27516,都是沙箱逃逸。"""
    assert _jinja_version_ok(version) is ok


def test_refuses_to_render_with_vulnerable_jinja(monkeypatch):
    """裝到有漏洞的 jinja2 時必須拒絕展開(fail closed),不可照常執行。"""
    monkeypatch.setattr(jinja2, "__version__", "3.1.5")
    r = _render(source="SELECT 1")
    assert not r.ok
    assert "沙箱逃逸" in r.error


@pytest.mark.parametrize("req", ["requirements.txt", "requirements-dev.txt"])
def test_requirements_pin_safe_jinja(req):
    """依賴宣告本身要擋住有漏洞的版本,不能只靠執行期檢查。"""
    lines = [l.split("#")[0].strip().replace(" ", "")
             for l in (PKG_ROOT / req).read_text(encoding="utf-8").splitlines()]
    jinja_lines = [l for l in lines if l.lower().startswith("jinja2")]
    assert jinja_lines == ["jinja2>=3.1.6"]


# --- 讀檔 / 環境變數 ---
# MR 內容若能讀檔,就能把審查機上的檔案(含 config/gitlab.env 的 token、
# config/sandbox.env 的密碼)讀進「展開後的 SQL」,再隨 prompt 或 MR 留言外洩。
FILE_ACCESS_PAYLOADS = [
    "{% include 'macros/anomaly/config.sql' %}",
    "{% include 'mrt_RETAIL_M1.sql' %}",
    "{% import 'macros/anomaly/logic.sql' as m %}{{ m.is_inward_all('RETAIL_M1') }}",
    "{% from 'macros/anomaly/config.sql' import get_config %}x",
    "{% extends 'mrt_RETAIL_M1.sql' %}",
]


@pytest.mark.parametrize("payload", FILE_ACCESS_PAYLOADS)
def test_template_cannot_read_files(payload):
    r = _render(source=payload)
    assert not r.ok, f"樣板讀到了檔案:{payload!r} -> {r.sql[:80]!r}"
    assert r.sql == ""


def test_env_var_is_not_available():
    r = _render(source="SELECT '{{ env_var(\"PATH\") }}'")
    assert not r.ok
    assert r.sql == ""


def test_source_mode_without_code_root_loads_no_macros():
    """傳 source= 卻沒指定 code_root 時,不可自行猜一個目錄去載 macro。"""
    r = render_model(MODEL, source="{{ get_config() }}", database=SAMPLE_DB)
    assert not r.ok
    assert "get_config" in r.error


def test_symlinked_macro_files_are_skipped(tmp_path):
    """macro 目錄內的符號連結可能指向任意檔案,一律略過(Linux CI 上實際建立連結)。"""
    outside = tmp_path / "outside.sql"
    outside.write_text("{% macro leaked() %}SECRET{% endmacro %}", encoding="utf-8")
    (tmp_path / "proj" / "macros").mkdir(parents=True)
    try:
        (tmp_path / "proj" / "macros" / "link.sql").symlink_to(outside)
    except OSError:
        pytest.skip("此環境無法建立符號連結(Windows 需要額外權限)")
    _, macros, _ = build_env(tmp_path / "proj")
    assert "leaked" not in macros


def test_symlink_check_is_applied(tmp_path, monkeypatch):
    """同上,但不依賴作業系統建立符號連結的權限。"""
    _write_macro(tmp_path, "link.sql", "{% macro leaked() %}SECRET{% endmacro %}")
    _write_macro(tmp_path, "real.sql", "{% macro kept() %}OK{% endmacro %}")
    original = pathlib.Path.is_symlink
    monkeypatch.setattr(pathlib.Path, "is_symlink",
                        lambda self: self.name == "link.sql" or original(self))
    _, macros, _ = build_env(tmp_path)
    assert macros == ["kept"]


# --- 識別字注入 ---
IDENT_INJECTIONS = [
    "{{ ref('x\"; DROP TABLE t; --') }}",
    "{{ ref('x].[y') }}",
    "{{ ref('a b') }}",
    "{{ ref('') }}",
    "{{ ref('表') }}",
    "{{ ref('t\\u200b') }}",
    "{{ source('raw', 'T\"; DELETE FROM t; --') }}",
    "{{ source('r\"aw', 'T_SAMPLE_TXN') }}",
    "{{ ref('pkg\"x', 't') }}",
    "{{ ref(1) }}",
]


@pytest.mark.parametrize("payload", IDENT_INJECTIONS)
def test_identifier_injection_is_blocked(payload):
    """不合法的表名/來源名一律拒絕,不得拼進 SQL。"""
    r = _render(source=f"SELECT * FROM {payload}")
    assert not r.ok, f"識別字注入沒有被擋下:{payload!r} -> {r.sql!r}"
    assert "不合法" in r.error
    assert r.sql == ""


@pytest.mark.parametrize("database, schema", [
    ('SAMPLE_DW"; DROP TABLE t; --', "dbo"),
    (SAMPLE_DB, 'dbo"."x'),
])
def test_database_and_schema_are_validated(database, schema):
    r = _render(database=database, schema=schema)
    assert not r.ok
    assert "不合法" in r.error


# --- 覆寫內建 ---
@pytest.mark.parametrize("name", ["ref", "source", "var", "config", "return",
                                  "env_var", "this", "execute", "range", "namespace"])
def test_macro_cannot_shadow_builtins(tmp_path, name):
    """MR 自訂與內建同名的 macro:實測可繞過識別字白名單、蓋掉呼叫端的變數值。"""
    _write_macro(tmp_path, "evil.sql",
                 f"{{% macro {name}(a='', b='') %}}{{{{ return(a) }}}}{{% endmacro %}}")
    r = _render(code_root=tmp_path, source="SELECT 1")
    assert not r.ok
    assert "內建同名" in r.error


def test_shadowed_ref_cannot_bypass_whitelist(tmp_path):
    """修正前:自訂 ref 回傳原字串,展開出 `x"; DELETE FROM t; --`。"""
    _write_macro(tmp_path, "evil.sql", "{% macro ref(n) %}{{ return(n) }}{% endmacro %}")
    r = _render(code_root=tmp_path, source="SELECT * FROM {{ ref('x\"; DELETE FROM t; --') }}")
    assert not r.ok
    assert "DELETE" not in r.sql


# --- 偽造哨兵 ---
@pytest.mark.parametrize("fake", [
    "/*__SEGCRA_SRC_L1__*/",
    "/*__SEGCRA_0000000000000000_L1__*/",
    "/*__SEGCRA_deadbeefdeadbeef_L999__*/",
])
def test_fake_sentinels_do_not_hijack_line_map(fake):
    """MR 內容寫了長得像哨兵的註解:不得改變行號,也不得讓精確對應降級。"""
    text = f"SELECT 1 {fake}\n{fake}SELECT 2\nSELECT {{{{ 3 }}}}"
    r = _render(source=text)
    assert r.ok
    assert r.sql == f"SELECT 1 {fake}\n{fake}SELECT 2\nSELECT 3"   # 原樣保留,不被吃掉
    assert r.map_method == "sentinel"
    assert r.line_map == {1: 1, 2: 2, 3: 3}


# --- 呼叫端資料 ---
def test_template_cannot_mutate_caller_variables():
    """修正前:`{% do var('codes').append(...) %}` 改到呼叫端的 list,而且因為渲染兩次被改了兩次。"""
    caller = {"target_date": "2026-02-01", "codes": ["OP01"]}
    r = _render(source="{% do var('codes').append('EVIL') %}{{ var('codes') }}",
                variables=caller)
    assert r.ok, r.error
    assert r.sql == "['OP01']"
    assert caller["codes"] == ["OP01"]
    assert r.map_method == "sentinel"


def test_target_is_read_only():
    r = _render(source="{% do target.update({'schema': 'x'}) %}{{ target.schema }}")
    assert not r.ok


# --- 資訊洩漏 ---
@pytest.mark.parametrize("payload", [
    "{{ ref }}", "{{ get_config }}", "{{ var }}", "{{ range }}", "{{ cycler(1) }}",
    "{{ [ref] }}", "{{ {'k': var} }}", '{{ ""|attr("format") }}',
])
def test_objects_are_not_printed(payload):
    """函式與物件的字串表示含記憶體位址與內部名稱,不可輸出。"""
    r = _render(source=payload)
    assert not r.ok
    assert "0x" not in r.sql


@pytest.mark.parametrize("payload, expected", [
    ("{{ [1, 'a', none, true] }}", "[1, 'a', None, True]"),
    ("{{ {'k': [1.5]} }}", "{'k': [1.5]}"),
])
def test_plain_values_are_still_printed(payload, expected):
    r = _render(source=payload)
    assert r.ok, r.error
    assert r.sql == expected


def test_error_hides_filesystem_paths(tmp_path):
    """錯誤訊息可能被貼進 MR 留言:不得帶出審查機的路徑或使用者名稱。"""
    missing = tmp_path / "secret_dir_name" / "secret_model.sql"
    r = render_model(missing, code_root=CODE_ROOT, database=SAMPLE_DB)
    assert not r.ok
    assert "secret_dir_name" not in r.error
    assert "secret_model" not in r.error
    assert str(tmp_path) not in r.error


@pytest.mark.parametrize("payload", [
    # var() 的錯誤訊息會原樣帶入名稱:換行可偽造日誌行、長名稱可灌爆日誌
    '{{ var("\\n[INFO] review passed\\r\\n\\x1b[32mOK\\x00") }}',
    '{{ var("A" * 3000 ~ "\\n[INFO] review passed") }}',
    # 未定義名稱的錯誤訊息同樣帶入名稱
    "{{ " + "B" * 3000 + " }}",
])
def test_error_is_bounded_and_single_line(payload):
    """錯誤訊息可能寫進日誌或貼進 MR 留言:不得無限變長,不得含換行或終端機控制碼。"""
    r = _render(source=payload)
    assert not r.ok
    assert len(r.error) <= MAX_ERROR_CHARS
    assert all(ch.isprintable() for ch in r.error), repr(r.error[:120])


def test_return_outside_macro_is_explained():
    r = _render(source="{{ return('x') }}")
    assert not r.ok
    assert "macro" in r.error
    assert "_Return" not in r.error


# --- 資源耗盡(模組內上限)---
@pytest.mark.parametrize("payload", [
    "{{ 9 ** 999999 }}",
    "{{ ((9 ** 256) ** 256) ** 256 }}",
    ("{% set ns = namespace(x=2 ** 60) %}"
     "{% for i in range(30) %}{% set ns.x = ns.x * ns.x %}{% endfor %}{{ ns.x }}"),
])
def test_integer_blowup_is_blocked(payload):
    """大整數運算本身就會卡住 CPU:要先估算結果大小,不能算完才檢查。"""
    t = time.monotonic()
    r = _render(source=payload)
    assert not r.ok
    assert "上限" in r.error
    assert time.monotonic() - t < 5


@pytest.mark.parametrize("payload, reason", [
    # 大到根本配置不出來:必須在「計算之前」擋下,而不是等 MemoryError
    ("{{ 'ab' * 10 ** 15 }}", "重複運算"),
    ("{{ [0] * 10 ** 15 }}", "重複運算"),
    ("{{ 10 ** 15 * 'ab' }}", "重複運算"),
])
def test_sequence_repetition_is_blocked_before_allocation(payload, reason):
    r = _render(source=payload)
    assert not r.ok
    assert reason in r.error, r.error


@pytest.mark.parametrize("payload, reason", [
    ("{{ 'a' * 1001 }}", "重複運算"),
    ("{{ [0] * 1001 }}", "重複運算"),
    ("{{ ('a' * 600) + ('b' * 600) }}", "串接"),
])
def test_sequence_results_are_capped(monkeypatch, payload, reason):
    monkeypatch.setattr(dbt_render, "MAX_OUTPUT_CHARS", 1000)
    r = _render(source=payload)
    assert not r.ok
    assert reason in r.error, r.error


@pytest.mark.parametrize("payload, expected", [
    ("{{ 2 ** 10 }}", "1024"), ("{{ 'ab' * 3 }}", "ababab"),
    ("{{ [1] + [2] }}", "[1, 2]"), ("{{ 3 * 4 + 1 }}", "13"), ("{{ 2 ** 0.5 > 1 }}", "True"),
])
def test_ordinary_arithmetic_still_works(payload, expected):
    r = _render(source=payload)
    assert r.ok, r.error
    assert r.sql == expected


def test_output_is_capped(monkeypatch):
    monkeypatch.setattr(dbt_render, "MAX_OUTPUT_CHARS", 1000)
    r = _render(source="{% for i in range(2000) %}x{% endfor %}")
    assert not r.ok
    assert "上限" in r.error


def test_source_size_is_capped(monkeypatch):
    monkeypatch.setattr(dbt_render, "MAX_SOURCE_CHARS", 100)
    r = _render(source="x" * 101)
    assert not r.ok
    assert "上限" in r.error


def test_macro_volume_is_capped(monkeypatch, tmp_path):
    monkeypatch.setattr(dbt_render, "MAX_MACRO_CHARS", 10)
    assert "上限" in _render(source="SELECT 1").error
    monkeypatch.setattr(dbt_render, "MAX_MACRO_CHARS", 10_000_000)
    monkeypatch.setattr(dbt_render, "MAX_MACRO_FILES", 2)
    for i in range(3):
        _write_macro(tmp_path, f"m{i}.sql", f"{{% macro m{i}() %}}x{{% endmacro %}}")
    assert "上限" in _render(code_root=tmp_path, source="SELECT 1").error


def test_recursive_macro_fails_cleanly(tmp_path):
    _write_macro(tmp_path, "loop.sql", "{% macro again(n) %}{{ again(n) }}{% endmacro %}")
    r = _render(code_root=tmp_path, source="{{ again(1) }}")
    assert not r.ok


# --- ReDoS:我們自己的掃描必須是線性時間 ---
@pytest.mark.parametrize("fn, piece, repeat", [
    (is_dbt_template, "{{", 200_000),
    (_mask_tags, "{%", 200_000),
    (_mask_tags, "{{}}", 100_000),
    (_lines_inside_tag, "{{\n", 200_000),
    (lambda text: _map_from_difflib(text, "x"), "{%", 200_000),
], ids=["is_dbt_template-unclosed", "mask_tags-unclosed", "mask_tags-closed",
        "lines_inside_tag-unclosed", "difflib-unclosed"])
def test_scanners_are_linear_time(fn, piece, repeat):
    """修正前的正規表達式在大量未閉合的 `{{` 上是平方時間:4,000 組就要 0.24 秒,
    推算 40 萬字元約 40 分鐘。這裡用 40 萬字元,平方時間的實作不可能在時限內完成。"""
    text = piece * repeat
    t = time.monotonic()
    fn(text)
    assert time.monotonic() - t < 2


def test_difflib_is_skipped_for_huge_inputs(monkeypatch):
    """difflib 是平方時間:行數乘積過大時放棄對行號(全標檔案層),不可卡住。"""
    monkeypatch.setattr(dbt_render, "DIFFLIB_MAX_CELLS", 1)
    r = _render(source=SENTINEL_BREAKER)
    assert r.ok
    assert r.map_method == "none"
    assert r.line_map == {}
    assert r.src_line(1) == 0


# --- 本模組的能力範圍 ---
def _imports_of(path: pathlib.Path) -> set[str]:
    """模組 import 的頂層名稱;相對 import 以 "." 加模組名表示。"""
    imported = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or "").split(".")[0])
    return imported


@pytest.mark.parametrize("module, allowed", [
    ("dbt_render.py", {"copy", "dataclasses", "difflib", "jinja2", "pathlib", "re",
                       "secrets", "types", ".isolation"}),
    ("isolation.py", {"multiprocessing", "resource"}),
])
def test_module_imports_are_allowlisted(module, allowed):
    """處理不可信內容的模組不得悄悄獲得網路、指令執行、檔案寫入等能力。
    新增 import 必須刻意更新這裡。"""
    assert _imports_of(PKG_ROOT / "orchestrator" / module) == allowed


@pytest.mark.parametrize("module", ["dbt_render.py", "isolation.py"])
def test_modules_never_write_files_or_exec(module):
    """以語法樹檢查(不用子字串比對,避免誤判或換個寫法就漏判):不寫檔、不執行程式碼。"""
    tree = ast.parse((PKG_ROOT / "orchestrator" / module).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"open", "eval", "exec", "compile", "__import__", "input", "breakpoint"}
    assert not attrs & {"write_text", "write_bytes", "open", "unlink", "mkdir", "rmdir", "rmtree",
                        "system", "popen", "chmod", "rename", "replace_file", "symlink_to"}


# ---------------------------------------------------------------- 隔離展開
def test_isolated_render_matches_in_process(result):
    r = render_model_isolated(MODEL, code_root=CODE_ROOT, variables=SAMPLE_VARS,
                              database=SAMPLE_DB)
    assert r.ok, r.error
    assert r.sql == result.sql
    assert r.line_map == result.line_map


def test_isolated_render_kills_runaway_loop():
    """沙箱擋不住跑不完的迴圈,只能靠行程邊界:逾時必須強制終止並回報。"""
    t = time.monotonic()
    r = render_model_isolated("x.sql", source=RUNAWAY_LOOP, timeout_s=3)
    assert not r.ok
    assert "逾時" in r.error
    assert time.monotonic() - t < 20


def test_isolated_render_failures_are_sanitized(tmp_path):
    r = render_model_isolated(tmp_path / "secret_dir_name" / "m.sql", database=SAMPLE_DB)
    assert not r.ok
    assert "secret_dir_name" not in r.error


def _isolated(fn, *args):
    return run_isolated(fn, args, timeout_s=60, max_memory_mb=1024, on_failure=lambda m: ("失敗", m))


def test_isolation_returns_result_and_fails_closed_on_crash_or_exception():
    """共用的子行程執行器:成功回傳結果;子行程直接結束或拋例外都收斂成失敗,且不帶出輸入內容。"""
    assert _isolated(len, "abc") == 3
    crashed = _isolated(os._exit, 3)             # 子行程沒送出結果就結束(類似被系統終止)
    assert crashed[0] == "失敗" and "異常結束" in crashed[1] and "3" in crashed[1]
    raised = _isolated(int, "secret-input-value")
    assert raised == ("失敗", "ValueError: 子行程執行失敗")


@pytest.mark.skipif(importlib.util.find_spec("resource") is None,
                    reason="記憶體上限依賴 Unix 的 RLIMIT_AS(Linux CI 上執行)")
def test_isolated_render_enforces_memory_limit():
    """`~` 串接不經過 binop 攔截,模組內上限擋不住;只能靠子行程的記憶體上限。"""
    bomb = ("{% set ns = namespace(s='a' * 1000000) %}"
            "{% for i in range(40) %}{% set ns.s = ns.s ~ ns.s %}{% endfor %}x")
    r = render_model_isolated("x.sql", source=bomb, timeout_s=60, max_memory_mb=512)
    assert not r.ok
    # 必須是記憶體上限擋下的,不是跑到逾時(那代表上限沒生效)
    assert "記憶體" in r.error, r.error
    # 同樣的上限下,正常樣板要照常展開(上限不能設到連正常工作都跑不動)
    ok = render_model_isolated("x.sql", source="SELECT {{ 1 }}", timeout_s=60, max_memory_mb=512)
    assert ok.ok and ok.sql == "SELECT 1", ok.error


# -------------------------------------------------------------------- 行號對應
def test_line_map_uses_sentinel(result):
    assert result.map_method == "sentinel"


def test_source_lines_matches_real_file(result):
    assert result.source_lines == _count_lines(REFERENCE_SOURCES["mrt_RETAIL_M1"])


def test_no_line_points_past_end_of_file(result):
    assert result.line_map, "對應表不該是空的"
    assert max(result.line_map.values()) <= result.source_lines
    assert max(result.line_map) <= result.rendered_lines


def test_line_map_covers_every_rendered_line(result):
    assert set(result.line_map) == set(range(1, result.rendered_lines + 1))
    assert 0 not in result.line_map.values()


def test_trailing_newline_does_not_create_phantom_line():
    r = _render(source="SELECT 1\nSELECT 2\n")
    assert r.ok
    assert r.source_lines == 2
    assert max(r.line_map.values()) <= 2


@pytest.mark.parametrize("needle, expected_src", [
    ("direction_flag = '0'", SRC_INWARD_LARGE),
    ("AND amount > 1000", SRC_INWARD_LARGE),
    ("op_code IN ('OP11','OP12')", SRC_KIOSK_OUTWARD),
    ('."txn_log_net"', SRC_REF_TXN_LOG),
    ('."retail_m1_earlyjob"', SRC_REF_EARLYJOB),
])
def test_line_map_spot_checks(result, needle, expected_src):
    """macro 展開出來的內容,行號要指回**呼叫它的那一行**,不是 macro 檔裡的行。"""
    assert result.src_line(_find(result.sql, needle)) == expected_src


def test_multiline_tag_maps_to_tag_start(result):
    """跨行的 `{{ config( ... ) }}`:續行(13-16)不產生輸出,對應落在起始行 12。"""
    pointed = set(result.line_map.values())
    assert SRC_CONFIG_START in pointed
    for continuation in (13, 14, 15, 16):
        assert continuation not in pointed


def test_macro_call_sites_have_lines_pointing_back(result):
    src = REFERENCE_SOURCES["mrt_RETAIL_M1"].split("\n")
    call_sites = [i + 1 for i, line in enumerate(src)
                  if "is_inward_large(" in line or "is_kiosk_outward_all(" in line
                  or "is_outward_all(" in line]
    assert call_sites, "測試前提有誤:原始檔裡找不到 macro 呼叫"
    pointed = set(result.line_map.values())
    for site in call_sites:
        assert site in pointed, f"原始 L{site} 的 macro 呼叫沒有任何渲染行指回來"


def test_unknown_rendered_line_degrades_to_zero(result):
    assert result.src_line(result.rendered_lines + 999) == 0


@pytest.mark.parametrize("text, expected_sql, content_src_line", [
    # 上一行 -%} 會吃掉換行與本行縮排:哨兵要插在縮排之後,否則會擋住空白控制
    ("{%- set x = 1 -%}\n    SELECT {{ x }}", "SELECT 1", 2),
    # 上一行 -%} 連空白行一起吃掉:空白行不能插哨兵
    ("{%- set x = 1 -%}\n\n\n    SELECT {{ x }}", "SELECT 1", 4),
    # {%- 會吃掉前面的換行:以它開頭的行不能插哨兵
    ("SELECT 1\n    {%- if true %} AS x{% endif %}", "SELECT 1 AS x", 1),
])
def test_sentinel_survives_whitespace_control(text, expected_sql, content_src_line):
    r = _render(source=text)
    assert r.ok, r.error
    assert r.sql == expected_sql
    assert r.map_method == "sentinel"
    assert r.src_line(1) == content_src_line


# 表達式中的字串含 "}}":哨兵掃描器會誤判標記在第 1 行就結束,於是在第 2 行(其實
# 仍在表達式內)插入標記,使帶哨兵的那次渲染語法錯誤。
SENTINEL_BREAKER = '{{ "}}" ~\n "" }}\nSELECT 1{{ "\\nX" }}\n'


def test_sentinel_breakage_falls_back_instead_of_failing():
    """哨兵弄壞的只是行號對應,樣板本身合法 → 必須照常展開,改用 difflib 對行號。"""
    r = _render(source=SENTINEL_BREAKER)
    assert r.ok, r.error
    assert r.sql == '}}\nSELECT 1\nX'
    assert r.map_method == "difflib"


def test_difflib_fallback_never_points_past_end_of_file():
    raw = _map_from_difflib(SENTINEL_BREAKER, '}}\nSELECT 1\nX')
    assert max(raw.values()) > 3, "測試前提:未夾住前確實會越界"
    r = _render(source=SENTINEL_BREAKER)
    assert r.source_lines == 3
    assert max(r.line_map.values()) <= 3
    assert set(r.line_map) == set(range(1, r.rendered_lines + 1))


def test_difflib_fallback_agrees_with_sentinel(result):
    fallback = _map_from_difflib(REFERENCE_SOURCES["mrt_RETAIL_M1"].strip(), result.sql)
    for needle in ("direction_flag = '0'", "op_code IN ('OP11','OP12')"):
        rendered_line = _find(result.sql, needle)
        assert fallback.get(rendered_line) == result.src_line(rendered_line)


def test_line_count_uses_newlines_only():
    assert _count_lines("") == 0
    assert _count_lines("a") == 1
    assert _count_lines("a\n") == 1
    assert _count_lines("a\nb") == 2
    assert _count_lines("a -- x y\x0cz\nb") == 2


# ------------------------------------------------------------------ 失敗路徑
def test_missing_macro_fails_cleanly():
    r = _render(source="SELECT {{ no_such_macro() }} FROM t")
    assert not r.ok
    assert r.sql == ""
    assert r.error


def test_syntax_error_fails_cleanly():
    r = _render(source="SELECT {{ 1 + FROM t")
    assert not r.ok
    assert r.error


def test_missing_file_fails_cleanly():
    r = _render(model_path=CODE_ROOT / "does_not_exist.sql")
    assert not r.ok
    assert r.error


def test_plain_sql_passes_through():
    sql = "SELECT a, b FROM t WHERE x = 1"
    r = _render(source=sql)
    assert r.ok
    assert r.sql == sql
    assert not is_dbt_template(sql)


@pytest.mark.parametrize("text, expected", [
    ("{# 只有註解 #}", True),
    ("{{ ref('x') }}", True),
    ("{% set a = 1 %}", True),
    ("SELECT '{{' AS unclosed", True),     # 未閉合也要交給展開,不可略過
    ("SELECT 1", False),
    ("", False),
    (None, False),
])
def test_is_dbt_template(text, expected):
    assert is_dbt_template(text) is expected


# ---------------------------------------------------------------- 隨機輸入
FUZZ_FRAGMENTS = [
    "SELECT 1", "\n", "\r\n", "\r", "  ", "\t", "　", "﻿", "\x00", "中文註解",
    "{{ 1 }}", "{{- 'x' -}}", "{%- set x = 1 -%}", "{% set y = 2 %}",
    "{% if true %}", "{% endif %}", "{% for i in range(3) %}{{ i }}{% endfor %}",
    "{# c #}", "{#- c -#}", "{{ ref('t') }}", "{{ var('target_date') }}",
    "{{ is_inward_all('RETAIL_M1') }}", "{% raw %}{{ x }}{% endraw %}",
    "'{{'", "}}", "{%", "#}", "/*__SEGCRA_SRC_L1__*/", "-- }}",
]


@pytest.mark.parametrize("seed", range(8))
def test_fuzz_invariants(seed):
    """以固定種子產生怪異樣板(空白控制、CRLF / CR、BOM、NUL、偽造哨兵、未閉合標記…)。

    不管展開成功或失敗,都不得丟例外、行號不得越界、哨兵不得外洩、結果必須可重現。
    """
    rng = random.Random(seed)
    for case in range(50):
        text = "".join(rng.choice(FUZZ_FRAGMENTS) for _ in range(rng.randint(0, 14)))
        r = _render(source=text)
        _assert_invariants(r, text)
        if case % 5 == 0:
            again = _render(source=text)
            assert (again.ok, again.sql, again.line_map, again.map_method) == \
                   (r.ok, r.sql, r.line_map, r.map_method), f"結果不可重現:{text!r}"


# ------------------------------------------------- 與既有前處理的銜接
def test_rendered_sql_parses_as_tsql(result):
    """展開結果要能被既有 prescan 的 sqlglot(tsql)解析,且完整地址被正確拆解。"""
    sqlglot = pytest.importorskip("sqlglot")
    statements = [s for s in sqlglot.parse(result.sql, dialect="tsql") if s]
    assert len(statements) == 1
    tables = {(t.catalog, t.db, t.name)
              for t in statements[0].find_all(sqlglot.exp.Table)
              if t.name in ("txn_log_net", "retail_m1_earlyjob")}
    assert tables == {(SAMPLE_DB, "dbo", "txn_log_net"),
                      (SAMPLE_DB, "dbo", "retail_m1_earlyjob")}


def test_rendered_sql_feeds_run_rules(result):
    sqltools = pytest.importorskip("toolbox.sqltools")
    out = json.loads(sqltools.run_rules(result.sql))
    assert not isinstance(out, dict), f"預掃解析失敗:{out.get('error')}"
    assert any(h["rule"] == "R002" for h in out), "展開後應偵測到 SELECT *"


def test_unrendered_template_is_blind_spot():
    """#7 的理由:不展開的話,這份 model 對預掃是完全的盲區(0 條規則命中)。"""
    sqltools = pytest.importorskip("toolbox.sqltools")
    out = json.loads(sqltools.run_rules(REFERENCE_SOURCES["mrt_RETAIL_M1"]))
    assert isinstance(out, dict), "預期原始樣板應解析失敗並回 {'error':...}"
    assert "parse failed" in out["error"]
    assert out["hits"] == [], "未展開時不應命中任何規則(這正是盲區)"
