"""全部測試共用的設定。

冷讀(orchestrator/cold_read.py)在審查流程裡另外呼叫一次模型。其他測試只換掉主審查的
run_agent;冷讀若照常跑,會去連真的模型端點、失敗、記成環節未完整,所有走整條 review_mr 的
測試決策都會跟著變。所以預設把冷讀換成「沒命中」。測冷讀本身的測試要求 real_cold_read,
就不換(模型呼叫由那些測試自己換掉 cold_read.run_agent)。
"""
import os

import pytest

from orchestrator import cold_read, pipeline

# 部署端用的模型設定檔(跑測試前有設的話)。測 repo 預設值的測試一律讀 repo 那份(下面的 autouse
# fixture 把這兩個變數拿掉);檢查部署端那份能不能用的,另外由 deploy_models_config 拿到這個值
_DEPLOY_ENV = {name: os.environ[name] for name in ("SEGCRA_MODELS_CONFIG", "SEGCRA_DBT_DATABASE")
               if name in os.environ}


@pytest.fixture(autouse=True)
def _repo_config_only(monkeypatch):
    """部署端設了 SEGCRA_MODELS_CONFIG(改了模型、上下文、dbt 接線)、SEGCRA_DBT_DATABASE 時,斷言
    repo 預設值的測試會讀到部署端那份而紅。需要這兩個變數的測試自己 setenv。"""
    monkeypatch.delenv("SEGCRA_MODELS_CONFIG", raising=False)
    monkeypatch.delenv("SEGCRA_DBT_DATABASE", raising=False)


@pytest.fixture
def deploy_env():
    """跑測試前設的 SEGCRA_MODELS_CONFIG、SEGCRA_DBT_DATABASE(有設的才有)。"""
    return dict(_DEPLOY_ENV)


@pytest.fixture(autouse=True)
def _cold_read_not_flagged(request, monkeypatch):
    if "real_cold_read" in request.fixturenames:
        return

    async def not_flagged(cfg, mr, head_texts=None):
        return cold_read.ColdRead(flagged=False)

    monkeypatch.setattr(pipeline, "_cold_read", not_flagged)


@pytest.fixture
def real_cold_read():
    """用真的冷讀流程(組輸入、解析、位置對應)。模型呼叫仍要換掉 cold_read.run_agent。"""
    return cold_read
