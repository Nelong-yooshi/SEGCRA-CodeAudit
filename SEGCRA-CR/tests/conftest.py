"""全部測試共用的設定。

冷讀(orchestrator/cold_read.py)在審查流程裡另外呼叫一次模型。其他測試只換掉主審查的
run_agent;冷讀若照常跑,會去連真的模型端點、失敗、記成環節未完整,所有走整條 review_mr 的
測試決策都會跟著變。所以預設把冷讀換成「沒命中」。測冷讀本身的測試要求 real_cold_read,
就不換(模型呼叫由那些測試自己換掉 cold_read.run_agent)。
"""
import pytest

from orchestrator import cold_read, pipeline


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
