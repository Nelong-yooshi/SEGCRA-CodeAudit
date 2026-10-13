"""重放模擬:用「照實測特徵行為的假模型」量測資生成的補缺機制會不會收斂。

**為什麼需要這個工具**:這條路的參數(一批補幾個、最多補幾輪)只有在大規則上才
看得出差別,而大規則在真端點上單次呼叫動輒 10 分鐘以上——掃一遍 batch 要好幾天。
假模型把同一個決策壓到毫秒,代價是它只驗「機制」,不驗「真模型會不會照做」。

假模型的參數取自實測,但**各自的可追溯程度不同**,照實標註:
  * 輸出上限 8192 tokens → `config/models.yaml` 的 max_output_tokens,
    專案把字元換算成 token 用的是 len/3 → 約 24,576 字元。可查。
  * 每個案例約 873 字元 → RETAIL_M1_EVAL 在公司端點上的那次實跑(14 個案例、
    JSON 本體 12,219 字)。**那次的輸出沒有存成檔案**,這個數字只在當時的
    終端機輸出裡,repo 裡沒有可核對的憑據——要再驗一次得重連端點重跑。
  * 對照組:`eval/testdata_cache/R-140.json`(真模型 gemma4:31b 的凍結輸出,
    repo 裡就有)每個案例中位數只有 196 字元。**案例大小是規則相依的**:
    R-140 一列 8 欄、每案例一列;RETAIL_M1 的資料列寬得多。
  * 條件約 120 字元 → 同上那次實跑的粗估,精確度不高(條件只影響骨架的大小,
    對結論幾乎沒有影響)。

**這個差異會改變結論的適用範圍**(--case-chars 可以自己驗):
  * 873 字元/案例:整份裝不下 → 要骨架 + 逐批補,batch 的選擇才有意義
  * 196 字元/案例:46 個案例一次就裝得下 → 1 次呼叫收工,整套補缺機制不會啟動
  也就是說,補缺機制是**寬資料列的大規則**才會用到的安全網,不是每個規則都走的路。

**限制(不要拿它的數字當品質證據)**:
  * 它假設模型完全照「只回缺的那幾個」做。真模型可能改條件編號、可能自己合併條件
    ——用 --comply 可以模擬「只照做一部分」,但真實的不聽話形式比這更多。
  * 它量的是「要幾次呼叫、補不補得滿」,不是「生出來的測資對不對」。

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

BUDGET_CHARS = 8192 * 3
CHARS_PER_CASE = 873     # 可用 --case-chars 覆寫,看結論對這個假設有多敏感
CHARS_PER_COND = 120


_case_chars = CHARS_PER_CASE


def _conds(n):
    return [{"id": f"C{i}", "desc": f"條件 {i}", "check_sql": "SELECT 1"}
            for i in range(1, n + 1)]


DDL = ["CREATE TABLE t (a INT, b INT)"]


def _case(cid, direction):
    """把案例填到實測大小(873 字元),否則補缺的回應永遠不會被截斷,
    就測不到「補缺被截斷 → 批量減半」那條路。"""
    c = {"case_id": f"{cid}-{direction[0].upper()}", "condition_id": cid,
         "direction": direction, "note": "", "rows": [["t", 1, 2]],
         "expect_flagged": direction == "true"}
    pad = _case_chars - len(json.dumps(c, ensure_ascii=False)) - 2
    if pad > 0:
        c["note"] = "x" * pad
    return c


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
                              "cases": []}, meta)
        if "只要補缺的案例" in user:
            want = re.findall(r"條件 (C\d+)\(.*?\) 的 (true|false) 向", user)
            if want and self.comply < 1:
                want = want[:max(1, int(len(want) * self.comply))]
            self.modes.append(f"補缺×{len(want)}")
            return self._out({"cases": [_case(cid, d) for cid, d in want]}, meta)
        # 整份計畫:能塞幾個算幾個,超過上限就截斷
        self.modes.append("整份")
        every = [_case(c["id"], d) for c in self.conds for d in ("true", "false")]
        room = BUDGET_CHARS - len(self.conds) * CHARS_PER_COND - 200
        fit = max(0, room // _case_chars)
        body = {"schema_ddl": DDL, "conditions": self.conds, "cases": every[:fit]}
        return self._out(body, meta, truncated=fit < len(every))

    def _out(self, body, meta, truncated=None):
        text = json.dumps(body, ensure_ascii=False)
        if meta is not None:
            over = len(text) > BUDGET_CHARS if truncated is None else truncated
            meta["finish_reason"] = "length" if over else "stop"
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
    ap.add_argument("--case-chars", type=int, default=CHARS_PER_CASE,
                    help="每個案例的字元數。873 = RETAIL_M1 實測(寬資料列),"
                         "196 = R-140 凍結測資的中位數。案例大小是**規則相依的**,"
                         "用它看結論對這個假設有多敏感")
    ap.add_argument("--sweep", action="store_true", help="掃一遍 batch")
    a = ap.parse_args()
    global _case_chars
    _case_chars = a.case_chars

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
