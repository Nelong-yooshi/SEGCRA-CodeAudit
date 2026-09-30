"""dbt 接進審查管線(#7 第 1、2 點的管線端)。

檔名刻意跟 dbt_render / dbt_impact 自己的單元測試檔分開,也跟 #12 即將加入的
tests/test_spec_exec.py 分開——這裡只測「接線」本身(find_spec 的檔名對應、
預掃前展開、樣板不送進沙盒),不重覆模組自己的單元測試範圍。

每一步都要能獨立驗證「開關關閉時,行為與接線前完全相同」,所以大多數測試案例
都成對出現:一個確認新行為生效,一個確認舊行為分毫不變。
"""
import asyncio
import json
import pathlib

import pytest

import orchestrator.pipeline as _pipeline_mod
from orchestrator.config import DBT_DATABASE_PLACEHOLDER, Config, _load_dbt_section, load_config
from orchestrator.dbt_render import _RELATION_NOTICE, render_model
from orchestrator.pipeline import (_DBT_NOTICE_TITLE, _PARSE_FAIL_TITLE, _RENDER_FAIL_TITLE,
                                   _dry_run_report, apply_policy, enforce_dbt_notice,
                                   enforce_dbt_render_failure, enforce_hints, enforce_parse,
                                   prescan)
from orchestrator.spec_exec import find_spec, run_spec_exec
from toolbox.sqltools import run_rules as _run_rules


def _run(coro):
    return asyncio.run(coro)


def _write_spec(dir_: pathlib.Path, name: str, body: str = "# 規格\n內容") -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{name}.md").write_text(body, encoding="utf-8")


def _mr(files, title="", description=""):
    return {"title": title, "description": description, "files": files}


class _FakeHub:
    """只實作 find_spec 會用到的 call(),記錄呼叫次數。

    contents 沒有的路徑,回傳的是 mock 模式的佔位文字(與 toolbox/gitlab.py 的
    get_file 相同:不丟例外、回一段不以 # 開頭的說明)——管線實際跑的時候 hub
    一定存在,這才是 find_spec 真正會遇到的情境。
    """

    def __init__(self, contents: dict | None = None):
        self.contents = contents or {}
        self.calls: list[str] = []

    async def call(self, name, params, truncate=False):
        assert name == "gitlab__get_file"
        path = params["path"]
        self.calls.append(path)
        return self.contents.get(path, f"(mock 模式無 {path} 的完整內容,請依 diff 判斷)")


@pytest.fixture
def specs_dir(tmp_path, monkeypatch):
    """把 SPECS_DIR 指到空的暫存目錄:不清掉的話會讀到 repo 裡真正的 specs/*.md,
    真假答案混在一起,測試就驗不到要驗的那條路徑。"""
    import orchestrator.spec_exec as spec_exec_mod
    monkeypatch.setattr(spec_exec_mod, "SPECS_DIR", tmp_path)
    return tmp_path


DBT_MR = _mr([{"path": "models/mrt_RETAIL_M1.sql", "full_content": "SELECT 1"}])


# ---------------------------------------------------- 開關:預設關閉,行為不變
def test_path_fallback_off_by_default(specs_dir):
    """by_path 預設 False:規格檔就在那裡,也不依檔名對應——與接線前完全相同。
    這是「合併後正式審查行為不變」這句話成立的依據。"""
    _write_spec(specs_dir, "RETAIL_M1")
    hub = _FakeHub({"specs/RETAIL_M1.md": "# 規格"})
    assert _run(find_spec(hub, DBT_MR)) == (None, None)
    assert _run(find_spec(None, DBT_MR)) == (None, None)
    assert hub.calls == []


def test_pipeline_passes_dbt_flag_to_find_spec():
    """review_mr() 呼叫 find_spec 時必須帶上 dbt 開關,不能寫死開啟或漏傳。"""
    import inspect
    import orchestrator.pipeline as pipeline_mod
    src = inspect.getsource(pipeline_mod.review_mr)
    assert 'find_spec(hub, mr, by_path=cfg.dbt.get("enabled") is True)' in src


# --------------------------------------------------- 開啟後:先查本機 specs/
def test_local_spec_found_when_hub_present(specs_dir):
    """管線實際執行時 hub 一定存在(mock 模式也是)。本機 specs/ 有對應規格時必須
    找得到——曾經只在 hub 為 None 時查本機,導致 demo 與 golden set 永遠找不到。"""
    _write_spec(specs_dir, "RETAIL_M1", "# 本機規格")
    hub = _FakeHub()
    assert _run(find_spec(hub, DBT_MR, by_path=True)) == ("RETAIL_M1", "# 本機規格")
    assert hub.calls == []          # 本機找到就不必再問 GitLab


def test_local_spec_found_without_hub(specs_dir):
    _write_spec(specs_dir, "RETAIL_M1", "# 本機規格")
    assert _run(find_spec(None, DBT_MR, by_path=True)) == ("RETAIL_M1", "# 本機規格")


def test_local_spec_case_mismatch_not_used(specs_dir):
    """只有大小寫不同的規格檔不採用(resolve_spec 的語意),交人工確認。"""
    _write_spec(specs_dir, "retail_m1")
    assert _run(find_spec(None, DBT_MR, by_path=True)) == (None, None)


def test_local_spec_directory_named_like_spec_ignored(specs_dir):
    """specs/ 底下剛好有名為 X.md 的**資料夾**時不可當成規格(不可讀取失敗炸掉)。"""
    (specs_dir / "RETAIL_M1.md").mkdir(parents=True)
    assert _run(find_spec(None, DBT_MR, by_path=True)) == (None, None)


def test_first_matching_file_wins(specs_dir):
    _write_spec(specs_dir, "SECOND", "# 第二個")
    mr = _mr([{"path": "models/mrt_FIRST.sql", "full_content": "SELECT 1"},
              {"path": "models/mrt_SECOND.sql", "full_content": "SELECT 2"}])
    assert _run(find_spec(None, mr, by_path=True))[0] == "SECOND"


@pytest.mark.parametrize("files", [
    [],
    [{"path": "models/sources.yml", "full_content": "version: 2"}],   # 不是 .sql
])
def test_nothing_to_match(specs_dir, files):
    assert _run(find_spec(_FakeHub(), _mr(files), by_path=True)) == (None, None)


# ------------------------------------------ 開啟後:本機沒有才探測 GitLab
def test_gitlab_probed_when_local_missing(specs_dir):
    hub = _FakeHub({"specs/RETAIL_M1.md": "# 從 GitLab 讀到的規格"})
    assert _run(find_spec(hub, DBT_MR, by_path=True)) == ("RETAIL_M1", "# 從 GitLab 讀到的規格")
    # 候選順序是「完整檔名優先」:先探測 mrt_RETAIL_M1(不存在),才退到 RETAIL_M1
    assert hub.calls == ["specs/mrt_RETAIL_M1.md", "specs/RETAIL_M1.md"]


def test_gitlab_full_name_candidate_wins_first(specs_dir):
    hub = _FakeHub({"specs/mrt_BOTH.md": "# 完整檔名優先"})
    mr = _mr([{"path": "models/mrt_BOTH.sql", "full_content": "SELECT 1"}])
    assert _run(find_spec(hub, mr, by_path=True)) == ("mrt_BOTH", "# 完整檔名優先")
    assert hub.calls == ["specs/mrt_BOTH.md"]


def test_gitlab_placeholder_or_non_spec_content_rejected(specs_dir):
    """get_file 對不存在的路徑回傳佔位文字或錯誤頁,不是丟例外——必須確認長得像
    規格(以 # 開頭),跟 R 編號那條路徑的判斷一致。"""
    hub = _FakeHub({"specs/RETAIL_M1.md": "<html>not a spec</html>"})
    assert _run(find_spec(hub, DBT_MR, by_path=True)) == (None, None)


def test_gitlab_exception_is_swallowed_and_next_candidate_tried(specs_dir):
    class _Flaky(_FakeHub):
        async def call(self, name, params, truncate=False):
            self.calls.append(params["path"])
            if params["path"] == "specs/mrt_RETAIL_M1.md":
                raise ConnectionError("暫時性網路錯誤")
            return "# 第二個候選"
    hub = _Flaky()
    assert _run(find_spec(hub, DBT_MR, by_path=True)) == ("RETAIL_M1", "# 第二個候選")


# ------------------------------------------------------ R 編號永遠優先
@pytest.mark.parametrize("by_path", [False, True])
def test_rule_code_path_unchanged(specs_dir, by_path):
    """有 R 編號時走原本的路徑,不論開關——即使檔名也對得到另一份規格。"""
    _write_spec(specs_dir, "R-201", "# R-201 規格全文")
    _write_spec(specs_dir, "RETAIL_M1", "# 這份不該被用到")
    mr = _mr(DBT_MR["files"], description="對應 R-201")
    assert _run(find_spec(_FakeHub(), mr, by_path=by_path)) == ("R-201", "# R-201 規格全文")


def test_rule_code_without_spec_not_rescued_by_path(specs_dir):
    """MR 提到了 R 編號但那份規格不存在:要如實回報「無規格」,不能被檔名對應蓋掉。"""
    _write_spec(specs_dir, "RETAIL_M1", "# 檔名對得到")
    mr = _mr(DBT_MR["files"], description="對應 R-999")
    assert _run(find_spec(_FakeHub(), mr, by_path=True)) == ("R-999", None)


# ------------------------------------------------------------ 路徑逃逸
@pytest.mark.parametrize("malicious_path", [
    "../../../etc/passwd.sql",
    "..\\..\\config\\sandbox.env.sql",
    "/etc/passwd.sql",
    "C:/Windows/win.ini.sql",
    "models/../../specs/../../../etc/shadow.sql",
    "models/\x00null.sql",
])
def test_path_traversal_produces_no_candidates(specs_dir, malicious_path):
    """model 路徑來自待審 MR,是攻擊者可控字串。resolve_spec 的路徑白名單必須先擋下,
    本機不讀、GitLab 一次都不問——確認這條新接線沒有另開後門。"""
    hub = _FakeHub()
    mr = _mr([{"path": malicious_path, "full_content": "SELECT 1"}])
    assert _run(find_spec(hub, mr, by_path=True)) == (None, None)
    assert hub.calls == []


# ============================================================ 執行驗證
# 規格找到了,但 SQL 是 dbt 樣板:不可原樣送進沙盒(只會得到語法錯誤,然後回報
# 「SQL 無法在測資上執行、請修正 SQL」——錯誤地指控開發者的程式壞了)。

class _Cfg:
    dbt = {"enabled": True, "database": "DBT_PLACEHOLDER"}


@pytest.fixture
def no_llm_no_sandbox(monkeypatch):
    """任何 LLM 呼叫或沙盒執行都視為失敗:樣板的情境必須在這兩步之前就停下來。"""
    import orchestrator.spec_exec as spec_exec_mod

    async def _no_llm(*a, **k):
        raise AssertionError("不該呼叫測資生成 LLM")

    def _no_sandbox(*a, **k):
        raise AssertionError("不該把 SQL 送進沙盒")
    monkeypatch.setattr(spec_exec_mod, "generate_cases", _no_llm)
    monkeypatch.setattr(spec_exec_mod, "execute_cases", _no_sandbox)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM {{ ref('txn_log') }}",
    "{% set x = 1 %}SELECT {{ x }}",
    "{# 註解 #}SELECT 1",
])
def test_dbt_template_never_sent_to_sandbox(no_llm_no_sandbox, sql):
    mr = _mr([{"path": "models/mrt_x.sql", "full_content": sql}])
    result = _run(run_spec_exec(_Cfg(), None, mr, "RETAIL_M1", "# 規格"))
    assert result["passed"] is False
    assert result["dbt_template"] is True
    [finding] = result["findings"]
    assert finding["severity"] == "major"
    assert "dbt 樣板" in finding["title"]
    assert "修正 SQL" not in finding["suggestion"]    # 不可暗示是開發者的程式有錯


def test_no_spec_takes_precedence_over_template_guard(no_llm_no_sandbox):
    """沒有規格時維持原本的「無規格可驗」訊息,不被樣板檢查蓋掉。"""
    mr = _mr([{"path": "models/mrt_x.sql", "full_content": "SELECT {{ 1 }}"}])
    result = _run(run_spec_exec(_Cfg(), None, mr, None, None))
    assert result.get("no_spec") is True


def test_run_spec_exec_tolerates_cfg_without_dbt(no_llm_no_sandbox, specs_dir):
    """呼叫端可能傳沒有 dbt 欄位的替身 cfg(例如其他測試):視為未開啟,不可炸掉。"""
    class _BareCfg:
        pass
    result = _run(run_spec_exec(_BareCfg(), None, DBT_MR))
    assert result.get("no_spec") is True


# =================================================================
# 第二步:預掃前先展開(#7 第 1 點的管線端;config.dbt 開關)
# =================================================================

class _FakePrescanHub:
    """只實作 prescan() 用得到的 call_json(),記錄每次呼叫實際送出的 sql,
    用來驗證「展開後的 SQL 有沒有真的被拿去跑規則層」。"""

    def __init__(self):
        self.rules_calls: list[str] = []
        self.lint_calls: list[str] = []

    async def call_json(self, name, args):
        if name == "sqltools__run_rules":
            self.rules_calls.append(args["sql"])
            return []
        if name == "sqltools__lint":
            self.lint_calls.append(args["sql"])
            return []
        raise AssertionError(f"未預期的工具呼叫:{name}")


DBT_MODEL = "SELECT * FROM {{ ref('txn_log') }} WHERE amount > 1000"
PLAIN_SQL = "SELECT * FROM txn_log WHERE amount > 1000"


def _files(path, content):
    return [{"path": path, "full_content": content}]


# ---------------------------------------------- 關閉時(預設)行為完全不變
def test_prescan_dbt_cfg_none_leaves_dbt_file_unrendered(render_must_not_run):
    """dbt_cfg 完全不給(呼叫端沒傳,例如舊程式碼)——必須是安全的預設,
    不能因為忘記傳這個參數就意外展開。"""
    hub = _FakePrescanHub()
    _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL)))
    assert hub.rules_calls == [DBT_MODEL]   # 原樣送進規則層,樣板沒被展開


# 下面三條「應維持關閉」的測試,資料庫名刻意給**正確的**約定假名:否則就算開關
# 判斷壞了,也會被後面的資料庫名檢查擋下、照樣看起來「沒展開」,測試就驗不到開關
# 本身(突變測試實際抓到過這個遮蔽)。所以除了結果,也斷言根本沒有嘗試展開。
def test_prescan_dbt_cfg_disabled_leaves_dbt_file_unrendered(render_must_not_run):
    hub = _FakePrescanHub()
    entries = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                           {"enabled": False, "database": DBT_DATABASE_PLACEHOLDER}))
    assert hub.rules_calls == [DBT_MODEL]
    assert "dbt_render_error" not in entries[0]


def test_prescan_dbt_cfg_missing_enabled_key_defaults_off(render_must_not_run):
    """dbt_cfg 給了字典,但沒有 enabled 這個鍵——一樣視為關閉,不是預設開啟。"""
    hub = _FakePrescanHub()
    entries = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                           {"database": DBT_DATABASE_PLACEHOLDER}))
    assert hub.rules_calls == [DBT_MODEL]
    assert "dbt_render_error" not in entries[0]


def test_prescan_dbt_cfg_truthy_but_not_bool_true_does_not_enable(render_must_not_run):
    """enabled 是非布林的真值(例如字串)時**不能**被當成開啟。config.py 的
    _load_dbt_section() 會在設定檔載入時就擋掉這種值,但 prescan() 自己也要有
    這道防線——它是這個模組唯一真正決定「要不要展開」的地方,不能只依賴
    上游有做過檢查。"""
    hub = _FakePrescanHub()
    entries = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                           {"enabled": "true", "database": DBT_DATABASE_PLACEHOLDER}))
    assert hub.rules_calls == [DBT_MODEL]
    assert "dbt_render_error" not in entries[0]


def test_prescan_plain_sql_never_touched_even_when_enabled():
    """不是 dbt 樣板的檔案,開啟時也不該被送去展開(沒有 Jinja 標記,
    is_dbt_template() 為 False,直接跳過,省一次子行程開銷)。"""
    hub = _FakePrescanHub()
    _run(prescan(hub, _files("sql/rules/r201.sql", PLAIN_SQL),
                {"enabled": True, "database": "DBT_PLACEHOLDER"}))
    assert hub.rules_calls == [PLAIN_SQL]


# ---------------------------------------------------------- 開啟時的行為
def test_prescan_enabled_expands_dbt_template():
    """核心行為:開啟後,dbt 樣板展開成純 SQL 才送進規則層。"""
    hub = _FakePrescanHub()
    entries = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                           {"enabled": True, "database": "DBT_PLACEHOLDER"}))
    assert hub.rules_calls == ['SELECT * FROM "DBT_PLACEHOLDER"."dbo"."txn_log" WHERE amount > 1000']
    assert "dbt_render_error" not in entries[0]


def test_prescan_surfaces_relation_notice_when_ref_used():
    """#10 review 要求:ref()/source() 展開出的表名有已知精確度落差,接線時要讓
    審查者看得到,不能只是展開「成功」就沒事——DBT_MODEL 用了 ref(),提醒要出現。"""
    hub = _FakePrescanHub()
    entries = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                           {"enabled": True, "database": "DBT_PLACEHOLDER"}))
    assert "ref()" in entries[0]["dbt_render_notice"]


def test_prescan_no_relation_notice_when_ref_not_used():
    """沒用到 ref()/source() 的 dbt 樣板(例如只用了 var())不該被貼提醒。"""
    hub = _FakePrescanHub()
    src = "SELECT {{ var('threshold', 1000) }} AS threshold"
    entries = _run(prescan(hub, _files("models/mrt_x.sql", src),
                           {"enabled": True, "database": "DBT_PLACEHOLDER"}))
    assert "dbt_render_notice" not in entries[0]


def test_prescan_no_relation_notice_when_disabled():
    """開關關閉時完全不展開,自然也不會有這個提醒——確認它不會憑空冒出來。"""
    hub = _FakePrescanHub()
    entries = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL)))
    assert "dbt_render_notice" not in entries[0]


def test_prescan_lint_stays_on_original_text_not_rendered_sql():
    """lint 的違規結果帶行號(sqlfluff 的 start_line_no),但 LLM 看到的 diff
    用的是原始檔案行號——兩個基準不一致時,LLM 有機會把展開後的行號誤植進
    finding,指向原始檔案中錯誤的位置。行號能可靠對回原始檔前(#7 第 4 點,
    尚未接上),lint 必須一直吃原始文字,只有 rule-base(不帶行號)才吃
    展開後的 SQL。這條測試把這個決定鎖住,不讓未來的重構不小心把兩者對齊。"""
    hub = _FakePrescanHub()
    _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                {"enabled": True, "database": "DBT_PLACEHOLDER"}))
    assert hub.lint_calls == [DBT_MODEL]                      # 原始 Jinja 文字
    assert hub.rules_calls != hub.lint_calls                   # 規則層吃的是展開後的


def test_prescan_enabled_without_database_fails_closed_not_silently():
    """database 沒填(config 預設值)時,用到 ref()/source() 的 model 一定展開
    失敗——這是刻意的安全預設(見 config/models.yaml 的 dbt 區塊註解),不是 bug。
    展開失敗要退回原樣文字,讓既有的 parse_error 路徑接手,而不是憑空造一個
    看似合理的表名。"""
    hub = _FakePrescanHub()
    entries = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                           {"enabled": True, "database": ""}))
    assert hub.rules_calls == [DBT_MODEL]              # 退回原樣,不是亂猜的 SQL
    assert entries[0]["dbt_render_error"]               # 但原因要留痕,不能無聲無息


def test_prescan_enabled_macro_not_available_fails_closed():
    """呼叫到自訂 macro 的 model——目前沒有列目錄的 GitLab 工具,沒有 macro 目錄
    可餵,展開一定失敗。這跟現在完全沒接線時的行為(sqlglot 直接在 Jinja 標記上
    解析失敗)效果一致:都是 fail closed,不會因為「有試著展開」就多一種
    看似成功、實則錯誤的 SQL。"""
    hub = _FakePrescanHub()
    src = "SELECT * FROM t WHERE {{ is_large_amount('t') }}"
    entries = _run(prescan(hub, _files("models/mrt_x.sql", src),
                           {"enabled": True, "database": "DBT_PLACEHOLDER"}))
    assert hub.rules_calls == [src]
    assert entries[0]["dbt_render_error"]


def test_prescan_render_error_recorded_and_not_mixed_into_sql():
    """展開失敗時,錯誤原因只記在 entry["dbt_render_error"],不會混進
    送給規則層/LLM 的 sql 字串本身——兩者要嚴格分開,不能把錯誤訊息
    意外串進使用者看到的 SQL 內容裡。"""
    hub = _FakePrescanHub()
    src = "SELECT {{ target.database }}"   # 沒給 database,一定展開失敗
    entries = _run(prescan(hub, _files("models/mrt_x.sql", src),
                           {"enabled": True, "database": ""}))
    assert hub.rules_calls == [src]
    assert entries[0]["dbt_render_error"] not in hub.rules_calls[0]


def test_prescan_multiple_files_only_dbt_ones_expanded():
    """一個 MR 同時改了 dbt model 與一般 SQL 檔:只有前者被展開。"""
    hub = _FakePrescanHub()
    files = [{"path": "models/mrt_x.sql", "full_content": DBT_MODEL},
            {"path": "sql/rules/r201.sql", "full_content": PLAIN_SQL}]
    _run(prescan(hub, files, {"enabled": True, "database": "DBT_PLACEHOLDER"}))
    assert hub.rules_calls == [
        'SELECT * FROM "DBT_PLACEHOLDER"."dbo"."txn_log" WHERE amount > 1000',
        PLAIN_SQL,
    ]


def test_prescan_model_path_with_traversal_does_not_touch_filesystem():
    """f["path"] 來自待審 MR,是攻擊者可控字串。source= 已經給了完整內容,
    render_model_isolated 不該因為 path 長得像逃逸路徑就嘗試讀取檔案系統——
    這裡驗證的是展開仍然成功、用的是 source 給的內容,不是意外讀到某個
    真實檔案(讀到的話結果會明顯不同,或直接失敗)。"""
    hub = _FakePrescanHub()
    src = "SELECT {{ 1 + 1 }} AS two"
    entries = _run(prescan(hub, _files("../../../../etc/passwd", src),
                           {"enabled": True, "database": "DBT_PLACEHOLDER"}))
    assert hub.rules_calls == ["SELECT 2 AS two"]
    assert "dbt_render_error" not in entries[0]


def test_prescan_undefined_var_fails_closed_cleanly():
    """展開失敗的路徑(缺 macro、缺 var、缺 database……)都要乾淨地收斂成
    ok=False,不能卡住或丟未捕捉的例外一路炸穿 prescan()——子行程逾時與
    記憶體保護本身由 dbt_render 自己的測試涵蓋,這裡只驗證 prescan 這層
    確實把控制權交給 render_model_isolated,而不是繞過它直接同步渲染。"""
    hub = _FakePrescanHub()
    src = "SELECT {{ var('undeclared_var') }}"
    entries = _run(prescan(hub, _files("models/mrt_x.sql", src),
                           {"enabled": True, "database": "DBT_PLACEHOLDER"}))
    assert hub.rules_calls == [src]
    assert entries[0]["dbt_render_error"]


# --------------------------------------------------------- config.dbt 驗證
def test_load_dbt_section_defaults_to_disabled_when_key_missing(no_db_env):
    assert _load_dbt_section({}) == {"enabled": False, "database": ""}


def test_load_dbt_section_accepts_explicit_values(no_db_env):
    raw = {"dbt": {"enabled": True, "database": "DBT_PLACEHOLDER"}}
    assert _load_dbt_section(raw) == {"enabled": True, "database": "DBT_PLACEHOLDER"}


@pytest.mark.parametrize("bad_enabled", ["true", "false", "1", "0", 1, 0, None, [], {}])
def test_load_dbt_section_rejects_non_bool_enabled(bad_enabled):
    """尤其是字串 "false":Python 的 bool("false") 是 True,這種筆誤絕不能
    被靜默接受成「真的開啟了」。"""
    with pytest.raises(ValueError):
        _load_dbt_section({"dbt": {"enabled": bad_enabled}})


def test_load_dbt_section_rejects_non_string_database():
    with pytest.raises(ValueError):
        _load_dbt_section({"dbt": {"enabled": False, "database": 123}})


@pytest.fixture
def no_db_env(monkeypatch):
    monkeypatch.delenv("SEGCRA_DBT_DATABASE", raising=False)


def test_database_from_env_var_overrides_file(monkeypatch):
    """資料庫名由環境變數提供,設定檔裡的值只當後備(repo 是公開的,不寫進版控)。"""
    monkeypatch.setenv("SEGCRA_DBT_DATABASE", "DBT_PLACEHOLDER")
    raw = {"dbt": {"enabled": True, "database": "IGNORED"}}
    assert _load_dbt_section(raw)["database"] == "DBT_PLACEHOLDER"


def test_database_falls_back_to_file_without_env(no_db_env):
    assert _load_dbt_section({"dbt": {"database": "SAMPLE_DW"}})["database"] == "SAMPLE_DW"


@pytest.mark.parametrize("bad", ['PROD"; DROP TABLE t; --', "PROD DW", "PROD-DW",
                                 "PROD.dbo", "ＰＲＯＤ", " PROD_DW", "1PROD"])
def test_database_must_be_identifier(monkeypatch, bad):
    """名稱會被拼進 SQL 識別字,載入時就過白名單;填錯要在啟動時就報錯,
    不是等到每份 model 審查時才各自展開失敗。"""
    monkeypatch.setenv("SEGCRA_DBT_DATABASE", bad)
    with pytest.raises(ValueError) as exc:
        _load_dbt_section({})
    # 錯誤訊息不回顯名稱本身:正式資料庫名不該因此出現在日誌裡
    assert bad.strip() not in str(exc.value)


def test_empty_env_var_means_unset(monkeypatch):
    monkeypatch.setenv("SEGCRA_DBT_DATABASE", "")
    assert _load_dbt_section({})["database"] == ""


def test_load_dbt_section_rejects_non_dict_section():
    with pytest.raises(ValueError):
        _load_dbt_section({"dbt": "enabled"})


def test_config_dataclass_default_is_disabled():
    """直接建構 Config(...)(例如測試、或未來新腳本)沒給 dbt 時,
    落在安全狀態,不是拋例外也不是意外開啟。"""
    cfg = Config(endpoint="http://x/v1", api_key="k", default_profile="review",
                profiles={}, roles={}, budget={}, policy={})
    assert cfg.dbt == {"enabled": False, "database": ""}


def test_real_models_yaml_defaults_dbt_disabled(no_db_env):
    """對正式的 config/models.yaml 做一次真的載入——不是造假資料,是驗證
    這次改動真的以安全的狀態進到設定檔裡,而不是只有測試裡的假設定安全。"""
    cfg = load_config()
    assert cfg.dbt["enabled"] is False


# ------------------------------- relation_notice 確定性揭露(#14 review 合併前要求)
# prescan 的 dbt_render_notice 只會隨整包 JSON 送進模型的任務描述;報告裡有沒有這句
# 提醒不能取決於模型願不願意轉述。enforce_dbt_notice 在後處理鏈上強制補一條 info。


def _notice_findings(report):
    return [f for f in report.get("findings", []) if f.get("title") == _DBT_NOTICE_TITLE]


def test_notice_added_even_when_model_said_nothing():
    """模型的報告完全沒提 → 程式一定補上,內容是固定文字,落在正確的檔案。"""
    pre = [{"path": "models/mrt_x.sql", "rules": [], "dbt_render_notice": _RELATION_NOTICE}]
    report = enforce_dbt_notice({"findings": []}, pre)
    [f] = _notice_findings(report)
    assert f == {"file": "models/mrt_x.sql", "line": 0, "severity": "info",
                 "title": _DBT_NOTICE_TITLE, "detail": _RELATION_NOTICE,
                 "suggestion": f["suggestion"], "citations": []}
    assert f["suggestion"]


def test_notice_added_when_report_has_no_findings_key():
    """模型輸出連 findings 鍵都沒有(常見的格式漂移)也要補得進去,不可丟例外。"""
    pre = [{"path": "models/mrt_x.sql", "rules": [], "dbt_render_notice": _RELATION_NOTICE}]
    assert len(_notice_findings(enforce_dbt_notice({}, pre))) == 1


@pytest.mark.parametrize("entry", [
    {"path": "models/mrt_x.sql", "rules": []},                               # 沒用 ref()
    {"path": "models/mrt_x.sql", "rules": [], "dbt_render_notice": None},
    {"path": "models/mrt_x.sql", "rules": [], "dbt_render_notice": ""},
    {"path": "models/mrt_x.sql", "rules": [], "parse_error": "x",            # 展開失敗
     "dbt_render_error": "x"},
])
def test_no_notice_without_relation_usage(entry):
    """沒有提醒時不可憑空補:否則每個 dbt 檔都被貼提醒,審查者會開始忽略它。"""
    assert _notice_findings(enforce_dbt_notice({"findings": []}, [entry])) == []


def test_notice_not_duplicated_and_idempotent():
    """同一檔已有這條(例如管線重跑後處理)不重複;每個檔各自一條。"""
    pre = [{"path": "models/a.sql", "rules": [], "dbt_render_notice": _RELATION_NOTICE},
           {"path": "models/b.sql", "rules": [], "dbt_render_notice": _RELATION_NOTICE},
           {"path": "models/a.sql", "rules": [], "dbt_render_notice": _RELATION_NOTICE}]
    report = enforce_dbt_notice({"findings": []}, pre)
    report = enforce_dbt_notice(report, pre)
    assert sorted(f["file"] for f in _notice_findings(report)) == ["models/a.sql",
                                                                   "models/b.sql"]


def test_notice_keeps_model_findings_untouched():
    """只追加,不改動、不刪除模型原本的 finding。"""
    original = {"file": "models/a.sql", "line": 3, "severity": "major", "title": "t",
                "detail": "d", "suggestion": "", "citations": []}
    pre = [{"path": "models/a.sql", "rules": [], "dbt_render_notice": _RELATION_NOTICE}]
    report = enforce_dbt_notice({"findings": [dict(original)]}, pre)
    assert report["findings"][0] == original
    assert len(report["findings"]) == 2


def test_review_mr_enforces_notice_after_keyword_checks_and_before_policy():
    """review_mr() 的後處理鏈必須呼叫它:
    * 在 enforce_hints / enforce_style 之後——它們以「報告全文含關鍵字」判斷模型是否
      已回應檢核點,程式補的文字若先進報告,可能被誤當成回應而吞掉檢核點
    * 在 apply_policy 之前——決策要看得到它"""
    import inspect
    import orchestrator.pipeline as pipeline_mod
    src = inspect.getsource(pipeline_mod.review_mr)
    call = "report = enforce_dbt_notice(report, pre)"
    assert src.count(call) == 1
    assert src.index("report = enforce_hints(report, pre)") < src.index(call)
    assert src.index("report = enforce_style(report, pre)") < src.index(call)
    assert src.index(call) < src.index("report = apply_policy(")


def test_notice_text_never_masks_an_unanswered_hint():
    """就算提醒的措辭哪天含有檢核點關鍵字(例如 H003 的「規格」),依管線順序執行時,
    模型沒回應的檢核點仍要被補出來,不能被程式自己補的文字吞掉。"""
    notice ="表名與規格核定的不一定相同"          # 故意含 H003 的關鍵字
    pre = [{"path": "models/mrt_x.sql", "dbt_render_notice": notice,
            "rules": [{"rule": "H003", "severity": "hint", "message": "涉及 R-101"}]}]
    report = enforce_dbt_notice(enforce_hints({"findings": []}, pre), pre)
    titles = [f["title"] for f in report["findings"]]
    assert any(t.startswith("[H003]") for t in titles), titles
    assert _DBT_NOTICE_TITLE in titles


class _FakeDryRunHub:
    def __init__(self):
        self.posted: list[dict] = []

    async def call_json(self, name, args):
        assert name == "memory__lookup_similar_reviews"
        return []

    async def call(self, name, args):
        self.posted.append({"name": name, **args})


def test_dry_run_also_surfaces_notice():
    """不經 LLM 的 dry-run 也會揭露 parse_error;提醒也要一樣出現,並貼回 MR。"""
    hub = _FakeDryRunHub()
    pre = [{"path": "models/mrt_x.sql", "rules": [], "dbt_render_notice": _RELATION_NOTICE}]
    report = _run(_dry_run_report(hub, "1", {"files": []}, pre, None, None))
    assert len(_notice_findings(report)) == 1
    assert any(p["name"] == "gitlab__post_inline_comment"
               and p["body"] == _DBT_NOTICE_TITLE for p in hub.posted)


def test_notice_end_to_end_from_real_render_and_does_not_change_decision():
    """真的展開(子行程)→ prescan 帶提醒 → 補 finding → 決策閘門。
    info 不影響決策:其他條件都乾淨時,有沒有提醒結果都一樣。"""
    hub = _FakePrescanHub()
    pre = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                       {"enabled": True, "database": "DBT_PLACEHOLDER"}))
    policy = {"auto_approve": {"max_diff_lines": 30, "min_score": 95,
                               "allowed_severities": ["info"],
                               "forbid_pending_hints": True},
              "block": {"min_blockers": 1}}
    mr = {"files": [{"path": "models/mrt_x.sql", "diff": "+x"}]}

    def _base():
        return {"score": 100, "findings": [], "_spec_exec": {"passed": True}}

    with_notice = apply_policy(enforce_dbt_notice(_base(), pre), mr, policy)
    without = apply_policy(_base(), mr, policy)
    assert len(_notice_findings(with_notice)) == 1
    assert with_notice["decision"] == without["decision"] == "auto_approved"


# ------------------------------------------ 資料庫假名固定並寫進文件(#14 review)
_PLACEHOLDER_SETTING = "SEGCRA_DBT_DATABASE=DBT_PLACEHOLDER"
_REPO = pathlib.Path(__file__).resolve().parents[1]


def test_placeholder_database_documented_identically_in_config_and_docs():
    """假名必須固定(golden set 的標準答案才不會飄),而且設定檔註解與文件要寫同一個值。"""
    yaml_text = (_REPO / "config" / "models.yaml").read_text(encoding="utf-8")
    doc_text = (_REPO / "docs" / "09-dbt展開與反查.md").read_text(encoding="utf-8")
    assert _PLACEHOLDER_SETTING in yaml_text
    assert _PLACEHOLDER_SETTING in doc_text


def test_placeholder_database_passes_loader_and_renders(monkeypatch):
    """文件寫的假名要真的能用:過載入時的識別字白名單,且能展開 ref()。"""
    monkeypatch.setenv("SEGCRA_DBT_DATABASE", _PLACEHOLDER_SETTING.split("=", 1)[1])
    dbt = _load_dbt_section({"dbt": {"enabled": True}})
    assert dbt["database"] == "DBT_PLACEHOLDER"
    hub = _FakePrescanHub()
    _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL), dbt))
    assert hub.rules_calls == [
        'SELECT * FROM "DBT_PLACEHOLDER"."dbo"."txn_log" WHERE amount > 1000']


def test_tracked_config_still_has_no_database_value(no_db_env):
    """假名走環境變數,設定檔的 database 仍是空字串(維持「不寫進版控」的做法)。"""
    assert load_config().dbt["database"] == ""


# ---------------------------- 資料庫名必須是約定的假名(#14 合併前 review 最後一點)
# 漏設或拼錯時展開不會報錯,只會產出錯的表名,報告看起來卻完全正常。兩道檢查:
# 載入設定時擋(不讓審查帶著錯的設定啟動),展開前再擋(dbt_cfg 可能不經 load_config)。
_WRONG_DATABASES = ["DBT_PLACEHOLDR",      # 拼錯
                    "dbt_placeholder",     # 大小寫不同也不算
                    "DBT_PLACEHOLDER_",    # 多一個字元
                    "PROD_DW"]             # 有人照舊習慣填了真名


def test_placeholder_constant_matches_documented_value():
    """程式裡的約定值要與設定檔註解、docs/09 寫的是同一個(那兩處另有測試互相比對)。"""
    assert DBT_DATABASE_PLACEHOLDER == _PLACEHOLDER_SETTING.split("=", 1)[1]


@pytest.mark.parametrize("wrong", _WRONG_DATABASES)
def test_load_rejects_wrong_database_when_enabled(monkeypatch, wrong):
    monkeypatch.setenv("SEGCRA_DBT_DATABASE", wrong)
    with pytest.raises(ValueError) as exc:
        _load_dbt_section({"dbt": {"enabled": True}})
    msg = str(exc.value)
    assert DBT_DATABASE_PLACEHOLDER in msg and "SEGCRA_DBT_DATABASE" in msg
    assert wrong not in msg.replace(DBT_DATABASE_PLACEHOLDER, "")   # 不回顯實際的值


def test_load_rejects_missing_database_when_enabled(no_db_env):
    """開啟卻漏設環境變數:以前會等到每個檔各自展開失敗,現在載入時就擋下。"""
    with pytest.raises(ValueError, match="SEGCRA_DBT_DATABASE"):
        _load_dbt_section({"dbt": {"enabled": True}})


@pytest.mark.parametrize("database", ["", "SAMPLE_DW", "DBT_PLACEHOLDER"])
def test_disabled_does_not_require_placeholder(no_db_env, database):
    """關閉時不用展開,也就不檢查——不能因為這道檢查讓預設關閉的設定載入失敗。"""
    raw = {"dbt": {"enabled": False, "database": database}}
    assert _load_dbt_section(raw) == {"enabled": False, "database": database}


@pytest.fixture
def render_must_not_run(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("資料庫名不符時不可展開")
    monkeypatch.setattr(_pipeline_mod, "render_model_isolated", _boom)


@pytest.mark.parametrize("wrong", _WRONG_DATABASES + ["", None])
def test_prescan_refuses_to_render_with_wrong_database(render_must_not_run, wrong):
    """不經 load_config 直接傳入 dbt_cfg 時,展開前也要擋:不展開、退回原文,
    讓既有的 parse_error → enforce_parse 交人工;原因要留痕,且不回顯實際的值。"""
    hub = _FakePrescanHub()
    entries = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                           {"enabled": True, "database": wrong}))
    assert hub.rules_calls == [DBT_MODEL]
    err = entries[0]["dbt_render_error"]
    assert DBT_DATABASE_PLACEHOLDER in err
    if wrong:
        assert wrong not in err.replace(DBT_DATABASE_PLACEHOLDER, "")
    assert "dbt_render_notice" not in entries[0]


def test_prescan_wrong_database_does_not_touch_plain_sql(render_must_not_run):
    """純 SQL 本來就不展開,資料庫名不符也不影響它的規則檢查。"""
    hub = _FakePrescanHub()
    entries = _run(prescan(hub, _files("sql/rules/r201.sql", PLAIN_SQL),
                           {"enabled": True, "database": "PROD_DW"}))
    assert hub.rules_calls == [PLAIN_SQL]
    assert "dbt_render_error" not in entries[0]


# ------------------------------------ 展開失敗的確定性揭露(#16 review)
# 以前只靠「未展開的原文讓 sqlglot 解析失敗 → enforce_parse」間接揭露。model 主體
# 就是一句 macro 呼叫時,原文解析得過、0 條規則命中,報告卻沒有任何訊號。
_INJECTION_MACROS = (
    "{% macro purge() %}\nDELETE FROM txn_staging\n{% endmacro %}\n"
    "{% macro q() %}'; DELETE FROM txn_staging; --{% endmacro %}\n"
    "{% macro c() %}*/ DELETE FROM txn_staging; /*{% endmacro %}\n"
    '{% macro dq() %}x"; DELETE FROM txn_staging; --{% endmacro %}\n')

# 標記出現的位置 → model 原文。後四種是「看起來不影響 SQL」的位置
_MACRO_POSITIONS = {
    "whole-model": "{{ purge() }}",
    "line-comment": "SELECT 1 AS a;\n-- {{ purge() }}",
    "block-comment": "SELECT 1 AS a; /* {{ c() }} */",
    "string-literal": "SELECT '{{ q() }}' AS a",
    "quoted-identifier": 'SELECT 1 AS "{{ dq() }}"',
}


def _rule_codes(sql):
    r = json.loads(_run_rules(sql))
    return [h["rule"] for h in (r["hits"] if isinstance(r, dict) else r)]


@pytest.mark.parametrize("position", list(_MACRO_POSITIONS))
def test_macro_output_escapes_comments_and_strings(tmp_path, position):
    """為什麼不能「標記只在註解/字串裡就不報」:macro 的輸出可以帶換行、*/、引號
    跳出去。拿得到 macro 時,四種位置展開後都是 DELETE 無 WHERE(R001 blocker);
    拿不到 macro 時展開失敗,規則層掃原文 0 命中。只有展開失敗的 finding 能揭露。"""
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "m.sql").write_text(_INJECTION_MACROS, encoding="utf-8")
    src = _MACRO_POSITIONS[position]
    with_macros = render_model("m.sql", code_root=tmp_path, source=src,
                               database=DBT_DATABASE_PLACEHOLDER)
    assert with_macros.ok, with_macros.error
    assert "R001" in _rule_codes(with_macros.sql)               # 實際會執行的 SQL
    assert _rule_codes(src) == []                               # 規則層看到的原文

    hub = _FakePrescanHub()
    pre = _run(prescan(hub, _files("models/m.sql", src),
                       {"enabled": True, "database": DBT_DATABASE_PLACEHOLDER}))
    assert pre[0]["dbt_render_error"]                           # 沒有 macro 目錄 → 失敗
    report = enforce_dbt_render_failure(enforce_parse({"findings": []}, pre), pre)
    [f] = report["findings"]
    assert f["title"] == _RENDER_FAIL_TITLE and f["severity"] == "major"


def test_render_failure_blocks_auto_approval_end_to_end():
    """整句 macro 呼叫、模型什麼都沒報:仍然不得自動放行。"""
    hub = _FakePrescanHub()
    pre = _run(prescan(hub, _files("models/m.sql", "{{ purge_staging() }}"),
                       {"enabled": True, "database": DBT_DATABASE_PLACEHOLDER}))
    assert "parse_error" not in pre[0]                          # 以前靠的那條路不成立
    policy = {"auto_approve": {"max_diff_lines": 30, "min_score": 95,
                               "allowed_severities": ["info"],
                               "forbid_pending_hints": True},
              "block": {"min_blockers": 1}}
    report = {"score": 100, "findings": [], "_spec_exec": {"passed": True}}
    report = enforce_dbt_render_failure(enforce_parse(report, pre), pre)
    report = apply_policy(report, {"files": [{"path": "models/m.sql", "diff": "+x"}]}, policy)
    assert report["decision"] == "needs_human"


def test_render_success_is_not_reported():
    hub = _FakePrescanHub()
    pre = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                       {"enabled": True, "database": DBT_DATABASE_PLACEHOLDER}))
    assert enforce_dbt_render_failure({"findings": []}, pre)["findings"] == []


def test_disabled_is_not_reported(render_must_not_run):
    """關閉時不展開、也就沒有展開失敗:維持既有行為(樣板解析失敗走 enforce_parse)。"""
    hub = _FakePrescanHub()
    pre = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL), {"enabled": False}))
    assert enforce_dbt_render_failure({"findings": []}, pre)["findings"] == []


def test_database_mismatch_is_reported():
    """資料庫名不符而不展開,也是「規則掃的是原文」,一樣要揭露。"""
    hub = _FakePrescanHub()
    pre = _run(prescan(hub, _files("models/m.sql", "{{ purge_staging() }}"),
                       {"enabled": True, "database": "PROD_DW"}))
    [f] = enforce_dbt_render_failure({"findings": []}, pre)["findings"]
    assert DBT_DATABASE_PLACEHOLDER in f["detail"] and "PROD_DW" not in f["detail"]


def test_parse_and_render_failure_on_same_file_yield_one_finding():
    """同一個檔案兩條都成立時只留展開失敗那條(較具體,且會提到原文也解析失敗)。"""
    pre = [{"path": "models/m.sql", "rules": [], "parse_error": "ParseError: x",
            "dbt_render_error": "UndefinedError: 'm' is undefined"}]
    report = enforce_dbt_render_failure(enforce_parse({"findings": []}, pre), pre)
    [f] = report["findings"]
    assert f["title"] == _RENDER_FAIL_TITLE and "也無法解析" in f["detail"]
    assert _PARSE_FAIL_TITLE not in [x["title"] for x in report["findings"]]


def test_parse_failure_without_dbt_is_still_reported_by_enforce_parse():
    """一般的解析失敗(沒有展開這回事)維持原本的揭露,不受影響。"""
    pre = [{"path": "sql/x.sql", "rules": [], "parse_error": "ParseError: x"}]
    report = enforce_dbt_render_failure(enforce_parse({"findings": []}, pre), pre)
    assert [f["title"] for f in report["findings"]] == [_PARSE_FAIL_TITLE]


def test_render_failure_not_duplicated_and_model_findings_kept():
    original = {"file": "models/m.sql", "line": 3, "severity": "minor", "title": "t",
                "detail": "d", "suggestion": "", "citations": []}
    pre = [{"path": "models/m.sql", "rules": [], "dbt_render_error": "E"}]
    report = enforce_dbt_render_failure({"findings": [dict(original)]}, pre)
    report = enforce_dbt_render_failure(report, pre)
    assert report["findings"][0] == original
    assert [f["title"] for f in report["findings"]].count(_RENDER_FAIL_TITLE) == 1


def test_review_mr_enforces_render_failure_after_keyword_checks_and_before_policy():
    import inspect
    src = inspect.getsource(_pipeline_mod.review_mr)
    call = "report = enforce_dbt_render_failure(report, pre)"
    assert src.count(call) == 1
    assert src.index("report = enforce_hints(report, pre)") < src.index(call)
    assert src.index("report = enforce_style(report, pre)") < src.index(call)
    assert src.index(call) < src.index("report = apply_policy(")


def test_dry_run_reports_render_failure_once():
    hub = _FakeDryRunHub()
    pre = [{"path": "models/m.sql", "rules": [], "parse_error": "ParseError: x",
            "dbt_render_error": "E"}]
    report = _run(_dry_run_report(hub, "1", {"files": []}, pre, None, None))
    assert [f["title"] for f in report["findings"]] == [_RENDER_FAIL_TITLE]
