"""archive — 安全解開 GitLab 打包下載的 tar.gz,取回 dbt 專案的 .sql / .yml(#15)。

GitLab `/repository/archive` 回的是待審 MR 那個 commit 的程式碼,**是不可信內容**:
MR 的作者可以在裡面放任何東西——壓縮炸彈、`../` 路徑、符號連結、成千上萬個小檔。
這個模組只做一件事:把一包 tar.gz 變成 {相對路徑: 文字內容},任何一步不對就整包失敗。

流程(任何一步失敗 = 整包失敗,ok=False;**絕不回傳「部分成功」**):

  1. 壓縮檔大小上限、必須是 gzip(下載端也會擋大小,這裡再擋一次)
  2. 邊解壓邊計數,解出的 tar 一超過上限就中止。
     tar.gz 的檔案清單藏在壓縮資料裡,不解壓就讀不到——所以「先看宣告大小、不先
     全部解壓」必須搭配這一步,否則壓縮炸彈會在「讀清單」時就發作
  3. 讀 tar 清單(只讀標頭),逐一檢查每個成員的名稱、路徑、類型、宣告大小
  4. 只挑出指定目錄下、副檔名在白名單內的檔案,檢查檔數、合計大小、逐檔大小
  5. 用 tarfile 內建的 filter="data" 解到權限 700 的暫存目錄(與上面自己寫的檢查
     互為備援——#15 的條件:不要只靠自己寫的檢查)
  6. 讀回內容(嚴格 UTF-8)並核對大小,隨即刪除暫存目錄

為什麼指定目錄內的符號連結要讓**整包**失敗,而不是略過:略過會讓某個 macro 憑空
消失,反查就會回報「沒有影響」——這正是 #15 要求避免的「抓不到就當作沒有」。
指定目錄外的成員一律丟棄、不解壓,所以只做名稱檢查(路徑穿越、控制字元等)。

錯誤訊息可能被貼進 MR 留言或寫進日誌:不含成員名稱、不含檔案內容,長度有上限。

本模組不做網路、不執行子行程;唯一的檔案寫入是解到自己建立的暫存目錄。
"""
import gzip
import io
import os
import shutil
import stat
import tarfile
import tempfile
import unicodedata
from dataclasses import dataclass, field

# 上限(#15 條件 3):超過任何一項就整包失敗、交人工
MAX_COMPRESSED_BYTES = 10 * 1024 * 1024   # 壓縮檔
MAX_TAR_BYTES = 64 * 1024 * 1024          # 解壓後的整個 tar(含標頭與不收的檔案)
MAX_MEMBERS = 20_000                      # tar 內成員總數(含目錄與不收的檔案)
MAX_FILES = 2_000                         # 收下的檔案數(與 dbt_render.MAX_MACRO_FILES 相同)
MAX_TOTAL_BYTES = 50 * 1024 * 1024        # 收下的檔案合計
MAX_FILE_BYTES = 1_000_000                # 單檔(與 dbt_render.MAX_SOURCE_CHARS 同級)
MAX_ERROR_CHARS = 300

# 只收需要的副檔名(#15 條件 6),其餘一律丟棄
ALLOWED_SUFFIXES = (".sql", ".yml")

_CHUNK = 64 * 1024
_REGULAR_TYPES = (tarfile.REGTYPE, tarfile.AREGTYPE)
_WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul",
                               *(f"com{i}" for i in range(10)),
                               *(f"lpt{i}" for i in range(10))})


class ArchiveError(Exception):
    """本模組自己判定的失敗;訊息是固定文字,不含壓縮檔內容。"""


@dataclass(frozen=True)
class ArchiveResult:
    ok: bool
    # 去掉頂層目錄後的相對路徑(以 / 分隔)→ 文字內容。ok=False 時一定是空的。
    files: dict[str, str] = field(default_factory=dict)
    error: str | None = None


def extract_archive(data: bytes, prefixes: tuple[str, ...]) -> ArchiveResult:
    """解開 GitLab 打包下載的 tar.gz,只取 prefixes 目錄下的 .sql / .yml。

    prefixes  要收的目錄(相對專案根,例如 ("models", "macros"));目錄外的成員一律丟棄
    任何失敗都收斂成 ok=False,不丟例外。
    """
    try:
        if not hasattr(tarfile, "data_filter"):
            # filter="data" 是 #15 的明確要求;沒有它就不解壓,不退回無過濾的解法
            raise ArchiveError("此 Python 版本的 tarfile 沒有 data filter,拒絕解壓")
        wanted_dirs = _check_prefixes(prefixes)
        with tarfile.open(fileobj=_gunzip_capped(data), mode="r:") as tar:
            wanted = _select_members(tar, wanted_dirs)
            files = _extract_and_read(tar, wanted)
        return ArchiveResult(ok=True, files=files)
    except Exception as e:                  # noqa: BLE001 — 一律收斂成失敗結果
        return ArchiveResult(ok=False, error=_safe_error(e))


# ------------------------------------------------------------------ 1–2. 解壓
def _gunzip_capped(data: bytes) -> io.BytesIO:
    """解壓 gzip,解出的位元組一超過 MAX_TAR_BYTES 就中止(擋壓縮炸彈)。

    gzip.read(n) 每次最多解出 n 位元組,記憶體用量受 _CHUNK 控制,不會一次解爆。
    直接回傳緩衝區(不用 getvalue() 另外複製一份),記憶體上限約等於 MAX_TAR_BYTES。
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ArchiveError("壓縮檔內容必須是位元組")
    if len(data) > MAX_COMPRESSED_BYTES:
        raise ArchiveError(f"壓縮檔超過 {MAX_COMPRESSED_BYTES} 位元組上限")
    if bytes(data[:2]) != b"\x1f\x8b":
        raise ArchiveError("不是 gzip 格式的壓縮檔")
    out = io.BytesIO()
    total = 0
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        while True:
            chunk = gz.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_TAR_BYTES:
                raise ArchiveError(f"解壓後超過 {MAX_TAR_BYTES} 位元組上限(疑似壓縮炸彈)")
            out.write(chunk)
    out.seek(0)
    return out


# ------------------------------------------------------------- 3–4. 檢查與挑選
def _check_prefixes(prefixes) -> tuple[str, ...]:
    """呼叫端給的目錄也要檢查:它們之後會拿來比對成員路徑,而且日後會來自專案的
    dbt_project.yml(model-paths / macro-paths)——同樣是 MR 可以改的不可信內容。
    只容許結尾的 /;開頭的 / 是絕對路徑,直接拒絕,不「幫忙修正」。"""
    if not isinstance(prefixes, tuple) or not prefixes:
        raise ArchiveError("必須指定至少一個要收的目錄")
    checked = []
    for p in prefixes:
        if not isinstance(p, str):
            raise ArchiveError("目錄名稱必須是字串")
        parts = _split_relative(p.rstrip("/"))
        checked.append("/".join(parts))
    return tuple(checked)


def _split_relative(name: str) -> list[str]:
    """把 / 分隔的相對路徑拆成段落,任何可疑的寫法都直接失敗。"""
    if not name or "\x00" in name or not name.isprintable():
        raise ArchiveError("路徑是空的,或含控制字元")
    if "\\" in name or ":" in name:
        # 反斜線在 Windows 是分隔符;冒號是磁碟代號 / 替代資料流。GitLab 在 Linux
        # 上打包不會產生這兩種,出現就當成惡意或損毀
        raise ArchiveError("路徑含反斜線或冒號")
    if name.startswith("/"):
        raise ArchiveError("路徑是絕對路徑")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ArchiveError("路徑含 ..、. 或空段落")
    for p in parts:
        # Windows 會默默去掉結尾的點與空白(`a.sql.` 變成 `a.sql`),可用來繞過重複檢查
        # 覆蓋另一個檔案;CON、NUL 等是裝置名稱。正式環境是 Linux,但開發與審查者的
        # 電腦可能是 Windows,而正常的 dbt 專案不會有這種名稱,一律拒絕
        if p.endswith((".", " ")) or p.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise ArchiveError("路徑含 Windows 保留名稱,或段落結尾是點或空白")
    return parts


def _under(rel: str, dirs: tuple[str, ...]) -> bool:
    return any(rel == d or rel.startswith(d + "/") for d in dirs)


def _select_members(tar: tarfile.TarFile, dirs: tuple[str, ...]) -> list[tuple[tarfile.TarInfo, str]]:
    """逐一檢查成員(只讀標頭),回傳要收的 (成員, 去掉頂層目錄的相對路徑)。

    GitLab 的打包一律把所有內容放在單一頂層目錄(<專案>-<sha>-<sha>/)底下;
    不是這個形狀就不是我們預期的東西,整包失敗。
    """
    top = None
    seen: set[str] = set()
    wanted: list[tuple[tarfile.TarInfo, str]] = []
    count = n_files = total = 0
    for m in tar:
        count += 1
        if count > MAX_MEMBERS:
            raise ArchiveError(f"壓縮檔成員超過 {MAX_MEMBERS} 個上限")
        parts = _split_relative(m.name.rstrip("/") if m.isdir() else m.name)
        if top is None:
            top = parts[0]
        elif parts[0] != top:
            raise ArchiveError("壓縮檔不是單一頂層目錄的結構")
        if len(parts) == 1:
            if not m.isdir():
                raise ArchiveError("壓縮檔頂層不是目錄")
            continue
        rel = "/".join(parts[1:])
        # 重複的成員:tar 允許同名項目,後面的會蓋掉前面的,可用來夾帶內容。
        # 大小寫不同、Unicode 正規化不同也算重複(在不分大小寫的檔案系統上會互相覆蓋)
        key = unicodedata.normalize("NFC", rel).casefold()
        if key in seen:
            raise ArchiveError("壓縮檔含重複的成員")
        seen.add(key)
        if not _under(rel, dirs):
            continue                        # 指定目錄外:丟棄,不解壓
        if m.isdir():
            continue
        if m.type not in _REGULAR_TYPES:
            # 符號連結、硬連結、裝置檔、FIFO、稀疏檔……一律整包失敗(理由見模組說明)
            raise ArchiveError("指定目錄內含符號連結、硬連結或特殊檔案")
        if not rel.lower().endswith(ALLOWED_SUFFIXES):
            continue                        # 不需要的副檔名:丟棄
        if m.size > MAX_FILE_BYTES:
            raise ArchiveError(f"單一檔案超過 {MAX_FILE_BYTES} 位元組上限")
        n_files += 1
        total += m.size
        if n_files > MAX_FILES:
            raise ArchiveError(f"檔案數超過 {MAX_FILES} 個上限")
        if total > MAX_TOTAL_BYTES:
            raise ArchiveError(f"檔案合計超過 {MAX_TOTAL_BYTES} 位元組上限")
        wanted.append((m, rel))
    if top is None:
        raise ArchiveError("壓縮檔是空的")
    return wanted


# ------------------------------------------------------------- 5–6. 解出與讀回
def _extract_and_read(tar: tarfile.TarFile, wanted) -> dict[str, str]:
    """解到權限 700 的暫存目錄、讀回、刪除。刪除失敗也算失敗(不能留下程式碼)。"""
    tmp = tempfile.mkdtemp(prefix="segcra-archive-")
    try:
        os.chmod(tmp, 0o700)                # mkdtemp 在 POSIX 上本來就是 700;明寫出來
        tar.extractall(path=tmp, members=[m for m, _ in wanted], filter="data")
        root = os.path.realpath(tmp)
        files: dict[str, str] = {}
        for m, rel in wanted:
            path = os.path.join(tmp, *m.name.split("/"))
            if not os.path.realpath(path).startswith(root + os.sep):
                raise ArchiveError("解出的檔案落在暫存目錄之外")
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_size != m.size:
                raise ArchiveError("解出的不是一般檔案,或大小與宣告不符")
            with open(path, "rb") as fh:
                raw = fh.read(MAX_FILE_BYTES + 1)
            if len(raw) != m.size:
                # 正常情況下不會發生(上一步剛核對過大小);防的是解出到讀回之間被換掉
                raise ArchiveError("讀回的內容長度與宣告不符")
            try:
                files[rel] = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ArchiveError("檔案不是 UTF-8 文字") from None
        return files
    finally:
        shutil.rmtree(tmp)                  # 失敗會丟例外 → 外層收斂成 ok=False


def _safe_error(e: BaseException) -> str:
    """只留我們自己的固定文字;第三方例外(tarfile、gzip)的訊息可能帶成員名稱,不回顯。"""
    if isinstance(e, ArchiveError):
        text = f"ArchiveError: {e}"
    elif isinstance(e, MemoryError):
        text = "MemoryError: 解壓時記憶體不足,已中止"
    else:
        text = f"{type(e).__name__}: 壓縮檔無法解析或無法解出"
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:MAX_ERROR_CHARS]
