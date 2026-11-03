"""R004 抓得到哪些、會不會抓錯(#67)——不呼叫 LLM、不連資料庫。

#60 處理的是「訊息不要回顯憑證原文」,這張是**偵測範圍**,兩件事分開。

R004 是 blocker:漏抓等於密碼直接進正式環境,誤報則直接把 MR 擋下來。而 #48 之後
`.yml` / `.yaml` 只留文字層的命中,R004 是設定檔上唯一的憑證防線。
"""
import json

import pytest

from toolbox.sqltools import run_rules


def _codes(sql: str) -> list[str]:
    out = json.loads(run_rules(sql))
    hits = out["hits"] if isinstance(out, dict) else out
    return [h["rule"] for h in hits]


# ─────────────────── 1. 原本漏抓的寫法 ───────────────────

@pytest.mark.parametrize("sql,label", [
    ("CREATE LOGIN app WITH PASSWORD = N'6789'", "T-SQL 的 Unicode 字串前綴"),
    ("CREATE LOGIN app WITH PASSWORD = n'6789'", "小寫的 n 前綴"),
    ("SET @password = 6789", "數字沒加引號"),
    ("password: 6789", "YAML 不加引號的數字"),
    ("password: S3cr3tPass", "YAML 不加引號的字串"),
    ("api_key: sk-live-0000", "YAML 的 API 金鑰"),
    ("EXEC sp_addlogin 'app', '6789'", "位置參數,沒有鍵名"),
    ("EXEC sp_addlinkedsrvlogin 'srv', 'false', NULL, 'app', '6789'", "連結伺服器的登入"),
    ("SET @pwd = '12'", "少於 3 個字的值"),
])
def test_這些寫法都要抓到(sql, label):
    assert "R004" in _codes(sql), label


def test_抓到的是blocker():
    out = json.loads(run_rules("CREATE LOGIN app WITH PASSWORD = N'6789'"))
    hits = out["hits"] if isinstance(out, dict) else out
    assert [h["severity"] for h in hits if h["rule"] == "R004"] == ["blocker"]


# ─────────────────── 2. 原本誤報的寫法 ───────────────────

@pytest.mark.parametrize("sql,label", [
    ("password: \"{{ env_var('DB_PASSWORD') }}\"", "dbt 的 env_var"),
    ("password = '${DB_PASSWORD}'", "shell 風格的佔位符"),
    ("password: {{ var('pw') }}", "Jinja 變數"),
    ("SET @api_key = '${API_KEY}'", "佔位符放在 SQL 裡"),
])
def test_佔位符不是寫死的值(sql, label):
    """`{{ }}`、`${}`、`env_var(...)` 都是「去別的地方拿」。R004 是 blocker,
    這種誤報會直接把 MR 擋下來。"""
    assert "R004" not in _codes(sql), label


@pytest.mark.parametrize("sql,label", [
    ("SELECT id FROM t WHERE token = 'ACTIVE'", "欄位剛好叫 token,比對狀態值"),
    ("SELECT id FROM t WHERE u.secret = 'N'", "帶表別名的比較"),
    ("SELECT id FROM t JOIN u ON u.api_key = 'LEGACY'", "JOIN 條件裡的比較"),
    ("SELECT id FROM t WHERE a = 1 AND token = 'ACTIVE'", "AND 後面的比較"),
])
def test_欄位比較降成提醒不是blocker(sql, label):
    """`WHERE token = 'ACTIVE'` 是拿狀態值在比對,不是寫死憑證。降成 H004 交模型判斷 ——
    沒有被回應的檢核點照樣擋自動放行,所以不是靜靜放過。"""
    codes = _codes(sql)
    assert "R004" not in codes, label
    assert "H004" in codes, f"{label}:要留一個提醒給模型"


# ─────────────────── 3. 原本就正確的不能壞掉 ───────────────────

@pytest.mark.parametrize("sql", [
    "DECLARE @GITHUB_PASSWORD NVARCHAR(50)",          # 只有變數名稱
    "{{ env_var('GITHUB_PASSWORD') }}",               # 只有讀取
    "SELECT account_id FROM txn WHERE amount > 1000",
    "SELECT 1 FROM users WHERE password = other_column",   # 對欄位比較
])
def test_這些不該命中(sql):
    assert "R004" not in _codes(sql)


@pytest.mark.parametrize("sql", [
    "SET @GITHUB_PASSWORD = 'ghp_xxx123'",            # 變數名稱 + 寫死的值
    "CREATE LOGIN app WITH PASSWORD = '6789'",
])
def test_原本就抓得到的照樣抓得到(sql):
    assert "R004" in _codes(sql)


# ─────────────────── 4. 降級的兩道安全條件 ───────────────────

@pytest.mark.parametrize("sql,label", [
    ("SELECT 1 WHERE password = 'secret'", "密碼家族 + 值只有字母"),
    ("SELECT 1 WHERE pwd = 'abcdef'", "pwd 也是密碼家族"),
    ("SELECT 1 FROM u WHERE u.passwd = 'letmein'", "帶別名的密碼比較"),
])
def test_密碼家族的比較不降級(sql, label):
    """`WHERE password = 'secret'` 的值看起來像狀態碼(只有字母),但鍵名是密碼 ——
    那就是寫死的密碼,只是寫在比較裡。密碼家族一律不降級。"""
    assert "R004" in _codes(sql), label


@pytest.mark.parametrize("sql,label", [
    ("SELECT 1 WHERE api_key = 'sk-live-0000'", "有數字與連字號"),
    ("SELECT 1 WHERE token = 'eyJhbGci0'", "看起來像 JWT"),
    ("SELECT 1 WHERE secret = 'a1b2c3'", "英數混合"),
])
def test_值不像狀態碼就不降級(sql, label):
    """狀態碼是 'ACTIVE'、'N' 這種純字母的短字串。有數字或符號的一律當成憑證 ——
    降錯的代價是寫死的金鑰不再擋下 MR。"""
    assert "R004" in _codes(sql), label
