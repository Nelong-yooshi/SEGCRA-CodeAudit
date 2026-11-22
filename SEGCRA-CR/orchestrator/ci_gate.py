"""ci_gate — 審查做成 CI 的最後一個 stage 時用的兩件事:審查結果換成 job 的結束碼(exit_code)、
確認 /ci/review 的呼叫者(check_request)。

結束碼的對照與理由、為什麼 job 呼叫審查服務而不在 runner 上跑審查,見 docs/06 第 4 節;
接上去的步驟見 README §7。
"""
from __future__ import annotations

EXIT_APPROVED = 0
EXIT_NEEDS_HUMAN = 3
EXIT_BLOCKED = 4
EXIT_INCOMPLETE = 5
EXIT_CODES = frozenset({EXIT_APPROVED, EXIT_NEEDS_HUMAN, EXIT_BLOCKED, EXIT_INCOMPLETE})

_BY_DECISION = {"auto_approved": EXIT_APPROVED, "needs_human": EXIT_NEEDS_HUMAN,
                "blocked": EXIT_BLOCKED}


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


# 拒絕(確定不符)回 403;向 GitLab 查不到(連不上、逾時、非 2xx、內容不對)回 502;服務本身驗不了呼叫者
# (mock 模式、GITLAB_PROJECT 不是數字)回 503。job 印出的 HTTP 碼就分得出是哪一種
STATUS_REJECTED = 403
STATUS_LOOKUP_FAILED = 502
STATUS_UNAVAILABLE = 503

JOB_TOKEN_REJECTED = "job token 無效或 job 已經結束(GitLab 拒絕了這個 job token)"
MOCK_MODE_TEXT = ("審查服務在 mock 模式(GITLAB_URL、GITLAB_TOKEN、GITLAB_PROJECT 沒有設齊),沒有 GitLab,"
                  "CI 那條路驗不了呼叫者,一律不開始審查")
PROJECT_NOT_NUMBER_TEXT = ("GITLAB_PROJECT 不是數字:要填專案的數字 id(CI 那條路拿它跟 job 所在的專案比對,"
                           "填路徑一律對不上)")


def lookup_failure_text(what: str, exc: BaseException) -> str:
    """向 GitLab 查 `what` 失敗 → 固定文字(原因的說法跟取 MR 共用 mr_fetch_reason,不帶例外原文)。"""
    from toolbox.gitlab import NotAnMR
    from .mr_fetch_error import mr_fetch_reason
    if isinstance(exc, NotAnMR):
        reason = "GitLab 回了 200,但內容不是預期的資料(確認 GITLAB_URL 指到 GitLab 本身)"
    else:
        reason = mr_fetch_reason(exc)
    return f"向 GitLab 查{what}失敗:{reason}"


def check_job(job, *, project_id) -> str | None:
    """第一步:job token 查到的 job(gitlab.job_from_token)。符合回 None,否則回拒絕原因。"""
    if not isinstance(job, dict):
        return JOB_TOKEN_REJECTED
    if job.get("status") != "running":
        return "job 不是執行中"
    jp = job.get("pipeline")
    if not isinstance(jp, dict) or not isinstance(jp.get("id"), int):
        return "查不到 job 所在的 pipeline"
    if str(jp.get("project_id")) != str(project_id):
        return "job 不屬於這個專案"
    return None


def check_pipeline(job, pipeline, *, mr_iid: int, sha: str) -> str | None:
    """第二步:job 所在的 pipeline(gitlab.pipeline_info,這個專案的)。job 要先通過 check_job。"""
    jp = job["pipeline"]
    if not isinstance(pipeline, dict) or pipeline.get("id") != jp["id"]:
        return "查不到 job 所在的 pipeline"
    if pipeline.get("source") != "merge_request_event":
        return "job 所在的 pipeline 不是 MR pipeline"
    if not isinstance(sha, str) or not sha or pipeline.get("sha") != sha or jp.get("sha") != sha:
        return "要審的 commit 不是這條 pipeline 的 commit"
    if pipeline.get("ref") != f"refs/merge-requests/{mr_iid}/head":
        return "這條 pipeline 不是這個 MR 的"
    return None


def check_mr(mr, *, sha: str) -> str | None:
    """第三步:MR 目前的狀態(gitlab.mr_head)。"""
    if not isinstance(mr, dict) or mr.get("state") != "opened":
        return "MR 不是開啟中"
    if mr.get("sha") != sha:
        return "要審的 commit 不是 MR 目前的 head(MR 已經有新的 commit)"
    return None


def check_request(job, pipeline, mr, *, project_id, mr_iid: int, sha: str) -> str | None:
    """確認 /ci/review 的呼叫者:是這個專案、這個 MR 的 MR pipeline 裡正在跑的 job,
    要審的 sha 就是那條 pipeline 的 sha、也是 MR 目前的 head。符合回 None,否則回原因
    (固定文字,不帶呼叫端送來的內容)。三步依序:check_job、check_pipeline、check_mr。

    這裡只判斷「拿到的資料符不符合」;向 GitLab 查不到的情況不會走到這裡(呼叫端先接住,
    見 lookup_failure_text):查不到不等於不符。

    job       gitlab.job_from_token 的結果(GitLab 的 GET /job;GitLab 拒絕這個 token 時是 None)
    pipeline  gitlab.pipeline_info 的結果(這個專案的那條 pipeline)
    mr        gitlab.mr_head 的結果"""
    return (check_job(job, project_id=project_id)
            or check_pipeline(job, pipeline, mr_iid=mr_iid, sha=sha)
            or check_mr(mr, sha=sha))
