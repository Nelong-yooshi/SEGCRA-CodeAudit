"""以 commit 取回 dbt 專案(orchestrator/dbt_project.py,#15 第 3 階段)。

不連網:以假的 download 取代 toolbox.gitlab.download_archive,回傳測試中當場產生的
tar.gz。重點是:只下載一次、從同一包讀 dbt_project.yml 與目錄、dbt_project.yml
當成不可信內容檢查、任何失敗都是 ok=False(不當成「沒有 macro」)、快取只收成功的
結果且有上限、快取內容不會被呼叫端改到。
"""
import gzip
import io
import tarfile

import pytest

from orchestrator import archive, dbt_project
from orchestrator.dbt_project import load_dbt_project
from toolbox.gitlab import ArchiveDownloadError

SHA = "0123456789abcdef0123456789abcdef01234567"
SHA2 = "fedcba9876543210fedcba9876543210fedcba98"
TOP = "proj-0123abc-0123abc"
MARK = "SECRETCONTENT"


def _gz(files: dict[str, bytes | str], top_name: str = TOP, dirs: tuple[str, ...] = ()) -> bytes:
    """files:相對 repo 根的路徑 → 內容;全部放在 GitLab 打包的單一頂層目錄下。
    dirs:要另外放進去的目錄成員(真實 GitLab 的打包每一層目錄都有自己的成員)。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in (top_name, *(f"{top_name}/{d}" for d in dirs)):
            entry = tarfile.TarInfo(name)
            entry.type = tarfile.DIRTYPE
            tar.addfile(entry)
        for name, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            ti = tarfile.TarInfo(f"{top_name}/{name}")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    return gzip.compress(buf.getvalue())


class _Download:
    """假的 download_archive:記錄呼叫,回傳準備好的壓縮檔或丟指定例外。"""

    def __init__(self, data=None, exc=None):
        self.data, self.exc, self.calls = data, exc, []

    def __call__(self, sha, path="", *, max_bytes):
        self.calls.append((sha, path, max_bytes))
        if self.exc:
            raise self.exc
        return self.data


@pytest.fixture(autouse=True)
def _fresh_cache():
    dbt_project.clear_cache()
    yield
    dbt_project.clear_cache()


BASIC = {
    "dbt_project.yml": "name: sample\nversion: '1.0'\n",
    "models/a.sql": "select 1",
    "models/schema.yml": "version: 2",
    "macros/m.sql": "{% macro m() %}1{% endmacro %}",
    "analyses/s.sql": "outside",           # 不在任何要收的目錄裡
    "README.md": "x",
}


def _fails(result, *, not_in_error=(MARK,)):
    assert result.ok is False and result.files == {} and result.root_files == {} and result.error
    assert result.names == ()
    for s in not_in_error:
        assert s not in result.error, result.error
    return result.error


# ------------------------------------------------------------------ 正常情況
def test_default_paths_from_repo_root():
    dl = _Download(_gz(BASIC))
    p = load_dbt_project(SHA, download=dl)
    assert p.ok, p.error
    assert p.files == {"models/a.sql": "select 1", "models/schema.yml": "version: 2",
                       "macros/m.sql": "{% macro m() %}1{% endmacro %}"}
    assert p.model_paths == ("models",) and p.macro_paths == ("macros",)
    assert p.root_files == {"dbt_project.yml": BASIC["dbt_project.yml"]}   # 沒有套件檔
    assert dl.calls == [(SHA, "", archive.MAX_COMPRESSED_BYTES)]      # 只下載一次


@pytest.mark.parametrize("names", [("packages.yml",), ("dependencies.yml",),
                                   ("packages.yml", "dependencies.yml")])
def test_root_package_files_are_returned_separately(names):
    """packages.yml / dependencies.yml 決定專案有沒有用 dbt 套件,從同一包取回;
    放在 root_files,不混進 model / macro 的 files(macro 反查只看那些目錄)。"""
    extra = {n: f"packages:\n  - package: {n}\n" for n in names}
    dl = _Download(_gz(BASIC | extra))
    p = load_dbt_project(SHA, download=dl)
    assert p.ok, p.error
    assert p.root_files == {"dbt_project.yml": BASIC["dbt_project.yml"]} | extra
    assert not set(p.root_files) & set(p.files)
    assert len(dl.calls) == 1


def test_root_files_come_from_the_project_dir_not_the_repo_root():
    """dbt 讀的是專案根目錄的 packages.yml;repo 根目錄或更深層的同名檔都不算。"""
    files = ({f"dbt/{k}": v for k, v in BASIC.items()}
             | {"packages.yml": "repo-root", "dbt/models/packages.yml": "nested"})
    p = load_dbt_project(SHA, "dbt", download=_Download(_gz(files)))
    assert p.ok, p.error
    assert set(p.root_files) == {"dbt_project.yml"}
    assert p.files["models/packages.yml"] == "nested"           # 在 models 裡是普通屬性檔


def test_directory_named_like_a_root_file_is_not_that_file():
    p = load_dbt_project(SHA, download=_Download(_gz(BASIC | {"packages.yml/x.yml": "x"})))
    assert p.ok, p.error
    assert set(p.root_files) == {"dbt_project.yml"}


def test_symlinked_package_file_fails_whole_project():
    """packages.yml 是符號連結:看不到真正內容,不可當成「沒用套件」——整包失敗。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in (("dbt_project.yml", b"name: x\n"), ("models/a.sql", b"1")):
            ti = tarfile.TarInfo(f"{TOP}/{name}")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        link = tarfile.TarInfo(f"{TOP}/packages.yml")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    err = _fails(load_dbt_project(SHA, download=_Download(gzip.compress(buf.getvalue()))))
    assert "符號連結" in err


def test_project_in_subdirectory():
    """專案不在 repo 根目錄:只下載那個目錄,回傳的路徑相對於專案根目錄。"""
    files = {f"dbt/{k}": v for k, v in BASIC.items()} | {"other/macros/x.sql": "no"}
    dl = _Download(_gz(files))
    p = load_dbt_project(SHA, "dbt", download=dl)
    assert p.ok, p.error
    assert set(p.files) == {"models/a.sql", "models/schema.yml", "macros/m.sql"}
    assert dl.calls[0][1] == "dbt"


def test_subdirectory_archive_shaped_like_real_gitlab():
    """真實 GitLab 實測(docs/09)的形狀:指定 path=dbt_sub 時,頂層目錄多一個 `-dbt_sub`
    後綴,底下**保留 dbt_sub/ 這一層**,每層目錄都有自己的成員。"""
    sha = "ce555752b104a16c105873f5351e4fe666a277ba"
    top = f"sample-project-{sha}-{sha}-dbt_sub"
    files = {"dbt_sub/dbt_project.yml": "name: sub\n",
             "dbt_sub/macros/sub_macro.sql": "{% macro m() %}1{% endmacro %}",
             "dbt_sub/models/sub_model.sql": "select 1"}
    data = _gz(files, top_name=top, dirs=("dbt_sub", "dbt_sub/macros", "dbt_sub/models"))
    p = load_dbt_project(sha, "dbt_sub", download=_Download(data))
    assert p.ok, p.error
    assert set(p.files) == {"macros/sub_macro.sql", "models/sub_model.sql"}
    assert p.root_files == {"dbt_project.yml": "name: sub\n"}


def test_trailing_slash_in_project_dir_is_accepted():
    files = {f"dbt/{k}": v for k, v in BASIC.items()}
    assert load_dbt_project(SHA, "dbt/", download=_Download(_gz(files))).ok


def test_custom_paths_from_dbt_project_yml():
    files = {"dbt_project.yml": "name: x\nmodel-paths: ['transform', 'marts/']\n"
                                "macro-paths: [lib/macros]\n",
             "transform/a.sql": "1", "marts/b.sql": "2", "lib/macros/m.sql": "3",
             "models/ignored.sql": "4", "macros/ignored.sql": "5"}
    p = load_dbt_project(SHA, download=_Download(_gz(files)))
    assert p.ok, p.error
    assert set(p.files) == {"transform/a.sql", "marts/b.sql", "lib/macros/m.sql"}
    assert p.model_paths == ("transform", "marts") and p.macro_paths == ("lib/macros",)


def test_legacy_source_paths():
    """dbt 1.0 以前叫 source-paths。"""
    files = {"dbt_project.yml": "source-paths: [src]\n", "src/a.sql": "1"}
    p = load_dbt_project(SHA, download=_Download(_gz(files)))
    assert p.ok and p.model_paths == ("src",) and "src/a.sql" in p.files


def test_model_paths_wins_over_source_paths():
    files = {"dbt_project.yml": "model-paths: [m]\nsource-paths: [s]\n",
             "m/a.sql": "1", "s/b.sql": "2"}
    p = load_dbt_project(SHA, download=_Download(_gz(files)))
    assert p.ok and set(p.files) == {"m/a.sql"}


def test_project_without_macros_dir_is_ok_and_empty_not_failure():
    """專案真的沒有 macro 是合法的(ok=True);與「取不到」(ok=False)分得開。"""
    files = {"dbt_project.yml": "name: x\n", "models/a.sql": "1"}
    p = load_dbt_project(SHA, download=_Download(_gz(files)))
    assert p.ok and set(p.files) == {"models/a.sql"}


# ------------------------------------------------------------------ 失敗:一律 ok=False
def test_missing_dbt_project_yml_fails():
    """找不到 dbt_project.yml 不可退回預設目錄:多半是 dbt.project_dir 設錯了。"""
    err = _fails(load_dbt_project(SHA, download=_Download(_gz({"models/a.sql": "1"}))))
    assert "沒有 dbt_project.yml" in err


def test_jinja_in_paths_reports_the_real_reason():
    err = _fails(load_dbt_project(SHA, download=_Download(_gz(
        {"dbt_project.yml": "macro-paths: [\"{{ env_var('X') }}\"]\n"}))))
    assert "樣板標記" in err


@pytest.mark.parametrize("yml", [
    "::: not yaml :::\n- [",
    "- a\n- b\n",                                     # 最上層不是物件
    "model-paths: models\n",                          # 不是清單
    "model-paths: []\n",                              # 空清單
    "model-paths: [" + ", ".join(f"d{i}" for i in range(dbt_project.MAX_PATHS + 1)) + "]\n",
    "model-paths: [1]\n",                             # 不是字串
    "macro-paths: [\"{{ env_var('X') }}\"]\n",        # 樣板標記
    "macro-paths: [/etc]\n",                          # 絕對路徑
    "macro-paths: ['../outside']\n",
    "macro-paths: ['a/../b']\n",
    "macro-paths: ['a b']\n",
    "macro-paths: ['a\\\\b']\n",
    "macro-paths: ['']\n",
], ids=["not-yaml", "top-list", "not-list", "empty", "too-many", "non-str", "jinja", "abs",
        "dotdot", "dotdot-mid", "space", "backslash", "blank"])
def test_bad_dbt_project_yml_fails(yml):
    """dbt_project.yml 來自待審的 commit:看不懂或可疑就失敗,不幫忙修正。"""
    _fails(load_dbt_project(SHA, download=_Download(_gz({"dbt_project.yml": yml}))))


@pytest.mark.parametrize("project_dir", ["../x", "/abs", "a/../b", "a b", "a\\b", "/", None, 1])
def test_bad_project_dir_fails_without_download(project_dir):
    dl = _Download(_gz(BASIC))
    _fails(load_dbt_project(SHA, project_dir, download=dl))
    assert dl.calls == []


@pytest.mark.parametrize("sha", ["main", SHA[:7], SHA.upper(), "", None])
def test_bad_sha_fails_without_download(sha):
    dl = _Download(_gz(BASIC))
    _fails(load_dbt_project(sha, download=dl))
    assert dl.calls == []


def test_download_failure_is_reported_with_fixed_text():
    dl = _Download(exc=ArchiveDownloadError("GitLab 回應 HTTP 403"))
    err = _fails(load_dbt_project(SHA, download=dl))
    assert "HTTP 403" in err


def test_unexpected_exception_is_not_echoed():
    dl = _Download(exc=RuntimeError(f"boom {MARK}"))
    err = _fails(load_dbt_project(SHA, download=dl))
    assert err.startswith("RuntimeError:")


def test_malicious_archive_fails_whole_project():
    """macros 目錄裡有符號連結:整個專案失敗(不是少一個 macro 照常回傳)。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in (("dbt_project.yml", b"name: x\n"), ("models/a.sql", b"1")):
            ti = tarfile.TarInfo(f"{TOP}/{name}")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        link = tarfile.TarInfo(f"{TOP}/macros/{MARK}.sql")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    _fails(load_dbt_project(SHA, download=_Download(gzip.compress(buf.getvalue()))))


def test_not_gzip_download_fails():
    _fails(load_dbt_project(SHA, download=_Download(b"<html>login page</html>")))


# ------------------------------------------------------------------ 快取
def test_same_commit_is_downloaded_once():
    dl = _Download(_gz(BASIC))
    a = load_dbt_project(SHA, download=dl)
    b = load_dbt_project(SHA, download=dl)
    assert a.ok and b.ok and a.files == b.files
    assert len(dl.calls) == 1


def test_cache_key_includes_project_dir_and_commit():
    files = BASIC | {f"dbt/{k}": v for k, v in BASIC.items()}
    dl = _Download(_gz(files))
    load_dbt_project(SHA, download=dl)
    load_dbt_project(SHA, "dbt", download=dl)
    load_dbt_project(SHA2, download=dl)
    assert len(dl.calls) == 3


def test_failures_are_not_cached():
    """失敗不快取:GitLab 暫時連不上時,下一次審查要能重試。"""
    bad = _Download(exc=ArchiveDownloadError("GitLab 回應 HTTP 502"))
    assert not load_dbt_project(SHA, download=bad).ok
    good = _Download(_gz(BASIC))
    assert load_dbt_project(SHA, download=good).ok
    assert len(good.calls) == 1


def test_returned_files_can_not_poison_the_cache():
    """呼叫端改了拿到的字典,下一次審查拿到的內容不能跟著變。"""
    dl = _Download(_gz(BASIC))
    first = load_dbt_project(SHA, download=dl)
    first.files["macros/m.sql"] = "{% macro m() %}DROP TABLE x{% endmacro %}"
    first.files["macros/evil.sql"] = "x"
    first.root_files["packages.yml"] = "packages: []"          # 假裝有用套件
    again = load_dbt_project(SHA, download=dl)                  # 這次來自快取
    assert again.files["macros/m.sql"] == "{% macro m() %}1{% endmacro %}"
    assert "macros/evil.sql" not in again.files
    assert "packages.yml" not in again.root_files
    again.root_files["dependencies.yml"] = "x"                  # 改快取給的那一份
    again.files["macros/m.sql"] = "tampered"                    # 改快取給的那一份
    third = load_dbt_project(SHA, download=dl)
    assert third.files["macros/m.sql"] == "{% macro m() %}1{% endmacro %}"
    assert set(third.root_files) == {"dbt_project.yml"}
    assert len(dl.calls) == 1


def test_cache_entry_limit_evicts_least_recently_used(monkeypatch):
    monkeypatch.setattr(dbt_project, "CACHE_MAX_ENTRIES", 2)
    shas = ["a" * 40, "b" * 40, "c" * 40]
    dl = _Download(_gz(BASIC))
    for s in shas:
        load_dbt_project(s, download=dl)
    load_dbt_project(shas[0], download=dl)          # 最舊的被丟掉,要重抓
    assert [c[0] for c in dl.calls] == shas + [shas[0]]


def test_cache_byte_limit(monkeypatch):
    size = dbt_project._size(load_dbt_project(SHA, download=_Download(_gz(BASIC))))
    dbt_project.clear_cache()
    monkeypatch.setattr(dbt_project, "CACHE_MAX_BYTES", size * 2 - 1)   # 只放得下一筆
    dl = _Download(_gz(BASIC))
    load_dbt_project("a" * 40, download=dl)
    load_dbt_project("b" * 40, download=dl)
    load_dbt_project("a" * 40, download=dl)
    assert len(dl.calls) == 3


def test_project_larger_than_cache_is_not_cached_and_does_not_flush_others(monkeypatch):
    """放不下的大專案不進快取;而且不能為了塞它,先把其他正常的快取項目全部擠掉。"""
    small = _Download(_gz(BASIC))
    big = _Download(_gz(BASIC | {"macros/big.sql": "x" * 5000}))
    monkeypatch.setattr(dbt_project, "CACHE_MAX_BYTES", 3000)
    assert load_dbt_project("a" * 40, download=small).ok
    assert load_dbt_project("b" * 40, download=big).ok
    assert load_dbt_project("b" * 40, download=big).ok          # 大的沒被快取
    assert len(big.calls) == 2
    assert load_dbt_project("a" * 40, download=small).ok        # 小的還在快取裡
    assert len(small.calls) == 1


def test_cache_size_counts_root_files(monkeypatch):
    """快取總量要連 root_files 一起算:否則一個很大的 packages.yml 可以繞過上限。"""
    big = _Download(_gz(BASIC | {"packages.yml": "x" * 5000}))
    monkeypatch.setattr(dbt_project, "CACHE_MAX_BYTES", 3000)
    assert load_dbt_project(SHA, download=big).ok
    assert load_dbt_project(SHA, download=big).ok
    assert len(big.calls) == 2                                  # 太大,沒進快取


# ------------------------------------------------------------------ seed / snapshot / 檔名清單
FULL = BASIC | {
    "models/py/score.py": "def model(dbt, session): ...",
    "models/schema.yaml": "version: 2\n",
    "seeds/codes.csv": "a,b\n",
    "seeds/props.yml": "version: 2\nseeds:\n  - name: codes\n",
    "seeds/notes.txt": "x",
    "snapshots/snap.sql": "{% snapshot snap_txn %}select 1{% endsnapshot %}",
    "snapshots/dims.yml": "snapshots:\n  - name: snap_dim\n",
}


def test_seed_and_snapshot_dirs_are_collected_and_listed():
    """#20 review:#19 需要 seed / Python model 的名稱(只要檔名)與 snapshot 的名稱
    (寫在區塊上,要讀內容)。內容只收 .sql / .yml / .yaml;檔名清單收所有副檔名。"""
    p = load_dbt_project(SHA, download=_Download(_gz(FULL)))
    assert p.ok, p.error
    assert p.seed_paths == ("seeds",) and p.snapshot_paths == ("snapshots",)
    assert {"snapshots/snap.sql", "snapshots/dims.yml", "seeds/props.yml",
            "models/schema.yaml"} <= set(p.files)
    assert "seeds/codes.csv" not in p.files and "models/py/score.py" not in p.files
    assert p.names == ("models/a.sql", "models/py/score.py", "models/schema.yaml",
                       "models/schema.yml", "seeds/codes.csv", "seeds/notes.txt",
                       "seeds/props.yml", "snapshots/dims.yml", "snapshots/snap.sql")
    assert "macros/m.sql" not in p.names and "analyses/s.sql" not in p.names


def test_names_are_relative_to_the_project_dir():
    files = {f"dbt/{k}": v for k, v in FULL.items()} | {"seeds/outside.csv": "x"}
    p = load_dbt_project(SHA, "dbt", download=_Download(_gz(files)))
    assert p.ok, p.error
    assert "seeds/codes.csv" in p.names and "seeds/outside.csv" not in p.names
    assert all(not n.startswith("dbt/") for n in p.names)


@pytest.mark.parametrize("yml, seeds, snapshots", [
    ("seed-paths: [data]\nsnapshot-paths: [snaps]\n", ("data",), ("snaps",)),
    ("data-paths: [legacy]\n", ("legacy",), ("snapshots",)),                 # dbt 1.0 以前
    ("seed-paths: [new]\ndata-paths: [old]\n", ("new",), ("snapshots",)),    # 新名稱為準
], ids=["custom", "legacy-data-paths", "new-wins"])
def test_seed_and_snapshot_paths_from_dbt_project_yml(yml, seeds, snapshots):
    p = load_dbt_project(SHA, download=_Download(_gz({"dbt_project.yml": yml})))
    assert p.ok and p.seed_paths == seeds and p.snapshot_paths == snapshots


@pytest.mark.parametrize("yml", [
    "seed-paths: [\"{{ env_var('X') }}\"]\n", "snapshot-paths: ['../out']\n",
    "seed-paths: []\n", "snapshot-paths: snaps\n",
], ids=["seed-jinja", "snapshot-dotdot", "seed-empty", "snapshot-not-list"])
def test_bad_seed_or_snapshot_paths_fail(yml):
    """seed / snapshot 目錄設定同樣是不可信內容:看不懂就失敗,不幫忙修正。"""
    _fails(load_dbt_project(SHA, download=_Download(_gz({"dbt_project.yml": yml}))))


def test_symlink_in_seed_dir_fails_whole_project():
    """seed 目錄要收屬性檔的內容(可能設 alias):裡面的符號連結同樣整包失敗。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        ti = tarfile.TarInfo(f"{TOP}/dbt_project.yml")
        ti.size = 8
        tar.addfile(ti, io.BytesIO(b"name: x\n"))
        link = tarfile.TarInfo(f"{TOP}/seeds/props.yml")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
    _fails(load_dbt_project(SHA, download=_Download(gzip.compress(buf.getvalue()))))


def test_symlinked_parent_of_seed_dir_fails_whole_project():
    """seed-paths 是 data/seeds、而 data 是連結:seed 目錄看起來是空的,名單會被當成完整
    (ref() 找不到的 seed 被判成不存在)→ 整包失敗。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        ti = tarfile.TarInfo(f"{TOP}/dbt_project.yml")
        yml = b"name: x\nseed-paths: ['data/seeds']\n"
        ti.size = len(yml)
        tar.addfile(ti, io.BytesIO(yml))
        link = tarfile.TarInfo(f"{TOP}/data")
        link.type, link.linkname = tarfile.SYMTYPE, "../shared"
        tar.addfile(link)
    error = _fails(load_dbt_project(SHA, download=_Download(gzip.compress(buf.getvalue()))))
    assert "上層" in error


def test_names_survive_the_cache():
    dl = _Download(_gz(FULL))
    first = load_dbt_project(SHA, download=dl)
    again = load_dbt_project(SHA, download=dl)               # 來自快取
    assert again.names == first.names and again.seed_paths == ("seeds",)
    assert again.snapshot_paths == ("snapshots",) and len(dl.calls) == 1


def test_cache_size_counts_names():
    """檔名清單也算進快取總量:否則上萬個檔名可以繞過上限。"""
    project = dbt_project.DbtProject(ok=True, names=("seeds/" + "x" * 1000 + ".csv",))
    assert dbt_project._size(project) >= 1000


# ------------------------------------------------------------------ extra_ref_names
def test_extra_ref_names_from_every_source():
    p = load_dbt_project(SHA, download=_Download(_gz(FULL)))
    assert dbt_project.extra_ref_names(p) == {"codes", "score", "snap_txn", "snap_dim"}


@pytest.mark.parametrize("extra", [
    {"seeds/README.md": "x", "seeds/data.json": "{}"},                 # 不是 .csv
    {"analyses/x.csv": "a", "macros/y.py": "x"},                       # 不在 seed / model 目錄
    {"snapshots/notes.sql": "-- 沒有 snapshot 區塊的檔案"},
    {"seeds/aux.md": "x", "seeds/notes\x1b[31m.txt": "x", "docs/bad\x1b[31m.csv": "a"},
    {"seeds/.csv": "a", "seeds/LICENSE": "x", "models/py/.py": "x"},             # 沒有主檔名
], ids=["non-csv", "outside-dirs", "no-block", "odd-names-of-non-resources", "no-stem"])
def test_extra_ref_names_ignores_files_that_are_not_resources(extra):
    p = load_dbt_project(SHA, download=_Download(_gz(FULL | extra)))
    assert dbt_project.extra_ref_names(p) == {"codes", "score", "snap_txn", "snap_dim"}


def test_snapshot_block_with_whitespace_control():
    files = BASIC | {"snapshots/s.sql": "{%- snapshot snap_ws -%}\nselect 1\n{%- endsnapshot -%}"}
    p = load_dbt_project(SHA, download=_Download(_gz(files)))
    assert dbt_project.extra_ref_names(p) == {"snap_ws"}


@pytest.mark.parametrize("extra", [
    {"snapshots/s.sql": "{% snapshot {{ var('n') }} %}select 1{% endsnapshot %}"},   # 樣板名稱
    {"snapshots/s.sql": "{% snapshot %}select 1{% endsnapshot %}"},                 # 沒有名稱
    {"snapshots/s.sql": "{% snapshot a b %}select 1{% endsnapshot %}"},             # 看不懂
    {"snapshots/s.sql": "{% snapshot snap-x %}select 1{% endsnapshot %}"},          # 不是識別字
    {"snapshots/s.yml": "snapshots: [1\n"},                                        # yml 解析不了
    {"snapshots/s.yml": "snapshots: snap_x\n"},                                    # 不是清單
    {"snapshots/s.yml": "snapshots:\n  - relation: x\n"},                          # 沒有名稱
    {"snapshots/s.yml": "- a\n"},                                                  # 最上層不是物件
    {"seeds/bad\x1b[31m.csv": "a"},                                                # seed 檔名含控制字元
    {"models/py/bad\n.py": "def model(): ..."},                                    # .py 檔名含控制字元
], ids=["templated", "no-name", "garbled", "not-ident", "yml-unreadable", "yml-not-list", "yml-no-name",
        "yml-top-list", "seed-control-char", "py-control-char"])
def test_extra_ref_names_is_none_when_anything_is_unreadable(extra):
    """看不懂就回 None(= 名單不完整):#19 不會因此把找不到的 ref() 判成確定不存在。"""
    p = load_dbt_project(SHA, download=_Download(_gz(BASIC | extra)))
    assert p.ok, p.error
    assert dbt_project.extra_ref_names(p) is None


def test_snapshot_yml_total_size_is_capped(monkeypatch):
    """MR 可以放很多大 yml 拖慢審查:snapshot 的 yml 合計超過上限就不解析,名單視為不完整。
    (每個檔案單獨都在上限內,合計才超過)"""
    yml = "snapshots:\n  - name: snap_a\n"
    p = load_dbt_project(SHA, download=_Download(_gz(BASIC | {"snapshots/a.yml": yml,
                                                               "snapshots/b.yml": yml})))
    assert dbt_project.extra_ref_names(p) == {"snap_a"}
    monkeypatch.setattr(dbt_project, "MAX_SNAPSHOT_YML_CHARS", len(yml) * 2 - 1)
    assert dbt_project.extra_ref_names(p) is None


def test_snapshot_yml_is_loaded_safely():
    """yml 是不可信內容:只用安全載入器。用不安全的載入器時,這個標籤會被執行並得到
    名稱 abc;安全載入器不認得它 → 解析失敗 → None。"""
    yml = "snapshots:\n  - name: !!python/object/apply:builtins.str [abc]\n"
    p = load_dbt_project(SHA, download=_Download(_gz(BASIC | {"snapshots/s.yml": yml})))
    assert p.ok, p.error
    assert dbt_project.extra_ref_names(p) is None


@pytest.mark.parametrize("project", [None, "x", dbt_project.DbtProject(ok=False, error="e")],
                         ids=["none", "str", "failed"])
def test_extra_ref_names_without_a_project_is_none(project):
    assert dbt_project.extra_ref_names(project) is None


def test_default_download_is_the_gitlab_tool(monkeypatch):
    """沒有注入時用 toolbox.gitlab.download_archive(未設定唯讀 token → 失敗,不連網)。"""
    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", "")
    err = _fails(load_dbt_project(SHA))
    assert "GITLAB_READ_TOKEN" in err
