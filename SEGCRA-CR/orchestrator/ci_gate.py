"""ci_gate — 審查做成部署端 CI 的最後一個 stage:審查結果換成 job 的結束碼、確認呼叫者。

部署端要掛審查的 repo 有自己的 MR pipeline,而且設定了 pipeline 要過才能合併。審查做成那條
pipeline 裡的一個 job,pipeline 會等它跑完,結果就在同一條上,既有的合併設定直接用。這條路上
**不寫 commit status**:job 的結果就是閘門。

job 不在 runner 上直接跑審查,而是呼叫審查服務(scripts/webhook_server.py 的 /ci/review)
等結果。理由:runner 上跑的 job 由 MR 的 pipeline 設定決定,MR 可以加一個指定同一台 runner 的
job;審查需要的 GitLab token 與模型端點的通行碼若放在 runner 上,這種 job 讀得到。審查服務
那台機器上才有這些秘密,job 只帶自己的 CI_JOB_TOKEN。

| 審查結果 | 結束碼 | job / pipeline |
|---|---|---|
| auto_approved | 0 | 成功 / 綠燈,可以合併 |
| needs_human(審完了,交給人判斷) | 3 | 失敗但 `allow_failure: exit_codes: [3]` 允許 / passed with warnings |
| blocked | 4 | 失敗 / 紅燈,鎖住 |
| 審查沒跑完整(報告裡有環節未完整、整份失敗或逾時、被新的 commit 取代) | 5 | 失敗 / 紅燈,鎖住;重審就是 retry 那個 job |

needs_human 用 3:1 與 2 是程式異常結束時常見的結束碼(Python 沒接住的例外是 1、參數錯誤是 2),
允許失敗的那個碼不能跟它們重疊,否則 job 自己出錯也會被當成「審完了、交給人判斷」。
"""
from __future__ import annotations

import os

EXIT_APPROVED = 0
EXIT_NEEDS_HUMAN = 3
EXIT_BLOCKED = 4
EXIT_INCOMPLETE = 5
EXIT_CODES = frozenset({EXIT_APPROVED, EXIT_NEEDS_HUMAN, EXIT_BLOCKED, EXIT_INCOMPLETE})

_BY_DECISION = {"auto_approved": EXIT_APPROVED, "needs_human": EXIT_NEEDS_HUMAN,
                "blocked": EXIT_BLOCKED}


# 同一個 MR 推了新的 commit(新的 pipeline)時要不要再審。部署端的維運在審查服務的 .env 設,
# MR 改不到:
#   every  每次都審(預設)
#   first  審過一次之後,之後的 pipeline 不再呼叫模型,回 EXIT_NEEDS_HUMAN(交人工、可以合併),
#          並在 MR 上寫明這次的改動沒有經過 AI 審查。見 should_skip 的條件
REPEAT_EVERY, REPEAT_FIRST = "every", "first"


def review_repeat() -> str:
    """CI_REVIEW_REPEAT 的值。不認得的值當成 every:設錯時往「多審」的方向走。"""
    value = os.environ.get("CI_REVIEW_REPEAT", "every").strip().lower()
    return value if value in (REPEAT_EVERY, REPEAT_FIRST) else REPEAT_EVERY


def should_skip(mode: str, previous: int | None, force: bool) -> bool:
    """這次的 CI 審查要不要跳過(不呼叫模型、回 EXIT_NEEDS_HUMAN)。只有這些條件都成立才跳過:

      * 模式是 first,而且這次不是手動要求重審(force)
      * 審查服務自己記得這個 MR(同一個目標分支)上一次**跑完**的結果(previous)——
        只看服務自己的紀錄,不看 GitLab 的 label 或留言:那些開發者改得到
      * 上一次是放行或交人工。上一次是擋下(4)或沒跑完整(5)時照樣審:不然被擋的 MR
        推一個新 commit 就變成可以合併"""
    return (mode == REPEAT_FIRST and not force
            and previous in (EXIT_APPROVED, EXIT_NEEDS_HUMAN))


SKIPPED_NOTE = (
    "這次的改動**沒有經過 AI 審查**:部署端設定為「同一個 MR 只自動審第一次」(CI_REVIEW_REPEAT=first)。"
    "上一次 AI 審查的結論是「{previous}」,那是之前的 commit 的結論,不代表這次的改動。"
    "合併前請有合併權限的人看過這次的改動。要重新審查:手動執行 pipeline 並帶變數 SEGCRA_REVIEW=1。")
_PREVIOUS_TEXT = {EXIT_APPROVED: "自動放行", EXIT_NEEDS_HUMAN: "交人工確認"}


def skipped_note(previous: int) -> str:
    return SKIPPED_NOTE.format(previous=_PREVIOUS_TEXT.get(previous, "交人工確認"))


def exit_code(report) -> int:
    """審查報告 → job 的結束碼。沒跑完整一律 EXIT_INCOMPLETE,就算決策是 needs_human:
    沒跑完就是沒有結論(可能是 MR 本身就有問題,例如展開不了),不能交給「審完了」那一條路。"""
    from .pipeline import review_incomplete
    if (not isinstance(report, dict) or report.get("writeback_skipped")
            or not isinstance(report.get("findings", []), list)):
        return EXIT_INCOMPLETE
    try:
        if review_incomplete(report):
            return EXIT_INCOMPLETE
    except Exception:                        # noqa: BLE001 — 判讀不了也是沒有結論
        return EXIT_INCOMPLETE
    return _BY_DECISION.get(report.get("decision"), EXIT_INCOMPLETE)


def check_request(job, pipeline, mr, *, project_id, mr_iid: int, sha: str) -> str | None:
    """確認 /ci/review 的呼叫者:是這個專案、這個 MR 的 MR pipeline 裡正在跑的 job,
    要審的 sha 就是那條 pipeline 的 sha、也是 MR 目前的 head。符合回 None,否則回原因
    (固定文字,不帶呼叫端送來的內容)。

    job       gitlab.job_from_token 的結果(GitLab 的 GET /job)
    pipeline  gitlab.pipeline_info 的結果(這個專案的那條 pipeline)
    mr        gitlab.mr_head 的結果"""
    if not isinstance(job, dict):
        return "job token 無效或 job 已經結束"
    if job.get("status") != "running":
        return "job 不是執行中"
    jp = job.get("pipeline")
    if not isinstance(jp, dict) or not isinstance(jp.get("id"), int):
        return "查不到 job 所在的 pipeline"
    if str(jp.get("project_id")) != str(project_id):
        return "job 不屬於這個專案"
    if not isinstance(pipeline, dict) or pipeline.get("id") != jp["id"]:
        return "查不到 job 所在的 pipeline"
    if pipeline.get("source") != "merge_request_event":
        return "job 所在的 pipeline 不是 MR pipeline"
    if not isinstance(sha, str) or not sha or pipeline.get("sha") != sha or jp.get("sha") != sha:
        return "要審的 commit 不是這條 pipeline 的 commit"
    ref = pipeline.get("ref")
    if ref != f"refs/merge-requests/{mr_iid}/head":
        return "這條 pipeline 不是這個 MR 的"
    if not isinstance(mr, dict) or mr.get("state") != "opened":
        return "MR 不是開啟中"
    if mr.get("sha") != sha:
        return "要審的 commit 不是 MR 目前的 head(MR 已經有新的 commit)"
    return None
