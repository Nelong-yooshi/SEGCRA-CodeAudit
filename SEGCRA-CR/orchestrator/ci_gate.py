"""ci_gate — 審查做成 CI 的最後一個 stage 時用的兩件事:審查結果換成 job 的結束碼(exit_code)、
確認 /ci/review 的呼叫者(check_request)。

結束碼的對照與理由、為什麼 job 呼叫審查服務而不在 runner 上跑審查,見 docs/06 第 4 節;
接上去的步驟見 README §7。
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


# 同一個 MR 推了新的 commit(新的 pipeline)時要不要再審(環境變數 CI_REVIEW_REPEAT,服務啟動時讀一次):
#   every  每次都審(預設)
#   first  審過一次之後,之後的 pipeline 整份審查都不跑(規則檢查與 AI 都沒有),回 EXIT_NEEDS_HUMAN
#          (交人工、可以合併),並在 MR 上寫明從哪個 commit 之後沒有經過審查。見 should_skip 的條件
REPEAT_EVERY, REPEAT_FIRST = "every", "first"
REPEAT_ENV = "CI_REVIEW_REPEAT"


class ReviewRepeatError(ValueError):
    """CI_REVIEW_REPEAT 設了但是空的,或不是 every / first:服務不啟動。"""


def review_repeat(environ=None) -> str:
    """CI_REVIEW_REPEAT 的值(去掉前後空白、不分大小寫)。沒設是 every;**設了但是空的、或不是 every /
    first 就丟 ReviewRepeatError**:部署端以為開了 first、其實每次都審(或反過來),從哪裡都看不出來。
    服務啟動時讀一次(webhook_server.main),啟動訊息印出用的是哪一種。"""
    # 名稱寫成字面:設定參數的檢查(env_registry)靠字面的名稱找讀法
    raw = os.environ.get("CI_REVIEW_REPEAT") if environ is None else environ.get("CI_REVIEW_REPEAT")
    if raw is None:
        return REPEAT_EVERY
    value = raw.strip().lower()
    if not value:
        raise ReviewRepeatError(f"環境變數 {REPEAT_ENV} 有設定但是空的:要填 every 或 first,不用就把它拿掉")
    if value not in (REPEAT_EVERY, REPEAT_FIRST):
        raise ReviewRepeatError(f"環境變數 {REPEAT_ENV} 只收 every 或 first(不分大小寫)")
    return value


def should_skip(mode: str, record: dict | None, *, sha: str, target_branch: str,
                running: bool) -> bool:
    """這次的 CI 審查要不要跳過(整份審查不跑、回 EXIT_NEEDS_HUMAN)。只有這些條件都成立才跳過:

      * 模式是 first
      * 同一個 MR 沒有還在跑的 CI 審查(running):有的話照樣審這次、取消舊的。不然「retry 還在審時推
        新 commit」,新的 pipeline 會照舊的紀錄跳過,retry 擋下的結論只留在舊的 pipeline 上
      * 審查服務自己記得這個 MR 上一次**真的審完**的結果(record)—— 只看服務自己的紀錄,不看 GitLab 的
        label 或留言:那些開發者改得到
      * 這次不是有人要求重審:上一次處理的(審過或跳過的)同一個 commit 又送來,就是有人對它按了 retry
        或重跑 pipeline(MR 的 pipeline 帶不了手動變數,「重審」只能用這個方式表達)
      * 目標分支跟紀錄相同,而且兩邊都取得到:換過目標分支(包括換回原本的)、或取不到,都照樣審
      * 上一次真的審的結果是放行或交人工。擋下(4)或沒跑完整(5)時照樣審:不然被擋的 MR 推一個新
        commit 就變成可以合併"""
    if mode != REPEAT_FIRST or running or not isinstance(record, dict):
        return False
    if record.get("last_sha") == sha:
        return False
    branch = record.get("branch")
    if not branch or not target_branch or branch != target_branch:
        return False
    return record.get("code") in (EXIT_APPROVED, EXIT_NEEDS_HUMAN)


SKIPPED_NOTE = (
    "`{since}` 之後的改動都**沒有經過審查**(規則檢查與 AI 都沒有跑):部署端設定為「同一個 MR 只自動審第一次」"
    "(CI_REVIEW_REPEAT=first)。上一次審查(`{since}`)的結論是「{previous}」,不代表之後的改動。"
    "合併前請有合併權限的人從 `{since}` 開始看過之後的改動。要重新審查:對這個 MR 最新的 pipeline 的審查 job"
    "按 retry(或對同一個 commit 再跑一次 pipeline)。")
_PREVIOUS_TEXT = {EXIT_APPROVED: "自動放行", EXIT_NEEDS_HUMAN: "交人工確認"}


def skipped_note(previous: int, reviewed_sha: str) -> str:
    """跳過時的留言。reviewed_sha 是上一次真的審的 commit(連續跳過好幾次也不變)。"""
    short = reviewed_sha[:8] if isinstance(reviewed_sha, str) and reviewed_sha else "(不明)"
    return SKIPPED_NOTE.format(previous=_PREVIOUS_TEXT.get(previous, "交人工確認"), since=short)


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
