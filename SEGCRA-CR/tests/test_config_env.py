"""`LLM_TEMPERATURE` / `LLM_SEED` 環境變數覆蓋的驗證 — 不呼叫 LLM。

為什麼值得測:這兩個變數是**手打在跑批指令前面**的
(`LLM_TEMPERATURE=0 LLM_SEED=42 python eval/run_eval.py ...`),打錯是常態,
而兩種打錯的後果都很安靜:

  * 打成非數字 → Python 內建訊息是 `could not convert string to float: 'O'`,
    不會說是哪個環境變數、也不會說該填什麼
  * 打成超出範圍的值(temperature=10)→ **根本不報錯**,端點可能自己夾住也可能
    照收,於是整輪數字是在一個沒人知道的設定下量出來的

第二種比第一種糟:它不會停下來。所以這裡兩種都擋,而且錯誤訊息要講得出
「哪個變數、你給了什麼、該給什麼」。
"""
import os

import pytest

from orchestrator.config import Config

_PROFILES = {"review": {"model": "m", "num_ctx": 32768,
                        "temperature": 0.1, "max_output_tokens": 8192}}


def _cfg() -> Config:
    return Config(endpoint="http://x/v1", api_key="k", default_profile="review",
                  profiles=_PROFILES, roles={}, budget={"diff": 8000}, policy={})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每條測試都從「兩個變數都沒設定」開始——沒設定與設成空字串是不同的情況。"""
    monkeypatch.delenv("LLM_TEMPERATURE", raising=False)
    monkeypatch.delenv("LLM_SEED", raising=False)


# ─────────────────── 沒設定時維持 profile 的值 ───────────────────

def test_沒設定環境變數時用_profile_的值():
    p = _cfg().profile("review")
    assert p.temperature == 0.1
    assert getattr(p, "seed", None) is None


# ─────────────────── 正常值 ───────────────────

def test_正常值會覆蓋掉_profile(monkeypatch):
    monkeypatch.setenv("LLM_TEMPERATURE", "0")
    monkeypatch.setenv("LLM_SEED", "42")
    p = _cfg().profile("review")
    assert p.temperature == 0.0
    assert p.seed == 42


def test_空字串的_seed_代表取消固定_恢復隨機(monkeypatch):
    """空字串是**有意義的值**,不是「沒設定」。

    出能力報告時要看模型真實的分布,就用 `LLM_SEED=` 把固定 seed 關掉。
    如果這裡把空字串當成無效值擋下來,那條路就斷了。
    """
    monkeypatch.setenv("LLM_SEED", "")
    assert _cfg().profile("review").seed is None
    monkeypatch.setenv("LLM_SEED", "   ")      # 只有空白也算
    assert _cfg().profile("review").seed is None


# ─────────────────── 壞值要擋,而且訊息要有用 ───────────────────

@pytest.mark.parametrize("bad", ["O", "零", "0.1.2", "", " "])
def test_非數字的_temperature_要擋下來(monkeypatch, bad):
    """注意 temperature 的空字串也是錯的——它沒有「取消」的語意(seed 才有)。"""
    monkeypatch.setenv("LLM_TEMPERATURE", bad)
    with pytest.raises(ValueError) as e:
        _cfg().profile("review")
    msg = str(e.value)
    assert "LLM_TEMPERATURE" in msg, "訊息要講是哪個環境變數"
    assert repr(bad) in msg or "不是數字" in msg, "訊息要講你給了什麼"


@pytest.mark.parametrize("bad", ["-0.1", "2.1", "10", "100"])
def test_超出範圍的_temperature_要擋下來(monkeypatch, bad):
    """這一條比「非數字」那條重要:超出範圍**不會自己報錯**。

    端點可能默默夾住、也可能照收,兩種都讓這輪的數字量在沒人知道的設定下,
    而且要到事後對不上才會發現——那時候已經燒掉十幾個小時了。
    """
    monkeypatch.setenv("LLM_TEMPERATURE", bad)
    with pytest.raises(ValueError, match="超出合理範圍"):
        _cfg().profile("review")


@pytest.mark.parametrize("bad", ["4 2", "四十二", "42.0", "0x2a"])
def test_非整數的_seed_要擋下來(monkeypatch, bad):
    monkeypatch.setenv("LLM_SEED", bad)
    with pytest.raises(ValueError) as e:
        _cfg().profile("review")
    assert "LLM_SEED" in str(e.value)


def test_負數的_seed_要擋下來(monkeypatch):
    monkeypatch.setenv("LLM_SEED", "-1")
    with pytest.raises(ValueError, match="不可為負"):
        _cfg().profile("review")


# ─────────────────── role_profile 走同一條路 ───────────────────

def test_角色_profile_也吃得到覆蓋與驗證(monkeypatch):
    """testgen / arbiter 是另一個進入點,不能只有 profile() 有防線。

    執行驗證的兩個角色都走 role_profile(),漏掉的話壞值會從那裡溜進去。
    """
    cfg = Config(endpoint="http://x/v1", api_key="k", default_profile="review",
                 profiles=_PROFILES, roles={"testgen": "review"},
                 budget={"diff": 8000}, policy={})
    monkeypatch.setenv("LLM_SEED", "7")
    assert cfg.role_profile("testgen").seed == 7
    monkeypatch.setenv("LLM_SEED", "nope")
    with pytest.raises(ValueError):
        cfg.role_profile("testgen")


# ─────────────────── reasoning_effort ───────────────────

def test_沒設定時不送這個參數():
    """預設不送,不是預設關閉思考。

    送了不支援的參數,端點可能把整個請求退回——那比「多花一點 token」嚴重得多。
    所以 None 的語意是「不送」,讓端點用它自己的預設。
    """
    assert _cfg().profile("review").reasoning_effort is None


@pytest.mark.parametrize("raw,expect", [
    ("none", "none"), ("NONE", "none"), ("  high  ", "high"),
    ("minimal", "minimal"), ("low", "low"), ("medium", "medium"),
])
def test_合法檔位會被正規化(monkeypatch, raw, expect):
    monkeypatch.setenv("LLM_REASONING_EFFORT", raw)
    assert _cfg().profile("review").reasoning_effort == expect


@pytest.mark.parametrize("raw", ["", "   "])
def test_空字串代表取消覆蓋_不是送_none(monkeypatch, raw):
    """空字串 → 不送這個參數(端點預設,會思考);送 "none" → 明確要求不思考。

    兩者不同。混淆的話,想「恢復預設」的人會以為自己關掉了思考,
    量出來的比較就白做了。
    """
    monkeypatch.setenv("LLM_REASONING_EFFORT", raw)
    assert _cfg().profile("review").reasoning_effort is None


@pytest.mark.parametrize("bad", ["bogus", "0", "off", "false", "無", "none;high"])
def test_不認得的檔位要當場擋下(monkeypatch, bad):
    """端點收到不認得的值可能整個請求退回(還好,看得到錯誤),也可能**默默忽略**。

    後者才是要防的:你以為關掉了思考、實際上沒有,於是「關掉思考有沒有用」
    這個比較整輪白做,而且不會有任何徵兆。
    """
    monkeypatch.setenv("LLM_REASONING_EFFORT", bad)
    with pytest.raises(ValueError) as e:
        _cfg().profile("review")
    assert "LLM_REASONING_EFFORT" in str(e.value)
    assert "可用" in str(e.value), "訊息要講得出有哪些檔位可以用"


def test_角色_profile_也吃得到(monkeypatch):
    """測資生成走的是 role_profile(\"testgen\"),不是 profile()——漏掉的話這個功能
    對它完全沒有作用,而它正是最需要省輸出預算的那一個。"""
    cfg = Config(endpoint="http://x/v1", api_key="k", default_profile="review",
                 profiles=_PROFILES, roles={"testgen": "review"},
                 budget={"diff": 8000}, policy={})
    monkeypatch.setenv("LLM_REASONING_EFFORT", "none")
    assert cfg.role_profile("testgen").reasoning_effort == "none"
