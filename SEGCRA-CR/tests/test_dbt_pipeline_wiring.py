"""dbt 接進審查管線(#7 第 2、3 點的管線端)。

檔名刻意跟 dbt_render / dbt_impact 自己的單元測試檔分開,也跟 #12 即將加入的
tests/test_spec_exec.py 分開——這裡只測「接線」本身(find_spec 的路徑備援、
之後幾步的預掃/反查接線),不重覆模組自己的單元測試範圍。

每一步都要能獨立驗證「現有 golden case 的行為完全不變」,所以大多數測試案例
都成對出現:一個確認新行為生效,一個確認舊行為(有 R 編號時)分毫不變。
"""
import asyncio
import pathlib

import pytest

from orchestrator.config import Config, _load_dbt_section, load_config
from orchestrator.pipeline import prescan
from orchestrator.spec_exec import find_spec


def _run(coro):
    return asyncio.run(coro)


def _write_spec(dir_: pathlib.Path, name: str, body: str = "# 規格\n內容") -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{name}.md").write_text(body, encoding="utf-8")


def _mr(files, title="", description=""):
    return {"title": title, "description": description, "files": files}


class _FakeHub:
    """只實作 find_spec 會用到的 call(),記錄呼叫次數以驗證「有 R 編號時
    不會多探測路徑對應」。"""

    def __init__(self, contents: dict):
        self.contents = contents   # path -> 內容(或例外)
        self.calls: list[str] = []

    async def call(self, name, params, truncate=False):
        assert name == "gitlab__get_file"
        path = params["path"]
        self.calls.append(path)
        val = self.contents.get(path)
        if val is None:
            raise FileNotFoundError(path)
        return val


# --------------------------------------------------------- mock 模式(hub=None)
def test_mock_path_fallback_used_when_no_rule_code(tmp_path, monkeypatch):
    """#7 第 2 點的原始情境:純程式名的 model,完全沒有 R 編號。"""
    import orchestrator.spec_exec as spec_exec_mod

    _write_spec(tmp_path, "RETAIL_M1", "# R-201\n單日提領規則")
    monkeypatch.setattr(spec_exec_mod, "SPECS_DIR", tmp_path)

    mr = _mr([{"path": "models/mrt_RETAIL_M1.sql", "full_content": "SELECT 1"}])
    code, text = _run(find_spec(None, mr))
    assert code == "RETAIL_M1"
    assert text == "# R-201\n單日提領規則"


def test_mock_path_fallback_none_when_nothing_matches(tmp_path, monkeypatch):
    import orchestrator.spec_exec as spec_exec_mod
    monkeypatch.setattr(spec_exec_mod, "SPECS_DIR", tmp_path)

    mr = _mr([{"path": "models/mrt_UNKNOWN.sql", "full_content": "SELECT 1"}])
    assert _run(find_spec(None, mr)) == (None, None)


def test_mock_path_fallback_not_used_when_rule_code_present(tmp_path, monkeypatch):
    """有 R 編號時維持現行行為:即使檔名也對得到規格,一律以 R 編號為準。

    這是安全性的核心保證——現有 32 個 golden case 全部含 R 編號,這條測試
    確認新增的路徑對應分支對它們是不可觸達的死碼,不會改變任何既有結果。
    """
    import orchestrator.spec_exec as spec_exec_mod

    _write_spec(tmp_path, "R-201", "# R-201 規格全文")
    _write_spec(tmp_path, "RETAIL_M1", "# 這份不該被用到")
    monkeypatch.setattr(spec_exec_mod, "SPECS_DIR", tmp_path)

    mr = _mr([{"path": "models/mrt_RETAIL_M1.sql", "full_content": "SELECT 1"}],
             description="對應 R-201")
    code, text = _run(find_spec(None, mr))
    assert (code, text) == ("R-201", "# R-201 規格全文")


def test_mock_path_fallback_no_files_returns_none(tmp_path, monkeypatch):
    import orchestrator.spec_exec as spec_exec_mod
    monkeypatch.setattr(spec_exec_mod, "SPECS_DIR", tmp_path)
    assert _run(find_spec(None, _mr([]))) == (None, None)


def test_mock_path_fallback_first_matching_file_wins(tmp_path, monkeypatch):
    """一個 MR 改了多個檔案時,依檔案清單順序取第一個對得到規格的。"""
    import orchestrator.spec_exec as spec_exec_mod

    _write_spec(tmp_path, "SECOND", "# 第二個")
    monkeypatch.setattr(spec_exec_mod, "SPECS_DIR", tmp_path)

    mr = _mr([
        {"path": "models/mrt_FIRST.sql", "full_content": "SELECT 1"},   # 對不到規格
        {"path": "models/mrt_SECOND.sql", "full_content": "SELECT 2"},  # 對得到
    ])
    code, _ = _run(find_spec(None, mr))
    assert code == "SECOND"


def test_mock_path_fallback_non_sql_file_skipped(tmp_path, monkeypatch):
    """非 .sql 檔案(例如 yml)沒有辦法依檔名對應,不該讓函式炸掉。"""
    import orchestrator.spec_exec as spec_exec_mod
    monkeypatch.setattr(spec_exec_mod, "SPECS_DIR", tmp_path)

    mr = _mr([{"path": "models/sources.yml", "full_content": "version: 2"}])
    assert _run(find_spec(None, mr)) == (None, None)


# ------------------------------------------------------------- real 模式(hub)
# 這些案例都把 SPECS_DIR 指到空的 tmp_path:find_spec() 對有 R 編號的情況一律
# 先查本機 SPECS_DIR(mock 與 real 共用這段,見程式碼),不清掉的話會讀到 repo
# 裡真正的 specs/*.md,真假答案混在一起,測試就驗不到「hub 真的被呼叫了」。

@pytest.fixture
def empty_specs_dir(tmp_path, monkeypatch):
    import orchestrator.spec_exec as spec_exec_mod
    monkeypatch.setattr(spec_exec_mod, "SPECS_DIR", tmp_path)
    return tmp_path


def test_real_path_fallback_probes_gitlab(empty_specs_dir):
    hub = _FakeHub({"specs/RETAIL_M1.md": "# 從 GitLab 讀到的規格"})
    mr = _mr([{"path": "models/mrt_RETAIL_M1.sql", "full_content": "SELECT 1"}])
    code, text = _run(find_spec(hub, mr))
    assert (code, text) == ("RETAIL_M1", "# 從 GitLab 讀到的規格")
    # 候選順序是「完整檔名優先」:先探測 mrt_RETAIL_M1(不存在),才退到 RETAIL_M1
    assert hub.calls == ["specs/mrt_RETAIL_M1.md", "specs/RETAIL_M1.md"]


def test_real_path_fallback_full_name_candidate_wins_first(empty_specs_dir):
    """完整檔名(含 mrt_ 前綴)本身就有對應規格時,不必再探測第二個候選。"""
    hub = _FakeHub({"specs/mrt_BOTH.md": "# 完整檔名優先"})
    mr = _mr([{"path": "models/mrt_BOTH.sql", "full_content": "SELECT 1"}])
    code, text = _run(find_spec(hub, mr))
    assert (code, text) == ("mrt_BOTH", "# 完整檔名優先")
    assert hub.calls == ["specs/mrt_BOTH.md"]


def test_real_path_fallback_none_when_gitlab_has_nothing(empty_specs_dir):
    hub = _FakeHub({})
    mr = _mr([{"path": "models/mrt_UNKNOWN.sql", "full_content": "SELECT 1"}])
    assert _run(find_spec(hub, mr)) == (None, None)


def test_real_path_fallback_rejects_non_markdown_content(empty_specs_dir):
    """gitlab__get_file 對不存在的路徑可能回傳空字串或非規格內容(例如 GitLab
    的錯誤頁面文字),不是丟例外——不能只判斷「有沒有拿到東西」,要確認長得
    像規格(以 # 開頭),跟 R 編號那條路徑的判斷一致。"""
    hub = _FakeHub({"specs/mrt_RETAIL_M1.md": "not a spec, just some html or empty page",
                    "specs/RETAIL_M1.md": "also not a spec"})
    mr = _mr([{"path": "models/mrt_RETAIL_M1.sql", "full_content": "SELECT 1"}])
    assert _run(find_spec(hub, mr)) == (None, None)


def test_real_path_fallback_not_used_when_rule_code_present(empty_specs_dir):
    """有 R 編號時走原本的路徑,完全不會多打任何 gitlab__get_file 探測路徑對應
    的請求——避免真實環境下對有編號的 MR 產生不必要的額外 API 呼叫。"""
    hub = _FakeHub({"specs/R-201.md": "# R-201 規格"})
    mr = _mr([{"path": "models/mrt_RETAIL_M1.sql", "full_content": "SELECT 1"}],
             description="對應 R-201")
    code, text = _run(find_spec(hub, mr))
    assert (code, text) == ("R-201", "# R-201 規格")
    assert hub.calls == ["specs/R-201.md"]


def test_real_path_fallback_no_files_returns_none(empty_specs_dir):
    hub = _FakeHub({})
    assert _run(find_spec(hub, _mr([]))) == (None, None)


@pytest.mark.parametrize("malicious_path", [
    "../../../etc/passwd.sql",
    "..\\..\\config\\sandbox.env.sql",
    "/etc/passwd.sql",
    "C:/Windows/win.ini.sql",
    "models/../../specs/../../../etc/shadow.sql",
    "models/\x00null.sql",
])
def test_real_path_fallback_rejects_path_traversal(empty_specs_dir, malicious_path):
    """model_path 來自待審的 MR,是攻擊者可控的字串。resolve_spec()(dbt_impact 的
    normalize_path 白名單)必須先擋下逃逸路徑,產生的候選清單要是空的——不能讓
    這裡把 ../../config/sandbox.env 這種路徑原樣傳給 gitlab__get_file。

    這裡不是要重測 normalize_path 本身(dbt_impact 自己的測試已經涵蓋),而是
    確認*這條新接線*確實把攻擊面留給了那層白名單擋,自己沒有另開後門。
    """
    hub = _FakeHub({})   # 只要 hub 完全沒被呼叫就代表候選清單是空的
    mr = _mr([{"path": malicious_path, "full_content": "SELECT 1"}])
    assert _run(find_spec(hub, mr)) == (None, None)
    assert hub.calls == []


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
def test_load_dbt_section_defaults_to_disabled_when_key_missing():
    assert _load_dbt_section({}) == {"enabled": False, "database": ""}


def test_load_dbt_section_accepts_explicit_values():
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


def test_load_dbt_section_rejects_non_dict_section():
    with pytest.raises(ValueError):
        _load_dbt_section({"dbt": "enabled"})


def test_config_dataclass_default_is_disabled():
    """直接建構 Config(...)(例如測試、或未來新腳本)沒給 dbt 時,
    落在安全狀態,不是拋例外也不是意外開啟。"""
    cfg = Config(endpoint="http://x/v1", api_key="k", default_profile="review",
                profiles={}, roles={}, budget={}, policy={})
    assert cfg.dbt == {"enabled": False, "database": ""}


def test_real_models_yaml_defaults_dbt_disabled():
    """對正式的 config/models.yaml 做一次真的載入——不是造假資料,是驗證
    這次改動真的以安全的狀態進到設定檔裡,而不是只有測試裡的假設定安全。"""
    cfg = load_config()
    assert cfg.dbt["enabled"] is False
