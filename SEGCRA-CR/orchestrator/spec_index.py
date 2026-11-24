"""spec_index — 規格依 project 編號對應程式(dbt 開啟時審查找規格的唯一方法)。

部署端的 project 編號與規格是一對一:

  * 規格:repo 根目錄 `specs/` 底下的 `.md`,檔名不限。編號取自「基本資料」表格
    `序號 / 程式名稱` 那一列的值,`/` 前面那一段(例如 `DEMO02 / RULE_X1` → `DEMO02`)。
  * 程式:dbt 專案 model 目錄(`dbt_project.yml` 的 model-paths)底下的**第一層資料夾**
    就是 project 編號;資料夾底下可以再分子資料夾(`models/DEMO02/staging/x.sql`
    也屬於 DEMO02)。

MR 裡寫的 `R-` 編號、model 的檔名都**不**決定用哪份規格:那等於讓送審者自己挑規格,
寫一個寬鬆規格的編號,執行驗證就會拿錯的規格驗。

對不到、或對到不只一份時一律不挑(回 None 加上原因),由呼叫端記成「無規格可驗」交人工:

  * 檔案不在任何 project 資料夾(不在 dbt 專案裡、不在 model 目錄裡、直接放在 model 目錄底下)
  * 編號沒有對應的規格;同一個編號有兩份以上規格
  * MR 改到的程式分屬不同 project
  * 檔案從一個 project 資料夾搬到另一個

規格清單取不到(唯讀 token 沒設、沒有釘住 commit、下載或解開失敗)是環境的問題,
記在 `SpecIndex.error`(gitlab.UNAVAILABLE_TEXT 的代碼),不能說成「沒有規格」。

輸入(規格內容、MR 的路徑)一律是不可信的:編號只接受英數字、底線與 `-`,
原因說明裡的路徑與編號都先過白名單,超過長度就截斷。
"""
import contextvars
import re
import unicodedata
from dataclasses import dataclass, field

SPECS_DIR = "specs"
DEFAULT_MODEL_PATHS = ("models",)
DEFAULT_MACRO_PATHS = ("macros",)
# 程式檔:model 目錄底下的 SQL 與 Python model。其他(yml、csv…)不決定 project
_PROGRAM_SUFFIXES = (".sql", ".py")
_PROJECT_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SAFE_PATH = re.compile(r"[A-Za-z0-9_./-]{1,300}")
_ROW_KEY = "序號/程式名稱"
_FENCE = re.compile(r"^\s*(```|~~~)")
_BOM = "﻿"
MAX_SPECS = 2_000
MAX_NOTE_ITEMS = 5


@dataclass(frozen=True)
class SpecIndex:
    """一次審查用的「編號 → 規格」對照表。error 有值時其餘欄位都是空的。"""

    specs: dict = field(default_factory=dict)        # 代號(規格檔名去掉 .md)→ 內容
    by_project: dict = field(default_factory=dict)   # 編號 → 代號(排序過的 tuple)
    prefix: str = ""                                 # dbt 專案在 repo 裡的目錄
    model_paths: tuple = DEFAULT_MODEL_PATHS
    macro_paths: tuple = DEFAULT_MACRO_PATHS
    error: str | None = None


@dataclass(frozen=True)
class SpecChoice:
    """查找結果。code 與 text 都有值 = 對到;note 是對不到時給人看的原因。"""

    code: str | None = None
    text: str | None = None
    unavailable: str | None = None
    note: str | None = None


_CURRENT: contextvars.ContextVar = contextvars.ContextVar("segcra_spec_index", default=None)


def use_index(index: "SpecIndex | None") -> None:
    """設定這次審查用的對照表(None = 解除)。跟 gitlab.pin_review_ref 一樣跟著 context 走:
    同時進行的兩份審查各自在自己的工作裡設定,互不影響;背景執行緒會帶上 context。"""
    _CURRENT.set(index)


def current_index() -> "SpecIndex | None":
    return _CURRENT.get()


# ------------------------------------------------------------------ 規格的編號
def project_code(text) -> str | None:
    """規格「基本資料」表格 `序號 / 程式名稱` 那一列的編號;沒有、不合法或不只一個值時回 None。

    全形字(`／`、全形英數)先轉成半形再比。程式碼區塊裡的內容不算。"""
    if not isinstance(text, str):
        return None
    found = set()
    in_fence = False
    for raw in text.lstrip(_BOM).splitlines():
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = unicodedata.normalize("NFKC", raw).strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2 or re.sub(r"\s+", "", cells[0]) != _ROW_KEY:
            continue
        value = cells[1].split("/", 1)[0].strip()
        found.add(value)
    if len(found) != 1:
        return None
    value = found.pop()
    return value if _PROJECT_CODE.fullmatch(value) else None


def build_index(files: dict, *, prefix: str = "", model_paths=DEFAULT_MODEL_PATHS,
                macro_paths=DEFAULT_MACRO_PATHS) -> SpecIndex:
    """files 是 {repo 相對路徑: 內容}。只收 `specs/` 底下直接一層的 `.md`(審查讀規格的位置);
    內容是空字串的(這個 MR 刪掉的)不算。"""
    specs: dict = {}
    for path, text in sorted(files.items()):
        if not (isinstance(path, str) and isinstance(text, str) and text):
            continue
        if not (path.startswith(SPECS_DIR + "/") and path.endswith(".md")
                and path.count("/") == 1):
            continue
        code = path[len(SPECS_DIR) + 1:-len(".md")]
        if code and len(specs) < MAX_SPECS:
            specs[code] = text
    by_project: dict = {}
    for code, text in specs.items():
        project = project_code(text)
        if project is not None:
            by_project.setdefault(project, []).append(code)
    return SpecIndex(specs=specs, by_project={k: tuple(sorted(v)) for k, v in by_project.items()},
                     prefix=prefix, model_paths=_dirs(model_paths) or DEFAULT_MODEL_PATHS,
                     macro_paths=_dirs(macro_paths))


def _dirs(paths) -> tuple:
    if isinstance(paths, str):
        paths = (paths,)
    out = []
    for p in paths or ():
        if isinstance(p, str):
            p = p.strip("/")
            if p in ("", "."):
                p = ""
            out.append(p)
    return tuple(dict.fromkeys(out))


# ------------------------------------------------------------------ 程式的編號
def _under(rel: str, directory: str) -> bool:
    return directory == "" or rel.startswith(directory + "/")


def project_of(path, index: SpecIndex) -> tuple[str | None, str | None]:
    """程式檔(repo 相對路徑)屬於哪個 project。回傳 (編號, None) 或 (None, 不屬於的原因)。"""
    from .dbt_impact import normalize_path   # 跟 macro 反查同一套路徑白名單
    path = normalize_path(path)
    if path is None:
        return None, "路徑不合法"
    rel = path
    if index.prefix:
        if not path.startswith(index.prefix + "/"):
            return None, "不在 dbt 專案的目錄裡"
        rel = path[len(index.prefix) + 1:]
    for directory in sorted(index.model_paths, key=len, reverse=True):
        if not _under(rel, directory):
            continue
        rest = rel[len(directory) + 1:] if directory else rel
        parts = rest.split("/")
        if len(parts) < 2:
            return None, "直接放在 model 目錄底下,不在任何 project 資料夾裡"
        if not _PROJECT_CODE.fullmatch(parts[0]):
            return None, "所在的資料夾名稱不是 project 編號的格式"
        return parts[0], None
    return None, "不在 dbt 的 model 目錄裡"


def _is_program(path, index: SpecIndex) -> bool:
    """會決定 project 的檔案:.sql / .py,而且不是 macro。"""
    if not (isinstance(path, str) and path.lower().endswith(_PROGRAM_SUFFIXES)):
        return False
    rel = path[len(index.prefix) + 1:] if index.prefix and path.startswith(index.prefix + "/") \
        else path
    return not any(d and _under(rel, d) for d in index.macro_paths)


def _shown(path) -> str:
    """寫進說明的路徑:先過路徑白名單,再只留英數字與 `_./-`(說明會貼回 MR)。"""
    from .dbt_impact import normalize_path
    if isinstance(path, str) and normalize_path(path) is not None and _SAFE_PATH.fullmatch(path):
        return path
    return "(不合法或含特殊字元的路徑)"


def _listed(items) -> str:
    items = list(items)
    text = "、".join(items[:MAX_NOTE_ITEMS])
    return text + (f" 等 {len(items)} 個" if len(items) > MAX_NOTE_ITEMS else "")


def choose(index: SpecIndex, files) -> SpecChoice:
    """MR(或單一 model)改到的程式對到哪份規格。files 是 MR 的變更清單([{path, old_path}])。

    沒有任何程式檔(例如只改 macro):回空的 SpecChoice,沒有原因——受影響的 model 另外逐一查。"""
    if index.error:
        return SpecChoice(unavailable=index.error)
    outside, projects, moved = [], set(), []
    for f in files or ():
        if not isinstance(f, dict):
            continue
        path, old = f.get("path"), f.get("old_path")
        if not _is_program(path, index):
            continue
        project, reason = project_of(path, index)
        if project is None:
            outside.append(f"{_shown(path)}({reason})")
            continue
        projects.add(project)
        if isinstance(old, str) and old != path and _is_program(old, index):
            before, _ = project_of(old, index)
            if before != project:
                moved.append(f"{_shown(old)} → {_shown(path)}")
    if outside:
        return SpecChoice(note=f"這個檔案不屬於任何 project:{_listed(outside)}")
    if moved:
        return SpecChoice(note=f"檔案搬到了另一個 project 資料夾:{_listed(moved)}"
                               ",規格的對應改變了,需要人工確認")
    if not projects:
        return SpecChoice()
    if len(projects) > 1:
        return SpecChoice(note=f"改到的程式分屬不同 project({_listed(sorted(projects))}),"
                               "無法決定用哪一份規格")
    project = projects.pop()
    codes = index.by_project.get(project, ())
    if len(codes) == 1:
        return SpecChoice(code=codes[0], text=index.specs[codes[0]])
    if len(codes) > 1:
        return SpecChoice(note=f"project {project} 有 {len(codes)} 份規格"
                               f"({_listed(f'specs/{c}.md' for c in codes)}),不自行擇一")
    near = sorted(p for p in index.by_project if p.lower() == project.lower())
    hint = f";只有大小寫不同的編號 {near[0]} 有規格,未採用" if near else ""
    return SpecChoice(note=f"project {project} 沒有對應的規格(specs/ 裡沒有「序號」是 "
                           f"{project} 的規格{hint})")


# ------------------------------------------------------------------ 讀規格
def load_index(versions: dict, *, prefix: str = "", model_paths=DEFAULT_MODEL_PATHS,
               macro_paths=DEFAULT_MACRO_PATHS, real_mode: bool | None = None, download=None,
               local_dir=None) -> SpecIndex:
    """讀這次審查的規格,建對照表。任何失敗都收斂成 error 有值的 SpecIndex,不丟例外。

    versions     這個 MR 改到的規格 {代號: 內容};空字串 = 這個 MR 刪掉了
    真實模式:用唯讀 token 打包下載審查釘住的那個 commit 的 `specs/`(跟 dbt 專案同一種取法,
                一次請求),本來就是這個 MR 的版本
    mock 模式:讀工具內建的 specs/,再疊上 versions
    real_mode / download / local_dir  測試用
    """
    kw = {"prefix": prefix, "model_paths": model_paths, "macro_paths": macro_paths}
    try:
        from toolbox import gitlab
        if real_mode is None:
            real_mode = bool(gitlab.REAL_MODE)
        if real_mode:
            files, error = _download_specs(gitlab, download)
            if error:
                return SpecIndex(error=error, **kw)
        else:
            files = _local_specs(local_dir)
        for code, text in (versions or {}).items():
            if isinstance(code, str) and isinstance(text, str):
                files[f"{SPECS_DIR}/{code}.md"] = text
        return build_index(files, **kw)
    except Exception:                       # noqa: BLE001 — 讀不到就是取不到,不中斷審查
        return SpecIndex(error="fetch_failed", **kw)


def _download_specs(gitlab, download) -> tuple[dict, str | None]:
    from . import archive
    sha = gitlab.review_ref()
    if not sha:
        return {}, "no_review_ref"
    if download is None:
        download = gitlab.download_archive
    try:
        data = download(sha, SPECS_DIR, max_bytes=archive.MAX_COMPRESSED_BYTES)
    except Exception:                       # noqa: BLE001 — 訊息可能含設定細節,只留代碼
        return {}, ("no_read_token" if not (gitlab.GITLAB_READ_TOKEN
                                            and gitlab.GITLAB_READ_TOKEN != gitlab.GITLAB_TOKEN)
                    else "fetch_failed")
    result = archive.extract_archive(data, (SPECS_DIR,), suffixes=archive.SPEC_SUFFIXES)
    if not result.ok:
        return {}, "fetch_failed"
    return dict(result.files), None


def _local_specs(local_dir) -> dict:
    if local_dir is None:
        from . import spec_exec               # 跟 R 編號那條路讀同一個目錄
        local_dir = spec_exec.SPECS_DIR
    if not local_dir.is_dir():
        return {}
    return {f"{SPECS_DIR}/{p.name}": p.read_text(encoding="utf-8")
            for p in sorted(local_dir.glob("*.md")) if p.is_file()}
