"""展開器從「記憶體裡的專案檔案」讀 macro(build_env / render_model 的 macro_files;#15 接線 W1)。

從 GitLab 取回的 dbt 專案是 {路徑: 內容} 的字典,不在磁碟上。這裡驗證:
1. 字典來源與磁碟來源的展開結果**完全相同**,而且都等於 dbt compile 的標準答案
2. 字典來源受同一套限制(檔數、總字元、內建同名、壞檔記錄),路徑另外再檢查一次
3. 載入順序與磁碟相同(同名 macro 由後載入的勝出,順序不同結果就不同)
"""
import importlib.util
import pathlib

import pytest

import orchestrator.dbt_render as dbt_render
from orchestrator.dbt_render import DEFAULT_ADAPTER, build_env, render_model, render_model_isolated

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
REFERENCE_PROJECT = PKG_ROOT / "tests" / "dbt_reference"
COMPILED_DIR = PKG_ROOT / "tests" / "fixtures" / "dbt_compiled"
REFERENCE_MACRO_DIRS = ("examples/sample/RETAIL_M1_code/macros", "tests/dbt_reference/macros")
SAMPLE_VARS = {"target_date": "2026-02-01"}

_spec = importlib.util.spec_from_file_location("dbt_reference_generate_w1",
                                               REFERENCE_PROJECT / "generate.py")
_reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_reference)
REFERENCE_SOURCES = _reference.reference_sources()


def _sample_db() -> str:
    """與 test_dbt_render.py 用同一個去識別化的資料庫名(從那裡讀,不重複寫一份)。"""
    import re
    text = (PKG_ROOT / "tests" / "test_dbt_render.py").read_text(encoding="utf-8")
    return re.search(r'^SAMPLE_DB = "([^"]+)"', text, re.M).group(1)


DB = _sample_db()


def _read_tree(root: pathlib.Path, *subdirs: str) -> dict[str, str]:
    """把磁碟上的目錄讀成 {相對 root 的路徑: 內容}(所有檔案,不只 .sql)。"""
    out = {}
    for sub in subdirs:
        for f in sorted((root / sub).rglob("*")):
            if f.is_file():
                try:
                    out[f.relative_to(root).as_posix()] = f.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    pass
    return out


REFERENCE_FILES = _read_tree(PKG_ROOT, *REFERENCE_MACRO_DIRS)


def _targets() -> list[pathlib.Path]:
    return sorted(p for p in COMPILED_DIR.iterdir() if p.is_dir())


def _write(root: pathlib.Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(text.encode("utf-8"))


def _both(tmp_path, files: dict[str, str], source: str, **kw):
    """同一份專案分別從磁碟與字典展開,回傳 (磁碟結果, 字典結果)。"""
    _write(tmp_path, files)
    disk = render_model("m.sql", code_root=tmp_path, source=source, **kw)
    mem = render_model("m.sql", macro_files=files, source=source, **kw)
    return disk, mem


def _same(a, b) -> None:
    for field in ("ok", "sql", "error", "line_map", "map_method", "macros", "macro_conflicts",
                  "macro_problems", "source_lines", "rendered_lines", "relation_notice"):
        assert getattr(a, field) == getattr(b, field), field


# ─────────────────── 1. 與磁碟來源、與 dbt compile 相同 ───────────────────

@pytest.mark.parametrize("target, name", [(t, n) for t in _targets() for n in REFERENCE_SOURCES],
                         ids=lambda v: v.name if isinstance(v, pathlib.Path) else v)
def test_字典來源與磁碟來源逐欄相同_且等於dbt_compile(target, name):
    common = dict(source=REFERENCE_SOURCES[name], variables=SAMPLE_VARS, database=DB,
                  macro_dirs=REFERENCE_MACRO_DIRS, adapter=target.name)
    disk = render_model(f"{name}.sql", code_root=PKG_ROOT, **common)
    mem = render_model(f"{name}.sql", macro_files=REFERENCE_FILES, **common)
    assert disk.ok and mem.ok, (disk.error, mem.error)
    _same(disk, mem)
    assert mem.sql == (target / f"{name}.sql").read_text(encoding="utf-8")
    assert mem.macros, "字典來源沒有載入任何 macro"


def test_對照專案確實有東西可比():
    assert _targets() and len(REFERENCE_SOURCES) >= 2
    assert sum(p.endswith(".sql") for p in REFERENCE_FILES) >= 2


def test_子行程版也能用字典來源_結果與同行程相同():
    name = "mrt_RETAIL_M1"
    common = dict(source=REFERENCE_SOURCES[name], variables=SAMPLE_VARS, database=DB,
                  macro_dirs=REFERENCE_MACRO_DIRS, macro_files=REFERENCE_FILES)
    here = render_model(f"{name}.sql", **common)
    isolated = render_model_isolated(f"{name}.sql", **common)
    assert here.ok and isolated.ok, (here.error, isolated.error)
    _same(here, isolated)


def test_build_env兩種來源的macro清單相同():
    _, disk, c1, p1 = build_env(PKG_ROOT, macro_dirs=REFERENCE_MACRO_DIRS)
    _, mem, c2, p2 = build_env(macro_dirs=REFERENCE_MACRO_DIRS, macro_files=REFERENCE_FILES)
    assert disk == mem and disk and (c1, p1) == (c2, p2)


# ─────────────────── 2. 載入順序與磁碟相同 ───────────────────

def _macro(name: str, body: str) -> str:
    return "{% macro " + name + "() %}" + body + "{% endmacro %}"


def test_同名macro_兩種來源選到同一個_衝突清單相同(tmp_path):
    files = {"macros/a.sql": _macro("pick", "from_a"), "macros/b.sql": _macro("pick", "from_b")}
    disk, mem = _both(tmp_path, files, "{{ pick() }}")
    assert disk.ok and mem.ok
    _same(disk, mem)
    assert mem.sql == "from_b" and mem.macro_conflicts == ["pick: macros/a.sql / macros/b.sql"]


def test_字典的插入順序不影響結果(tmp_path):
    a, b = _macro("pick", "from_a"), _macro("pick", "from_b")
    one = render_model("m.sql", macro_files={"macros/a.sql": a, "macros/b.sql": b}, source="{{ pick() }}")
    two = render_model("m.sql", macro_files={"macros/b.sql": b, "macros/a.sql": a}, source="{{ pick() }}")
    _same(one, two)
    assert one.sql == "from_b"


def test_子目錄的排序依路徑分段_不是整串字串(tmp_path):
    """整串比較時 "macros/a-b.sql" 排在 "macros/a/b.sql" 前面(- 的編碼比 / 小),
    磁碟上的 sorted(rglob) 卻是依分段比:先 a/ 再 a-b.sql。兩種來源要一致。"""
    files = {"macros/a/b.sql": _macro("pick", "from_dir"),
             "macros/a-b.sql": _macro("pick", "from_file")}
    disk, mem = _both(tmp_path, files, "{{ pick() }}")
    assert disk.ok and mem.ok
    _same(disk, mem)
    assert mem.macro_conflicts == ["pick: macros/a/b.sql / macros/a-b.sql"]


def test_多個macro目錄_依設定的順序載入(tmp_path):
    files = {"m1/x.sql": _macro("pick", "from_m1"), "m2/x.sql": _macro("pick", "from_m2")}
    for dirs, winner in ((("m1", "m2"), "from_m2"), (("m2", "m1"), "from_m1")):
        disk, mem = _both(tmp_path, files, "{{ pick() }}", macro_dirs=dirs)
        assert disk.ok and mem.ok
        _same(disk, mem)
        assert mem.sql == winner


def test_跨檔互相呼叫_與磁碟相同(tmp_path):
    files = {"macros/a.sql": _macro("outer", "[{{ inner() }}]"),
             "macros/z/inner.sql": _macro("inner", "x")}
    disk, mem = _both(tmp_path, files, "{{ outer() }}")
    _same(disk, mem)
    assert mem.sql == "[x]"


# ─────────────────── 3. 只載入 macro 目錄底下的 .sql ───────────────────

def test_macro目錄以外的檔案不載入(tmp_path):
    files = {"macros/a.sql": _macro("ok", "1"),
             "models/b.sql": _macro("from_model", "2"),
             "macros_backup/c.sql": _macro("from_similar_dir", "3"),   # 前綴相近但不是同一個目錄
             "macros.sql": _macro("from_root_file", "4")}
    disk, mem = _both(tmp_path, files, "{{ ok() }}")
    _same(disk, mem)
    assert mem.macros == ["ok"]


def test_不是sql的檔案不載入(tmp_path):
    files = {"macros/a.sql": _macro("ok", "1"), "macros/schema.yml": _macro("from_yml", "2"),
             "macros/readme.md": _macro("from_md", "3"), "macros/x.sql.bak": _macro("from_bak", "4")}
    disk, mem = _both(tmp_path, files, "{{ ok() }}")
    _same(disk, mem)
    assert mem.macros == ["ok"]


def test_空的字典_沒有macro可用但能展開一般內容():
    r = render_model("m.sql", macro_files={}, source="select {{ 1 + 1 }}")
    assert r.ok and r.sql == "select 2" and r.macros == []


def test_用到沒載入的macro_大聲失敗_不產出少一段的SQL():
    r = render_model("m.sql", macro_files={"models/b.sql": _macro("m", "x")}, source="a {{ m() }} b")
    assert not r.ok and "m" in r.error


def test_model從磁碟讀_macro從字典讀(tmp_path):
    (tmp_path / "m.sql").write_text("{{ one() }}", encoding="utf-8")
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "disk.sql").write_text(_macro("one", "from_disk"), encoding="utf-8")
    r = render_model(tmp_path / "m.sql", macro_files={"macros/x.sql": _macro("one", "from_dict")})
    assert r.ok and r.sql == "from_dict"          # 給了字典就不再讀 model 旁邊的 macros/


# ─────────────────── 4. 同一套限制 ───────────────────

def test_兩種來源同時給_拒絕(tmp_path):
    r = render_model("m.sql", code_root=tmp_path, macro_files={}, source="x")
    assert not r.ok and "只能擇一" in r.error
    with pytest.raises(dbt_render.DbtRenderError):
        build_env(tmp_path, macro_files={})


@pytest.mark.parametrize("bad", ["macros/a.sql", ["macros/a.sql"], 5, (("macros/a.sql", "x"),)],
                         ids=["字串", "清單", "數字", "tuple"])
def test_不是字典_拒絕(bad):
    r = render_model("m.sql", macro_files=bad, source="x")
    assert not r.ok and "字典" in r.error


@pytest.mark.parametrize("files", [{"macros/a.sql": b"x"}, {"macros/a.sql": None}, {"macros/a.sql": 5},
                                   {5: "x"}, {None: "x"}, {b"macros/a.sql": "x"},
                                   {("macros", "a.sql"): "x"}],
                         ids=["內容 bytes", "內容 None", "內容 數字", "路徑 數字", "路徑 None",
                              "路徑 bytes", "路徑 tuple"])
def test_路徑或內容不是字串_拒絕(files):
    r = render_model("m.sql", macro_files=files, source="x")
    assert not r.ok and "必須是字串" in r.error


SECRET_PATH = "zzSECRETzz"


@pytest.mark.parametrize("path", [
    "../macros/" + SECRET_PATH + ".sql", "macros/../" + SECRET_PATH + ".sql",
    "/macros/" + SECRET_PATH + ".sql", "macros//" + SECRET_PATH + ".sql",
    "macros/./" + SECRET_PATH + ".sql", "macros\\" + SECRET_PATH + ".sql",
    "C:/macros/" + SECRET_PATH + ".sql", "macros/" + SECRET_PATH + ".sql/", "",
    "other/../../" + SECRET_PATH + ".txt",
], ids=["開頭 ..", "中間 ..", "絕對路徑", "空的分段", "單點", "反斜線", "磁碟代號", "結尾斜線",
        "空字串", "不在 macro 目錄也照檢查"])
def test_不合法的路徑_整份拒絕_訊息不回顯路徑(path):
    files = {"macros/ok.sql": _macro("ok", "1"), path: _macro("evil", "2")}
    r = render_model("m.sql", macro_files=files, source="{{ ok() }}")
    assert not r.ok and "不合法的路徑" in r.error
    assert SECRET_PATH not in r.error


def test_macro目錄設定不合法_字典來源同樣拒絕():
    for dirs in ("../macros", "/etc", "a//b", "C:/x", ""):
        r = render_model("m.sql", macro_files={}, source="x", macro_dirs=(dirs,))
        assert not r.ok and "macro 目錄設定不合法" in r.error, dirs


def test_檔數上限(monkeypatch, tmp_path):
    monkeypatch.setattr(dbt_render, "MAX_MACRO_FILES", 3)
    files = {f"macros/f{i}.sql": _macro(f"m{i}", "x") for i in range(4)}
    disk, mem = _both(tmp_path, files, "x")
    _same(disk, mem)
    assert not mem.ok and "上限" in mem.error
    assert render_model("m.sql", macro_files=dict(list(files.items())[:3]), source="x").ok


def test_檔數上限只算macro目錄底下的sql(monkeypatch):
    monkeypatch.setattr(dbt_render, "MAX_MACRO_FILES", 2)
    files = {"macros/a.sql": _macro("a", "1"), **{f"models/m{i}.sql": "select 1" for i in range(50)}}
    assert render_model("m.sql", macro_files=files, source="{{ a() }}").sql == "1"


def test_總字元上限(monkeypatch, tmp_path):
    monkeypatch.setattr(dbt_render, "MAX_MACRO_CHARS", 200)
    files = {"macros/a.sql": _macro("a", "x" * 150), "macros/b.sql": _macro("b", "y" * 150)}
    disk, mem = _both(tmp_path, files, "x")
    _same(disk, mem)
    assert not mem.ok and "字元上限" in mem.error


def test_與內建同名的macro_拒絕(tmp_path):
    files = {"macros/a.sql": _macro("ref", "x")}
    disk, mem = _both(tmp_path, files, "x")
    _same(disk, mem)
    assert not mem.ok and "內建同名" in mem.error


def test_壞掉的macro檔_略過並記錄_其他照常(tmp_path):
    files = {"macros/bad.sql": "{% macro broken( %}", "macros/good.sql": _macro("good", "ok")}
    disk, mem = _both(tmp_path, files, "{{ good() }}")
    _same(disk, mem)
    assert mem.ok and mem.sql == "ok"
    assert len(mem.macro_problems) == 1 and mem.macro_problems[0].startswith("macros/bad.sql:")


def test_字典內容不會被改動():
    files = {"macros/a.sql": _macro("a", "1"), "models/x.sql": "select 1"}
    before = dict(files)
    render_model("m.sql", macro_files=files, source="{{ a() }}")
    assert files == before


def test_不給macro_files時行為不變(tmp_path):
    """原有的兩種用法:給 code_root;或只給 source(不載入任何 macro)。"""
    _write(tmp_path, {"macros/a.sql": _macro("a", "1")})
    assert render_model("m.sql", code_root=tmp_path, source="{{ a() }}").sql == "1"
    assert not render_model("m.sql", source="{{ a() }}").ok
    assert DEFAULT_ADAPTER


def test_macro目錄互相包含_同一個檔只載入一次_兩種來源相同(tmp_path):
    """macro-paths 寫了 macros 與 macros/shared 時,shared 底下的檔會被列到兩次;
    載入兩次的話,它定義的 macro 會被記成「與自己同名」。"""
    files = {"macros/a.sql": _macro("a", "1"), "macros/shared/b.sql": _macro("b", "2")}
    for dirs in (("macros", "macros/shared"), ("macros/shared", "macros"), ("macros", "macros")):
        disk, mem = _both(tmp_path, files, "{{ a() }}{{ b() }}", macro_dirs=dirs)
        _same(disk, mem)
        assert mem.ok and mem.sql == "12" and mem.macro_conflicts == [], dirs
        assert mem.macros == ["a", "b"]


def test_檔數上限以去掉重複後的數量計算(monkeypatch):
    monkeypatch.setattr(dbt_render, "MAX_MACRO_FILES", 2)
    files = {"macros/a.sql": _macro("a", "1"), "macros/b.sql": _macro("b", "2")}
    assert render_model("m.sql", macro_files=files, source="{{ a() }}",
                        macro_dirs=("macros", "macros")).ok
