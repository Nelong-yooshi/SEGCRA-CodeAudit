"""審查時給 dbt 變數(排程每次執行時傳入的值)——不呼叫 LLM、不連資料庫。

部署端的 model 用 `var("target_date")` 取「這次執行的日期」,值由排程每次傳入。審查時沒有人傳:
沒有預設值的 `var()` 展開失敗(dbt 沒傳值時也是),報告多一條正式環境不會發生的 major,規則層
與執行驗證也跑不到這個 model。

  1. 設定檔:dbt.vars(固定值)、dbt.run_date_vars(這次執行的日期)的格式檢查;空的、null、兩邊都給
     就停,訊息寫出是哪個變數
  2. 一份審查的變數:run_date_vars 一律是執行驗證測資的日期(WIN_START)
  3. 展開:一份審查裡的展開都帶同一組;沒開始一份審查時不帶;呼叫端指定的照用
  4. 範例 model 的寫法(examples/sample/RETAIL_M1_code:同一個變數一處有預設值、一處沒有,讀前一天)
  5. 沒給值時的錯誤訊息說得出要設什麼
  6. 整條 review_mr:有設定時不再報「展開失敗」
  7. dbt_project.yml 的 vars:設定檔 > 專案 > 預設值;無法確定時展開失敗
  8. modules.datetime:算日期可用,看主機現在時間的擋下,其他模組展開失敗
"""
from __future__ import annotations

import contextvars
import json

import pytest

from orchestrator import dbt_render, pipeline
from orchestrator.config import DBT_DATABASE_PLACEHOLDER as DB
from orchestrator.config import _load_dbt_section
from orchestrator.dbt_render import render_model, render_model_isolated, start_review_invocation
from orchestrator.config import PKG_ROOT
from orchestrator.spec_exec import WIN_START

# 範例 model:同一個變數一處有預設值、一處沒有;讀上游前一天的結果
EXAMPLE_DIR = PKG_ROOT / "examples" / "sample" / "RETAIL_M1_code"
EXAMPLE = (EXAMPLE_DIR / "mrt_RETAIL_M1.sql").read_text(encoding="utf-8")


def _in_review(fn, variables=None):
    """在一份審查裡跑(另一份 context,不把這份審查的值留給後面的測試)。"""
    def run():
        start_review_invocation(variables)
        return fn()
    return contextvars.copy_context().run(run)


# ─────────────────── 1. 設定檔 ───────────────────

def test_設定檔收兩個設定():
    out = _load_dbt_section({"dbt": {"vars": {"region": "north", "lookback": 7, "full": True},
                                      "run_date_vars": ["target_date", "target_date"]}})
    assert out["vars"] == {"region": "north", "lookback": 7, "full": True}
    assert out["run_date_vars"] == ["target_date"]                      # 重複的只留一個


def test_沒設定時回傳的內容不變():
    out = _load_dbt_section({"dbt": {}})
    assert "vars" not in out and "run_date_vars" not in out


def test_值收清單與物件_名稱收非空字串():
    """`--vars` 可以傳清單、物件;名稱只拿來查表,不會拼進 SQL。"""
    out = _load_dbt_section({"dbt": {"vars": {"codes": ["OP01", "OP02"], "my-var": "x",
                                              "conf": {"a": 1, "b": [True, 2.5]}}}})
    assert out["vars"] == {"codes": ["OP01", "OP02"], "my-var": "x", "conf": {"a": 1, "b": [True, 2.5]}}


def test_沒加引號的日期換成_YYYY_MM_DD_字串():
    import datetime
    out = _load_dbt_section({"dbt": {"vars": {"cutoff": datetime.date(2026, 1, 1)}}})
    assert out["vars"] == {"cutoff": "2026-01-01"}


@pytest.mark.parametrize("section, words", [
    ({"vars": ["target_date"]}, ["dbt.vars", "list"]),                         # 不是物件
    ({"vars": {"": "x"}}, ["dbt.vars", "變數名稱不合法"]),
    ({"vars": {"region": None}}, ["dbt.vars.region", "null"]),
    ({"vars": {"region": ""}}, ["dbt.vars.region", "空的"]),
    ({"vars": {"region": "   "}}, ["dbt.vars.region", "空的"]),
    ({"vars": {"x": object()}}, ["dbt.vars.x", "object"]),
    ({"vars": {"x": ["a", None]}}, ["dbt.vars.x[1]", "null"]),
    ({"run_date_vars": "target_date"}, ["dbt.run_date_vars", "str"]),         # 不是清單
    ({"run_date_vars": [1]}, ["dbt.run_date_vars", "不合法"]),
    ({"run_date_vars": [" "]}, ["dbt.run_date_vars", "不合法"]),
])
def test_設定檔格式不對時擋下_寫出是哪個變數(section, words):
    with pytest.raises(ValueError) as e:
        _load_dbt_section({"dbt": section}, "設定檔 /deploy/models.yaml")
    for w in words + ["設定檔 /deploy/models.yaml"]:
        assert w in str(e.value), (w, str(e.value))


def test_沒加引號的日期時間要加引號():
    import datetime
    with pytest.raises(ValueError, match="dbt.vars.at.*加引號"):
        _load_dbt_section({"dbt": {"vars": {"at": datetime.datetime(2026, 1, 1, 8, 0)}}})


@pytest.mark.parametrize("key, value", [("vars", None), ("vars", {}), ("run_date_vars", None),
                                        ("run_date_vars", [])])
def test_寫了鍵但是空的就停(key, value):
    with pytest.raises(ValueError, match=f"dbt.{key} 有寫但是空的"):
        _load_dbt_section({"dbt": {key: value}})


def test_同一個變數兩邊都給就停_不安靜地蓋掉():
    with pytest.raises(ValueError, match="target_date.*同時寫在 dbt.vars 與 dbt.run_date_vars"):
        _load_dbt_section({"dbt": {"vars": {"target_date": "2026-01-01"}, "run_date_vars": ["target_date"]}})


def test_載入設定檔時錯誤訊息帶上設定檔的路徑(tmp_path):
    import yaml
    from orchestrator.config import load_config
    raw = yaml.safe_load((PKG_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    raw.setdefault("dbt", {})["vars"] = {"region": None}
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError) as e:
        load_config(path)
    assert str(path) in str(e.value) and "dbt.vars.region" in str(e.value)


# ─────────────────── 2. 一份審查的變數 ───────────────────

def test_日期變數一律是測資的日期():
    got = pipeline._review_variables({"vars": {"region": "north"},
                                      "run_date_vars": ["target_date", "as_of"]})
    assert got == {"region": "north", "target_date": WIN_START, "as_of": WIN_START}


@pytest.mark.parametrize("cfg", [None, {}, {"enabled": True}, "x"])
def test_沒有設定時沒有變數(cfg):
    assert pipeline._review_variables(cfg) == {}


# ─────────────────── 3. 展開 ───────────────────

def test_一份審查裡的展開都帶同一組變數():
    def work():
        a = render_model_isolated("models/m.sql", source="{{ var('target_date') }}")
        b = render_model_isolated("models/m.sql", source="{{ var('target_date') }}", incremental=True)
        return a, b
    a, b = _in_review(work, {"target_date": WIN_START})
    assert a.ok and b.ok, (a.error, b.error)
    assert a.sql == b.sql == WIN_START


def test_沒開始一份審查時不帶變數():
    r = contextvars.copy_context().run(
        lambda: render_model_isolated("models/m.sql", source="{{ var('target_date') }}"))
    assert not r.ok and "target_date" in r.error


def test_呼叫端指定的變數照用():
    r = _in_review(lambda: render_model_isolated(
        "models/m.sql", source="{{ var('target_date') }}", variables={"target_date": "x"}),
        {"target_date": WIN_START})
    assert r.ok and r.sql == "x"


def test_不把變數留給後面():
    _in_review(lambda: None, {"target_date": WIN_START})
    assert dbt_render._REVIEW_VARIABLES.get() is None


# ─────────────────── 4. 範例 model 的寫法 ───────────────────

def test_範例_model_的寫法_兩處都是測資的日期():
    r = render_model("models/mrt_RETAIL_M1.sql", code_root=EXAMPLE_DIR, source=EXAMPLE, database=DB,
                     variables=pipeline._review_variables({"run_date_vars": ["target_date"]}))
    assert r.ok, r.error
    assert f"TABLE_DATE = '{WIN_START}'" in r.sql
    assert f"DATEADD(DAY, -1, '{WIN_START}')" in r.sql


def test_範例_model_的寫法_沒設定時照舊展開失敗():
    r = render_model("models/mrt_RETAIL_M1.sql", code_root=EXAMPLE_DIR, source=EXAMPLE, database=DB)
    assert not r.ok and "target_date" in r.error


# ─────────────────── 5. 錯誤訊息 ───────────────────

def test_沒給值時說得出要設什麼():
    r = render_model("models/m.sql", source="{{ var('target_date') }}")
    assert not r.ok
    assert "dbt.vars" in r.error and "dbt.run_date_vars" in r.error


# ─────────────────── 6. 整條 review_mr ───────────────────

from tests.test_dbt_affected import (CFG, _Loader, review)  # noqa: E402,F401
from tests.test_dbt_affected import _fresh_cache  # noqa: E402,F401

MODEL = "SELECT a FROM t WHERE TABLE_DATE = '{{ var(\"target_date\") }}'\n"


def _files():
    return {"dbt_project.yml": "name: sample\n", "models/mrt_BIG.sql": MODEL}


def _run(review, cfg_dbt):
    files = [{"path": "models/mrt_BIG.sql", "diff": "@@ -0,0 +1 @@\n+" + MODEL}]
    report, _ = review(files, _Loader(_files()), cfg_dbt=cfg_dbt)
    return report


def test_review_mr_有設定時不再報展開失敗(review):
    report = _run(review, {**CFG, "run_date_vars": ["target_date"]})
    titles = {f["title"] for f in report["findings"]}
    assert pipeline._RENDER_FAIL_TITLE not in titles, json.dumps(report["findings"], ensure_ascii=False)


def test_review_mr_沒設定時照舊報展開失敗(review):
    report = _run(review, dict(CFG))
    assert pipeline._RENDER_FAIL_TITLE in {f["title"] for f in report["findings"]}


# ─────────────────── 7. dbt_project.yml 的 vars ───────────────────

def _info(project_yml: str, cli_vars=None):
    from orchestrator.dbt_context import _with_concat_default
    from orchestrator.dbt_relations import project_info_from_files
    info = project_info_from_files({"dbt_project.yml": project_yml, "models/m.sql": "SELECT 1"},
                                   root_files_checked=True)
    return _with_concat_default(info, None, cli_vars)


def _var(project_yml: str, variables=None, src="{{ var('region') }}"):
    return render_model("models/m.sql", source=src, variables=variables, project=_info(project_yml))


def test_專案宣告的變數照_dbt_展開():
    r = _var("name: p\nvars:\n  region: north\n")
    assert r.ok and r.sql == "north"


def test_設定檔蓋過專案():
    r = _var("name: p\nvars:\n  region: north\n", {"region": "south"})
    assert r.ok and r.sql == "south"


def test_MR_改了專案的值_用改過的():
    """專案資訊來自 MR 的 head:改了 dbt_project.yml,展開的就是新值(不會用抄到設定檔的舊值)。"""
    assert _var("name: p\nvars:\n  region: east\n").sql == "east"


def test_專案沒有_也沒給值_照舊失敗而且說要設什麼():
    r = _var("name: p\n")
    assert not r.ok and "dbt_project.yml 的 vars 與設定檔也沒有給" in r.error


@pytest.mark.parametrize("yml", [
    "name: p\nvars:\n  p:\n    region: north\n",                     # 專案層級的子區塊
    "name: p\nvars:\n  region: \"{{ env_var('R') }}\"\n",              # 樣板值
])
def test_專案的值無法確定時展開失敗_寫出原因(yml):
    r = _var(yml)
    assert not r.ok and "無法確定" in r.error


def test_子區塊時設定檔給了的照用():
    r = _var("name: p\nvars:\n  p:\n    region: north\n", {"region": "south"})
    assert r.ok and r.sql == "south"


@pytest.mark.parametrize("project_var, cli, expected", [
    (None, True, True),       # 只有 --vars:照它
    (True, False, None),      # 專案靠 var 的 true 串接、--vars 蓋成 false:結果看 flags,不重算
    (None, "yes", None),      # 不是布林:無法確定
    (True, True, True),
])
def test_schema_concat_設定檔優先於專案(project_var, cli, expected):
    from orchestrator.dbt_relations import SCHEMA_CONCAT_KEY
    yml = "name: p\n" + (f"vars:\n  {SCHEMA_CONCAT_KEY}: {str(project_var).lower()}\n" if project_var else "")
    info = _info(yml, {SCHEMA_CONCAT_KEY: cli})
    assert info.schema_concat is expected


def test_schema_concat_設定檔沒寫時照專案():
    from orchestrator.dbt_relations import SCHEMA_CONCAT_KEY
    info = _info(f"name: p\nvars:\n  {SCHEMA_CONCAT_KEY}: true\n", {})
    assert info.schema_concat is True


# ─────────────────── 8. modules.datetime ───────────────────

PREV_DAY = ("{% set d = modules.datetime.datetime.strptime(var(\"target_date\"), \"%Y-%m-%d\") %}"
            "{% set prev = (d - modules.datetime.timedelta(days=1)).strftime(\"%Y-%m-%d\") %}"
            "SELECT acct_id FROM t WHERE txn_date = '{{ prev }}'")


def test_用_modules_datetime_算前一天():
    r = render_model("models/m.sql", source=PREV_DAY, variables={"target_date": "2026-06-01"})
    assert r.ok and r.sql == "SELECT acct_id FROM t WHERE txn_date = '2026-05-31'"


@pytest.mark.parametrize("src", [
    "{{ modules.datetime.datetime.now() }}",
    "{{ modules.datetime.date.today() }}",
    "{{ modules.datetime.datetime.utcnow() }}",
    "{{ modules.datetime.datetime.fromtimestamp(0) }}",
    "{{ modules.datetime.datetime.strptime('2026-01-01', '%Y-%m-%d').now() }}",
])
def test_modules_datetime_看主機現在時間的擋下(src):
    r = render_model("models/m.sql", source=src)
    assert not r.ok and "沒有照實模擬" in r.error


@pytest.mark.parametrize("name", ["pytz", "re", "itertools"])
def test_不提供的模組展開失敗_寫出名稱(name):
    r = render_model("models/m.sql", source=f"{{{{ modules.{name} }}}}")
    assert not r.ok and f"modules.{name} 不提供" in r.error


def test_modules_是唯讀的_macro_不能叫這個名字(tmp_path):
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "m.sql").write_text("{% macro modules() %}x{% endmacro %}", encoding="utf-8")
    r = render_model("models/m.sql", code_root=tmp_path, source="SELECT 1")
    assert not r.ok and "modules" in r.error
