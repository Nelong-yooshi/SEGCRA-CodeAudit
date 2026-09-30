"""Skill 載入與 context 預算的測試 — 不呼叫 LLM。

為什麼值得單獨測:skill 決定模型「知道什麼」。常駐的 skill 每次都進 prompt,按需的
要模型自己用 load_skill 工具取——而「模型會不會主動去載」正是三明治架構其他地方都
不敢賭的事。管線因此對兩個 skill 做了**確定性強制載入**(改到 rules/ 檔案就一定載
anomaly-rules;預掃命中 R004/H004 就一定載 secure-sql),不讓模型自己決定。

eval/README「覆蓋缺口」列的「skill 載入(目前沒有效能類 case)」指的就是:
`perf-review` 這個 skill 存在,但既沒有強制載入的觸發條件、也沒有任何 golden case
驗證它會被載入 —— 它完全靠模型自己想到要載(見文末的測試)。

驗的東西:
  1. 六個 skill 都解析得出來,frontmatter 欄位正確。
  2. always / on_demand 的分工:常駐的直接進 prompt,按需的只出現在索引裡。
  3. 索引不重複列出常駐 skill(它們的內文已經在 prompt 裡了,再列一次浪費 context)。
  4. context 預算:diff 超出預算時逐檔略過,而且**留下明顯的佔位訊息**——
     靜靜截斷等於用不完整的資料做審查,卻沒有人知道。
  5. 記錄 perf-review 目前沒有強制載入觸發條件這件事。
"""
import pytest

from orchestrator.config import estimate_tokens
from orchestrator.pipeline import build_diff_section
from orchestrator.skills_loader import always_skills, load_skills, skills_index

SKILLS = load_skills()


# ─────────────────── 載入與分類 ───────────────────

def test_所有_skill_都解析得出來():
    """frontmatter 壞掉的檔案會被靜默跳過(load_skills 的 continue),

    所以要明確驗每一個都在——少一個不會有錯誤訊息,只會讓模型少知道一件事。
    """
    expected = {"anomaly-rules", "mr-comment-format", "objective-reviewer",
                "perf-review", "secure-sql", "sql-review"}
    assert set(SKILLS) == expected


def test_每個_skill_都有描述與內文():
    for name, s in SKILLS.items():
        assert s.description.strip(), f"{name} 沒有 description,模型看索引時不知道何時該載"
        assert s.body.strip(), f"{name} 沒有內文"
        assert s.trigger in ("always", "on_demand"), f"{name} 的 trigger 值不合法"


def test_常駐與按需的分工():
    always = {n for n, s in SKILLS.items() if s.trigger == "always"}
    assert always == {"objective-reviewer", "sql-review"}, (
        "常駐 skill 每次都佔 context,增減要是有意識的決定")


def test_索引只列按需的_skill():
    """常駐 skill 的內文已經直接放進 prompt 了,索引再列一次是白白浪費 context。"""
    index = skills_index(SKILLS)
    for name, s in SKILLS.items():
        if s.trigger == "always":
            assert f"`{name}`" not in index, f"常駐的 {name} 不該出現在按需索引裡"
        else:
            assert f"`{name}`" in index, f"按需的 {name} 沒出現在索引,模型不會知道它存在"


def test_常駐區塊包含所有常駐_skill_的內文():
    block = always_skills(SKILLS)
    for name, s in SKILLS.items():
        if s.trigger == "always":
            assert s.body[:40] in block, f"{name} 的內文沒進常駐區塊"


# ─────────────────── context 預算 ───────────────────

def test_預算內的_diff_完整保留():
    files = [{"path": "a.sql", "diff": "+SELECT 1;"}]
    section = build_diff_section(files, budget_tokens=1000)
    assert "SELECT 1;" in section
    assert "超出 context 預算" not in section


def test_超出預算的檔案要留下明顯的佔位訊息():
    """靜靜截斷是最危險的:模型拿到不完整的 diff 卻以為看到了全部,

    人看報告時也不知道有東西沒被審到。所以要留可見的訊息。
    """
    files = [{"path": "huge.sql", "diff": "+x" * 5000}]
    section = build_diff_section(files, budget_tokens=10)
    assert "超出 context 預算" in section
    assert "huge.sql" in section, "略過了哪個檔案要講清楚"


def test_超出預算只略過該檔不影響後面的檔案():
    """一個大檔不該讓後面的小檔跟著被丟掉。"""
    files = [
        {"path": "huge.sql", "diff": "+x" * 5000},
        {"path": "small.sql", "diff": "+SELECT 2;"},
    ]
    section = build_diff_section(files, budget_tokens=50)
    assert "超出 context 預算" in section
    assert "SELECT 2;" in section, "後面的小檔被連累丟掉了"


def test_每個檔案都會出現在_diff_區塊裡():
    files = [{"path": f"f{i}.sql", "diff": f"+SELECT {i};"} for i in range(5)]
    section = build_diff_section(files, budget_tokens=1000)
    for i in range(5):
        assert f"f{i}.sql" in section


@pytest.mark.parametrize("text,at_least", [
    ("", 1),                       # 空字串也回傳至少 1,不會回 0 讓除法炸掉
    ("SELECT 1;", 1),
    ("x" * 300, 100),
])
def test_token_估算不會回傳零(text, at_least):
    assert estimate_tokens(text) >= at_least


# ─────────────────── 記錄現況:perf-review 沒有強制載入 ───────────────────

def test_現況_perf_review_完全靠模型自己想到要載():
    """管線目前只對兩個 skill 做確定性強制載入(見 pipeline.review_mr):

      - 改到 sql/rules/ 底下的檔案  → 一定載 anomaly-rules
      - 預掃命中 R004 / H004(資安)→ 一定載 secure-sql

    `perf-review`(SQL 效能:索引、掃描、JOIN 成本)**沒有任何觸發條件**,
    只出現在索引裡等模型自己決定要不要載——這正是架構其他地方都不賭的事。

    這個測試不是在說現況是錯的(效能問題不像資安那樣非擋不可),而是把
    「這件事目前是賭模型」這個事實釘在測試裡,不要變成沒人記得的隱性假設。
    要改成強制載入的話,得先想清楚觸發條件(例如預掃出全表掃描、或 diff 涉及大表)。
    """
    assert SKILLS["perf-review"].trigger == "on_demand"
    assert "perf-review" in skills_index(SKILLS), "至少要讓模型知道有這個 skill 可以載"
