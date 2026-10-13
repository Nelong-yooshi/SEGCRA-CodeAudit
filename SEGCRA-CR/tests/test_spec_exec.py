"""執行驗證 spec_exec 的確定性部分 — 測資形狀檢查、規則碼抽取、仲裁後的分支,
不呼叫 LLM、不連資料庫(三個角色裡會呼叫模型的那兩個都換成假的)。

為什麼值得單獨測:spec_exec 的三個角色裡,角色一(測資生成)與角色三(仲裁)是模型,
**但夾在中間與後面的判定邏輯全是確定性的**:形狀檢查、覆蓋缺口計算、仲裁判定後要
剔除案例還是開 major finding。這些邏輯用 golden case 驗不到,因為觸發哪個分支取決於
模型當下生出什麼——eval/README「覆蓋缺口」列的「spec_exec 仲裁(測資錯 vs SQL 錯)」
就是這個原因一直沒有對應 case。

把模型換成固定回傳值之後,兩個分支都能穩定走到。

驗的東西:
  1. _shape_check:逐條件 true/false 兩向覆蓋檢查、非法案例剔除、
     **表名白名單(擋 SQL 注入)**、缺 schema_ddl。
  2. extract_rule_codes:從標題/描述/檔名/內容抓規則碼。
  3. 無規格可驗 → major + 不得自動放行(「必跑」的意思是沒 spec 就不能放行,不是跳過)。
  4. 仲裁判「測資錯」→ 剔除該案例、補覆蓋缺口、標記 dropped;
     **剔除後那個方向就沒驗到,所以不能算通過**——這是最容易被誤解成「通過」的分支。
  5. 仲裁判「SQL 錯」→ 開 major finding、passed=False。
  6. 仲裁 LLM 掛掉時保守倒向「SQL 錯」(寧可誤報待人工,不可誤放)。
"""
import asyncio
import json
import pathlib
import re
import sys

import pytest

from orchestrator import spec_exec
from orchestrator.spec_exec import _shape_check, extract_rule_codes, generate_cases, run_spec_exec


def _case(cid, cond, direction, flagged=True, table="transactions"):
    return {"case_id": cid, "condition_id": cond, "direction": direction,
            "rows": [[table, 1, "2026-06-01 10:00:00"]],
            "expect_flagged": flagged, "note": f"{cid} 測試"}


def _plan(cases, conditions=("C1",), ddl=("CREATE TABLE transactions (id INT);",)):
    return {"schema_ddl": list(ddl),
            "conditions": [{"id": c, "desc": f"條件 {c}"} for c in conditions],
            "cases": list(cases)}


# ─────────────────── 形狀與覆蓋檢查 ───────────────────

def test_兩向覆蓋齊全時沒有缺口():
    plan, gaps, dropped = _shape_check(
        _plan([_case("C1-T", "C1", "true"), _case("C1-F", "C1", "false", False)]))
    assert gaps == []
    assert len(plan["cases"]) == 2
    assert dropped == []


def test_缺一個方向就記成覆蓋缺口():
    """只有 true 向 = 只驗了「該命中的有命中」,沒驗「不該命中的沒命中」。

    漏報與誤報是兩種不同的錯,少一個方向就有一半沒驗到。
    """
    _, gaps, _ = _shape_check(_plan([_case("C1-T", "C1", "true")]))
    assert any("C1" in g and "false" in g for g in gaps)


def test_多個條件各自檢查覆蓋():
    plan = _plan([_case("C1-T", "C1", "true"), _case("C1-F", "C1", "false", False),
                  _case("C2-T", "C2", "true")],
                 conditions=("C1", "C2"))
    _, gaps, _ = _shape_check(plan)
    assert len(gaps) == 1 and "C2" in gaps[0]      # C1 齊全,只缺 C2 的 false 向


def test_表名白名單擋掉非法識別字():
    """rows 的第一欄是要被組進 SQL 的表名,必須擋住注入。

    測資是模型生成的,模型可能幻覺出奇怪的字串;這一關是確定性防線,不能只靠模型乖。
    """
    for bad_table in ("transactions; DROP TABLE users", "tx-log", "1table", ""):
        plan, _, dropped = _shape_check(
            _plan([_case("C1-T", "C1", "true", table=bad_table)]))
        assert plan["cases"] == [], f"非法表名 {bad_table!r} 沒被剔除"
        assert dropped and "形狀不合法" in dropped[0]["reason"]


@pytest.mark.parametrize("broken", [
    {"case_id": "X", "condition_id": "C1", "direction": "sideways",      # 方向不合法
     "rows": [["t", 1]], "expect_flagged": True},
    {"case_id": "X", "condition_id": "C9", "direction": "true",          # 條件不存在
     "rows": [["t", 1]], "expect_flagged": True},
    {"case_id": "X", "condition_id": "C1", "direction": "true",          # 沒有資料列
     "rows": [], "expect_flagged": True},
    {"case_id": "X", "condition_id": "C1", "direction": "true",          # 預期值不是布林
     "rows": [["t", 1]], "expect_flagged": "yes"},
])
def test_形狀不合法的案例會被剔除(broken):
    plan, _, dropped = _shape_check(_plan([broken]))
    assert plan["cases"] == []
    assert len(dropped) == 1


def test_沒有合法_DDL_算缺口():
    _, gaps, _ = _shape_check(_plan([_case("C1-T", "C1", "true")], ddl=("DROP TABLE x;",)))
    assert "無合法 schema_ddl" in gaps


def test_整份計畫不是物件時不炸():
    plan, gaps, _ = _shape_check("這不是 JSON 物件")
    assert plan["cases"] == [] and gaps


# ─────────────────── 測資生成的重試(覆蓋不完整也要重試) ───────────────────
#
# 背景:舊行為只要解析出任何合法案例就直接收下,不管 coverage_gaps 是不是空的。
# 同一份規格、同一個 prompt,不同次呼叫可能拆出不同數量的條件與案例——單次取樣
# 抽到不完整覆蓋,舊版就把那次不完整的結果直接當成生成完成,不會再試。

class _FakeCfg:
    """只需要 role_profile 回傳一個有 model / temperature 等欄位的東西。"""
    def role_profile(self, role):
        from orchestrator.config import ModelProfile
        return ModelProfile(model="fake", num_ctx=1024, temperature=0,
                            max_output_tokens=256)


def _raw(plan):
    return json.dumps(plan)


def test_第一次覆蓋不完整_補齊後就採用補齊的那版(monkeypatch):
    incomplete = _plan([_case("C1-T", "C1", "true")])                        # 缺 false 向
    complete = _plan([_case("C1-T", "C1", "true"), _case("C1-F", "C1", "false", False)])
    calls = [_raw(incomplete), _raw(complete)]

    async def fake_run_agent(*a, **kw):
        return calls.pop(0)

    monkeypatch.setattr(spec_exec, "run_agent", fake_run_agent)
    plan, gaps, dropped = asyncio.run(generate_cases(_FakeCfg(), "R-999", "規格內容"))
    assert gaps == []
    assert len(plan["cases"]) == 2
    assert calls == [], "第一次不完整,必須觸發第二次呼叫"


def test_多次都沒抽到完整覆蓋時保留覆蓋最多的一版(monkeypatch):
    """三次都沒抽到 0 缺口——不能因為「試完了」就退化成空案例,也不能只認最後一次
    (最後一次剛好抽差也要收較好的那次),要保留看過的裡面覆蓋最多的。"""
    worse = _plan([_case("C1-T", "C1", "true")], conditions=("C1", "C2"))       # 3 個缺口
    better = _plan([_case("C1-T", "C1", "true"), _case("C1-F", "C1", "false", False),
                    _case("C2-T", "C2", "true")], conditions=("C1", "C2"))       # 1 個缺口
    calls = [_raw(worse), _raw(better), _raw(worse)]

    async def fake_run_agent(*a, **kw):
        return calls.pop(0)

    monkeypatch.setattr(spec_exec, "run_agent", fake_run_agent)
    plan, gaps, dropped = asyncio.run(
        generate_cases(_FakeCfg(), "R-999", "規格內容", max_attempts=3))
    assert len(gaps) == 1
    assert len(plan["cases"]) == 3, "留下的應該是缺口較少的 better,不是最後一次的 worse"
    assert calls == [], "三次都沒抽到 0 缺口,應該用完全部嘗試次數"


def test_全部嘗試都沒有合法案例才真的失敗(monkeypatch):
    async def fake_run_agent(*a, **kw):
        return "這次模型完全沒輸出 JSON"

    monkeypatch.setattr(spec_exec, "run_agent", fake_run_agent)
    plan, gaps, dropped = asyncio.run(
        generate_cases(_FakeCfg(), "R-999", "規格內容", max_attempts=2))
    assert plan["cases"] == []
    assert len(gaps) == 1 and "測資生成失敗" in gaps[0]
    assert "2" in gaps[0], "失敗原因要帶出試了幾次,方便回溯"


def test_LLM_呼叫失敗後仍能在下次成功時採用完整結果(monkeypatch):
    """第一次連線失敗、第二次才拿到完整覆蓋——不能因為中途掛過一次就整體判失敗。"""
    complete = _plan([_case("C1-T", "C1", "true"), _case("C1-F", "C1", "false", False)])
    calls = [ConnectionError("端點連不到"), _raw(complete)]

    async def fake_run_agent(*a, **kw):
        nxt = calls.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(spec_exec, "run_agent", fake_run_agent)
    plan, gaps, dropped = asyncio.run(generate_cases(_FakeCfg(), "R-999", "規格內容"))
    assert gaps == []
    assert len(plan["cases"]) == 2


def _all_dirs(conditions):
    """每個條件兩向都有案例(0 缺口)。"""
    return [_case(f"{c}-{d[0].upper()}", c, d, d == "true")
            for c in conditions for d in ("true", "false")]


def test_挑最佳版本看覆蓋數_不被拆得少的0缺口版本騙走(monkeypatch):
    """review 第 4 點:缺口是對照模型**自己拆出的**條件算的。拆得少、全覆蓋的
    那次是 0 缺口,但實際驗到的「條件 × 方向」比拆得齊、只缺一點的那次少。

      第 1 次:拆 4 個條件、覆蓋 7/8(1 缺口)
      第 2 次:只拆 1 個條件、覆蓋 2/2(0 缺口)← 不能因為 0 缺口就提前收下
      第 3 次:拆 4 個條件、覆蓋 4/8(4 缺口)
    應收第 1 次,而且第 2 次的 0 缺口不能讓它提前結束(第 3 次要被呼叫到)。"""
    four = ("C1", "C2", "C3", "C4")
    first = _plan(_all_dirs(four)[:-1], conditions=four)
    second = _plan(_all_dirs(("C1",)), conditions=("C1",))
    third = _plan(_all_dirs(four)[::2], conditions=four)
    calls = [_raw(first), _raw(second), _raw(third)]

    async def fake_run_agent(*a, **kw):
        return calls.pop(0)

    monkeypatch.setattr(spec_exec, "run_agent", fake_run_agent)
    plan, gaps, _ = asyncio.run(
        generate_cases(_FakeCfg(), "R-999", "規格內容", max_attempts=3))
    assert calls == [], "第 2 次拆得比第 1 次少,0 缺口也不能提前結束"
    assert len(plan["conditions"]) == 4 and len(plan["cases"]) == 7, \
        "應收覆蓋 7 個「條件 × 方向」的第 1 次,不是 0 缺口但只覆蓋 2 個的第 2 次"
    assert gaps == ["條件 C4 缺 false 向案例"]


def test_0缺口且條件數不少於之前的最大值才提前結束(monkeypatch):
    """第 1 次拆 2 個條件有缺口,第 2 次拆齊 2 個且 0 缺口 → 收第 2 次,不再試。"""
    two = ("C1", "C2")
    calls = [_raw(_plan(_all_dirs(two)[:3], conditions=two)),
             _raw(_plan(_all_dirs(two), conditions=two)),
             "不該被呼叫到"]

    async def fake_run_agent(*a, **kw):
        return calls.pop(0)

    monkeypatch.setattr(spec_exec, "run_agent", fake_run_agent)
    plan, gaps, _ = asyncio.run(
        generate_cases(_FakeCfg(), "R-999", "規格內容", max_attempts=3))
    assert gaps == [] and len(plan["cases"]) == 4
    assert calls == ["不該被呼叫到"]


def test_重試的prompt帶上一次的缺口_每次都不同(monkeypatch):
    """review 第 4 點:評測固定 temperature 0 + seed 42,同一個 prompt 會得到逐位元組
    相同的輸出。重試不帶這次的缺口,第 2、3 次送的是同一個 prompt,第 3 次等於白跑。

    #23 第 2 點之後,已經有可用計畫時走的是**補缺模式**(只問缺的那幾個,不整份
    重來),措辭跟整份重試不同;但「要指名缺什麼」「每次都要不同」這兩個要求不變,
    所以這裡斷言的是條件編號與方向有沒有出現,不綁特定句型。"""
    two = ("C1", "C2")
    calls = [_raw(_plan(_all_dirs(two)[:3], conditions=two)),       # 缺 C2 false
             _raw(_plan(_all_dirs(two)[:2], conditions=two)),       # 缺 C2 兩向
             _raw(_plan(_all_dirs(two)[:3], conditions=two))]
    users = []

    async def fake_run_agent(cfg, profile, system, user, **kw):
        users.append(user)
        return calls.pop(0)

    monkeypatch.setattr(spec_exec, "run_agent", fake_run_agent)
    asyncio.run(generate_cases(_FakeCfg(), "R-999", "規格內容", max_attempts=3))
    assert len(users) == 3
    assert "C2" in users[1] and "false 向" in users[1], f"沒指名缺的那一向:{users[1][:120]}"
    # 第 2 次模型回的是一份更差的計畫(C2 兩向都沒有)。#23 第 2 點之後**不會**
    # 因此退步:已經拿到的 C2-true 保留在最佳版裡,所以第 3 次要補的仍然只有
    # C2-false。先前的行為是拿第 2 次的結果去算缺口,於是又去要一次已經有的
    # C2-true——把重試次數花在重拿已經成功的東西上。
    assert "C2" in users[2] and "false 向" in users[2], f"沒指名缺的那一向:{users[2][:120]}"
    assert "true 向" not in users[2], (
        "又去要一次已經拿到的 C2-true,表示最佳版沒有被保留")
    assert len(set(users)) == 3, "三次嘗試的 prompt 必須互不相同"


def test_重試的prompt指出條件拆得比之前少(monkeypatch):
    four = ("C1", "C2", "C3", "C4")
    calls = [_raw(_plan(_all_dirs(four)[:-1], conditions=four)),
             _raw(_plan(_all_dirs(("C1",)), conditions=("C1",))),
             _raw(_plan(_all_dirs(four)[:-1], conditions=four))]
    users = []

    async def fake_run_agent(cfg, profile, system, user, **kw):
        users.append(user)
        return calls.pop(0)

    monkeypatch.setattr(spec_exec, "run_agent", fake_run_agent)
    asyncio.run(generate_cases(_FakeCfg(), "R-999", "規格內容", max_attempts=3))
    # 第 2 次回的計畫只拆出 1 個條件(比先前的 4 個少),補缺模式在那種情況下會
    # 退回整份重試——因為繼續「只補缺的」等於接受那份拆得更少的計畫。
    assert "只拆出 1 個原子條件" in users[2] and "4 個" in users[2]


# ─────────────────── 規則碼抽取 ───────────────────

@pytest.mark.parametrize("mr,expected", [
    ({"title": "調整 R-201 閾值", "description": "", "files": []}, ["R-201"]),
    ({"title": "", "description": "依 R-093 核定規格", "files": []}, ["R-093"]),
    ({"title": "", "description": "",
      "files": [{"path": "sql/rules/r305.sql", "full_content": "-- R-305: 夜間"}]}, ["R-305"]),
    # 多個規則碼要全部抓到並排序(去重)
    ({"title": "R-201 與 R-093", "description": "R-201 相關", "files": []},
     ["R-093", "R-201"]),
    ({"title": "純維護", "description": "無規則碼", "files": []}, []),
])
def test_抽取規則碼(mr, expected):
    assert extract_rule_codes(mr) == expected


# ─────────────────── 無規格可驗 ───────────────────

def test_找不到規格時不得自動放行():
    """「必跑」的意思:不是沒 spec 就跳過,而是沒 spec 就不能自動放行。"""
    mr = {"files": [{"path": "sql/rules/r777.sql", "full_content": "SELECT 1;"}]}
    r = asyncio.run(run_spec_exec(None, None, mr, spec_code="R-777", spec_text=None))
    assert r["passed"] is False
    assert r["no_spec"] is True
    assert any(f["severity"] == "major" for f in r["findings"])


# ─────────────────── 仲裁後的兩個分支 ───────────────────

def _stub_spec_exec(monkeypatch, verdict, mismatch_case="C1-T"):
    """把三個角色裡會呼叫模型/資料庫的部分換掉,只留中間的確定性判定邏輯。"""
    plan = _plan([_case("C1-T", "C1", "true"), _case("C1-F", "C1", "false", False)])

    async def fake_generate(cfg, code, text, profile_name=None):
        return plan, [], []

    def fake_execute(sql, plan_):
        return {
            "engine": "stub", "sandbox_error": None, "testdata_error": None,
            "sql_error": None,
            "case_results": [{"case_id": "C1-T"}, {"case_id": "C1-F"}],
            "mismatches": [{
                "case_id": mismatch_case, "condition_id": "C1", "direction": "true",
                "note": "邊界案例", "rows": [["transactions", 1]],
                "expect_flagged": True, "actual_flagged": False, "actual_rows": 0,
            }],
        }

    async def fake_arbitrate(cfg, spec_text, sql, mm, profile_name=None):
        return verdict

    monkeypatch.setattr(spec_exec, "generate_cases", fake_generate)
    monkeypatch.setattr(spec_exec, "execute_cases", fake_execute)
    monkeypatch.setattr(spec_exec, "arbitrate", fake_arbitrate)

    mr = {"files": [{"path": "sql/rules/r201.sql", "full_content": "SELECT 1;"}]}
    return asyncio.run(run_spec_exec(None, None, mr, "R-201", "規格內容"))


def test_仲裁判測資錯_剔除案例並補覆蓋缺口(monkeypatch):
    """這個分支最容易被誤解:仲裁說「是測資錯不是 SQL 錯」聽起來像沒事,

    但剔除那個案例之後,該條件的那個方向就**沒有被驗證過**,所以必須補成覆蓋缺口、
    而且整體不能算通過。不這樣做的話,測資生成得越爛、被剔除得越多,反而越容易「通過」。
    """
    r = _stub_spec_exec(monkeypatch, {"who_is_wrong": "testdata", "reason": "測資日期寫錯"})

    assert r["dropped_cases"] and r["dropped_cases"][0]["by"] == "arbiter"
    assert any("遭仲裁剔除" in g for g in r["coverage_gaps"])
    assert any(c.get("dropped") for c in r["case_results"] if c["case_id"] == "C1-T")
    assert r["passed"] is False, "被剔除的方向沒驗到,不能算通過"


def test_仲裁判_SQL_錯_開_major_finding(monkeypatch):
    r = _stub_spec_exec(monkeypatch,
                        {"who_is_wrong": "sql", "reason": "閾值用了 >= 但規格寫超過"})
    assert r["passed"] is False
    majors = [f for f in r["findings"] if f["severity"] == "major"]
    assert majors and "不符" in majors[0]["title"]
    assert "閾值" in majors[0]["detail"]          # 仲裁理由要帶進報告,可回溯
    assert r["dropped_cases"] == [], "判 SQL 錯時不該剔除案例"


def test_仲裁結果可以從_dropped_cases_看出來(monkeypatch):
    """給 eval 斷言用的落點:dropped_cases 裡 by=arbiter 就代表仲裁判過「測資錯」。

    eval/README「覆蓋缺口」提到「目前也沒有能斷言仲裁結果的訊號」,
    對應的訊號就是從這個欄位讀(見 run_eval.py 的 arbiter_dropped)。
    """
    r = _stub_spec_exec(monkeypatch, {"who_is_wrong": "testdata", "reason": "x"})
    assert [d["by"] for d in r["dropped_cases"]] == ["arbiter"]

    r = _stub_spec_exec(monkeypatch, {"who_is_wrong": "sql", "reason": "x"})
    assert [d["by"] for d in r["dropped_cases"]] == []


# ─────────────────── 執行中止後,沒跑到的案例也是覆蓋缺口 ───────────────────

def _plan_n(n_conditions: int):
    conds = [f"C{i}" for i in range(1, n_conditions + 1)]
    cases = []
    for c in conds:
        cases.append(_case(f"{c}-T", c, "true"))
        cases.append(_case(f"{c}-F", c, "false", False))
    return _plan(cases, conditions=tuple(conds))


def _stub_abort(monkeypatch, plan, ran: int, error_key: str):
    """模擬:跑到第 ran+1 個案例時中止(現行行為是直接放棄剩下全部)。"""
    async def fake_generate(cfg, code, text, profile_name=None, max_attempts=3):
        return plan, [], []

    def fake_execute(sql, plan_):
        results = [{"case_id": c["case_id"], "condition_id": c["condition_id"],
                    "direction": c["direction"], "note": "", "expect_flagged": True,
                    "actual_flagged": True, "actual_rows": [], "ok": True}
                   for c in plan_["cases"][:ran]]
        out = {"engine": "stub", "sandbox_error": None, "testdata_error": None,
               "sql_error": None, "case_results": results, "mismatches": []}
        out[error_key] = "模擬的中止原因"
        return out

    monkeypatch.setattr(spec_exec, "generate_cases", fake_generate)
    monkeypatch.setattr(spec_exec, "execute_cases", fake_execute)
    mr = {"files": [{"path": "sql/rules/r201.sql", "full_content": "SELECT 1;"}]}
    return asyncio.run(run_spec_exec(None, None, mr, "R-201", "規格內容"))


@pytest.mark.parametrize("error_key", ["sql_error", "testdata_error"])
def test_執行中止時沒跑到的案例要列入覆蓋缺口(monkeypatch, error_key):
    """`execute_cases` 的 per-case 迴圈遇到測資建不起來或 SQL 跑不起來會直接
    `return out`,放棄剩下的案例。**中止本身是合理的**(SQL 真的壞掉時跑完只會
    得到一堆相同錯誤),不合理的是報告沒交代這件事。

    仲裁剔除案例時已經有對應處理(「被剔除 = 該向未驗證 → 列入覆蓋缺口」),
    這裡是同一個道理的另一半:**沒跑到 = 沒驗到**。少了它,報告會顯示
    「跑了 3 個案例、零缺口」,讀起來像全覆蓋,實際上多數案例根本沒執行——
    而 coverage_gaps 正是人用來判斷「這次到底驗了多少」的欄位。
    """
    plan = _plan_n(10)                      # 10 條件 → 20 案例
    r = _stub_abort(monkeypatch, plan, ran=3, error_key=error_key)

    assert r["passed"] is False
    assert len(r["case_results"]) == 3, "中止後只有前 3 個案例有結果"
    unrun = [g for g in r["coverage_gaps"] if "因執行中止而未驗證" in g]
    assert len(unrun) == 17, f"17 個沒跑到的案例都要列成缺口,實得 {len(unrun)}"
    assert any("C10-F" in g for g in unrun), "最後一個案例也要在列"
    assert not any("C1-T" in g for g in unrun), "已經跑過的案例不該被列成未驗證"


def test_全部跑完時不會冒出未驗證的缺口(monkeypatch):
    """反向:正常跑完不該因為這個修改多出任何缺口(否則每份報告都會被汙染)。"""
    plan = _plan_n(3)
    r = _stub_abort(monkeypatch, plan, ran=len(plan["cases"]), error_key="sql_error")
    assert [g for g in r["coverage_gaps"] if "因執行中止而未驗證" in g] == []


# ─────────────────── 輸出被截斷時不可把殘骸當結果 ───────────────────

def _truncating_agent(sequence):
    """sequence 的每一項是 (finish_reason, raw)。"""
    seq = list(sequence)

    async def fake(cfg, profile, system, user, hub=None, use_tools=True,
                   verbose=True, trace=None, meta=None):
        reason, raw = seq.pop(0)
        if meta is not None:
            meta["finish_reason"] = reason
            meta["completion_tokens"] = 8192 if reason == "length" else 1234
        return raw
    return fake, seq


def test_截斷的輸出不採用_即使修補後解析得出來(monkeypatch):
    """**這是這道防線的全部意義**:被 max_tokens 硬切的 JSON,會被 extract_json
    的 json-repair 修補成合法物件,`_shape_check` 只看得到「案例比較少」,
    看不出它是殘骸。實測 RETAIL_M1 就是這樣:finish_reason=length、
    completion_tokens 剛好 8192、輸出斷在一個數字中間,卻照樣解析成功,
    案例從應有的 46 個掉到 9 個——然後那 9 個被當成正常結果收下。

    所以截斷必須在**看內容之前**就判定為失敗,不能指望形狀檢查代勞。
    """
    # 關鍵:截斷的那次**剛好 0 缺口**(殘骸裡剩下的案例,對它自己列出的條件是
    # 齊全的)。這時「缺口非空才重試」完全幫不上忙——舊邏輯會把它當完美結果
    # 立刻回傳,而它其實只涵蓋了規格的一小部分。只有看 finish_reason 擋得住。
    truncated_but_looks_perfect = _raw(
        _plan([_case("C1-T", "C1", "true"), _case("C1-F", "C1", "false", False)]))
    full = _plan([_case("C1-T", "C1", "true"), _case("C1-F", "C1", "false", False),
                  _case("C2-T", "C2", "true"), _case("C2-F", "C2", "false", False)],
                 conditions=("C1", "C2"))
    fake, seq = _truncating_agent([("length", truncated_but_looks_perfect),
                                   ("stop", _raw(full))])

    monkeypatch.setattr(spec_exec, "run_agent", fake)
    plan, gaps, dropped = asyncio.run(generate_cases(_FakeCfg(), "R-999", "規格內容"))

    assert seq == [], "截斷那次必須觸發重試,即使它看起來零缺口"
    assert gaps == []
    assert len(plan["cases"]) == 4, "採用的應該是沒被截斷的完整那次,不是截斷的殘骸"


def test_全部嘗試都被截斷時的失敗原因要指出是長度問題(monkeypatch):
    """「規則太大裝不下」跟「模型輸出亂七八糟」的修法完全不同——
    前者要調 max_output_tokens 或分批生成,後者是 prompt/模型的問題。
    失敗原因混為一談,看的人無從判斷該修哪邊。"""
    raw = _raw(_plan([_case("C1-T", "C1", "true")]))
    fake, _ = _truncating_agent([("length", raw)] * 3)

    monkeypatch.setattr(spec_exec, "run_agent", fake)
    plan, gaps, dropped = asyncio.run(
        generate_cases(_FakeCfg(), "R-999", "規格內容", max_attempts=3))

    assert plan["cases"] == []
    assert len(gaps) == 1
    assert "截斷" in gaps[0] and "3/3" in gaps[0]
    assert "max_output_tokens" in gaps[0], "要指出可以調哪個設定"


def test_沒給meta的呼叫端不受影響(monkeypatch):
    """meta 是選用參數,既有的 5 個呼叫點都沒傳——不能因為加了它就改變行為。"""
    complete = _plan([_case("C1-T", "C1", "true"), _case("C1-F", "C1", "false", False)])

    async def fake_no_meta(*a, **kw):
        assert "meta" in kw, "generate_cases 應該要傳 meta"
        return _raw(complete)

    monkeypatch.setattr(spec_exec, "run_agent", fake_no_meta)
    plan, gaps, _ = asyncio.run(generate_cases(_FakeCfg(), "R-999", "規格內容"))
    assert gaps == [] and len(plan["cases"]) == 2


# ─────────────────── 格式錯誤要看得見,不可偽裝成覆蓋缺口(#23 第 1 點) ───────────────────

_CLEAN_PLAN = '''{"schema_ddl": ["CREATE TABLE t (a INT)"],
 "conditions": [{"id": "C1", "desc": "a > 0"}],
 "cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
            "note": "", "rows": [["t", 1]], "expect_flagged": true},
           {"case_id": "C1-F", "condition_id": "C1", "direction": "false",
            "note": "", "rows": [["t", 0]], "expect_flagged": false}]}'''

# 會真的觸發 json-repair 的壞法:結尾多一個逗號。嚴格解析失敗、repair 修得回來,
# 而且修完案例還在——「修補過但仍可用」。
#
# 註:要讓 extract_json 走到 repair 並不容易,它會先掃第一段**括號平衡**的子字串
# 去試 json.loads,所以很多「看起來壞掉」的輸入其實有一段合法子物件可以解出來。
# 這也說明了真實世界那次(R-140 少一個 `]`)為什麼難查:壞法要夠特定才會走到 repair,
# 而走到的時候它不會失敗,只會悄悄給你一個語意錯誤的物件。
_REPAIRABLE_PLAN = _CLEAN_PLAN.replace('"expect_flagged": false}]}',
                                       '"expect_flagged": false},]}')

# 修補得回來、但案例全部過不了形狀檢查(缺 condition_id)——修完等於什麼都不剩。
_REPAIRABLE_BUT_EMPTY = _REPAIRABLE_PLAN.replace('"condition_id": "C1", ', '')


def _gen(monkeypatch, outputs: list[str]):
    """讓 generate_cases 依序拿到指定的模型輸出。"""
    import asyncio

    from orchestrator import spec_exec as se
    seq = list(outputs)

    async def fake(cfg, profile, system, user, hub=None, verbose=True,
                   use_tools=True, trace=None, meta=None):
        if meta is not None:
            meta["finish_reason"] = "stop"
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(se, "run_agent", fake)
    return asyncio.run(se.generate_cases(_FakeCfg(), "R-999", "規格內容"))


def test_修補過的輸出不可以提前結束(monkeypatch):
    """修補過的計畫,它的 0 缺口可能是假的——被 repair 吃掉的案例不會出現在任何
    地方,剩下的對它自己列出的條件剛好齊全。所以不能當成「完整覆蓋」收下。

    第一次給修補過的、第二次給乾淨的:應該要採用第二次。
    """
    plan, gaps, _ = _gen(monkeypatch, [_REPAIRABLE_PLAN, _CLEAN_PLAN])
    assert len(plan["cases"]) == 2
    assert not any("JSON 不合法" in g for g in gaps), (
        f"採用的是乾淨的計畫,不該再標格式錯誤:{gaps}")


def test_只能拿到修補過的結果時_缺口要先講真正的原因(monkeypatch):
    """三次都要修補、但修完仍有可用案例時,要回傳最佳版(不能因為 retry 用完就
    退化成沒案例),但**缺口清單的第一條要講格式問題**。

    放第一條是因為看的人通常只看第一條:寫「缺條件 C5 的 false 向」會把人導向
    「改 prompt 讓模型多生一個案例」,而真正該做的是讓輸出的 JSON 合法。
    """
    plan, gaps, _ = _gen(monkeypatch, [_REPAIRABLE_PLAN] * 3)
    assert plan["cases"], "重試用完就丟掉已抽到的結果了"
    assert gaps and "JSON 不合法" in gaps[0], (
        f"格式問題沒有放在第一條,看的人會照著後面的缺口去調 prompt:{gaps}")


def test_修補後什麼都不剩時_失敗原因要指向格式而不是覆蓋度(monkeypatch):
    """先前這種情況只會說「N 次嘗試皆無合法輸出」,看的人無從判斷是 prompt 問題、
    模型問題、還是規則太大裝不下——而這三者的修法完全不同。"""
    plan, gaps, _ = _gen(monkeypatch, [_REPAIRABLE_BUT_EMPTY] * 3)
    assert plan["cases"] == []
    assert gaps and "JSON 不合法" in gaps[0], f"失敗原因沒指向格式:{gaps}"


def test_乾淨的輸出不會被誤標成格式錯誤(monkeypatch):
    """反方向同樣重要:誤標會讓人去修一個不存在的格式問題。"""
    plan, gaps, _ = _gen(monkeypatch, [_CLEAN_PLAN])
    assert len(plan["cases"]) == 2
    assert not gaps, f"乾淨的計畫不該有缺口:{gaps}"


def test_乾淨的結果一律勝過修補過的(monkeypatch):
    """即使修補過的那份「覆蓋數」比較多也一樣——它的覆蓋數本身就不可信,
    拿它跟乾淨的比是比錯東西。"""
    big_broken = _REPAIRABLE_PLAN.replace(
        '"conditions": [{"id": "C1", "desc": "a > 0"}]',
        '"conditions": [{"id": "C1", "desc": "a > 0"}, {"id": "C2", "desc": "a < 9"}]')
    plan, gaps, _ = _gen(monkeypatch, [big_broken, _CLEAN_PLAN, _CLEAN_PLAN])
    assert len(plan["conditions"]) == 1, (
        f"採用了修補過的那份(條件 {len(plan['conditions'])} 個),"
        "它的覆蓋數不可信,不該勝過乾淨的")
    assert not any("JSON 不合法" in g for g in gaps)


# ─────────────────── 只補缺的方向,不整份重來(#23 第 2 點) ───────────────────

def test_補缺的案例會併回先前的計畫(monkeypatch):
    """大規則一次生不完(實測 RETAIL_M1 要 46 個案例,模型自己停在 14 個;
    調高上限到 4 倍也只多 3 個)。每次都要求「輸出完整計畫」等於每次都在重付
    已經成功那部分的代價,而那正是裝不下的原因。

    這裡:第一次給 C1 兩向 + C2 true,第二次只回缺的 C2 false,應該合併成 4 個案例。
    """
    first = _plan(_all_dirs(("C1", "C2"))[:3], conditions=("C1", "C2"))
    only_missing = {"cases": [c for c in _plan(_all_dirs(("C1", "C2")),
                                               conditions=("C1", "C2"))["cases"]
                              if c["condition_id"] == "C2" and c["direction"] == "false"]}
    plan, gaps, _ = _gen(monkeypatch, [_raw(first), json.dumps(only_missing, ensure_ascii=False)])
    assert len(plan["cases"]) == 4, f"沒有併回去,拿到 {len(plan['cases'])} 個案例"
    assert not gaps, f"補完了卻還有缺口:{gaps}"


def test_補生成不可以發明新的條件(monkeypatch):
    """模型自己加一個條件,覆蓋率會看起來變好——但那個條件根本不在規格的拆解裡,
    等於用一個假的分母把覆蓋率灌上去。"""
    first = _plan(_all_dirs(("C1", "C2"))[:3], conditions=("C1", "C2"))
    bogus = {"cases": [{"case_id": "X-T", "condition_id": "C_NEW", "direction": "true",
                        "note": "", "rows": [["t", 1]], "expect_flagged": True}]}
    plan, _, dropped = _gen(monkeypatch,
                            [_raw(first), json.dumps(bogus, ensure_ascii=False)] * 2)
    assert all(c["condition_id"] in ("C1", "C2") for c in plan["cases"]), \
        "併進了不在原計畫裡的條件"
    assert dropped, "被拒絕的案例沒有記錄下來"


def test_補生成不可以改寫已經有的案例(monkeypatch):
    """補生成只補缺的,不該動已經驗過的案例——否則每一輪的結果都可能被後面的
    嘗試悄悄改掉,而那無法從報告看出來。"""
    first = _plan(_all_dirs(("C1", "C2"))[:3], conditions=("C1", "C2"))
    original = [c for c in _shape_check(first)[0]["cases"]]
    overwrite = {"cases": [{**original[0], "rows": [["t", 999]], "note": "改過的"}]}
    plan, _, _ = _gen(monkeypatch,
                      [_raw(first), json.dumps(overwrite, ensure_ascii=False)] * 2)
    same = [c for c in plan["cases"] if c["case_id"] == original[0]["case_id"]]
    assert same and same[0]["rows"] == original[0]["rows"], "已經有的案例被改寫了"


def test_補生成的_schema_一律沿用先前的(monkeypatch):
    """先前的案例是依原本的 schema 寫的。補生成若回了不同的 DDL 還照收,
    那些案例就跑不起來了——而失敗會出現在沙盒執行階段,離真正的原因很遠。"""
    first = _plan(_all_dirs(("C1",)), conditions=("C1",))
    first["cases"] = first["cases"][:1]            # 留一個缺口,才會進補缺模式
    other_ddl = {"schema_ddl": ["CREATE TABLE 別的表 (x INT)"], "cases": []}
    plan, _, _ = _gen(monkeypatch,
                      [_raw(first), json.dumps(other_ddl, ensure_ascii=False)] * 2)
    assert plan["schema_ddl"] == _shape_check(first)[0]["schema_ddl"], \
        "補生成換掉了 schema,先前的案例會跑不起來"


# ─────────────────── 標籤與資料要一致(#23 第 3 點) ───────────────────

@pytest.mark.parametrize("sql,ok", [
    ("SELECT CASE WHEN EXISTS(SELECT 1 FROM t WHERE a > 0) THEN 1 ELSE 0 END", True),
    ("select 1", True),
    ("SELECT 1; DROP TABLE t", False),          # 分號 = 可以接第二個語句
    ("SELECT 1 -- 註解", False),
    ("SELECT 1 /* 註解 */", False),
    ("DELETE FROM t", False),                   # 不是 SELECT
    ("EXEC xp_cmdshell 'dir'", False),
    ("SELECT * FROM t WHERE x = (SELECT TOP 1 a FROM u)", True),
    ("", False),
    ("SELECT " + "x" * 700, False),             # 超長
])
def test_判斷式必須長得像判斷式(sql, ok):
    """check_sql 是模型產生的、會進沙盒執行的 SQL,所以收得比 _DDL_OK 更緊。

    這**不是**完整的 SQL 剖析,擋不住所有壞東西;它的作用是讓判斷式維持在判斷式
    的形狀,而不是變成另一個可以執行任意語句的入口。沙盒本身的權限收斂是 #26 第 2 點。
    """
    assert spec_exec._check_sql_ok(sql) is ok


def test_判斷式形狀不合法時只丟掉那個欄位_不丟掉條件():
    """條件本身是好的,只是附帶的判斷式不能用——不該因此讓這個條件的案例全部消失。"""
    plan = _plan(_all_dirs(("C1",)), conditions=("C1",))
    plan["conditions"][0]["check_sql"] = "SELECT 1; DROP TABLE t"
    cleaned, gaps, dropped = _shape_check(plan)
    assert len(cleaned["conditions"]) == 1, "整個條件被丟掉了"
    assert "check_sql" not in cleaned["conditions"][0], "不合法的判斷式還留著"
    assert len(cleaned["cases"]) == 2, "條件的案例跟著消失了"
    assert any("check_sql" in str(d.get("reason")) for d in dropped), "沒有記錄原因"


def test_沒給判斷式時行為完全不變():
    """check_sql 是可選的:既有的測資計畫(含已經凍結的)不帶這個欄位,
    不該因為這次改動而有任何行為差異。"""
    plan = _plan(_all_dirs(("C1",)), conditions=("C1",))
    cleaned, gaps, dropped = _shape_check(plan)
    assert not gaps and not dropped
    assert all("check_sql" not in c for c in cleaned["conditions"])


# ── 標籤應有的真假向量 ──

def test_只驗被標的那一條_正向():
    conds = [{"id": "C1", "desc": "x"}, {"id": "C2", "desc": "y"}]
    want = spec_exec._label_expectation(conds, {"condition_id": "C1", "direction": "true"})
    assert want == {"C1": True}


def test_只驗被標的那一條_反向():
    conds = [{"id": "C1", "desc": "x"}, {"id": "C2", "desc": "y"}, {"id": "C3", "desc": "z"}]
    want = spec_exec._label_expectation(conds, {"condition_id": "C2", "direction": "false"})
    assert want == {"C2": False}


def test_不要求其他條件成立_否則OR結構的規格100趴誤報():
    """原本的版本要求「反向案例只有被標的那一條不成立,其餘全部成立」,用意是讓
    反向案例能把失敗歸因到某一條。那對純 AND 的規格成立,對「條件 1/2/3 任一
    成立」這種 OR 結構**在數學上不可能成立**:

        C1-1:kiosk_outbound_cnt 介於 2–3
        C3-1:kiosk_outbound_cnt 介於 4–10      ← 交集是空集合

    真端點實測(RETAIL_M1_EVAL):30 個案例 30 個被判標籤不符,含 22 個行為
    完全正確的。而 _label_gaps 會把標籤不符轉成覆蓋缺口 → 只要規格是 OR 結構,
    spec_exec 的結果就全盤報廢。
    """
    conds = [{"id": "C1-1", "desc": "cnt 介於 2–3"},
             {"id": "C3-1", "desc": "cnt 介於 4–10"}]   # 互斥,不可能同時成立
    want = spec_exec._label_expectation(conds, {"condition_id": "C1-1",
                                                "direction": "true"})
    assert "C3-1" not in want, "對互斥的條件下判定 = 必然誤報"


# ── 算不出來就整個放棄 ──

class _Cur:
    def __init__(self, answers): self.answers = list(answers)
    def execute(self, q): self._next = self.answers.pop(0)
    def fetchone(self):
        if isinstance(self._next, Exception): raise self._next
        return self._next


def test_判斷式算不出來時整個放棄而不是部分回報():
    """少算一條就可能把「兩條同時不成立」看成「只有一條不成立」——
    那正是這道檢查要抓的東西,半套的結果比沒有更危險。"""
    conds = [{"id": "C1", "desc": "x", "check_sql": "SELECT 1"},
             {"id": "C2", "desc": "y", "check_sql": "SELECT 1"}]
    assert spec_exec._eval_conditions(_Cur([(1,), RuntimeError("boom")]), conds) is None
    assert spec_exec._eval_conditions(_Cur([(1,), None]), conds) is None, "沒有列也要放棄"
    # 有條件沒附判斷式 → 整個放棄(不能只驗一半的條件)
    assert spec_exec._eval_conditions(_Cur([(1,)]),
                                      [conds[0], {"id": "C2", "desc": "y"}]) is None
    assert spec_exec._eval_conditions(_Cur([(1,), (0,)]), conds) == {"C1": True, "C2": False}


# ── 不符要列入覆蓋缺口 ──

def test_標籤不符要列入覆蓋缺口():
    """不改 ok(那是「行為符不符合預期」),另外列入缺口:那一向沒有被有效驗證,
    不可據以自動放行。與仲裁剔除、執行中止是同一個原則。"""
    results = [{"case_id": "C2-F", "condition_id": "C2", "direction": "false",
                "ok": True, "label_ok": False, "label_mismatch": ["C7"]}]
    gaps = spec_exec._label_gaps(results)
    assert len(gaps) == 1
    assert "C2-F" in gaps[0] and "C7" in gaps[0] and "未經有效驗證" in gaps[0]


def test_標籤相符或沒驗時不產生缺口():
    """沒給判斷式(label_ok 不存在)時不該憑空生出缺口。"""
    assert spec_exec._label_gaps([{"case_id": "A", "label_ok": True}]) == []
    assert spec_exec._label_gaps([{"case_id": "A"}]) == []


class _FakeCursor:
    """只做 execute_cases 用到的事:execute / fetchall / fetchone / description。"""

    def __init__(self):
        self.description = None

    def execute(self, sql, params=None):
        self.description = [("col",)] if sql.lstrip().upper().startswith("SELECT") else None
        self._sql = sql

    def fetchone(self):
        return ("16.0.1000.6", "Developer Edition")

    def fetchall(self):
        return [(1,)]

    def close(self):
        pass


class _FakeCon:
    def __init__(self, **kw):
        pass

    def cursor(self):
        return _FakeCursor()

    def rollback(self):
        pass

    def close(self):
        pass


def _fake_sandbox(monkeypatch):
    """把 pymssql 換成假的,讓 execute_cases 整條路跑得完——不連真沙盒。"""
    import types
    fake = types.ModuleType("pymssql")
    fake.connect = lambda **kw: _FakeCon(**kw)
    monkeypatch.setitem(sys.modules, "pymssql", fake)
    monkeypatch.setattr(spec_exec, "_sandbox_params",
                        lambda: {"server": "x", "port": 1433, "user": "sa",
                                 "password": "pw"})
    monkeypatch.setattr(spec_exec, "_drop_stale_sandbox_dbs", lambda admin: None)


class _CondCursor(_FakeCursor):
    """判斷式查詢回固定答案:conds_true 裡的條件回 1,其餘回 0。"""

    answers: dict = {}

    def execute(self, sql, params=None):
        super().execute(sql, params)
        self._cond = self.answers.get(sql.strip())

    def fetchone(self):
        if self._cond is None:
            return ("16.0.1000.6", "Developer Edition")
        return (1 if self._cond else 0,)


def test_其他條件同時不成立時只記錄不判定(monkeypatch):
    """OR 結構的規格裡,不同分支的條件本來就互斥——同時有幾條不成立是正常的,
    不可以當成「這個案例沒測到它宣稱的那一條」。所以記在 label_cofail,
    不影響 label_ok。真端點實測:不這樣分開,30 個案例 30 個被判不符。"""
    _fake_sandbox(monkeypatch)
    _CondCursor.answers = {"SELECT 1 WHERE A": True,      # 被標的這一條:成立
                           "SELECT 1 WHERE B": False}     # 別的分支:不成立
    monkeypatch.setattr(_FakeCon, "cursor", lambda self: _CondCursor())
    plan = {"schema_ddl": ["CREATE TABLE t (id INT)"],
            "conditions": [{"id": "A", "desc": "a", "check_sql": "SELECT 1 WHERE A"},
                           {"id": "B", "desc": "b", "check_sql": "SELECT 1 WHERE B"}],
            "cases": [{"case_id": "A-T", "condition_id": "A", "direction": "true",
                       "expect_flagged": True, "note": "", "rows": [["t", 1]]}]}
    out = spec_exec.execute_cases("SELECT id FROM t", plan)
    r = out["case_results"][0]
    assert r["label_ok"] is True, f"被標的那一條成立,不該判不符:{r.get('label_mismatch')}"
    assert r["label_cofail"] == ["B"], "其他條件不成立要記下來(供診斷)"


def test_execute_cases_沒有conditions欄位也不能炸(monkeypatch):
    """`execute_cases` 不只收 `generate_cases()` 的結果:`eval/preflight.py` 的沙盒
    探針與 `eval/check_frozen_testdata.py` 都自己組 plan。#23 第 3 點的標籤檢查
    直接取 `plan["conditions"]`,少了這個欄位就 KeyError——而 execute_cases 把所有
    例外都包成 `sandbox_error`,於是 preflight 回報「沙盒執行異常」、整輪跑批中止在
    「環境壞了」,實際上是我們的程式。真的發生過(公司端點上的起飛前檢查第一次
    就撞到,而且本機沒有沙盒、重現不了)。
    """
    _fake_sandbox(monkeypatch)
    plan = {"schema_ddl": ["CREATE TABLE t (id INT, amt INT)"],
            "cases": [{"case_id": "P1", "condition_id": "C1", "direction": "positive",
                       "expect_flagged": True, "note": "", "rows": [["t", 1, 500]]}]}
    out = spec_exec.execute_cases("SELECT id FROM t WHERE amt > 100", plan)
    assert not out["sandbox_error"], out["sandbox_error"]
    assert len(out["case_results"]) == 1
    assert "label_ok" not in out["case_results"][0], "沒有條件可比就不該下標籤結論"


def test_preflight的沙盒探針帶得動標籤檢查():
    """探針的案例宣稱測 C1,plan 就要有 C1——不然它只驗到「少了 conditions 不會炸」,
    驗不到正常情況。這條是在上面那個迴歸之後補的。"""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "eval"))
    import preflight
    ids = {c["id"] for c in preflight.PROBE_PLAN.get("conditions", [])}
    used = {c["condition_id"] for c in preflight.PROBE_PLAN["cases"]}
    assert used <= ids, f"探針的案例宣稱測 {used - ids},但 plan 裡沒有這些條件"


# ─────────────────── 大規則:截斷 → 骨架 → 逐批補(#23 第 2 點的實際可用性) ───────────────────

def _trunc_raw():
    """模擬被輸出上限切斷的回應。"""
    return "{"


def _gen_meta(monkeypatch, outputs, finish_reasons):
    """outputs 與 finish_reasons 一一對應,讓測試能模擬「前幾次截斷」。"""
    import asyncio

    from orchestrator import spec_exec as se
    seq, fr = list(outputs), list(finish_reasons)
    users = []

    async def fake(cfg, profile, system, user, hub=None, verbose=True,
                   use_tools=True, trace=None, meta=None):
        users.append(user)
        if meta is not None:
            meta["finish_reason"] = fr.pop(0) if len(fr) > 1 else fr[0]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(se, "run_agent", fake)
    out = asyncio.run(se.generate_cases(_FakeCfg(), "R-999", "規格內容"))
    return out, users


def test_第一次截斷時改要骨架而不是精簡版的完整計畫(monkeypatch):
    """大規則連精簡過的完整計畫都裝不下——實測 RETAIL_M1_EVAL 在正式設定下
    3/3 次截斷、0 個案例。繼續要「完整計畫但精簡一點」只會再截斷三次。

    骨架(只有 schema 與條件)的輸出小得多,拿到之後案例靠補缺一批一批補。
    """
    skeleton = {"schema_ddl": ["CREATE TABLE t (a INT)"],
                "conditions": [{"id": "C1", "desc": "a > 0"}], "cases": []}
    _, users = _gen_meta(monkeypatch,
                         [_trunc_raw(), json.dumps(skeleton, ensure_ascii=False)],
                         ["length", "stop"])
    assert "骨架" in users[1], f"截斷後沒有改要骨架:{users[1][:140]}"
    assert "cases 一律留空" in users[1]


def test_骨架可以成為補缺的起點(monkeypatch):
    """沒有這一條,大規則會停在「第一次截斷 → 沒有計畫 → 補缺永遠不啟動」。"""
    skeleton = {"schema_ddl": ["CREATE TABLE t (a INT)"],
                "conditions": [{"id": "C1", "desc": "a > 0"}], "cases": []}
    both = {"cases": _shape_check(_plan(_all_dirs(("C1",)), conditions=("C1",)))[0]["cases"]}
    (plan, gaps, _), users = _gen_meta(
        monkeypatch,
        [_trunc_raw(), json.dumps(skeleton, ensure_ascii=False),
         json.dumps(both, ensure_ascii=False)],
        ["length", "stop", "stop"])
    assert len(plan["cases"]) == 2, f"骨架之後沒有補進案例:{plan['cases']}"
    assert not gaps, f"補完了卻還有缺口:{gaps}"
    assert "只要補缺的案例" in users[2], "第 3 次不是補缺模式"


# ── 跨次累積:接著上次的缺口補(--resume) ──

def test_seed_plan_直接進補缺模式不重新生成整份(monkeypatch):
    """大規則一輪補不完(實測 46 個「條件×方向」,13 次呼叫 151 分鐘補到 30 個),
    但補缺是可累積的:接著上次的結果再跑一輪就會往前推進。

    所以給了 seed_plan 時,第一次呼叫就必須是**補缺**——不可以再問一次整份計畫
    或骨架,那會把已經生好的案例丟掉重來(每次呼叫 11-16 分鐘)。
    """
    ids = ("C1", "C2", "C3")
    seed = _shape_check(_plan(_all_dirs(("C1",)), conditions=ids))[0]  # 缺 C2/C3
    rest = {"cases": _all_dirs(("C2", "C3"))}
    (plan, gaps, _), users = _gen_meta_seed(
        monkeypatch, seed, [json.dumps(rest, ensure_ascii=False)], ["stop"])
    assert "只要補缺的案例" in users[0], f"第一次就該是補缺:{users[0][:120]}"
    assert len(plan["cases"]) == 6 and not gaps, f"沒補滿:{len(plan['cases'])} {gaps}"


def test_seed_plan_已經補滿時不呼叫模型(monkeypatch):
    """已經滿了還去問模型 = 白付一次 11-16 分鐘的呼叫。"""
    ids = ("C1",)
    seed = _shape_check(_plan(_all_dirs(ids), conditions=ids))[0]
    (plan, gaps, _), users = _gen_meta_seed(monkeypatch, seed, ["{}"], ["stop"])
    assert users == [], "已經補滿卻還是呼叫了模型"
    assert len(plan["cases"]) == 2 and not gaps


def _gen_meta_seed(monkeypatch, seed, outputs, finish_reasons):
    import asyncio as _a

    from orchestrator import spec_exec as se
    seq, fr, users = list(outputs), list(finish_reasons), []

    async def fake(cfg, profile, system, user, hub=None, verbose=True,
                   use_tools=True, trace=None, meta=None):
        users.append(user)
        if meta is not None:
            meta["finish_reason"] = fr.pop(0) if len(fr) > 1 else fr[0]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(se, "run_agent", fake)
    out = _a.run(se.generate_cases(_FakeCfg(), "R-999", "規格內容", seed_plan=seed))
    return out, users


def test_補缺被截斷時縮小批量而不是退回整份重來(monkeypatch):
    """補缺的回應被截斷,代表「這一批要太多」,不代表「補缺這條路不行」。

    退回去要整份計畫會把已經拿到的骨架與案例全部丟掉,再走一次已知裝不下的路
    ——重放模擬:補缺被截斷時退回整份,最後拿到 **0/46** 個案例(真實端點量到的
    正是 0 個案例、3/3 截斷);改成把批量減半重試則補滿 46/46。
    """
    skeleton = {"schema_ddl": ["CREATE TABLE t (a INT)"],
                "conditions": [{"id": f"C{i}", "desc": "x"} for i in range(1, 21)],
                "cases": []}
    ids = tuple(f"C{i}" for i in range(1, 21))   # 20 個條件 = 40 個「條件×方向」,
    both = {"cases": _shape_check(_plan(_all_dirs(ids),               # 比一批的量多
                                        conditions=ids))[0]["cases"]}
    # 第 3 次(第一批補缺)被截斷 → 第 4 次必須還是補缺,而且要的數量更少
    (plan, gaps, _), users = _gen_meta(
        monkeypatch,
        [_trunc_raw(), json.dumps(skeleton, ensure_ascii=False),
         _trunc_raw(), json.dumps(both, ensure_ascii=False)],
        ["length", "stop", "length", "stop"])
    assert "只要補缺的案例" in users[3], (
        f"補缺被截斷後退回整份重來了:{users[3][:140]}")
    asked = [int(re.search(r"要補的\((\d+) 個", u).group(1))
             for u in users if "只要補缺的案例" in u]
    assert asked[1] < asked[0], f"批量沒有縮小:{asked}"
    assert len(plan["cases"]) == 40 and not gaps, f"最後沒補滿:{len(plan['cases'])} 案例 {gaps}"


def test_補缺被截斷且還沒有任何計畫時才要骨架(monkeypatch):
    """縮批量的前提是「手上已經有一份計畫(骨架)」。還沒有計畫就被截斷時,
    要的仍然是骨架——不要把兩種情況混在一起。"""
    skeleton = {"schema_ddl": ["CREATE TABLE t (a INT)"],
                "conditions": [{"id": "C1", "desc": "x"}], "cases": []}
    _, users = _gen_meta(monkeypatch,
                         [_trunc_raw(), _trunc_raw(),
                          json.dumps(skeleton, ensure_ascii=False)],
                         ["length", "length", "stop"])
    assert "骨架" in users[1] and "骨架" in users[2], (
        f"沒有計畫時被截斷卻沒要骨架:{users[1][:80]} / {users[2][:80]}")


def test_補缺的輪數不佔整份生成的次數(monkeypatch):
    """混在同一個預算裡,補一輪就用掉一次整份生成的機會,大規則永遠補不完。

    這裡:1 次截斷 + 1 次骨架 + 多輪補缺,總呼叫數要超過 max_attempts(3)。
    """
    skeleton = {"schema_ddl": ["CREATE TABLE t (a INT)"],
                "conditions": [{"id": f"C{i}", "desc": "x"} for i in range(1, 6)],
                "cases": []}
    # 每輪只補一個案例,所以要補很多輪才補得完 5 個條件 × 2 向 = 10 個
    one = {"cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
                      "note": "", "rows": [["t", 1]], "expect_flagged": True}]}
    (plan, _, _), users = _gen_meta(
        monkeypatch,
        [_trunc_raw(), json.dumps(skeleton, ensure_ascii=False),
         json.dumps(one, ensure_ascii=False)],
        ["length", "stop", "stop"])
    assert len(users) > 3, (
        f"只呼叫了 {len(users)} 次——補缺用掉了整份生成的預算")


# ─────────────────── 基準＋變體(測資正確率的修法) ───────────────────
#
# 舊的覆蓋模型是「每個條件 × true/false 兩向」。對合取規則它**定義不出正確的
# 預期**:單獨讓一個條件成立不會讓規則命中,所以那種案例的 expect_flagged 必然
# 是錯的。真端點實測(RETAIL_M1_EVAL,46 個案例 0 缺口):12 個預期是錯的,
# 其中 10 個就是這個原因。
#
# 新的形狀:
#   基準(kind="base",屬於某個分支)  → 整個分支都成立,規則必然命中
#   變體(kind="variant",壞掉某一條)→ 從基準只壞一條,規則必然不命中
# 預期值由構造決定,不問模型(它算錯過)。

def _bv_plan(cases, conds=(("C1", "B1"), ("C2", "B1"), ("C3", "B2"))):
    return {"schema_ddl": ["CREATE TABLE t (id INT);"],
            "conditions": [{"id": i, "desc": f"條件 {i}", "branch": b} for i, b in conds],
            "cases": list(cases)}


def _base(cid, branch, flagged=None):
    c = {"case_id": cid, "kind": "base", "branch": branch,
         "rows": [["t", 1]], "note": ""}
    if flagged is not None:
        c["expect_flagged"] = flagged
    return c


def _variant(cid, cond, flagged=None):
    c = {"case_id": cid, "kind": "variant", "condition_id": cond,
         "rows": [["t", 1]], "note": ""}
    if flagged is not None:
        c["expect_flagged"] = flagged
    return c


def test_基準的預期是命中_變體的預期是不命中_不問模型():
    """模型給的 expect_flagged 一律覆寫。實測它在 46 個案例裡錯了 12 個,
    而預期值錯比沒有案例更糟——正確的實作會被判成不符。"""
    plan = _bv_plan([_base("B1", "B1", flagged=False),      # 模型給錯的值
                     _variant("V1", "C1", flagged=True)])   # 也給錯
    cleaned, _, _ = _shape_check(plan)
    by = {c["case_id"]: c for c in cleaned["cases"]}
    assert by["B1"]["expect_flagged"] is True, "基準必然命中"
    assert by["V1"]["expect_flagged"] is False, "變體必然不命中"


def test_覆蓋的定義變成_每個分支要有基準_每個條件要有變體():
    plan = _bv_plan([_base("B1", "B1"), _variant("V1", "C1")])
    _, gaps, _ = _shape_check(plan)
    assert any("分支 B2 缺基準" in g for g in gaps), gaps
    assert any("條件 C2 缺變體" in g for g in gaps), gaps
    assert any("條件 C3 缺變體" in g for g in gaps), gaps
    assert not any("C1 缺變體" in g for g in gaps)


def test_齊全時沒有缺口():
    plan = _bv_plan([_base("b1", "B1"), _base("b2", "B2"),
                     _variant("v1", "C1"), _variant("v2", "C2"),
                     _variant("v3", "C3")])
    _, gaps, _ = _shape_check(plan)
    assert gaps == [], gaps


def test_基準的分支必須是宣告過的():
    plan = _bv_plan([_base("x", "沒宣告的分支")])
    cleaned, _, dropped = _shape_check(plan)
    assert not cleaned["cases"] and dropped and "branch" in dropped[0]["reason"]


def test_舊形狀的計畫仍然讀得懂():
    """現有的凍結檔是「每條件兩向」,換了設計不能讓它們全部失效。"""
    plan = _plan(_all_dirs(("C1",)), conditions=("C1",))
    cleaned, gaps, dropped = _shape_check(plan)
    assert len(cleaned["cases"]) == 2 and not gaps and not dropped
    assert all("kind" not in c for c in cleaned["cases"])


def test_隔離性在分支內可以判定():
    """原本因為 OR 分支互斥而不得不拔掉的判定,在基準＋變體的形狀下回來了:
    分支**內**是純 AND,「其他條件都成立」在數學上做得到。"""
    conds = [{"id": "C1", "desc": "a", "branch": "B1"},
             {"id": "C2", "desc": "b", "branch": "B1"},
             {"id": "C3", "desc": "c", "branch": "B2"}]
    want = spec_exec._label_expectation(conds, {"condition_id": "C1",
                                                "direction": "false",
                                                "kind": "variant"})
    assert want == {"C1": False, "C2": True}, "只管同分支,且只有被壞的那條為假"
    want_b = spec_exec._label_expectation(conds, {"kind": "base", "branch": "B1",
                                                  "condition_id": "__base__B1",
                                                  "direction": "true"})
    assert want_b == {"C1": True, "C2": True}, "基準:同分支全部成立"
    assert "C3" not in want and "C3" not in want_b, "別的分支本來就互斥,不可判定"


def test_不是資料層面的條件不要求變體():
    """「每帳戶每日至多一筆」、「輸出欄位」、「待補事項」這類條件造不出
    「只壞這一條」的變體,要求它會永遠補不完。它們本來該由靜態審查管。"""
    plan = {"schema_ddl": ["CREATE TABLE t (id INT);"],
            "conditions": [{"id": "C1", "desc": "金額門檻", "branch": "B1"},
                           {"id": "C9", "desc": "每帳戶每日至多一筆",
                            "branch": "B1", "data_level": False}],
            "cases": [_base("b", "B1"), _variant("v", "C1")]}
    _, gaps, _ = _shape_check(plan)
    assert gaps == [], f"不該要求 C9 的變體:{gaps}"


def test_補缺會先要基準再要變體():
    """變體要從基準改出來,沒有基準就沒有東西可以改。"""
    plan = _shape_check(_bv_plan([]))[0]
    missing = spec_exec._missing_pairs(plan)
    kinds = [k for k, _ in missing]
    assert kinds[:2] == ["base", "base"], missing
    assert set(kinds[2:]) == {"variant"}


def test_要變體時prompt要附上基準的資料列():
    """不附的話模型只能自己重新發明一組資料,那正是預期值出錯的來源
    (實測 46 個案例 12 個錯,10 個是「自己發明、只滿足一條」)。"""
    plan = _shape_check(_bv_plan([_base("b1", "B1")]))[0]
    plan["cases"][0]["rows"] = [["t", 777]]
    missing = [("variant", "C1")]
    text = spec_exec._fill_gaps_prompt("R-1", "規格", plan, missing, 8)
    assert "777" in text, "基準的資料列沒有附在 prompt 裡"
    assert "只改一處" in text or "只改要壞的那一處" in text


def test_補缺要基準時說明該分支有哪些條件():
    plan = _shape_check(_bv_plan([]))[0]
    text = spec_exec._fill_gaps_prompt("R-1", "規格", plan, [("base", "B1")], 8)
    assert "分支 B1" in text and "C1" in text and "C2" in text
    assert "同時成立" in text


def test_舊形狀的補缺描述不變():
    plan = _shape_check(_plan([], conditions=("C1",)))[0]
    text = spec_exec._fill_gaps_prompt("R-1", "規格", plan, [("C1", "true")], 8)
    assert "條件 C1" in text and "true 向" in text


def test_補缺回來的基準與變體都要併得進去():
    """`_merge_cases` 原本一律用 condition_id 判斷「這個條件在不在原計畫裡」,
    而基準的 condition_id 是我們自己補的 `__base__<分支>`、不在條件清單裡
    ——於是補缺回來的基準被整批拒收,補缺模式永遠拿不到基準。實測確認過。
    """
    conds = [{"id": "C1", "desc": "a", "branch": "B1"},
             {"id": "C2", "desc": "b", "branch": "B1"},
             {"id": "C3", "desc": "c", "branch": "B2"}]
    plan, _, _ = _shape_check({"schema_ddl": ["CREATE TABLE t (id INT)"],
                               "conditions": conds, "cases": []})
    add = {"cases": [
        {"case_id": "b1", "kind": "base", "branch": "B1", "rows": [["t", 1]]},
        {"case_id": "v1", "kind": "variant", "condition_id": "C1", "rows": [["t", 1]]},
        {"case_id": "x1", "kind": "base", "branch": "沒宣告過", "rows": [["t", 1]]},
        {"case_id": "x2", "kind": "variant", "condition_id": "C9", "rows": [["t", 1]]}]}
    merged, added, dropped = spec_exec._merge_cases(plan, add)
    assert added == 2, f"基準/變體沒併進去:{[d.get('reason') for d in dropped]}"
    assert len(dropped) == 2, "沒宣告的分支與不存在的條件要拒收"
    got = {(c["case_id"], c["kind"], c["expect_flagged"]) for c in merged["cases"]}
    assert got == {("b1", "base", True), ("v1", "variant", False)}


def test_同一個分支的基準不會被補第二次():
    """補缺是只併不覆寫:已經驗過的基準不該被後來的回應改寫。"""
    conds = [{"id": "C1", "desc": "a", "branch": "B1"}]
    plan, _, _ = _shape_check({"schema_ddl": ["CREATE TABLE t (id INT)"],
                               "conditions": conds,
                               "cases": [_base("first", "B1")]})
    add = {"cases": [{"case_id": "second", "kind": "base", "branch": "B1",
                      "rows": [["t", 2]]}]}
    merged, added, dropped = spec_exec._merge_cases(plan, add)
    assert added == 0 and len(dropped) == 1
    assert [c["case_id"] for c in merged["cases"]] == ["first"]
