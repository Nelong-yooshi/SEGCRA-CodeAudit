"""判斷校準的確定性測試 — 不呼叫 LLM、不連資料庫。

為什麼值得單獨測:這三道後處理做的是**把模型的指控往下壓**(降級、壓到 minor、
甚至移除),壓錯的代價是真缺陷從報告上消失。所以每一條都要證明兩件事:
該壓的壓得到,**不該壓的不會被壓到**。

驗的東西:
  1. 執行驗證對照「實作與規格不符」:兩向都驗過且符合預期才降成 info;有覆蓋缺口、
     對不上條件、案例被剔除、結果不符 → 一律維持原判(只加註)。
  2. 兩種測資格式都吃得下:舊格式(每條件 true/false 兩向)與新格式(基準案例 +
     單一條件變體)。
  3. 執行驗證沒跑成(無規格 / 沒有案例結果)→ 整步跳過;無規格另外記「要人看」。
  4. 程式自己補的執行驗證 finding 不被自己降級(它是證據,不是指控)。
  5. 規格「待補 / 未載明」段落:規格不符類指控改成 info 提問 + 要人看;沒對應到的
     finding 不受影響。
  6. 斷言要有依據:自述等價 → 最多 minor;語法斷言解析得過 → 移除、解析不過 → 最多
     minor;fan-out 要指名 JOIN 鍵並說明會進到輸出,否則最多 minor。
  7. 驗收情境:三種等價重構(反連接改寫、過濾移進條件聚合、CTE 別名在外層 WHERE)
     跑完一輪後沒有 major、決策不是 blocked。
"""
import pytest

from orchestrator.pipeline import (_DBT_NOTICE_TITLE, _FORMAT_TITLE, _PARSE_FAIL_TITLE,
                                   _RENDER_FAIL_TITLE, _TOOL_REFUSAL_TITLE,
                                   _UNREVIEWABLE_TITLE, apply_policy, apply_rubric,
                                   calibrate_with_execution, enforce_claim_evidence,
                                   enforce_spec_pending)

CONDS = [{"id": "C1", "desc": "交易類型為 WITHDRAW"},
         {"id": "C2", "desc": "七日內累計金額達 500000 以上"}]


def _case(cid, expect, ok=True, **over):
    r = {"case_id": f"{cid}-{expect}", "condition_id": cid, "expect_flagged": expect,
         "actual_flagged": expect if ok else not expect, "ok": ok}
    r.update(over)
    return r


def _spec_exec(**over):
    """C1、C2 都兩向驗過且全部符合預期(舊格式)。"""
    base = {"passed": True, "spec_code": "R-201", "conditions": CONDS,
            "coverage_gaps": [], "dropped_cases": [],
            "case_results": [_case("C1", True), _case("C1", False),
                             _case("C2", True), _case("C2", False)]}
    base.update(over)
    return base


def _mismatch(detail="缺少 WITHDRAW 的過濾", severity="major", **over):
    f = {"file": "sql/rules/r201.sql", "line": 10, "severity": severity,
         "title": "實作與核定規格不符", "detail": detail, "suggestion": "", "citations": []}
    f.update(over)
    return f


def _report(findings, spec_exec=None):
    r = {"findings": findings}
    if spec_exec is not None:
        r["_spec_exec"] = spec_exec
    return r


# ---- 1. 執行驗證對照模型的「規格不符」指控 ----

def test_條件兩向都驗過且符合預期_指控降成_info():
    """同一份報告並列「案例皆符合預期」與「實作與規格不符 major」是實測看到的矛盾。
    執行驗證已經對這個條件跑過成立/不成立兩向,指控沒被重現 → 降成 info。"""
    r = calibrate_with_execution(_report([_mismatch()], _spec_exec()))
    f = r["findings"][0]
    assert f["severity"] == "info"
    assert "執行驗證未重現" in f["detail"]
    assert "C1" in f["detail"]
    assert r["_calibration"]["downgraded"][0]["conditions"] == "C1"


def test_新格式的基準案例加變體也算驗過():
    """新格式把「整支條件都成立」拆成基準案例(掛在 __base__<分支>),條件自己只留
    把單一條件弄假的變體。兩種格式都要吃得下,否則測資格式一改,這一步就靜默失效。"""
    conds = [{"id": "C1", "desc": "交易類型為 WITHDRAW", "branch": "B1"}]
    se = _spec_exec(conditions=conds,
                    case_results=[_case("__base__B1", True), _case("C1", False)])
    r = calibrate_with_execution(_report([_mismatch()], se))
    assert r["findings"][0]["severity"] == "info"


def test_只驗了一向不算驗過():
    """只跑「成立」那向,證明不了「不成立時不會誤報」→ 不得據以降級。"""
    se = _spec_exec(case_results=[_case("C1", True)])
    r = calibrate_with_execution(_report([_mismatch()], se))
    assert r["findings"][0]["severity"] == "major"
    assert "執行驗證未覆蓋" in r["findings"][0]["detail"]


def test_條件有覆蓋缺口_維持原判並註明():
    se = _spec_exec(coverage_gaps=["條件 C1 缺 false 向案例"])
    r = calibrate_with_execution(_report([_mismatch()], se))
    f = r["findings"][0]
    assert f["severity"] == "major"
    assert "執行驗證未覆蓋" in f["detail"] and "C1" in f["detail"]
    assert r["_calibration"]["uncovered"]


def test_覆蓋缺口的條件編號逐字比對_C10_不算_C1():
    """C1 和 C10 共用前綴。拿子字串比對會讓 C10 的缺口把 C1 一起當成沒驗到,
    結果是「有驗過的指控照樣維持 major」——方向保守,但判斷是錯的。"""
    se = _spec_exec(coverage_gaps=["條件 C10 缺 true 向案例"])
    r = calibrate_with_execution(_report([_mismatch()], se))
    assert r["findings"][0]["severity"] == "info"


def test_案例被仲裁剔除_不算驗過():
    se = _spec_exec(case_results=[_case("C1", True, dropped=True), _case("C1", False)])
    r = calibrate_with_execution(_report([_mismatch()], se))
    assert r["findings"][0]["severity"] == "major"


def test_案例結果不符預期_不算驗過():
    se = _spec_exec(case_results=[_case("C1", True, ok=False), _case("C1", False)])
    r = calibrate_with_execution(_report([_mismatch()], se))
    assert r["findings"][0]["severity"] == "major"


def test_對不上任何條件_維持原判並註明():
    f = _mismatch(title="實作與核定規格不符", detail="報表的欄位順序與規格附錄不同")
    r = calibrate_with_execution(_report([f], _spec_exec()))
    assert r["findings"][0]["severity"] == "major"
    assert "無法對應到執行驗證的任何條件" in r["findings"][0]["detail"]


def test_只命中一個泛用字眼不算對上():
    """條件描述有兩個以上有辨識度的字串時,要求至少命中兩個。只憑一個(例如同時
    出現在好幾條條件裡的字眼)就認定對上,會把不相干的指控降級。"""
    conds = [{"id": "C1", "desc": "七日內累計金額達 500000 以上"}]
    se = _spec_exec(conditions=conds,
                    case_results=[_case("C1", True), _case("C1", False)])
    r = calibrate_with_execution(_report([_mismatch(detail="累計區間的起算日沒有說明")], se))
    assert r["findings"][0]["severity"] == "major"


def test_非規格不符類的_finding_不碰():
    f = {"file": "a.sql", "line": 1, "severity": "major", "title": "[R001] DELETE 沒有 WHERE",
         "detail": "全表刪除", "suggestion": "", "citations": []}
    r = calibrate_with_execution(_report([f], _spec_exec()))
    assert r["findings"][0] == f


def test_程式補的執行驗證_finding_不被自己降級():
    """沙盒跑出來的「行為不符」是證據,不是待對照的指控。拿證據去對照證據,會讓
    唯一真的跑過的那條 finding 被自己的案例結果降成 info。"""
    f = {"file": "a.sql", "line": 0, "severity": "major",
         "title": "執行驗證失敗:實作與規格行為不符",
         "detail": "規則 R-201 在依規格生成的測資上行為不符:交易類型為 WITHDRAW 的案例",
         "suggestion": "", "citations": []}
    r = calibrate_with_execution(_report([f], _spec_exec()))
    assert r["findings"][0]["severity"] == "major"
    assert "執行驗證未重現" not in r["findings"][0]["detail"]


def test_無規格時不降級_而是記下要人看():
    """「沒找到規格」的意思是無法確認意圖,不是問題變小。"""
    r = calibrate_with_execution(_report([_mismatch()], {"passed": False, "no_spec": True,
                                                         "conditions": [], "case_results": []}))
    assert r["findings"][0]["severity"] == "major"
    assert any("無規格可對照" in x for x in r["_needs_human"])


@pytest.mark.parametrize("se", [
    None,                                              # 執行驗證整段沒跑(逾時/例外)
    {"passed": False, "conditions": [], "case_results": []},
    {"passed": False, "conditions": CONDS, "case_results": []},   # 測資建不起來
])
def test_執行驗證沒跑成就整步跳過(se):
    r = calibrate_with_execution(_report([_mismatch()], se))
    assert r["findings"][0]["severity"] == "major"
    assert "執行驗證" not in r["findings"][0]["detail"]
    assert "_calibration" not in r


# ---- 2. 規格「待補 / 未載明」 ----

SPEC_PENDING = """# R-201 大額提款通報

## 判定條件
- 交易類型為 WITHDRAW
- 七日內累計金額達 500000 以上

## 幣別處理(待補)
- 外幣交易的折算基準由業務單位另行提供。
"""


def test_待補段落的指控改成提問並要人看():
    f = _mismatch(title="實作與核定規格不符:幣別未折算",
                  detail="程式直接把外幣金額與門檻相比,未依幣別折算")
    r = enforce_spec_pending(_report([f]), SPEC_PENDING)
    out = r["findings"][0]
    assert out["severity"] == "info"
    assert out["title"] == "規格未載明,請業務單位確認"
    assert "不符" not in out["title"]
    assert any("規格未載明" in x for x in r["_needs_human"])


def test_待補的標題底下的細項也算同一件事():
    """「## 幣別處理(待補)」的標記在標題上,細項寫在底下。只看標記那一行會漏掉
    細項講的「折算基準」,那正是指控會用的字眼。"""
    f = _mismatch(title="實作與核定規格不符", detail="折算基準與規格不一致")
    r = enforce_spec_pending(_report([f]), SPEC_PENDING)
    assert r["findings"][0]["severity"] == "info"


def test_規格沒有待補段落就不動():
    spec = "# R-201\n## 判定條件\n- 交易類型為 WITHDRAW\n"
    f = _mismatch()
    r = enforce_spec_pending(_report([f]), spec)
    assert r["findings"][0] == f
    assert "_needs_human" not in r


def test_待補段落沒對應到的指控不受影響():
    f = _mismatch(detail="缺少 WITHDRAW 的過濾條件")
    r = enforce_spec_pending(_report([f]), SPEC_PENDING)
    assert r["findings"][0]["severity"] == "major"


def test_非規格不符類的_finding_要命中兩個字眼才壓():
    """不是在指控違規的 finding(例如效能提問),只憑一個字眼就壓成 info,等於用
    「規格有別的地方待補」去蓋掉一條不相干的 finding。"""
    one = {"file": "a.sql", "line": 1, "severity": "major", "title": "外幣金額欄位型別過窄",
           "detail": "decimal(10,0) 會截斷小數", "suggestion": "", "citations": []}
    r = enforce_spec_pending(_report([one]), SPEC_PENDING)
    assert r["findings"][0]["severity"] == "major"
    two = {**one, "detail": "折算基準未給,且外幣交易的小數會被截斷"}
    r2 = enforce_spec_pending(_report([two]), SPEC_PENDING)
    assert r2["findings"][0]["severity"] == "info"


def test_沒有規格時不做這一步():
    f = _mismatch()
    for spec in (None, ""):
        assert enforce_spec_pending(_report([f.copy()]), spec)["findings"][0]["severity"] == "major"


# ---- 3. 斷言要有依據 ----

JOIN_SQL = """SELECT a.acct_no, SUM(b.amount) AS total
FROM accounts AS a
LEFT JOIN tx AS b ON b.acct_no = a.acct_no
WHERE b.tx_type = 'WITHDRAW'
GROUP BY a.acct_no;"""


def _mr(sql=JOIN_SQL, path="sql/rules/r201.sql", **over):
    f = {"path": path, "diff": "@@ -1 +1 @@\n+" + sql.replace("\n", "\n+"),
         "full_content": sql}
    f.update(over)
    return {"files": [f]}


def _finding(title, detail, severity="major", file="sql/rules/r201.sql"):
    return {"file": file, "line": 1, "severity": severity, "title": title,
            "detail": detail, "suggestion": "", "citations": []}


def test_自述等價的_finding_最多_minor():
    f = _finding("過濾條件移進條件聚合", "改寫後最終結果相同,但可讀性下降")
    r = enforce_claim_evidence(_report([f]), _mr())
    assert r["findings"][0]["severity"] == "minor"
    assert r["_calibration"]["capped"][0]["by"] == "self-equivalent"


def test_語法斷言_解析得過就不能報():
    """「CTE 裡定義的欄位不可在外層 WHERE 使用」在 T-SQL 是可以的。解析得過代表
    這個斷言被確定性否證 → 移除,並留在 _calibration 可追。"""
    sql = ("WITH t AS (SELECT acct_no, amount * 2 AS doubled FROM tx)\n"
           "SELECT acct_no FROM t WHERE doubled > 100;")
    f = _finding("別名不可在 WHERE 使用", "doubled 是 CTE 的別名,無法執行", severity="blocker")
    r = enforce_claim_evidence(_report([f]), _mr(sql))
    assert r["findings"] == []
    assert r["_calibration"]["dropped"][0]["by"] == "syntax-claim-verified-ok"


def test_語法斷言_驗不了的壓到_minor():
    """解析不過代表「我們驗不了這個斷言」。真的語法錯誤由預掃的 enforce_parse 與
    沙盒的執行驗證確定性補報,不靠模型這條撐著。"""
    f = _finding("語法錯誤", "這段 SQL 無法執行")
    r = enforce_claim_evidence(_report([f]), _mr("SELEKT ( FROM WHERE"))
    assert r["findings"][0]["severity"] == "minor"
    assert r["_calibration"]["capped"][0]["by"] == "syntax-claim-unverified"


def test_fanout_指名_JOIN_鍵並說明會進輸出_維持原判():
    f = _finding("LEFT JOIN 造成列數膨脹",
                 "tx 的 acct_no 不唯一,多出來的列會進到輸出的 total")
    r = enforce_claim_evidence(_report([f]), _mr())
    assert r["findings"][0]["severity"] == "major"


def test_fanout_沒指名_JOIN_鍵_壓到_minor():
    f = _finding("LEFT JOIN 造成列數膨脹", "join 可能讓列數變多,建議改寫")
    r = enforce_claim_evidence(_report([f]), _mr())
    assert r["findings"][0]["severity"] == "minor"
    assert r["_calibration"]["capped"][0]["by"] == "fanout-unsubstantiated"


def test_fanout_指名了鍵但沒說會進輸出_壓到_minor():
    """反連接改寫的誤報就長這樣:鍵講對了,但被排除的列根本不會進輸出。"""
    f = _finding("LEFT JOIN 造成列數膨脹", "acct_no 在 tx 不唯一")
    r = enforce_claim_evidence(_report([f]), _mr())
    assert r["findings"][0]["severity"] == "minor"


def test_fanout_而_SQL_根本沒有_JOIN_壓到_minor():
    f = _finding("列數膨脹", "acct_no 不唯一會讓輸出的列數增加")
    r = enforce_claim_evidence(_report([f]), _mr("SELECT acct_no FROM tx;"))
    assert r["findings"][0]["severity"] == "minor"


def test_沒有_full_content_時從_diff_還原_SQL():
    """真實 GitLab 模式沒有 full_content。還原不到 SQL 就驗不了語法斷言,
    結果會變成「驗證得過」或「沒有 JOIN 鍵」,兩邊都判錯。"""
    sql = ("WITH t AS (SELECT acct_no, amount * 2 AS doubled FROM tx)\n"
           "SELECT acct_no FROM t WHERE doubled > 100;")
    mr = {"files": [{"path": "sql/rules/r201.sql",
                     "diff": "@@ -0,0 +1,2 @@\n" + "\n".join("+" + ln for ln in sql.splitlines()),
                     "full_content": None}]}
    f = _finding("別名不可在 WHERE 使用", "doubled 無法執行", severity="blocker")
    r = enforce_claim_evidence(_report([f]), mr)
    assert r["findings"] == []


def test_程式補的執行驗證_finding_不被壓():
    f = _finding("執行驗證失敗:實作與規格行為不符",
                 "SQL 無法在測資上執行:語法錯誤 near 'FORM'")
    r = enforce_claim_evidence(_report([f]), _mr())
    assert r["findings"][0]["severity"] == "major"


def test_一般的正確性_finding_不受影響():
    """三道都不該碰的對照組:沒有自述等價、沒有語法斷言、沒有 fan-out。"""
    f = _finding("比較運算子與規格不一致", "規格寫「達」應為 >=,實作寫 >")
    r = enforce_claim_evidence(_report([f]), _mr())
    assert r["findings"][0] == f


# ---- 4. 驗收情境:三種等價重構跑完一輪後不得有 major、決策不是 blocked ----

POLICY = {"auto_approve": {"max_diff_lines": 30, "min_score": 95,
                           "allowed_severities": ["info"], "forbid_pending_hints": True},
          "block": {"min_blockers": 1}}


def test_三種等價重構_不出現_major_決策不是_blocked():
    cte = ("WITH t AS (SELECT acct_no, amount * 2 AS doubled FROM tx)\n"
           "SELECT acct_no FROM t WHERE doubled > 100;")
    findings = [
        _finding("LEFT JOIN 造成列數膨脹", "join 讓列數變多"),
        _finding("過濾條件移進條件聚合", "最終結果相同,但不易讀"),
        _finding("別名不可在 WHERE 使用", "doubled 無法執行", severity="blocker"),
    ]
    mr = {"files": [{"path": "sql/rules/r201.sql", "diff": "@@ -1 +1 @@\n+SELECT 1;",
                     "full_content": cte}]}
    r = apply_policy(apply_rubric(enforce_claim_evidence(_report(findings), mr)), mr, POLICY)
    assert not [f for f in r["findings"] if f["severity"] in ("major", "blocker")]
    assert r["decision"] != "blocked"


def test_條件沒標分支時_所有基準案例都要通過():
    """新格式的基準案例掛在 `__base__<分支>` 下。條件沒標分支(或標成全域 `*`)時要把
    所有基準案例都算進來:只認同名分支的話,沒標分支的條件永遠對不到任何基準案例,
    變成「只看變體案例就算驗過」——那是放寬,方向錯了。"""
    for branch in (None, "*"):
        conds = [{"id": "C1", "desc": "交易類型為 WITHDRAW", "branch": branch}]
        se = _spec_exec(conditions=conds,
                        case_results=[_case("__base__B1", True, ok=False),
                                      _case("C1", False)])
        r = calibrate_with_execution(_report([_mismatch()], se))
        assert r["findings"][0]["severity"] == "major", branch

    # 自己兩向都過了,但分支的基準案例沒過 → 這個條件在「整支成立」的情境下沒驗過,
    # 一樣不算驗過。只認同名分支的話,沒標分支的條件會漏掉這一半。
    for branch in (None, "*"):
        conds = [{"id": "C1", "desc": "交易類型為 WITHDRAW", "branch": branch}]
        se = _spec_exec(conditions=conds,
                        case_results=[_case("C1", True), _case("C1", False),
                                      _case("__base__B1", True, ok=False)])
        r = calibrate_with_execution(_report([_mismatch()], se))
        assert r["findings"][0]["severity"] == "major", branch


# ---- 5. 程式自己補的 finding 是證據,三道都不得碰它 ----

PROGRAM_FINDINGS = [
    # 預掃解析失敗:內文本來就寫「無法解析」,正好命中語法斷言的樣式
    _finding(_PARSE_FAIL_TITLE, "sqlglot 無法解析此檔,rule-base 規則未執行"),
    _finding(_RENDER_FAIL_TITLE, "樣板展開失敗,規則掃的是未展開的原文"),
    _finding(_DBT_NOTICE_TITLE, "展開後的表名未驗證"),
    _finding(_FORMAT_TITLE, "模型輸出的 finding 不是物件"),
    _finding(_TOOL_REFUSAL_TITLE, "模型要求呼叫未開放的寫入工具"),
    _finding(_UNREVIEWABLE_TITLE, "diff 過大,內容無法取得,未經審查"),
    _finding("[R001] DELETE 沒有 WHERE", "全表刪除;這段 SQL 無法執行會造成列數膨脹"),
    _finding("執行驗證失敗:實作與規格行為不符",
             "外幣交易的折算基準與核定規格不符:門檻不一致,但結果相同"),
]


@pytest.mark.parametrize("f", PROGRAM_FINDINGS, ids=lambda f: f["title"][:18])
def test_程式補的_finding_三道都不碰(f):
    """程式補的 finding 是確定性證據,不是待對照的指控。這些內文刻意寫得會命中
    三道的樣式(「無法解析」「無法執行」「結果相同」「與核定規格不符」):壓到它們
    等於把唯一真的驗過的那條結論弄掉。"""
    before = dict(f)
    r = _report([f], _spec_exec())
    r = enforce_claim_evidence(enforce_spec_pending(
        calibrate_with_execution(r), SPEC_PENDING), _mr())
    assert r["findings"] == [before]
