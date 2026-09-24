"""dbt 接進審查管線(#7 第 1、2 點的管線端)。

檔名刻意跟 dbt_render / dbt_impact 自己的單元測試檔分開,也跟 #12 即將加入的
tests/test_spec_exec.py 分開——這裡只測「接線」本身(find_spec 的檔名對應、
預掃前展開、樣板不送進沙盒),不重覆模組自己的單元測試範圍。

每一步都要能獨立驗證「開關關閉時,行為與接線前完全相同」,所以大多數測試案例
都成對出現:一個確認新行為生效,一個確認舊行為分毫不變。
"""
import asyncio
import pathlib

import pytest

from orchestrator.config import Config, _load_dbt_section, load_config
from orchestrator.pipeline import prescan
from orchestrator.spec_exec import find_spec, run_spec_exec


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
    dbt = {"enabled": True, "database": "SAMPLE_DW"}


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
def test_prescan_dbt_cfg_none_leaves_dbt_file_unrendered():
    """dbt_cfg 完全不給(呼叫端沒傳,例如舊程式碼)——必須是安全的預設,
    不能因為忘記傳這個參數就意外展開。"""
    hub = _FakePrescanHub()
    _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL)))
    assert hub.rules_calls == [DBT_MODEL]   # 原樣送進規則層,樣板沒被展開


def test_prescan_dbt_cfg_disabled_leaves_dbt_file_unrendered():
    hub = _FakePrescanHub()
    _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                {"enabled": False, "database": "SAMPLE_DW"}))
    assert hub.rules_calls == [DBT_MODEL]


def test_prescan_dbt_cfg_missing_enabled_key_defaults_off():
    """dbt_cfg 給了字典,但沒有 enabled 這個鍵——一樣視為關閉,不是預設開啟。"""
    hub = _FakePrescanHub()
    _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL), {"database": "SAMPLE_DW"}))
    assert hub.rules_calls == [DBT_MODEL]


def test_prescan_dbt_cfg_truthy_but_not_bool_true_does_not_enable():
    """enabled 是非布林的真值(例如字串)時**不能**被當成開啟。config.py 的
    _load_dbt_section() 會在設定檔載入時就擋掉這種值,但 prescan() 自己也要有
    這道防線——它是這個模組唯一真正決定「要不要展開」的地方,不能只依賴
    上游有做過檢查。"""
    hub = _FakePrescanHub()
    _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                {"enabled": "true", "database": "SAMPLE_DW"}))
    assert hub.rules_calls == [DBT_MODEL]


def test_prescan_plain_sql_never_touched_even_when_enabled():
    """不是 dbt 樣板的檔案,開啟時也不該被送去展開(沒有 Jinja 標記,
    is_dbt_template() 為 False,直接跳過,省一次子行程開銷)。"""
    hub = _FakePrescanHub()
    _run(prescan(hub, _files("sql/rules/r201.sql", PLAIN_SQL),
                {"enabled": True, "database": "SAMPLE_DW"}))
    assert hub.rules_calls == [PLAIN_SQL]


# ---------------------------------------------------------- 開啟時的行為
def test_prescan_enabled_expands_dbt_template():
    """核心行為:開啟後,dbt 樣板展開成純 SQL 才送進規則層。"""
    hub = _FakePrescanHub()
    entries = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                           {"enabled": True, "database": "SAMPLE_DW"}))
    assert hub.rules_calls == ['SELECT * FROM "SAMPLE_DW"."dbo"."txn_log" WHERE amount > 1000']
    assert "dbt_render_error" not in entries[0]


def test_prescan_surfaces_relation_notice_when_ref_used():
    """#10 review 要求:ref()/source() 展開出的表名有已知精確度落差,接線時要讓
    審查者看得到,不能只是展開「成功」就沒事——DBT_MODEL 用了 ref(),提醒要出現。"""
    hub = _FakePrescanHub()
    entries = _run(prescan(hub, _files("models/mrt_x.sql", DBT_MODEL),
                           {"enabled": True, "database": "SAMPLE_DW"}))
    assert "ref()" in entries[0]["dbt_render_notice"]


def test_prescan_no_relation_notice_when_ref_not_used():
    """沒用到 ref()/source() 的 dbt 樣板(例如只用了 var())不該被貼提醒。"""
    hub = _FakePrescanHub()
    src = "SELECT {{ var('threshold', 1000) }} AS threshold"
    entries = _run(prescan(hub, _files("models/mrt_x.sql", src),
                           {"enabled": True, "database": "SAMPLE_DW"}))
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
                {"enabled": True, "database": "SAMPLE_DW"}))
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
                           {"enabled": True, "database": "SAMPLE_DW"}))
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
    _run(prescan(hub, files, {"enabled": True, "database": "SAMPLE_DW"}))
    assert hub.rules_calls == [
        'SELECT * FROM "SAMPLE_DW"."dbo"."txn_log" WHERE amount > 1000',
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
                           {"enabled": True, "database": "SAMPLE_DW"}))
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
                           {"enabled": True, "database": "SAMPLE_DW"}))
    assert hub.rules_calls == [src]
    assert entries[0]["dbt_render_error"]


# --------------------------------------------------------- config.dbt 驗證
def test_load_dbt_section_defaults_to_disabled_when_key_missing(no_db_env):
    assert _load_dbt_section({}) == {"enabled": False, "database": ""}


def test_load_dbt_section_accepts_explicit_values(no_db_env):
    raw = {"dbt": {"enabled": True, "database": "SAMPLE_DW"}}
    assert _load_dbt_section(raw) == {"enabled": True, "database": "SAMPLE_DW"}


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
    """正式資料庫名只放在部署機的環境變數:repo 是公開的,不可寫進進版控的設定檔。"""
    monkeypatch.setenv("SEGCRA_DBT_DATABASE", "PROD_DW")
    raw = {"dbt": {"enabled": True, "database": "IGNORED"}}
    assert _load_dbt_section(raw)["database"] == "PROD_DW"


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
