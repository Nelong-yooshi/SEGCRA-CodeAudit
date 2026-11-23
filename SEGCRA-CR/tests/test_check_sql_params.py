"""判斷式(check_sql)的參數宣告(S1)——不連資料庫。

部署端現場:模型產生的判斷式會直接用 `@start_date` / `@end_date`,而判斷式是
**單獨**送進沙盒執行的,沒有宣告就回錯誤 137(Must declare the scalar variable),
整個案例變成「標籤未驗證」。家裡的模型會把日期寫成字面值,所以在這邊看不出來 ——
所以這份測試**不依賴模型**,判斷式一律手寫。
"""
import pytest

# testgen_system 以 test 開頭,直接匯入會被 pytest 當成測試收集(而且「通過」)。
from orchestrator.spec_exec import testgen_system as _testgen_system
from orchestrator.spec_exec import (WIN_END, WIN_START, _eval_conditions, _window_decls,
                                    prepare_sql)


class _Cursor:
    """記下執行過的 SQL;可以指定某一句要丟什麼例外。"""

    def __init__(self, error=None, value=1):
        self.executed, self._error, self._value = [], error, value

    def execute(self, sql):
        self.executed.append(sql)
        if self._error is not None:
            raise self._error

    def fetchone(self):
        return (self._value,)


# ─────────────────── 1. 判斷式拿得到執行窗參數 ───────────────────

@pytest.mark.parametrize("check_sql,expect", [
    ("SELECT CASE WHEN EXISTS (SELECT 1 FROM t WHERE tx_time >= @start_date) THEN 1 ELSE 0 END",
     ["@start_date"]),
    ("SELECT CASE WHEN EXISTS (SELECT 1 FROM t WHERE tx_time < @end_date) THEN 1 ELSE 0 END",
     ["@end_date"]),
    ("SELECT CASE WHEN EXISTS (SELECT 1 FROM t "
     "WHERE tx_time >= @start_date AND tx_time < @end_date) THEN 1 ELSE 0 END",
     ["@start_date", "@end_date"]),
    ("SELECT CASE WHEN EXISTS (SELECT 1 FROM t WHERE amount >= 1000) THEN 1 ELSE 0 END", []),
])
def test_判斷式用到的執行窗參數會先宣告(check_sql, expect):
    cur = _Cursor()
    values, why = _eval_conditions(cur, [{"id": "c1", "check_sql": check_sql}])
    assert values == {"c1": True} and why == ""
    sent = cur.executed[0]
    for name in expect:
        assert "DECLARE" in sent and name in sent.split("SELECT TOP 1")[0], name
    if not expect:
        assert not sent.startswith("DECLARE"), "沒用到就不要多宣告"


def test_宣告的值跟受審SQL一致():
    """條件的語意要跟受審 SQL 執行時一樣 —— 兩邊都用固定執行窗的那組值。"""
    assert _window_decls("x @start_date @end_date") == (
        f"DECLARE @start_date DATE = '{WIN_START}', @end_date DATE = '{WIN_END}';\n")
    prepared = prepare_sql("SELECT 1 FROM t WHERE d >= @start_date AND d < @end_date")
    assert f"'{WIN_START}'" in prepared and f"'{WIN_END}'" in prepared


# ─────────────────── 2. 失敗訊息要分得出種類 ───────────────────

def test_沒宣告的參數說得出是判斷式的問題():
    """宣告過之後還出現錯誤 137,代表判斷式用了我們不知道的參數 —— 那是判斷式本身的
    問題,不是「條件不成立」。兩者混在一起的話,看報告的人不知道是沒驗到還是真的有事。"""
    err = Exception('Must declare the scalar variable "@threshold".')
    values, why = _eval_conditions(_Cursor(error=err), [{"id": "c2", "check_sql": "SELECT 1"}])
    assert values is None
    assert "@threshold" in why and "判斷式本身的問題" in why


def test_其他執行失敗照舊帶出原因():
    err = Exception("Invalid column name 'amountt'.")
    values, why = _eval_conditions(_Cursor(error=err), [{"id": "c3", "check_sql": "SELECT 1"}])
    assert values is None and "判斷式執行失敗" in why and "amountt" in why


def test_沒有判斷式時說明是哪一條():
    values, why = _eval_conditions(_Cursor(), [{"id": "c4"}])
    assert values is None and "c4" in why


# ─────────────────── 3. prompt 的輔助要求 ───────────────────

def test_prompt要求日期寫字面值():
    """只當輔助:模型不一定照做,真正的保證是上面的自動宣告。"""
    t = _testgen_system()
    assert "日期一律寫成字面值" in t
    assert "@start_date" in t and "自動宣告" in t
