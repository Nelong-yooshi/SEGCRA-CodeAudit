"""引用白名單 validate_citations 與 eval 訊號 read_signal 的測試 — 不呼叫 LLM。

為什麼值得單獨測:引用白名單擋的是「模型捏造法規條號」這種幻覺——它講得越具體、
越像真的,人越容易信。這條防線完全是確定性比對,但 golden set 只有 mr_501 一個 case
(eval/README「覆蓋缺口」列的一項),而且一個 case 只能走到一條分支:模型那次剛好
捏造了什麼,就只驗到什麼。合法引用會不會被誤刪、多筆引用會不會只擋到一筆,都沒驗過。

誤刪比漏擋更難發現:報告裡少了一條合法依據,不會有任何錯誤訊息。

驗的東西:
  1. 白名單的三條放行規則:核定規格、團隊慣例、知識庫項目 id。
  2. 白名單外一律剔除,而且剔除記錄要留在 _removed_citations(可稽核)。
  3. 同一條 finding 裡合法與非法引用混在一起時,只剔除非法的那些。
  4. 沒附規格時(spec_code=None),連「規格」字樣也不放行——不能靠模型自稱。
  5. read_signal:eval 斷言能讀到的訊號,含新增的 arbiter_dropped /
     spec_exec_sql_fault(用來分辨是哪一道防線接住的)。
"""
import sys
from pathlib import Path

import pytest

from orchestrator.pipeline import validate_citations

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
from run_eval import read_signal  # noqa: E402


def _report(*citations):
    return {"findings": [{"severity": "major", "title": "t",
                          "citations": [dict(c) for c in citations]}]}


def _kept(report):
    return [c["source"] for c in report["findings"][0]["citations"]]


# ─────────────────── 白名單放行的三條規則 ───────────────────

@pytest.mark.parametrize("source", [
    "specs/R-201.md",          # 規格檔路徑含規則碼
    "R-201",                   # 只寫規則碼
    "核定規格 R-201",           # 中文前綴 + 規則碼
    "《specs/R-201.md》",       # 帶書名號(_norm 會去掉)
])
def test_附上的核定規格可以引用(source):
    r = validate_citations(_report({"source": source, "article": "需求項目 9"}), "R-201")
    assert _kept(r) == [source]
    assert "_removed_citations" not in r


@pytest.mark.parametrize("source", ["團隊慣例", "team convention", "慣例:命名規則"])
def test_團隊慣例可以引用(source):
    r = validate_citations(_report({"source": source, "article": ""}), "R-201")
    assert _kept(r) == [source]


def test_知識庫項目_id_可以引用():
    """引用知識庫裡真實存在的慣例項目 id(如 bind-no-sysdate)。"""
    r = validate_citations(_report({"source": "bind-no-sysdate", "article": ""}), "R-201")
    assert _kept(r) == ["bind-no-sysdate"]


def test_泛稱規格在有附規格時放行():
    r = validate_citations(_report({"source": "核定規格", "article": "第 3 條"}), "R-201")
    assert _kept(r) == ["核定規格"]


# ─────────────────── 白名單外一律剔除 ───────────────────

@pytest.mark.parametrize("source", [
    "個人資料保護法",           # 經典幻覺:聽起來權威、實際沒附上
    "個資法 第 6 條",
    "金管會函釋",
    "銀行法第 45 條",
    "內部稽核手冊 v3",
    "https://example.com/rule",
])
def test_白名單外的引用要剔除(source):
    r = validate_citations(_report({"source": source, "article": "x"}), "R-201")
    assert _kept(r) == []
    assert r["_removed_citations"], "剔除了卻沒留記錄,無法稽核"
    assert source in r["_removed_citations"][0]


def test_現況_含spec或規格字樣就放行():
    """記錄現況(這是 validate_citations docstring 寫明的設計):只要這次有附上任何
    規格,來源含「規格」或「spec」字樣就放行,不比對是不是**那一份**規格。

    這條規則存在的理由是容忍口語寫法(「核定規格」「依規格第 3 條」),
    代價見下一個測試。
    """
    r = validate_citations(_report({"source": "核定規格", "article": "第 3 條"}), "R-201")
    assert _kept(r) == ["核定規格"]


@pytest.mark.parametrize("fabricated", [
    "specs/R-999.md",      # 沒附上的規格檔
    "R-999",               # 只寫規則碼
    "依核定規格 R-888 第 3 條",   # 泛稱 + 指名了別的規則碼
])
def test_指名了沒附上的規則碼要剔除(fabricated):
    """迴歸測試:曾經只要來源含「規格/spec」字樣就放行,不比對是不是這次附上的那份。

    後果是模型捏造一份沒附上、甚至不存在的 `specs/R-999.md`,只要本次有附任何規格
    就會被放行——而「依據某份沒給過的規格」正是白名單要擋的典型捏造形式。
    收緊方式:來源**指名規則碼**時必須與本次附上的相符;不指名的泛稱維持放行
    (見下一個測試)。
    """
    r = validate_citations(_report({"source": fabricated, "article": ""}), "R-201")
    assert _kept(r) == []


@pytest.mark.parametrize("vague", ["核定規格", "依 spec 第 3 條", "本規則的規格文件"])
def test_不指名規則碼的泛稱維持放行(vague):
    """收緊時刻意保留的空間:模型用口語講「依核定規格」是合理的寫法,

    一併剔除會誤刪合法依據——而誤刪不會有任何錯誤訊息,比漏擋更難發現。
    """
    r = validate_citations(_report({"source": vague, "article": ""}), "R-201")
    assert _kept(r) == [vague]


def test_沒附規格時連規格字樣都不放行():
    """spec_code=None 代表這次沒有附任何核定規格,那就不存在「依規格」這件事。"""
    r = validate_citations(_report({"source": "核定規格", "article": "第 3 條"}), None)
    assert _kept(r) == []


def test_合法與非法混在一起時只剔除非法的():
    """一條 finding 引用多個來源時,不能因為有一個是假的就把整條清空
    ——那會連帶弄丟真正的依據。"""
    r = validate_citations(
        _report({"source": "specs/R-201.md", "article": "需求項目 9"},
                {"source": "個人資料保護法", "article": "第 6 條"},
                {"source": "團隊慣例", "article": ""}),
        "R-201")
    assert _kept(r) == ["specs/R-201.md", "團隊慣例"]
    assert len(r["_removed_citations"]) == 1


def test_沒有引用時不留剔除記錄():
    r = validate_citations({"findings": [{"severity": "info", "title": "t"}]}, "R-201")
    assert "_removed_citations" not in r


# ─────────────────── eval 訊號 ───────────────────

def test_訊號_injection_hit():
    assert read_signal({"_injection_scan": [{"category": "x"}]}, "injection_hit") is True
    assert read_signal({}, "injection_hit") is False


def test_訊號_citations_removed():
    assert read_signal({"_removed_citations": ["個資法"]}, "citations_removed") is True
    assert read_signal({}, "citations_removed") is False


def test_訊號_arbiter_dropped():
    """#8 點名缺的「能斷言仲裁結果的訊號」。

    分得出「仲裁判測資錯」與「仲裁判 SQL 錯」,才知道該修測資生成還是修 SQL。
    """
    dropped_by_arbiter = {"_spec_exec": {"dropped_cases": [
        {"case_id": "C1-T", "by": "arbiter", "reason": "測資日期寫錯"}]}}
    assert read_signal(dropped_by_arbiter, "arbiter_dropped") is True

    # 形狀檢查剔除的不算仲裁——那是測資根本不合法,連跑都沒跑到
    dropped_by_shape = {"_spec_exec": {"dropped_cases": [
        {"case_id": "X", "by": "shape-check", "reason": "形狀不合法"}]}}
    assert read_signal(dropped_by_shape, "arbiter_dropped") is False
    assert read_signal({}, "arbiter_dropped") is False


def test_訊號_spec_exec_sql_fault_分得出是哪道防線接住的():
    """執行驗證自己抓到 vs 靜態審查抓到,要分得出來。

    分不出來的話,執行驗證默默失效、靜態審查剛好補上時,測試還是綠的
    ——防線少了一道卻沒有人知道(eval/README 的 mr_403 / mr_407 就是這種情況)。
    """
    by_spec_exec = {"findings": [
        {"severity": "major", "title": "執行驗證失敗:實作與規格行為不符"}]}
    assert read_signal(by_spec_exec, "spec_exec_sql_fault") is True

    by_static_review = {"findings": [
        {"severity": "major", "title": "實作與核定規格不符(時間窗邊界錯誤)"}]}
    assert read_signal(by_static_review, "spec_exec_sql_fault") is False


def test_未知訊號名稱要報錯而不是回傳_None():
    """回傳 None 的話,斷言 `got == want` 會靜默失敗,讓人以為是管線有問題。

    寧可直接炸,讓寫 case 的人立刻知道訊號名打錯了。
    """
    with pytest.raises(KeyError) as e:
        read_signal({}, "typo_signal")
    assert "arbiter_dropped" in str(e.value), "錯誤訊息要列出可用訊號,方便改正"
