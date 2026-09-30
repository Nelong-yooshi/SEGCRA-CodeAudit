"""GitLab 打包下載(toolbox/gitlab.py 的 download_archive,#15 第 2 階段)。

不連網:以假的 httpx.stream 取代,記錄實際送出的請求。重點是
權限(只用唯讀 token)、輸入(只接受完整 sha 與合法子目錄)、大小與時間上限、
不跟隨轉址、錯誤訊息不外洩 token,以及這個函式**不會**被當成模型可呼叫的工具。
"""
import httpx
import pytest

from toolbox import gitlab

SHA = "0123456789abcdef0123456789abcdef01234567"
READ_TOKEN = "read-token-SECRET-123"
WRITE_TOKEN = "write-token-SECRET-456"


class _FakeResponse:
    def __init__(self, status=200, chunks=(b"data",), headers=None, raise_in_iter=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)
        self._raise = raise_in_iter
        self.consumed = 0

    def iter_raw(self):
        for c in self._chunks:
            if self._raise:
                raise self._raise
            self.consumed += 1
            yield c

    def iter_bytes(self):                       # 若有人改用會自動解壓的讀法,測試要抓到
        raise AssertionError("必須用 iter_raw() 讀原始位元組")


class _Recorder:
    """記錄 httpx.stream 的呼叫參數,回傳預先準備好的假回應。"""

    def __init__(self, response=None, exc=None):
        self.calls = []
        self.response = response or _FakeResponse()
        self.exc = exc

    def __call__(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.exc:
            raise self.exc
        rec = self

        class _Ctx:
            def __enter__(self):
                return rec.response

            def __exit__(self, *a):
                return False

        return _Ctx()


@pytest.fixture
def real_env(monkeypatch):
    monkeypatch.setattr(gitlab, "GITLAB_URL", "https://gitlab.test")
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", "42")
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", WRITE_TOKEN)
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", READ_TOKEN)


@pytest.fixture
def fake_http(monkeypatch):
    def _install(**kw):
        rec = _Recorder(**kw)
        monkeypatch.setattr(httpx, "stream", rec)
        return rec
    return _install


def _err(excinfo):
    msg = str(excinfo.value)
    for secret in (READ_TOKEN, WRITE_TOKEN, "gitlab.test"):
        assert secret not in msg, msg
    return msg


# ------------------------------------------------------------------ 正常情況
def test_downloads_with_read_token_only(real_env, fake_http):
    rec = fake_http(response=_FakeResponse(chunks=[b"ab", b"cd"]))
    assert gitlab.download_archive(SHA, "dbt", max_bytes=100) == b"abcd"
    [call] = rec.calls
    assert call["method"] == "GET"
    assert call["url"] == "https://gitlab.test/api/v4/projects/42/repository/archive.tar.gz"
    assert call["params"] == {"sha": SHA, "path": "dbt"}
    assert call["headers"]["PRIVATE-TOKEN"] == READ_TOKEN        # 唯讀那把
    assert WRITE_TOKEN not in str(call)                            # 有寫入權的那把不能出現
    assert call["headers"]["Accept-Encoding"] == "identity"
    assert call["follow_redirects"] is False
    assert call["trust_env"] is False                  # 不讀 HTTP_PROXY 等:token 不送往代理
    assert call["timeout"]


def test_empty_path_downloads_whole_repo_without_path_param(real_env, fake_http):
    rec = fake_http()
    gitlab.download_archive(SHA, max_bytes=100)
    assert rec.calls[0]["params"] == {"sha": SHA}


def test_sha256_commit_accepted(real_env, fake_http):
    fake_http()
    assert gitlab.download_archive("a" * 64, max_bytes=100) == b"data"


# ------------------------------------------------------------------ 權限
@pytest.mark.parametrize("missing", ["GITLAB_URL", "GITLAB_PROJECT", "GITLAB_READ_TOKEN"])
def test_missing_setting_fails_without_request(real_env, fake_http, monkeypatch, missing):
    rec = fake_http()
    monkeypatch.setattr(gitlab, missing, "")
    with pytest.raises(gitlab.ArchiveDownloadError) as e:
        gitlab.download_archive(SHA, max_bytes=100)
    _err(e)
    assert rec.calls == []


def test_never_falls_back_to_write_token(real_env, fake_http, monkeypatch):
    """#15 條件 1:唯讀 token 沒設,就算有寫入權的 GITLAB_TOKEN 在,也不能拿來用。"""
    rec = fake_http()
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", "")
    with pytest.raises(gitlab.ArchiveDownloadError):
        gitlab.download_archive(SHA, max_bytes=100)
    assert rec.calls == []


def test_read_token_identical_to_write_token_is_refused(real_env, fake_http, monkeypatch):
    """兩把設成同一把,「分開」就形同虛設:不下載。"""
    rec = fake_http()
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", WRITE_TOKEN)
    with pytest.raises(gitlab.ArchiveDownloadError, match="同一把") as e:
        gitlab.download_archive(SHA, max_bytes=100)
    _err(e)
    assert rec.calls == []


@pytest.mark.parametrize("url", [
    "https://gitlab.test", "https://gitlab.test:8443/", "http://localhost:8929",
    "http://127.0.0.1:8929", "http://[::1]:8929", "HTTP://LOCALHOST:8929",
])
def test_https_or_loopback_is_accepted(real_env, fake_http, monkeypatch, url):
    fake_http()
    monkeypatch.setattr(gitlab, "GITLAB_URL", url)
    assert gitlab.download_archive(SHA, max_bytes=100) == b"data"


@pytest.mark.parametrize("url", [
    "http://gitlab.test",                    # 明文、不在本機:token 會以明文過網路
    "http://192.0.2.10:8929",                # 內網位址也一樣(RFC 5737 文件專用位址)
    "http://localhost.gitlab.test",          # 看起來像 localhost 的其他主機
    "ftp://gitlab.test",
    "https://user:pass@gitlab.test",         # 網址夾帶帳密
    "gitlab.test",                           # 沒有 scheme
    "https://",
    "http://[::1",                           # 解析失敗
], ids=["http-remote", "http-lan", "fake-localhost", "ftp", "userinfo", "no-scheme",
        "no-host", "broken"])
def test_insecure_or_bad_url_refused_without_request(real_env, fake_http, monkeypatch, url):
    rec = fake_http()
    monkeypatch.setattr(gitlab, "GITLAB_URL", url)
    with pytest.raises(gitlab.ArchiveDownloadError, match="GITLAB_URL") as e:
        gitlab.download_archive(SHA, max_bytes=100)
    msg = _err(e)
    assert "pass" not in msg and url not in msg
    assert rec.calls == []


def test_not_exposed_to_the_model():
    """模型能自己決定下載哪個 commit 的哪個目錄 = MR 內容能操控審查機抓任意程式碼。"""
    from orchestrator.tool_hub import TOOL_GROUPS
    exposed = {fn.__name__ for fns in TOOL_GROUPS.values() for fn in fns}
    assert "download_archive" not in exposed


# ------------------------------------------------------------------ 輸入檢查
@pytest.mark.parametrize("sha", [
    "main", "HEAD", SHA[:7], SHA.upper(), SHA + "0", SHA[:-1] + "g",
    f"{SHA}\n", f" {SHA}", "../" + SHA[3:], "", None, 123,
], ids=["branch", "head", "short", "upper", "long", "nonhex", "newline", "space",
        "dotdot", "empty", "none", "int"])
def test_bad_sha_rejected_without_request(real_env, fake_http, sha):
    """只接受完整 commit 編號(#15 條件 8):分支在審查中途可能被推新 commit。"""
    rec = fake_http()
    with pytest.raises(gitlab.ArchiveDownloadError, match="sha"):
        gitlab.download_archive(sha, max_bytes=100)
    assert rec.calls == []


@pytest.mark.parametrize("path", [
    "../x", "/abs", "a/../b", "a/./b", "a//b", "a/", "a b", "a?x=1", "a%2F..", "a\\b",
    "a\nb", "a#b", "..", ".", None,
])
def test_bad_path_rejected_without_request(real_env, fake_http, path):
    rec = fake_http()
    with pytest.raises(gitlab.ArchiveDownloadError, match="子目錄"):
        gitlab.download_archive(SHA, path, max_bytes=100)
    assert rec.calls == []


@pytest.mark.parametrize("path", ["models", "dbt/models", "my-project_1/dbt.v2"])
def test_good_paths_accepted(real_env, fake_http, path):
    fake_http()
    assert gitlab.download_archive(SHA, path, max_bytes=100) == b"data"


@pytest.mark.parametrize("max_bytes", [0, -1, None, "100", 1.5, True])
def test_bad_max_bytes_rejected(real_env, fake_http, max_bytes):
    rec = fake_http()
    with pytest.raises(gitlab.ArchiveDownloadError, match="上限"):
        gitlab.download_archive(SHA, max_bytes=max_bytes)
    assert rec.calls == []


# ------------------------------------------------------------------ 回應處理
@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 404, 500])
def test_non_200_fails_and_body_is_not_echoed(real_env, fake_http, status):
    """轉址(不跟隨)、權限不足、找不到、伺服器錯誤,一律失敗;不回顯回應內容。"""
    fake_http(response=_FakeResponse(status=status, chunks=[b"BODY-SECRET"]))
    with pytest.raises(gitlab.ArchiveDownloadError) as e:
        gitlab.download_archive(SHA, max_bytes=100)
    msg = _err(e)
    assert str(status) in msg and "BODY-SECRET" not in msg


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "GZIP"])
def test_transport_compression_refused(real_env, fake_http, encoding):
    fake_http(response=_FakeResponse(headers={"Content-Encoding": encoding}))
    with pytest.raises(gitlab.ArchiveDownloadError, match="傳輸層壓縮"):
        gitlab.download_archive(SHA, max_bytes=100)


def test_declared_size_over_limit_fails_before_reading(real_env, fake_http):
    rec = fake_http(response=_FakeResponse(headers={"Content-Length": "101"}))
    with pytest.raises(gitlab.ArchiveDownloadError, match="上限"):
        gitlab.download_archive(SHA, max_bytes=100)
    assert rec.response.consumed == 0


def test_streamed_size_over_limit_stops_early(real_env, fake_http):
    """沒有(或謊報)Content-Length 時,邊讀邊算,一超過就停,不把剩下的讀完。"""
    rec = fake_http(response=_FakeResponse(headers={"Content-Length": "10"},
                                           chunks=[b"x" * 60] * 100))
    with pytest.raises(gitlab.ArchiveDownloadError, match="上限"):
        gitlab.download_archive(SHA, max_bytes=100)
    assert rec.response.consumed == 2


def test_exact_limit_is_accepted(real_env, fake_http):
    fake_http(response=_FakeResponse(chunks=[b"x" * 100]))
    assert len(gitlab.download_archive(SHA, max_bytes=100)) == 100


def test_deadline(real_env, fake_http, monkeypatch):
    """伺服器一點一點慢慢送:整體時間超過上限就停。"""
    clock = iter([0.0] + [gitlab.ARCHIVE_DEADLINE_S + 1.0] * 10)
    monkeypatch.setattr(gitlab.time, "monotonic", lambda: next(clock))
    fake_http(response=_FakeResponse(chunks=[b"x"] * 5))
    with pytest.raises(gitlab.ArchiveDownloadError, match="秒上限"):
        gitlab.download_archive(SHA, max_bytes=100)


@pytest.mark.parametrize("exc", [
    httpx.ConnectError(f"cannot reach http://gitlab.test?token={READ_TOKEN}"),
    httpx.ReadTimeout(f"timeout {READ_TOKEN}"),
    OSError(f"boom {WRITE_TOKEN}"),
])
def test_transport_errors_do_not_leak(real_env, fake_http, exc):
    """httpx 的例外訊息可能帶 URL 甚至參數:只留例外類型。"""
    fake_http(exc=exc)
    with pytest.raises(gitlab.ArchiveDownloadError) as e:
        gitlab.download_archive(SHA, max_bytes=100)
    assert type(exc).__name__ in _err(e)


def test_error_during_streaming_does_not_leak(real_env, fake_http):
    fake_http(response=_FakeResponse(chunks=[b"a", b"b"],
                                     raise_in_iter=httpx.ReadError(f"x {READ_TOKEN}")))
    with pytest.raises(gitlab.ArchiveDownloadError) as e:
        gitlab.download_archive(SHA, max_bytes=100)
    _err(e)
