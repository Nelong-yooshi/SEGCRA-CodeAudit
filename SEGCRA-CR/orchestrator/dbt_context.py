"""dbt_context — 審查一個 MR 之前,把它那個 commit 的 dbt 專案準備好。

預掃要展開 dbt model,需要專案的 macro 與專案資訊(哪些 model / source 存在);這些都不在
MR 的 diff 裡。這裡依 MR 的 head commit 取回專案(dbt_project.load_dbt_project),整理成
預掃用得到的形狀:

  * 變更檔在不在 dbt 專案裡(設定檔的 dbt.project_dir)、相對專案根目錄的路徑
  * 變更檔在 head commit 的**完整內容**——真實 GitLab 模式的 diff 只有新增的行,
    只改了幾行的 model 拿片段去展開沒有意義
  * macro 目錄底下的 .sql(交給展開器的 macro_files)
  * 專案資訊(dbt_relations.project_info_from_files),用來驗證 ref() / source()
  * MR 改到 model 以外的東西(macro、屬性檔、專案設定檔…)時的 macro 反查結果
    (dbt_mr_impact.analyze_mr_macro_impact),以及比較前後差異要用的變更前專案

**取不回來就是取不回來**:error 不是 None 時,呼叫端要把專案內的 .sql 一律當成「無法
展開」交人工,不可以退回「沒有 macro」繼續展開——那會把呼叫專案 macro 的 model 展成
少了一段的 SQL,或讓規則掃到片段卻看起來沒問題。

本模組不渲染樣板、不寫檔;連網只發生在 read_file(預設 toolbox.gitlab.read_file_at,先讀
dbt_project.yml 判斷目錄設定)與 load(預設 load_dbt_project → download_archive),兩者都用唯讀 token。

mock 模式(沒有設定 GitLab)沒有地方可以打包下載。若 `<FIXTURES_DIR>/dbt_archives/<commit 編號>/`
放了那個 commit 的專案,就把它打包成跟 GitLab 一樣的形狀、交給**同一個** load_dbt_project
(解壓、路徑檢查、大小上限、讀設定都走同一條路);沒有放就維持「不適用」。用途是 demo、測試與評測
能實際跑到接線——內附的範例在 examples/dbt_wiring/。
專案的屬性檔(yml)是待審 repo 的內容:整理專案資訊時要解析它們,放在有逾時與記憶體上限的
子行程裡做(_summarize_isolated),逾時或失敗就當成專案取不回來。
"""
import dataclasses
import gzip
import io
import re
import tarfile
from dataclasses import dataclass, field

from .dbt_impact import ImpactReport, normalize_path, path_kind
from .dbt_mr_impact import analyze_mr_macro_impact
from .dbt_project import (DbtLayout, DbtProject, DbtProjectError, check_project_dir,
                          extra_ref_names, load_dbt_project, read_layout)
from .dbt_relations import project_info_from_files
from .dbt_render import (DEFAULT_ISOLATED_MEMORY_MB, DEFAULT_ISOLATED_TIMEOUT_S, DbtProjectInfo,
                         is_dbt_template)
from .isolation import run_isolated

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class DbtContext:
    prefix: str = ""                 # dbt 專案在 repo 裡的目錄;空字串 = repo 根目錄
    error: str | None = None         # 不是 None:專案取不回來(固定文字,可以寫進報告)
    project: DbtProject | None = None
    info: DbtProjectInfo | None = None
    macro_files: dict = field(default_factory=dict)   # macro 目錄底下的 .sql {路徑: 內容}
    # macro 反查的結果。None = 不需要反查(MR 在專案裡只改到 model);專案取不回來時是
    # 「全部 model 可能受影響」
    impact: ImpactReport | None = None
    # 變更前(MR 的 base commit)的專案,比較受影響 model 前後差異用;取不回來是 None
    base_project: DbtProject | None = None
    base_info: DbtProjectInfo | None = None
    base_macro_files: dict = field(default_factory=dict)
    # dbt 會讀的目錄(dbt_project.yml 的設定);None = 不知道,專案目錄裡的檔案都算
    layout: DbtLayout | None = None

    def __post_init__(self):
        # 沒有內容、也沒有失敗原因的狀態不應該出現;萬一出現,當成取不回來(交人工),
        # 不讓用到 project 的地方丟 AttributeError
        if self.project is None and self.error is None:
            object.__setattr__(self, "error", "dbt 專案的狀態不完整(沒有內容,也沒有失敗原因)")

    def is_macro(self, path) -> bool:
        """這個變更檔是不是專案的 macro 檔(不是 model,不能拿去當 model 展開)。

        **要有反查結果才算**:macro 檔不走 model 的展開與解析檢查,是因為它對 model 的影響
        改由反查負責。兩者必須同時成立,不能出現「不檢查、也沒有反查」的空窗。"""
        rel = self.relative(path)
        return (rel is not None and self.project is not None and self.impact is not None
                and path_kind(rel, self.project.model_paths, self.project.macro_paths,
                              self.project.resource_paths) == "macro")

    def touches_dbt(self, files) -> bool:
        """這個 MR 有沒有改到 dbt 的東西(樣板、macro、屬性檔、專案設定檔)。

        給「dbt 的變更沒有經過執行驗證」的防線用,寧可多算:專案取不回來、或有反查結果
        (改到 model 以外的專案檔)都算;專案裡的檔案,head 的完整內容或手上的片段有樣板
        標記也算。"""
        if self.error is not None or self.impact is not None:
            return True
        for f in files:
            if not isinstance(f, dict) or self.relative(f.get("path")) is None:
                continue
            head = self.head_text(f.get("path"))
            text = f.get("full_content") or f.get("diff") or ""
            if (head is not None and is_dbt_template(head)) or is_dbt_template(text):
                return True
        return False

    def relative(self, path) -> str | None:
        """repo 內的路徑 → 相對 dbt 專案根目錄的路徑;不屬於 dbt(或路徑不合法)回 None。

        知道專案的目錄設定時,只有 dbt 會讀的目錄與根目錄的設定檔才算:專案就在 repo 根目錄時,
        CI 設定、一般的 SQL 檔不是 dbt 的檔案。"""
        rel = _relative(path, self.prefix)
        if rel is None or (self.layout is not None and not self.layout.contains(rel)):
            return None
        return rel

    def head_text(self, path) -> str | None:
        """變更檔在 head commit 的完整內容;沒取回專案、不在專案裡、或那個檔沒被收進來回 None。"""
        rel = self.relative(path)
        if rel is None or self.project is None:
            return None
        return self.project.files.get(rel)


def _relative(path, prefix: str) -> str | None:
    path = normalize_path(path)
    if path is None:
        return None
    if not prefix:
        return path
    return path[len(prefix) + 1:] if path.startswith(prefix + "/") else None


_PROJECT_SUFFIXES = (".sql", ".yml", ".yaml")
_INVALID_PATH = "(不合法的路徑)"
# 先讀 dbt_project.yml 判斷目錄設定時,檔案大小的上限
MAX_LAYOUT_BYTES = 1_000_000


def _changed_in_project(files, prefix: str, layout: DbtLayout | None = None, *,
                        any_file: bool = False) -> list[str]:
    """MR 改到的 dbt 檔案(相對專案根目錄;含改名前的舊路徑)。

    知道目錄設定(layout)時,只算 dbt 會讀的目錄與根目錄的設定檔;不知道時退回「專案目錄裡的
    .sql / .yml / .yaml」;any_file 為 True 時專案目錄裡的任何檔案都算(還沒讀到目錄設定時的
    初步判斷)。路徑不合法的一律算進來:看不懂就往「取回專案、交給反查」靠,反查那邊也是這樣
    (dbt_mr_impact._changed_paths 遇到不合法的路徑就判成全部 model 可能受影響)。"""
    found = []
    for f in files:
        for key in ("path", "old_path"):
            raw = f.get(key) if isinstance(f, dict) else None
            if raw is None and key == "old_path" and isinstance(f, dict):
                continue                    # 沒有舊路徑(非改名)
            if normalize_path(raw) is None:
                found.append(_INVALID_PATH)
                continue
            rel = _relative(raw, prefix)
            if rel is None:
                continue
            if any_file or (layout.contains(rel) if layout is not None
                            else rel.endswith(_PROJECT_SUFFIXES)):
                found.append(rel)
    return found


def _needs_project(files, prefix: str, layout: DbtLayout | None = None) -> bool:
    """MR 改到 dbt 的檔案(或改名、移走)才需要取回專案。

    .sql 不看內容有沒有樣板標記:真實 GitLab 模式拿到的只有 diff 新增的行,改到的那幾行
    沒有標記不代表整個檔案沒有。屬性檔、專案設定檔改了會影響 model 的行為,要交給 macro
    反查判斷。"""
    return bool(_changed_in_project(files, prefix, layout))


def _head_layout(mr: dict, prefix: str, real_mode: bool, read_file) -> tuple[DbtLayout | None, bool]:
    """head commit 的 dbt_project.yml → 目錄設定。回傳 (目錄設定, 檔案是否不存在)。

    在打包下載整個專案**之前**先讀這一個檔:專案就在 repo 根目錄時,只改了 dbt 以外檔案的 MR
    不必下載整個 repo。讀不到或看不懂時回 (None, False),呼叫端退回不看目錄設定的判斷——
    接下來取回專案那一步也會失敗、交人工,不會因此當成沒事。"""
    sha = mr.get("sha")
    if not isinstance(sha, str) or _SHA.fullmatch(sha) is None:
        return None, False
    path = f"{prefix}/dbt_project.yml" if prefix else "dbt_project.yml"
    if read_file is None:
        if real_mode:
            from toolbox.gitlab import read_file_at as read_file
        else:
            read_file = _read_fixture_file
    try:
        text = read_file(sha, path, max_bytes=MAX_LAYOUT_BYTES)
    except Exception:                       # noqa: BLE001 — 讀不到就退回原本的判斷
        return None, False
    if text is None:
        return None, True
    try:
        return read_layout(text), False
    except DbtProjectError:
        return None, False


def prepare_dbt_context(mr: dict, dbt_cfg: dict | None, *, real_mode: bool | None = None,
                        load=load_dbt_project, summarize=None,
                        read_file=None) -> DbtContext | None:
    """回傳 None = 不需要 / 不適用,預掃的行為與沒有這個模組時完全相同:

      * dbt.enabled 不是 True(預設關閉)
      * mock 模式,而且 fixture 目錄裡沒有放這個 MR 那個 commit 的專案(見模組說明)
      * MR 沒有改到 dbt 的檔案(dbt_project.yml 設定的目錄與根目錄的設定檔;先讀這一個檔判斷,
        讀不到時退回「專案目錄裡的 .sql / .yml / .yaml」)
      * head commit 沒有 dbt_project.yml,而且 MR 沒有改到專案目錄裡的 .sql / .yml / .yaml
        (有改到時算取回失敗)

    其餘情況一律回傳 DbtContext;取回失敗時 error 有值(見模組說明)。不丟例外。

    real_mode / load / summarize / read_file  測試用;預設是 toolbox.gitlab.REAL_MODE /
                load_dbt_project / _summarize_isolated / toolbox.gitlab.read_file_at
    """
    if not dbt_cfg or dbt_cfg.get("enabled") is not True:
        return None
    if real_mode is None:
        from toolbox import gitlab
        real_mode = gitlab.REAL_MODE
    if real_mode is not True:
        # mock 模式:fixture 目錄有放這個 commit 的專案才適用;呼叫端自己給了 load 就照它的
        if not isinstance(mr, dict) or _fixture_project_dir(mr.get("sha")) is None:
            return None
        if load is load_dbt_project:
            load = _load_from_fixtures
        # fixture 的變更清單一定是完整的(mock 的 get_mr_diff 不帶 truncated 這個欄位)
        mr = {**mr, "truncated": mr.get("truncated", False)}
    try:
        prefix = check_project_dir(dbt_cfg.get("project_dir", ""))
    except DbtProjectError as e:
        # 設定檔載入時已檢查過;走到這裡代表設定是程式另外組的。不知道專案在哪,
        # 就把整個 repo 當成專案內(寧可多交人工)
        return _failed("", str(e))
    files = mr.get("files") if isinstance(mr, dict) else None
    if not isinstance(files, list) or not _changed_in_project(files, prefix, any_file=True):
        return None                          # 專案目錄裡沒有任何變更:連設定檔都不必讀
    layout, missing = _head_layout(mr, prefix, real_mode, read_file)
    if missing:
        # head commit 讀不到 dbt_project.yml:設定的專案目錄錯了、MR 把它刪了或移走了,或唯讀
        # token 看不到這個專案(GitLab 對沒有權限的專案也回「找不到」)。
        # 跟以前一樣要讓人看到「取不回來」,不可以靜靜變成不適用(只是不必再下載整個專案)
        if not _needs_project(files, prefix):
            return None
        return _failed(prefix, "head commit 讀不到 dbt_project.yml(專案目錄設定錯誤,或唯讀 token 看不到這個專案)")
    if not _needs_project(files, prefix, layout):
        return None
    try:
        return _prepare(mr, prefix, load, summarize or _summarize_isolated, layout)
    except Exception as e:                  # noqa: BLE001 — 一律收斂成取回失敗
        return _failed(prefix, f"準備 dbt 專案時發生非預期錯誤({type(e).__name__})", layout)


def _failed(prefix: str, reason: str, layout: DbtLayout | None = None) -> DbtContext:
    """專案取不回來:專案裡的檔案交人工(見模組說明),macro 反查也當成做不了——
    不知道 MR 有沒有改到 macro,不可以回報「沒有影響」。"""
    return DbtContext(prefix=prefix, error=reason, layout=layout, impact=ImpactReport(
        uncertain=(f"無法取回 dbt 專案({reason}),macro 反查未執行,保守視為全部 model 可能受影響",),
        all_models_possibly_affected=True))


def _prepare(mr: dict, prefix: str, load, summarize, layout: DbtLayout | None = None) -> DbtContext:
    sha, head_sha = mr.get("sha"), mr.get("head_sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        return _failed(prefix, "MR 沒有可用的 head commit 編號", layout)
    if head_sha is not None and head_sha != sha:
        # GitLab 回的兩個 head commit 對不上:不挑一個硬做。
        # 注意這**不是**用來偵測「取變更清單期間 MR 被推了新 commit」——兩個欄位來自同一次
        # 回應,那種情況這裡看不出來。那時取回的專案是 sha 這一版、變更清單可能是新的一版;
        # 審查結果(commit status)也是寫在 sha 這一版上,新的 commit 沒有 status、要等它
        # 自己的那次審查,所以不會因此放行沒審過的版本。
        return _failed(prefix, "MR 的 head commit 資訊不一致(sha 與 diff_refs.head_sha 不同)", layout)
    project = load(sha, prefix)
    if not isinstance(project, DbtProject) or not project.ok:
        reason = project.error if isinstance(project, DbtProject) and project.error else "原因不明"
        return _failed(prefix, reason, layout)
    summary = summarize(project)
    if summary is None:
        return _failed(prefix, "dbt 專案的屬性檔解析逾時或失敗", project.layout)
    info, macro_files = summary
    ctx = DbtContext(prefix=prefix, project=project, info=info, macro_files=macro_files,
                     layout=project.layout)
    # MR 在專案裡只改到 model:沒有 macro 反查要做,也就不需要變更前的專案(少一次下載、
    # 少一個失敗點)。分類用反查自己的規則(dbt_impact.path_kind),兩邊不會各說各話
    changed = _changed_in_project(mr["files"], prefix, project.layout)
    if all(path_kind(rel, project.model_paths, project.macro_paths, project.resource_paths) == "model"
           for rel in changed):
        return ctx
    impact = analyze_mr_macro_impact(mr, mr.get("base_sha"), prefix,
                                     load=lambda sha_, _dir: load(sha_, prefix))
    base = load(mr.get("base_sha"), prefix) if impact.affected_models else None   # 反查已取過,走快取
    base_summary = summarize(base) if isinstance(base, DbtProject) and base.ok else None
    if base_summary is not None:           # 變更前的專案整理不出來:當成沒有可以比較的基準
        return dataclasses.replace(ctx, impact=impact, base_project=base,
                                   base_info=base_summary[0], base_macro_files=base_summary[1])
    return dataclasses.replace(ctx, impact=impact)


def _summarize(project: DbtProject) -> tuple[DbtProjectInfo, dict]:
    """專案資訊 + macro 檔。會解析待審 repo 的 yml,正式路徑一律經 _summarize_isolated 呼叫。"""
    extra = extra_ref_names(project)
    info = project_info_from_files(
        {**project.files, **project.root_files},
        model_paths=project.model_paths, macro_paths=project.macro_paths,
        # seed / snapshot / Python model:知道它們存在(ref() 找得到),但表名一律標成無法確定
        # (由 project_info_from_files 依 other_ref_names 處理)
        other_ref_names=extra, root_files_checked=True)
    macro_files = {path: text for path, text in project.files.items()
                   if path.endswith(".sql")
                   and any(path.startswith(d + "/") for d in project.macro_paths)}
    return info, macro_files


def _summarize_isolated(project: DbtProject) -> tuple[DbtProjectInfo, dict] | None:
    """在子行程裡整理專案資訊;逾時、記憶體超限、異常或回傳的形狀不對都回 None。"""
    result = run_isolated(_summarize, (project,), timeout_s=DEFAULT_ISOLATED_TIMEOUT_S,
                          max_memory_mb=DEFAULT_ISOLATED_MEMORY_MB, on_failure=lambda _message: None)
    if (isinstance(result, tuple) and len(result) == 2
            and isinstance(result[0], DbtProjectInfo) and isinstance(result[1], dict)):
        return result
    return None


# ------------------------------------------------------------------ mock 模式的專案來源
MAX_FIXTURE_FILES = 2_000


def _fixture_project_dir(sha):
    """<FIXTURES_DIR>/dbt_archives/<sha>/;sha 不是完整的 commit 編號、或目錄不存在回 None。

    sha 會被接進路徑,所以只接受完整的 commit 編號(純十六進位),`..` 之類的值到不了檔案系統。"""
    if not (isinstance(sha, str) and _SHA.fullmatch(sha)):
        return None
    from toolbox import gitlab
    path = gitlab.FIXTURES_DIR / "dbt_archives" / sha
    return path if path.is_dir() else None


def _fixture_archive(sha, path="", *, max_bytes):
    """把 fixture 目錄打包成 GitLab `/repository/archive` 的形狀(單一頂層目錄;指定子目錄時
    保留那一層),給 load_dbt_project 的 download 參數用。簽章與 toolbox.gitlab.download_archive 相同。"""
    root = _fixture_project_dir(sha)
    if root is None:
        raise DbtProjectError("fixture 裡沒有這個 commit 的 dbt 專案")
    # 頂層只能有一層:多層的專案目錄(a/b)換成 a-b,底下保留原本的目錄
    top = f"fixture-{sha}" + (f"-{path.replace('/', '-')}" if path else "")
    # 符號連結也放進去(GitLab 的打包也會有),交給 extract_archive 照樣擋下,不在這裡靜默跳過
    members = sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink())
    if len(members) > MAX_FIXTURE_FILES:
        raise DbtProjectError(f"fixture 的 dbt 專案超過 {MAX_FIXTURE_FILES} 個檔案")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        entry = tarfile.TarInfo(top)
        entry.type = tarfile.DIRTYPE
        tar.addfile(entry)
        for member in members:
            rel = member.relative_to(root).as_posix()
            if path and not rel.startswith(path + "/"):
                continue
            info = tarfile.TarInfo(f"{top}/{rel}")
            if member.is_symlink():
                info.type, info.linkname = tarfile.SYMTYPE, str(member.readlink())
                tar.addfile(info)
                continue
            data = member.read_bytes()
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    data = gzip.compress(buf.getvalue())
    if len(data) > max_bytes:
        raise DbtProjectError(f"fixture 的 dbt 專案打包後超過 {max_bytes} 位元組上限")
    return data


def _read_fixture_file(sha, path: str, *, max_bytes: int) -> str | None:
    """mock 模式的 read_file_at:讀 fixture 裡那個 commit 的單一檔案;不存在回 None。
    符號連結當成讀不到(丟例外),與打包那條路一樣不跟隨連結。"""
    root = _fixture_project_dir(sha)
    if root is None:
        raise DbtProjectError("fixture 裡沒有這個 commit 的 dbt 專案")
    target = root.joinpath(*path.split("/"))
    if target.is_symlink():
        raise DbtProjectError("fixture 裡的檔案是符號連結")
    if not target.is_file():
        return None
    data = target.read_bytes()
    if max_bytes < len(data):
        raise DbtProjectError(f"檔案超過 {max_bytes} 位元組上限")
    return data.decode("utf-8")


def _load_from_fixtures(sha, project_dir: str = "") -> DbtProject:
    return load_dbt_project(sha, project_dir, download=_fixture_archive)
