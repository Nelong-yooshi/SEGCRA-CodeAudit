"""dbt_context — 審查一個 MR 之前,把它那個 commit 的 dbt 專案準備好(#15 接線)。

預掃要展開 dbt model,需要專案的 macro 與專案資訊(哪些 model / source 存在);這些都不在
MR 的 diff 裡。這裡依 MR 的 head commit 取回專案(dbt_project.load_dbt_project),整理成
預掃用得到的形狀:

  * 變更檔在不在 dbt 專案裡(設定檔的 dbt.project_dir)、相對專案根目錄的路徑
  * 變更檔在 head commit 的**完整內容**——真實 GitLab 模式的 diff 只有新增的行,
    只改了幾行的 model 拿片段去展開沒有意義
  * macro 目錄底下的 .sql(交給展開器的 macro_files)
  * 專案資訊(dbt_relations.project_info_from_files),用來驗證 ref() / source()

**取不回來就是取不回來**:error 不是 None 時,呼叫端要把專案內的 .sql 一律當成「無法
展開」交人工,不可以退回「沒有 macro」繼續展開——那會把呼叫專案 macro 的 model 展成
少了一段的 SQL,或讓規則掃到片段卻看起來沒問題。

本模組不渲染樣板、不寫檔;連網只發生在 load(預設 load_dbt_project → download_archive)。
"""
import re
from dataclasses import dataclass, field

from .dbt_impact import normalize_path
from .dbt_project import (DbtProject, DbtProjectError, check_project_dir, extra_ref_names,
                          load_dbt_project)
from .dbt_relations import project_info_from_files
from .dbt_render import DbtProjectInfo

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class DbtContext:
    prefix: str = ""                 # dbt 專案在 repo 裡的目錄;空字串 = repo 根目錄
    error: str | None = None         # 不是 None:專案取不回來(固定文字,可以寫進報告)
    project: DbtProject | None = None
    info: DbtProjectInfo | None = None
    macro_files: dict = field(default_factory=dict)   # macro 目錄底下的 .sql {路徑: 內容}

    def relative(self, path) -> str | None:
        """repo 內的路徑 → 相對 dbt 專案根目錄的路徑;不在專案裡(或路徑不合法)回 None。"""
        return _relative(path, self.prefix)

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


def _needs_project(files, prefix: str) -> bool:
    """專案目錄裡有 .sql 被改到才需要取回專案。

    不看內容有沒有樣板標記:真實 GitLab 模式拿到的只有 diff 新增的行,改到的那幾行沒有
    標記不代表整個檔案沒有。"""
    for f in files:
        rel = _relative(f.get("path") if isinstance(f, dict) else None, prefix)
        if rel is not None and rel.endswith(".sql"):
            return True
    return False


def prepare_dbt_context(mr: dict, dbt_cfg: dict | None, *, real_mode: bool | None = None,
                        load=load_dbt_project) -> DbtContext | None:
    """回傳 None = 不需要 / 不適用,預掃的行為與沒有這個模組時完全相同:

      * dbt.enabled 不是 True(預設關閉)
      * 不是真實 GitLab 模式(mock:fixture 已經帶完整內容,也沒有 GitLab 可以取回專案)
      * MR 沒有改到 dbt 專案目錄裡的 .sql

    其餘情況一律回傳 DbtContext;取回失敗時 error 有值(見模組說明)。不丟例外。

    real_mode / load  測試用;預設是 toolbox.gitlab.REAL_MODE / load_dbt_project
    """
    if not dbt_cfg or dbt_cfg.get("enabled") is not True:
        return None
    if real_mode is None:
        from toolbox import gitlab
        real_mode = gitlab.REAL_MODE
    if real_mode is not True:
        return None
    try:
        prefix = check_project_dir(dbt_cfg.get("project_dir", ""))
    except DbtProjectError as e:
        # 設定檔載入時已檢查過;走到這裡代表設定是程式另外組的。不知道專案在哪,
        # 就把整個 repo 當成專案內(寧可多交人工)
        return DbtContext(error=str(e))
    files = mr.get("files") if isinstance(mr, dict) else None
    if not isinstance(files, list) or not _needs_project(files, prefix):
        return None
    try:
        return _prepare(mr, prefix, load)
    except Exception as e:                  # noqa: BLE001 — 一律收斂成取回失敗
        return DbtContext(prefix=prefix, error=f"準備 dbt 專案時發生非預期錯誤({type(e).__name__})")


def _prepare(mr: dict, prefix: str, load) -> DbtContext:
    sha, head_sha = mr.get("sha"), mr.get("head_sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        return DbtContext(prefix=prefix, error="MR 沒有可用的 head commit 編號")
    if head_sha is not None and head_sha != sha:
        # GitLab 回的兩個 head commit 對不上:不挑一個硬做。
        # 注意這**不是**用來偵測「取變更清單期間 MR 被推了新 commit」——兩個欄位來自同一次
        # 回應,那種情況這裡看不出來。那時取回的專案是 sha 這一版、變更清單可能是新的一版;
        # 審查結果(commit status)也是寫在 sha 這一版上,新的 commit 沒有 status、要等它
        # 自己的那次審查,所以不會因此放行沒審過的版本。
        return DbtContext(prefix=prefix,
                          error="MR 的 head commit 資訊不一致(sha 與 diff_refs.head_sha 不同)")
    project = load(sha, prefix)
    if not isinstance(project, DbtProject) or not project.ok:
        reason = project.error if isinstance(project, DbtProject) and project.error else "原因不明"
        return DbtContext(prefix=prefix, error=reason)
    info = project_info_from_files(
        {**project.files, **project.root_files},
        model_paths=project.model_paths, macro_paths=project.macro_paths,
        other_ref_names=extra_ref_names(project), root_files_checked=True)
    macro_files = {path: text for path, text in project.files.items()
                   if path.endswith(".sql")
                   and any(path.startswith(d + "/") for d in project.macro_paths)}
    return DbtContext(prefix=prefix, project=project, info=info, macro_files=macro_files)
