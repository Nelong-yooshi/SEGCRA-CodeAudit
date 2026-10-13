"""釘住重放模擬的參數對得上真端點的實測——不連端點,純算術。

模擬的用途是「在毫秒內掃出補缺機制的參數」,代價是它的結論只在參數貼近真實時
才成立。第一版用字元數換算 token(`estimate_tokens()` 的 3 字元 ≈ 1 token),
高估容量約 6 倍,於是把 `_FILL_BATCH` 調到 24——真端點上連續兩次截斷、
白跑 31 分鐘。所以參數必須釘住,不能靠人記得去對。

實測來源:RETAIL_M1_EVAL / gemma4:31b / 2026-10-13(eval/_runs/dump_r1)。
換端點或換模型時這些數字會變,屆時要重新量,而不是把這裡的斷言放寬。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "tools"))

import sim_testgen as sim  # noqa: E402

# (名稱, 案例數, 條件數, 實測 completion_tokens, 實測是否被截斷)
MEASURED = [
    ("骨架 23 個條件",     0, 23, 7003, False),
    ("補缺 6 個(成功)",    6,  0, 6221, False),
    ("補缺 12 個(截斷)",  12,  0, 8192, True),
    ("補缺 24 個(截斷)",  24,  0, 8192, True),
]


def _cost(cases, conds, overhead=sim.THINKING_MIN):
    return cases * sim.TOKENS_PER_CASE + conds * sim.TOKENS_PER_COND + overhead


@pytest.mark.parametrize("name,cases,conds,real_tokens,truncated", MEASURED)
def test_模擬的截斷判定與實測一致(name, cases, conds, real_tokens, truncated):
    assert (_cost(cases, conds) > sim.BUDGET_TOKENS) is truncated, name


@pytest.mark.parametrize("name,cases,conds,real_tokens,truncated",
                         [m for m in MEASURED if not m[4]])
def test_沒被截斷的那幾次_估算要接近實測(name, cases, conds, real_tokens, truncated):
    """只有沒被截斷的才比得了:被截斷的 completion_tokens 一律等於上限,
    看不出「如果讓它寫完會用掉多少」。"""
    est = _cost(cases, conds)
    assert abs(est - real_tokens) / real_tokens < 0.05, f"{name}:估 {est}、實測 {real_tokens}"


def test_單位成本不含思考成本():
    """實測的 6221 / 7003 都已經含思考,直接拿去除會把思考算兩次——
    第一版就是這樣,模擬變成連骨架都裝不下,與實測不符。

    用 THINKING_MIN 推導:這兩次恰好是思考最省的,所以 790/240 是「內容本身」
    最接近的估計(思考較多的那幾次無法分離兩者)。"""
    assert sim.TOKENS_PER_CASE == pytest.approx((6221 - sim.THINKING_MIN) / 6, rel=0.02)
    assert sim.TOKENS_PER_COND == pytest.approx((7003 - sim.THINKING_MIN) / 23, rel=0.02)


def test_訂批量要用最壞的思考量():
    """按中位數訂會固定有一部分呼叫被截斷浪費掉。實測兩次預測失準都是這樣來的:
    批量 10 / 上限 12288 → 10×790 + 5027 = 12927 > 12288(實測截斷)
    批量  6 / 上限  8192 →  6×790 + 5027 =  9767 >  8192(實測 3 次中 1 次截斷)
    """
    import orchestrator.spec_exec as se
    budget = 12288            # config/models.yaml 的 testgen profile
    safe = (budget - sim.THINKING_WORST) // sim.TOKENS_PER_CASE
    assert se._FILL_BATCH <= safe, (
        f"批量 {se._FILL_BATCH} 在最壞的思考量({sim.THINKING_WORST})下會截斷"
        f"(安全值 {safe})")
    assert sim.THINKING_WORST > sim.THINKING_MEDIAN > sim.THINKING_MIN


def test_骨架的餘裕要算得出懸崖在哪():
    """骨架自己被截斷時程式沒有出路(再要一次骨架 → 再截斷 → 0 個案例),
    所以「幾個條件會踩到」是必須看得見的數字。"""
    fits = (sim.BUDGET_TOKENS - sim.THINKING_MIN) // sim.TOKENS_PER_COND
    assert 25 <= fits <= 29, f"骨架容量算出來是 {fits} 個條件,與實測的 23/86% 不符"
