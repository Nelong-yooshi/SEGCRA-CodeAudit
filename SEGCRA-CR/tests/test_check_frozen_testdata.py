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
    for name, old, new in cft.MUTANTS[code]:
        mutated = cft.mutate(sql, old, new)
        assert mutated != sql, name


def test_原文對不上時直接報錯而不是默默不改():
    with pytest.raises(ValueError, match="對不上"):
        cft.mutate("SELECT 1", "不存在的原文", "")
