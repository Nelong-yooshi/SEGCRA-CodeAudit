"""部署端那份模型設定檔(跑測試前設了 SEGCRA_MODELS_CONFIG 才檢查):載入得過、每個 profile 的
reasoning_effort 是端點認得的值。

其他測試斷言的是 repo 的預設值,一律讀 repo 那份(tests/conftest.py 的 autouse fixture 把這兩個
變數拿掉);部署端那份另外在這裡檢查。
"""
import pytest

from orchestrator.config import _EFFORT_LEVELS, load_config


def test_部署端的設定檔載入得過_reasoning_effort_合法(monkeypatch, deploy_env):
    if "SEGCRA_MODELS_CONFIG" not in deploy_env:
        pytest.skip("跑測試前沒有設 SEGCRA_MODELS_CONFIG")
    for name, value in deploy_env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)
    cfg = load_config()
    for name in cfg.profiles:
        effort = cfg.profile(name).reasoning_effort
        assert effort is None or effort in _EFFORT_LEVELS, (name, effort)
