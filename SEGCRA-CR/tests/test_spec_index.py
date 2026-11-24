"""spec_index:規格依 project 編號對應程式。

管線的接線(find_spec、受影響的 model、整條 review_mr)在 test_dbt_pipeline_wiring、
test_dbt_affected、test_dbt_exec;這裡只測模組本身。
"""
import gzip
import io
import tarfile

import pytest

from orchestrator import archive, spec_index
from orchestrator.spec_index import (SpecIndex, build_index, choose, load_index, project_code,
                                     project_of)


def _spec(project: str, name: str = "X", extra: str = "") -> str:
    return (f"# {name}\n\n## 基本資料\n\n| 欄位 | 值 |\n|---|---|\n"
            f"| 序號 / 程式名稱 | {project} / {name} |\n{extra}")


# ─────────────────── 規格的編號 ───────────────────

@pytest.mark.parametrize("row, want", [
    ("| 序號 / 程式名稱 | DEMO02 / RULE_X1 |", "DEMO02"),
    ("| 序號/程式名稱 | DEMO02/RULE_X1 |", "DEMO02"),                  # 沒有空白
    ("|序號 /  程式名稱|  P-02  /  x |", "P-02"),
    ("| 序號 ／ 程式名稱 | Ｐ０２ ／ ATM |", "P02"),                          # 全形轉半形
    ("| 序號 / 程式名稱 | P02 |", "P02"),                                    # 沒有程式名稱
    ("| 序號 / 程式名稱 | P02 / x | 備註 |", "P02"),                          # 多一欄
    ("| 序號 / 程式名稱 |  / ATM |", None),                                  # 編號空白
    ("| 序號 / 程式名稱 | P 02 / ATM |", None),                              # 含空白
    ("| 序號 / 程式名稱 | ../x / ATM |", None),                              # 不合法字元
    ("| 序號 / 程式名稱 | 專案二 / ATM |", None),                             # 非 ASCII
    ("| 序號 / 程式名稱 | " + "P" * 65 + " / ATM |", None),                  # 太長
    ("| 程式名稱 | P02 |", None),                                            # 不是那一列
    ("序號 / 程式名稱:P02", None),                                          # 不是表格
])
def test_project_code(row, want):
    assert project_code(f"# 規格\n\n## 基本資料\n\n| 欄位 | 值 |\n|---|---|\n{row}\n") == want


def test_project_code_bom_and_crlf():
    assert project_code("﻿" + _spec("P02").replace("\n", "\r\n")) == "P02"


def test_project_code_ignores_code_blocks():
    text = "# x\n\n```\n| 序號 / 程式名稱 | FAKE / x |\n```\n" + _spec("P02")
    assert project_code(text) == "P02"
    assert project_code("# x\n\n```\n| 序號 / 程式名稱 | FAKE / x |\n```\n") is None


def test_project_code_conflicting_rows_is_none():
    """兩列寫了不同的編號:不自行擇一。寫了兩次同一個值沒關係。"""
    assert project_code(_spec("P01") + "| 序號 / 程式名稱 | P02 / x |\n") is None
    assert project_code(_spec("P01") + "| 序號 / 程式名稱 | P01 / x |\n") == "P01"


@pytest.mark.parametrize("text", [None, 3, "", "# 沒有基本資料\n"])
def test_project_code_missing(text):
    assert project_code(text) is None


# ─────────────────── 對照表 ───────────────────

def test_build_index_only_top_level_specs():
    idx = build_index({"specs/a.md": _spec("P01"), "specs/sub/b.md": _spec("P02"),
                       "docs/c.md": _spec("P03"), "specs/d.txt": _spec("P04"),
                       "specs/e.md": "", "specs/f.md": "# 沒有編號"})
    assert idx.by_project == {"P01": ("a",)}
    assert set(idx.specs) == {"a", "f"}


def test_build_index_groups_same_project():
    idx = build_index({"specs/b.md": _spec("P01"), "specs/a.md": _spec("P01")})
    assert idx.by_project == {"P01": ("a", "b")}


@pytest.mark.parametrize("path, prefix, model_paths, want", [
    ("models/P01/x.sql", "", ("models",), ("P01", None)),
    ("models/P01/a/b/c.sql", "", ("models",), ("P01", None)),
    ("dbt/models/P01/x.sql", "dbt", ("models",), ("P01", None)),
    ("src/P01/x.sql", "", ("src", "models"), ("P01", None)),               # 自訂 model-paths
    ("models/x.sql", "", ("models",), (None, "直接放在 model 目錄底下")),
    ("models/P01/x.sql", "dbt", ("models",), (None, "不在 dbt 專案的目錄裡")),
    ("sql/x.sql", "", ("models",), (None, "不在 dbt 的 model 目錄裡")),
    ("models/P 01/x.sql", "", ("models",), (None, "不是 project 編號的格式")),
    ("models/../P01/x.sql", "", ("models",), (None, "路徑不合法")),
    ("/models/P01/x.sql", "", ("models",), (None, "路徑不合法")),
])
def test_project_of(path, prefix, model_paths, want):
    project, reason = project_of(path, build_index({}, prefix=prefix, model_paths=model_paths))
    assert project == want[0]
    assert (reason is None) if want[1] is None else (want[1] in reason)


def test_project_of_root_model_path():
    """model-paths 設成專案根目錄時,第一層資料夾就是編號。"""
    assert project_of("P01/x.sql", build_index({}, model_paths=(".",)))[0] == "P01"


def _idx(**specs):
    return build_index({f"specs/{name}.md": _spec(project) for name, project in specs.items()})


def test_choose_matches_one_spec():
    got = choose(_idx(a="P01", b="P02"), [{"path": "models/P01/x.sql"},
                                         {"path": "models/P01/sources.yml"},
                                         {"path": "macros/m.sql"}])
    assert (got.code, got.note) == ("a", None) and "P01" in got.text


def test_choose_python_model_counts():
    assert choose(_idx(a="P01"), [{"path": "models/P01/x.py"}]).code == "a"


def test_choose_custom_macro_dir_is_not_a_program():
    idx = build_index({"specs/a.md": _spec("P01")}, macro_paths=("lib/macros",))
    assert choose(idx, [{"path": "lib/macros/m.sql"}]).note is None
    assert "不屬於任何 project" in choose(idx, [{"path": "macros/m.sql"}]).note


def test_choose_error_is_unavailable():
    got = choose(SpecIndex(error="no_read_token"), [{"path": "models/P01/x.sql"}])
    assert (got.code, got.unavailable, got.note) == (None, "no_read_token", None)


def test_choose_outside_takes_priority_over_match():
    """同一個 MR 有一個檔不在 project 裡:整個 MR 不對規格(不能只驗對得到的那一半)。"""
    got = choose(_idx(a="P01"), [{"path": "models/P01/x.sql"}, {"path": "models/y.sql"}])
    assert got.code is None and "models/y.sql" in got.note


def test_choose_note_lists_at_most_five():
    files = [{"path": f"models/f{i}.sql"} for i in range(8)]
    note = choose(_idx(), files).note
    assert "等 8 個" in note and "models/f5.sql" not in note


def test_choose_note_hides_unsafe_paths():
    got = choose(_idx(), [{"path": "models/<script>.sql"}, {"path": "models/x‮.sql"}])
    assert "<script>" not in got.note and "‮" not in got.note


def test_choose_ignores_odd_entries():
    assert choose(_idx(a="P01"), ["x", None, {"path": None}, {"path": "models/P01/x.sql"}]).code == "a"


# ─────────────────── 讀規格 ───────────────────

SHA = "c3" * 20


def _tgz(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        top = tarfile.TarInfo("proj-c3c3-specs")
        top.type = tarfile.DIRTYPE
        tar.addfile(top)
        for name, content in files.items():
            data = content.encode("utf-8")
            ti = tarfile.TarInfo(f"proj-c3c3-specs/{name}")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    return gzip.compress(buf.getvalue())


@pytest.fixture
def real(monkeypatch):
    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", "test-write-token")
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", "test-read-token")
    gitlab.pin_review_ref(SHA)
    yield gitlab
    gitlab.pin_review_ref(None)


def test_load_real_reads_specs_of_pinned_commit(real):
    calls = []

    def download(sha, path, *, max_bytes):
        calls.append((sha, path, max_bytes))
        return _tgz({"specs/a.md": _spec("P01"), "specs/notes.txt": "x", "specs/b.sql": "x"})
    idx = load_index({}, real_mode=True, download=download)
    assert idx.error is None and idx.by_project == {"P01": ("a",)}
    assert calls == [(SHA, "specs", archive.MAX_COMPRESSED_BYTES)]


@pytest.mark.parametrize("setup, code", [
    (lambda g: g.pin_review_ref(None), "no_review_ref"),
    (lambda g: setattr(g, "GITLAB_READ_TOKEN", ""), "no_read_token"),
    (lambda g: setattr(g, "GITLAB_READ_TOKEN", g.GITLAB_TOKEN), "no_read_token"),   # 兩把同一把
    (lambda g: None, "fetch_failed"),
])
def test_load_real_failures(real, setup, code):
    setup(real)

    def download(sha, path, *, max_bytes):
        raise RuntimeError("token=SECRET")
    idx = load_index({}, real_mode=True, download=download)
    assert (idx.error, idx.specs, idx.by_project) == (code, {}, {})


@pytest.mark.parametrize("data", [b"not gzip", b""])
def test_load_real_bad_archive(real, data):
    assert load_index({}, real_mode=True, download=lambda *a, **k: data).error == "fetch_failed"


def test_load_real_empty_specs_is_no_spec(real):
    """包裡只有頂層目錄(specs/ 底下沒有檔案):確定沒有規格,不是取不到。"""
    idx = load_index({}, real_mode=True, download=lambda *a, **k: _tgz({}))
    assert idx.error is None and idx.by_project == {}


def test_load_real_symlink_fails_whole(real):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        top = tarfile.TarInfo("t")
        top.type = tarfile.DIRTYPE
        tar.addfile(top)
        link = tarfile.TarInfo("t/specs/a.md")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    data = gzip.compress(buf.getvalue())
    assert load_index({}, real_mode=True, download=lambda *a, **k: data).error == "fetch_failed"


def test_load_mock_overlays_mr_versions(tmp_path):
    (tmp_path / "a.md").write_text(_spec("P01"), encoding="utf-8")
    (tmp_path / "b.md").write_text(_spec("P02"), encoding="utf-8")
    (tmp_path / "dir.md").mkdir()
    idx = load_index({"a": "", "c": _spec("P03"), "b": _spec("P09")}, real_mode=False,
                     local_dir=tmp_path)
    assert idx.by_project == {"P03": ("c",), "P09": ("b",)}


def test_load_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(spec_index, "build_index", lambda *a, **k: 1 / 0)
    assert load_index({}, real_mode=False, local_dir=tmp_path).error == "fetch_failed"


def test_current_index_follows_context():
    import contextvars
    idx = _idx(a="P01")
    spec_index.use_index(idx)
    try:
        assert contextvars.copy_context().run(spec_index.current_index) is idx
    finally:
        spec_index.use_index(None)
    assert spec_index.current_index() is None


# ─────────────────── 打包解壓的副檔名 ───────────────────

def test_extract_archive_suffix_choices():
    data = _tgz({"specs/a.md": "# a", "specs/b.sql": "SELECT 1"})
    assert set(archive.extract_archive(data, ("specs",)).files) == {"specs/b.sql"}
    md = archive.extract_archive(data, ("specs",), suffixes=archive.SPEC_SUFFIXES)
    assert set(md.files) == {"specs/a.md"}
    bad = archive.extract_archive(data, ("specs",), suffixes=(".md", ".sh"))
    assert not bad.ok and bad.files == {}
