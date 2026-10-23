"""受影響的 model 查規格時,「取不到」不是「沒有規格」。

token 沒設、沒釘住 commit、規格檔太大、取檔失敗時,find_spec 的回傳值帶 `unavailable` 代碼
(spec_exec.SpecLookup)。這些 model 另外列成「取不到規格」、寫出是哪一種設定、升到 major,
不列進「沒有對應到規格」:那會叫開發者去補一份其實存在的規格,維運也看不出是設定的問題。
給審查模型的規格段也分開寫。
"""
import json

import pytest

from orchestrator import pipeline
from orchestrator.dbt_affected import SPEC_UNAVAILABLE, AffectedCheck, impact_finding
from tests.test_dbt_affected import _check, _cm, _macro_ctx, _only


class _Lookup(tuple):
    """spec_exec.SpecLookup 的形狀:(code, text) 加一個 unavailable 屬性。"""

    def __new__(cls, code, text, unavailable=None):
        obj = super().__new__(cls, (code, text))
        obj.unavailable = unavailable
        return obj


def _find(result):
    async def find(hub, mr, *, by_path=False):
        return result
    return find


@pytest.mark.parametrize("code", sorted(SPEC_UNAVAILABLE))
def test_取不到規格時記下原因_不算對到也不算沒有(code):
    check, *_ = _check("SELECT 1 AS id", find_spec=_find(_Lookup("BIG", None, code)))
    m = _only(check)
    assert (m.spec, m.spec_unavailable, m.spec_looked_up) == (None, code, True)
    assert check.spec is None


def test_不認得的原因記成取檔失敗():
    check, *_ = _check("SELECT 1 AS id", find_spec=_find(_Lookup("BIG", None, "ZZ_weird")))
    assert _only(check).spec_unavailable == "fetch_failed"


@pytest.mark.parametrize("result", [_Lookup("BIG", None), ("BIG", None), (None, None)])
def test_確定沒有規格時照舊是沒有對應到(result):
    check, *_ = _check("SELECT 1 AS id", find_spec=_find(result))
    assert _only(check).spec_unavailable is None and _only(check).spec is None


def test_有對到規格時不看_unavailable():
    """內容拿到了就是對到(unavailable 只描述拿不到的情況)。"""
    check, *_ = _check("SELECT 1 AS id", find_spec=_find(_Lookup("BIG", "# BIG", "too_large")))
    assert (_only(check).spec, _only(check).spec_unavailable) == ("BIG", None)


@pytest.mark.parametrize("code", sorted(SPEC_UNAVAILABLE))
def test_finding_取不到另外列_寫出哪一種設定_升到major(code):
    fd = impact_finding(AffectedCheck(impact=_macro_ctx().impact,
                                      models=(_cm("models/a.sql", spec_unavailable=code),)))
    assert fd["severity"] == "major"
    assert "**取不到規格**" in fd["detail"] and "不需要補規格" in fd["detail"]
    assert f"models/a.sql({SPEC_UNAVAILABLE[code]})" in fd["detail"]
    assert "沒有對應到規格" not in fd["detail"]


def test_finding_取不到與沒有對應到分開列():
    fd = impact_finding(AffectedCheck(impact=_macro_ctx().impact, models=(
        _cm("models/a.sql", spec_unavailable="no_read_token"), _cm("models/b.sql"))))
    assert "沒有對應到規格:models/b.sql" in fd["detail"]
    assert "models/a.sql(未設定唯讀的 GITLAB_READ_TOKEN)" in fd["detail"]
    assert "沒有對應到規格:models/a.sql" not in fd["detail"]


def test_finding_沒有取不到時不提():
    fd = impact_finding(AffectedCheck(impact=_macro_ctx().impact, models=(_cm("models/b.sql"),)))
    assert "取不到規格" not in fd["detail"]


def test_finding_不認得的代碼寫成取檔失敗():
    fd = impact_finding(AffectedCheck(impact=_macro_ctx().impact,
                                      models=(_cm("models/a.sql", spec_unavailable="ZZ"),)))
    assert f"models/a.sql({SPEC_UNAVAILABLE['fetch_failed']})" in fd["detail"] and "ZZ" not in fd["detail"]


def test_給審查的規格段_取不到的分開列():
    affected = AffectedCheck(impact=_macro_ctx().impact, models=(
        _cm("models/a.sql", spec_unavailable="fetch_failed"), _cm("models/b.sql")))
    text, codes = pipeline._affected_spec_section((), affected, "dbt", 10_000)
    assert codes == ()
    assert "- 沒有對應到規格的 model:dbt/models/b.sql" in text
    assert "- 取不到規格的 model(環境問題,規格可能存在,不要當成沒有規格):dbt/models/a.sql" in text
    assert "沒有對應到規格的 model:dbt/models/a.sql" not in text


def test_沒有取不到的_規格段不提():
    affected = AffectedCheck(impact=_macro_ctx().impact, models=(_cm("models/b.sql"),))
    text, _ = pipeline._affected_spec_section((), affected, "", 10_000)
    assert "取不到規格" not in text


def test_報告不帶原始錯誤():
    async def boom(hub, mr, *, by_path=False):
        raise ConnectionError("ZZ_HOST_192.0.2.10_ZZ")
    check, *_ = _check("SELECT 1 AS id", find_spec=boom)
    fd = impact_finding(check)
    assert "ZZ_HOST" not in json.dumps(fd, ensure_ascii=False) and "ConnectionError" not in fd["detail"]
    assert fd["severity"] == "major"
