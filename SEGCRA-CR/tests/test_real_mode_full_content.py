"""真實 GitLab 模式要拿到**整份新檔**,不是 diff 裡那幾行(#22)。

為什麼這層特別容易漏:**golden case 都有 `full_content`**,所以
`full_content or _sql_from_diff(...)` 的第二條路在評測裡永遠不會被走到,
而正式環境每次都走。實測 `mr_702`(大檔只改一行)在真實模式下送進預掃的 SQL
只剩 `AND eod_balance < 1000` 一行,sqlglot 直接解析失敗。

這裡驗三件事:
  1. real 模式依 head sha 補 full_content
  2. `get_file` 不指定 ref 時抓「正在審的那一版」,不是目標分支
  3. **規格不跟 MR 走** —— 否則 MR 能改自己的規格讓自己過
"""
import json

import pytest

from toolbox import gitlab


@pytest.fixture(autouse=True)
def _reset_pin():
    gitlab.pin_review_ref(None)
    yield
    gitlab.pin_review_ref(None)


SHA = "a" * 40


@pytest.fixture
def real(monkeypatch):
    """切到 real 模式,並把 GitLab API 換成可控的假回應。"""
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", "http://gl")
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", "t")
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", "1")

    state = {"files": {}, "fetched": []}

    def fake_api(method, path, **kw):
        return {"title": "T", "description": "D", "sha": SHA,
                "diff_refs": {"base_sha": "b" * 40, "head_sha": SHA,
                              "start_sha": "c" * 40}}

    def fake_pages(path, params=None):
        return [{"new_path": "sql/big.sql", "old_path": "sql/big.sql",
                 "diff": "@@\n+AND eod_balance < 1000\n"}], False

    def fake_file_at(path, ref):
        state["fetched"].append((path, ref))
        return state["files"].get((path, ref))

    monkeypatch.setattr(gitlab, "_api", fake_api)
    monkeypatch.setattr(gitlab, "_api_pages", fake_pages)
    monkeypatch.setattr(gitlab, "_file_at", fake_file_at)
    return state


# ─────────────────── 1. real 模式補 full_content ───────────────────

def test_依_head_sha_補上整份新檔(real):
    """這是整個 #22 的重點:預掃與執行驗證要的是整份檔,不是 diff 的那幾行。"""
    real["files"][("sql/big.sql", SHA)] = "SELECT 1\nFROM t\nWHERE x > 0\n"
    out = json.loads(gitlab.get_mr_diff("7"))
    f = out["files"][0]
    assert f["full_content"] == "SELECT 1\nFROM t\nWHERE x > 0\n"
    assert real["fetched"] == [("sql/big.sql", SHA)], "要用 head sha 抓,不是分支名"


def test_取不到內容時不可以塞空字串(real):
    """下游是 `full_content or _sql_from_diff(...)`。塞空字串會讓下游以為
    「這個檔是空的」而不是「我們沒拿到」——前者會靜默地用空 SQL 跑完,
    後者才會退回用 diff。"""
    real["files"] = {}                      # 取不到
    f = json.loads(gitlab.get_mr_diff("7"))["files"][0]
    assert "full_content" not in f
    assert f["full_content_unavailable"] is True


def test_拿不到_head_sha_就不抓(real, monkeypatch):
    """沒有 head sha 時不能退回用分支名:分支在審查中途可能被推新 commit,
    那會變成「審了 A、驗了 B」而且完全沒有徵兆。"""
    monkeypatch.setattr(gitlab, "_api",
                        lambda *a, **k: {"title": "T", "sha": SHA, "diff_refs": None})
    f = json.loads(gitlab.get_mr_diff("7"))["files"][0]
    assert real["fetched"] == [], "拿不到 head sha 卻還是去抓了"
    assert "full_content" not in f


def test_拿不到內容的檔案不用多打一次_API(real):
    """GitLab 對過大/被摺疊的檔案本來就不給內容,再打一次只是浪費時間。"""
    def pages(path, params=None):
        return [{"new_path": "big.sql", "diff": "", "too_large": True}], False
    real_api_pages = pages
    import toolbox.gitlab as g
    g._api_pages = real_api_pages
    f = json.loads(gitlab.get_mr_diff("7"))["files"][0]
    assert f["unreviewable"] is True
    assert real["fetched"] == []


# ─────────────────── 2. get_file 的預設版本 ───────────────────

@pytest.fixture
def seen_ref(monkeypatch):
    """攔下 get_file 實際送出的 `ref` 參數。

    get_file 內部是 `import httpx` 再 `httpx.get(...)`,所以換掉 sys.modules
    裡的 httpx 就攔得到,不用真的發請求。
    """
    import sys
    import types
    seen = {}

    class _Resp:
        status_code = 200
        text = "content"

        def raise_for_status(self):
            pass

    def _get(url, **kw):
        seen.update(kw.get("params") or {})
        return _Resp()

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(get=_get))
    return seen


def test_不指定_ref_時抓正在審的那一版(real, seen_ref):
    """先前預設是 `main`,所以模型用這個工具看檔案時拿到的是**改之前**的內容
    ——看起來正常,實際上在審錯的東西。"""
    gitlab.pin_review_ref(SHA)
    gitlab.get_file("sql/big.sql")
    assert seen_ref["ref"] == SHA


def test_沒有釘住版本時退回目標分支(real, seen_ref):
    """拿不到被審的版本是降級,不是正常狀況;但也不能因此就不給內容。"""
    gitlab.pin_review_ref(None)
    gitlab.get_file("specs/R-1.md")
    assert seen_ref["ref"] == gitlab.SPEC_REF


def test_明確指定的_ref_優先(real, seen_ref):
    """規格就是靠這條路:find_spec 明確傳 SPEC_REF,不吃預設值。"""
    gitlab.pin_review_ref(SHA)
    gitlab.get_file("specs/R-1.md", ref="main")
    assert seen_ref["ref"] == "main", "明確傳的 ref 被釘住的版本蓋掉了"


# ─────────────────── 3. 規格不跟 MR 走(安全性) ───────────────────

def test_規格一律從目標分支讀(monkeypatch):
    """**這一條是安全性,不是整潔。**

    #22 把 `get_file` 的預設從 `main` 改成「正在審的那一版」。如果規格也跟著走,
    一個 MR 就能在同一次提交裡改掉自己的規格,讓執行驗證拿改過的規格去生測資、
    再拿那份測資驗自己——整道防線自己把門打開,而且報告上看起來完全正常。

    所以 `find_spec` 必須**明確**傳 `ref=SPEC_REF`,不能吃預設值。
    這個測試釘住的是「有傳」,不是「預設剛好是對的」。
    """
    import asyncio

    from orchestrator import spec_exec

    calls = []

    class _Hub:
        async def call(self, name, params, truncate=False):
            calls.append(params)
            return "(找不到)"

    monkeypatch.setattr(spec_exec, "SPECS_DIR", __import__("pathlib").Path("/nonexistent"))
    mr = {"title": "R-999 調整", "description": "", "files": [{"path": "sql/x.sql"}]}
    asyncio.run(spec_exec.find_spec(_Hub(), mr))

    assert calls, "find_spec 沒有去抓規格"
    for p in calls:
        assert p.get("ref") == gitlab.SPEC_REF, (
            f"規格讀取沒有明確指定 ref(拿到 {p.get('ref')!r})——"
            "吃預設值的話,get_file 的預設一改,MR 就能改自己的規格")


# ─────────────────── 4. 端到端:大檔只改一行(#22 的完成條件) ───────────────────

# 168 行規模的真實形狀:多層 CTE。diff 只有最後那一行。
# 這正是 issue #22 描述的 mr_702 情境——從 diff 還原出來的 SQL 只剩
# `AND eod_balance < 1000`,sqlglot 連 SELECT 都看不到。
_BIG_SQL = """-- R-201: 單日 ATM 現金提領累計達 100000(含)通報
WITH base AS (
    SELECT account_id, amount, tx_time, tx_type, channel
    FROM transactions
    WHERE tx_time >= @start_date AND tx_time < @end_date
), filtered AS (
    SELECT account_id, amount, tx_time
    FROM base
    WHERE tx_type = 'WITHDRAW' AND channel = 'ATM'
), agg AS (
    SELECT account_id, SUM(amount) AS total_amount, MIN(amount) AS eod_balance
    FROM filtered
    GROUP BY account_id
)
SELECT account_id, total_amount
FROM agg
WHERE total_amount >= 100000
  AND eod_balance < 1000;"""

_ONE_LINE_DIFF = "@@ -17,2 +17,3 @@\n   WHERE total_amount >= 100000\n+  AND eod_balance < 1000;\n"


def _review_real(monkeypatch, tmp_path, *, serve_full: bool):
    """在 real 模式下跑完整條 review_mr,回傳 (報告, 預掃結果)。

    serve_full=False 模擬「拿不到完整檔」(也就是 #22 修正前的行為),
    用來證明這個測試真的分得出有沒有修。
    """
    import asyncio

    from orchestrator import pipeline, spec_exec
    from orchestrator.config import load_config

    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", "http://gl")
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", "t")
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", "1")
    monkeypatch.setenv("REVIEW_OUTPUT", str(tmp_path))
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", tmp_path)

    monkeypatch.setattr(gitlab, "_api", lambda *a, **k: {
        "title": "R-201 例行調整", "description": "加上結餘條件", "sha": SHA,
        "diff_refs": {"base_sha": "b" * 40, "head_sha": SHA, "start_sha": "c" * 40}})
    monkeypatch.setattr(gitlab, "_api_pages", lambda p, params=None: (
        [{"new_path": "sql/rules/r201.sql", "diff": _ONE_LINE_DIFF}], False))
    monkeypatch.setattr(gitlab, "_file_at",
                        lambda path, ref: _BIG_SQL if serve_full else None)
    # 回寫 GitLab 的動作在 real 模式會發 HTTP,這裡不是要驗那個
    for fn in ("post_inline_comment", "post_summary", "set_commit_status", "set_label"):
        monkeypatch.setattr(gitlab, fn, lambda *a, **k: "ok")

    async def fake_agent(cfg, profile, system, user, hub, verbose=True,
                         use_tools=True, trace=None, meta=None):
        return '{"score": 90, "verdict": "approve", "summary": "相符。", "findings": []}'
    monkeypatch.setattr(pipeline, "run_agent", fake_agent)

    async def no_cases(*a, **k):
        return {"schema_ddl": [], "conditions": [], "cases": []}, ["測試中略過"], []
    monkeypatch.setattr(spec_exec, "generate_cases", no_cases)

    captured: dict = {}
    report = asyncio.run(review_or_raise(pipeline, load_config(), captured))
    return report, captured.get("prescan_raw") or []


def review_or_raise(pipeline, cfg, captured):
    return pipeline.review_mr(cfg, "900", capture=captured)


def test_大檔只改一行_預掃要拿到整份檔而不是那一行(monkeypatch, tmp_path):
    """#22 的完成條件:真實模式下預掃解析成功。

    修正前送進預掃的是從 diff 還原的 `AND eod_balance < 1000`——sqlglot 連
    SELECT 都看不到,解析失敗,AST 規則整組沒跑。而這個缺口 golden set 量不到,
    因為 golden case 都有 full_content。
    """
    _, pre = _review_real(monkeypatch, tmp_path, serve_full=True)
    assert pre, "沒有拿到預掃結果"
    entry = pre[0]
    assert "parse_error" not in entry, (
        f"預掃解析失敗了:{entry.get('parse_error')}——"
        "代表送進去的不是整份檔")


def test_拿不到完整檔時_預掃解析失敗而且會被揭露(monkeypatch, tmp_path):
    """反面對照,兩個作用:

    1. 證明上一個測試真的分得出有沒有修(不是恰好都綠)
    2. 釘住降級時的行為:拿不到完整檔**不可以靜默**——`enforce_parse` 要補一條
       finding,否則 MR 可能因為「沒有命中任何規則」而被自動放行
    """
    report, pre = _review_real(monkeypatch, tmp_path, serve_full=False)
    assert pre and "parse_error" in pre[0], "拿不到完整檔卻解析成功了?測試設計有問題"
    titles = [f.get("title") for f in report.get("findings", [])]
    assert any("預掃無法解析" in str(t) for t in titles), (
        f"解析失敗沒有被揭露,findings = {titles}")
