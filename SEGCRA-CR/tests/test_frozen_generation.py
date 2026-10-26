"""凍結檔記下測資的生成方式(模型逐筆寫,或程式照結構產生)。

兩種方式的覆蓋與預期值錯誤率要分開比,所以存檔時一定要寫明;加這個欄位之前凍結的檔案
沒有記錄,讀的時候標成「未記錄」,不猜。
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
import freeze_testdata as fz  # noqa: E402
import testdata_cache as tc  # noqa: E402

PLAN = {"schema_ddl": ["CREATE TABLE t (id INT)"], "conditions": [],
        "cases": [{"case_id": "a", "rows": [["t", 1]]}]}


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "CACHE_DIR", tmp_path)


@pytest.mark.parametrize("generation", sorted(tc.GENERATIONS))
def test_存檔記下生成方式(tmp_path, generation):
    tc.save("R-X", "規格", PLAN, [], profile="review", model="m", generation=generation)
    data = json.loads((tmp_path / "R-X.json").read_text(encoding="utf-8"))
    assert data["generation"] == generation
    assert tc.generation_label(data) == tc.GENERATIONS[generation]


def test_沒寫生成方式就不能存(tmp_path):
    with pytest.raises(TypeError):
        tc.save("R-X", "規格", PLAN, [], profile="review", model="m")
    assert not (tmp_path / "R-X.json").exists()


@pytest.mark.parametrize("bad", ["", "A", "model", None])
def test_不認得的生成方式擋下來_不留檔案(tmp_path, bad):
    with pytest.raises(ValueError, match="生成方式"):
        tc.save("R-X", "規格", PLAN, [], profile="review", model="m", generation=bad)
    assert not (tmp_path / "R-X.json").exists()


def test_舊的凍結檔標成未記錄_不猜():
    assert tc.generation_label({"plan": PLAN}) == "未記錄(加這個欄位之前凍結的)"
    assert tc.generation_label({"generation": "zzz"}) == "不認得的值:zzz"


def test_現有的凍結檔讀得出說法():
    data = json.loads((Path(__file__).resolve().parents[1] / "eval" / "testdata_cache"
                       / "R-140.json").read_text(encoding="utf-8"))
    assert tc.generation_label(data)            # 不管有沒有記,都要有一句話,不能丟例外


@pytest.mark.parametrize("plan,expected", [
    (PLAN, "model_cases"),                                   # 現在唯一的方式:plan 不帶
    (dict(PLAN, generation="structure"), "structure"),       # 程式照結構產生的會自己帶
])
def test_凍結腳本把生成方式寫進去(monkeypatch, plan, expected):
    saved = {}

    async def fake_generate(cfg, code, spec_text, **kw):
        return plan, [], []
    monkeypatch.setattr(fz, "generate_cases", fake_generate)
    monkeypatch.setattr(fz, "_reference_check", lambda code, p: (None, None))
    monkeypatch.setattr(tc, "save", lambda *a, **kw: saved.update(kw))
    monkeypatch.setattr(sys, "argv", ["freeze_testdata.py", "R-140"])
    assert asyncio.run(fz.main()) == 0
    assert saved["generation"] == expected
