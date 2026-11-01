"""受影響的 model 查規格時,「取不到」不是「沒有規格」。

token 沒設、沒釘住 commit、專案或 commit 找不到、規格檔太大、取檔失敗、取回的內容不是規格時,
find_spec 的回傳值帶 `unavailable` 代碼(spec_exec.SpecLookup)。這些 model 另外列成「取不到規格」、
寫出是哪一種設定(說法引用 gitlab.UNAVAILABLE_TEXT)、升到 major,不列進「沒有對應到規格」:那會叫
開發者去補一份其實存在的規格,維運也看不出是設定的問題。給審查模型的規格段也分開寫。
"""
import asyncio
import json

import httpx
import pytest

from orchestrator import pipeline
from orchestrator.dbt_affected import AffectedCheck, impact_finding
from orchestrator.spec_exec import SpecLookup as _Lookup
from tests.test_dbt_affected import _check, _cm, _macro_ctx, _only
from toolbox import gitlab
from toolbox.gitlab import UNAVAILABLE_TEXT


def _text(code):
    return UNAVAILABLE_TEXT[code].rstrip("。")


def _find(result):
    async def find(hub, mr, *, by_path=False):
        return result
    return find


@pytest.mark.parametrize("code", sorted(UNAVAILABLE_TEXT))
def test_取不到規格時記下原因_不算對到也不算沒有(code):
    check, *_ = _check("SELECT 1 AS id", find_spec=_find(_Lookup("BIG", None, code)))
    m = _only(check)
    assert (m.spec, m.spec_unavailable, m.spec_looked_up) == (None, code, True)
    assert check.spec is None


def test_不認得的原因記成取檔失敗():
    check, *_ = _check("SELECT 1 AS id", find_spec=_find(_Lookup("BIG", None, "ZZ_weird")))
    assert _only(check).spec_unavailable == "fetch_failed"


@pytest.mark.parametrize("result", [_Lookup("BIG", None), _Lookup(None, None)])
def test_確定沒有規格時照舊是沒有對應到(result):
    check, *_ = _check("SELECT 1 AS id", find_spec=_find(result))
    assert _only(check).spec_unavailable is None and _only(check).spec is None


def test_有對到規格時不看_unavailable():
    """內容拿到了就是對到(unavailable 只描述拿不到的情況)。"""
    check, *_ = _check("SELECT 1 AS id", find_spec=_find(_Lookup("BIG", "# BIG", "too_large")))
    assert (_only(check).spec, _only(check).spec_unavailable) == ("BIG", None)


@pytest.mark.parametrize("code", sorted(UNAVAILABLE_TEXT))
def test_finding_取不到另外列_寫出哪一種設定_升到major(code):
    fd = impact_finding(AffectedCheck(impact=_macro_ctx().impact,
                                      models=(_cm("models/a.sql", spec_unavailable=code),)))
    assert fd["severity"] == "major"
    assert "**取不到規格**" in fd["detail"] and "不需要補規格" in fd["detail"]
    assert f"models/a.sql({_text(code)})" in fd["detail"]
    assert "沒有對應到規格" not in fd["detail"]


def test_finding_取不到與沒有對應到分開列():
    fd = impact_finding(AffectedCheck(impact=_macro_ctx().impact, models=(
        _cm("models/a.sql", spec_unavailable="no_read_token"), _cm("models/b.sql"))))
    assert "沒有對應到規格:models/b.sql" in fd["detail"]
    assert "models/a.sql(取檔用的唯讀憑證沒有設定)" in fd["detail"]
    assert "GITLAB_" not in fd["detail"]         # 不寫環境變數名(會貼回 MR)
    assert "沒有對應到規格:models/a.sql" not in fd["detail"]


def test_finding_沒有取不到時不提():
    fd = impact_finding(AffectedCheck(impact=_macro_ctx().impact, models=(_cm("models/b.sql"),)))
    assert "取不到規格" not in fd["detail"]


def test_finding_不認得的代碼寫成取檔失敗():
    fd = impact_finding(AffectedCheck(impact=_macro_ctx().impact,
                                      models=(_cm("models/a.sql", spec_unavailable="ZZ"),)))
    assert f"models/a.sql({_text('fetch_failed')})" in fd["detail"] and "ZZ" not in fd["detail"]


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


def test_查規格的回傳值不是_SpecLookup_時不會靜靜當成沒有規格():
    """直接讀 unavailable、不給預設值:find_spec 的介面改了,這一步記成「發生非預期錯誤、全部
    model 都可能受影響」(major),不會退回「確定沒有規格」。"""
    check, *_ = _check("SELECT 1 AS id", find_spec=_find(("BIG", None)))
    assert check.models == () and check.impact.all_models_possibly_affected
    assert any("AttributeError" in u for u in check.impact.uncertain)
    assert impact_finding(check)["severity"] == "major"


# ─────────────────── 用真的 find_spec 與 gitlab.fetch_file,只換掉 GitLab 的回應 ───────────────────

SPEC = "# BIG 規格\n\n## 條件\n- 金額大於 100\n"


@pytest.fixture
def real_gitlab(monkeypatch):
    """真實 GitLab 模式的設定(假的位址與 token);HTTP 回應由各測試決定。"""
    from orchestrator import spec_exec
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", "https://gitlab.invalid")
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", "1")
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", "test-write-token")
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", "test-read-token")
    calls = []

    def reply(status=200, text=SPEC, raises=False):
        def get(url, headers=None, params=None, timeout=None):
            calls.append({"url": url, "ref": (params or {}).get("ref")})
            if raises:
                raise httpx.ConnectError("連不上")
            return httpx.Response(status, text=text, request=httpx.Request("GET", url))
        monkeypatch.setattr(httpx, "get", get)
    gitlab.pin_review_ref("e1b2" + "0" * 36)
    yield reply, calls
    gitlab.pin_review_ref(None)
    assert spec_exec.fetch_file is gitlab.fetch_file      # 沒有被換掉:走的是真的取檔


def _real_check():
    from orchestrator.spec_exec import find_spec
    return _only(_check("SELECT 1 AS id", find_spec=find_spec)[0])


@pytest.mark.parametrize("setup,code", [
    (lambda reply: setattr(gitlab, "GITLAB_READ_TOKEN", ""), "no_read_token"),          # token 沒設
    (lambda reply: gitlab.pin_review_ref(None), "no_review_ref"),                        # 沒釘住 commit
    (lambda reply: reply(404, '{"message":"404 Project Not Found"}'), "not_found"),     # 專案或權限
    (lambda reply: reply(200, "這不是規格,只是一段文字"), "not_a_spec"),               # 內容不是規格
    (lambda reply: reply(raises=True), "fetch_failed"),                                 # 連不上
])
def test_用真的_find_spec_取不到時受影響_model_都記成取不到_說法也對(real_gitlab, setup, code):
    reply, calls = real_gitlab
    reply()                                       # 預設回一份正常的規格
    setup(reply)
    m = _real_check()
    assert (m.spec, m.spec_unavailable) == (None, code)
    fd = impact_finding(AffectedCheck(impact=_macro_ctx().impact, models=(m,)))
    assert fd["severity"] == "major" and f"({_text(code)})" in fd["detail"]
    assert "沒有對應到規格" not in fd["detail"]


def test_用真的_find_spec_檔案確實不存在時是沒有對應到(real_gitlab):
    reply, calls = real_gitlab
    reply(404, '{"message":"404 File Not Found"}')
    m = _real_check()
    assert (m.spec, m.spec_unavailable, m.spec_looked_up) == (None, None, True)
    assert calls and all(c["ref"] == "e1b2" + "0" * 36 for c in calls)   # 讀的是釘住的那一版


def test_用真的_find_spec_拿到規格時對到(real_gitlab):
    reply, calls = real_gitlab
    reply()
    m = _real_check()
    assert m.spec_unavailable is None and m.spec



# ─────────────────── 審查流程實際走的那一條:pipeline._find_spec_in_mr 包過的 find_spec ───────────────────

def _mr_with(files):
    return {"title": "t", "description": "", "files": files}


@pytest.mark.parametrize("code", sorted(UNAVAILABLE_TEXT))
def test_審查流程包過的_find_spec_也帶出取不到的原因(monkeypatch, code):
    """審查流程不是直接用 find_spec,而是 _find_spec_in_mr 包過的那一個(規格換成 MR 的版本)。
    包裝把 unavailable 丟掉的話,正式審查裡受影響的 model 一律變成「沒有對應到規格」。"""
    async def find(hub, target, **kw):
        return _Lookup("BIG", None, code)
    monkeypatch.setattr(pipeline, "find_spec", find)
    wrapped = pipeline._find_spec_in_mr(_mr_with([{"path": "macros/a.sql", "diff": "@@ -1 +1 @@\n+x"}]))
    check, *_ = _check("SELECT 1 AS id", find_spec=wrapped)
    assert _only(check).spec_unavailable == code
    lookup = asyncio.run(wrapped(None, {"title": "", "description": "", "files": []}))
    assert lookup.unavailable == code


def test_審查流程包過的_find_spec_用_MR_的規格版本時不算取不到(monkeypatch):
    spec = "# BIG 規格\n\n## 條件\n- MR 改過的版本\n"

    async def find(hub, target, **kw):
        return _Lookup("BIG", None, "no_read_token")
    monkeypatch.setattr(pipeline, "find_spec", find)
    wrapped = pipeline._find_spec_in_mr(_mr_with([{"path": "specs/BIG.md", "diff": "@@ -1 +1 @@\n+x",
                                                    "full_content": spec}]))
    lookup = asyncio.run(wrapped(None, {"title": "", "description": "", "files": []}))
    assert lookup == ("BIG", spec) and lookup.unavailable is None


def test_本機規格也回_SpecLookup(monkeypatch):
    """mock 模式(評測、demo)讀本機 specs/ 的那條路也要回 SpecLookup,包裝才讀得到 unavailable。"""
    from orchestrator import spec_exec
    monkeypatch.setattr(spec_exec, "_real_mode", lambda: False)
    lookup = asyncio.run(spec_exec.find_spec(None, {"title": "R-140 調整", "description": "", "files": []}))
    assert isinstance(lookup, spec_exec.SpecLookup) and lookup[0] == "R-140" and lookup[1]
    assert lookup.unavailable is None
