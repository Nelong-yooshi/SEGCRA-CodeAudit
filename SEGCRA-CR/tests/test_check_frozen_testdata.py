"""eval/check_frozen_testdata.py 的確定性部分——不連沙盒。

真正的檢查要在 MS SQL 沙盒上跑;這裡只釘住「寫錯的版本對得上正確 SQL」:
正向對照的 SQL 改了寫法(例如空白、別名),替換會對不上,工具就量不到東西。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import check_frozen_testdata as cft  # noqa: E402


@pytest.mark.parametrize("code", sorted(cft.MUTANTS))
def test_每個寫錯的版本都對得上正確SQL且真的改到東西(code):
    sql = cft.reference_sql(code)
    for name, old, new, *where in cft.MUTANTS[code]:
        mutated = cft.mutate(sql, old, new, *where)
        assert mutated != sql, name


@pytest.mark.parametrize("code", sorted(cft.MUTANTS))
def test_每個寫錯的版本改出來的SQL都不一樣(code):
    """兩筆改出同一份 SQL = 其中一筆沒在量它自己宣稱的那個條件。
    指名「第幾處」之後這種錯更容易發生(抄上一筆忘了改 nth)。"""
    seen = {}
    sql = cft.reference_sql(code)
    for name, old, new, *where in cft.MUTANTS[code]:
        out = cft.mutate(sql, old, new, *where)
        assert out not in seen, f"{name} 與 {seen.get(out)} 改出同一份 SQL"
        seen[out] = name


def test_原文對不上時直接報錯而不是默默不改():
    with pytest.raises(ValueError, match="對不上"):
        cft.mutate("SELECT 1", "不存在的原文", "")


def test_出現次數與宣告不符時報錯():
    """「共幾處」是漂移偵測:正確 SQL 改版後次數變了要直接報錯,
    不可以悄悄改到別的地方,或是什麼都沒改卻顯示「抓不到」。"""
    with pytest.raises(ValueError, match="對不上"):
        cft.mutate("a a", "a", "b", "1/1")       # 實際 2 處,宣告 1 處


def test_指名第幾處只改那一處():
    assert cft.mutate("x a y a z", "a", "B", "2/2") == "x a y B z"
    assert cft.mutate("x a y a z", "a", "B", "1/2") == "x B y a z"
    assert cft.mutate("x a y a z", "a", "B", "*/2") == "x B y B z"
