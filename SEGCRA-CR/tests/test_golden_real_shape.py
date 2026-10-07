"""真實程式形狀的 golden case(`eval/golden/mr_701.json`)——確定性的那幾段,
不呼叫 LLM、不連沙盒。

**為什麼要有這個檔**:現有 golden case 都是 10~20 行的簡化 SQL,與正式環境的
規則程式有結構性落差(真實規則是 dbt model,`dbt compile` 後是 200 行、多層 CTE、
視窗函式、OR-of-ANDs 的三組條件)。mr_701 用的就是 `dbt compile` 的實際輸出
(`tests/fixtures/dbt_compiled/sqlserver/mrt_RETAIL_M1.sql`),只改兩處讓它能在
沙盒執行。整個 case 的端到端行為要模型與 MS SQL 沙盒才驗得完,但底下這幾件事
是確定性的,值得在 CI 上釘住——它們壞掉時,那個 case 會以看起來無關的方式失敗
(「無規格可驗」而不是「檔名對應壞了」)。

驗的東西:
  1. 規格**依 model 檔名**對應:`mrt_RETAIL_M1_EVAL.sql` → `specs/RETAIL_M1_EVAL.md`。
     真實規則沒有 R 編號,這是它唯一的規格來源。評測規格刻意不用正式名稱:
     真正的 `mrt_RETAIL_M1.sql` 不能對到這份照程式改寫的規格。
  2. 這條路確實由 `dbt.enabled` 控制:關閉時必須對不到(#14 的安全預設)。
  3. `case_config()` 只對標記過的 case 換設定,不影響其他 case。
  4. 已編譯的 SQL 不會被誤判成 dbt 樣板(否則會被擋在沙盒外)。
  5. `prepare_sql()` 會宣告 `@start_date`——沙盒跑得動的前提。
  6. **已知缺口**:`dbt compile` 原始輸出的三段式表名,`prepare_sql()` 不會改寫。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
from run_eval import case_config  # noqa: E402

from orchestrator.config import Config  # noqa: E402
from orchestrator.dbt_render import is_dbt_template  # noqa: E402
from orchestrator.spec_exec import extract_rule_codes, find_spec, prepare_sql  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parents[1]
CASE_PATH = PKG_ROOT / "eval" / "golden" / "mr_701.json"
BUGGED_PATH = PKG_ROOT / "eval" / "golden" / "mr_702.json"
COMPILED = PKG_ROOT / "tests" / "fixtures" / "dbt_compiled" / "sqlserver" / "mrt_RETAIL_M1.sql"


@pytest.fixture
def case() -> dict:
    return json.loads(CASE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def bugged() -> dict:
    return json.loads(BUGGED_PATH.read_text(encoding="utf-8"))


def _cfg(**dbt) -> Config:
    return Config(endpoint="", api_key="", default_profile="review",
                  profiles={"review": {"model": "fake", "num_ctx": 1024,
                                       "temperature": 0, "max_output_tokens": 256}},
                  roles={}, budget={}, policy={},
                  **({"dbt": dbt} if dbt else {}))


def test_真實規則沒有R編號(case):
    """這正是需要依檔名對應規格的原因——不是這個 case 特別,是真實規則都這樣。"""
    assert extract_rule_codes(case) == []


def test_規格依model檔名對應得到(case):
    code, text = asyncio.run(find_spec(None, case, by_path=True))
    assert code == "RETAIL_M1_EVAL"
    assert text and text.lstrip().startswith("#")
    assert "資料表定義" in text, "執行驗證要靠這段建表,規格少了它測資生成建不起來"


def test_dbt接線關閉時對不到規格(case):
    """#14 的安全預設:關閉時行為與接線前完全相同。

    這條要單獨釘住——不然哪天預設被改開了,測試只會變得「更容易過」,
    沒有任何紅燈提醒行為已經變了。
    """
    code, text = asyncio.run(find_spec(None, case, by_path=False))
    assert code is None and text is None


def test_case_config只對標記過的case開啟dbt接線(case):
    base = _cfg()
    assert base.dbt["enabled"] is False

    same = case_config(base, {})
    assert same is base, "沒標記的 case 要原樣傳回,不做任何複製或改動"

    opened = case_config(base, case["_golden"])
    assert opened.dbt["enabled"] is True
    assert opened.dbt["database"], "展開 ref()/source() 需要資料庫名,空字串會展開失敗"
    assert base.dbt["enabled"] is False, "不得就地改到傳進來的設定(其他 case 還要用)"


def test_case_config一律用約定假名_不讀環境變數(case, monkeypatch):
    """評測不連資料庫,資料庫名只會被拼進表名字串。讀環境變數的話,開發機設了別的值
    就會被 #16 的「必須等於約定假名」擋下,或拼出不同的表名,兩份 baseline 不可比。"""
    from orchestrator.config import DBT_DATABASE_PLACEHOLDER
    monkeypatch.setenv("SEGCRA_DBT_DATABASE", "SOME_REAL_DB")
    opened = case_config(_cfg(), case["_golden"])
    assert opened.dbt["database"] == DBT_DATABASE_PLACEHOLDER


def test_已編譯的SQL不會被當成dbt樣板(case):
    """被判成樣板的話,spec_exec 會直接回報「尚未支援樣板的執行驗證」而不執行——
    那就測不到任何東西了。"""
    assert is_dbt_template(case["files"][0]["full_content"]) is False


def test_prepare_sql會宣告start_date(case):
    prepared = prepare_sql(case["files"][0]["full_content"])
    assert "DECLARE @start_date DATE" in prepared
    assert "SAMPLE_DW" not in prepared, "case 裡的 SQL 應已改成沙盒建得出來的單純表名"


def test_埋雷版只動條件1的那一行(bugged):
    """雷的位置本身就是這個 case 的難度來源,必須釘住。

    三組條件的 `eod_balance` 那一行長得一模一樣,只改第一組——其餘兩組維持
    `<= 1000`,掃過去會覺得「樣式一致」。哪天有人「順手把三組改一致」,
    這個 case 就退化成一眼看得出來的錯,測不到原本要測的東西。
    """
    sql = bugged["files"][0]["full_content"]
    assert sql.count("AND eod_balance < 1000") == 1, "條件 1 的雷不見了"
    assert sql.count("AND eod_balance <= 1000") == 2, "另外兩組不該被一起改掉"


def test_埋雷版的規格也對得到(bugged):
    code, text = asyncio.run(find_spec(None, bugged, by_path=True))
    assert code == "RETAIL_M1_EVAL" and text


def test_正式名稱的model對不到評測專用規格(case):
    """`specs/RETAIL_M1_EVAL.md` 是照範例程式改寫、讓正向對照乾淨的版本(拿掉了範本
    規格的兩項排除資料)。若它以正式名稱放在 `specs/`,之後審真正的 `mrt_RETAIL_M1.sql`
    都會依檔名對到它,範本規格有要求、程式卻沒做的部分就不會再被檢查。"""
    real = json.loads(json.dumps(case))
    real["files"][0]["path"] = "models/mrt_RETAIL_M1.sql"
    code, text = asyncio.run(find_spec(None, real, by_path=True))
    assert code != "RETAIL_M1_EVAL" and (text is None or "評測專用" not in text)


def test_埋雷版是大檔裡只改一行的維護型diff(bugged):
    """issue #8 §3 要的情境之一。其餘 case 不是新檔就是小檔全改,量不到
    「改動很小、檔案很大」時管線還找不找得到問題。"""
    f = bugged["files"][0]
    added = [ln for ln in f["diff"].splitlines() if ln.startswith("+")]
    assert len(added) == 1, "維護型 diff 應該只有一行變更"
    assert len(f["full_content"].splitlines()) > 150, "但整份檔案要夠大"


def test_執行驗證失敗時小diff也不得自動放行(bugged):
    """**這個 case 最有價值的斷言**:diff 只有 1 行,遠低於 max_diff_lines(30),
    又不是新規則檔——單看變更規模,它完全有資格自動放行。唯一擋下它的是
    執行驗證沒過(config/models.yaml 寫明的硬條件,不可由設定關閉)。

    政策層若哪天被改鬆,這條會紅;只靠「變更很小所以安全」放行,正是這套
    系統最不該犯的錯。
    """
    from orchestrator.config import load_config
    from orchestrator.pipeline import apply_policy

    report = {"score": 85,
              "findings": [{"severity": "major",
                            "title": "執行驗證失敗:實作與規格行為不符"}]}
    out = apply_policy(report, bugged, load_config().policy)

    assert out["_policy_signals"]["change_lines"] == 1
    assert out["_policy_signals"]["new_rule"] is False
    assert out["decision"] == "needs_human", "執行驗證沒過就不得自動放行"


def test_編譯輸出的三段式表名prepare_sql不會改寫():
    """**已知缺口,不是這個測試在挑剔**:`dbt compile` 的輸出一定帶三段式表名
    (`"<database>"."<schema>"."<表>"`),但執行驗證的沙盒把測資建在隨機命名的
    `segcra_sbx_*` 資料庫、表名是規格 DDL 裡的單純名稱——兩邊對不起來,查詢會
    以「Invalid object name」失敗。

    這條路徑是 README 寫的正式流程(「規則 SQL 先 dbt compile 展開再送進管線」),
    所以這不只是評測的問題。mr_701 目前是靠**改寫**表名繞過;真正的修法要嘛讓
    沙盒以同名資料庫承接,要嘛在 prepare_sql 剝掉限定詞,兩者都要單獨設計與審。

    這個測試釘住的是「現況就是不會改寫」——哪天有人實作了改寫,這裡會紅,
    提醒回來把 mr_701 的繞道拿掉。
    """
    prepared = prepare_sql(COMPILED.read_text(encoding="utf-8"))
    assert '"SAMPLE_DW"."dbo"."txn_log_net"' in prepared
