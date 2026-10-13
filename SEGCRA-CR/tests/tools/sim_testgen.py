"""重放模擬:用「照實測特徵行為的假模型」量測資生成的補缺機制會不會收斂。

**為什麼需要這個工具**:這條路的參數(一批補幾個、最多補幾輪)只有在大規則上才
看得出差別,而大規則在真端點上單次呼叫動輒 10 分鐘以上——掃一遍 batch 要好幾天。
假模型把同一個決策壓到毫秒,代價是它只驗「機制」,不驗「真模型會不會照做」。

假模型的參數改用**端點回報的 token 數**,不再用字元數換算——換算是錯的:
`estimate_tokens()` 假設 3 個字元 ≈ 1 token,而測資的 rows 全是日期與數字,
實測 completion_tokens=8192 只換到 3923 個字元(0.48 字元/token),容量被高估約 6 倍。
第一版模擬就是這樣把 _FILL_BATCH 調到 24,真端點上連續兩次截斷、白跑 31 分鐘。

RETAIL_M1_EVAL 在 gemma4:31b 上的實測(eval/_runs/dump_r1,2026-10-13):
  * 輸出上限 8192 completion_tokens
  * 一個案例約 1040 tokens(補缺 6 個成功那次:6 個案例、6221 tokens)
    案例本身是 5-7 列 × 9 欄,不是浪費:這條規則的條件是彙總值
    (「次數介於 2–3」),要湊出來就必須有多列
  * 一個條件約 304 tokens(骨架:23 個條件 + 2 段 DDL = 7003 tokens)
    → 骨架已用掉 86% 的上限,**27 個條件左右骨架自己就會被截斷**
  * 思考 tokens 會吃掉同一個上限,而且每次不一樣:同樣要 6 個,
    前兩次 stop、第三次 length。所以沒有「安全批量」這種東西,
    只有「被截斷就縮」——用 --overhead 模擬這個變動部分

用法:
    python tests/tools/sim_testgen.py                 # RETAIL_M1 規模(23 個條件)
    python tests/tools/sim_testgen.py --conds 7       # R-140 規模
    python tests/tools/sim_testgen.py --sweep         # 掃 batch,列出呼叫數與覆蓋
    python tests/tools/sim_testgen.py --comply 0.5    # 模型只照做一半
"""
import argparse
import asyncio
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from orchestrator import spec_exec as se   # noqa: E402

# 實測是在 8192 的上限下量的,所以這個值不動——tests/test_sim_calibration.py
# 靠它對照實測。要掃別的上限用 --budget(設定檔現在給測資生成的是 12288,
# 因為實測證明 8192 是我們自己寫的限制:模型一次寫得出 11533 tokens)。
MEASURED_BUDGET = 8192
BUDGET_TOKENS = MEASURED_BUDGET
# 下面兩個是**扣掉思考成本之後**的單位成本:實測的 6221 / 7003 都已經含思考,
# 直接拿去除會把思考算兩次(第一版這樣寫,模擬結果變成連骨架都裝不下,與實測不符)。
#   每案例 = (6221 - 1481) / 6  ≈ 790    (補缺 6 個成功那次,思考最省的一次)
#   每條件 = (7003 - 1481) / 23 ≈ 240    (骨架 23 個條件 + 2 段 DDL)
TOKENS_PER_CASE = 790
TOKENS_PER_COND = 240
# 思考燒掉的部分。端點沒有分開回報,這是從「總 tokens - 案例數 × 790」倒推的。
# 10 次成功的呼叫:1481、2675、2711、3236、3316、3514、3851、4467、4900、5027
# → 範圍 1481–5027、中位數 3415,**與要求的數量沒有明顯關係**(要 3 個那次最高)。
THINKING_MIN = 1481      # 最省的那次;上面的 790/240 就是用它推導出來的
THINKING_MEDIAN = 3415
THINKING_WORST = 5027    # **訂批量要用這個**,不是中位數
# 預設取最省值,模擬才會重現實測的那幾次(tests/test_sim_calibration.py 靠這個)。
# 要訂批量請明確傳 --overhead 5027:思考與案例內容搶同一個輸出上限,按中位數訂
# 會固定有一部分呼叫被截斷浪費掉(13-20 分鐘/次)。
THINKING_TOKENS = THINKING_MIN


_overhead = THINKING_TOKENS


def _conds(n):
    return [{"id": f"C{i}", "desc": f"條件 {i}", "check_sql": "SELECT 1"}
            for i in range(1, n + 1)]


DDL = ["CREATE TABLE t (a INT, b INT)"]


def _case(cid, direction):
    """案例的**內容**只要形狀對就好:裝不裝得下是用 token 數算的(見 _tokens),
    不是看這個 dict 有多長。"""
    return {"case_id": f"{cid}-{direction[0].upper()}", "condition_id": cid,
            "direction": direction, "note": "", "rows": [["t", 1, 2]],
            "expect_flagged": direction == "true"}


def _tokens(n_cases: int = 0, n_conds: int = 0) -> int:
    """這一份回應大概要燒掉幾個 completion_tokens(含思考)。"""
    return n_cases * TOKENS_PER_CASE + n_conds * TOKENS_PER_COND + _overhead


class Fake:
    """依 prompt 判斷這次被要求的是整份、骨架、還是補缺,各自照實測特徵回應。"""

    def __init__(self, conds, comply=1.0):
        self.conds = conds
        self.comply = comply
        self.calls = 0
        self.modes = []

    async def __call__(self, cfg, profile, system, user, hub=None, verbose=True,
                       use_tools=True, trace=None, meta=None):
        self.calls += 1
        if "骨架" in user:
            self.modes.append("骨架")
            return self._out({"schema_ddl": DDL, "conditions": self.conds,
                              "cases": []}, meta,
                             cost=_tokens(n_conds=len(self.conds)))
        if "只要補缺的案例" in user:
            want = re.findall(r"條件 (C\d+)\(.*?\) 的 (true|false) 向", user)
            if want and self.comply < 1:
                want = want[:max(1, int(len(want) * self.comply))]
            self.modes.append(f"補缺×{len(want)}")
            # 注意:燒掉的 token 按**被要求的數量**算,不是按回了幾個。
            # 實測如此:要 24 個時只吐出 5 個完整案例就用光 8192 tokens,
            # 要 12 個時吐出 7 個——要得越多,思考越久、單位 token 的產出越少。
            return self._out({"cases": [_case(cid, d) for cid, d in want]}, meta,
                             cost=_tokens(n_cases=len(want)))
        # 整份計畫:能塞幾個算幾個,超過上限就截斷
        self.modes.append("整份")
        every = [_case(c["id"], d) for c in self.conds for d in ("true", "false")]
        room = BUDGET_TOKENS - _tokens(n_conds=len(self.conds))
        fit = max(0, room // TOKENS_PER_CASE)
        body = {"schema_ddl": DDL, "conditions": self.conds, "cases": every[:fit]}
        return self._out(body, meta, truncated=fit < len(every))

    def _out(self, body, meta, truncated=None, cost=None):
        text = json.dumps(body, ensure_ascii=False)
        if meta is not None:
            over = (cost or 0) > BUDGET_TOKENS if truncated is None else truncated
            meta["finish_reason"] = "length" if over else "stop"
            meta["completion_tokens"] = min(cost or 0, BUDGET_TOKENS)
        return text


class _Cfg:
    def role_profile(self, _):
        return type("P", (), {"model": "fake", "reasoning_effort": ""})()

    def profile(self, name):
        return self.role_profile(name)


def run(n_conds, batch=None, rounds=None, comply=1.0):
    fake = Fake(_conds(n_conds), comply)
    old = (se.run_agent, se._FILL_BATCH, se._FILL_ROUNDS)
    se.run_agent = fake
    if batch:
        se._FILL_BATCH = batch
    if rounds:
        se._FILL_ROUNDS = rounds
    try:
        plan, gaps, dropped = asyncio.run(
            se.generate_cases(_Cfg(), "SIM", "規格內容"))
    finally:
        se.run_agent, se._FILL_BATCH, se._FILL_ROUNDS = old
    got = len({(c["condition_id"], c["direction"]) for c in plan["cases"]})
    return fake, got, n_conds * 2, gaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conds", type=int, default=23,
                    help="條件數(23 = RETAIL_M1_EVAL 的規模,7 = R-140)")
    ap.add_argument("--batch", type=int, help="覆寫 _FILL_BATCH")
    ap.add_argument("--rounds", type=int, help="覆寫 _FILL_ROUNDS")
    ap.add_argument("--comply", type=float, default=1.0,
                    help="模型照做的比例(1 = 完全照做,0.5 = 只回一半)")
    ap.add_argument("--budget", type=int, default=MEASURED_BUDGET,
                    help="輸出上限(max_output_tokens)。預設 8192 = 實測時的值;"
                         "設定檔現在給測資生成的是 12288")
    ap.add_argument("--overhead", type=int, default=THINKING_TOKENS,
                    help="思考燒掉的 tokens(預設 1500)。這個值每次呼叫都不同——"
                         "實測同樣要 6 個案例,前兩次成功、第三次被截斷。"
                         "調高它就會看到「沒有安全批量、只能靠被截斷就縮」")
    ap.add_argument("--sweep", action="store_true", help="掃一遍 batch")
    a = ap.parse_args()
    global _overhead, BUDGET_TOKENS
    _overhead = a.overhead
    BUDGET_TOKENS = a.budget

    if a.sweep:
        print(f"條件 {a.conds} 個 → 需要 {a.conds * 2} 個「條件×方向」"
              f"(照做比例 {a.comply})")
        print(f"{'batch':>6} {'呼叫':>5} {'覆蓋':>8}  過程")
        for b in (4, 8, 12, 16, 20, 24, 32, a.conds * 2):
            fake, got, need, _ = run(a.conds, b, a.rounds, a.comply)
            print(f"{b:>6} {fake.calls:>5} {f'{got}/{need}':>8}  "
                  f"{' → '.join(fake.modes)}")
        return
    fake, got, need, gaps = run(a.conds, a.batch, a.rounds, a.comply)
    print(f"條件 {a.conds} 個 → 需要 {need} 個「條件×方向」")
    print(f"模型呼叫 {fake.calls} 次:{' → '.join(fake.modes)}")
    print(f"拿到 {got}/{need}  缺口 {len(gaps)} 條")


if __name__ == "__main__":
    main()
