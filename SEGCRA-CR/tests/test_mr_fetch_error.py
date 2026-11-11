"""取不到 MR 時,錯誤訊息要說出原因,而且不能帶出 GitLab 的網址。

例如 GitLab 用內部 CA 簽發的憑證、執行環境不信任它時,如果只寫「無法取得 MR 1」,
要另外在執行環境裡測才知道是憑證問題。這裡用 httpx 真正的例外
(訊息格式與實際相同)走完整條 review_mr,確認訊息能直接判斷該怎麼處理。
"""
import asyncio

import httpx
import pytest

from orchestrator import pipeline
from orchestrator.config import load_config
from toolbox import gitlab

HOST = "gitlab.internal.example"
URL = f"https://{HOST}/api/v4/projects/3/merge_requests/7"


def _status_error(code: int) -> httpx.HTTPStatusError:
    """用 httpx 自己的 raise_for_status 產生例外,訊息格式才會跟實際一樣。"""
    req = httpx.Request("GET", URL)
    try:
        httpx.Response(code, request=req).raise_for_status()
    except httpx.HTTPStatusError as e:
        return e
    raise AssertionError("raise_for_status 沒有丟例外")


CASES = {
    "憑證": httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "self-signed certificate in certificate chain (_ssl.c:1011)"),
    "401": _status_error(401),
    "403": _status_error(403),
    "404": _status_error(404),
    "5xx": _status_error(502),
    "逾時": httpx.ReadTimeout("timed out"),
    "名稱解析": httpx.ConnectError("[Errno -2] Name or service not known"),
    "連不到": httpx.ConnectError("[Errno 111] Connection refused"),
}

EXPECTED = {
    "憑證": "憑證驗證失敗",
    "401": "401",
    "403": "403",
    "404": "404",
    "5xx": "5xx",
    "逾時": "逾時",
    "名稱解析": "主機名稱解析失敗",
    "連不到": "連不到 GitLab",
}


def _review_with_api_error(monkeypatch, tmp_path, exc):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "GITLAB_URL", f"https://{HOST}")
    monkeypatch.setattr(gitlab, "GITLAB_TOKEN", "t")
    monkeypatch.setattr(gitlab, "GITLAB_READ_TOKEN", "ro")
    monkeypatch.setattr(gitlab, "GITLAB_PROJECT", "3")
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", tmp_path)

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(gitlab, "_api", boom)
    with pytest.raises(RuntimeError) as info:
        asyncio.run(pipeline.review_mr(load_config(), "7"))
    return str(info.value)


@pytest.mark.parametrize("kind", sorted(CASES))
def test_取MR失敗時訊息說出原因(monkeypatch, tmp_path, kind):
    msg = _review_with_api_error(monkeypatch, tmp_path, CASES[kind])
    assert msg.startswith("無法取得 MR 7:")
    assert EXPECTED[kind] in msg, msg


@pytest.mark.parametrize("kind", sorted(CASES))
def test_取MR失敗時訊息不帶出GitLab網址(monkeypatch, tmp_path, kind):
    msg = _review_with_api_error(monkeypatch, tmp_path, CASES[kind])
    assert HOST not in msg and "http" not in msg, msg


def test_憑證錯誤不會被歸成連線錯誤():
    """憑證失敗也是 ConnectError、訊息也像連線問題;要先認出憑證,處理方式才對。"""
    raw = ("Error executing tool gitlab__get_mr_diff: [SSL: CERTIFICATE_VERIFY_FAILED] "
           "certificate verify failed: unable to get local issuer certificate (ConnectError)")
    assert "憑證驗證失敗" in pipeline._mr_fetch_reason(raw)


def test_無法分類的錯誤有提示怎麼查():
    reason = pipeline._mr_fetch_reason("Error executing tool gitlab__get_mr_diff: 奇怪的錯")
    assert "無法分類" in reason and "curl" in reason


def test_回應不是JSON時說明不是MR資料():
    assert pipeline._mr_fetch_reason("(錯誤:未知的工具 gitlab__get_mr_diff)") == "回應不是預期的 MR 資料"


def test_mock模式找不到測試資料(monkeypatch, tmp_path):
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", tmp_path)
    with pytest.raises(RuntimeError) as info:
        asyncio.run(pipeline.review_mr(load_config(), "987654"))
    msg = str(info.value)
    assert "mock 模式找不到這個編號的測試資料" in msg
    # 部署時漏設其中一個,會悄悄變成 mock 模式:訊息要點出是哪三個變數
    assert all(name in msg for name in ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_PROJECT"))


def test_取得成功時照常往下走(monkeypatch, tmp_path):
    """改成自己解析 JSON 之後,正常的 MR 不能被誤判成取不到。"""
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("REVIEW_OUTPUT", str(tmp_path))
    report = asyncio.run(pipeline.review_mr(load_config(), "001", dry_run=True))
    assert report["verdict"] == "dry_run"
