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
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator.pipeline import enforce_rules          # noqa: E402
from toolbox.sqltools import _text_rules, run_rules      # noqa: E402

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
def test_整個預掃結果不含憑證原文以外的回顯(sql, secret, _kind):
    """預掃結果本身會整包放進 prompt,但那沒問題——SQL 原文本來就在 prompt 裡。
    這條確認的是**訊息欄位**乾淨:它是唯一會被複製進 finding 與 MR 留言的部分。"""
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
