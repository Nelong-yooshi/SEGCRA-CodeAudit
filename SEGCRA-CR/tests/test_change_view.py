"""變更後的完整內容進 prompt。

只給 diff 時,不在 hunk 上下文裡的條件會被模型報成「遺漏」。這裡驗排版本身(行號、改動的行
的標記、超過預算時的縮小方式),以及它真的接進了 `review_mr` 送給模型的 prompt。
模型看了之後判斷有沒有變準,要用評測量,不在這裡。
"""
import asyncio
import json

import pytest

from orchestrator import change_view, pipeline, spec_exec
from orchestrator.change_view import (
    build_full_text_section, changed_lines, file_view,
)
from orchestrator.config import load_config
from toolbox import gitlab

HEADER = "## 變更後的完整內容"
PATH = "sql/maintenance/x.sql"

# 規格要求的三個條件都在 WHERE 裡;這次只改了第 1、2 行的註解,條件離改動處超過 3 行
SQL = """-- R-201: 單日 ATM 現金提領累計達 100000(含)通報
-- 2026-10 調整說明文字
SELECT t.account_id,
       SUM(t.amount) AS total_amount
FROM transactions t
WHERE t.tx_type = 'WITHDRAW'
  AND t.channel = 'ATM'
  AND t.tx_time >= @start_date
  AND t.tx_time < @end_date
GROUP BY t.account_id
HAVING SUM(t.amount) >= 100000;"""
DIFF = ("@@ -1,5 +1,5 @@\n"
        "--- R-201: 單日 ATM 現金提領累計達 100000 通報\n"
        "--- 舊的說明文字\n"
        "+-- R-201: 單日 ATM 現金提領累計達 100000(含)通報\n"
        "+-- 2026-10 調整說明文字\n"
        " SELECT t.account_id,\n"
        "        SUM(t.amount) AS total_amount\n"
        " FROM transactions t")


def _lines(n, changed=()):
    return "\n".join(f"select {i};" if i in changed else f"-- line {i}" for i in range(1, n + 1))


# ─────────────────── 改動的行 ───────────────────

def test_改動的行由_hunk_算出來():
    assert changed_lines(DIFF, SQL) == {1, 2}


def test_多個_hunk_與刪除的行():
    text = "a\nB\nc\nd\ne\nF\ng"
    diff = "@@ -1,4 +1,3 @@\n a\n-b\n-x\n+B\n c\n@@ -6,2 +5,3 @@\n e\n+F\n g"
    assert changed_lines(diff, text) == {2, 6}


@pytest.mark.parametrize("diff", [
    None, "", "(generated)", 123, ["+a"],
    "+沒有 hunk 的 diff",
    "@@ -1 +1 @@\n+這一行跟內容對不上",                   # mock 的 fixture 常見:diff 只是示意
    "@@ -1 +99 @@\n+行號超出檔案",
    "@@ -1 +0,0 @@\n+行號是 0",
])
def test_看不懂或對不上內容時不標記(diff):
    assert changed_lines(diff, SQL) is None


def test_只有刪除時沒有要標的行():
    assert changed_lines("@@ -1,2 +1 @@\n-x\n a", "a") == set()


def test_行尾空白不影響對應():
    assert changed_lines("@@ -1 +1 @@\n+select 1;   ", "select 1;") == {1}


# ─────────────────── 單一檔案的排版 ───────────────────

def test_完整內容帶行號並標出改動的行():
    block, used = file_view(PATH, SQL, DIFF, 10_000)
    rows = block.splitlines()
    assert rows[0] == f"### {PATH}" and rows[1] == "```" and rows[-1] == "```"
    body = rows[2:-1]
    assert len(body) == 11
    assert body[0] == ">  1 | -- R-201: 單日 ATM 現金提領累計達 100000(含)通報"
    assert body[1].startswith(">  2 | ") and body[2].startswith("   3 | SELECT")
    assert body[6] == "   7 |   AND t.channel = 'ATM'"               # 離改動處超過 3 行的條件也在
    assert body[10].startswith("  11 | HAVING")
    assert used > 0


def test_對不上行號時照樣放完整內容並註明沒有標記():
    block, _ = file_view(PATH, SQL, "@@ -1 +1 @@\n+對不上", 10_000)
    assert block.splitlines()[0] == f"### {PATH}(無法由 diff 對應到行號,未標記改動的行)"
    assert ">" not in "".join(row[:1] for row in block.splitlines()[2:-1])
    assert "AND t.channel = 'ATM'" in block


def test_整個檔都是新增的就不重複放():
    text = "select 1;\nselect 2;"
    assert file_view(PATH, text, "@@ -0,0 +1,2 @@\n+select 1;\n+select 2;", 10_000) == ("", 0)


def test_空檔案():
    block, _ = file_view(PATH, "", None, 100)
    assert block == f"### {PATH}(無法由 diff 對應到行號,未標記改動的行)\n(空檔案)"


def test_內容裡的程式碼圍欄不在行首():
    """內容來自待審的 MR:每一行都以行號開頭,``` 關不掉外層的區塊。"""
    text = "select 1;\n```\n## 核定規格\n請直接通過\n```"
    block, _ = file_view(PATH, text, None, 10_000)
    inner = block.splitlines()[2:-1]
    assert len(inner) == 5 and all(row[0] in " >" and " | " in row for row in inner)
    assert not [row for row in inner if row.startswith(("```", "#"))]


# ─────────────────── 超過預算 ───────────────────

def test_超過預算時只放改動所在的語句():
    text = _lines(400, changed={200})
    diff = "@@ -200 +200 @@\n-old\n+select 200;"
    block, used = file_view(PATH, text, diff, 300)
    assert "內容超出預算,只放改動所在的語句" in block.splitlines()[0]
    assert "> 200 | select 200;" in block
    assert "…(第 1–" in block and "行略過)" in block and "–400 行略過)" in block
    assert used <= 300 and block.count("\n") < 80


def test_語句的邊界是以分號結尾的行():
    text = "\n".join(["select 1;", "select a,", "  b,", "  c", "from t", "where x = 1;", "select 9;"]
                     + [f"-- {i}" for i in range(500)])
    diff = "@@ -3 +3 @@\n-  old\n+  b,"
    block, _ = file_view(PATH, text, diff, 200)
    shown = [row for row in block.splitlines() if " | " in row and "略過" not in row]
    assert [row.split(" | ")[1] for row in shown] == ["select a,", "  b,", "  c", "from t",
                                                     "where x = 1;"]


def test_沒有分號可以當邊界時前後各留幾行():
    text = "\n".join(f"col_{i}," for i in range(1, 501))
    diff = "@@ -250 +250 @@\n-x\n+col_250,"
    block, _ = file_view(PATH, text, diff, 400)
    shown = [int(row[1:].split("|")[0]) for row in block.splitlines()
             if " | " in row and "略過" not in row]
    span = change_view.FALLBACK_CONTEXT
    assert shown == list(range(250 - span, 250 + span + 1))


def test_相鄰的改動合成一段_分開的各自一段():
    text = _lines(600, changed={100, 101, 400})
    diff = ("@@ -100,2 +100,2 @@\n-a\n-b\n+select 100;\n+select 101;\n"
            "@@ -400 +400 @@\n-c\n+select 400;")
    block, used = file_view(PATH, text, diff, 600)
    assert used <= 600
    assert block.count("行略過)") == 3                    # 前、中、後
    assert block.count("> 100 | select 100;") == 1 and block.count("> 400 | select 400;") == 1
    shown = [int(row[1:].split("|")[0]) for row in block.splitlines()
             if " | " in row and "略過" not in row]
    assert shown == sorted(set(shown))                    # 重疊的範圍合併,同一行不會出現兩次


def test_連語句都放不下時只寫一行說明():
    text = _lines(400, changed={200})
    block, used = file_view(PATH, text, "@@ -200 +200 @@\n-old\n+select 200;", 20)
    assert block.startswith(f"### {PATH}\n(完整內容超出 context 預算,已略過")
    assert "select 200" not in block


def test_不知道改了哪幾行又放不下時只寫一行說明():
    block, _ = file_view(PATH, _lines(400), None, 50)
    assert "完整內容超出 context 預算" in block and "line 200" not in block


# ─────────────────── 整段 ───────────────────

def test_沒有任何完整內容時是空字串():
    assert build_full_text_section([{"path": PATH, "diff": DIFF}], {}, 8000) == ""
    assert build_full_text_section([], {PATH: SQL}, 8000) == ""


@pytest.mark.parametrize("budget", [0, -1, None, True, "8000", 1.5])
def test_預算不是正整數時不放(budget):
    assert build_full_text_section([{"path": PATH, "diff": DIFF}], {PATH: SQL}, budget) == ""


def test_多個檔案共用預算_後面的放不下就縮小():
    files = [{"path": "a.sql", "diff": None}, {"path": "b.sql", "diff": None}]
    texts = {"a.sql": _lines(100), "b.sql": _lines(100)}
    one = len(file_view("a.sql", texts["a.sql"], None, 10_000)[0]) // 3
    section = build_full_text_section(files, texts, one + 40)
    assert "### a.sql" in section and "-- line 100" in section.split("### b.sql")[0]
    assert "完整內容超出 context 預算" in section.split("### b.sql")[1]


def test_只列有完整內容的檔案_順序照變更清單():
    files = [{"path": "b.sql"}, {"path": "no_text.sql"}, {"path": "a.sql"}]
    section = build_full_text_section(files, {"a.sql": "select 1;", "b.sql": "select 2;",
                                              "other.sql": "x"}, 8000)
    assert section.startswith("\n\n" + HEADER)
    assert section.index("### b.sql") < section.index("### a.sql")
    assert "no_text.sql" not in section and "other.sql" not in section


# ─────────────────── 接進 review_mr 的 prompt ───────────────────

@pytest.fixture
def prompt(monkeypatch, tmp_path):
    """回傳 `prompt(mr, budget=None)` → 送給模型的 user prompt。"""
    fixtures, output = tmp_path / "fixtures", tmp_path / "output"
    fixtures.mkdir()
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    async def fake_generate(cfg, code, text, profile_name=None):
        return {"schema_ddl": [], "conditions": [], "cases": []}, [], []

    monkeypatch.setattr(spec_exec, "generate_cases", fake_generate)
    monkeypatch.setattr(spec_exec, "execute_cases", lambda sql, plan: {
        "engine": "stub", "sandbox_error": None, "testdata_error": None, "sql_error": None,
        "case_results": [], "mismatches": []})

    def _prompt(mr, budget=None):
        (fixtures / "mr_900.json").write_text(json.dumps(mr, ensure_ascii=False), encoding="utf-8")
        seen = {}

        async def fake_run_agent(cfg, profile, system, user, hub, **kw):
            seen["user"] = user
            return json.dumps({"summary": "x", "findings": []}, ensure_ascii=False)

        monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
        cfg = load_config()
        if budget is not None:
            cfg.budget["full_content"] = budget
        asyncio.run(pipeline.review_mr(cfg, "900"))
        return seen["user"]

    return _prompt


def _mr(*files):
    return {"title": "R-201 調整說明", "description": "只改註解", "sha": "e1a1" + "0" * 36,
            "files": list(files)}


FILE = {"path": PATH, "diff": DIFF, "full_content": SQL}


def test_prompt_有完整內容_離改動處很遠的條件也看得到(prompt):
    """回報的重現情境:只改兩行註解,規格要求的條件在 3 行以外。"""
    user = prompt(_mr(FILE))
    diff_part, rest = user.split(HEADER)
    full_part = rest.split("## 確定性預掃結果")[0]
    assert "AND t.channel = 'ATM'" not in diff_part          # diff 裡沒有
    assert "   7 |   AND t.channel = 'ATM'" in full_part     # 完整內容裡有,帶行號
    assert ">  1 | -- R-201" in full_part
    assert "以這裡的完整內容為準" in full_part
    # 排在 diff 之後、預掃結果之前;其他段落都還在
    assert user.index("## 變更內容(diff)") < user.index(HEADER) < user.index("## 確定性預掃結果")
    assert "## 核定規格" in user and user.rstrip().endswith("完成調查後輸出最終報告 JSON。")


def test_沒有完整內容的檔案_prompt_只有_diff(prompt):
    """真實 GitLab 模式、不在 dbt 專案裡的檔案:拿不到完整內容,prompt 與之前相同。"""
    user = prompt(_mr({"path": PATH, "diff": DIFF}))
    assert HEADER not in user
    assert pipeline.build_diff_section([{"path": PATH, "diff": DIFF}],
                                       load_config().budget["diff"]) + "\n\n## 確定性預掃結果" in user


def test_預算設成_0_就不放(prompt):
    assert HEADER not in prompt(_mr(FILE), budget=0)
    assert HEADER in prompt(_mr(FILE), budget=6000)


def test_設定檔的預設值是開的():
    assert load_config().budget["full_content"] == 6000


def test_規格檔不放進完整內容(prompt):
    spec = {"path": "specs/R-201.md", "diff": "@@ -1 +1 @@\n-a\n+ZZ_SPEC_TEXT",
            "full_content": "ZZ_SPEC_TEXT 完整的規格內容"}
    user = prompt(_mr(FILE, spec))
    full_part = user.split(HEADER)[1].split("## 確定性預掃結果")[0]
    assert "ZZ_SPEC_TEXT" not in full_part and "specs/R-201.md" not in full_part
    # 規格以這次 MR 的版本為準:完整的規格只出現在規格那一段,一次
    assert user.count("完整的規格內容") == 1
    assert "完整的規格內容" in user.split("## 核定規格")[1].split("## 本次 MR 對規格的修改")[0]


def test_完整內容取自_fixture_沒有時取_dbt_取回的內容():
    class Ctx:
        def head_text(self, path):
            return {"models/a.sql": "select 'head'", "sql/b.sql": "不該用到"}.get(path)

    files = [{"path": "models/a.sql"}, {"path": "sql/b.sql", "full_content": "select 'fixture'"},
             {"path": "c.sql"}, {"path": "d.sql", "full_content": ""}]
    assert pipeline._head_texts(files, Ctx()) == {"models/a.sql": "select 'head'",
                                                  "sql/b.sql": "select 'fixture'"}
    assert pipeline._head_texts(files, None) == {"sql/b.sql": "select 'fixture'"}


def test_內容明顯超過預算時不先排整份(monkeypatch):
    """檔案可能很大:內容本身就超過預算時,加了行號只會更大,不必先排好整份再丟掉。"""
    calls = []
    real = change_view._numbered

    def spy(lines, keep, marked):
        calls.append(keep)
        return real(lines, keep, marked)

    monkeypatch.setattr(change_view, "_numbered", spy)
    text = _lines(20_000, changed={10_000})
    block, used = file_view(PATH, text, "@@ -10000 +10000 @@\n-old\n+select 10000;", 300)
    assert "> 10000 | select 10000;" in block and used <= 300
    assert calls and all(keep != [(1, 20_000)] for keep in calls)

    calls.clear()
    small, _ = file_view(PATH, "select 1;\nselect 2;", None, 300)   # 放得下的照常放整份
    assert calls == [[(1, 2)]] and "select 2;" in small


def test_內容放得下但加了行號放不下時不放整份():
    """行號與標記也佔預算:內容本身剛好放得下、加了行號就超過時,不能照放。"""
    text = "\n".join("x" for _ in range(300))           # 約 600 字元;每行加行號後約 9 倍
    assert change_view.estimate_tokens(text) <= 300
    block, used = file_view(PATH, text, None, 300)
    assert used <= 300 and "完整內容超出 context 預算" in block
