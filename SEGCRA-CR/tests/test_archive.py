"""安全解壓(orchestrator/archive.py,#15)。

壓縮檔來自待審 MR 的 commit,是不可信內容。這裡的每一條惡意案例都在測試中當場
產生(不放任何二進位檔進 repo),確認:任何不對勁都讓**整包**失敗、錯誤訊息不回顯
壓縮檔內容、暫存目錄一定被刪掉、成功時只拿到指定目錄下的 .sql / .yml。

模組是多層防護(自己的檢查 → tarfile 的 filter="data" → 解出後核對)。所以失敗案例
**都要驗證是被哪一層、以什麼理由擋下的**:只驗「有失敗」的話,某一層壞了也會被
後面的層遮住,測試照樣通過,等於那一層沒有測試在守。
"""
import ast
import gzip
import io
import os
import pathlib
import random
import shutil
import subprocess
import sys
import tarfile
import tracemalloc

import pytest

from orchestrator import archive
from orchestrator.archive import ArchiveResult, extract_archive

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
TOP = "proj-0123abc-0123abc"
DIRS = ("models", "macros")
MARK = "SECRETNAME"          # 惡意成員名稱裡的記號:錯誤訊息不可出現它

# 各層失敗理由的固定字樣(與 archive.py 的訊息對應)
LINK = "符號連結、硬連結或特殊檔案"
DOTS = "路徑含 ..、. 或空段落"
ABS = "絕對路徑"
SEP = "反斜線或冒號"
CTRL = "控制字元"
UNPARSABLE = "壓縮檔無法解析或無法解出"


# ------------------------------------------------------------------ 產生壓縮檔
def _info(name, type_=tarfile.REGTYPE, size=0, linkname=""):
    ti = tarfile.TarInfo(name)
    ti.type = type_
    ti.size = size
    ti.linkname = linkname
    ti.mode = 0o755 if type_ == tarfile.DIRTYPE else 0o644
    return ti


def _tar(entries, fmt=tarfile.PAX_FORMAT) -> bytes:
    """entries:(名稱, 內容 bytes)是一般檔;(名稱, TarInfo 型別, linkname)三項的是
    其他類型(tarfile 的型別常數本身就是 bytes,所以用項數區分,不用型別判斷);
    名稱結尾是 / 表示目錄。回傳未壓縮的 tar。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt) as tar:
        for entry in entries:
            name = entry[0]
            if name.endswith("/"):
                tar.addfile(_info(name.rstrip("/"), tarfile.DIRTYPE))
            elif len(entry) == 3:
                tar.addfile(_info(name, entry[1], linkname=entry[2]))
            else:
                tar.addfile(_info(name, size=len(entry[1])), io.BytesIO(entry[1]))
    return buf.getvalue()


def _gz(entries, **kw) -> bytes:
    return gzip.compress(_tar(entries, **kw))


def _project(extra=()):
    """GitLab 打包的正常形狀:單一頂層目錄 + 目錄成員 + 檔案。"""
    return [(f"{TOP}/",), (f"{TOP}/models/",), (f"{TOP}/macros/",),
            (f"{TOP}/models/a.sql", b"select 1\n"),
            (f"{TOP}/models/schema.yml", b"version: 2\n"),
            (f"{TOP}/macros/m.sql", b"{% macro m() %}1{% endmacro %}\n"),
            *extra]


def _fails(result: ArchiveResult, reason: str | None = None, *, not_in_error=(MARK,)):
    """失敗的共同要求:ok=False、沒有任何檔案、錯誤訊息存在且不回顯內容;
    給了 reason 時,錯誤訊息必須含這個理由(驗證是哪一層擋下的)。"""
    assert result.ok is False
    assert result.files == {}
    assert result.error
    for s in not_in_error:
        assert s not in result.error, result.error
    if reason is not None:
        assert reason in result.error, result.error
    return result.error


def test_helper_really_builds_special_members():
    """測試用的產生器本身要先驗:它曾經把符號連結誤建成一般檔,惡意案例全都沒測到。"""
    raw = _tar([(f"{TOP}/l", tarfile.SYMTYPE, "/x"), (f"{TOP}/f", tarfile.FIFOTYPE, ""),
                (f"{TOP}/d/",), (f"{TOP}/r", b"data")])
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        kinds = {m.name: (m.issym(), m.isfifo(), m.isdir(), m.isreg()) for m in tar}
    assert kinds == {f"{TOP}/l": (True, False, False, False),
                     f"{TOP}/f": (False, True, False, False),
                     f"{TOP}/d": (False, False, True, False),
                     f"{TOP}/r": (False, False, False, True)}


# -------------------------------------------------------------------- 正常情況
def test_extracts_only_wanted_dirs_and_suffixes():
    data = _gz(_project([
        (f"{TOP}/macros/sub/deep.sql", b"-- deep\n"),
        (f"{TOP}/models/README.md", b"not wanted"),          # 副檔名不收
        (f"{TOP}/models/x.SQL", b"select 2\n"),               # 大寫副檔名照收
        (f"{TOP}/seeds/data.sql", b"outside"),                 # 目錄外不收
        (f"{TOP}/dbt_project.yml", b"name: x\n"),              # 目錄外不收
    ]))
    r = extract_archive(data, DIRS)
    assert r.ok, r.error
    assert r.files == {
        "models/a.sql": "select 1\n",
        "models/schema.yml": "version: 2\n",
        "models/x.SQL": "select 2\n",
        "macros/m.sql": "{% macro m() %}1{% endmacro %}\n",
        "macros/sub/deep.sql": "-- deep\n",
    }


def test_prefix_is_a_directory_boundary_not_a_string_prefix():
    """"models" 不可順便收下 "models_backup/"。"""
    r = extract_archive(_gz(_project([(f"{TOP}/models_backup/b.sql", b"x")])), DIRS)
    assert r.ok and "models_backup/b.sql" not in r.files


def test_nested_prefix():
    """dbt 專案不在 repo 根目錄時,目錄是多層的(例如 dbt/macros)。"""
    r = extract_archive(_gz([(f"{TOP}/",), (f"{TOP}/dbt/macros/m.sql", b"x"),
                             (f"{TOP}/other/macros/n.sql", b"y")]), ("dbt/macros",))
    assert r.ok and r.files == {"dbt/macros/m.sql": "x"}


def test_no_wanted_files_is_ok_and_empty():
    """專案真的沒有 macro 是合法的:回 ok=True、空結果(與「失敗」分得開)。"""
    r = extract_archive(_gz([(f"{TOP}/",), (f"{TOP}/docs/a.md", b"x")]), DIRS)
    assert r.ok is True and r.files == {} and r.error is None


def test_dangerous_members_outside_wanted_dirs_are_discarded_not_extracted():
    """指定目錄外的連結不會被解出(只收指定目錄),所以丟棄即可、不讓整包失敗;
    否則舊版 GitLab 不支援 path 參數、拿到整個 repo 時,任何無關的連結都會卡住審查。"""
    r = extract_archive(_gz(_project([
        (f"{TOP}/docs/link", tarfile.SYMTYPE, "/etc/passwd"),
        (f"{TOP}/docs/hard", tarfile.LNKTYPE, f"{TOP}/models/a.sql"),
        (f"{TOP}/docs/fifo", tarfile.FIFOTYPE, ""),
    ])), DIRS)
    assert r.ok, r.error
    assert set(r.files) == {"models/a.sql", "models/schema.yml", "macros/m.sql"}


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git 產生真實的打包格式")
def test_real_git_archive_format(tmp_path):
    """GitLab 的 /repository/archive 就是 git archive:用真的 git 產生一包來解。"""
    repo = tmp_path / "repo"
    (repo / "models").mkdir(parents=True)
    (repo / "macros").mkdir()
    (repo / "models" / "a.sql").write_bytes(b"select 1\n")
    (repo / "macros" / "m.sql").write_bytes("-- 中文註解\n".encode("utf-8"))
    (repo / "README.md").write_bytes(b"x")
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
           "-c", "core.autocrlf=false"]
    for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(git + cmd, cwd=repo, check=True)
    out = tmp_path / "a.tar.gz"
    subprocess.run(git + ["archive", "--format=tar.gz", f"--prefix={TOP}/", "HEAD",
                          "-o", str(out)], cwd=repo, check=True)
    r = extract_archive(out.read_bytes(), DIRS)
    assert r.ok, r.error
    assert r.files == {"models/a.sql": "select 1\n", "macros/m.sql": "-- 中文註解\n"}


# ------------------------------------------------------ 惡意成員:整包失敗
@pytest.mark.parametrize("extra", [
    [(f"{TOP}/macros/{MARK}.sql", tarfile.SYMTYPE, "/etc/passwd")],        # 符號連結
    [(f"{TOP}/macros/{MARK}.sql", tarfile.SYMTYPE, "../models/a.sql")],    # 指向目錄內
    [(f"{TOP}/macros/{MARK}.sql", tarfile.LNKTYPE, f"{TOP}/models/a.sql")],  # 硬連結
    [(f"{TOP}/macros/{MARK}.sql", tarfile.FIFOTYPE, "")],                  # FIFO
    [(f"{TOP}/macros/{MARK}.sql", tarfile.CHRTYPE, "")],                   # 字元裝置
    [(f"{TOP}/macros/{MARK}.sql", tarfile.BLKTYPE, "")],                   # 區塊裝置
    [(f"{TOP}/macros/{MARK}.sql", tarfile.CONTTYPE, "")],                  # contiguous
    [(f"{TOP}/models/{MARK}", tarfile.SYMTYPE, "../shared")],              # 沒有副檔名
    [(f"{TOP}/macros/{MARK}.txt", tarfile.SYMTYPE, "/etc/passwd")],        # 不收的副檔名
], ids=["sym-abs", "sym-inside", "hard", "fifo", "chr", "blk", "cont", "sym-noext",
        "sym-txt"])
def test_links_and_special_files_inside_wanted_dirs_fail_whole_archive(extra):
    """由第一層(自己的類型檢查)擋下——不是等 filter 或解出後核對才發現。"""
    _fails(extract_archive(_gz(_project(extra)), DIRS), LINK)


def test_symlink_replacing_wanted_dir_itself_fails():
    """macros 本身是符號連結(指向別處):若只是略過,反查會以為專案沒有 macro。"""
    entries = [(f"{TOP}/",), (f"{TOP}/models/a.sql", b"select 1"),
               (f"{TOP}/macros", tarfile.SYMTYPE, "../shared_macros")]
    _fails(extract_archive(_gz(entries), DIRS), LINK)


@pytest.mark.parametrize("name, reason", [
    (f"/{MARK}/models/a.sql", ABS),
    (f"../{MARK}/models/a.sql", DOTS),
    (f"{TOP}/../{MARK}.sql", DOTS),
    (f"{TOP}/models/../../{MARK}.sql", DOTS),
    (f"{TOP}/models/./{MARK}.sql", DOTS),
    (f"{TOP}/models//{MARK}.sql", DOTS),
    (f"{TOP}/models\\..\\..\\{MARK}.sql", SEP),     # Windows 分隔符
    (f"{TOP}/models/C:{MARK}.sql", SEP),            # 磁碟代號 / 替代資料流
    (f"{TOP}/models/{MARK}\n.sql", CTRL),           # 控制字元(日誌注入)
    (f"{TOP}/models/{MARK}\x1b[31m.sql", CTRL),
    (f"{TOP}/docs/../../{MARK}.sql", DOTS),         # 指定目錄外也要檢查名稱
], ids=["abs", "dotdot-top", "dotdot-after-top", "dotdot-deep", "dot", "empty-seg",
        "backslash", "colon", "newline", "escape", "dotdot-outside"])
def test_suspicious_names_fail_whole_archive(name, reason):
    _fails(extract_archive(_gz(_project([(name, b"x")])), DIRS), reason)


@pytest.mark.parametrize("entries, reason", [
    ([(f"{TOP}/models/a.sql", b"1"), (f"other/{MARK}.sql", b"2")], "單一頂層目錄"),
    ([(f"{MARK}.sql", b"1")], "頂層不是目錄"),
    ([(f"{TOP}/models/a.sql", b"1"), (f"{TOP}/models/a.sql", b"2")], "重複"),
    ([(f"{TOP}/models/a.sql", b"1"), (f"{TOP}/models/A.SQL", b"2")], "重複"),
    ([(f"{TOP}/models/café.sql", b"1"), (f"{TOP}/models/café.sql", b"2")], "重複"),
    ([(f"{TOP}/docs/x.md", b"1"), (f"{TOP}/docs/x.md", b"2")], "重複"),   # 目錄外也算
], ids=["two-tops", "file-at-top", "dup", "dup-case", "dup-nfc-nfd", "dup-outside"])
def test_structure_violations_fail_whole_archive(entries, reason):
    _fails(extract_archive(_gz(entries), DIRS), reason)


def test_duplicate_can_not_smuggle_content_past_review():
    """tar 允許同名項目、後者覆蓋前者:第一個看起來無害、第二個才是真內容。"""
    entries = [(f"{TOP}/",), (f"{TOP}/macros/m.sql", b"-- harmless"),
               (f"{TOP}/macros/m.sql", b"{% macro m() %}DROP TABLE x{% endmacro %}")]
    _fails(extract_archive(_gz(entries), DIRS), "重複")


# ------------------------------------------------------------ 格式與內容
@pytest.mark.parametrize("data, reason", [
    (b"", "gzip"),
    (b"PK\x03\x04" + b"\0" * 100, "gzip"),                    # zip
    (_tar(_project()), "gzip"),                               # 沒壓縮的 tar
    (b"\x1f\x8b" + bytes(range(200)), UNPARSABLE),            # gzip 標頭 + 垃圾
    (gzip.compress(b"not a tar at all" * 100), UNPARSABLE),   # gzip 但內容不是 tar
], ids=["empty", "zip", "plain-tar", "gzip-garbage", "gzip-not-tar"])
def test_not_a_gzipped_tar_fails(data, reason):
    _fails(extract_archive(data, DIRS), reason)


def test_truncated_archive_fails():
    good = _gz(_project())
    _fails(extract_archive(good[: len(good) // 2], DIRS))


def test_declared_size_larger_than_data_fails():
    """標頭宣告的大小比實際資料多(截斷的 tar):不可把殘缺內容當成完整檔案。"""
    raw = _tar([(f"{TOP}/",), (f"{TOP}/models/a.sql", b"x" * 5000)])
    cut = raw[: raw.index(b"x" * 5000) + 100]           # 資料只留 100 位元組
    _fails(extract_archive(gzip.compress(cut), DIRS))


def test_empty_tar_fails():
    _fails(extract_archive(gzip.compress(_tar([])), DIRS), "空的")


def test_non_utf8_wanted_file_fails():
    _fails(extract_archive(_gz(_project([(f"{TOP}/macros/{MARK}.sql", b"\xff\xfe\x00bad")])),
                           DIRS), "UTF-8")


def test_non_utf8_discarded_file_does_not_matter():
    r = extract_archive(_gz(_project([(f"{TOP}/models/bin.png", b"\x89PNG\xff\xfe")])), DIRS)
    assert r.ok, r.error


def test_non_bytes_input_fails():
    _fails(extract_archive("not bytes", DIRS), "位元組")       # type: ignore[arg-type]


@pytest.mark.parametrize("prefixes, reason", [
    ((), "至少一個"), (["models"], "至少一個"), ((None,), "字串"),
    (("../x",), DOTS), (("/abs",), ABS), (("a\\b",), SEP), (("a/../b",), DOTS),
    (("",), "路徑"), (("mo\ndels",), CTRL),
], ids=["empty", "list", "none", "dotdot", "abs", "backslash", "dotdot-mid", "blank", "ctrl"])
def test_bad_prefixes_fail(prefixes, reason):
    """目錄日後來自專案的 dbt_project.yml(不可信):可疑的一律拒絕,不「幫忙修正」。"""
    _fails(extract_archive(_gz(_project()), prefixes), reason)


def test_prefix_trailing_slash_is_accepted():
    r = extract_archive(_gz(_project()), ("models/", "macros"))
    assert r.ok and "models/a.sql" in r.files


# ------------------------------------------------------------------ 上限
def test_limits_match_issue_15():
    """#15 條件 3 的數值(壓縮檔 10 MB、合計 50 MB、2,000 檔),改動要有意識。"""
    assert archive.MAX_COMPRESSED_BYTES == 10 * 1024 * 1024
    assert archive.MAX_TOTAL_BYTES == 50 * 1024 * 1024
    assert archive.MAX_FILES == 2_000
    assert archive.MAX_FILE_BYTES <= archive.MAX_TOTAL_BYTES <= archive.MAX_TAR_BYTES
    assert archive.ALLOWED_SUFFIXES == (".sql", ".yml")


def test_compressed_size_limit(monkeypatch):
    data = _gz(_project())
    monkeypatch.setattr(archive, "MAX_COMPRESSED_BYTES", len(data) - 1)
    _fails(extract_archive(data, DIRS), "壓縮檔超過")
    monkeypatch.setattr(archive, "MAX_COMPRESSED_BYTES", len(data))
    assert extract_archive(data, DIRS).ok


def test_per_file_limit_boundary(monkeypatch):
    monkeypatch.setattr(archive, "MAX_FILE_BYTES", 10)
    ok = _gz([(f"{TOP}/",), (f"{TOP}/models/a.sql", b"x" * 10)])
    assert extract_archive(ok, DIRS).ok
    _fails(extract_archive(_gz([(f"{TOP}/",), (f"{TOP}/models/a.sql", b"x" * 11)]), DIRS),
           "單一檔案")


def test_per_file_limit_ignores_discarded_files(monkeypatch):
    """不收的大檔(例如 seeds 的 CSV)不算在逐檔上限裡——只丟棄,不讓整包失敗。"""
    monkeypatch.setattr(archive, "MAX_FILE_BYTES", 10)
    r = extract_archive(_gz([(f"{TOP}/",), (f"{TOP}/models/a.sql", b"x"),
                             (f"{TOP}/models/big.csv", b"x" * 100)]), DIRS)
    assert r.ok, r.error


def test_file_count_limit(monkeypatch):
    monkeypatch.setattr(archive, "MAX_FILES", 3)
    entries = [(f"{TOP}/",)] + [(f"{TOP}/models/{i}.sql", b"x") for i in range(3)]
    assert extract_archive(_gz(entries), DIRS).ok
    _fails(extract_archive(_gz(entries + [(f"{TOP}/models/9.sql", b"x")]), DIRS), "檔案數")


def test_total_size_limit(monkeypatch):
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 25)
    entries = [(f"{TOP}/",)] + [(f"{TOP}/models/{i}.sql", b"x" * 10) for i in range(2)]
    assert extract_archive(_gz(entries), DIRS).ok
    _fails(extract_archive(_gz(entries + [(f"{TOP}/models/9.sql", b"x" * 10)]), DIRS), "合計")


def test_member_count_limit_counts_discarded_members(monkeypatch):
    """成千上萬個不收的小檔也要擋:它們一樣要逐一讀標頭。"""
    monkeypatch.setattr(archive, "MAX_MEMBERS", 50)
    entries = [(f"{TOP}/",)] + [(f"{TOP}/docs/{i}.md", b"") for i in range(60)]
    _fails(extract_archive(_gz(entries), DIRS), "成員超過")


def test_decompressed_size_limit_catches_bomb(monkeypatch):
    """1 MB 的零壓縮後只剩約 1 KB:解壓上限要在解到一半時就擋下。"""
    monkeypatch.setattr(archive, "MAX_TAR_BYTES", 256 * 1024)
    bomb = _gz([(f"{TOP}/",), (f"{TOP}/docs/zeros.bin", b"\0" * (1024 * 1024))])
    assert len(bomb) < 10_000
    _fails(extract_archive(bomb, DIRS), "炸彈")


class _Zeros(io.RawIOBase):
    """不佔記憶體地產生大量的零,用來做出真實大小的壓縮炸彈。"""

    def __init__(self, n):
        self.left = n

    def readable(self):
        return True

    def readinto(self, b):
        n = min(len(b), self.left)
        b[:n] = b"\0" * n
        self.left -= n
        return n


def test_real_size_bomb_is_stopped_without_blowing_memory():
    """用實際上限:壓縮後不到 1 MB、解開是上限的 4 倍。必須失敗,且記憶體用量受上限約束。"""
    size = archive.MAX_TAR_BYTES * 4
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tar:
        tar.addfile(_info(f"{TOP}/docs/zeros.bin", size=size),
                    io.BufferedReader(_Zeros(size), 1024 * 1024))
    bomb = buf.getvalue()
    assert len(bomb) < archive.MAX_COMPRESSED_BYTES
    tracemalloc.start()
    try:
        r = extract_archive(bomb, DIRS)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    _fails(r, "炸彈")
    assert peak < archive.MAX_TAR_BYTES * 2, peak


# ------------------------------------------------------------ 暫存目錄與過濾器
@pytest.fixture
def recorded_tmp(monkeypatch):
    """記下模組建立的暫存目錄,事後檢查有沒有被刪掉。"""
    made = []
    real = archive.tempfile.mkdtemp

    def _mkdtemp(*a, **k):
        made.append(real(*a, **k))
        return made[-1]

    monkeypatch.setattr(archive.tempfile, "mkdtemp", _mkdtemp)
    return made


def test_temp_dir_removed_after_success(recorded_tmp):
    assert extract_archive(_gz(_project()), DIRS).ok
    assert len(recorded_tmp) == 1 and not os.path.exists(recorded_tmp[0])


def test_temp_dir_removed_after_failure_during_read(recorded_tmp):
    """在解出之後才失敗(非 UTF-8):暫存目錄已經建了,也一定要刪掉。"""
    _fails(extract_archive(_gz(_project([(f"{TOP}/macros/b.sql", b"\xff")])), DIRS), "UTF-8")
    assert len(recorded_tmp) == 1 and not os.path.exists(recorded_tmp[0])


def test_failure_before_extraction_creates_no_temp_dir(recorded_tmp):
    """檢查階段就失敗的,不該建暫存目錄——任何內容都沒有落地。"""
    _fails(extract_archive(_gz(_project([(f"{TOP}/macros/x.sql", tarfile.SYMTYPE, "/")])),
                           DIRS), LINK)
    assert recorded_tmp == []


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 沒有 POSIX 權限位元")
def test_temp_dir_is_private(monkeypatch):
    seen = []
    real = archive.tarfile.TarFile.extractall

    def _spy(self, path, *a, **k):
        seen.append(os.stat(path).st_mode & 0o777)
        return real(self, path, *a, **k)

    monkeypatch.setattr(archive.tarfile.TarFile, "extractall", _spy)
    assert extract_archive(_gz(_project()), DIRS).ok
    assert seen == [0o700]


def test_cleanup_failure_is_a_failure(monkeypatch, recorded_tmp):
    """刪不掉暫存目錄 = 程式碼留在審查機上:不能回報成功。"""
    def _broken(path, *a, **k):
        raise OSError("disk says no")

    data = _gz(_project())
    monkeypatch.setattr(archive.shutil, "rmtree", _broken)   # 也就是全域的 shutil.rmtree
    try:
        _fails(extract_archive(data, DIRS), not_in_error=("disk says no",))
    finally:
        monkeypatch.undo()
        shutil.rmtree(recorded_tmp[0])


def test_uses_tarfile_data_filter(monkeypatch):
    """#15 條件 5:一定要帶 filter="data",不能只靠自己寫的檢查。"""
    seen = []
    real = archive.tarfile.TarFile.extractall

    def _spy(self, *a, **k):
        seen.append(k.get("filter"))
        return real(self, *a, **k)

    monkeypatch.setattr(archive.tarfile.TarFile, "extractall", _spy)
    assert extract_archive(_gz(_project()), DIRS).ok
    assert seen == ["data"]


def test_refuses_without_data_filter(monkeypatch):
    """Python 太舊、沒有 data filter:不解壓,不退回無過濾的解法。"""
    monkeypatch.delattr(archive.tarfile, "data_filter")
    _fails(extract_archive(_gz(_project()), DIRS), "data filter")


def test_data_filter_is_a_real_second_line(monkeypatch):
    """自己的類型檢查若被繞過(這裡直接放寬),filter="data" 仍要擋下往外跳的連結。"""
    monkeypatch.setattr(archive, "_REGULAR_TYPES",
                        archive._REGULAR_TYPES + (tarfile.SYMTYPE,))
    err = _fails(extract_archive(_gz(_project([(f"{TOP}/macros/x.sql", tarfile.SYMTYPE,
                                                "../../../../etc/passwd")])), DIRS))
    assert LINK not in err                   # 確實是第二層擋下的,不是第一層


def test_post_extract_check_is_a_real_third_line(monkeypatch):
    """前兩層都放過一個指向暫存目錄內的連結時(filter 允許目錄內的連結),解出後
    的核對仍要發現「這不是一般檔案」。"""
    monkeypatch.setattr(archive, "_REGULAR_TYPES",
                        archive._REGULAR_TYPES + (tarfile.SYMTYPE, tarfile.LNKTYPE))
    err = _fails(extract_archive(_gz(_project([(f"{TOP}/macros/x.sql", tarfile.LNKTYPE,
                                                f"{TOP}/models/a.sql")])), DIRS))
    assert "解出的不是一般檔案,或大小與宣告不符" in err


# ------------------------------------------------------------------ 錯誤訊息
def test_third_party_error_text_is_not_echoed(monkeypatch):
    """tarfile / gzip 的例外訊息可能帶成員名稱:只留例外類型與固定文字。"""
    def _boom(*a, **k):
        raise tarfile.ReadError(f"bad member {MARK}")

    data = _gz(_project())                          # 先做好:之後 tarfile.open 會被換掉
    monkeypatch.setattr(archive.tarfile, "open", _boom)
    err = _fails(extract_archive(data, DIRS), UNPARSABLE)
    assert err.startswith("ReadError:")


def test_error_is_short_and_printable(monkeypatch):
    monkeypatch.setattr(archive, "MAX_ERROR_CHARS", 20)
    err = _fails(extract_archive(b"", DIRS))
    assert len(err) <= 20 and err.isprintable()


# ------------------------------------------------------------------ 隨機輸入
@pytest.mark.parametrize("seed", range(6))
def test_fuzz_never_raises_and_never_returns_partial(seed):
    """隨機竄改一包正常的壓縮檔:不得丟例外;失敗時不帶任何檔案;成功時每個檔案
    都必須在指定目錄內、副檔名在白名單內(竄改可能改到內容或檔名,但不能越界)。"""
    rng = random.Random(seed)
    good = _tar(_project([(f"{TOP}/macros/sub/x.sql", b"select 3\n")]))
    for _ in range(40):
        raw = bytearray(good)
        for _ in range(rng.randint(1, 8)):
            raw[rng.randrange(len(raw))] = rng.randrange(256)
        for data in (gzip.compress(bytes(raw)), bytes(raw), bytes(raw[: rng.randrange(len(raw))])):
            r = extract_archive(data, DIRS)
            assert isinstance(r, ArchiveResult)
            if r.ok:
                for k in r.files:
                    assert k.split("/")[0] in DIRS and k.lower().endswith((".sql", ".yml")), k
                    assert ".." not in k.split("/")
            else:
                assert r.files == {} and r.error


# ------------------------------------------------------------------ 模組邊界
def test_module_imports_are_allowlisted():
    """處理不可信內容的模組不得悄悄獲得網路、子行程等能力;新增 import 要刻意更新這裡。"""
    tree = ast.parse((PKG_ROOT / "orchestrator" / "archive.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or "").split(".")[0])
    assert imported == {"gzip", "io", "os", "shutil", "stat", "tarfile", "tempfile",
                        "unicodedata", "dataclasses"}


def test_module_never_execs():
    tree = ast.parse((PKG_ROOT / "orchestrator" / "archive.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"eval", "exec", "compile", "__import__", "input", "breakpoint"}
