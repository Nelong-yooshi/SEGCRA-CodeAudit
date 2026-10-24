"""判斷校準的確定性測試 — 不呼叫 LLM、不連資料庫。

**方向(#39 review 定的)**:模型照實報,後處理只能依「執行出來的證據」調整。
Code review 是上正式環境前最後一道,漏抓的代價(帶著問題進正式環境、要到稽核才發現)
比誤報(有人花幾分鐘確認)高得多。所以這三道後處理:

  * **不刪 finding、不降嚴重度**,除非有執行出來的證據推翻它;
  * 對不上證據的**加註並交人工**(`_needs_human`);
  * **只碰模型寫的指控**(程式補的那些本身就是證據)。

上一版用「用字」判斷指控有沒有依據,重放 441 份真實報告時三個樣式全部誤中
(通過數 371 → 108、少抓 292 個植入缺陷,乾淨報告一份也沒變乾淨)。所以這個檔案裡
**有一半的測試是在證明「不該動的沒有被動」** —— 那些例子直接取自那次重放。
"""
import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from orchestrator import pipeline
from orchestrator.pipeline import (apply_policy, apply_rubric,
                                   calibrate_with_execution, enforce_claim_evidence,
                                   enforce_spec_pending)

SPEC_DIR = pathlib.Path(__file__).resolve().parents[1] / "specs"
CONDS = [{"id": "C1", "desc": "交易類型為 WITHDRAW"},
         {"id": "C2", "desc": "七日內累計金額達 500000 以上"}]
EXECUTED = "sql/rules/r201.sql"


def _case(cid, expect, ok=True, **over):
    r = {"case_id": f"{cid}-{expect}", "condition_id": cid, "expect_flagged": expect,
         "actual_flagged": expect if ok else not expect, "ok": ok}
    r.update(over)
    return r


def _spec_exec(**over):
    """C1、C2 都兩向驗過且全部符合預期,執行的是 EXECUTED 那個檔。"""
    base = {"passed": True, "spec_code": "R-201", "conditions": CONDS,
            "executed_file": EXECUTED, "coverage_gaps": [], "dropped_cases": [],
            "case_results": [_case("C1", True), _case("C1", False),
                             _case("C2", True), _case("C2", False)]}
    base.update(over)
    return base


def _claim(detail="缺少 WITHDRAW 的過濾", severity="major", title="實作與核定規格不符",
           file=EXECUTED, **over):
    f = {"file": file, "line": 10, "severity": severity, "title": title,
         "detail": detail, "suggestion": "", "citations": []}
    f.update(over)
    return f


def _report(findings, spec_exec=None):
    r = {"findings": findings}
    if spec_exec is not None:
        r["_spec_exec"] = spec_exec
    return r


# ─────────── 1. 執行驗證對照模型的指控:加註 + 交人工,不降級 ───────────

def test_兩向都通過時加註並交人工_但不降級():
    """「兩向案例都通過」證明的是「生成的那幾筆資料上沒有差異」,不是「模型講的情境
    沒問題」。重放實測:降級讓 8 份報告從 needs_human 變成 auto_approved,其中 5 份是
    真的有錯(指控對,但生成的案例沒落在被改動的邊界上)。"""
    r = calibrate_with_execution(_report([_claim()], _spec_exec()))
    f = r["findings"][0]
    assert f["severity"] == "major", "不得降級"
    assert "執行驗證未重現" in f["detail"] and "C1" in f["detail"]
    assert any("未重現" in x for x in r["_needs_human"])
    assert r["_calibration"]["annotated"]


def test_對不上條件時註明未覆蓋並交人工():
    f = _claim(detail="報表的欄位順序與規格附錄不同")
    r = calibrate_with_execution(_report([f], _spec_exec()))
    assert r["findings"][0]["severity"] == "major"
    assert "執行驗證未覆蓋" in r["findings"][0]["detail"]
    assert r["_needs_human"]


def test_有覆蓋缺口時註明未覆蓋():
    se = _spec_exec(coverage_gaps=["條件 C1 缺 false 向案例"])
    r = calibrate_with_execution(_report([_claim()], se))
    assert r["findings"][0]["severity"] == "major"
    assert "執行驗證未覆蓋" in r["findings"][0]["detail"]


def test_只對實際執行的那個檔案加註():
    """`run_spec_exec` 只跑 MR 裡最後一個有內容的 SQL 檔。別的檔案根本沒被執行,
    拿這份結果說它「未重現」是錯的(#39 review 第 2 點)。"""
    other = _claim(file="sql/rules/r999.sql")
    r = calibrate_with_execution(_report([other], _spec_exec()))
    assert r["findings"][0]["detail"] == "缺少 WITHDRAW 的過濾", "沒執行的檔不得加註"
    assert "_needs_human" not in r


def test_沒記執行檔案時對所有檔都加註():
    """舊的結果(或測試用的替身)沒有 executed_file → 退回原本的行為,不要整步失效。"""
    se = _spec_exec()
    del se["executed_file"]
    r = calibrate_with_execution(_report([_claim(file="sql/rules/r999.sql")], se))
    assert "執行驗證" in r["findings"][0]["detail"]


def test_程式補的finding不碰():
    """判斷靠 `_source` 欄位,不是標題:注入掃描那條沒有固定前綴,而模型寫成
    「[R-140] 實作與核定規格不符」反而會被誤認成程式補的(#39 review 第 4 點)。"""
    program = _claim(title="執行驗證失敗:實作與規格行為不符",
                     detail="規則 R-201 在依規格生成的測資上行為不符",
                     _source=pipeline.PROGRAM_SOURCE)
    model_with_bracket = _claim(title="[R-140] 實作與核定規格不符")
    r = calibrate_with_execution(_report([program, model_with_bracket], _spec_exec()))
    assert "執行驗證未重現" not in r["findings"][0]["detail"], "程式補的是證據,不是指控"
    assert "執行驗證未重現" in r["findings"][1]["detail"], "模型寫的要對照,不管標題長怎樣"


def test_無規格時記一筆要人看():
    r = calibrate_with_execution(_report([_claim()], {"passed": False, "no_spec": True,
                                                      "conditions": [], "case_results": []}))
    assert r["findings"][0]["severity"] == "major"
    assert any("無規格可對照" in x for x in r["_needs_human"])


@pytest.mark.parametrize("se", [
    None,
    {"passed": False, "conditions": [], "case_results": []},
    {"passed": False, "conditions": CONDS, "case_results": []},
])
def test_執行驗證沒跑成就整步跳過(se):
    r = calibrate_with_execution(_report([_claim()], se))
    assert r["findings"][0]["detail"] == "缺少 WITHDRAW 的過濾"
    assert "_calibration" not in r


# ─────────── 2. 規格「待補 / 未載明」:加註 + 交人工,不降級 ───────────

R140 = (SPEC_DIR / "R-140.md").read_text(encoding="utf-8")


def test_真規格加真門檻錯誤_維持major不加註():
    """#39 review 的重現案例。團隊的規格範本每份都有「## 待補(核定規格未載明,
    不從程式碼腦補)」這個標題,而 system prompt 規定不符的標題要寫「實作與核定規格
    不符」—— 上一版把標題那一行也當成待補內容,切出的「核定」「定規」兩邊重疊就過了
    門檻,於是幾乎每條規格不符都被改成 info(重放:336 條 major、37 條 blocker)。"""
    f = _claim(detail="規格寫達 50 萬(含),實作用 >,剛好 50 萬的交易會漏報")
    r = enforce_spec_pending(_report([f]), R140)
    assert r["findings"][0]["severity"] == "major"
    assert "待補" not in r["findings"][0]["detail"]
    assert "_needs_human" not in r


def test_真的命中待補項目_加註並交人工():
    """R-140 的待補段列的是「幣別」「cash_flag 的來源」「同一帳戶跨分行」三項。"""
    f = _claim(title="實作與核定規格不符:幣別未折算",
               detail="外幣交易直接把金額與門檻相比,未依幣別折算")
    r = enforce_spec_pending(_report([f]), R140)
    out = r["findings"][0]
    assert out["severity"] == "major", "規格沒寫不會讓影響變小"
    assert "待補 / 未載明" in out["detail"] and "幣別" in out["detail"]
    assert any("規格未載明:幣別" in x for x in r["_needs_human"])
    assert r["_calibration"]["annotated"][0]["by"] == "spec-pending"


def test_待補項目逐項比對_不是整段字詞的聯集():
    """R-140 的待補段三項加起來有幾十個字詞,聯集比對的話任何提到其中兩個的 finding
    都會命中。逐項比對才能說「這條指控講的就是那一項待補的事」。"""
    # 「分行」只出現在第三項、「來源」只在第二項:各自命中自己那一項
    f1 = _claim(detail="同一帳戶跨分行的交易被合併計算")
    assert "同一帳戶跨分行" in enforce_spec_pending(
        _report([f1]), R140)["findings"][0]["detail"]
    # 同時提到兩項各一個字詞、但都不足以構成任一項 → 不命中
    f2 = _claim(detail="資料表無此欄位,且判定時機不明")
    assert "待補" not in enforce_spec_pending(_report([f2]), R140)["findings"][0]["detail"]


@pytest.mark.parametrize("detail", [
    # 項目名是「`cash_flag` 的來源」,這條講的是「有沒有過濾」——同一個欄位、不同的事
    "條件 2 沒有過濾 cash_flag = 'Y',現金註記未被排除",
    # 項目名是「同一帳戶跨分行」,這條講的是「同一天多筆只取一筆」
    "同一帳戶當日多筆只取一筆,應逐筆通報",
])
def test_只共用部分字詞不算講同一件事(detail):
    """上一版比對項目名切出來的字詞:一個英數詞或兩個中文詞就算對上,於是這兩條
    真的缺陷都被加註成「規格把它列在待補」。那段話會貼到 MR 上,看的人很可能把
    真缺陷當成規格的問題放過去(#55 review 第二輪)。"""
    r = enforce_spec_pending(_report([_claim(detail=detail)]), R140)
    assert "待補" not in r["findings"][0]["detail"], r["findings"][0]["detail"]
    assert "_needs_human" not in r


@pytest.mark.parametrize("detail", [
    "外幣交易未依幣別折算",                       # 項目名「幣別」
    "`cash_flag` 的來源不明,無法確認由誰寫入",     # 反引號與粗體都要抹掉才對得上
    "同一帳戶跨分行的交易被合併計算",
])
def test_整個項目名出現就算講同一件事(detail):
    r = enforce_spec_pending(_report([_claim(detail=detail)]), R140)
    assert "待補 / 未載明" in r["findings"][0]["detail"], detail
    assert r["_needs_human"]


def test_項目名分段比對_每一段都要出現():
    """R-410 的項目名是「金額欄位的幣別/單位」,模型寫的是「金額欄位未限制幣別」——
    講的就是那一項,但不可能照抄帶斜線的完整字串。按「的」拆段、每段都要出現,
    段內用「/」列出的替代寫法命中一個就夠。"""
    spec = ("# R-998\n## 待補(核定規格未載明)\n"
            "- **金額欄位的幣別/單位**:規格未載是否需限制單一幣別。\n")
    hit = enforce_spec_pending(
        _report([_claim(detail="金額欄位未限制幣別,不同幣別直接相加")]), spec)
    assert "待補 / 未載明" in hit["findings"][0]["detail"]
    # 只提到其中一段(幣別)不算:那一項講的是「金額的幣別」這件事
    miss = enforce_spec_pending(
        _report([_claim(detail="通報筆數的幣別標示方式與規格不符")]), spec)
    assert "待補" not in miss["findings"][0]["detail"], miss["findings"][0]["detail"]


def test_反引號與粗體不影響比對():
    """規格寫「**`cash_flag` 的來源**」,模型寫的是「cash_flag 的來源不明」——
    反引號與粗體是規格的排版,不抹掉就對不上。"""
    r = enforce_spec_pending(
        _report([_claim(detail="cash_flag 的來源不明,無法確認由誰寫入")]), R140)
    assert "待補 / 未載明" in r["findings"][0]["detail"]
    assert any("cash_flag" in x for x in r["_needs_human"])


def test_泛用的後綴不參與比對():
    """規格寫「金額**欄位**的幣別」,模型寫的是「金額的幣別」——「欄位」是規格的
    行文習慣,不是指認主題的字。"""
    spec = ("# R-997\n## 待補(核定規格未載明)\n"
            "- **金額欄位的幣別**:規格未載是否需限制單一幣別。\n")
    r = enforce_spec_pending(
        _report([_claim(detail="金額的幣別程式自己決定了一種處理方式")]), spec)
    assert "待補 / 未載明" in r["findings"][0]["detail"]


def test_太泛的項目名不比對():
    """「條件」「欄位」這種項目名幾乎出現在每一條規格不符的指控裡,一比就中。"""
    spec = ("# R-998\n## 待補(核定規格未載明)\n"
            "- **條件**:未載明。\n- **幣別**:未載明。\n")
    r = enforce_spec_pending(
        _report([_claim(detail="條件 2 的門檻與規格不符")]), spec)
    assert "待補" not in r["findings"][0]["detail"], r["findings"][0]["detail"]


def test_加註的說法是規格沒有寫這件事的要求():
    """原本寫「這件事沒有可對照的核定要求」,看的人會以為是程式缺了對照的東西。"""
    r = enforce_spec_pending(_report([_claim(detail="外幣交易未依幣別折算")]), R140)
    assert "規格沒有寫這件事的要求" in r["findings"][0]["detail"]


@pytest.mark.parametrize("title,detail", [
    ("疑似審查規避指令", "MR 描述要求直接通過審查;另外幣別也沒處理"),
    ("疑似提示注入攻擊(確定性掃描命中)", "MR 文字含操縱審查器的指令"),
])
def test_安全類的finding一律不碰(title, detail):
    """規格待補跟「有人想操縱審查器」是兩件無關的事。重放裡連「疑似審查規避指令」
    都被改成 info。"""
    r = enforce_spec_pending(_report([_claim(title=title, detail=detail,
                                             severity="blocker")]), R140)
    assert r["findings"][0]["severity"] == "blocker"
    assert "待補" not in r["findings"][0]["detail"]


def test_規格沒有待補段落就不動():
    spec = "# R-999\n## 判定條件\n- 交易類型為 WITHDRAW\n"
    f = _claim()
    r = enforce_spec_pending(_report([f]), spec)
    assert r["findings"][0] == f and "_needs_human" not in r


def test_沒有規格時不做這一步():
    for spec in (None, ""):
        f = _claim()
        assert enforce_spec_pending(_report([f]), spec)["findings"][0] == f


def test_repo_裡每份有待補段的規格都抽得出項目():
    """團隊範本的待補段標題固定是「## 待補(核定規格未載明,不從程式碼腦補)」,
    抽不出項目的話這一道等於沒作用,而且不會有任何徵兆。"""
    found = {}
    for path in sorted(SPEC_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if "待補" in text:
            found[path.name] = pipeline._pending_items(text)
    assert found, "repo 裡應該有帶待補段的規格"
    for name, items in found.items():
        assert items, f"{name} 有待補段卻抽不出任何項目"


# ─────────── 3. 只有沙盒實際執行成功才動嚴重度 ───────────

def _ran_ok(**over):
    base = {"executed_file": EXECUTED, "sql_error": None,
            "case_results": [_case("C1", True)]}
    base.update(over)
    return base


def test_沙盒跑成功_無法執行的blocker降到major():
    f = _claim(title="語法錯誤:別名不可在 WHERE 使用",
               detail="doubled 是 CTE 的別名,這段 SQL 無法執行", severity="blocker")
    r = enforce_claim_evidence(_report([f], _ran_ok()))
    out = r["findings"][0]
    assert out["severity"] == "major", "降一級,不是移除"
    assert "沙盒已實際執行成功" in out["detail"]
    assert r["_calibration"]["downgraded"][0]["by"] == "sandbox-ran-ok"
    assert r["_needs_human"], "跑得動不等於算得對,仍要交人工"


def test_沙盒跑成功但指控不是無法執行_不動():
    f = _claim(title="門檻寫錯", detail="規格寫 >=,實作寫 >", severity="blocker")
    r = enforce_claim_evidence(_report([f], _ran_ok()))
    assert r["findings"][0]["severity"] == "blocker"


def test_沒跑成功就不動():
    """沙盒不可用、SQL 真的跑不起來、或根本沒執行 → 沒有證據,不得調整。"""
    f = _claim(title="語法錯誤", detail="無法執行", severity="blocker")
    for se in (None, {}, _ran_ok(sql_error="Incorrect syntax near 'FORM'"),
               _ran_ok(case_results=[]), _ran_ok(executed_file="")):
        r = enforce_claim_evidence(_report([dict(f)], se))
        assert r["findings"][0]["severity"] == "blocker", se


def test_不是執行的那個檔不動():
    f = _claim(title="語法錯誤", detail="無法執行", severity="blocker",
               file="sql/rules/r999.sql")
    r = enforce_claim_evidence(_report([f], _ran_ok()))
    assert r["findings"][0]["severity"] == "blocker"


def test_major以下的無法執行指控不動():
    """只處理 blocker:它會直接擋下 MR,所以「被執行否證」要反映在決策上。
    major 本來就是交人工,沒有調整的必要。"""
    f = _claim(title="語法錯誤", detail="無法執行", severity="major")
    r = enforce_claim_evidence(_report([f], _ran_ok()))
    assert r["findings"][0]["severity"] == "major"
    assert "沙盒已實際執行成功" not in r["findings"][0]["detail"]


# ── 以下是上一版用「用字」判斷時會誤中的例子,全部取自 441 份報告的重放 ──

@pytest.mark.parametrize("detail", [
    "改寫後與原本不等價:NULL 時結果不同",
    "看似等價,但金額為 NULL 時會漏報",
    "新舊結果不完全相同,邊界 300 會漏報",
    "DISTINCT 只能去除完全相同的重複行,無法修正膨脹",
    "MR 描述宣稱「等價重構」,但 NULL 時結果不同",
])
def test_自述等價的樣式不再被壓(detail):
    """否定、讓步、引述都會中到「等價」這個樣式。最後一句尤其糟:只要 MR 作者在描述
    寫「等價重構」,模型引述它就能把自己的反駁壓掉。被壓的 10 條有 8 條在有缺陷的
    題目上。"""
    f = _claim(title="改寫改變了行為", detail=detail)
    r = enforce_claim_evidence(_report([f], _ran_ok()))
    assert r["findings"][0]["severity"] == "major"
    assert r["findings"][0]["detail"] == detail, "不得加註也不得改寫"


@pytest.mark.parametrize("sql", [
    "SELECT amount * 2 AS amt2 FROM txn WHERE amt2 > 100",     # 同一層的別名用在 WHERE
    "SELECT acct FROM txn WHERE SUM(amount) > 100 GROUP BY acct",   # WHERE 裡用聚合
    "SELECT no_such_col FROM txn",                             # 欄位不存在
])
def test_解析得過不代表跑得動(sql):
    """這三段 sqlglot 都解析得過,但 SQL Server 跑不動。上一版「解析得過就整條刪掉」
    會把真的語法問題刪掉——而現在的判準是「沙盒有沒有真的跑完」,所以這三段只要沒進
    沙盒就不會被動到。"""
    f = _claim(title="語法錯誤", detail=f"這段無法執行:{sql}", severity="blocker")
    r = enforce_claim_evidence(_report([f], None))
    assert r["findings"][0]["severity"] == "blocker"


def test_規範類的不得在某處使用不再被刪():
    """「不得在…使用」這個樣式會中到規範類的 finding。"""
    f = _claim(title="不得在正式報表使用 SELECT *",
               detail="欄位變動時報表會跟著變,團隊慣例禁止", severity="major")
    r = enforce_claim_evidence(_report([f], _ran_ok()))
    assert r["findings"][0]["severity"] == "major"
    assert "沙盒" not in r["findings"][0]["detail"]


def test_fanout指控沒指名鍵也不再被壓():
    """被壓的 14 條有 9 條在有缺陷的題目上,只是模型沒照樣式寫出鍵名。"""
    f = _claim(title="LEFT JOIN 造成列數膨脹", detail="join 可能讓列數變多")
    r = enforce_claim_evidence(_report([f], _ran_ok()))
    assert r["findings"][0]["severity"] == "major"
    assert r["findings"][0]["detail"] == "join 可能讓列數變多"


def test_dbt樣板解析不過也不再被壓():
    """dbt 檔的原始 Jinja 本來就解析不過。那種報告裡已經有程式補的展開失敗 finding
    在講這件事了,不需要再壓模型那條。"""
    f = _claim(title="語法錯誤", detail="{{ ref('x') }} 無法執行", severity="blocker")
    r = enforce_claim_evidence(_report([f], None))
    assert r["findings"][0]["severity"] == "blocker"


# ─────────── 4. #39 的完成條件(照 review 調整後) ───────────

POLICY = {"auto_approve": {"max_diff_lines": 30, "min_score": 95,
                           "allowed_severities": ["info"], "forbid_pending_hints": True},
          "block": {"min_blockers": 1}}
MR = {"files": [{"path": EXECUTED, "diff": "@@ -1 +1 @@\n+SELECT 1;",
                 "full_content": "SELECT 1;"}]}


def test_完成條件_三種等價重構的決策不是blocked():
    """review 把這條從「沒有 major」改成「決策不是 blocked」:多報一條 major 只是
    多一個人看,而擋下才是真的擾民。唯一降級的那條靠沙盒跑成功的證據。"""
    findings = [
        _claim(title="LEFT JOIN 造成列數膨脹", detail="join 讓列數變多"),
        _claim(title="過濾條件移進條件聚合", detail="最終結果相同,但不易讀"),
        _claim(title="別名不可在 WHERE 使用", detail="doubled 無法執行",
               severity="blocker"),
    ]
    r = enforce_claim_evidence(_report(findings, _ran_ok()))
    r = apply_policy(apply_rubric(r), MR, POLICY)
    assert not [f for f in r["findings"] if f["severity"] == "blocker"]
    assert r["decision"] == "needs_human"


def test_完成條件_加註不會讓任何東西自動放行():
    """三道加起來也不能放行:加註的同時一定記 _needs_human。"""
    r = _report([_claim()], _spec_exec())
    r = calibrate_with_execution(r)
    r = enforce_spec_pending(r, R140)
    r = enforce_claim_evidence(r)
    r = apply_policy(apply_rubric(r), MR, POLICY)
    assert r["decision"] == "needs_human"
    assert r["_policy_signals"]["needs_human"]


# ─────────── 加註的用字要分得出「驗過但沒重現」與「根本沒驗到」 ───────────
#
# 兩者的意思完全不同:前者是「跑過了,那幾筆資料上沒差異」(矛盾,交人工判斷),
# 後者是「這條指控沒有經過執行驗證」(沒有任何證據)。看報告的人要分得出來。

def test_只驗了一向算沒驗到():
    """只跑「成立」那向,證明不了「不成立時不會誤報」。"""
    se = _spec_exec(case_results=[_case("C1", True)])
    f = calibrate_with_execution(_report([_claim()], se))["findings"][0]
    assert "執行驗證未覆蓋" in f["detail"] and "未重現" not in f["detail"]


def test_案例被仲裁剔除算沒驗到():
    se = _spec_exec(case_results=[_case("C1", True, dropped=True), _case("C1", False)])
    f = calibrate_with_execution(_report([_claim()], se))["findings"][0]
    assert "執行驗證未覆蓋" in f["detail"]


def test_案例結果不符預期算沒驗到():
    se = _spec_exec(case_results=[_case("C1", True, ok=False), _case("C1", False)])
    f = calibrate_with_execution(_report([_claim()], se))["findings"][0]
    assert "執行驗證未覆蓋" in f["detail"]


def test_覆蓋缺口的條件編號逐字比對_C10_不算_C1():
    """C1 和 C10 共用前綴。拿子字串比對會讓 C10 的缺口把 C1 一起當成沒驗到,
    於是註解寫成「未覆蓋」——方向保守,但講的事情是錯的。"""
    se = _spec_exec(coverage_gaps=["條件 C10 缺 true 向案例"])
    f = calibrate_with_execution(_report([_claim()], se))["findings"][0]
    assert "執行驗證未重現" in f["detail"], f["detail"]


def test_新格式的基準案例加變體也算驗過():
    """新格式把「整支條件都成立」拆成基準案例(掛在 __base__<分支>),條件自己只留
    把單一條件弄假的變體。兩種格式都要吃得下,否則測資格式一改這一步就靜默失效。"""
    conds = [{"id": "C1", "desc": "交易類型為 WITHDRAW", "branch": "B1"}]
    se = _spec_exec(conditions=conds,
                    case_results=[_case("__base__B1", True), _case("C1", False)])
    f = calibrate_with_execution(_report([_claim()], se))["findings"][0]
    assert "執行驗證未重現" in f["detail"]


def test_沒標分支的條件_所有基準案例都要通過():
    """條件沒標分支(或標成全域 `*`)時取所有基準案例:取多了只會要求更多案例通過,
    不會放寬。"""
    conds = [{"id": "C1", "desc": "交易類型為 WITHDRAW"}]
    # 刻意讓條件自己的兩向都通過、只有基準沒通過:**不取基準的話會看成「驗過了」**
    se = _spec_exec(conditions=conds,
                    case_results=[_case("C1", True), _case("C1", False),
                                  _case("__base__B1", True, ok=False)])
    f = calibrate_with_execution(_report([_claim()], se))["findings"][0]
    assert "執行驗證未覆蓋" in f["detail"], "基準沒通過就不能說「驗過了」"


def test_只命中一個泛用字眼不算對上():
    """條件描述有兩個以上有辨識度的字串時,要求至少命中兩個;只憑一個(例如同時出現
    在好幾條條件裡的字眼)就認定對上,註解會指到錯的條件。"""
    conds = [{"id": "C1", "desc": "七日內累計金額達 500000 以上"}]
    se = _spec_exec(conditions=conds,
                    case_results=[_case("C1", True), _case("C1", False)])
    f = calibrate_with_execution(
        _report([_claim(detail="累計區間的起算日沒有說明")], se))["findings"][0]
    assert "無法對應到執行驗證的任何條件" in f["detail"]


# ─────────── 程式補的 finding 一定要標 _source ───────────

def test_程式補的finding都標了來源():
    """判斷「這條是誰寫的」靠這個欄位。少標一個地方,那條就會被當成模型的指控拿去
    對照——而它其實是確定性證據。"""
    from orchestrator import spec_exec
    # spec_exec 不能 import pipeline(pipeline 匯入它,反向會循環),所以那邊的字串
    # 是寫死的 —— 兩邊要一致
    assert spec_exec._finding("a.sql", "major", "t", "d")["_source"] == "program"
    assert pipeline.PROGRAM_SOURCE == "program"
    inj = pipeline.enforce_injection({"findings": []},
                                     [{"category": "prompt_leak"}])["findings"][0]
    assert inj["_source"] == pipeline.PROGRAM_SOURCE
    pre = [{"path": "a.sql", "rules": [{"rule": "R001", "message": "DELETE 沒有 WHERE",
                                        "severity": "major"}]}]
    assert pipeline.enforce_rules({"findings": []}, pre)["findings"][0]["_source"] \
        == pipeline.PROGRAM_SOURCE


def test_程式補的規格不符類finding不被拿去對照():
    """實際會踩到的例子:檢核點補報的內文含「與核定規格不符」,正好命中指控的樣式。
    拿執行結果去對照它,等於拿證據去對照證據。"""
    program = _claim(title="[H003] 檢核點待人工確認(模型未回應)",
                     detail="與核定規格不符的部分模型沒有回應",
                     _source=pipeline.PROGRAM_SOURCE)
    r = calibrate_with_execution(_report([program], _spec_exec()))
    assert "執行驗證" not in r["findings"][0]["detail"]
    assert "_needs_human" not in r


def test_項目名含泛用字眼時不靠那些字眼命中():
    """待補項目的名稱剛好含「規格」「條件」這類到處都有的字眼時,不可以只靠它們命中
    ——那會讓任何一條規格不符的指控都對上這一項。"""
    # 用只在 _PENDING_SKIP 裡的字眼(核定、定規):_distinct_tokens 自己已經濾掉
    # 「規格」「條件」,所以那兩個測不到這一道。
    # 這正是 review 找到的誤判:system prompt 規定不符的標題要寫「實作與核定規格不符」,
    # 切出來的「核定」「定規」跟規格文件的用語重疊,於是幾乎每條指控都會對上。
    spec = ("# R-999\n\n## 待補(核定規格未載明,不從程式碼腦補)\n\n"
            "- **核定規格的版本**:尚未確認用哪一版。\n")
    f = _claim(detail="門檻寫錯:規格寫 >=,實作寫 >,剛好等於門檻的會漏報")
    r = enforce_spec_pending(_report([f]), spec)
    assert "待補" not in r["findings"][0]["detail"], r["findings"][0]["detail"]
    # 對照:真的在講那一項(用它特有的字眼)就要命中
    f2 = _claim(detail="實作讀的是舊的核定規格的版本,與現行版本對不上")
    assert "待補" in enforce_spec_pending(_report([f2]), spec)["findings"][0]["detail"]


def test_經過_put_program_finding_的一律標上來源():
    """程式專用標題的 finding 都走這個函式(`enforce_parse`、`enforce_format`、
    `enforce_dbt_notice`…,同事在 #33/#44 加的 `enforce_dbt_unexecuted`、
    `enforce_spec_change` 也是)。在這裡標 `_source`,新增的 enforce_* 就不必各自記得
    —— 漏標的後果是那條程式補的 finding 會被校準當成模型的指控去對照。"""
    report = {"findings": []}
    pipeline._put_program_finding(
        report, {"file": "a.sql", "line": 0, "severity": "major", "title": "程式專用標題",
                 "detail": "與核定規格不符", "suggestion": "", "citations": []})
    assert report["findings"][0]["_source"] == pipeline.PROGRAM_SOURCE
    # 而且校準不會碰它
    r = calibrate_with_execution({**report, "_spec_exec": _spec_exec()})
    assert "執行驗證" not in r["findings"][0]["detail"]


def test_條件對照要切中文二字詞():
    """條件描述與模型的措辭幾乎不會逐字相同:「單日累計提領達 100000」vs
    「同一天的提領總額沒有達到門檻」。整串比對的話永遠對不上,於是每一條指控都
    變成「未覆蓋」,加註就失去意義。

    (待補比對改成整段之後,原本順便覆蓋到這裡的測試不見了 —— 這條補回來。)
    """
    from orchestrator.pipeline import _distinct_tokens, _match_conditions
    toks = _distinct_tokens("單日累計提領達 100000")
    assert "提領" in toks and "累計" in toks, "中文要切成二字詞"

    conds = [{"id": "C1", "desc": "單日累計提領達 100000"},
             {"id": "C2", "desc": "通道為自助設備"}]
    got = _match_conditions(conds, "實作與核定規格不符:累計提領的門檻用了嚴格大於")
    assert [c["id"] for c in got] == ["C1"]
