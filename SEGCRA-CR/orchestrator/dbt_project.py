"""dbt_project — 以 MR 的 commit 取回 dbt 專案的 models / macros(#15 第 3 階段)。

流程(任何一步失敗 = ok=False,**絕不當成「專案沒有 macro」**,#15 條件 7):

  1. 以 commit 編號打包下載 dbt 專案所在的目錄——只呼叫一次 GitLab
  2. 從這一包先解出專案根目錄的 dbt_project.yml(讀出 model / macro / seed / snapshot
     的目錄設定)與 packages.yml / dependencies.yml(有沒有用 dbt 套件)
  3. 從**同一包**再解出這些目錄的 .sql / .yml / .yaml,並列出 model / seed / snapshot
     目錄的檔名(不另外用讀檔 API:那預設讀 main,讀到的就不是被審的那一版)
  4. 成功的結果放進記憶體快取(以 commit 編號為鍵,#15 條件 9);失敗不快取,
     下一次審查還能重試

dbt_project.yml 來自待審的 commit,同樣是不可信內容:目錄設定裡有 Jinja 標記、
絕對路徑、..、數量過多,一律失敗,不「幫忙修正」。

快取只放在記憶體(不落地):暫存目錄仍由 archive.extract_archive 每次用完即刪
(#15 條件 7「讀完即刪」與條件 9「同一個 MR 重跑不重抓」兩者都成立)。
"""
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

import yaml

from . import archive

# dbt 的預設目錄(dbt_project.yml 沒寫時)
DEFAULT_MODEL_PATHS = ("models",)
DEFAULT_MACRO_PATHS = ("macros",)
DEFAULT_SEED_PATHS = ("seeds",)
DEFAULT_SNAPSHOT_PATHS = ("snapshots",)
MAX_PATHS = 20                          # 單一設定項最多幾個目錄
CACHE_MAX_ENTRIES = 8
CACHE_MAX_BYTES = 100 * 1024 * 1024     # 快取內所有檔案內容合計(字元數,近似位元組)
MAX_ERROR_CHARS = 300
# extra_ref_names 解析的 snapshot yml 合計上限(字元)。真實專案遠低於此;超過就不在審查
# 行程裡解析(1 MB 的 yml 約需 0.5–2.5 秒),名單視為不完整
MAX_SNAPSHOT_YML_CHARS = 1_000_000
# 專案根目錄要取回的設定檔。packages.yml / dependencies.yml 決定專案有沒有用 dbt 套件:
# 套件的 model / source 不在 repo 裡,有用套件時 ref() / source() 找不到不代表不存在
ROOT_FILES = ("dbt_project.yml", "packages.yml", "dependencies.yml")

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# snapshot 的名稱寫在區塊上({% snapshot 名稱 %}),不是檔名
_SNAPSHOT_TAG = re.compile(r"\{%-?\s*snapshot\b")
_SNAPSHOT_BLOCK = re.compile(r"\{%-?\s*snapshot\s+([^\s%]+)\s*-?%\}")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LIBYAML = getattr(yaml, "__with_libyaml__", False) and hasattr(yaml, "CSafeLoader")


@dataclass(frozen=True)
class DbtProject:
    ok: bool
    # 相對 dbt 專案根目錄的路徑(以 / 分隔)→ 內容;model / macro / seed / snapshot 目錄下的
    # .sql / .yml / .yaml
    files: dict[str, str] = field(default_factory=dict)
    model_paths: tuple[str, ...] = ()
    macro_paths: tuple[str, ...] = ()
    error: str | None = None
    # 專案根目錄的設定檔(ROOT_FILES 中存在的那些)→ 內容。ok=True 時一定含 dbt_project.yml;
    # 其餘不在這裡就是**確實不存在**(已從同一包檢查過),呼叫端可據此傳
    # dbt_relations.project_info_from_files(root_files_checked=True)
    root_files: dict[str, str] = field(default_factory=dict)
    seed_paths: tuple[str, ...] = ()
    snapshot_paths: tuple[str, ...] = ()
    # model / seed / snapshot 目錄下所有檔案的路徑(任何副檔名、只有名稱),已排序
    names: tuple[str, ...] = ()


class DbtProjectError(Exception):
    """本模組判定的失敗;訊息是固定文字,不含下載內容。"""


_cache: "OrderedDict[tuple[str, str], DbtProject]" = OrderedDict()
_cache_lock = threading.Lock()          # webhook server 以執行緒並行審查


def load_dbt_project(sha: str, project_dir: str = "", *, download=None) -> DbtProject:
    """取回某個 commit 的 dbt 專案。任何失敗都收斂成 ok=False,不丟例外。

    sha          MR 的 head commit(完整編號)
    project_dir  dbt 專案在 repo 裡的目錄;空字串 = repo 根目錄(設定檔 dbt.project_dir)
    download     測試用;預設是 toolbox.gitlab.download_archive
    """
    try:
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise DbtProjectError("commit 編號不合法")
        prefix = check_project_dir(project_dir)
        key = (sha, prefix)
        with _cache_lock:
            if key in _cache:
                _cache.move_to_end(key)
                return _copy(_cache[key])
        if download is None:
            from toolbox.gitlab import download_archive as download
        data = download(sha, prefix, max_bytes=archive.MAX_COMPRESSED_BYTES)
        project = _parse(data, prefix)
        _remember(key, project)
        return project
    except Exception as e:                  # noqa: BLE001 — 一律收斂成失敗結果
        return DbtProject(ok=False, error=_safe_error(e))


# ------------------------------------------------------------------ 解析
def _parse(data: bytes, prefix: str) -> DbtProject:
    base = f"{prefix}/" if prefix else ""
    cfg = archive.extract_archive(data, tuple(f"{base}{name}" for name in ROOT_FILES))
    if not cfg.ok:
        raise DbtProjectError(f"解不開打包下載的內容({cfg.error})")
    # 只收剛好是這些檔名的檔案;同名的目錄(例如 packages.yml/x.yml)不算
    root_files = {name: cfg.files[f"{base}{name}"] for name in ROOT_FILES
                  if f"{base}{name}" in cfg.files}
    text = root_files.get("dbt_project.yml")
    if text is None:
        raise DbtProjectError("專案目錄裡沒有 dbt_project.yml(請檢查設定檔的 dbt.project_dir)")
    model_paths, macro_paths, seed_paths, snapshot_paths = _read_paths(text)
    # 收內容:model / macro / snapshot(名稱寫在檔案內容裡)/ seed(屬性檔可能設 alias)。
    # 列檔名:model(.py model)/ seed(.csv)/ snapshot
    wanted = tuple(dict.fromkeys(f"{base}{p}" for p in
                                 model_paths + macro_paths + seed_paths + snapshot_paths))
    listed = tuple(dict.fromkeys(f"{base}{p}" for p in model_paths + seed_paths + snapshot_paths))
    got = archive.extract_archive(data, wanted, list_prefixes=listed)
    if not got.ok:
        raise DbtProjectError(f"解不開打包下載的內容({got.error})")
    files = {rel[len(base):]: content for rel, content in got.files.items()}
    names = tuple(rel[len(base):] for rel in got.names)
    return DbtProject(ok=True, files=files, model_paths=model_paths, macro_paths=macro_paths,
                      root_files=root_files, seed_paths=seed_paths,
                      snapshot_paths=snapshot_paths, names=names)


def _read_paths(text: str) -> tuple[tuple[str, ...], ...]:
    try:
        raw = yaml.safe_load(text)
    except Exception:                       # noqa: BLE001
        raise DbtProjectError("dbt_project.yml 不是合法的 YAML") from None
    if not isinstance(raw, dict):
        raise DbtProjectError("dbt_project.yml 的最上層必須是物件")
    # dbt 1.0 以前叫 source-paths / data-paths;兩個都寫時以新名稱為準(與 dbt 相同)
    model_key = "model-paths" if "model-paths" in raw else "source-paths"
    seed_key = "seed-paths" if "seed-paths" in raw else "data-paths"
    return (_path_list(raw, model_key, DEFAULT_MODEL_PATHS),
            _path_list(raw, "macro-paths", DEFAULT_MACRO_PATHS),
            _path_list(raw, seed_key, DEFAULT_SEED_PATHS),
            _path_list(raw, "snapshot-paths", DEFAULT_SNAPSHOT_PATHS))


def _path_list(raw: dict, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, list) or not value or len(value) > MAX_PATHS:
        raise DbtProjectError(f"dbt_project.yml 的 {key} 必須是 1 到 {MAX_PATHS} 個目錄的清單")
    paths = []
    for p in value:
        if not isinstance(p, str):
            raise DbtProjectError(f"dbt_project.yml 的 {key} 只能是字串")
        if "{" in p or "}" in p:
            # dbt 允許在這裡寫 Jinja(例如 env_var);我們不展開設定檔,看不懂就失敗。
            # 下面的字元白名單也會擋掉,這裡先擋是為了讓錯誤訊息講對原因
            raise DbtProjectError(f"dbt_project.yml 的 {key} 含樣板標記,無法判斷實際目錄")
        paths.append(_check_relative(p.rstrip("/"), f"dbt_project.yml 的 {key}"))
    return tuple(paths)


def extra_ref_names(project) -> frozenset[str] | None:
    """ref() 找得到、但不是 model 目錄 .sql 的名稱:seed(.csv 檔名)、Python model
    (.py 檔名)、snapshot({% snapshot 名稱 %} 區塊與 yml 的 snapshots:)。給
    dbt_relations.project_info_from_files(other_ref_names=...) 用。

    **看不懂就回 None**(= 名單不完整,ref() 找不到時不能判定不存在):專案沒取回成功、
    seed / Python model 的檔名含控制字元、snapshot 區塊的名稱不是合法識別字(例如樣板)、
    區塊寫法看不懂、yml 解析不了或形狀不對、snapshot 的 yml 合計超過上限。
    """
    if not isinstance(project, DbtProject) or not project.ok:
        return None
    found: set[str] = set()
    yml_chars = 0
    for path in project.names:
        stem, dot, ext = path.rsplit("/", 1)[-1].rpartition(".")
        if not dot or not stem:
            continue
        ext = ext.lower()
        if ext == "csv" and _under(path, project.seed_paths):
            found.add(stem)
        elif ext == "py" and _under(path, project.model_paths):
            found.add(stem)
        else:
            continue
        if not stem.isprintable():
            # 檔名清單只過安全必要的檢查:含控制字元的名稱看不懂 → 名單視為不完整
            return None
    for path, content in project.files.items():
        if not _under(path, project.snapshot_paths):
            continue
        lower = path.lower()
        if lower.endswith(".sql"):
            blocks = [m.group(1) for m in _SNAPSHOT_BLOCK.finditer(content)]
            # 每個 {% snapshot 都要對到一個寫得出名稱的區塊;對不上就是看不懂
            if (len(blocks) != len(_SNAPSHOT_TAG.findall(content))
                    or not all(_IDENT.fullmatch(name) for name in blocks)):
                return None
            found.update(blocks)
        elif lower.endswith((".yml", ".yaml")):
            yml_chars += len(content)
            if yml_chars > MAX_SNAPSHOT_YML_CHARS:
                # MR 可以放很多大 yml 拖慢審查;正常專案遠低於上限 → 不解析,名單視為不完整
                return None
            snapshots = _yaml_snapshots(content)
            if snapshots is None:
                return None
            found.update(snapshots)
    return frozenset(found)


def _yaml_snapshots(text: str) -> list[str] | None:
    """yml 的 snapshots: 清單裡的名稱(新版 dbt 可以在 yml 定義 snapshot)。看不懂回 None。"""
    try:
        if _LIBYAML:
            doc = yaml.load(text, Loader=yaml.CSafeLoader)    # 安全載入器的 C 版
        else:
            doc = yaml.safe_load(text)
    except Exception:                       # noqa: BLE001 — 看不懂就是看不懂
        return None
    if doc is None:
        return []
    if not isinstance(doc, dict):
        return None
    entries = doc.get("snapshots")
    if entries is None:
        return []
    if not isinstance(entries, list):
        return None
    names = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not _IDENT.fullmatch(name):
            return None
        names.append(name)
    return names


def _under(path: str, dirs: tuple[str, ...]) -> bool:
    return any(path == d or path.startswith(d + "/") for d in dirs)


def check_project_dir(project_dir) -> str:
    """空字串 = repo 根目錄;其餘必須是 repo 內的相對目錄(只容許結尾的 /)。"""
    if not isinstance(project_dir, str):
        raise DbtProjectError("設定檔的 dbt.project_dir 必須是字串")
    if project_dir == "":
        return ""
    return _check_relative(project_dir.rstrip("/"), "設定檔的 dbt.project_dir")


def _check_relative(path: str, what: str) -> str:
    """只接受 repo 內的相對目錄:英數與 _ . - 以 / 分段,不含 ..、. 或空段落。"""
    parts = path.split("/")
    if (not path or path.startswith("/")
            or any(p in ("", ".", "..") for p in parts)
            or not all(re.fullmatch(r"[A-Za-z0-9_.\-]+", p) for p in parts)):
        raise DbtProjectError(f"{what} 不是合法的相對目錄")
    return path


# ------------------------------------------------------------------ 快取
def _size(project: DbtProject) -> int:
    return (sum(len(k) + len(v) for d in (project.files, project.root_files) for k, v in d.items())
            + sum(map(len, project.names)))


def _copy(project: DbtProject) -> DbtProject:
    """快取進出都複製 files / root_files:呼叫端改了拿到的字典,不能影響下一次審查拿到的內容。
    (目錄設定與 names 是 tuple,不可變,直接沿用)"""
    return DbtProject(ok=project.ok, files=dict(project.files),
                      model_paths=project.model_paths, macro_paths=project.macro_paths,
                      error=project.error, root_files=dict(project.root_files),
                      seed_paths=project.seed_paths, snapshot_paths=project.snapshot_paths,
                      names=project.names)


def _remember(key, project: DbtProject) -> None:
    """快取成功的結果(失敗在 _parse 就丟例外,不會走到這裡);單筆超過總量上限不快取;
    超過筆數或總量就從最久沒用的開始丟。"""
    if _size(project) > CACHE_MAX_BYTES:
        return
    with _cache_lock:
        _cache[key] = _copy(project)
        _cache.move_to_end(key)
        while len(_cache) > CACHE_MAX_ENTRIES or sum(map(_size, _cache.values())) > CACHE_MAX_BYTES:
            _cache.popitem(last=False)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _safe_error(e: BaseException) -> str:
    if isinstance(e, DbtProjectError):
        text = f"DbtProjectError: {e}"
    else:
        # 下載失敗(ArchiveDownloadError)的訊息本來就是固定文字;其他例外只留類型
        from toolbox.gitlab import ArchiveDownloadError
        text = (f"ArchiveDownloadError: {e}" if isinstance(e, ArchiveDownloadError)
                else f"{type(e).__name__}: 無法取得 dbt 專案")
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:MAX_ERROR_CHARS]
