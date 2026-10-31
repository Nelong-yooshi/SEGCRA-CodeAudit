"""gitlab 工具模組 — MR 讀取與審查結果回寫,行程內直接呼叫。

兩種模式(以環境變數切換):
- mock(預設):MR 從 fixtures/*.json 讀,post_* 寫到 review_output/,離線 demo 用
- real:設定 GITLAB_URL + GITLAB_TOKEN + GITLAB_PROJECT 後走 GitLab REST API v4

打包下載(download_archive,#15)另外需要 GITLAB_READ_TOKEN(唯讀),見該函式說明。
"""
import json
import contextvars
import os
import re
import time
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(os.environ.get("FIXTURES_DIR", PKG_ROOT / "fixtures"))
OUTPUT_DIR = Path(os.environ.get("REVIEW_OUTPUT", PKG_ROOT / "review_output"))

GITLAB_URL = os.environ.get("GITLAB_URL", "")          # e.g. http://localhost:8929
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "")
GITLAB_PROJECT = os.environ.get("GITLAB_PROJECT", "")  # project id 或 URL-encoded path
REAL_MODE = bool(GITLAB_URL and GITLAB_TOKEN and GITLAB_PROJECT)
# 專案層級的唯讀 token(read_api,角色 Reporter),只給打包下載用(#15 條件 1)
GITLAB_READ_TOKEN = os.environ.get("GITLAB_READ_TOKEN", "")



# --- real mode helpers ---------------------------------------------------

def _api(method: str, path: str, **kwargs):
    import httpx
    r = httpx.request(
        method, f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT}{path}",
        headers={"PRIVATE-TOKEN": GITLAB_TOKEN}, timeout=30, **kwargs)
    r.raise_for_status()
    return r.json()


# 每頁 100 筆(GitLab 上限)→ 最多 5,000 個檔案。一個 MR 動輒超過這個數字,
# 代表拆分 MR 才是正解,不該無上限追下去(那會拖垮審查本身的時效)。
MAX_DIFF_PAGES = 50


def _api_pages(path: str, params: dict | None = None) -> tuple[list, bool]:
    """取回分頁 API 的全部資料。回傳 (資料, 是否因超過頁數上限而截斷)。

    PR #10 review 指出:`get_mr_diff()` 用的 `/merge_requests/:iid/diffs`
    沒有處理分頁,GitLab 這個 API 預設一頁 20 筆(上限 100)——第 21 個檔案
    之後不會被注入掃描、不會進規則預掃、不會被模型審查,diff 行數也不會算
    進去,MR 可能因此被誤判成小改而自動放行。
    """
    import httpx
    items, page = [], 1
    while page and page <= MAX_DIFF_PAGES:
        r = httpx.get(f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT}{path}",
                      headers={"PRIVATE-TOKEN": GITLAB_TOKEN}, timeout=30,
                      params={**(params or {}), "per_page": 100, "page": page})
        r.raise_for_status()
        items.extend(r.json())
        nxt = r.headers.get("X-Next-Page", "").strip()
        page = int(nxt) if nxt else 0
    return items, bool(page)


# GitLab MR 的 diff_refs 三個欄位,名稱照 GitLab API 原樣(post_inline_comment 也用這組)
_DIFF_REF_KEYS = ("base_sha", "head_sha", "start_sha")


# 這次審查釘住的 commit。get_mr_diff 取到 head_sha 時設定,之後 get_file 沒指定
# ref 就用它——**模型看到的要是被審的那一版,不是目標分支的現況**。
# 釘住而不是用分支名,是因為分支在審查中途可能被推新 commit,那會變成「審了 A、
# 驗了 B」而且完全沒有徵兆(同一個理由讓 _SHA 不接受分支名)。
# **用 ContextVar 而不是模組層級的全域變數**:webhook 每個 MR 開一條執行緒同時審
# (scripts/webhook_server.py 的 _trigger),全域變數會讓 A 的 get_file 讀到 B 的 sha
# ——兩份 MR 的內容互相污染,而且報告上看不出來。
# ContextVar 在每條執行緒(以及每個 async task)各有自己的值。
_REVIEW_REF: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "segcra_review_ref", default=None)


def pin_review_ref(sha: str | None) -> None:
    """把這次審查釘在某個 commit 上;None = 解除。

    由 get_mr_diff 自動呼叫。拿不到 head_sha 時會是 None——那時 get_file **不會**
    退回目標分支,而是明講取不到(退回去拿到的是改之前的內容,那正是 #22 原本的
    bug)。
    """
    _REVIEW_REF.set(sha)


def review_ref() -> str | None:
    return _REVIEW_REF.get()


# 取回單一檔案的大小上限。超過就當成「取不到完整內容」交人工,不要讓一個超大的
# 檔案拖住預掃、注入掃描與沙盒(與審查流程的時間上限同一個原則)。
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(1024 * 1024)))


def _read_token() -> str | None:
    """讀程式碼用**唯讀** token。沒設定、或與有寫入權的那把相同,都回 None。

    呼叫端據此標成「取不到完整內容」交人工,**不退回用有寫入權的 GITLAB_TOKEN**。
    拒絕「兩把設成同一把」是因為那等於沒有唯讀隔離,只是看起來有設定——
    打包下載(download_archive)本來就是這個規則,讀檔也照做才一致。
    """
    if not GITLAB_READ_TOKEN or GITLAB_READ_TOKEN == GITLAB_TOKEN:
        return None
    return GITLAB_READ_TOKEN


def _file_at(path: str, ref: str) -> str | None:
    """取某個 commit 上的檔案完整內容;取不到回 None(不編造、不退回別的版本)。

    回 None 的情況:沒有唯讀 token、HTTP 失敗、404、或超過 MAX_FILE_BYTES。
    呼叫端一律把 None 當成「這個檔沒拿到」,不是「這個檔是空的」。
    """
    token = _read_token()
    if not token:
        return None
    import urllib.parse
    import httpx
    try:
        r = httpx.get(
            f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT}/repository/files/"
            f"{urllib.parse.quote(path, safe='')}/raw",
            headers={"PRIVATE-TOKEN": token}, params={"ref": ref}, timeout=30)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    if len(r.content) > MAX_FILE_BYTES:
        return None
    return r.text


def _diff_refs(mr: dict) -> dict:
    """取出 MR 的 base_sha / head_sha / start_sha;拿不到或不是完整 commit 編號的給 None。

    GitLab 在 MR 還沒有 commit、或 diff 尚未算好時 `diff_refs` 是 null;mock 模式的
    fixture 通常也沒有。呼叫端一律要處理 None(不能做前後版本比較就交人工),不可以
    拿別的值代替。"""
    refs = mr.get("diff_refs")
    refs = refs if isinstance(refs, dict) else {}
    return {k: v if isinstance(v := refs.get(k), str) and _SHA.fullmatch(v) else None
            for k in _DIFF_REF_KEYS}


# --- 打包下載(#15)------------------------------------------------------

# 完整的 commit 編號(SHA-1 40 位或 SHA-256 64 位,小寫)。不接受分支名:分支在審查
# 中途可能被推新 commit,抓到的就不是被審的那一版(#15 條件 8)
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# 打包的子目錄:只接受英數與 _ . -,以 / 分段(不接受 ..、開頭的 /、空白、URL 字元)
_ARCHIVE_PATH = re.compile(r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*")
ARCHIVE_DEADLINE_S = 120          # 整次下載的時間上限(防伺服器慢慢送、把審查卡住)
# 常見失敗的處理提示(固定文字)。打包與列目錄 API 要 read_api:實測 read_repository
# 只能讀單檔,打包回 403
_ARCHIVE_STATUS_HINTS = {
    401: "GITLAB_READ_TOKEN 無效、已過期或已撤銷",
    403: "GITLAB_READ_TOKEN 權限不足:需要 read_api,角色至少 Reporter",
    404: "找不到專案或 commit,或 GITLAB_READ_TOKEN 看不到這個專案",
}


class ArchiveDownloadError(Exception):
    """打包下載失敗。訊息是固定文字,不含 token、URL、回應內容。"""


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _check_archive_url(url: str) -> None:
    """本機以外一律要 https;網址裡不可夾帶帳密。訊息不回顯網址。

    README §8 的測試 GitLab 綁在本機 127.0.0.1(或經 SSH tunnel 的 localhost),
    用 http 沒有經過網路;其餘位址的 http 會讓唯讀 token 以明文傳輸。"""
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        raise ArchiveDownloadError("GITLAB_URL 不是合法的網址") from None
    if parts.username is not None or parts.password is not None:
        raise ArchiveDownloadError("GITLAB_URL 不可夾帶帳號密碼")
    if not host:
        raise ArchiveDownloadError("GITLAB_URL 不是合法的網址")
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and host in _LOOPBACK_HOSTS:
        return
    raise ArchiveDownloadError("GITLAB_URL 必須是 https(本機 localhost / 127.0.0.1 除外)")


def download_archive(sha: str, path: str = "", *, max_bytes: int) -> bytes:
    """以 GitLab `/repository/archive.tar.gz` 取回某個 commit 的程式碼(tar.gz 原始位元組)。

    **這不是給模型用的工具**:刻意不放進 orchestrator/tool_hub.py 的 TOOL_GROUPS。
    模型能自己決定下載哪個 commit 的哪個目錄,等於讓 MR 內容操控審查機去抓任意程式碼。

    sha        MR 的 head commit(完整編號,不接受分支名)
    path       只取這個子目錄;空字串 = 整個 repo(dbt 專案就在 repo 根目錄時)
    max_bytes  壓縮檔大小上限,超過就中止(呼叫端傳 archive.MAX_COMPRESSED_BYTES)

    權限:只用 GITLAB_READ_TOKEN(唯讀),**不退回用 GITLAB_TOKEN**——那把有寫入權,
    #15 條件 1 要求兩者分開。缺了、或兩把設成同一把,就失敗,由呼叫端交人工。
    連線:本機以外一律要 https(token 不以明文過網路);不讀 HTTP_PROXY 等環境變數
    (token 不送往代理);不跟隨轉址。
    回傳內容仍是不可信的,必須交給 orchestrator/archive.py 的 extract_archive() 解。
    任何失敗都丟 ArchiveDownloadError,訊息不含 token、URL 或回應內容。
    """
    _check_read_access("打包下載")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ArchiveDownloadError("sha 必須是完整的 commit 編號(40 或 64 位小寫十六進位)")
    if not isinstance(path, str) or (path and (
            not _ARCHIVE_PATH.fullmatch(path)
            or any(p in (".", "..") for p in path.split("/")))):
        raise ArchiveDownloadError("打包的子目錄名稱不合法")
    params = {"sha": sha, **({"path": path} if path else {})}
    data = _read_only_get("repository/archive.tar.gz", params, max_bytes, "壓縮檔")
    if data is None:
        raise ArchiveDownloadError(f"GitLab 回應 HTTP 404({_ARCHIVE_STATUS_HINTS[404]})")
    return data


def read_file_at(sha: str, path: str, *, max_bytes: int) -> str | None:
    """讀某個 commit 的單一檔案(文字)。檔案不存在回 None。

    **這不是給模型用的工具**(理由同 download_archive)。用途是在打包下載整個專案**之前**,
    先讀 dbt_project.yml 知道哪些目錄屬於 dbt:專案就在 repo 根目錄時,MR 只改了 dbt 以外的
    檔案就不必下載整個 repo。權限、連線、大小上限的規則與 download_archive 相同(只用唯讀
    token、不跟隨轉址、不讀代理設定);任何失敗都丟 ArchiveDownloadError。"""
    _check_read_access("讀取檔案")
    if not (isinstance(sha, str) and _SHA.fullmatch(sha)):
        raise ArchiveDownloadError("sha 必須是完整的 commit 編號(40 或 64 位小寫十六進位)")
    if not isinstance(path, str) or not _ARCHIVE_PATH.fullmatch(path) or any(
            p in (".", "..") for p in path.split("/")):
        raise ArchiveDownloadError("檔案路徑不合法")
    from urllib.parse import quote
    data = _read_only_get(f"repository/files/{quote(path, safe='')}/raw", {"ref": sha},
                          max_bytes, "檔案")
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ArchiveDownloadError("檔案不是 UTF-8 文字") from None


def _check_read_access(what: str) -> None:
    if not (GITLAB_URL and GITLAB_PROJECT and GITLAB_READ_TOKEN):
        raise ArchiveDownloadError(
            f"未設定 GITLAB_URL、GITLAB_PROJECT 或 GITLAB_READ_TOKEN,無法{what}")
    if GITLAB_READ_TOKEN == GITLAB_TOKEN:
        raise ArchiveDownloadError(
            f"GITLAB_READ_TOKEN 與 GITLAB_TOKEN 是同一把;{what}必須用另一把唯讀 token")
    _check_archive_url(GITLAB_URL)


def _read_only_get(endpoint: str, params: dict, max_bytes: int, what: str) -> bytes | None:
    """以唯讀 token 對專案的 API 發 GET,回傳原始位元組;HTTP 404 回 None。"""
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ArchiveDownloadError("必須指定正整數的下載大小上限")
    import httpx
    buf = bytearray()
    deadline = time.monotonic() + ARCHIVE_DEADLINE_S
    try:
        with httpx.stream(
                "GET", f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT}/{endpoint}",
                params=params,
                # 要求不做傳輸層壓縮:否則 httpx 會先自動解開,一個小封包可能在我們
                # 計算大小之前就在記憶體裡膨脹(下面也只讀原始位元組)
                headers={"PRIVATE-TOKEN": GITLAB_READ_TOKEN, "Accept-Encoding": "identity"},
                # 不跟隨轉址:自訂的 PRIVATE-TOKEN 標頭可能被帶到別的主機
                follow_redirects=False,
                # 不讀 HTTP_PROXY / HTTPS_PROXY / .netrc 等環境設定:token 不送往代理
                trust_env=False, timeout=30) as r:
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                hint = _ARCHIVE_STATUS_HINTS.get(r.status_code)
                raise ArchiveDownloadError(f"GitLab 回應 HTTP {r.status_code}"
                                           + (f"({hint})" if hint else ""))
            if r.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ArchiveDownloadError("GitLab 回應使用了傳輸層壓縮,拒絕處理")
            declared = r.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise ArchiveDownloadError(f"{what}超過 {max_bytes} 位元組上限")
            for chunk in r.iter_raw():
                buf += chunk
                if len(buf) > max_bytes:
                    raise ArchiveDownloadError(f"{what}超過 {max_bytes} 位元組上限")
                if time.monotonic() > deadline:
                    raise ArchiveDownloadError(f"下載超過 {ARCHIVE_DEADLINE_S} 秒上限")
    except ArchiveDownloadError:
        raise
    except Exception as e:
        # httpx 的例外訊息可能帶 URL;只留類型
        raise ArchiveDownloadError(f"下載失敗({type(e).__name__})") from None
    return bytes(buf)


# --- mock mode helpers ---------------------------------------------------

def _mock_mr(mr_id: str) -> dict:
    f = FIXTURES_DIR / f"mr_{_check_mr_id(mr_id)}.json"
    if not f.exists():
        raise FileNotFoundError(f"fixture 不存在: {f.name};現有: "
                                + ", ".join(p.name for p in FIXTURES_DIR.glob('mr_*.json')))
    return json.loads(f.read_text(encoding="utf-8"))


def _mock_output(mr_id: str, kind: str, payload: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    f = OUTPUT_DIR / f"mr_{mr_id}_review.json"
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {
        "mr_id": mr_id, "inline_comments": [], "summary": None, "labels": [],
        "commit_status": None}
    if kind == "inline":
        data["inline_comments"].append(payload)
    elif kind == "summary":
        data["summary"] = payload
    elif kind == "label":
        data["labels"].append(payload["label"])
    elif kind == "commit_status":
        data["commit_status"] = payload
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _check_mr_id(mr_id) -> str:
    """MR 編號只接受 1–10 位的 ASCII 數字(GitLab 的 MR iid 本來就是整數)。

    它會被接進 API 路徑(`/merge_requests/{mr_id}`)與 fixture 檔名,而 get_mr_diff /
    get_file 是審查模型也能呼叫的工具(#17):`1/../../issues/5` 這類值可以讓同一把
    token 去讀別的 API 端點或別的檔案。不用 isdigit() 單獨判斷:它也接受「²」等 Unicode 數字。"""
    mr_id = str(mr_id)
    if not (mr_id.isascii() and mr_id.isdigit() and len(mr_id) <= 10):
        raise ValueError("MR 編號必須是數字")
    return mr_id


# --- tools ---------------------------------------------------------------

# **釘住版本的動作不在這裡**:這個函式在模型的工具清單裡。審 !7 時模型若被 MR
# 描述引導去呼叫 get_mr_diff("8"),釘住的 sha 就會變成 !8 的,之後 get_file 看到的
# 是別的 MR 的檔案,連補讀規格也會讀錯版本。改由 pipeline 在取完 MR 之後釘
# (orchestrator/pipeline.py 的 review_mr),這個函式本身沒有副作用。
def get_mr_diff(mr_id: str) -> str:
    """取得 MR 的標題、描述、head commit sha 與 diff(逐檔)。

    另外帶 GitLab `diff_refs` 的 `base_sha` / `head_sha` / `start_sha`(欄位名照 GitLab
    原樣),給「依 commit 取回程式碼」的流程用;拿不到時是 None,見 `_diff_refs`。

    real 模式的 `files` 每項多帶 `unreviewable`:GitLab 對過大或被摺疊的檔案
    不會回傳 diff 內容(`too_large`/`collapsed`),這種檔案沒有經過任何審查,
    `unreviewable=True` 讓後面的 `enforce_unreviewable` 能攔下來,而不是
    靜默當成「這個檔案沒問題」。`truncated=True` 代表 MR 的檔案數超過
    `MAX_DIFF_PAGES` 能取到的上限,同樣不得自動放行。
    """
    mr_id = _check_mr_id(mr_id)
    if REAL_MODE:
        mr = _api("GET", f"/merge_requests/{mr_id}")
        changes, truncated = _api_pages(f"/merge_requests/{mr_id}/diffs")
        files = [{"path": c["new_path"], "old_path": c.get("old_path"),
                  "diff": c.get("diff") or "",
                  "deleted": bool(c.get("deleted_file")),
                  "unreviewable": bool(c.get("too_large") or c.get("collapsed"))}
                 for c in changes]
        refs = _diff_refs(mr)
        # **預掃與執行驗證要的是整份新檔,不是 diff 裡那幾行**。大檔小改的 MR,
        # 從 diff 還原出來的 SQL 可能只剩一行,sqlglot 解析不了、沙盒也沒東西可跑
        # ——而 golden case 都有 full_content,所以這條路在評測裡永遠不會被走到,
        # 正式環境卻每次都走(#22)。
        head = refs.get("head_sha")
        for f in files:
            if f["unreviewable"]:
                continue            # 本來就拿不到內容的,不用多打一次 API
            if f["deleted"]:
                continue            # 刪掉的檔在 head 上不存在,沒有新版可抓,不是「取不到」
            if not head:
                # **沒有 head sha 時一樣要標**。GitLab 在 MR 剛建立、diff 還沒算好時
                # `diff_refs` 是 null,而 webhook 往往就在這時候觸發;不標的話這些檔
                # 只有 diff 那幾行進得了預掃與沙盒,卻沒有任何提示。
                f["full_content_unavailable"] = True
                continue
            content = _file_at(f["path"], head)
            if content is not None:
                f["full_content"] = content
            else:
                # 取不到就讓它缺著:下游是 `full_content or _sql_from_diff(...)`,
                # 會退回只用 diff。**不要塞空字串**——那會讓下游以為
                # 「這個檔是空的」而不是「我們沒拿到」。
                f["full_content_unavailable"] = True
        return json.dumps({"title": mr["title"], "description": mr.get("description", ""),
                           "sha": mr.get("sha", ""), **refs, "files": files,
                           "truncated": truncated}, ensure_ascii=False)
    mr = _mock_mr(mr_id)
    return json.dumps({"title": mr["title"], "description": mr.get("description", ""),
                       "sha": mr.get("sha", f"mock-{mr_id}"), **_diff_refs(mr),
                       "files": mr["files"]}, ensure_ascii=False)


# 這個函式的 docstring 會**原樣送給模型**當工具說明(tool_hub.py 用 inspect.getdoc),
# 所以它必須短、而且只講模型需要知道的事;開發用的說明一律寫成 # 註解。
# 曾經把它寫長、還在裡面叫模型「讀規格請傳 ref=SPEC_REF」——模型照做會送出字面的
# "SPEC_REF" 字串,只會拿到 404。
#
# 不指定 ref 時抓「正在審的那一版」(get_mr_diff 釘住的 head sha),而不是目標分支
# 的現況:先前預設 main,模型看到的是**改之前**的內容,看起來正常、實際上在審錯的
# 東西(#22)。
#
# 拿不到釘住的版本時**不退回目標分支**,而是明講取不到——退回去就是 #22 原本的
# bug,而且沒有任何徵兆。
# 取不到檔案的原因:固定代碼 → 給人看的說法。**只有這一份**:代碼在 gitlab.py 判斷,
# 說法也放在這裡,呼叫端(執行驗證、dbt 反查)引用它 —— 分成兩份的話,新增一個代碼
# 要同時改三個地方,漏掉的那邊會在組訊息時丟 KeyError,而那是審查跑到一半的時候。
#
# 說法只寫**哪一類設定**出了問題,不寫環境變數名:這段會貼回 MR,而 MR 上該看到的是
# 「環節 + 問題 + 代碼」;代碼對應哪個設定寫在 docs/10,維運照代碼查(#89 第 3 點)。
UNAVAILABLE_TEXT = {
    "no_read_token": "取檔用的唯讀憑證沒有設定。",      # nosec B105 — 這是錯誤代碼,不是憑證
    "no_review_ref": "這次審查沒有釘住 commit。",
    "not_found": "專案或 commit 找不到(專案編號、權限或 commit 設定有問題)。",
    "too_large": "檔案超過單檔大小上限。",
    "fetch_failed": "向 GitLab 取檔失敗。",
    "not_a_spec": "取回的內容不是規格的格式。",
}

# 給模型看的訊息(get_file 的回傳)。模型拿到的是文字,不是代碼。
_PROSE = {
    # 連給模型的文字也不寫環境變數名:模型可能把它抄進 finding,而 finding 會貼回
    # MR —— 繞一圈還是外流了設定細節。模型需要知道的是「這個檔拿不到」。
    "no_read_token": ("(無法讀取檔案:取檔用的唯讀憑證沒有設定。"
                      "請依 diff 判斷,或交人工確認。)"),      # nosec B105 — 同上
    "no_review_ref": ("(無法取得審查中的版本:這次審查沒有釘住 commit。"
                      "請依 diff 判斷,或交人工確認。)"),
    "not_found": "(無法讀取檔案:專案或 commit 找不到。請依 diff 判斷,或交人工確認。)",
    "too_large": "({path} 超過單檔上限,未取回完整內容。請依 diff 判斷,或交人工確認。)",
    "fetch_failed": "(無法讀取檔案:取檔失敗。請依 diff 判斷,或交人工確認。)",
}
_ABSENT_PROSE = "(repo 內無 {path})"
_MOCK_ABSENT_PROSE = "(mock 模式無 {path} 的完整內容,請依 diff 判斷)"
# GitLab 的 404 分得出是哪一種:檔案不存在才是「確實沒有」,專案或 commit 找不到是
# 環境的問題(權限不足時 GitLab 也回 404 而不是 403)。
_FILE_NOT_FOUND = "file not found"


def fetch_file(path: str, ref: str = "", mr_id: str = "") -> tuple[str | None, str | None]:
    """取檔給**管線**用:回 (內容, 拿不到的代碼)。

    兩個都是 None 代表「這個檔案確實不存在」。

    **為什麼不是回一段文字讓呼叫端去判斷**:檔案的內容與錯誤訊息如果走同一條路回來,
    呼叫端只能比對字串,而那段字串可以由 MR 決定 —— 把規格檔的內容寫成跟錯誤訊息
    一字不差,報告就會說「驗證環境的問題」,維運去查一個沒有壞的設定。原因要跟內容
    分開回,照 HTTP 狀態碼判斷,不要從內容猜。
    """
    if not REAL_MODE:
        mr = _mock_mr(mr_id) if mr_id else None
        for f in (mr or {}).get("files", []):
            if f["path"] == path and "full_content" in f:
                return f["full_content"], None
        return None, None
    import urllib.parse
    import httpx
    ref = ref or review_ref() or ""
    if not ref:
        return None, "no_review_ref"
    token = _read_token()
    if not token:
        return None, "no_read_token"
    try:
        r = httpx.get(
            f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT}/repository/files/"
            f"{urllib.parse.quote(path, safe='')}/raw",
            headers={"PRIVATE-TOKEN": token}, params={"ref": ref}, timeout=30)
        if r.status_code == 404:
            # 只有「檔案不存在」算確實沒有;專案/commit 找不到、權限不足都是環境問題
            body = (r.text or "").lower()
            return (None, None) if _FILE_NOT_FOUND in body else (None, "not_found")
        r.raise_for_status()
        if len(r.content) > MAX_FILE_BYTES:
            return None, "too_large"
        return r.text, None
    except Exception:      # noqa: BLE001 — 原始錯誤不帶出去,只回代碼
        return None, "fetch_failed"


def get_file(path: str, ref: str = "", mr_id: str = "") -> str:
    """取得 repo 內檔案的完整內容(看 diff 不夠時用)。**給審查模型的工具**。

    模型拿到的是文字,所以把 `fetch_file` 的代碼轉成說明句。管線自己要分辨
    「拿不到」與「確實沒有」時,直接用 `fetch_file`,不要比對這裡的文字 ——
    那段文字也可能是檔案的內容(見 fetch_file 的說明)。
    """
    text, code = fetch_file(path, ref, mr_id)
    if text is not None:
        return text
    if code is None:
        return (_ABSENT_PROSE if REAL_MODE else _MOCK_ABSENT_PROSE).format(path=path)
    return _PROSE[code].format(path=path)


def get_mr_refs(mr_id: str) -> str:
    """MR 目前的 head 與 `diff_refs`(`base_sha`、`head_sha`、`start_sha`),**不取檔案內容**。

    回寫前用它比對這次審查是不是已經被新的 commit 取代,同一份結果也拿來開行內討論:
    `get_mr_diff` 會連每個檔案的內容一起取,只為了比對 sha 不值得。拿不到 `diff_refs` 時
    三個欄位是 None,見 `_diff_refs`。"""
    mr_id = _check_mr_id(mr_id)
    if REAL_MODE:
        mr = _api("GET", f"/merge_requests/{mr_id}")
        return json.dumps({"sha": mr.get("sha", ""), **_diff_refs(mr)}, ensure_ascii=False)
    mr = _mock_mr(mr_id)
    return json.dumps({"sha": mr.get("sha", f"mock-{mr_id}"), **_diff_refs(mr)},
                      ensure_ascii=False)


def _rejected_by_gitlab(e: Exception) -> bool:
    """GitLab 收到請求、明確拒絕(4xx,例如行號對不上 diff)。這種一定沒有建立任何東西。

    連線中斷、逾時、5xx 都不算:那些情況**不知道 GitLab 有沒有建立**,只是回應沒送到。"""
    import httpx
    return isinstance(e, httpx.HTTPStatusError) and 400 <= e.response.status_code < 500


def post_inline_comment(mr_id: str, file: str, line: int, body: str,
                        severity: str = "info", base_sha: str = "", head_sha: str = "",
                        start_sha: str = "") -> str:
    """在 MR 的特定檔案行號留下審查意見。

    `base_sha`、`head_sha`、`start_sha`(MR 的 `diff_refs`)由呼叫端帶進來,一份審查只取一次
    (見 `get_mr_refs`);沒帶齊時才自己取。

    行內討論被 GitLab 拒絕(4xx)時退成一般留言。連線中斷、逾時、5xx 時**不退**:GitLab 可能
    已經建立了討論、只是回應沒送到,再貼一般留言同一條就出現兩次。這時回傳
    `{"ok": false, "uncertain": true}`,由呼叫端記下來。"""
    if REAL_MODE:
        text = f"**[{severity}]** {body}"
        if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
            # 沒有對應的行號(檔案層的 finding):直接貼一般留言,不拿第 0 行去開行內討論、
            # 再靠 API 失敗分流;開頭不寫 `檔名:0`(看起來像第 0 行)
            _api("POST", f"/merge_requests/{mr_id}/notes",
                 json={"body": f"`{file}`(無對應行號)\n\n{text}"})
            return json.dumps({"ok": True}, ensure_ascii=False)
        note = {"body": f"`{file}:{line}`\n\n{text}"}
        refs = _diff_refs({"diff_refs": {"base_sha": base_sha, "head_sha": head_sha,
                                         "start_sha": start_sha}})
        if not all(refs.values()):
            try:
                refs = _diff_refs(_api("GET", f"/merge_requests/{mr_id}"))
            except Exception:
                refs = {}
        if not refs or not all(refs.values()):
            # 拿不到行內討論的位置:還沒送出任何東西,貼一般留言不會重複
            _api("POST", f"/merge_requests/{mr_id}/notes", json=note)
            return json.dumps({"ok": True}, ensure_ascii=False)
        try:
            _api("POST", f"/merge_requests/{mr_id}/discussions",
                 json={"body": text, "position": {"position_type": "text", "new_path": file,
                                                   "new_line": line, **refs}})
        except Exception as e:
            if not _rejected_by_gitlab(e):
                return json.dumps({"ok": False, "uncertain": True}, ensure_ascii=False)
            # 行號對不上 diff 時退成一般留言,不讓單一 finding 卡住整份審查
            _api("POST", f"/merge_requests/{mr_id}/notes", json=note)
    else:
        _mock_output(mr_id, "inline", {"file": file, "line": line,
                                       "severity": severity, "body": body})
    return json.dumps({"ok": True}, ensure_ascii=False)


def post_summary(mr_id: str, body: str, score: int, verdict: str) -> str:
    """發布審查總結留言(總評 + 分數 + 結論)。"""
    text = f"## 🤖 AI 審查報告\n\n**評分:{score}/100 — {verdict}**\n\n{body}"
    if REAL_MODE:
        _api("POST", f"/merge_requests/{mr_id}/notes", json={"body": text})
    else:
        _mock_output(mr_id, "summary", {"score": score, "verdict": verdict, "body": body})
    return json.dumps({"ok": True}, ensure_ascii=False)


def set_commit_status(sha: str, state: str, name: str = "segcra/review",
                      description: str = "", mr_id: str = "") -> str:
    """回寫 commit status(external pipeline)。state: success | failed | pending。
    CE 的 merge 閘門靠它:專案設定「Pipelines must succeed」後,
    failed 的 commit 無法 merge。"""
    if state not in ("success", "failed", "pending"):
        return json.dumps({"error": "state 必須是 success/failed/pending"}, ensure_ascii=False)
    if REAL_MODE:
        if not sha:
            return json.dumps({"error": "缺 sha,無法回寫 commit status"}, ensure_ascii=False)
        _api("POST", f"/statuses/{sha}",
             json={"state": state, "name": name, "description": description[:250]})
    else:
        _mock_output(mr_id or "unknown", "commit_status",
                     {"sha": sha, "state": state, "name": name,
                      "description": description[:250]})
    return json.dumps({"ok": True, "state": state}, ensure_ascii=False)


def create_rule_mr(file_path: str, content: str, title: str, description: str) -> str:
    """建立 branch + commit + MR(autopin 起草 binding 等流程用)。回傳 MR id。"""
    if REAL_MODE:
        import re as _re
        import time as _time
        branch = "feat/" + _re.sub(r"[^a-z0-9]+", "-",
                                   Path(file_path).stem.lower()) + f"-{int(_time.time())}"
        _api("POST", "/repository/commits", json={
            "branch": branch, "start_branch": "main",
            "commit_message": f"feat: {title}",
            "actions": [{"action": "create", "file_path": file_path, "content": content}]})
        mr = _api("POST", "/merge_requests", json={
            "source_branch": branch, "target_branch": "main",
            "title": title, "description": description})
        return json.dumps({"mr_iid": mr["iid"], "web_url": mr.get("web_url", "")},
                          ensure_ascii=False)
    ids = [int(p.stem.removeprefix("mr_")) for p in FIXTURES_DIR.glob("mr_*.json")
           if p.stem.removeprefix("mr_").isdigit()]
    new_id = f"{(max(ids) + 1 if ids else 1):03d}"
    (FIXTURES_DIR / f"mr_{new_id}.json").write_text(json.dumps({
        "title": title, "description": description,
        "files": [{"path": file_path, "diff": "(generated)", "full_content": content}],
        "_generated": True}, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.dumps({"mr_iid": new_id}, ensure_ascii=False)


def set_label(mr_id: str, label: str) -> str:
    """為 MR 加上標籤(如 ai-review::needs-changes)。"""
    if REAL_MODE:
        _api("PUT", f"/merge_requests/{mr_id}", json={"add_labels": label})
    else:
        _mock_output(mr_id, "label", {"label": label})
    return json.dumps({"ok": True}, ensure_ascii=False)

