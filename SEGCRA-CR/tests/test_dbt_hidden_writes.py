"""dbt 樣板裡沒有經過規則檢查的寫入語句。

規則層看的是展開後的 SQL。樣板可以拿展開器跟 dbt 真正執行時的差異當條件,把寫入語句藏在審查時
不會進的分支。這裡看樣板原文:固定文字裡的寫入關鍵字插記號再展開,記號沒出現就是沒展開到;
Jinja 標記字串裡的寫入關鍵字(hook、set)展開結果看不到,一律報。

展開都跑真的(同一個行程,正式路徑是隔離子行程)。
"""
import asyncio
import json
import time

import pytest

from orchestrator import dbt_hidden_writes as hw
from orchestrator import pipeline
from orchestrator.config import DBT_DATABASE_PLACEHOLDER, load_config
from orchestrator.dbt_render import RenderResult, render_model

DB = DBT_DATABASE_PLACEHOLDER
CFG = {"enabled": True, "database": DB}


def _render(source, incremental=False):
    return render_model("models/m.sql", source=source, database=DB,
                        **({"incremental": True} if incremental else {}))


def _check(source, *, incremental=None, calls=None):
    """跟預掃一樣:先正常展開(需要時增量也一次),再交給 find_hidden_writes。"""
    clean = {False: _render(source)}
    if incremental if incremental is not None else clean[False].incremental_checked:
        clean[True] = _render(source, True)
    assert all(r.ok for r in clean.values()), [r.error for r in clean.values()]

    def render(marked, inc):
        if calls is not None:
            calls.append(inc)
        return _render(marked, inc)
    return hw.find_hidden_writes(source, {k: v.sql for k, v in clean.items()}, render)


# ─────────────────── 藏在沒有展開到的分支 ───────────────────

def test_拿執行期的值當條件藏的寫入語句會被標出來():
    """審查時條件不成立、dbt 執行時可能成立的寫法。不依賴展開器給的實際值。"""
    src = "SELECT 1 AS a\n{% if invocation_id == 'never' %};DELETE FROM t{% endif %}"
    assert _check(src) == {hw.UNREACHED: [{"line": 2, "keyword": "DELETE"}]}


@pytest.mark.parametrize("cond", ["false", "this is undefined", "var('x', 0) == 7",
                                  "execute and not execute"])
def test_不論用什麼條件藏都會被標出來(cond):
    src = f"SELECT 1 AS a\n{{% if {cond} %}}\n;TRUNCATE TABLE t\n{{% endif %}}"
    assert _check(src) == {hw.UNREACHED: [{"line": 3, "keyword": "TRUNCATE"}]}


def test_this_is_defined_的語句一般模式就展開到_不報():
    """dbt 每次執行都有 this;展開器兩種模式都提供(增量分支掃描那時處理),規則層看得到。"""
    src = "SELECT 1 AS a\n{% if this is defined %};DELETE FROM t{% endif %}"
    assert _check(src) == {}


def test_增量分支的寫入語句在增量模式展開到_不報():
    src = "SELECT 1 AS a FROM s\n{% if is_incremental() %};DELETE FROM t WHERE d = 1{% endif %}"
    assert _check(src) == {}
    # 只看一般模式時就是沒展開到:增量模式那次一定要算進來
    assert _check(src, incremental=False) == {hw.UNREACHED: [{"line": 2, "keyword": "DELETE"}]}


def test_正常的增量model不報_也不多展開():
    src = ("{{ config(materialized='incremental', unique_key='id') }}\n"
           "SELECT id, amount FROM {{ source('raw', 'txn') }}\n"
           "{% if is_incremental() %}\nWHERE id > (SELECT MAX(id) FROM {{ this }})\n{% endif %}")
    calls = []
    assert _check(src, calls=calls) == {}
    assert calls == []                      # 原文沒有寫入關鍵字:不用插記號再展開


@pytest.mark.parametrize("cond", [
    "this.startswith is not defined",       # dbt 的 Relation 沒有字串的方法
    "this.keys is defined",                 # dbt 的 Relation 是 mapping
    "ref('x').split is not defined",
    "target.schema != 'dbo'",               # 展開器給固定值的 target.*
    "this | length > 100",
])
def test_拿展開器跟dbt分得出來的地方當條件_不會靜悄悄通過(cond):
    """展開器把這些寫法當成展開失敗(另外補 major)也可以;展開成功時,藏起來的語句一定要被標出來。"""
    src = f"SELECT 1 AS a\n{{% if {cond} %}}\n;DELETE FROM t\n{{% endif %}}"
    clean = _render(src)
    if not clean.ok:
        return                              # 展開失敗由 enforce_dbt_render_failure 補 major
    out = _check(src)
    if out != {}:
        assert out == {hw.UNREACHED: [{"line": 3, "keyword": "DELETE"}]}
    else:                                   # 審查時條件成立:DELETE 展開到了,規則層看得到
        assert "DELETE" in clean.sql


def test_迴圈一次都沒跑時算沒有展開到():
    src = "SELECT 1 AS a\n{% for t in [] %}\n;DROP TABLE {{ t }}\n{% endfor %}"
    assert _check(src) == {hw.UNREACHED: [{"line": 3, "keyword": "DROP"}]}


def test_關鍵字拆開寫也找得到():
    src = "SELECT 1 AS a\n{% if false %};DEL{{ '' }}ETE FROM t{% endif %}"
    assert _check(src) == {hw.UNREACHED: [{"line": 2, "keyword": "DELETE"}]}


def test_接在標記後面的關鍵字也找得到():
    """各段固定文字分開找:接起來時 `x{{ y }}DELETE` 會變成 `xDELETE`,不像關鍵字。"""
    src = "SELECT 1 AS a\n{% if false %};x{{ '' }}DELETE FROM t{% endif %}"
    assert _check(src) == {hw.UNREACHED: [{"line": 2, "keyword": "DELETE"}]}


def test_一般模式與增量模式都展開到的不報_各自只在一邊的也不報():
    src = ("SELECT 1 AS a\n{% if is_incremental() %};DELETE FROM t WHERE x = 1"
           "{% else %};INSERT INTO t VALUES (1){% endif %}")
    assert _check(src) == {}


def test_有展開到的跟沒展開到的分開列_行號各自正確():
    src = ("SELECT 1 AS a;\nUPDATE t SET a = 1 WHERE b = 2;\n"
           "{% if false %}\nMERGE INTO t USING s ON 1 = 1 WHEN MATCHED THEN DELETE;\n{% endif %}")
    assert _check(src) == {hw.UNREACHED: [{"line": 4, "keyword": "MERGE"},
                                          {"line": 4, "keyword": "DELETE"}]}


def test_大小寫轉換的區塊也認得記號():
    """`{% filter upper %}` 會把記號轉成大寫。"""
    src = "{% filter upper %}SELECT 1 AS a; delete from t where x = 1{% endfilter %}"
    assert _check(src) == {}


# ─────────────────── 寫在 Jinja 標記的字串裡 ───────────────────

@pytest.mark.parametrize("src,kw", [
    ('{{ config(post_hook="DELETE FROM t WHERE d < 1") }}\nSELECT 1 AS a', "DELETE"),
    ("{{ config(pre_hook=['grant select on t to r']) }}\nSELECT 1 AS a", "GRANT"),
    ("{% set q = 'drop table t' %}\nSELECT 1 AS a", "DROP"),
])
def test_寫在標記字串裡的寫入語句一律報(src, kw):
    assert _check(src) == {hw.IN_TAGS: [{"line": 1, "keyword": kw}]}


@pytest.mark.parametrize("src", [
    "{% if execute %}SELECT 1 AS a{% else %}SELECT 2 AS a{% endif %}",        # dbt 的變數
    "SELECT '{{ var(\"update_mode\", \"x\") }}' AS m",                       # 名稱的一部分
    "{# DELETE FROM t:舊寫法 #}\nSELECT 1 AS a",                               # Jinja 註解
    "SELECT updated_at, created_by, @delete_flag AS f, #drop_tmp.x FROM t",   # 識別字
    "SELECT is_delete, soft_delete, t.grant FROM t",                         # 前面接著識別字
])
def test_不是寫入語句的不報(src):
    assert _check(src) == {}


@pytest.mark.parametrize("name", ["updated_at", "is_delete", "soft_delete", "t.grant_x",
                                  "@delete_flag", "#drop_tmp", "$exec", "deleted2"])
def test_識別字裡的關鍵字不算_放在沒展開到的分支也不報(name):
    assert hw.occurrences(f"SELECT {name} FROM t") == ([], [])
    assert _check("SELECT 1 AS a\n{% if false %}SELECT " + name + " FROM t{% endif %}") == {}


def test_固定文字的SQL註解有展開到就不報():
    """SQL 註解 dbt 會照樣輸出,規則層看得到;只有沒展開到的才報。"""
    assert _check("-- 每日 delete 前先備份\nSELECT 1 AS a") == {}


# ─────────────────── 無法確認的情況 ───────────────────

def test_樣板偵測自己被插了記號而改變輸出時_記成無法確認():
    """記號出現了不代表規則層看到同一句:去掉記號後要跟第一次的展開結果相同。"""
    src = ("{% set b %}DELETE FROM t WHERE x = 1{% endset %}"
           "{% if '/*' in b %}{{ b }}{% endif %}\nSELECT 1 AS a")
    assert set(_check(src)) == {hw.UNVERIFIED}


def test_只差在uuid格式的值不算不同():
    """invocation_id 每次展開都不同;只差在 uuid 的兩份輸出結構相同。"""
    import uuid
    src = "SELECT 1 AS a; DELETE FROM t WHERE x = 1"
    clean = f"SELECT '{uuid.uuid4()}' AS a; DELETE FROM t WHERE x = 1"

    def render(marked, inc):
        return RenderResult(ok=True, sql=marked.replace("SELECT 1", f"SELECT '{uuid.uuid4()}'"))
    assert hw.find_hidden_writes(src, {False: clean}, render) == {}


@pytest.mark.parametrize("result", [
    lambda marked: RenderResult(ok=False, error="x", sql=marked),   # 帶著輸出也不能用
    lambda marked: RenderResult(ok=True, sql=None),
])
def test_加了記號後展開失敗時記成無法確認(result):
    src = "SELECT 1; DELETE FROM t WHERE x = 1"
    out = hw.find_hidden_writes(src, {False: src}, lambda marked, inc: result(marked))
    assert set(out) == {hw.UNVERIFIED}


def test_增量模式那次不同也記成無法確認():
    src = "SELECT 1; DELETE FROM t WHERE x = 1"

    def render(marked, inc):
        sql = hw._marker_re("t0").sub("", marked)
        return RenderResult(ok=True, sql=marked if not inc else sql + " -- 不一樣")
    out = hw.find_hidden_writes(src, {False: src, True: src}, render, token="t0")
    assert set(out) == {hw.UNVERIFIED}


def test_關鍵字太多時不逐一確認_也不展開():
    calls = []
    src = "SELECT 1;" + " DELETE FROM t WHERE x = 1;" * (hw.MAX_MARKED_KEYWORDS + 1)
    out = hw.find_hidden_writes(src, {False: src}, lambda s, i: calls.append(i))
    assert set(out) == {hw.UNVERIFIED} and calls == []
    # 剛好在上限內照常確認
    ok = "SELECT 1;" + " DELETE FROM t WHERE x = 1;" * hw.MAX_MARKED_KEYWORDS
    assert _check(ok) == {}


# ─────────────────── 結果與效能 ───────────────────

def test_結果不帶原文_也不帶記號():
    src = "SELECT 1 AS a\n{% if false %};DELETE FROM ZZ_SECRET_TABLE_ZZ{% endif %}"
    out = json.dumps(_check(src), ensure_ascii=False)
    assert "ZZ_SECRET_TABLE_ZZ" not in out and "SEGCRA" not in out


def test_記號每次不同():
    found = [(5, "DELETE")]
    assert hw.mark("SELECT DELETE", found, "aa") != hw.mark("SELECT DELETE", found, "bb")
    seen = []
    hw.find_hidden_writes("SELECT 1; DELETE FROM t WHERE x = 1", {False: ""},
                          lambda s, i: seen.append(s) or RenderResult(ok=False))
    hw.find_hidden_writes("SELECT 1; DELETE FROM t WHERE x = 1", {False: ""},
                          lambda s, i: seen.append(s) or RenderResult(ok=False))
    assert seen[0] != seen[1]


@pytest.mark.parametrize("src", ["{{ x", "{% if 'a", "SELECT '{{ \"DELETE", "{#", "", "{{}}"])
def test_沒有結尾的標記與字串不會出錯(src):
    hw.occurrences(src)


def test_很長的原文是線性時間():
    src = ("SELECT a {{ x }} '{% set y = \"z\" %}' {# c #}\n" * 40_000)
    start = time.monotonic()
    hw.occurrences(src)
    assert time.monotonic() - start < 5


# ─────────────────── 接進預掃與後處理 ───────────────────

class _Hub:
    def __init__(self):
        self.sqls = []

    async def call_json(self, name, args):
        if name == "sqltools__run_rules":
            self.sqls.append(args["sql"])
        return []

    async def call(self, name, args):
        return ""


@pytest.fixture
def _in_process(monkeypatch):
    calls = []

    def render(*args, **kwargs):
        calls.append(kwargs.get("incremental", False))
        return render_model(*args, **kwargs)
    monkeypatch.setattr(pipeline, "render_model_isolated", render)
    return calls


def _prescan(content, path="models/m.sql"):
    return asyncio.run(pipeline.prescan(_Hub(), [{"path": path, "full_content": content}],
                                        CFG))[0]


HIDDEN = "SELECT 1 AS a\n{% if invocation_id == 'never' %}\n;DELETE FROM t\n{% endif %}"


def test_預掃記下沒有展開到的寫入語句(_in_process):
    entry = _prescan(HIDDEN)
    assert entry["dbt_hidden_writes"] == {hw.UNREACHED: [{"line": 3, "keyword": "DELETE"}]}
    assert _in_process == [False, False]


def test_預掃的檢查本身出錯時記成無法確認(_in_process, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("壞了")
    monkeypatch.setattr(pipeline, "find_hidden_writes", boom)
    entry = _prescan(HIDDEN)
    assert set(entry["dbt_hidden_writes"]) == {hw.UNVERIFIED}
    assert "RuntimeError" in entry["dbt_hidden_writes"][hw.UNVERIFIED]


def test_展開失敗時不做這項檢查(_in_process):
    entry = _prescan("SELECT {{ undefined_macro() }}\n{% if false %};DELETE FROM t{% endif %}")
    assert "dbt_render_error" in entry and "dbt_hidden_writes" not in entry


def test_預掃的增量分支寫入語句不報(_in_process):
    """增量那次的展開結果也要交給檢查:只看一般模式時,這句會被當成沒展開到。"""
    entry = _prescan("SELECT 1 AS a\n{% if is_incremental() %}\n;DELETE FROM t WHERE d = 1\n"
                     "{% endif %}")
    assert "dbt_hidden_writes" not in entry
    assert _in_process == [False, True, False, True]


def test_再展開時帶同一份專案資訊(monkeypatch):
    """專案的 macro 與 ref()/source() 的驗證要跟第一次展開一樣,否則插記號那次會展開失敗或不同。"""
    seen = []

    def render(*args, **kwargs):
        seen.append(kwargs)
        return RenderResult(ok=True, sql=kwargs["source"])
    monkeypatch.setattr(pipeline, "render_model_isolated", render)

    class _Project:
        macro_paths = ("macros",)

    class _Ctx:
        project, macro_files, info = _Project(), {"macros/a.sql": "x"}, {"k": 1}
    src = "SELECT 1; DELETE FROM t WHERE x = 1"
    expanded = pipeline._Expanded(src, None, None)
    assert pipeline._hidden_writes("models/m.sql", src, expanded, CFG, _Ctx()) == {}
    assert seen and all(k["macro_files"] == {"macros/a.sql": "x"} and k["project"] == {"k": 1}
                        and k["macro_dirs"] == ("macros",) and k["database"] == DB for k in seen)


def test_沒有寫入語句的樣板預掃結果沒有這個欄位(_in_process):
    entry = _prescan("SELECT a FROM {{ source('raw', 'txn') }}")
    assert "dbt_hidden_writes" not in entry
    assert _in_process == [False]


def test_不是樣板的SQL不做這項檢查(_in_process):
    entry = _prescan("SELECT 1; DELETE FROM t WHERE x = 1", path="sql/x.sql")
    assert "dbt_hidden_writes" not in entry and _in_process == []


def _finding(report):
    return [f for f in report["findings"] if f["title"] == pipeline._HIDDEN_WRITE_TITLE]


def test_補一條major_寫出關鍵字與行號():
    pre = [{"path": "models/m.sql", "rules": [],
            "dbt_hidden_writes": {hw.UNREACHED: [{"line": 3, "keyword": "DELETE"}],
                                  hw.IN_TAGS: [{"line": 1, "keyword": "GRANT"}]}}]
    [f] = _finding(pipeline.enforce_dbt_hidden_writes({"findings": []}, pre))
    assert (f["file"], f["line"], f["severity"]) == ("models/m.sql", 1, "major")
    assert "DELETE(第 3 行)" in f["detail"] and "GRANT(第 1 行)" in f["detail"]
    assert "不得自動放行" in f["detail"]


def test_無法確認時寫出原因_行號是0():
    pre = [{"path": "models/m.sql", "dbt_hidden_writes": {hw.UNVERIFIED: "原因"}}]
    [f] = _finding(pipeline.enforce_dbt_hidden_writes({"findings": []}, pre))
    assert f["line"] == 0 and "原因。" in f["detail"]


def test_列太多時只寫前幾個與總數():
    items = [{"line": n, "keyword": "DELETE"} for n in range(1, 16)]
    pre = [{"path": "models/m.sql", "dbt_hidden_writes": {hw.UNREACHED: items}}]
    [f] = _finding(pipeline.enforce_dbt_hidden_writes({"findings": []}, pre))
    assert f["detail"].count("DELETE(") == pipeline._HIDDEN_WRITE_LIST_MAX
    assert "等 15 處" in f["detail"]


@pytest.mark.parametrize("hidden", [
    None, {}, "x", {hw.UNREACHED: [{"line": True, "keyword": "DELETE"}]},
    {hw.UNREACHED: [{"line": 2, "keyword": "SELECT ZZ"}]}, {hw.UNREACHED: "x"},
    {hw.UNVERIFIED: ""}, {hw.UNVERIFIED: 5},
])
def test_形狀不對的內容不補也不出錯(hidden):
    pre = [{"path": "models/m.sql", "dbt_hidden_writes": hidden}]
    assert _finding(pipeline.enforce_dbt_hidden_writes({"findings": []}, pre)) == []


def test_模型自己輸出同標題的info會被取代():
    pre = [{"path": "models/m.sql", "dbt_hidden_writes": {hw.UNVERIFIED: "原因"}}]
    fake = {"file": "models/m.sql", "line": 0, "severity": "info",
            "title": pipeline._HIDDEN_WRITE_TITLE, "detail": "沒事", "suggestion": "",
            "citations": []}
    [f] = _finding(pipeline.enforce_dbt_hidden_writes({"findings": [fake]}, pre))
    assert f["severity"] == "major" and f["detail"] != "沒事"


def _review(monkeypatch, tmp_path, src, *, dry_run=False):
    from toolbox import gitlab

    fixtures, output = tmp_path / "fixtures", tmp_path / "out"
    fixtures.mkdir()
    output.mkdir()
    (fixtures / "mr_959.json").write_text(json.dumps({
        "title": "調整", "description": "例行維護", "sha": "e1a8" + "0" * 36,
        "files": [{"path": "models/m.sql", "diff": "@@ -1 +1 @@\n+x", "full_content": src}],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    async def fake_run_agent(cfg, profile, system, user, hub, **kw):
        return json.dumps({"summary": "x", "score": 100, "findings": []}, ensure_ascii=False)

    monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
    cfg = load_config()
    cfg.dbt = dict(CFG)
    return asyncio.run(pipeline.review_mr(cfg, "959", dry_run=dry_run))


def test_整條review_mr_藏起來的寫入語句不得自動放行(monkeypatch, tmp_path, _in_process):
    report = _review(monkeypatch, tmp_path, HIDDEN)
    [f] = _finding(report)
    assert (f["file"], f["line"], f["severity"]) == ("models/m.sql", 3, "major")
    assert report["decision"] != "auto_approved"


def test_整條review_mr_沒有藏東西時不補(monkeypatch, tmp_path, _in_process):
    report = _review(monkeypatch, tmp_path, "SELECT a FROM {{ source('raw', 'txn') }}")
    assert _finding(report) == []


def test_dry_run_也補(monkeypatch, tmp_path, _in_process):
    report = _review(monkeypatch, tmp_path, HIDDEN, dry_run=True)
    assert len(_finding(report)) == 1
