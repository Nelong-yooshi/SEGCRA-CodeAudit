"""模型端點與「設了但是空的」環境變數:不符就停,寫出來源或變數名,不寫出值。"""
from pathlib import Path

import pytest

from orchestrator.config import PKG_ROOT, ConfigError, env_int, env_path, env_text, load_config

SECRET_URL = "http://user:s3cret-pass@gateway.internal:4000/v1"


def _models_yaml(tmp_path, endpoint_line):
    src = (PKG_ROOT / "config" / "models.yaml").read_text(encoding="utf-8")
    lines = [endpoint_line if ln.startswith("endpoint:") else ln for ln in src.splitlines()]
    assert any(ln.startswith("endpoint:") for ln in src.splitlines())
    d = tmp_path / "cfg"
    d.mkdir()
    p = d / "models.yaml"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("OLLAMA_URL", "SEGCRA_DBT_DATABASE", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("line", ["endpoint:", "endpoint: ''", "endpoint: '   '",
                                  "endpoint: localhost:11434/v1", "endpoint: ftp://host/v1",
                                  "endpoint: http:///v1"])
def test_設定檔的_endpoint_不是有效的網址就停_寫出檔案(tmp_path, line):
    p = _models_yaml(tmp_path, line)
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert "endpoint" in str(e.value) and str(p) in str(e.value)


def test_設定檔沒有_endpoint_這個鍵時用預設(tmp_path):
    p = _models_yaml(tmp_path, "# endpoint 拿掉了")
    assert load_config(p).endpoint == "http://localhost:11434/v1"


@pytest.mark.parametrize("value", ["localhost:11434/v1", "gateway.internal/v1", "   x   "])
def test_OLLAMA_URL_沒寫_http_就停_寫出變數名(monkeypatch, value):
    monkeypatch.setenv("OLLAMA_URL", value)
    with pytest.raises(ConfigError, match="OLLAMA_URL"):
        load_config()


def test_端點錯誤的訊息不寫出值(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "gateway-s3cret-pass/v1")
    with pytest.raises(ConfigError) as e:
        load_config()
    assert "s3cret" not in str(e.value)


def test_有效的端點照用_前後空白去掉(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", f"  {SECRET_URL}  ")
    assert load_config().endpoint == SECRET_URL


# ─────────── 設了但是空的、路徑、整數 ───────────

@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_文字型_設了但是空的就停(monkeypatch, value):
    monkeypatch.setenv("X_TEXT", value)
    with pytest.raises(ConfigError, match="X_TEXT"):
        env_text("X_TEXT", "d")


def test_文字型_沒設用預設_前後空白去掉(monkeypatch):
    monkeypatch.delenv("X_TEXT", raising=False)
    assert env_text("X_TEXT", "d") == "d"
    monkeypatch.setenv("X_TEXT", "  v ")
    assert env_text("X_TEXT", "d") == "v"


@pytest.mark.parametrize("value", ["", "abc", "0", "70000"])
def test_整數型_空的_不是整數_超出範圍就停_寫出變數名(monkeypatch, value):
    monkeypatch.setenv("X_INT", value)
    with pytest.raises(ConfigError, match="X_INT"):
        env_int("X_INT", 1433, hi=65535)


@pytest.mark.parametrize("value", ["", "  ", "relative/dir", "review_output"])
def test_路徑型_空的或相對路徑就停(monkeypatch, value):
    monkeypatch.setenv("X_PATH", value)
    with pytest.raises(ConfigError, match="X_PATH"):
        env_path("X_PATH", PKG_ROOT / "review_output")


def test_路徑型_絕對路徑照用_沒設用預設(monkeypatch, tmp_path):
    monkeypatch.setenv("X_PATH", str(tmp_path / "out"))
    assert env_path("X_PATH", Path("/nope")) == tmp_path / "out"
    monkeypatch.delenv("X_PATH")
    assert env_path("X_PATH", PKG_ROOT / "review_output") == PKG_ROOT / "review_output"


@pytest.mark.parametrize("name", ["SANDBOX_MSSQL_HOST", "SANDBOX_MSSQL_USER", "SANDBOX_MSSQL_PORT"])
def test_沙盒的連線設定_設了但是空的就停(monkeypatch, name):
    from orchestrator import spec_exec
    monkeypatch.setenv(name, "")
    with pytest.raises(ConfigError, match=name):
        spec_exec._sandbox_params()


def test_知識庫_repo_沒有_GitLab_設定時停_不退到測試用位址(monkeypatch):
    from orchestrator import knowledge_governance as kg
    monkeypatch.delenv("GITLAB_URL", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr(kg, "_load_gitlab_env", lambda: {})
    with pytest.raises(ConfigError) as e:
        kg.KnowledgeRepo()
    assert "GITLAB_URL" in str(e.value) and "GITLAB_TOKEN" in str(e.value)
    monkeypatch.setenv("GITLAB_URL", "")
    with pytest.raises(ConfigError, match="GITLAB_URL"):
        kg.KnowledgeRepo()


def test_知識庫_repo_環境變數沒設時讀_gitlab_env(monkeypatch):
    from orchestrator import knowledge_governance as kg
    monkeypatch.delenv("GITLAB_URL", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr(kg, "_load_gitlab_env",
                        lambda: {"GITLAB_URL": "https://gitlab.example/", "GITLAB_TOKEN": "t"})
    repo = kg.KnowledgeRepo()
    assert repo.url == "https://gitlab.example" and repo.token == "t"


def test_gitlab_的設定錯誤是_ConfigError():
    from toolbox.gitlab import GitLabConfigError
    assert issubclass(GitLabConfigError, ConfigError)
