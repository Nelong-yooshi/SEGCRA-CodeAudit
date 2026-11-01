"""設定參數一覽與程式要對得上(#58)——不呼叫 LLM、不連資料庫。

部署的人只看得到文件。文件漏一個參數,那個參數就等於不存在:沒人知道它可以設、
預設值是什麼、真實模式必不必填。所以這裡用語法樹掃程式,逐一對照 `docs/10-設定參數.md`。

**為什麼不能用字串比對**:有六個參數是透過 `config.py` 的 helper 讀的
(`env_seconds("SPEC_EXEC_TIMEOUT_SECONDS", 1200)` 之類),那幾個檔案裡根本沒有
`os.environ` 字樣。只認 `os.environ` / `os.getenv` 的掃描會漏掉它們,而漏掉的正好
包含 #53 補的那兩個時間上限 —— 一致性檢查會變成一條永遠綠的測試。
"""
import ast
import pathlib
import re

import pytest

from orchestrator.env_registry import (PKG_ROOT, scan_file, scan_repo,
                                       scan_repo_dynamic)

DOC = PKG_ROOT / "docs" / "10-設定參數.md"
# 表格列:| `NAME` | 預設值 | …
_ROW = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|\s*([^|]*?)\s*\|")
# 表頭:第一欄是「變數」。分隔線(|---|)不是表頭也不是資料列。
_HEADER = re.compile(r"^\|\s*變數\s*\|")
_DERIVED = "(衍生)"


def documented() -> dict[str, str]:
    """{變數名稱: 文件寫的預設值}。

    只讀「有預設值欄位」的表:最前面那張「真實模式必填」的第二欄是「不設會怎樣」,
    把它當成預設值的話,每一個必填參數都會被判成預設值不符。
    """
    out, in_table = {}, False
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith("|"):
            if _HEADER.match(line):
                # 表頭用**結構**認,不是看有沒有「預設值」三個字:用途欄寫到那三個字的
                # 參數(LLM_TIMEOUT 的「預設值刻意給得很寬」)會被當成表頭而整列被跳過。
                in_table = "預設值" in line
            elif in_table and (m := _ROW.match(line)):
                out.setdefault(m.group(1), m.group(2))
        elif line.startswith("#"):
            in_table = False          # 換一節就重新等下一張表的表頭
    return out


# 名稱算不出來的讀法:掃描看不到參數名稱,所以文件的一致性檢查也看不到它。
# 認帳用的清單,比對 (檔案, 表達式原文) 而不是行號——行號會因為上下改動而變。
_KNOWN_DYNAMIC = {
    # helper 本體:名稱是參數,實際的名稱在呼叫端(那些是字串常值,照樣掃得到)
    ("orchestrator/config.py", "os.environ[name]"),
    ("orchestrator/config.py", "os.environ.get(name)"),
    # 把 config/sandbox.env 的內容塞進環境:鍵來自那個檔案,不是程式寫死的
    ("orchestrator/spec_exec.py", "os.environ.setdefault(k.strip(), v.strip())"),
}


def test_名稱算不出來的讀法會被列出來(tmp_path):
    """只驗「沒有未認帳的」不夠:整個不收集的話那份清單是空的,測試照樣綠。
    這條從正面驗——這種寫法一定要出現在清單裡。"""
    from orchestrator.env_registry import _scan

    f = tmp_path / "x.py"
    f.write_text('import os\nk = "A"\na = os.getenv(f"SANDBOX_{k}")\n'
                 'b = os.environ[k]\nc = os.getenv("REAL_NAME")\n', encoding="utf-8")
    static, dynamic = _scan(f, "x.py")
    assert [r.name for r in static] == ["REAL_NAME"]
    assert len(dynamic) == 2, [r.name for r in dynamic]
    assert all(r.where.startswith("x.py:") for r in dynamic)


def test_動態讀法只有已知的那幾處():
    """`os.getenv(f"SANDBOX_{x}")` 這種寫法掃不出名稱。默默跳過的話,掃描少看一個
    參數而這裡照樣綠,文件就會缺一個參數——對部署的人來說那個參數等於不存在。
    新加的動態讀法要先寫進 docs/10,再加進這個清單。"""
    unknown = sorted((r.where, r.name) for r in scan_repo_dynamic()
                     if (r.where.split(":")[0], r.name) not in _KNOWN_DYNAMIC)
    assert unknown == [], f"沒認帳的動態讀法:{unknown}"


def test_掃描範圍含進入點():
    """`tools/`、`demo.py` 也是部署的人會跑的東西,在那裡新增的參數一樣要寫進文件。"""
    from orchestrator.env_registry import SCAN_DIRS, SCAN_FILES
    assert "tools" in SCAN_DIRS and "demo.py" in SCAN_FILES
    where = [r.where for reads in scan_repo().values() for r in reads]
    assert any(w.startswith("demo.py") for w in where), "demo.py 沒有被掃到"


def test_範例設定檔裡的變數都在表裡():
    """`config/*.env.example` 是部署的人實際會複製的檔案,也是第二個會走鐘的來源:
    改了名字只改表、沒改範例檔的話,照著範例設的人會設到一個沒人讀的變數。"""
    doc = documented()
    for sample in sorted((PKG_ROOT / "config").glob("*.env.example")):
        for line in sample.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name = line.split("=", 1)[0].strip()
                assert name in doc, f"{sample.name} 的 {name} 沒有寫進 {DOC.name}"


def test_程式讀的每個環境變數都寫進文件():
    missing = sorted(set(scan_repo()) - set(documented()))
    assert missing == [], f"這些參數沒有寫進 {DOC.name}:{missing}"


def test_文件裡沒有程式不讀的參數():
    """多寫的也要抓:參數改名或拿掉之後,文件會留下一個查不到的名字。"""
    stray = sorted(set(documented()) - set(scan_repo()))
    assert stray == [], f"{DOC.name} 寫了程式不讀的參數:{stray}"


def test_文件的預設值跟程式一致():
    """只比對算得出來的常數;路徑、設定檔的值這種由別的東西算出來的寫 `(衍生)`。"""
    bad = []
    doc_all = documented()
    for name, reads in scan_repo().items():
        doc = doc_all.get(name, "").strip("`")
        values = [r.default_value for r in reads if r.default_value is not None]
        if not values:
            if doc != _DERIVED:
                bad.append(f"{name}:程式的預設值算不出常數,文件要寫 {_DERIVED},現在寫 {doc!r}")
            continue
        want = values[0]
        if doc == _DERIVED:
            bad.append(f"{name}:程式的預設值是 {want!r},文件卻寫 {_DERIVED}")
        elif str(want) != doc and repr(want) != doc:
            bad.append(f"{name}:程式是 {want!r},文件寫 {doc!r}")
    assert bad == [], "\n".join(bad)


def test_真實模式必填的參數在文件裡標出來():
    required = ["GITLAB_URL", "GITLAB_TOKEN", "GITLAB_PROJECT",
                "GITLAB_WEBHOOK_SECRET", "SANDBOX_MSSQL_PASSWORD"]
    section = DOC.read_text(encoding="utf-8").split("## 真實模式必填", 1)[-1].split("\n## ", 1)[0]
    assert all(f"`{n}`" in section for n in required), section


def test_同一個參數在不同地方的預設值要一致():
    """多處讀同一個參數卻寫了不同的預設值,是程式本身的不一致:實際用到哪一個要看
    誰先被 import,而文件只能寫一個。現在沒有這種情況,有了就要先修程式。"""
    bad = []
    for name, reads in scan_repo().items():
        values = {r.default_value for r in reads if r.default_value is not None}
        if len(values) > 1:
            bad.append(f"{name}:{values}({[r.where for r in reads]})")
    assert bad == [], "\n".join(bad)


def test_掃描範圍不是空的():
    """路徑算錯時上面每一條都會變成永遠綠的斷言。"""
    found = scan_repo()
    assert len(found) >= 20, found


# ─────────────────── 掃描本身的行為 ───────────────────

@pytest.mark.parametrize("src,want", [
    ('import os\nx = os.environ["A_VAR"]', "A_VAR"),
    ('import os\nx = os.environ.get("B_VAR", "d")', "B_VAR"),
    ('import os\nx = os.getenv("C_VAR")', "C_VAR"),
    ('from os import getenv\nx = getenv("D_VAR")', "D_VAR"),
    # import 的寫法換一種就掃不到的話,新增的參數會靜悄悄繞過文件要求
    ('from os import environ\nx = environ.get("D1_VAR")', "D1_VAR"),
    ('from os import environ\nx = environ["D2_VAR"]', "D2_VAR"),
    ('import os as o\nx = o.environ.get("D3_VAR")', "D3_VAR"),
    ('import os as o\nx = o.getenv("D4_VAR")', "D4_VAR"),
    ('import os as o\nx = o.environ["D5_VAR"]', "D5_VAR"),
    ('import os\nos.environ.setdefault("E_VAR", "1")', "E_VAR"),
    ('x = env_seconds("F_VAR", 60)', "F_VAR"),          # helper:檔案裡沒有 os.environ
    ('x = _env_float("G_VAR", 0, 1)', "G_VAR"),
    ('from .config import env_seconds\nx = config.env_seconds("H_VAR", 1)', "H_VAR"),
])
def test_各種讀法都掃得到(tmp_path, src, want):
    f = tmp_path / "m.py"
    f.write_text(src, encoding="utf-8")
    assert [r.name for r in scan_file(f)] == [want]


@pytest.mark.parametrize("src", [
    'x = os.environ.get(name)',                  # 變數,不是常值:列不出名字
    'x = some_other("I_VAR", 1)',                # 不是讀環境變數的 helper
    'x = "J_VAR"',
])
def test_不是讀環境變數的不算(tmp_path, src):
    f = tmp_path / "m.py"
    f.write_text("import os\n" + src, encoding="utf-8")
    assert scan_file(f) == []


def test_語法壞掉的檔案不讓整個掃描炸掉(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def (((", encoding="utf-8")
    assert scan_file(f) == []


def test_預設值算得出來與算不出來分得出(tmp_path):
    f = tmp_path / "m.py"
    f.write_text('import os\n'
                 'a = os.environ.get("LIT", "60")\n'
                 'b = os.environ.get("FOLDED", str(1024 * 1024))\n'
                 'c = os.environ.get("DERIVED", PKG_ROOT / "x")\n', encoding="utf-8")
    got = {r.name: r.default_value for r in scan_file(f)}
    assert got == {"LIT": "60", "FOLDED": "1048576", "DERIVED": None}


def test_setdefault_設的值不算預設值(tmp_path):
    """評測在 run_eval.py 把 LLM_SEED 設成 42;那是評測要的固定值,不是程式的預設值。
    名稱照樣要收 —— 它也是一個可以設的參數。"""
    f = tmp_path / "m.py"
    f.write_text('import os\nos.environ.setdefault("SEEDY", "42")\n', encoding="utf-8")
    read = scan_file(f)[0]
    assert read.name == "SEEDY" and read.default_value is None
