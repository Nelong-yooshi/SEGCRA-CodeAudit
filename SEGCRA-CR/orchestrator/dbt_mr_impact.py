"""dbt_mr_impact — 把一個 MR 轉成 macro 反查的輸入並執行(#15 第 2 點「macro 反查接線」的轉接層)。

輸入是 get_mr_diff() 的結果(head commit、變更清單)與 MR 的 base commit;兩個 commit 的
dbt 專案都以 load_dbt_project() 取回,再交給 analyze_macro_impact_isolated():

  * 路徑換算:變更清單是相對 repo 根目錄,反查要相對 dbt 專案根目錄;專案目錄外的變更
    與 dbt 無關,不列入
  * 新增 / 刪除 / 改名:GitLab 的變更清單只有新舊路徑,不另外判斷——拿 head 與 base
    兩個專案比對:head 有、base 沒有 = 新增;改名與刪除則把舊路徑一併列為變更
  * 專案根目錄的設定檔(dbt_project.yml、packages.yml、dependencies.yml)被改:dispatch
    設定、變數、套件版本都可能改變 macro 的行為,無法逐一判斷 → 全部 model 可能受影響
  * head 與 base 的 model / macro 目錄設定不同:兩邊的檔案對不起來 → 不提供變更前內容
    (反查會標為不確定),並且全部 model 可能受影響
  * 反查拿到 model / macro 目錄的內容與 dbt_project.yml(hook 可能呼叫被改的 macro)
  * 反查只追 model:有 macro 變更、而專案有 snapshot 時,另外標為不確定(交人工)

**任何一步失敗都收斂成「全部 model 可能受影響」**(needs_human),絕不回傳
「沒有影響」(#15 條件 7)。本模組不連網(下載由 load 負責)、不寫檔、不渲染樣板。
"""
import dataclasses

from .dbt_impact import ImpactReport, analyze_macro_impact_isolated, normalize_path
from .dbt_project import ROOT_FILES, DbtProjectError, check_project_dir, load_dbt_project

MAX_REASON_CHARS = 300


def analyze_mr_macro_impact(mr_diff, base_sha, project_dir: str = "", *,
                            load=load_dbt_project,
                            analyze=analyze_macro_impact_isolated) -> ImpactReport:
    """mr_diff      get_mr_diff() 解析後的字典:sha(head commit)、files([{path, old_path}])、
                 truncated(變更清單是否被截斷)
    base_sha     MR 的 base commit(GitLab diff_refs.base_sha)
    project_dir  dbt 專案在 repo 裡的目錄;空字串 = repo 根目錄
    load / analyze  測試用;預設是 load_dbt_project / analyze_macro_impact_isolated

    不丟例外;任何失敗都回傳 all_models_possibly_affected=True 的結果。
    """
    try:
        return _analyze(mr_diff, base_sha, project_dir, load, analyze)
    except _Fail as e:
        return _fail(str(e))
    except Exception as e:                  # noqa: BLE001 — 一律收斂成保守結果
        return _fail(f"轉接層發生非預期錯誤({type(e).__name__})")


class _Fail(Exception):
    """本模組判定的失敗;訊息是固定文字或已經過清理的錯誤訊息。"""


def _analyze(mr_diff, base_sha, project_dir, load, analyze) -> ImpactReport:
    if not isinstance(mr_diff, dict) or not isinstance(mr_diff.get("files"), list):
        raise _Fail("MR 變更清單的格式不對")
    if mr_diff.get("truncated") is not False:
        # 只有明確的 False 才算完整;缺欄位或其他值一律當成被截斷(少看一個變更檔,
        # 反查就可能回報「沒有影響」)
        raise _Fail("MR 變更清單可能不完整(被截斷或缺少完整性標記),看不到全部變更")
    prefix = _project_prefix(project_dir)
    changed = _changed_paths(mr_diff["files"], prefix)
    if not changed:
        return ImpactReport()               # 專案目錄內沒有任何變更:與 dbt 無關

    head = load(mr_diff.get("sha"), project_dir)
    base = load(base_sha, project_dir)
    for label, project in (("變更後", head), ("變更前", base)):
        if not project.ok:
            raise _Fail(f"無法取回{label}的 dbt 專案({project.error})")

    reasons = []
    root_changed = sorted(changed & set(ROOT_FILES))
    if root_changed:
        reasons.append(f"專案設定檔 {', '.join(root_changed)} 有變更(可能改變 dispatch、"
                       "變數或套件版本),無法逐一判斷影響範圍")
    same_layout = (head.model_paths == base.model_paths
                   and head.macro_paths == base.macro_paths)
    if not same_layout:
        reasons.append("變更前後的 model / macro 目錄設定不同,無法比對變更前的內容")

    paths = sorted(changed)
    head_files, base_files = _for_analysis(head), _for_analysis(base)
    added = [p for p in paths if p in head_files and p not in base_files] if same_layout else []
    report = analyze(head_files, paths,
                     base_files=base_files if same_layout else None,
                     added_paths=added,
                     model_dirs=head.model_paths, macro_dirs=head.macro_paths)
    if not isinstance(report, ImpactReport):
        raise _Fail("反查回傳了非預期的資料")
    if reasons:
        report = dataclasses.replace(
            report, uncertain=report.uncertain + tuple(_clip(r) for r in reasons),
            all_models_possibly_affected=True)
    if report.changed_macro_files and any(_in_dirs(p, head.snapshot_paths) for p in head.names):
        # 反查只追 model:snapshot 呼叫了被改的 macro 時看不出來。不能說「沒有影響」→ 交人工
        # (不標「全部 model 可能受影響」:受影響的可能是 snapshot,不是 model)
        report = dataclasses.replace(
            report, uncertain=report.uncertain + (
                "macro 有變更,但專案有 snapshot,反查只追 model,snapshot 是否受影響未分析",))
    return report


def _for_analysis(project) -> dict[str, str]:
    """交給反查的檔案:model / macro 目錄的內容,加上 dbt_project.yml(反查要看裡面的 hook
    有沒有呼叫被改的 macro——有的話全部 model 都受影響)。snapshot / seed 目錄的內容不給:
    反查不追它們,給了只會被當成目錄外的檔案。"""
    dirs = project.model_paths + project.macro_paths
    files = {p: c for p, c in project.files.items() if _in_dirs(p, dirs)}
    if "dbt_project.yml" in project.root_files:
        files["dbt_project.yml"] = project.root_files["dbt_project.yml"]
    return files


def _in_dirs(path: str, dirs: tuple[str, ...]) -> bool:
    return any(path == d or path.startswith(d + "/") for d in dirs)


def _project_prefix(project_dir) -> str:
    """與 load_dbt_project 同一套規則。要在判斷「有沒有 dbt 變更」**之前**檢查:
    否則設定錯的目錄會讓每個 MR 都變成「專案內沒有變更 → 沒有影響」,錯誤被藏起來。"""
    try:
        return check_project_dir(project_dir)
    except DbtProjectError as e:
        raise _Fail(str(e)) from None


def _changed_paths(files: list, prefix: str) -> set[str]:
    """變更清單的新舊路徑 → 相對 dbt 專案根目錄的路徑。專案目錄外的變更不列入。
    任何一筆看不懂就失敗:少算一個變更檔,反查就可能回報「沒有影響」。"""
    changed = set()
    for item in files:
        if not isinstance(item, dict):
            raise _Fail("MR 變更清單的格式不對")
        for key in ("path", "old_path"):
            raw = item.get(key)
            if raw is None and key == "old_path":
                continue                    # 沒有舊路徑(非改名)
            path = normalize_path(raw)
            if path is None:
                raise _Fail("MR 變更清單含不合法的路徑")
            if not prefix:
                changed.add(path)
            elif path.startswith(prefix + "/"):
                changed.add(path[len(prefix) + 1:])
    return changed


def _clip(text: str) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    return text[:MAX_REASON_CHARS]


def _fail(reason: str) -> ImpactReport:
    return ImpactReport(uncertain=(_clip(f"macro 反查無法完成:{reason},保守視為全部 model "
                                         "可能受影響"),),
                        all_models_possibly_affected=True)
