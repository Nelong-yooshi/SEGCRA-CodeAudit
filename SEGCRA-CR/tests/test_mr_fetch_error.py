"""取不到 MR 時,錯誤訊息要照**失敗的階段**說出原因與處理方式,而且不能帶出 GitLab 的網址。

三個階段能直接拿來判斷的東西不同(見 orchestrator/mr_fetch_error.py):

  1. 還沒拿到 HTTP 回應:例外的型別與代碼(TLS 驗證代碼、`ssl.SSLError.reason`、DNS、errno、
     逾時是連線前還是連上之後)
  2. 拿到回應但不是 2xx:狀態碼(含轉址)
  3. 拿到 200:內容是不是 MR

**能用真的連線就用真的**:本機起 HTTP 伺服器,讓 `review_mr` 真的打過去(連不到的埠、https
打到 http 的埠、轉址、401/403/404/429/5xx、200 但不是 JSON、200 是 JSON 但不是 MR、連上了
卻不回應)。憑證、DNS、主機到不了這幾種要真的憑證或網路環境,就用跟 httpx 一樣的例外鏈
(`httpx.ConnectError` ← `httpcore.ConnectError` ← `ssl` / `socket` / `OSError`),代碼照實際值。
"""
import asyncio
import errno
import http.server
import json
import socket
import ssl
import threading

import httpcore
import httpx
import pytest

from orchestrator import pipeline
from orchestrator.config import load_config
from orchestrator.mr_fetch_error import mr_fetch_reason
from toolbox import gitlab

TOKEN = "zz-token-zz"


# ─────────────────── 共用:真的 GitLab 位址、真的 review_mr ───────────────────

def _real(monkeypatch, tmp_path, url):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", url)
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", TOKEN)
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", TOKEN)
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", "3")
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", tmp_path)


def _review_error(monkeypatch, tmp_path, url) -> str:
    _real(monkeypatch, tmp_path, url)
    with pytest.raises(RuntimeError) as info:
        asyncio.run(pipeline.review_mr(load_config(), "7"))
    msg = str(info.value)
    assert msg.startswith("無法取得 MR 7:"), msg
    assert info.value.__cause__ is None and info.value.__suppress_context__, \
        "例外鏈帶著 httpx 的訊息(含網址),不能跟著進 log"
    return msg


def _no_leak(msg: str, host: str):
    """例外訊息裡會有的東西都不能出現:主機、實際打的 API 路徑、token。
    (訊息自己的固定文字可以寫「改成 http://」,那不是網址。)"""
    for bad in (host, "/api/v4/projects/3", TOKEN):
        assert bad not in msg, (bad, msg)


class _Server:
    """本機的假 GitLab:每個請求都回同一個狀態碼與內容;`hang=True` 收到請求後不回應。"""

    def __init__(self, status=200, body=b"", headers=None, hang=False):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if hang:
                    outer.release.wait(5)
                    return
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.release = threading.Event()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def server():
    made = []

    def make(**kw):
        s = _Server(**kw)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ─────────────────── 1. 還沒拿到 HTTP 回應(真的連線)───────────────────

def test_連不到的埠_說是那個埠沒有服務(monkeypatch, tmp_path):
    msg = _review_error(monkeypatch, tmp_path, f"http://127.0.0.1:{_closed_port()}")
    assert "那個埠沒有服務" in msg
    _no_leak(msg, "127.0.0.1")


def test_https打到http的埠_說是協定不對(monkeypatch, tmp_path, server):
    s = server(status=200, body=b"{}")
    msg = _review_error(monkeypatch, tmp_path, f"https://127.0.0.1:{s.port}")
    assert "回的不是 HTTPS" in msg and "改成 http://" in msg
    assert "憑證" not in msg, "這不是憑證的問題,照信任 CA 去處理沒有用"
    _no_leak(msg, "127.0.0.1")


def test_連上了但一直不回_說是GitLab卡住不是網路(monkeypatch, tmp_path, server):
    real_request = httpx.request
    monkeypatch.setattr(httpx, "request",
                        lambda *a, **k: real_request(*a, **{**k, "timeout": 0.5}))
    s = server(hang=True)
    msg = _review_error(monkeypatch, tmp_path, f"http://127.0.0.1:{s.port}")
    assert "已經連上 GitLab" in msg and "不是網路問題" in msg
    _no_leak(msg, "127.0.0.1")


# ─────────────────── 1. 還沒拿到 HTTP 回應(例外鏈照 httpx 的形狀)───────────────────

def _connect_error(inner: BaseException, outer=httpx.ConnectError) -> BaseException:
    """httpx 的形狀:httpx.X ← httpcore.X ← 底層的 ssl / socket / OSError(本機實測過)。"""
    core_cls = getattr(httpcore, outer.__name__, httpcore.ConnectError)
    core = core_cls(str(inner))
    core.__cause__ = inner
    e = outer(f"{inner} (https://gitlab.internal.example/api/v4/projects/3)")
    e.__cause__ = core
    return e


def _cert_error(code: int, text: str) -> ssl.SSLCertVerificationError:
    e = ssl.SSLCertVerificationError(1, f"[SSL: CERTIFICATE_VERIFY_FAILED] certificate "
                                        f"verify failed: {text} (_ssl.c:1032)")
    e.verify_code, e.verify_message, e.reason = code, text, "CERTIFICATE_VERIFY_FAILED"
    return e


@pytest.mark.parametrize("code,text", [
    (18, "self-signed certificate"),
    (19, "self-signed certificate in certificate chain"),
    (20, "unable to get local issuer certificate"),
])
def test_憑證不受信任_說要設_SSL_CERT_FILE(code, text):
    msg = mr_fetch_reason(_connect_error(_cert_error(code, text)))
    assert "不受信任" in msg and "SSL_CERT_FILE" in msg, msg
    _no_leak(msg, "gitlab.internal.example")


@pytest.mark.parametrize("code,text", [(62, "Hostname mismatch"), (64, "IP address mismatch")])
def test_憑證名稱不符_說GITLAB_URL要寫憑證上的名稱(code, text):
    msg = mr_fetch_reason(_connect_error(_cert_error(code, text)))
    assert "名稱跟 GITLAB_URL 的主機不符" in msg, msg
    assert "SSL_CERT_FILE" not in msg, "信任 CA 解決不了名稱不符"


@pytest.mark.parametrize("code", [9, 10])
def test_憑證過期或還沒生效_說要換憑證(code):
    msg = mr_fetch_reason(_connect_error(_cert_error(code, "certificate has expired")))
    assert "已過期或還沒生效" in msg and "SSL_CERT_FILE" not in msg, msg


def test_其他憑證驗證代碼_說出代碼():
    msg = mr_fetch_reason(_connect_error(_cert_error(23, "certificate revoked")))
    assert "驗證代碼 23" in msg, msg


def test_其他TLS錯誤_說出OpenSSL的原因代碼():
    tls = ssl.SSLError(1, "[SSL: UNSUPPORTED_PROTOCOL] unsupported protocol")
    tls.reason = "UNSUPPORTED_PROTOCOL"
    msg = mr_fetch_reason(_connect_error(tls))
    assert "UNSUPPORTED_PROTOCOL" in msg and "憑證" not in msg, msg


@pytest.mark.parametrize("code,expect", [
    (socket.EAI_NONAME, "查不到位址"),
    (getattr(socket, "EAI_AGAIN", -3), "DNS 沒有回應"),
])
def test_名稱解析失敗_分得出查不到與DNS沒回應(code, expect):
    msg = mr_fetch_reason(_connect_error(socket.gaierror(code, "name resolution failed")))
    assert expect in msg, msg


def test_主機到不了_說是主機關機或容器停了():
    msg = mr_fetch_reason(_connect_error(OSError(errno.EHOSTUNREACH, "No route to host")))
    assert "到不了 GitLab 的主機" in msg and "容器停了" in msg, msg


def test_沒有網路路由():
    msg = mr_fetch_reason(_connect_error(OSError(errno.ENETUNREACH, "Network is unreachable")))
    assert "沒有通往 GitLab 的網路" in msg, msg


def test_連線前逾時與連上之後逾時_說法不同():
    before = mr_fetch_reason(_connect_error(TimeoutError("timed out"), httpx.ConnectTimeout))
    after = mr_fetch_reason(_connect_error(TimeoutError("timed out"), httpx.ReadTimeout))
    assert "還沒連上" in before and "已經連上" in after


# ─────────────────── 2. 拿到回應但不是 2xx(真的連線)───────────────────

@pytest.mark.parametrize("status,expect", [
    (301, "轉址"), (302, "轉址"), (308, "轉址"),
    (401, "token 無效"), (403, "沒有讀取這個專案的權限"), (404, "找不到專案或 MR"),
    (429, "限流"), (400, "請求被拒絕"), (502, "伺服器錯誤(502)"), (503, "伺服器錯誤(503)"),
])
def test_狀態碼各自說出原因(monkeypatch, tmp_path, server, status, expect):
    s = server(status=status, headers={"Location": "https://127.0.0.1/gitlab/"})
    msg = _review_error(monkeypatch, tmp_path, f"http://127.0.0.1:{s.port}")
    assert expect in msg, msg
    _no_leak(msg, "127.0.0.1")


def test_轉址的提示_檢查GITLAB_URL的開頭與路徑(monkeypatch, tmp_path, server):
    s = server(status=301, headers={"Location": "https://127.0.0.1/"})
    msg = _review_error(monkeypatch, tmp_path, f"http://127.0.0.1:{s.port}")
    assert "GITLAB_URL 的開頭(http 或 https)或路徑" in msg, msg


# ─────────────────── 3. 拿到 200,內容不是 MR(真的連線)───────────────────

@pytest.mark.parametrize("body", [
    b"<html><body>Sign in</body></html>",          # 代理或登入頁
    b"",                                            # 空的
])
def test_200但不是JSON_說GITLAB_URL要指到GitLab本身(monkeypatch, tmp_path, server, body):
    s = server(status=200, body=body, headers={"Content-Type": "text/html"})
    msg = _review_error(monkeypatch, tmp_path, f"http://127.0.0.1:{s.port}")
    assert "不是 JSON" in msg and "指到 GitLab 本身" in msg, msg


@pytest.mark.parametrize("payload", [{"message": "ok"}, [], [{"title": "x"}], "x"])
def test_200是JSON但不是MR(monkeypatch, tmp_path, server, payload):
    s = server(status=200, body=json.dumps(payload).encode(),
               headers={"Content-Type": "application/json"})
    msg = _review_error(monkeypatch, tmp_path, f"http://127.0.0.1:{s.port}")
    assert "不是 MR 的資料" in msg and "GITLAB_PROJECT" in msg, msg


@pytest.mark.parametrize("changes", [["new_path"], [{"path": "a.sql"}]])
def test_MR是對的_變更清單不是預期的形狀(monkeypatch, changes):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "_api", lambda method, path, **kw: {"title": "t", "sha": "a"})
    monkeypatch.setattr(gitlab, "_api_pages", lambda path, params=None: (list(changes), False))
    with pytest.raises(gitlab.NotAnMR):
        gitlab.get_mr_diff("7")


# ─────────────────── 我們自己的檢查與分類不到的 ───────────────────

def test_mock模式找不到測試資料_點出三個變數(monkeypatch, tmp_path):
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", tmp_path)
    with pytest.raises(RuntimeError) as info:
        asyncio.run(pipeline.review_mr(load_config(), "987654"))
    msg = str(info.value)
    assert "mock 模式找不到這個編號的測試資料" in msg
    # 部署時漏設其中一個,會悄悄變成 mock 模式:訊息要點出是哪三個變數
    assert all(name in msg for name in ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_PROJECT"))


def test_MR編號不是數字():
    assert mr_fetch_reason(ValueError("MR 編號必須是數字")) == "MR 編號必須是數字"


def test_分類不到的_說無法分類並提示用curl確認():
    msg = mr_fetch_reason(RuntimeError("something odd at https://gitlab.internal.example"))
    assert msg.startswith("無法分類的錯誤(RuntimeError)") and "curl" in msg
    _no_leak(msg, "gitlab.internal.example")


def test_不比對訊息字串_訊息長得像也不算():
    """只看型別與代碼:訊息裡有 CERTIFICATE_VERIFY_FAILED、'401' 的普通例外不會被當成那一種。"""
    msg = mr_fetch_reason(RuntimeError("[SSL: CERTIFICATE_VERIFY_FAILED] Client error '401'"))
    assert msg.startswith("無法分類的錯誤")


def test_取得成功時照常往下走(monkeypatch, tmp_path):
    """改成直接呼叫之後,正常的 MR 不能被誤判成取不到。"""
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("REVIEW_OUTPUT", str(tmp_path))
    report = asyncio.run(pipeline.review_mr(load_config(), "001", dry_run=True))
    assert report["verdict"] == "dry_run"
