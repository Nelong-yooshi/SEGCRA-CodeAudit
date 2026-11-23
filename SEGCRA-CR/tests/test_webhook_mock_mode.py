"""webhook 服務不是 real 模式就不啟動;要在本機用測試資料跑時用 --mock 明確打開,打開時一直有警告。"""
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

_GITLAB_VARS = ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_PROJECT")


@pytest.fixture()
def ws(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "x")
    import webhook_server as mod
    importlib.reload(mod)
    yield mod
    mod._mock = False


def test_三個_GitLab_設定都沒設時_服務不啟動_訊息寫出三個變數名():
    env = {k: v for k, v in os.environ.items() if k not in _GITLAB_VARS}
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "webhook_server.py"), "--port", "0"],
                       env=env, cwd=ROOT, capture_output=True, text=True, timeout=120,
                       encoding="utf-8", errors="replace")
    assert r.returncode != 0
    for name in _GITLAB_VARS:
        assert name in r.stderr, r.stderr
    assert "--mock" in r.stderr


def test_main_加了_mock_照常啟動():
    env = {k: v for k, v in os.environ.items() if k not in _GITLAB_VARS}
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "webhook_server.py"),
                             "--port", "0", "--host", "127.0.0.1", "--mock"],
                            env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    try:
        seen = []
        for _ in range(20):
            line = proc.stdout.readline()
            if not line:
                break
            seen.append(line)
            if "監聽" in line:
                break
        text = "".join(seen)
        assert "監聽" in text and "real_mode=off(mock)" in text, text
        assert "結果只寫進本機檔案" in text
        assert proc.poll() is None, "加了 --mock 卻沒有在跑"
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_加了_mock_照常_而且啟動時有警告(ws, monkeypatch, capsys):
    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    ws._require_real_mode(True)
    assert ws._mock is True
    assert ws.MOCK_NOTICE in capsys.readouterr().out


def test_沒加_mock_而且不是_real_模式時停(ws, monkeypatch):
    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    with pytest.raises(SystemExit) as e:
        ws._require_real_mode(False)
    assert all(name in str(e.value) for name in _GITLAB_VARS)


def test_real_模式又加了_mock_時停(ws, monkeypatch):
    from toolbox import gitlab
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    with pytest.raises(SystemExit, match="--mock"):
        ws._require_real_mode(True)
    ws._require_real_mode(False)
    assert ws._mock is False


def test_mock_時每一份審查的_log_再提醒一次(ws, monkeypatch, capsys):
    async def fake_review(cfg, mr_id, profile_name=None):
        return {"decision": "needs_human", "score": 70, "findings": []}
    monkeypatch.setattr(ws, "review_mr", fake_review)
    monkeypatch.setattr(ws, "_notify_incomplete", lambda *a, **k: None)
    ws._mock = True
    ws._run_review(7)
    assert f"MR !7 警告:{ws.MOCK_NOTICE}" in capsys.readouterr().out
    ws._mock = False
    ws._run_review(8)
    assert ws.MOCK_NOTICE not in capsys.readouterr().out
