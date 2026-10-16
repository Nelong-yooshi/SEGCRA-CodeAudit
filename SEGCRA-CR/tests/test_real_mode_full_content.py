"""真實 GitLab 模式要拿到**整份新檔**,不是 diff 裡那幾行(#22)。

為什麼這層特別容易漏:**golden case 都有 `full_content`**,所以
`full_content or _sql_from_diff(...)` 的第二條路在評測裡永遠不會被走到,
而正式環境每次都走。實測 `mr_702`(大檔只改一行)在真實模式下送進預掃的 SQL
只剩 `AND eod_balance < 1000` 一行,sqlglot 直接解析失敗。

這裡驗三件事:
  1. real 模式依 head sha 補 full_content
  2. `get_file` 不指定 ref 時抓「正在審的那一版」,不是目標分支
  3. **規格讀的也是正在審的那一版** —— 規格與程式會在同一個 MR 裡一起修正
"""
import json
import sys
import types

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
    # 讀程式碼用唯讀 token:沒設定時 get_file 直接回「讀不到」,
    # 不退回有寫入權的那把。
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", "ro")
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", "1")

    state = {"files": {}, "fetched": [], "api_pages": None}

    def fake_api(method, path, **kw):
        return {"title": "T", "description": "D", "sha": SHA,
                "diff_refs": {"base_sha": "b" * 40, "head_sha": SHA,
                              "start_sha": "c" * 40}}

    # 預設只有一個檔;測試可以塞 state["api_pages"] 換掉(例如要測刪除的檔)
    state["api_pages"] = ([{"new_path": "sql/big.sql", "old_path": "sql/big.sql",
                            "diff": "@@\n+AND eod_balance < 1000\n"}], False)

    def fake_pages(path, params=None):
        return state["api_pages"]

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
    # **而且要標成取不到**,否則這些檔只有 diff 那幾行進得了預掃與沙盒,
    # 卻沒有任何提示。GitLab 在 MR 剛建立、diff 還沒算好時 diff_refs 是 null,
    # 而 webhook 往往就在這時候觸發。
    assert f["full_content_unavailable"] is True


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
        content = b"content"      # get_file 要看位元組長度(單檔上限)

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


def test_沒有釘住版本時不可以退回目標分支(real, seen_ref):
    """**不退回**,明講取不到。

    退回目標分支拿到的是**改之前**的內容,那正是 #22 原本的 bug,而且沒有任何
    徵兆:報告看起來正常,實際上審的是舊版。
    """
    gitlab.pin_review_ref(None)
    out = gitlab.get_file("sql/x.sql")
    assert "無法取得審查中的版本" in out, out
    assert "ref" not in seen_ref, "不該發出請求去抓別的版本"


def test_明確指定的_ref_優先(real, seen_ref):
    """呼叫端明確傳的 ref 不可以被釘住的版本蓋掉(例如要比對改動前後時)。"""
    gitlab.pin_review_ref(SHA)
    gitlab.get_file("sql/x.sql", ref="main")
    assert seen_ref["ref"] == "main", "明確傳的 ref 被釘住的版本蓋掉了"


# ─────────────────── 3. 規格讀的是正在審的那一版 ───────────────────

def test_規格讀的是正在審的那一版(monkeypatch):
    """規格與程式是我們自己整理的,會在同一個 MR 裡一起修正。

    鎖在目標分支的話,「規格寫錯、跟程式一起修」的 MR 會被拿**舊規格**生測資,
    把正確的修正判成不符。

    代價是一個 MR 可以同時改規格與程式、讓執行驗證拿改過的規格驗自己——那一點
    改由「讓審查的人看見規格被改了」處理(#38 第 3 點),不是靠鎖版本:
    鎖版本會讓正常的修正無法通過。
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
        assert "ref" not in p, (
            f"規格讀取指定了 ref={p.get('ref')!r}——應該吃 get_file 的預設"
            "(正在審的那一版),否則規格跟程式一起修的 MR 會被拿舊規格驗")


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
    # 讀程式碼用唯讀 token:沒設定時 get_file 直接回「讀不到」,
    # 不退回有寫入權的那把。
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", "ro")
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

    # real 模式**不讀**工具內建的 specs/(那份是評測與離線開發用的樣本),所以規格
    # 也要從 GitLab 供。這同時釘住「規格讀的是正在審的那一版」:這裡回的規格
    # 只有走 get_file 才拿得到。
    spec_reads = []

    def fake_get_file(path, ref="", mr_id=""):
        spec_reads.append((path, ref))
        if path.startswith("specs/"):
            return "# R-201 單日 ATM 現金提領累計達 100000(含)通報\n\n規格內容。\n"
        return _BIG_SQL if serve_full else f"(repo 內無 {path})"
    monkeypatch.setattr(gitlab, "get_file", fake_get_file)
    # tool_hub 在**模組載入時**就把函式物件放進 TOOL_GROUPS,所以只換 gitlab.get_file
    # 不會影響模型(與 find_spec)實際呼叫到的那個。兩邊都要換。
    from orchestrator import tool_hub
    fake_get_file.__name__ = "get_file"
    monkeypatch.setitem(
        tool_hub.TOOL_GROUPS, "gitlab",
        [fake_get_file if f is gitlab.__dict__.get("get_file") or
         getattr(f, "__name__", "") == "get_file" else f
         for f in tool_hub.TOOL_GROUPS["gitlab"]])
    # 回寫 GitLab 的動作在 real 模式會發 HTTP,這裡不是要驗那個
    for fn in ("post_inline_comment", "post_summary", "set_commit_status", "set_label"):
        monkeypatch.setattr(gitlab, fn, lambda *a, **k: "ok")

    async def fake_agent(cfg, profile, system, user, hub, verbose=True,
                         use_tools=True, trace=None, meta=None):
        return '{"score": 90, "verdict": "approve", "summary": "相符。", "findings": []}'
    monkeypatch.setattr(pipeline, "run_agent", fake_agent)

    # **要真的走到沙盒**:回一個最小但合法的測資計畫,然後攔下 execute_cases
    # 看它實際收到什麼 SQL(原本回空結果,沙盒那條路根本沒被走到,
    # 所以「執行驗證也拿到整份檔」這件事其實沒有被驗過)。
    async def one_case(*a, **k):
        return ({"schema_ddl": ["CREATE TABLE transactions (account_id INT, "
                                "amount INT, tx_time DATETIME2, tx_type NVARCHAR(10), "
                                "channel NVARCHAR(10), eod_balance INT)"],
                 "conditions": [{"id": "C1", "desc": "提領累計達門檻"}],
                 "cases": [{"case_id": "C1-T", "condition_id": "C1",
                            "direction": "true", "expect_flagged": True, "note": "",
                            "rows": [["transactions", 1, 100000,
                                      "2026-06-01 10:00:00", "WITHDRAW", "ATM", 500]]}]},
                [], [])
    monkeypatch.setattr(spec_exec, "generate_cases", one_case)

    sandbox_sql: dict = {}

    def fake_execute(sql, plan):
        sandbox_sql["sql"] = sql
        return {"engine": "stub(不連資料庫)", "sandbox_error": None,
                "testdata_error": None, "sql_error": None,
                "case_results": [{"case_id": "C1-T"}], "mismatches": []}
    monkeypatch.setattr(spec_exec, "execute_cases", fake_execute)

    captured: dict = {}
    report = asyncio.run(review_or_raise(pipeline, load_config(), captured))
    return report, captured.get("prescan_raw") or [], sandbox_sql.get("sql"), spec_reads


def review_or_raise(pipeline, cfg, captured):
    return pipeline.review_mr(cfg, "900", capture=captured)


def test_大檔只改一行_預掃要拿到整份檔而不是那一行(monkeypatch, tmp_path):
    """#22 的完成條件:真實模式下預掃解析成功。

    修正前送進預掃的是從 diff 還原的 `AND eod_balance < 1000`——sqlglot 連
    SELECT 都看不到,解析失敗,AST 規則整組沒跑。而這個缺口 golden set 量不到,
    因為 golden case 都有 full_content。
    """
    _, pre, _, _ = _review_real(monkeypatch, tmp_path, serve_full=True)
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
    report, pre, _, _ = _review_real(monkeypatch, tmp_path, serve_full=False)
    assert pre and "parse_error" in pre[0], "拿不到完整檔卻解析成功了?測試設計有問題"
    titles = [f.get("title") for f in report.get("findings", [])]
    assert any("預掃無法解析" in str(t) for t in titles), (
        f"解析失敗沒有被揭露,findings = {titles}")


# ─────────── 取不到整份新檔的四種情況:唯讀 token、單檔上限、
#             沒釘住版本、以及跨執行緒不可互相污染 ───────────

def test_讀程式碼用唯讀token(real, seen_ref, monkeypatch):
    """依 #20／#22 留言的規矩:讀程式碼用唯讀的 GITLAB_READ_TOKEN。"""
    sent = {}

    def _get(url, **kw):
        sent.update(kw.get("headers") or {})

        class _R:
            status_code = 200
            text = "x"
            content = b"x"

            def raise_for_status(self):
                pass
        return _R()

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(get=_get))
    gitlab.pin_review_ref(SHA)
    gitlab.get_file("sql/x.sql")
    assert sent.get("PRIVATE-TOKEN") == "ro", "讀檔用了有寫入權的 token"


def test_沒有唯讀token時不退回寫入用的那把(real, monkeypatch):
    """沒設定就跟拿不到 head_sha 一樣:標成取不到、交人工,不要降級去用寫入 token。"""
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", "")
    called = []
    monkeypatch.setitem(sys.modules, "httpx",
                        types.SimpleNamespace(get=lambda *a, **k: called.append(1)))
    gitlab.pin_review_ref(SHA)
    out = gitlab.get_file("sql/x.sql")
    assert "GITLAB_READ_TOKEN" in out, out
    assert not called, "沒有唯讀 token 還是發了請求"


def test_超過單檔上限就不回內容(real, monkeypatch):
    """不要讓一個超大的檔案拖住預掃、注入掃描與沙盒(與審查流程的時間上限同一個原則)。"""
    big = "x" * 200

    def _get(url, **kw):
        class _R:
            status_code = 200
            text = big
            content = big.encode()

            def raise_for_status(self):
                pass
        return _R()

    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(get=_get))
    monkeypatch.setattr(gitlab, "MAX_FILE_BYTES", 100)
    gitlab.pin_review_ref(SHA)
    out = gitlab.get_file("sql/x.sql")
    assert "超過單檔上限" in out and big not in out, out


def test_釘住的版本不會跨執行緒污染():
    """webhook 每個 MR 開一條執行緒同時審:模組層級的全域變數會讓 A 讀到 B 的 sha
    ——兩份 MR 的內容互相污染,而且報告上看不出來。"""
    import threading

    seen = {}
    start = threading.Barrier(2)

    def worker(name, sha):
        gitlab.pin_review_ref(sha)
        start.wait(timeout=5)        # 兩條都設定完才讀,確保真的同時存在
        seen[name] = gitlab.review_ref()

    a = threading.Thread(target=worker, args=("A", "a" * 40))
    b = threading.Thread(target=worker, args=("B", "b" * 40))
    a.start(); b.start(); a.join(5); b.join(5)
    assert seen == {"A": "a" * 40, "B": "b" * 40}, seen


def test_取不到完整內容的檔案要交人工():
    """`full_content_unavailable` 原本沒有任何地方讀它。

    這些檔案只有 diff 那幾行進得了預掃與沙盒,大檔小改時還原出來可能只剩一行
    ——等於沒被實質審查過,不可以就這樣放行。
    """
    from orchestrator import pipeline
    mr = {"files": [{"path": "sql/big.sql", "full_content_unavailable": True},
                    {"path": "sql/ok.sql", "full_content": "SELECT 1"}]}
    out = pipeline.enforce_unreviewable({"findings": []}, mr)
    assert out["findings"], "取不到完整內容卻沒有補 finding"
    f = out["findings"][0]
    assert f["severity"] == "major"
    assert "sql/big.sql" in f["detail"] and "sql/ok.sql" not in f["detail"]


def test_同時標了unreviewable的檔案不重複列():
    """`unreviewable`(diff 都拿不到)比「只拿不到完整內容」更嚴重,列一次就好。"""
    from orchestrator import pipeline
    mr = {"files": [{"path": "a.sql", "unreviewable": True,
                     "full_content_unavailable": True}]}
    out = pipeline.enforce_unreviewable({"findings": []}, mr)
    assert out["findings"][0]["detail"].count("a.sql") == 1


def test_沙盒實際收到整份SQL(monkeypatch, tmp_path):
    """#22 的完成條件第 3 項:執行驗證也要拿到整份檔。

    原本的端到端測試把 `generate_cases` 換成回傳空結果,所以**沙盒那條路根本沒走到**
    ——「執行驗證也拿到整份檔」這件事沒有被驗過。
    """
    _, _, sql, _ = _review_real(monkeypatch, tmp_path, serve_full=True)
    assert sql, "執行驗證沒有跑到沙盒"
    assert "WITH base AS" in sql and "FROM transactions" in sql, (
        "沙盒收到的不是整份檔:" + repr(sql[:120]))
    assert sql.count("\n") > 5, "看起來只有 diff 那幾行"


def test_拿不到完整檔時沙盒收到的只有diff那幾行(monkeypatch, tmp_path):
    """反面對照:證明上一條真的分得出差別,而不是恰好都綠。"""
    _, _, sql, _ = _review_real(monkeypatch, tmp_path, serve_full=False)
    assert sql is None or "WITH base AS" not in sql, (
        "拿不到完整檔卻送了整份 SQL 進沙盒?測試設計有問題")


def test_補full_content時也要受單檔上限限制(real, monkeypatch):
    """`get_file`(模型用的)與 `_file_at`(補 full_content 用的)是兩條路,
    兩條都要有上限。原本只有前者有,後者的突變在驗證時**存活**了。

    超過上限就當成取不到 → `full_content_unavailable` → 交人工,不要讓一個
    超大的檔案拖住預掃、注入掃描與沙盒。
    """
    big = "x" * 5000

    def _get(url, **kw):
        class _R:
            status_code = 200
            text = big
            content = big.encode()

            def raise_for_status(self):
                pass
        return _R()

    # 這個測試要驗真正的 _file_at,所以不能用 real fixture 換掉的那個假的
    monkeypatch.undo()
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", "http://gl")
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", "ro")
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", "1")
    monkeypatch.setattr(gitlab, "MAX_FILE_BYTES", 100)
    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(get=_get))
    assert gitlab._file_at("sql/big.sql", SHA) is None, "超過上限卻回了內容"

    monkeypatch.setattr(gitlab, "MAX_FILE_BYTES", 10000)
    assert gitlab._file_at("sql/big.sql", SHA) == big, "沒超過上限卻不回內容"


def test_real模式的規格從GitLab讀而不是工具內建的那份(monkeypatch, tmp_path):
    """工具內建的 `specs/` 是評測與離線開發用的樣本,不是正式環境的規格。

    先讀本地的話,MR 改了 `specs/R-093.md` 時執行驗證拿到的還是內建那份 ——
    與「規格讀正在審的那一版」的決定互相矛盾,而且報告上看不出用的是哪一份。
    """
    _, _, sql, spec_reads = _review_real(monkeypatch, tmp_path, serve_full=True)
    assert any(p.startswith("specs/") for p, _ in spec_reads), (
        f"沒有從 GitLab 讀規格(讀取紀錄:{spec_reads})——"
        "real 模式讀到工具內建的那份了")
    assert sql, "規格沒讀到的話執行驗證不會跑"


def test_刪除的檔案不算取不到(real):
    """刪掉的檔在 head 上本來就不存在,`_file_at` 會拿到 404。

    當成「取不到完整內容」的話,**任何刪檔的 MR 都不會自動放行**,而且訊息是錯的
    (會說「可能是拿不到被審的 commit」)。
    """
    real["api_pages"] = ([{"new_path": "sql/keep.sql", "old_path": "sql/keep.sql",
                           "diff": "+x"},
                          {"new_path": "sql/gone.sql", "old_path": "sql/gone.sql",
                           "diff": "-y", "deleted_file": True}], False)
    out = json.loads(gitlab.get_mr_diff("7"))
    gone = [f for f in out["files"] if f["path"] == "sql/gone.sql"][0]
    assert gone["deleted"] is True
    assert "full_content_unavailable" not in gone, "刪除的檔被當成取不到了"
    assert ("sql/gone.sql", SHA) not in real["fetched"], "去抓已刪除的檔了"


def test_模型呼叫get_mr_diff不會換掉釘住的版本(real):
    """`get_mr_diff` 在模型的工具清單裡。審 !7 時模型若被 MR 描述引導去呼叫
    `get_mr_diff("8")`,釘住的 sha 若跟著換,之後 `get_file` 看到的就是別的 MR
    的檔案,連補讀規格也會讀錯版本。釘版本的動作屬於 pipeline,不屬於這個工具。
    """
    gitlab.pin_review_ref("a" * 40)
    gitlab.get_mr_diff("8")
    assert gitlab.review_ref() == "a" * 40, "工具呼叫把釘住的版本換掉了"


def test_只拿不到完整檔時不要說成完全沒審查():
    """取不到完整新檔的檔案其實有被審(diff 那幾行進了預掃與模型),只是看不到
    全貌。說成「沒有經過任何審查」會讓判讀的人誤判嚴重程度。"""
    from orchestrator import pipeline
    out = pipeline.enforce_unreviewable(
        {"findings": []},
        {"files": [{"path": "a.sql", "full_content_unavailable": True}]})
    detail = out["findings"][0]["detail"]
    assert "只看得到 diff 的片段" in detail
    assert "沒有經過注入掃描" not in detail


def test_完全拿不到內容時仍然要說沒經過審查():
    from orchestrator import pipeline
    out = pipeline.enforce_unreviewable(
        {"findings": []}, {"files": [{"path": "a.sql", "unreviewable": True}]})
    assert "沒有經過注入掃描" in out["findings"][0]["detail"]


def test_兩把token設成同一把等於沒有唯讀隔離(real, monkeypatch):
    """設成同一把只是看起來有設定:讀檔仍然帶著可以寫入的權限。

    打包下載(download_archive)本來就拒絕這種設法,讀檔也照做才一致。
    """
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", "same")
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", "same")
    called = []
    monkeypatch.setitem(sys.modules, "httpx",
                        types.SimpleNamespace(get=lambda *a, **k: called.append(1)))
    gitlab.pin_review_ref(SHA)
    out = gitlab.get_file("sql/x.sql")
    assert "GITLAB_READ_TOKEN" in out, out
    assert not called, "兩把一樣卻還是用它發了請求"
    assert gitlab._read_token() is None
