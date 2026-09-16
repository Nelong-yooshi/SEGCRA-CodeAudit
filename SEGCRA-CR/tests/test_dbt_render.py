"""dbt_render 的確定性測試 — 不呼叫 LLM、不連資料庫,秒級,可進 CI。

驗的四件事:
  1. 展開正確:macro 裡的門檻值與 op_code 白名單真的出現在輸出裡
     (沒展開的話規則層看到的是 `{{ is_inward_large(...) }}`,等於黑箱)。
  2. 行號對得回原始檔,而且走的是**哨兵**這條確定性路徑,不是 difflib 近似;
     行號不得超出檔案實際行數。
  3. **不猜值**:未定義的 dbt 內建、沒有預設值又沒被提供的 var,一律硬失敗。
     靜靜產出 `WHERE d = None` 這種 SQL 比展開失敗危險得多。
  4. 失敗時收斂成 ok=False,不丟例外、不產出半套 SQL。
"""
import json
import pathlib

import pytest

from orchestrator.dbt_render import (
    RenderResult, _map_from_difflib, build_env, is_dbt_template, render_model,
)

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
CODE_ROOT = PKG_ROOT / "examples" / "sample" / "RETAIL_M1_code"
MODEL = CODE_ROOT / "mrt_RETAIL_M1.sql"

# 原始檔裡這幾行是 macro 呼叫 / ref 呼叫,行號對應要指得回來
SRC_INWARD_LARGE = 70          # , SUM(CASE WHEN {{ is_inward_large('RETAIL_M1') }} ...
SRC_KIOSK_OUTWARD = 77         # , SUM(CASE WHEN {{ is_kiosk_outward_all('RETAIL_M1') }} ...
SRC_REF_TXN_LOG = 35           # FROM {{ ref('txn_log_net') }}
SRC_REF_EARLYJOB = 165         # FROM {{ ref('retail_m1_earlyjob') }} e
SRC_CONFIG_START = 12          # {{ config(  ← 跨行標記,續行是 13-16

# 這份 model 第 167 行是 `{{ var("target_date") }}`(無預設值),dbt 沒設定變數時
# 也會編譯失敗 → 渲染一律要明確帶入變數。
SAMPLE_VARS = {"target_date": "2026-02-01"}


@pytest.fixture(scope="module")
def result() -> RenderResult:
    r = render_model(MODEL, code_root=CODE_ROOT, variables=SAMPLE_VARS)
    assert r.ok, f"展開失敗:{r.error}"
    return r


def test_model_needs_vars_like_dbt_does():
    """不給變數時要跟 dbt 一樣失敗,不可用猜來的預設值硬渲染過去。

    本檔 L167 的 `var("target_date")` 沒有預設值。dbt 的 var() 看不到 L21 的
    `{% set %}`,所以 dbt 在沒有 --vars 時同樣編譯失敗——行為必須一致,否則
    我們會產出一份「dbt 根本編不出來」的 SQL,還拿去跟 dbt compile 對照。
    """
    r = render_model(MODEL, code_root=CODE_ROOT)
    assert not r.ok
    assert "target_date" in r.error


def _find(sql: str, needle: str) -> int:
    """回傳第一個含 needle 的渲染行號(1-based);找不到直接讓測試失敗。"""
    for idx, line in enumerate(sql.split("\n"), start=1):
        if needle in line:
            return idx
    pytest.fail(f"渲染輸出裡找不到 {needle!r}")


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


def test_ref_becomes_bare_table_name(result):
    """ref() 刻意展開成裸表名(沙盒要用自建測資表,不能帶正式環境前綴)。"""
    assert "FROM txn_log_net" in result.sql
    assert "FROM retail_m1_earlyjob" in result.sql


def test_render_is_deterministic():
    """同樣輸入必須逐字相同——否則沒辦法拿去跟 dbt compile 對照。"""
    a = render_model(MODEL, code_root=CODE_ROOT, variables=SAMPLE_VARS)
    b = render_model(MODEL, code_root=CODE_ROOT, variables=SAMPLE_VARS)
    assert a.sql == b.sql
    assert a.line_map == b.line_map


def test_macro_name_conflict_is_reported(tmp_path):
    """同名 macro 只能擇一,結果可能與 dbt 不同 → 必須回報,不可靜靜吞掉。

    dbt 用套件命名空間解析,我們是單一平面命名空間。不回報的話,「與
    dbt compile 一致」這條驗收會在無人察覺的情況下失效。
    """
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "a_first.sql").write_text(
        "{% macro dup() %}FIRST{% endmacro %}", encoding="utf-8")
    (tmp_path / "macros" / "z_second.sql").write_text(
        "{% macro dup() %}SECOND{% endmacro %}", encoding="utf-8")

    _, macros, conflicts = build_env(tmp_path)
    assert macros.count("dup") == 1, "同名不應在清單裡重複出現"
    assert len(conflicts) == 1
    assert "dup" in conflicts[0]
    assert "a_first.sql" in conflicts[0] and "z_second.sql" in conflicts[0]


# --------------------------------------------------------------- var 不猜值
def test_var_without_default_fails_loudly():
    """沒有預設值又沒被提供的 var 必須硬失敗。

    回 None 的話 Jinja 會渲染成字串 "None" 寫進 SQL(`WHERE d = None`),
    而且整個流程還會回報成功——這種錯最難抓,比展開失敗危險得多。
    """
    r = render_model(MODEL, code_root=CODE_ROOT,
                     source='SELECT * FROM t WHERE d = {{ var("nope") }}')
    assert not r.ok
    assert "nope" in r.error
    assert "None" not in r.sql


def test_var_with_default_is_used():
    """有預設值的 var 照常吃預設,不受上面那條影響。"""
    r = render_model(MODEL, code_root=CODE_ROOT,
                     source='SELECT {{ var("nope", "FALLBACK") }}')
    assert r.ok
    assert r.sql == "SELECT FALLBACK"


def test_var_default_and_override():
    """var() 有預設值要吃預設;呼叫端覆寫時要蓋掉。"""
    default = render_model(MODEL, code_root=CODE_ROOT, variables=SAMPLE_VARS)
    assert "'2026-02-01'" in default.sql

    custom = render_model(MODEL, code_root=CODE_ROOT,
                          variables={"target_date": "2026-03-15"})
    assert "'2026-03-15'" in custom.sql
    assert "'2026-02-01'" not in custom.sql


# -------------------------------------------------------------------- 行號對應
def test_line_map_uses_sentinel(result):
    """必須走哨兵這條確定性路徑。退到 difflib 代表哨兵被干擾了,要查。"""
    assert result.map_method == "sentinel"


def test_source_lines_matches_real_file(result):
    """source_lines 要等於檔案實際行數(檔尾換行不算多一行)。"""
    assert result.source_lines == len(MODEL.read_text(encoding="utf-8").splitlines())


def test_no_line_points_past_end_of_file(result):
    """行號絕不能超出檔案實際行數——貼到不存在的行,GitLab 會貼歪或拒收。"""
    real = len(MODEL.read_text(encoding="utf-8").splitlines())
    assert result.line_map, "對應表不該是空的"
    assert max(result.line_map.values()) <= real
    assert max(result.line_map) <= result.rendered_lines


def test_line_map_covers_every_rendered_line(result):
    """每個渲染行都要有對應,而且這份檔案不該有任何行退化成 0。"""
    assert len(result.line_map) == result.rendered_lines
    assert set(result.line_map) == set(range(1, result.rendered_lines + 1))
    assert 0 not in result.line_map.values()


def test_trailing_newline_does_not_create_phantom_line():
    """檔尾換行不可以生出一個指向「第 N+1 行」的對應。"""
    r = render_model(MODEL, code_root=CODE_ROOT, source="SELECT 1\nSELECT 2\n")
    assert r.ok
    assert r.source_lines == 2
    assert max(r.line_map.values()) <= 2


@pytest.mark.parametrize("needle, expected_src", [
    ("direction_flag = '0'", SRC_INWARD_LARGE),
    ("AND amount > 1000", SRC_INWARD_LARGE),
    ("op_code IN ('OP11','OP12')", SRC_KIOSK_OUTWARD),
    ("FROM txn_log_net", SRC_REF_TXN_LOG),
    ("FROM retail_m1_earlyjob", SRC_REF_EARLYJOB),
])
def test_line_map_spot_checks(result, needle, expected_src):
    """macro 展開出來的內容,行號要指回**呼叫它的那一行**,不是 macro 檔裡的行。"""
    assert result.src_line(_find(result.sql, needle)) == expected_src


def test_multiline_tag_maps_to_tag_start(result):
    """跨行的 `{{ config( ... ) }}`:續行(13-16)不產生輸出,對應落在起始行 12。

    釘住這個行為,免得日後有人把續行也插哨兵——那會把 Jinja 標記打斷。
    """
    pointed = set(result.line_map.values())
    assert SRC_CONFIG_START in pointed
    for continuation in (13, 14, 15, 16):
        assert continuation not in pointed


def test_macro_call_sites_have_lines_pointing_back(result):
    """反向確認:每個 macro 呼叫點都至少有一個渲染行指回來。"""
    src = MODEL.read_text(encoding="utf-8").split("\n")
    call_sites = [i + 1 for i, line in enumerate(src)
                  if "is_inward_large(" in line or "is_kiosk_outward_all(" in line
                  or "is_outward_all(" in line]
    assert call_sites, "測試前提有誤:原始檔裡找不到 macro 呼叫"
    pointed = set(result.line_map.values())
    for site in call_sites:
        assert site in pointed, f"原始 L{site} 的 macro 呼叫沒有任何渲染行指回來"


def test_unknown_rendered_line_degrades_to_zero(result):
    """查不到對應時回 0(= 檔案層留言),不能亂猜一個行號。"""
    assert result.src_line(result.rendered_lines + 999) == 0


def test_difflib_fallback_agrees_with_sentinel(result):
    """退路本身要能動。平常不會走到,但走到的時候不能是壞的。

    這份檔案上兩種方法應給出相同的關鍵對應;差異大就代表退路有問題。
    """
    raw = MODEL.read_text(encoding="utf-8")
    fallback = _map_from_difflib(raw, result.sql)
    for needle in ("direction_flag = '0'", "op_code IN ('OP11','OP12')",
                   "FROM txn_log_net", "FROM retail_m1_earlyjob"):
        rendered_line = _find(result.sql, needle)
        assert fallback.get(rendered_line) == result.src_line(rendered_line)


# ------------------------------------------------------------------ 失敗路徑
def test_missing_macro_fails_cleanly():
    """沒樁到的東西要大聲失敗,不可悄悄渲染成空字串。"""
    r = render_model(MODEL, code_root=CODE_ROOT,
                     source="SELECT {{ no_such_macro() }} FROM t")
    assert not r.ok
    assert r.sql == ""
    assert r.error


def test_syntax_error_fails_cleanly():
    """樣板語法壞掉時收斂成 ok=False,不丟例外。"""
    r = render_model(MODEL, code_root=CODE_ROOT, source="SELECT {{ 1 + FROM t")
    assert not r.ok
    assert r.error


def test_missing_file_fails_cleanly():
    r = render_model(CODE_ROOT / "does_not_exist.sql", code_root=CODE_ROOT)
    assert not r.ok
    assert r.error


def test_plain_sql_passes_through():
    """非樣板的純 SQL 要原樣通過,不被動到。"""
    sql = "SELECT a, b FROM t WHERE x = 1"
    r = render_model(MODEL, code_root=CODE_ROOT, source=sql)
    assert r.ok
    assert r.sql == sql
    assert not is_dbt_template(sql)


@pytest.mark.parametrize("text, expected", [
    ("{# 只有註解 #}", True),
    ("{{ ref('x') }}", True),
    ("{% set a = 1 %}", True),
    ("SELECT 1", False),
    ("", False),
])
def test_is_dbt_template(text, expected):
    """註解標記也算樣板——判成不是的話整份檔案會被跳過不展開。"""
    assert is_dbt_template(text) is expected


# ---------------------------------------------------------------------- 資安
# 這裡渲染的是**待審的 MR 內容**。用一般的 jinja2.Environment 的話,下列樣板
# 可以在審查機上取得任意程式執行(已實測:成功執行 os.getcwd())。
# 改用 SandboxedEnvironment 後全部被擋。這組測試是防止有人把沙箱改回去——
# 一旦被回退,失效是無聲的,不會有任何測試以外的徵兆。
SSTI_PAYLOADS = [
    "{{ ''.__class__.__mro__ }}",
    "{{ ''.__class__.__mro__[1].__subclasses__() | length }}",
    "{{ config.__globals__ }}",
    "{{ ref.__globals__ }}",
    ("{% for c in ''.__class__.__mro__[1].__subclasses__() %}"
     "{% if c.__name__ == 'catch_warnings' %}"
     "{{ c()._module.__builtins__['__import__']('os').getcwd() }}"
     "{% endif %}{% endfor %}"),
]


@pytest.mark.parametrize("payload", SSTI_PAYLOADS)
def test_template_injection_is_blocked(payload):
    """惡意 MR 樣板不得取得任意程式執行,必須被沙箱擋下。"""
    r = render_model(MODEL, code_root=CODE_ROOT, source=payload,
                     variables=SAMPLE_VARS)
    assert not r.ok, f"樣板注入沒有被擋下:{payload!r} -> {r.sql!r}"
    assert "SecurityError" in r.error
    assert r.sql == ""


def test_environment_is_sandboxed():
    """直接確認用的是沙箱環境,不是普通 Environment。"""
    from jinja2.sandbox import SandboxedEnvironment

    env, _, _ = build_env(CODE_ROOT)
    assert isinstance(env, SandboxedEnvironment)


def test_sandbox_does_not_break_legitimate_rendering(result):
    """沙箱不能誤傷正常的 dbt 構造(macro 呼叫、dict 取值、巢狀 return)。"""
    flat = " ".join(result.sql.split())
    assert "AND amount > 1000" in flat
    assert "AND op_code IN ('OP01','OP02','OP03')" in flat


# ------------------------------------------------- 與既有前處理的銜接
def test_rendered_sql_parses_as_tsql(result):
    """展開結果要能被既有 prescan 的 sqlglot(tsql)解析,否則規則層等於沒跑。"""
    sqlglot = pytest.importorskip("sqlglot")
    statements = [s for s in sqlglot.parse(result.sql, dialect="tsql") if s]
    assert len(statements) == 1


def test_rendered_sql_feeds_run_rules(result):
    """端到端:展開後的 SQL 丟進既有預掃,要能解析並命中真實規則。

    run_rules 正常時回 list,解析失敗才回 {"error": ..., "hits": [...]}(見 docs/01)。
    """
    sqltools = pytest.importorskip("toolbox.sqltools")
    out = json.loads(sqltools.run_rules(result.sql))
    assert not isinstance(out, dict), f"預掃解析失敗:{out.get('error')}"
    assert any(h["rule"] == "R002" for h in out), "展開後應偵測到 SELECT *"


def test_unrendered_template_is_blind_spot():
    """#7 的理由:不展開的話,這份 model 對預掃是完全的盲區。

    原始樣板送進 sqlglot 會在 `{% set %}` 那行解析失敗,AST 規則整組跳過,
    結果是**一條規則都不會命中**——藏在 T_TXN_FULL 裡的 `SELECT *` 也看不到。
    這條測試把「展開前 vs 展開後」的差距釘住,避免日後有人把展開拿掉。
    """
    sqltools = pytest.importorskip("toolbox.sqltools")
    raw = MODEL.read_text(encoding="utf-8")
    out = json.loads(sqltools.run_rules(raw))
    assert isinstance(out, dict), "預期原始樣板應解析失敗並回 {'error':...}"
    assert "parse failed" in out["error"]
    assert out["hits"] == [], "未展開時不應命中任何規則(這正是盲區)"
