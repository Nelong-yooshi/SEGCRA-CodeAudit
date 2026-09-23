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
