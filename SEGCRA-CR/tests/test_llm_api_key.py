"""模型端點的 API key 用環境變數 `LLM_API_KEY` 給 — 不呼叫 LLM。

要驗證的端點(OpenAI 相容的 vLLM 等)需要真的 key。只能寫在 `config/models.yaml` 的話,key 會放在
共用主機的檔案裡,容易被誤 commit,也會跟著程式碼進離線包。順序是環境變數 > yaml > 預設 `ollama`,
空字串當成沒設定。
"""
import asyncio

import pytest

from orchestrator import agent
from orchestrator.config import PKG_ROOT, Config, load_config

KEY = "sk-test-0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)


def _yaml(tmp_path, api_key_line):
    text = (PKG_ROOT / "config" / "models.yaml").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.startswith("api_key:")]
    if api_key_line is not None:
        lines.insert(0, api_key_line)
    path = tmp_path / "models.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_沒設定時用_models_yaml_的值(tmp_path):
    """用暫存的 yaml,不讀 repo 裡的那份:部署端改過自己的 models.yaml 時,自我檢查不該因此失敗。"""
    assert load_config(_yaml(tmp_path, "api_key: from-yaml")).api_key == "from-yaml"


def test_環境變數覆蓋_models_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    assert load_config().api_key == KEY
    assert load_config(_yaml(tmp_path, "api_key: from-yaml")).api_key == KEY


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_空字串或只有空白當成沒設定(monkeypatch, tmp_path, raw):
    monkeypatch.setenv("LLM_API_KEY", raw)
    assert load_config(_yaml(tmp_path, "api_key: from-yaml")).api_key == "from-yaml"


def test_前後的空白不帶進金鑰(monkeypatch):
    """貼上時多帶的空白會讓端點回 401,看不出是哪裡錯。"""
    monkeypatch.setenv("LLM_API_KEY", f"  {KEY} ")
    assert load_config().api_key == KEY


def test_yaml_沒寫時是預設值(tmp_path):
    assert load_config(_yaml(tmp_path, None)).api_key == "ollama"


def test_金鑰的環境變數不影響端點(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    monkeypatch.setenv("OLLAMA_URL", "http://model-host:8000/v1")
    cfg = load_config()
    assert cfg.endpoint == "http://model-host:8000/v1" and cfg.api_key == KEY


def test_印出設定時不帶金鑰(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    cfg = load_config()
    assert KEY not in repr(cfg) and KEY not in str(cfg)


def test_模型呼叫用的是這把金鑰(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def close(self):
            pass

    async def fake_run(client, *args, **kwargs):
        return "{}"

    monkeypatch.setattr(agent, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(agent, "_run_agent", fake_run)
    cfg = load_config()
    asyncio.run(agent.run_agent(cfg, cfg.profile(None), "s", "u", None, use_tools=False))
    assert seen["api_key"] == KEY


def test_直接建構_Config_時照常可以給金鑰():
    cfg = Config(endpoint="http://x/v1", api_key="k", default_profile="review", profiles={},
                 roles={}, budget={}, policy={})
    assert cfg.api_key == "k" and "api_key" not in repr(cfg)
