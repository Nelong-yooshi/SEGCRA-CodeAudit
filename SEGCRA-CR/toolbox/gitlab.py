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
GITLAB_PROJECT = os.environ.get("GITLAB_PROJECT", "")  # 專案的數字 id(CI 那條路拿它跟 job 的專案比對)
REAL_MODE = bool(GITLAB_URL and GITLAB_TOKEN and GITLAB_PROJECT)
# 專案層級的唯讀 token(read_api,角色 Reporter),只給打包下載用(#15 條件 1)
GITLAB_READ_TOKEN = os.environ.get("GITLAB_READ_TOKEN", "")



# --- real mode helpers ---------------------------------------------------

class NotAnMR(Exception):
    """GitLab 回了 200 的 JSON,內容卻不是 MR(GITLAB_URL 指到別的服務、代理…)。

    跟「回的不是 JSON」(json.JSONDecodeError)分開:兩者都是 200,但後者多半是網頁或
    登入頁。管線據此說出原因(orchestrator/mr_fetch_error.py),不必比對錯誤訊息。"""


class MockFixtureMissing(FileNotFoundError):
    """mock 模式找不到這個 MR 編號的測試資料。用自己的型別:其他來源的 FileNotFoundError(例如
    SSL_CERT_FILE 指到不存在的檔)不能被說成 mock 模式。繼承 FileNotFoundError,接 FileNotFoundError 的地方照樣接得到。"""


class InvalidMrId(ValueError):
    """MR 編號不是 1–10 位的 ASCII 數字。用型別分辨,不比對訊息。繼承 ValueError。"""


class InsecureGitlabUrl(Exception):
    """GITLAB_URL 不是 https(本機除外),或不是合法的網址、夾帶帳密。訊息是固定文字,不回顯網址。
    取 MR、寫回 GitLab 之前先擋:用 http 的話 token 會以明文送出,而 http 打到 https 的服務
    通常只會等到逾時,看不出是網址寫錯。"""


def check_gitlab_url() -> None:
    """GitLab 的 API 請求送出前檢查 GITLAB_URL(規則同打包下載,見 _url_problem)。"""
    problem = _url_problem(GITLAB_URL)
    if problem:
        raise InsecureGitlabUrl(problem)


class SslCertFileError(Exception):
    """環境變數 SSL_CERT_FILE 有設,但載入不了(不存在、是目錄、不是 PEM)。訊息是固定文字。"""


_SSL_CERT_FILE_OK: set = set()


def check_ssl_cert_file() -> None:
    """SSL_CERT_FILE 有設時先自己載入一次,載入不了就丟 SslCertFileError 說出是這個變數。

    httpx 每個請求都照 SSL_CERT_FILE 建 TLS 設定(GITLAB_URL 是 http 也會),載入失敗時丟的是
    沒有檔名的 FileNotFoundError、IsADirectoryError 或 ssl.SSLError,從那裡反推不出來源。
    同一個值載入成功過就不再試。"""
    raw = os.environ.get("SSL_CERT_FILE")
    if raw is None or raw in _SSL_CERT_FILE_OK:
        return
    import ssl
    if not raw.strip():
        raise SslCertFileError("環境變數 SSL_CERT_FILE 有設定但是空的:要指到 CA 檔(PEM),不用就把它拿掉")
    # 先看路徑本身:檔案不存在時各平台丟的例外不一樣(Linux 是 FileNotFoundError、Windows 是 OSError)
    if not os.path.exists(raw):
        raise SslCertFileError("環境變數 SSL_CERT_FILE 指到的檔案不存在(容器有沒有把 CA 檔掛進去)")
    if os.path.isdir(raw):
        raise SslCertFileError("環境變數 SSL_CERT_FILE 指到的是目錄,不是讀得到的檔案:要指到 CA 檔(PEM)")
    try:
        ssl.create_default_context(cafile=raw)
    except ssl.SSLError:
        raise SslCertFileError("環境變數 SSL_CERT_FILE 指到的檔案不是 PEM 格式的 CA 憑證") from None
    except OSError as e:
        raise SslCertFileError(f"環境變數 SSL_CERT_FILE 指到的不是讀得到的檔案({type(e).__name__},"
                               "例如是目錄或沒有讀取權限)") from None
    _SSL_CERT_FILE_OK.add(raw)


def tls_verify():
    """httpx 的 `verify=`:SSL_CERT_FILE 有設時用它建的 TLS 設定,沒設時 True(httpx 內建的根憑證)。

    給 `trust_env=False` 的請求用(_read_only_get):那種請求不讀任何環境變數,SSL_CERT_FILE
    也不讀,GitLab 是 https、憑證的根不在映像裡時,取 MR 正常、dbt 讀檔卻會憑證驗證失敗。
    載入不了時跟 check_ssl_cert_file 一樣丟 SslCertFileError。"""
    check_ssl_cert_file()
    raw = os.environ.get("SSL_CERT_FILE")
    if raw is None:
        return True
    import ssl
    return ssl.create_default_context(cafile=raw)


def _as_mr(mr):
    """GitLab 回的 MR:不是 MR 的資料就丟 NotAnMR(原本會在後面變成看不出原因的 KeyError)。"""
    if not isinstance(mr, dict) or "title" not in mr:
        raise NotAnMR("MR 的回應不是 MR 的資料")
    return mr


def _as_changes(changes):
    """MR 的變更清單:每一項都要有 new_path,不對就丟 NotAnMR。"""
    if not all(isinstance(c, dict) and "new_path" in c for c in changes):
        raise NotAnMR("MR 的變更清單不是預期的形狀")
    return changes


def _api(method: str, path: str, **kwargs):
    import httpx
    check_gitlab_url()
    check_ssl_cert_file()
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
    check_gitlab_url()
    check_ssl_cert_file()
    items, page = [], 1
    while page and page <= MAX_DIFF_PAGES:
        r = httpx.get(f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT}{path}",
                      headers={"PRIVATE-TOKEN": GITLAB_TOKEN}, timeout=30,
                      params={**(params or {}), "per_page": 100, "page": page})
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            # 每一頁都要是清單:null、數字、dict 都不是(dict 會被 extend 成它的 key 字串)
            raise NotAnMR("分頁的回應不是清單")
        items.extend(data)
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


def _url_problem(url) -> str | None:
    """本機以外一律要 https;網址裡不可夾帶帳密。回傳問題的說法(固定文字,不回顯網址),沒問題回 None。

    README §8 的測試 GitLab 綁在本機 127.0.0.1(或經 SSH tunnel 的 localhost),
    用 http 沒有經過網路;其餘位址的 http 會讓 token 以明文傳輸。"""
    import urllib.parse
    try:
        parts = urllib.parse.urlsplit(url if isinstance(url, str) else "")
        host = (parts.hostname or "").lower()
    except ValueError:
        return "GITLAB_URL 不是合法的網址"
    if parts.username is not None or parts.password is not None:
        return "GITLAB_URL 不可夾帶帳號密碼"
    if not host:
        return "GITLAB_URL 不是合法的網址"
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and host in _LOOPBACK_HOSTS:
        return None
    return "GITLAB_URL 必須是 https(本機 localhost / 127.0.0.1 除外)"


def _check_archive_url(url: str) -> None:
    """打包下載與唯讀取檔前的檢查(規則見 _url_problem)。"""
    problem = _url_problem(url)
    if problem:
        raise ArchiveDownloadError(problem)


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
    try:
        verify = tls_verify()
    except SslCertFileError as e:            # 訊息是固定文字,說得出是哪個變數
        raise ArchiveDownloadError(str(e)) from None
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
                # 不讀 HTTP_PROXY / HTTPS_PROXY / .netrc 等環境設定:token 不送往代理。
                # 不讀環境也就不讀 SSL_CERT_FILE,所以 TLS 設定由 verify 明確傳進去
                trust_env=False, verify=verify, timeout=30) as r:
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
        raise MockFixtureMissing(f"fixture 不存在: {f.name};現有: "
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
        raise InvalidMrId("MR 編號必須是數字")
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
        mr = _as_mr(_api("GET", f"/merge_requests/{mr_id}"))
        changes, truncated = _api_pages(f"/merge_requests/{mr_id}/diffs")
        # GitLab 對沒填說明的 MR 回 `"description": null`(鍵在、值是 null):`.get(鍵, 預設)`
        # 拿到的是 None,後面當字串用就會出錯。標題與 sha 一樣處理
        from orchestrator.text_fields import as_text
        for key in ("title", "description", "sha"):
            mr[key] = as_text(mr.get(key))
        files = [{"path": c["new_path"], "old_path": c.get("old_path"),
                  "diff": c.get("diff") or "",
                  "deleted": bool(c.get("deleted_file")),
                  "unreviewable": bool(c.get("too_large") or c.get("collapsed"))}
                 for c in _as_changes(changes)]
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
# GitLab 的 404 分不出是哪一種:檔案不存在、專案或 commit 找不到、token 沒有權限(GitLab 也回 404)
# 都是 404,代理或別的服務回的 404 頁也是。所以 404 時再確認那個 commit 取得到(回 200),
# 才算「這個檔案確實不存在」;其他一律算取不到。不看內文:內文的寫法由對方決定。


def _commit_exists(ref: str, token: str) -> bool:
    """這個 commit 取得到:回 200,而且內容是 commit 的資料(有 `id`)。

    只看 200 不夠:代理或登入頁也可能回 200 的網頁,那時說「檔案確實不存在」就是猜的。
    解析不了、不是 commit 的資料一律算取不到(丟的例外由 fetch_file 收成 fetch_failed)。"""
    import urllib.parse
    import httpx
    r = httpx.get(f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT}/repository/commits/"
                  f"{urllib.parse.quote(ref, safe='')}",
                  headers={"PRIVATE-TOKEN": token}, timeout=30)
    if r.status_code != 200:
        return False
    data = r.json()
    return isinstance(data, dict) and isinstance(data.get("id"), str) and bool(data["id"])


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
            # 只有 commit 取得到時才算檔案確實不存在;專案/commit 找不到、權限不足都是環境問題
            return (None, None) if _commit_exists(ref, token) else (None, "not_found")
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


# 寫入 GitLab 失敗的種類。寫回的函式不丟例外,失敗時回 {"ok": false, "failure": 種類}:
# 呼叫端(pipeline 的回寫、webhook 的通知)要知道有沒有寫上,而例外訊息可能帶主機位址,
# 不能原樣寫進報告或 log。
WRITE_REJECTED = "rejected"      # GitLab 明確拒絕(4xx):一定沒有寫上
WRITE_UNCERTAIN = "uncertain"    # 連線中斷、逾時、5xx:不知道有沒有寫上
WRITE_NO_SHA = "no_sha"          # 沒有 commit 的 sha,設不了 commit status
WRITE_FAILED = "failed"          # 其他(寫入工具本身出錯、回傳的形狀不對)
WRITE_FAILURES = (WRITE_REJECTED, WRITE_UNCERTAIN, WRITE_NO_SHA, WRITE_FAILED)

# 重送不會重複的寫入(設 label、設 commit status)失敗時的重送:總共送幾次、每次之間等幾秒
LABEL_ATTEMPTS = 2
STATUS_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0


def _write_failed(kind: str) -> str:
    return json.dumps({"ok": False, "failure": kind}, ensure_ascii=False)


def _failure_kind(e: Exception) -> str:
    return WRITE_REJECTED if _rejected_by_gitlab(e) else WRITE_UNCERTAIN


def write_failure(result) -> str | None:
    """寫回函式的結果有沒有寫上:寫上回 None,沒有回失敗的種類(WRITE_FAILURES 之一)。

    不是這幾個函式回的 JSON(例如 ToolHub 把例外轉成的錯誤字串)算 WRITE_FAILED;
    `failure` 不是已知的種類時也是,不把任意字串帶進報告。"""
    try:
        got = json.loads(result)
    except (ValueError, TypeError):
        return WRITE_FAILED
    if not isinstance(got, dict):
        return WRITE_FAILED
    if got.get("ok") is True:
        return None
    return got.get("failure") if got.get("failure") in WRITE_FAILURES else WRITE_FAILED


def _send_with_retry(send, attempts: int) -> str | None:
    """重送不會重複的寫入:失敗就再送,最多 attempts 次。成功回 None,失敗回種類。

    GitLab 明確拒絕(4xx)不重送:再送一樣被拒絕。"""
    attempt = 1
    while True:
        try:
            send()
            return None
        except Exception as e:               # noqa: BLE001 — 種類記下來,例外訊息不往外帶
            kind = _failure_kind(e)
            if kind == WRITE_REJECTED or attempt >= attempts:
                return kind
        attempt += 1
        time.sleep(RETRY_DELAY_SECONDS)


def post_inline_comment(mr_id: str, file: str, line: int, body: str,
                        severity: str = "info", base_sha: str = "", head_sha: str = "",
                        start_sha: str = "") -> str:
    """在 MR 的特定檔案行號留下審查意見。

    `base_sha`、`head_sha`、`start_sha`(MR 的 `diff_refs`)由呼叫端帶進來,一份審查只取一次
    (見 `get_mr_refs`);沒帶齊時才自己取。

    行內討論被 GitLab 拒絕(4xx)時退成一般留言。連線中斷、逾時、5xx 時**不退**:GitLab 可能
    已經建立了討論、只是回應沒送到,再貼一般留言同一條就出現兩次。這時回傳
    `{"ok": false, "failure": "uncertain"}`,由呼叫端記下來。"""
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
                return _write_failed(WRITE_UNCERTAIN)
            # 行號對不上 diff 時退成一般留言,不讓單一 finding 卡住整份審查
            _api("POST", f"/merge_requests/{mr_id}/notes", json=note)
    else:
        _mock_output(mr_id, "inline", {"file": file, "line": line,
                                       "severity": severity, "body": body})
    return json.dumps({"ok": True}, ensure_ascii=False)


def post_summary(mr_id: str, body: str, score: int, verdict: str) -> str:
    """發布審查總結留言(總評 + 分數 + 結論)。

    失敗時回 `{"ok": false, "failure": 種類}`(見 WRITE_FAILURES),**不重送**:逾時、5xx 時
    GitLab 可能已經建立了留言、只是回應沒送到,再送同一份總結就出現兩次。"""
    text = f"## 🤖 AI 審查報告\n\n**評分:{score}/100 — {verdict}**\n\n{body}"
    if REAL_MODE:
        try:
            _api("POST", f"/merge_requests/{mr_id}/notes", json={"body": text})
        except Exception as e:               # noqa: BLE001 — 種類記下來,例外訊息不往外帶
            return _write_failed(_failure_kind(e))
    else:
        _mock_output(mr_id, "summary", {"score": score, "verdict": verdict, "body": body})
    return json.dumps({"ok": True}, ensure_ascii=False)


def set_commit_status(sha: str, state: str, name: str = "segcra/review",
                      description: str = "", mr_id: str = "") -> str:
    """回寫 commit status(external pipeline)。state: success | failed | pending。
    不接 CI 時(webhook 那條路)merge 閘門靠它:專案設定「Pipelines must succeed」後,
    failed 的 commit 無法 merge。

    失敗時重送(同一個 sha 與 name 重送只會更新同一個 status,不會重複),最多
    STATUS_ATTEMPTS 次;最後還是失敗時回 `{"ok": false, "failure": 種類}`。"""
    if state not in ("success", "failed", "pending"):
        return json.dumps({"error": "state 必須是 success/failed/pending"}, ensure_ascii=False)
    if REAL_MODE:
        if not sha:
            return json.dumps({"ok": False, "failure": WRITE_NO_SHA,
                               "error": "缺 sha,無法回寫 commit status"}, ensure_ascii=False)
        failure = _send_with_retry(lambda: _api(
            "POST", f"/statuses/{sha}",
            json={"state": state, "name": name, "description": description[:250]}),
            STATUS_ATTEMPTS)
        if failure is not None:
            return _write_failed(failure)
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


REVIEW_LABEL_PREFIX = "ai-review::"
REVIEW_DECISIONS = ("auto_approved", "needs_human", "blocked")


def _stale_review_labels(label: str) -> str:
    """加上 `ai-review::<決策>` 時要一起拿掉的其他決策標籤(逗號分隔)。不是審查標籤回空字串。

    scoped label 互斥是 GitLab 付費版的功能;沒有授權時舊的會留著,MR 上同時掛著兩個結論,
    看的人分不出哪個是現在的。只列這三種決策:不先查 MR 現有的標籤(少一次請求、少一種失敗),
    代價是舊版程式用過的其他名稱不會被清掉。"""
    if not (isinstance(label, str) and label.startswith(REVIEW_LABEL_PREFIX)):
        return ""
    return ",".join(REVIEW_LABEL_PREFIX + d for d in REVIEW_DECISIONS
                    if REVIEW_LABEL_PREFIX + d != label)


def set_label(mr_id: str, label: str) -> str:
    """為 MR 加上標籤(如 ai-review::needs_human),同一個請求拿掉其他 ai-review:: 決策標籤。

    失敗時重送一次(加上已經有的標籤不會重複、拿掉沒有的標籤不會出錯),最後還是失敗時回
    `{"ok": false, "failure": 種類}`。"""
    stale = _stale_review_labels(label)
    payload = {"add_labels": label, **({"remove_labels": stale} if stale else {})}
    if REAL_MODE:
        failure = _send_with_retry(
            lambda: _api("PUT", f"/merge_requests/{mr_id}", json=payload),
            LABEL_ATTEMPTS)
        if failure is not None:
            return _write_failed(failure)
    else:
        _mock_output(mr_id, "label", {"label": label})
    return json.dumps({"ok": True}, ensure_ascii=False)


# --- CI 閘門用(不在模型的工具清單裡) -------------------------------------
# 審查做成部署端 CI 的一個 job 時(見 orchestrator/ci_gate.py),審查服務要確認呼叫它的是
# 這個專案、這個 MR 的 pipeline 裡正在跑的 job。

class GitLabLookupFailed(Exception):
    """CI 那條路向 GitLab 查 job、pipeline、MR 取不到(連不上、逾時、非 2xx、內容不對)。

    跟「查到了、但不符合」分開:查不到時不能說成 job token 無效或 MR 不是開啟中。底下的例外放在
    `__cause__`,說法由 ci_gate.lookup_failure_text 照型別與狀態碼給(不帶例外原文)。"""

    def __init__(self, what: str):
        super().__init__(what)
        self.what = what


# GET /job 帶的是 job 的 token:GitLab 拒絕這把 token 時回的狀態碼。在測試 GitLab 上量過:亂打的 token
# 與 job 已經結束後的 token 都是 401(執行中是 200)。其他狀態碼(包括 403)沒量到過,算取不到,不說成 token 無效
JOB_TOKEN_REJECTED_STATUS = frozenset({401})


def job_from_token(job_token: str) -> dict | None:
    """用 CI job 的 CI_JOB_TOKEN 向 GitLab 查它是哪一個 job(GET /job,帶 JOB-TOKEN)。

    GitLab 拒絕這把 token(JOB_TOKEN_REJECTED_STATUS)、或 token 是空的 / 太長 / 不是 ASCII 可印字元時
    回 None(確定不符)。
    連不上、其他狀態碼、內容不是物件時丟 GitLabLookupFailed(取不到)。只在 real 模式呼叫。
    token 是呼叫端送來的(不可信),只放在 header,不記 log。"""
    # GitLab 發的 job token 是 ASCII 可印字元;其他的不用問 GitLab 就知道不是(放進 header 也會丟例外)
    if (not isinstance(job_token, str) or not job_token or len(job_token) > 512
            or not job_token.isascii() or not job_token.isprintable()):
        return None
    import httpx
    try:
        check_ssl_cert_file()
        r = httpx.get(f"{GITLAB_URL}/api/v4/job", headers={"JOB-TOKEN": job_token}, timeout=30)
        if r.status_code in JOB_TOKEN_REJECTED_STATUS:
            return None
        r.raise_for_status()
        job = r.json()
        if not isinstance(job, dict):
            raise NotAnMR("GET /job 的回應不是物件")
    except Exception as e:                   # noqa: BLE001 — 取不到,原因由呼叫端照型別說
        raise GitLabLookupFailed("job(用 job token)") from e
    return job


# GITLAB_TOKEN 是哪一個帳號({"id": int, "username": str}),由 learn_own_user 查到後記下;還沒查到是 None。
# 只有這一份:要認「審查自己留的言」的地方都讀 own_user_id(),不各自再查。
_own_user: dict | None = None


def own_user_id() -> int | None:
    """GITLAB_TOKEN 的帳號 id;還沒查到(或查不到)是 None。不打 API。"""
    return _own_user["id"] if _own_user else None


def own_username() -> str:
    return _own_user["username"] if _own_user else ""


def learn_own_user() -> str | None:
    """向 GitLab 查 GITLAB_TOKEN 是哪一個帳號(GET /user),查到記下來回 None;查不到回原因。

    原因是固定文字加上狀態碼或例外型別,不帶例外原文(httpx 的訊息帶完整網址)。分得出
    「token 被拒絕」(重查也不會好,要換 GITLAB_TOKEN)與「連不上」(等 GitLab 起來就好)。
    webhook 服務用它認出審查自己留的言:project / group access token 的帳號名稱由 GitLab 產生,
    不一定是我們取的前綴。"""
    global _own_user
    if not REAL_MODE:
        return "mock 模式,沒有 GitLab"
    import httpx
    try:
        check_ssl_cert_file()
        r = httpx.get(f"{GITLAB_URL}/api/v4/user", headers={"PRIVATE-TOKEN": GITLAB_TOKEN},
                      timeout=10)
    except SslCertFileError as e:            # 訊息是固定文字,寫明是 SSL_CERT_FILE
        return f"GET /user 沒有送出:{e}"
    except Exception as e:                   # noqa: BLE001 — 只留型別,原文帶網址
        return f"GET /user 連不上 GitLab({type(e).__name__}):確認 GITLAB_URL(要寫 http:// 或 https://)與網路"
    if r.status_code in (401, 403):
        return f"GET /user 回 {r.status_code}:GITLAB_TOKEN 被拒絕(token 錯、過期或被撤銷),要換 token"
    if r.status_code != 200:
        return f"GET /user 回 {r.status_code}"
    try:
        user = r.json()
    except Exception:                        # noqa: BLE001 — 不是 JSON(不論編碼)
        user = None
    uid = user.get("id") if isinstance(user, dict) else None
    if not isinstance(uid, int) or isinstance(uid, bool):
        return ("GET /user 回 200,但不是帳號資料(不是 JSON 或沒有數字的 id):GITLAB_URL 可能指到網頁"
                "或代理的登入頁")
    name = user.get("username")
    name = name if isinstance(name, str) and name.isprintable() else ""
    _own_user = {"id": uid, "username": name[:100]}
    return None


def pipeline_info(pipeline_id: int) -> dict:
    """這個專案的某條 pipeline(GET /projects/:id/pipelines/:pipeline_id)。取不到丟 GitLabLookupFailed。"""
    try:
        if not isinstance(pipeline_id, int) or isinstance(pipeline_id, bool):
            raise NotAnMR("pipeline 的 id 不是整數")
        info = _api("GET", f"/pipelines/{pipeline_id}")
        if not isinstance(info, dict):
            raise NotAnMR("pipeline 的回應不是物件")
    except Exception as e:                   # noqa: BLE001
        raise GitLabLookupFailed("job 所在的 pipeline") from e
    return info


def mr_head(mr_id: str) -> dict | None:
    """MR 目前的 head commit 與狀態(GET /merge_requests/:iid):{sha, state, source_project_id}。

    real 模式取不到丟 GitLabLookupFailed;mock 模式沒有這個 MR 的 fixture 時回 None。"""
    mr_id = _check_mr_id(mr_id)
    if not REAL_MODE:
        try:
            mr = _mock_mr(mr_id)
        except FileNotFoundError:            # 沒有這個 MR 的 fixture
            return None
        return {"sha": mr.get("sha", f"mock-{mr_id}"), "state": "opened", "source_project_id": None}
    try:
        mr = _as_mr(_api("GET", f"/merge_requests/{mr_id}"))
    except Exception as e:                   # noqa: BLE001
        raise GitLabLookupFailed("MR") from e
    return {"sha": mr.get("sha") or "", "state": mr.get("state") or "",
            "source_project_id": mr.get("source_project_id")}
