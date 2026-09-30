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


def _gz(files: dict[str, bytes | str]) -> bytes:
    """files:相對 repo 根的路徑 → 內容;全部放在 GitLab 打包的單一頂層目錄下。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        top = tarfile.TarInfo(TOP)
        top.type = tarfile.DIRTYPE
        tar.addfile(top)
        for name, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            ti = tarfile.TarInfo(f"{TOP}/{name}")
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
    "seeds/s.sql": "outside",
    "README.md": "x",
}


def _fails(result, *, not_in_error=(MARK,)):
    assert result.ok is False and result.files == {} and result.root_files == {} and result.error
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


def test_default_download_is_the_gitlab_tool(monkeypatch):
    """沒有注入時用 toolbox.gitlab.download_archive(未設定唯讀 token → 失敗,不連網)。"""
    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", "")
    err = _fails(load_dbt_project(SHA))
    assert "GITLAB_READ_TOKEN" in err
