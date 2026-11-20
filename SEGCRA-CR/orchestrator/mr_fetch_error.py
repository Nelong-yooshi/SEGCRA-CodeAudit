"""取不到 MR 時,照**失敗的階段**說出原因與處理方式。

取 MR 會在三個階段失敗,每個階段能直接拿來判斷的東西不同:

  1. 還沒拿到 HTTP 回應(名稱解析、連線、TLS):沒有狀態碼,看例外的型別與代碼
     (`ssl.SSLCertVerificationError.verify_code`、`ssl.SSLError.reason`、`socket.gaierror`、
     `OSError.errno`、httpx 的逾時是連線前還是連上之後)。
  2. 拿到回應但不是 2xx:看狀態碼。
  3. 拿到 200:看內容是不是 MR(不是 JSON、或是 JSON 但不是 MR)。

**只看例外的型別與代碼,不比對訊息字串**:訊息的寫法會隨 httpx / OpenSSL / 作業系統的版本
變,而型別與代碼不會。**輸出只有固定的說明文字,不帶例外訊息**:httpx 的訊息常帶完整網址
(GitLab 的主機名稱),會被寫進 log。跟 `gitlab._rejected_by_gitlab`、
`pipeline._model_failure_kind` 同一種做法。
"""
from __future__ import annotations

import errno
import json
import socket
import ssl

import httpx

from toolbox.gitlab import NotAnMR

# OpenSSL 的憑證驗證代碼(X509_V_ERR_*)。分成三種,因為處理方式不同:
#   不受信任 → 執行環境要信任簽發的 CA;名稱不符 → GITLAB_URL 要寫憑證上的名稱;
#   過期 → GitLab 那邊要換憑證(或執行環境的時間錯了)。
_UNTRUSTED = {2, 18, 19, 20, 21}      # 取不到簽發者、自簽、鏈中自簽、找不到本機簽發者、無法驗證簽章
_NAME_MISMATCH = {62, 64}             # 主機名稱不符、IP 不符
_EXPIRED = {9, 10}                    # 還沒生效、已過期

_UNTRUSTED_TEXT = ("HTTPS 憑證不受信任(例如內部 CA 簽發的):執行環境要信任簽發的 CA。"
                   "本程式的 HTTPS 用的是 Python 自帶的 CA 清單、不看系統的,所以在主機上"
                   "信任了也不夠 —— 要把 CA 檔(PEM)放進執行環境,並把環境變數 SSL_CERT_FILE "
                   "指到它(容器要掛進去)")
_NAME_TEXT = ("HTTPS 憑證上的名稱跟 GITLAB_URL 的主機不符:GITLAB_URL 要寫憑證上的那個名稱"
              "(不能用 IP 或別名),或請 GitLab 管理者換一張包含這個名稱的憑證")
_EXPIRED_TEXT = ("HTTPS 憑證已過期或還沒生效:請 GitLab 管理者更新憑證;"
                 "也確認執行環境的系統時間是對的")
# https 打到講 http 的埠時,OpenSSL 回的原因代碼隨版本不同:部署的映像(OpenSSL 3.5)是
# WRONG_VERSION_NUMBER,CI 的環境是 RECORD_LAYER_FAILURE,更舊的版本有 PACKET_LENGTH_TOO_LONG。
_NOT_TLS_REASONS = frozenset({"WRONG_VERSION_NUMBER", "RECORD_LAYER_FAILURE", "PACKET_LENGTH_TOO_LONG"})
_WRONG_VERSION_TEXT = ("GITLAB_URL 寫的是 https,但那個埠回的不是 HTTPS(通常是 http):"
                       "改成 http://,或改成 GitLab 的 https 埠")

_STATUS_TEXT = {
    401: "GitLab 回 401:token 無效或已過期(GITLAB_TOKEN)",
    403: "GitLab 回 403:token 沒有讀取這個專案的權限",
    404: "GitLab 回 404:找不到專案或 MR(確認 GITLAB_PROJECT 與 MR 編號;token 看不到"
         "這個專案時 GitLab 也回 404)",
    429: "GitLab 回 429:請求太頻繁被限流,稍後再試",
}

_NOT_JSON_TEXT = ("GitLab 回了 200,但內容不是 JSON:GITLAB_URL 可能指到網頁或代理的登入頁,"
                  "要指到 GitLab 本身(不要帶 /api/v4 這類路徑)")
_NOT_MR_TEXT = ("GitLab 回了 200 的 JSON,但不是 MR 的資料:確認 GITLAB_URL 指到 GitLab 本身、"
                "GITLAB_PROJECT 是專案的數字 id")


def _chain(exc: BaseException):
    """例外與它的成因(`raise … from` 與處理中又丟出的),由外往內。"""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _first(exc: BaseException, kind):
    return next((e for e in _chain(exc) if isinstance(e, kind)), None)


def _errno_in(exc: BaseException, *codes) -> bool:
    wanted = {c for c in codes if c is not None}
    return any(isinstance(e, OSError) and e.errno in wanted for e in _chain(exc))


def _status_reason(code: int) -> str:
    if 300 <= code < 400:
        return (f"GitLab 回 {code} 轉址:GITLAB_URL 的開頭(http 或 https)或路徑不對,"
                f"改成轉址後的網址(只寫到 GitLab 的位址,不要帶 /api/v4)")
    if code in _STATUS_TEXT:
        return _STATUS_TEXT[code]
    if 500 <= code < 600:
        return f"GitLab 伺服器錯誤({code}),稍後再試;一直這樣就看 GitLab 的狀態"
    return f"GitLab 回 {code}:請求被拒絕(可以用 curl 對同一個 API 確認)"


def _tls_reason(exc: BaseException) -> str | None:
    cert = _first(exc, ssl.SSLCertVerificationError)
    if cert is not None:
        code = getattr(cert, "verify_code", None)
        if code in _UNTRUSTED:
            return _UNTRUSTED_TEXT
        if code in _NAME_MISMATCH:
            return _NAME_TEXT
        if code in _EXPIRED:
            return _EXPIRED_TEXT
        return f"HTTPS 憑證驗證失敗(驗證代碼 {code}):請 GitLab 管理者確認憑證"
    tls = _first(exc, ssl.SSLError)
    if tls is not None:
        if getattr(tls, "reason", None) in _NOT_TLS_REASONS:
            return _WRONG_VERSION_TEXT
        reason = getattr(tls, "reason", None)
        return f"跟 GitLab 建立 HTTPS 連線失敗({reason or type(tls).__name__})"
    return None


def _dns_reason(exc: BaseException) -> str | None:
    dns = _first(exc, socket.gaierror)
    if dns is None:
        return None
    if dns.errno == getattr(socket, "EAI_AGAIN", object()):
        return ("查 GitLab 的主機名稱時 DNS 沒有回應:確認執行環境的 DNS 設定"
                "(容器裡是 /etc/resolv.conf)")
    return ("GitLab 的主機名稱查不到位址:確認 GITLAB_URL 的主機名稱,"
            "以及執行環境查得到它(容器裡的 DNS 與主機上的可能不同)")


def _connect_reason(exc: BaseException) -> str | None:
    if _first(exc, httpx.ConnectTimeout) is not None:
        return "連線 GitLab 逾時(還沒連上):確認網路、防火牆與 GITLAB_URL 的主機和埠"
    if _first(exc, httpx.ReadTimeout) is not None:
        return ("已經連上 GitLab,但它一直沒有回應:GitLab 本身忙碌或卡住"
                "(看 GitLab 的狀態),不是網路問題")
    if _first(exc, httpx.TimeoutException) is not None:
        return "跟 GitLab 的連線逾時"
    if _errno_in(exc, errno.ECONNREFUSED):
        return ("連到了 GitLab 的主機,但那個埠沒有服務:確認 GITLAB_URL 的埠,"
                "以及 GitLab 有沒有在跑")
    if _errno_in(exc, errno.EHOSTUNREACH, getattr(errno, "WSAEHOSTUNREACH", None)):
        return ("到不了 GitLab 的主機:主機關機、GitLab 的容器停了,或中間的路由不通")
    if _errno_in(exc, errno.ENETUNREACH, getattr(errno, "WSAENETUNREACH", None)):
        return "執行環境沒有通往 GitLab 的網路:確認網路設定與防火牆"
    if _errno_in(exc, errno.ETIMEDOUT):
        return "連線 GitLab 逾時(還沒連上):確認網路、防火牆與 GITLAB_URL 的主機和埠"
    return None


def mr_fetch_reason(exc: BaseException) -> str:
    """取 MR 時丟出的例外 → 看得懂、可以照著處理的原因。固定文字,不帶例外訊息。"""
    # 2. 拿到回應但不是 2xx
    status = _first(exc, httpx.HTTPStatusError)
    if status is not None:
        return _status_reason(status.response.status_code)
    # 1. 還沒拿到回應:TLS 排在連線前面(TLS 失敗在 httpx 也是 ConnectError)
    for check in (_tls_reason, _dns_reason, _connect_reason):
        reason = check(exc)
        if reason:
            return reason
    # 3. 拿到 200,內容不是 MR
    if _first(exc, NotAnMR) is not None:
        return _NOT_MR_TEXT
    if _first(exc, json.JSONDecodeError) is not None:
        return _NOT_JSON_TEXT
    # 我們自己的檢查(mock 模式與 MR 編號)
    if isinstance(exc, FileNotFoundError):
        return ("mock 模式找不到這個編號的測試資料(要連 GitLab 時,GITLAB_URL、"
                "GITLAB_TOKEN、GITLAB_PROJECT 三個都要設)")
    if isinstance(exc, ValueError) and str(exc) == "MR 編號必須是數字":
        return "MR 編號必須是數字"
    return (f"無法分類的錯誤({type(exc).__name__}):可以用 curl 對同一個 API "
            f"(GITLAB_URL/api/v4/projects/<專案>/merge_requests/<編號>)確認")
