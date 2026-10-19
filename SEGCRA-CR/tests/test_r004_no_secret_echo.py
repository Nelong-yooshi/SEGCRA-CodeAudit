"""R004 的訊息不得回顯比對到的憑證原文 — 不呼叫 LLM、不連資料庫。

**為什麼是問題**:憑證本來就在 MR 的 diff 裡,所以 R004 命中時回顯那段文字**不是新的
外洩**;問題是這段訊息會走到三個地方——送進模型的預掃結果、`enforce_rules` 補的 finding
(標題取訊息前 60 字,所以標題與內文都帶著)、以及**貼回 MR 的行內留言與 `review_output/`
的結果檔**。作者把那一行改掉之後,diff 裡的憑證消失了,**留言與結果檔裡還留著一份**。

其他程式補報的 finding 一律不回顯作者寫的文字(注入掃描、寫入工具、截斷、同檔多處命中),
這一條要一致。

連**鍵名**都不原樣回顯:大小寫與 `api-key`/`api_key` 這些寫法也是作者的文字,而我們自己的
分類標籤(「API 金鑰類的鍵指定了字串常值」)資訊量一樣夠。
"""
import pytest

from orchestrator.pipeline import enforce_parse, enforce_rules
from toolbox.sqltools import _text_rules, parse_error_text, run_rules

# 假的值:每一個都夠特殊,出現在訊息裡一定抓得到
CASES = [
    ("SELECT 1 WHERE api_key = 'FAKE-EXAMPLE-KEY-000'", "FAKE-EXAMPLE-KEY-000", "API 金鑰"),
    ("SELECT * FROM OPENQUERY(l, 'mssql://svc_user:FAKEPASS000@db.example.invalid/x')",
     "FAKEPASS000", "連線字串"),
    ("EXEC sp_do @password = 'FAKEPW000ZZ'", "FAKEPW000ZZ", "密碼"),
    ("SELECT 1 WHERE ACCESS_KEY = 'FAKEAK000ZZ'", "FAKEAK000ZZ", "存取金鑰"),
    ("SELECT 1 WHERE auth-token = 'FAKETK000ZZ'", "FAKETK000ZZ", "存取權杖"),
    ("SELECT 1 WHERE credential = 'FAKECRED000'", "FAKECRED000", "憑證"),
]


def _r004(sql):
    return [h for h in _text_rules(sql) if h[0] == "R004"]


@pytest.mark.parametrize("sql,secret,kind", CASES)
def test_訊息不含憑證原文而且說得出種類(sql, secret, kind):
    [(_, severity, message)] = _r004(sql)
    assert severity == "blocker", "資安紅線,一律 blocker"
    assert secret not in message, f"訊息回顯了憑證:{message}"
    assert kind in message, message


@pytest.mark.parametrize("sql,secret,_kind", CASES)
def test_補出來的finding不含憑證原文(sql, secret, _kind):
    """走到底:`run_rules` → 預掃命中 → `enforce_rules` 補的 finding(標題與內文)。
    那條 finding 會被貼回 MR 留言,所以這裡才是真正要守的地方。"""
    import json
    hits = json.loads(run_rules(sql))
    assert any(h["rule"] == "R004" for h in hits), hits
    pre = [{"path": "a.sql", "rules": hits}]
    report = enforce_rules({"findings": []}, pre)
    r004 = [f for f in report["findings"] if "[R004]" in f["title"]]
    assert r004, [f["title"] for f in report["findings"]]
    for f in r004:
        blob = json.dumps(f, ensure_ascii=False)
        assert secret not in blob, f"finding 回顯了憑證:{blob}"


@pytest.mark.parametrize("sql,secret,_kind", CASES)
def test_R004_的訊息欄位不含憑證原文(sql, secret, _kind):
    """預掃結果本身會整包放進 prompt,但那沒問題——SQL 原文本來就在 prompt 裡。
    這條只驗 R004 的**訊息欄位**:它是會被複製進 finding 與 MR 留言的那一部分。"""
    import json
    for h in json.loads(run_rules(sql)):
        if h["rule"] == "R004":
            assert secret not in h["message"]


def test_鍵名的寫法不影響分類():
    """`API_KEY`、`api-key`、`apikey` 都要歸到同一類,而且訊息裡是我們的標籤、
    不是作者寫的那個字。"""
    for key in ("API_KEY", "api-key", "apikey", "Api_Key"):
        [(_, _, message)] = _r004(f"SELECT 1 WHERE {key} = 'FAKEVALUE000'")
        assert "API 金鑰" in message
        assert key not in message, message


def test_沒命中就不報():
    assert _r004("SELECT account_id FROM txn WHERE amount > 1000") == []
    # 鍵名像憑證但值不是字串常值(例如對欄位比較)→ 不是硬編碼
    assert _r004("SELECT 1 FROM users WHERE password = other_column") == []


# ───────── 同一份報告的其他 finding 也不能把憑證帶出去(#42 review) ─────────
#
# R004 的訊息乾淨了,但檔案解析不過時,「預掃無法解析此檔的 SQL」那條 finding 的內文
# 就是解析錯誤訊息 —— 而 sqlglot 的訊息帶著出錯位置附近的原文。那條一樣會貼回 MR 留言、
# 寫進 review_output/,所以同一個問題還在。
#
# **要防的是「解析錯誤把原文帶出去」,跟那段原文像不像密碼無關。** 所以不是「把疑似
# 憑證的部分遮掉」——用樣式遮只遮得到樣式認得的寫法,下面 LEAKY 的前兩個就是 R004
# 漏抓、遮罩也會一起漏的例子(#42 review 第二輪)。改成只留位置。

SECRET = "S3cr3t-P@ss"
LEAKY = [
    # 少打一個字 → 解析失敗,錯誤訊息帶著那一行
    f"SELEC * FROM t WHERE pwd = '{SECRET}'",
    # T-SQL 的 Unicode 字串:R004 的樣式認不得 N'…',所以遮罩也會漏
    f"SELEC * FROM t WHERE password = N'{SECRET}'",
    # 位置參數:值沒有跟在鍵名後面,R004 一樣認不得
    f"EXEC sp_addlogin 'app', '{SECRET}' SELEC 1",
    # 沒加引號的值
    f"SELEC * FROM t WHERE password = {SECRET}",
    # 斷詞就失敗(引號沒收):走的是 ParseError 以外的例外
    f"SELECT 1 WHERE x = '{SECRET}",
]


@pytest.mark.parametrize("sql", LEAKY)
def test_整份報告都不含憑證原文(sql):
    """走預掃 → enforce_rules + enforce_parse,斷言**整份報告**(含解析失敗那條)
    都不含那串密碼。"""
    import json
    raw = json.loads(run_rules(sql))
    entry = {"path": "a.sql", "rules": raw["hits"] if isinstance(raw, dict) else raw}
    if isinstance(raw, dict) and raw.get("error"):
        entry["parse_error"] = raw["error"]
    report = enforce_parse(enforce_rules({"findings": []}, [entry]), [entry])
    blob = json.dumps(report, ensure_ascii=False)
    assert SECRET not in blob, blob[:400]
    assert "parse_error" in entry, "這幾個例子都該解析失敗,否則這條什麼都沒驗"


@pytest.mark.parametrize("sql", LEAKY)
def test_解析錯誤訊息只留位置(sql):
    """位置要留著 —— 不然看報告的人不知道哪裡壞了;原文一概不留。"""
    import json
    err = json.loads(run_rules(sql))["error"]
    assert SECRET not in err and "SELEC" not in err, err
    assert "語法無法解析" in err
    assert ("行第" in err) or ("Error" in err), f"要嘛有位置、要嘛有例外類型:{err}"


def test_解析錯誤不帶_sqlglot_的描述與前後文():
    """`ParseError.errors` 的 description / start_context / highlight / end_context
    都夾著 token 的原文,一概不用。"""
    import sqlglot
    try:
        sqlglot.parse(f"SELEC * FROM t WHERE pwd = '{SECRET}'", dialect="tsql")
        raise AssertionError("這段應該解析失敗")
    except Exception as e:
        raw = getattr(e, "errors", [{}])[0]
        text = parse_error_text(e)
    assert str(raw.get("description", "")) not in text
    for key in ("start_context", "highlight", "end_context"):
        val = str(raw.get(key) or "")
        if len(val) > 3:
            assert val not in text, key
    assert "第 1 行第 12 欄" in text


def test_不是_ParseError_時只留例外類型():
    class Boom(Exception):
        def __str__(self):
            return f"tokenizer blew up near '{SECRET}'"

    text = parse_error_text(Boom())
    assert SECRET not in text and "Boom" in text


def test_模型工具的解析錯誤不必處理():
    """`sqltools__parse_ast` 只回給模型,而模型本來就看得到整份 SQL —— 那條不會變成
    finding、不會貼回 MR,所以不另外處理(#42 review 第二輪確認)。"""
    import json

    from toolbox.sqltools import parse_ast
    out = json.loads(parse_ast(f"SELEC * FROM t WHERE pwd = '{SECRET}'"))
    assert "error" in out
