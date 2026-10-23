"""取不到規格 ≠ 沒有規格(#90 第 2 點)——不呼叫 LLM、不連資料庫。

原本 `find_spec` 把「唯讀 token 沒設」「沒釘住 commit」「規格檔超過上限」「取檔失敗」
全部當成找不到規格,MR 上寫的是「請先補上規格」:環境出了問題,卻叫開發者去補一份
其實已經存在的規格;維運也看不出是 token 設錯了,因為每張 MR 都只說沒有規格。
"""
import asyncio

import pytest

from orchestrator import spec_exec
from orchestrator.spec_exec import SpecLookup, find_spec
from toolbox import gitlab

MR = {"title": "R-201 調整", "description": "", "files": [
    {"path": "sql/rules/r201.sql", "diff": "@@\n+SELECT 1", "full_content": "SELECT 1"}]}


class _Hub:
    """`get_file` 回什麼由測試決定;真實的 gitlab.get_file 就是回這些句子。"""

    def __init__(self, reply):
        self.reply = reply

    async def call(self, name, args, truncate=True):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture(autouse=True)
def _real_mode(monkeypatch):
    """real 模式才會走到 get_file;mock 模式會先讀本地 specs/。"""
    monkeypatch.setattr(spec_exec, "_real_mode", lambda: True)


@pytest.mark.parametrize("reply,code", [
    (gitlab._NO_READ_TOKEN, "no_read_token"),
    (gitlab._NO_REVIEW_REF, "no_review_ref"),
    ("(specs/R-201.md 超過單檔上限 1048576 bytes,未取回完整內容。請依 diff 判斷。)",
     "too_large"),
    (RuntimeError("連線掛了"), "fetch_failed"),
])
def test_拿不到的四種情況都記成拿不到(reply, code):
    lookup = asyncio.run(find_spec(_Hub(reply), MR))
    assert lookup.unavailable == code
    assert lookup == ("R-201", None), "規格碼照常帶出來,只是這次拿不到內容"


def test_檔案確實不存在不算拿不到():
    """repo 裡真的沒有那個檔,是 repo 的事實,該照實說「請補規格」。"""
    lookup = asyncio.run(find_spec(_Hub("(repo 內無 specs/R-201.md)"), MR))
    assert lookup.unavailable is None
    assert lookup == ("R-201", None)


def test_找得到時沒有拿不到的記號():
    lookup = asyncio.run(find_spec(_Hub("# R-201 規格\n內容"), MR))
    assert lookup.unavailable is None
    assert lookup[0] == "R-201" and lookup[1].startswith("#")


def test_結果仍然解得開成兩個值():
    """25 處呼叫端是 `code, text = await find_spec(...)`;換形狀不可以弄壞它們。"""
    code, text = asyncio.run(find_spec(_Hub("# R-201 規格"), MR))
    assert (code, text) == ("R-201", "# R-201 規格")
    assert SpecLookup("a", "b") == ("a", "b")


# ─────────────────── 接到報告上 ───────────────────

def _run(reply):
    return asyncio.run(spec_exec.run_spec_exec(None, _Hub(reply), MR, None, None))


def test_取不到規格時不要叫開發者補規格():
    out = _run(gitlab._NO_READ_TOKEN)
    assert out["passed"] is False
    assert out["spec_unavailable"] == "no_read_token"
    f = out["findings"][0]
    assert f["severity"] == "major"
    assert "取不到規格" in f["title"]
    assert "補" not in f["detail"].replace("不需要補規格", ""), f["detail"]
    assert "GITLAB_READ_TOKEN" in f["detail"], "維運要看得出是哪一個設定"


def test_真的沒有規格時照舊叫人補規格():
    out = _run("(repo 內無 specs/R-201.md)")
    assert out["no_spec"] is True
    assert "spec_unavailable" not in out
    assert "請先補上核定規格" in out["findings"][0]["detail"]


def test_取不到規格的訊息不帶原始錯誤():
    """訊息會貼回 MR:不可以出現例外類型、主機位址這類內部細節(#89 第 3 點)。"""
    out = _run(RuntimeError("[Errno 111] Connection refused to 10.0.0.9:443"))
    detail = out["findings"][0]["detail"]
    for leak in ("RuntimeError", "Errno", "10.0.0.9", "Connection refused"):
        assert leak not in detail, detail


def test_每個代碼都有給人看的說法():
    """新增代碼忘了寫說法的話,組訊息時會 KeyError,而那是在審查跑到一半的時候。"""
    assert set(spec_exec._SPEC_UNAVAILABLE) == {
        "no_read_token", "no_review_ref", "too_large", "fetch_failed"}


# ─────────────── 依檔名對應規格那條路(dbt 的 model 沒有 R 編號)───────────────

DBT_MR = {"title": "調整", "description": "", "files": [
    {"path": "models/mrt_RETAIL_M1.sql", "diff": "@@\n+SELECT 1",
     "full_content": "SELECT 1"}]}


@pytest.mark.parametrize("reply,code", [
    (gitlab._NO_READ_TOKEN, "no_read_token"),
    (RuntimeError("連線掛了"), "fetch_failed"),
])
def test_依檔名找規格時拿不到也要分出來(reply, code, monkeypatch, tmp_path):
    """dbt 的 model 沒有 R 編號,走的是這條路。只回 None 的話,token 沒設一樣會
    變成「沒有規格」—— 跟 R 編號那條是同一個病。"""
    monkeypatch.setattr(spec_exec, "SPECS_DIR", tmp_path)   # 本機沒有規格,才會問 GitLab
    lookup = asyncio.run(find_spec(_Hub(reply), DBT_MR, by_path=True))
    assert lookup.unavailable == code
    assert lookup == (None, None)


def test_依檔名找規格_檔案不存在不算拿不到(monkeypatch, tmp_path):
    monkeypatch.setattr(spec_exec, "SPECS_DIR", tmp_path)
    lookup = asyncio.run(find_spec(_Hub("(repo 內無 specs/RETAIL_M1.md)"),
                                   DBT_MR, by_path=True))
    assert lookup.unavailable is None


# ─────────────────── 端到端:決策真的變成交人工 ───────────────────

def test_取不到規格時不得自動放行(tmp_path, monkeypatch):
    """`passed: False` 只是結果欄位;決策那一端要真的擋下來才算數。"""
    from orchestrator.pipeline import apply_policy
    out = _run(gitlab._NO_READ_TOKEN)
    report = {"score": 100, "findings": out["findings"],
              "_spec_exec": {"passed": out["passed"]}}
    mr = {"files": [{"path": "sql/rules/r201.sql", "diff": "@@\n+SELECT 1"}]}
    policy = {"auto_approve": {"max_diff_lines": 30, "min_score": 95,
                               "allowed_severities": ["info"],
                               "forbid_pending_hints": True},
              "block": {"min_blockers": 1}}
    assert apply_policy(report, mr, policy)["decision"] == "needs_human"
