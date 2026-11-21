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
