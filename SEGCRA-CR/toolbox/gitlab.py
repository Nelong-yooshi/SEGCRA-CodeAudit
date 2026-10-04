"""gitlab 工具模組 — MR 讀取與審查結果回寫,行程內直接呼叫。

兩種模式(以環境變數切換):
- mock(預設):MR 從 fixtures/*.json 讀,post_* 寫到 review_output/,離線 demo 用
- real:設定 GITLAB_URL + GITLAB_TOKEN + GITLAB_PROJECT 後走 GitLab REST API v4

打包下載(download_archive,#15)另外需要 GITLAB_READ_TOKEN(唯讀),見該函式說明。
"""
import json
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
    if not (GITLAB_URL and GITLAB_PROJECT and GITLAB_READ_TOKEN):
        raise ArchiveDownloadError(
            "未設定 GITLAB_URL、GITLAB_PROJECT 或 GITLAB_READ_TOKEN,無法打包下載")
    if GITLAB_READ_TOKEN == GITLAB_TOKEN:
        raise ArchiveDownloadError(
            "GITLAB_READ_TOKEN 與 GITLAB_TOKEN 是同一把;打包下載必須用另一把唯讀 token")
    _check_archive_url(GITLAB_URL)
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ArchiveDownloadError("sha 必須是完整的 commit 編號(40 或 64 位小寫十六進位)")
    if not isinstance(path, str) or (path and (
            not _ARCHIVE_PATH.fullmatch(path)
            or any(p in (".", "..") for p in path.split("/")))):
        raise ArchiveDownloadError("打包的子目錄名稱不合法")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ArchiveDownloadError("必須指定正整數的下載大小上限")
    params = {"sha": sha, **({"path": path} if path else {})}
    import httpx
    buf = bytearray()
    deadline = time.monotonic() + ARCHIVE_DEADLINE_S
    try:
        with httpx.stream(
                "GET", f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT}/repository/archive.tar.gz",
                params=params,
                # 要求不做傳輸層壓縮:否則 httpx 會先自動解開,一個小封包可能在我們
                # 計算大小之前就在記憶體裡膨脹(下面也只讀原始位元組)
                headers={"PRIVATE-TOKEN": GITLAB_READ_TOKEN, "Accept-Encoding": "identity"},
                # 不跟隨轉址:自訂的 PRIVATE-TOKEN 標頭可能被帶到別的主機
                follow_redirects=False,
                # 不讀 HTTP_PROXY / HTTPS_PROXY / .netrc 等環境設定:token 不送往代理
                trust_env=False, timeout=30) as r:
            if r.status_code != 200:
                hint = _ARCHIVE_STATUS_HINTS.get(r.status_code)
                raise ArchiveDownloadError(f"GitLab 回應 HTTP {r.status_code}"
                                           + (f"({hint})" if hint else ""))
            if r.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ArchiveDownloadError("GitLab 回應使用了傳輸層壓縮,拒絕處理")
            declared = r.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise ArchiveDownloadError(f"壓縮檔超過 {max_bytes} 位元組上限")
            for chunk in r.iter_raw():
                buf += chunk
                if len(buf) > max_bytes:
                    raise ArchiveDownloadError(f"壓縮檔超過 {max_bytes} 位元組上限")
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
    f = FIXTURES_DIR / f"mr_{mr_id}.json"
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


# --- tools ---------------------------------------------------------------

def get_mr_diff(mr_id: str) -> str:
    """取得 MR 的標題、描述、head commit sha 與 diff(逐檔)。

    real 模式的 `files` 每項多帶 `unreviewable`:GitLab 對過大或被摺疊的檔案
    不會回傳 diff 內容(`too_large`/`collapsed`),這種檔案沒有經過任何審查,
    `unreviewable=True` 讓後面的 `enforce_unreviewable` 能攔下來,而不是
    靜默當成「這個檔案沒問題」。`truncated=True` 代表 MR 的檔案數超過
    `MAX_DIFF_PAGES` 能取到的上限,同樣不得自動放行。
    """
    if REAL_MODE:
        mr = _api("GET", f"/merge_requests/{mr_id}")
        changes, truncated = _api_pages(f"/merge_requests/{mr_id}/diffs")
        files = [{"path": c["new_path"], "old_path": c.get("old_path"),
                  "diff": c.get("diff") or "",
                  "unreviewable": bool(c.get("too_large") or c.get("collapsed"))}
                 for c in changes]
        return json.dumps({"title": mr["title"], "description": mr.get("description", ""),
                           "sha": mr.get("sha", ""), "files": files,
                           "truncated": truncated}, ensure_ascii=False)
    mr = _mock_mr(mr_id)
    return json.dumps({"title": mr["title"], "description": mr.get("description", ""),
                       "sha": mr.get("sha", f"mock-{mr_id}"), "files": mr["files"]},
                      ensure_ascii=False)


def get_file(path: str, ref: str = "main", mr_id: str = "") -> str:
    """取得 repo 內檔案完整內容(如 specs/R-xxx.md;看 diff 不夠時也可用)。
    mock 模式下 ref 無作用。"""
    if REAL_MODE:
        import urllib.parse
        import httpx
        r = httpx.get(
            f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT}/repository/files/"
            f"{urllib.parse.quote(path, safe='')}/raw",
            headers={"PRIVATE-TOKEN": GITLAB_TOKEN}, params={"ref": ref}, timeout=30)
        if r.status_code == 404:
            return f"(repo 內無 {path})"
        r.raise_for_status()
        return r.text
    mr = _mock_mr(mr_id) if mr_id else None
    if mr:
        for f in mr["files"]:
            if f["path"] == path and "full_content" in f:
                return f["full_content"]
    return f"(mock 模式無 {path} 的完整內容,請依 diff 判斷)"


def post_inline_comment(mr_id: str, file: str, line: int, body: str,
                        severity: str = "info") -> str:
    """在 MR 的特定檔案行號留下審查意見。"""
    if REAL_MODE:
        text = f"**[{severity}]** {body}"
        try:
            mr = _api("GET", f"/merge_requests/{mr_id}")
            pos = {"position_type": "text", "new_path": file, "new_line": line,
                   **{k: mr["diff_refs"][k] for k in ("base_sha", "head_sha", "start_sha")}}
            _api("POST", f"/merge_requests/{mr_id}/discussions",
                 json={"body": text, "position": pos})
        except Exception:
            # 行號對不上 diff 時退成一般留言,不讓單一 finding 卡住整份審查
            _api("POST", f"/merge_requests/{mr_id}/notes",
                 json={"body": f"`{file}:{line}`\n\n{text}"})
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

