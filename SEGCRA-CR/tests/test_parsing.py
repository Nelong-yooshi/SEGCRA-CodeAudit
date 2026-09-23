"""解析與 SQL 還原的純函式 — 不呼叫 LLM、不連資料庫。

這些函式的共同點:**它們處理的是「不可信的輸入」**(模型輸出、GitLab 的 diff),
而且壞掉的方式是**安靜的**——不會丟例外,只會產出錯的東西:

  extract_json         解析失敗 → 整次審查中斷,或更糟:解出殘缺的報告照樣往下跑
  decode_byte_fallback 沒修好 → 報告裡出現 <0xE3><0x80><0x80> 亂碼
  _sql_from_diff       還原錯 → 預掃與執行驗證看到的 SQL 不是真的那份
  prepare_sql          抽錯 → 執行驗證驗的不是 MR 要驗的邏輯
  arbitrate 的保底     倒錯方向 → 有問題的 SQL 被當成「測資錯」放過去

`_sql_from_diff` 特別值得注意:**所有 golden case 都有 `full_content`,所以評測永遠
不會走到這條路徑——但正式環境的 GitLab real mode 沒有 `full_content`,走的就是它。**
換句話說,正式環境最關鍵的 SQL 還原路徑,評測完全沒有覆蓋。

驗的東西:
  1. `extract_json`:```json 圍欄、前後雜訊、巢狀物件、壞 JSON 靠 json-repair 救回、
     真的救不回時回 None(而不是回半套)。
  2. `decode_byte_fallback`:gemma 的 byte-fallback 亂碼還原,且不動正常文字。
  3. `_sql_from_diff`:只取新增行、去掉 diff 標記。
  4. `prepare_sql`:從 `CREATE VIEW ... AS SELECT` / `INSERT ... SELECT` 抽出 SELECT、
     代入固定執行窗、把系統日期函式換成固定值(可重現的前提)。
  5. `arbitrate` 的兩條保底路徑:LLM 呼叫失敗、輸出無法解析 → 都保守倒向「SQL 錯」。
"""
import asyncio

import pytest

from orchestrator import spec_exec
from orchestrator.agent import decode_byte_fallback, extract_json
from orchestrator.pipeline import _sql_from_diff
from orchestrator.spec_exec import WIN_END, WIN_START, arbitrate, prepare_sql


# ─────────────────── extract_json:模型輸出的生命線 ───────────────────

def test_乾淨的_json():
    assert extract_json('{"score": 90}') == {"score": 90}


def test_markdown_圍欄要去掉():
    """system prompt 明講「不要 markdown 圍欄」,但模型還是會加——不能因此就解析失敗。"""
    for wrapped in ['```json\n{"score": 90}\n```',
                    '```\n{"score": 90}\n```']:
        assert extract_json(wrapped) == {"score": 90}


def test_前後有雜訊也要挑得出來():
    """模型會在 JSON 前後加開場白/結語。"""
    raw = '好的,以下是審查報告:\n{"score": 90}\n希望這對你有幫助!'
    assert extract_json(raw) == {"score": 90}


def test_巢狀物件要完整取出():
    """靠括號配對找邊界,不能取到第一個 } 就停。"""
    raw = '{"findings": [{"severity": "major", "nested": {"a": 1}}], "score": 70}'
    got = extract_json(raw)
    assert got["score"] == 70
    assert got["findings"][0]["nested"] == {"a": 1}


def test_壞掉的_json_靠_json_repair_救回():
    """LLM 結構化輸出的通病:未跳脫引號、缺逗號、截斷。

    寧可修好也不要整份丟掉——丟掉等於這次審查白跑 13-20 分鐘。
    """
    broken = '{"score": 90, "summary": "閾值用了 ">=" 但規格寫超過"}'
    got = extract_json(broken)
    assert got is not None, "json-repair 應該救得回來"
    assert got.get("score") == 90


def test_完全不是_json_回_None():
    """回 None 讓呼叫端丟出明確的錯誤;回空 dict 會讓殘缺報告靜靜往下跑。"""
    assert extract_json("模型今天心情不好,什麼都不想輸出。") is None
    assert extract_json("") is None


def test_取第一個完整物件():
    raw = '{"score": 90}\n{"score": 10}'
    assert extract_json(raw) == {"score": 90}


# ─────────────────── decode_byte_fallback:gemma 的亂碼 ───────────────────

def test_還原_byte_fallback_亂碼():
    """gemma / sentencepiece 偶爾把字元吐成字面的 <0xHH> 序列而不是該字元本身。

    不修的話報告裡會出現 <0xE3><0x80><0x80> 這種東西,直接影響可讀性與驗收觀感。
    """
    assert decode_byte_fallback("金額<0xE3><0x80><0x80>欄位") == "金額　欄位"


def test_沒有亂碼時原樣返回():
    """快速路徑:沒有 `<0x` 就直接返回,不做多餘處理。"""
    text = "一般的中文報告內容,含 SQL:SELECT * FROM t;"
    assert decode_byte_fallback(text) == text


def test_解不開的序列原樣保留():
    """無效的 byte 序列不該讓整份報告變空,保留原文讓人看得到異常。"""
    out = decode_byte_fallback("壞掉的<0xFF><0xFF><0xFF>序列")
    assert "序列" in out and "壞掉的" in out


# ─────────────────── _sql_from_diff:正式環境唯一的還原路徑 ───────────────────

def test_只取新增行():
    """real mode 的 GitLab 沒有 full_content,只有 unified diff。

    取錯的話,預掃與執行驗證看到的 SQL 就不是這個 MR 真正的那份。
    """
    diff = ("@@ -1,3 +1,4 @@\n"
            " SELECT account_id\n"
            "-FROM old_table\n"
            "+FROM transactions\n"
            "+WHERE amount > 100000;")
    assert _sql_from_diff(diff) == "FROM transactions\nWHERE amount > 100000;"


def test_去掉_diff_的標頭():
    """`+++ b/path` 開頭也是 `+`,不能被當成新增的 SQL 行。"""
    diff = "--- a/r.sql\n+++ b/r.sql\n@@ -1 +1 @@\n+SELECT 1;"
    assert _sql_from_diff(diff) == "SELECT 1;"


def test_沒有新增行時回空字串():
    """純刪除的 diff → 沒有 SQL 可驗。回空字串讓上游走「無可執行 SQL」的分支。"""
    assert _sql_from_diff("@@ -1,2 +0,0 @@\n-SELECT 1;\n-SELECT 2;") == ""
    assert _sql_from_diff("") == ""


# ─────────────────── prepare_sql:執行驗證的前置 ───────────────────

def test_從_CREATE_VIEW_抽出_SELECT():
    """VIEW 在 SQL Server 必須是批次第一句,而沙盒要先建表灌資料,所以只取 SELECT。

    `mr_204` 的問題正是在這一段:它的 VIEW 引用了 `@start_date`,作為 VIEW 本身不合法,
    但因為沙盒只取 SELECT 執行,執行驗證測不到那個問題——只有完整的模型審查看得出來。
    """
    sql = ("CREATE OR ALTER VIEW v_detail AS\n"
           "SELECT account_id FROM transactions WHERE tx_type = 'WITHDRAW';")
    out = prepare_sql(sql)
    assert "CREATE" not in out.upper()
    assert "SELECT" in out.upper() and "transactions" in out


def test_從_INSERT_抽出_SELECT():
    """報表表不在沙盒 schema 裡,所以驗的是 SELECT 的篩選邏輯。"""
    sql = "INSERT INTO report_t (account_id)\nSELECT account_id FROM transactions;"
    out = prepare_sql(sql)
    assert "INSERT" not in out.upper()
    assert "transactions" in out


def test_開頭註解不會讓語句類型判斷失準():
    """規則檔開頭幾乎都是註解。先跳過註解再判斷,否則 VIEW / INSERT 會被當成一般 SELECT。"""
    sql = ("-- R-201: 單日提領通報\n"
           "-- 維護人:someone\n"
           "CREATE VIEW v AS SELECT account_id FROM transactions;")
    out = prepare_sql(sql)
    assert "CREATE" not in out.upper()


def test_一般_SELECT_原樣保留():
    sql = "SELECT account_id FROM transactions WHERE amount >= 100000;"
    assert "transactions" in prepare_sql(sql)


def test_時間窗參數以_DECLARE_代入固定值():
    """執行窗固定才可重現。用了 @start_date 就要有對應的 DECLARE,否則執行會報錯。"""
    out = prepare_sql("SELECT 1 FROM t WHERE tx_time >= @start_date AND tx_time < @end_date;")
    assert "DECLARE" in out.upper()
    assert WIN_START in out and WIN_END in out


def test_沒用到參數就不加_DECLARE():
    out = prepare_sql("SELECT 1 FROM t;")
    assert "DECLARE" not in out.upper()


@pytest.mark.parametrize("fn", ["GETDATE()", "SYSDATETIME()", "CURRENT_TIMESTAMP"])
def test_系統日期函式換成固定值(fn):
    """`bind-no-sysdate` 禁用這些函式,但真的出現時執行驗證還是得跑得動、而且可重現
    ——不換掉的話,同一份測資在不同日期跑出不同結果。"""
    out = prepare_sql(f"SELECT 1 FROM t WHERE tx_time < {fn};")
    assert fn.rstrip("()") not in out.upper().replace("CAST", ""), f"{fn} 沒被換掉"
    assert WIN_END in out


# ─────────────────── arbitrate 的兩條保底路徑 ───────────────────

class _FakeCfg:
    """只需要 role_profile 回傳一個有 model / temperature 等欄位的東西。"""
    def role_profile(self, role):
        from orchestrator.config import ModelProfile
        return ModelProfile(model="fake", num_ctx=1024, temperature=0,
                            max_output_tokens=256)


_MISMATCH = {"case_id": "C1-T", "condition_id": "C1", "direction": "true",
             "note": "邊界案例", "rows": [["transactions", 1]],
             "expect_flagged": True, "actual_flagged": False, "actual_rows": 0}


def test_仲裁_LLM_呼叫失敗時保守倒向_SQL_錯(monkeypatch):
    """**方向很重要**:倒向「SQL 錯」= 開 major finding、擋下自動放行(誤報,人來看);
    倒向「測資錯」= 剔除案例、當成沒事(可能誤放)。

    策略是寧可誤差也不要錯放,所以環境掛掉時必須倒向前者。
    """
    async def boom(*a, **kw):
        raise ConnectionError("gate 連不到上游")

    monkeypatch.setattr(spec_exec, "run_agent", boom)
    verdict = asyncio.run(arbitrate(_FakeCfg(), "規格", "SELECT 1;", _MISMATCH))
    assert verdict["who_is_wrong"] == "sql"
    assert verdict["arbiter_unparseable"] is True
    assert "ConnectionError" in verdict["reason"]


@pytest.mark.parametrize("raw", [
    "模型今天不想輸出 JSON",              # 完全解析不出來
    '{"who_is_wrong": "maybe"}',         # 解出來了但值不合法
    '{"who_is_wrong": "both"}',
    '{"reason": "忘了寫 who_is_wrong"}',
])
def test_仲裁輸出無法解析時也倒向_SQL_錯(monkeypatch, raw):
    """只接受 testdata / sql 兩個值。任何其他東西都當成「不可信」並保守處理
    ——不能因為模型回了個看起來像 JSON 的東西就採信。"""
    async def fake(*a, **kw):
        return raw

    monkeypatch.setattr(spec_exec, "run_agent", fake)
    verdict = asyncio.run(arbitrate(_FakeCfg(), "規格", "SELECT 1;", _MISMATCH))
    assert verdict["who_is_wrong"] == "sql"
    assert verdict["arbiter_unparseable"] is True


@pytest.mark.parametrize("who", ["testdata", "sql"])
def test_合法的仲裁判定原樣返回(monkeypatch, who):
    async def fake(*a, **kw):
        return f'{{"who_is_wrong": "{who}", "reason": "理由"}}'

    monkeypatch.setattr(spec_exec, "run_agent", fake)
    verdict = asyncio.run(arbitrate(_FakeCfg(), "規格", "SELECT 1;", _MISMATCH))
    assert verdict["who_is_wrong"] == who
    assert "arbiter_unparseable" not in verdict


# ─────────────────── prompt 落檔(診斷用,預設關閉) ───────────────────

def test_預設不落檔(tmp_path, monkeypatch):
    """`SEGCRA_DUMP_PROMPTS` 沒設就什麼都不做。

    這是正式流程上的預設值,所以它必須是零影響:不建目錄、不寫檔、不丟例外。
    """
    import orchestrator.agent as agent
    monkeypatch.setattr(agent, "DUMP_PROMPTS", "")
    agent._dump("m", [{"role": "user", "content": "x"}], 0)
    assert list(tmp_path.iterdir()) == []


def test_落檔會寫出實際送出的每一段(tmp_path, monkeypatch):
    """開啟後,每次呼叫的完整 messages 要能原文還原。

    用途是把兩次跑的 prompt diff 起來,分辨「跑兩次不一樣」是模型取樣還是
    送出去的輸入本來就不同(見 eval/BASELINE.md §1、§7)。所以 system 與 user
    兩段都必須在檔案裡,少一段 diff 就看不出差在哪。
    """
    import orchestrator.agent as agent
    monkeypatch.setattr(agent, "DUMP_PROMPTS", str(tmp_path))
    monkeypatch.setattr(agent, "_dump_seq", 0)
    agent._dump("gemma4:31b", [{"role": "system", "content": "SYS-MARKER"},
                               {"role": "user", "content": "USER-MARKER"}], 3)
    files = list(tmp_path.glob("*.txt"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "SYS-MARKER" in text and "USER-MARKER" in text
    assert "iteration=3" in text
    # 模型名含冒號,檔名不能直接用——冒號在部分檔案系統上是非法字元
    assert ":" not in files[0].name


def test_落檔失敗不可以弄垮跑批(monkeypatch):
    """診斷功能寫不出去就算了,不該讓一輪十幾小時的跑批中途爆掉。"""
    import orchestrator.agent as agent
    monkeypatch.setattr(agent, "DUMP_PROMPTS", "/proc/不可能建得起來/x")
    agent._dump("m", [{"role": "user", "content": "x"}], 0)   # 不得丟例外
