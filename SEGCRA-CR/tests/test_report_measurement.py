"""報告裡給評測量測用的兩個稽核欄位。

  * `_spec_lookup`:這次審查對到的規格代號,跟執行驗證分開記。執行驗證逾時或失敗時沒有
    `_spec_exec`,但規格其實有找到,所以評測判斷「有沒有找到規格」看這個欄位。
  * `_output_meta` 的讀入與生成量:最後一次呼叫的 `prompt_tokens`,以及整份審查每一輪的加總
    (有工具呼叫時每一輪都重讀整串對話,只看最後一次會低估)。

兩個都只由程式寫,模型自己輸出的同名欄位會被蓋掉;不放規格內容。
"""
import asyncio
import json

import pytest

from orchestrator import pipeline
from orchestrator.config import load_config
from orchestrator.dbt_affected import SpecForModels
from toolbox import gitlab

SPEC_TEXT_MARKER = "單日 ATM 現金提領累計"      # specs/R-201.md 的內容(不可以出現在 _spec_lookup)
SQL = "SELECT account_id FROM transactions WHERE amount >= 100000;"


# ─────────────────── _spec_lookup 的組法 ───────────────────

def _spec(code):
    return SpecForModels(code=code, text=f"{code} 的規格內容", models=("models/m.sql",))


def test_MR_對到的規格排第一_受影響_model_的規格接在後面_不重複():
    out = pipeline._spec_lookup("R-201", (_spec("R-300"), _spec("R-201"), _spec("R-400")))
    assert out == {"spec_codes": ["R-201", "R-300", "R-400"]}


def test_只改_macro_時是受影響_model_的規格():
    assert pipeline._spec_lookup(None, (_spec("R-300"),)) == {"spec_codes": ["R-300"]}


@pytest.mark.parametrize("code,affected", [(None, ()), ("", ()), (None, None)])
def test_沒對到規格是空清單(code, affected):
    assert pipeline._spec_lookup(code, affected) == {"spec_codes": []}


def test_只有代號_不放規格內容():
    out = json.dumps(pipeline._spec_lookup("R-201", (_spec("R-300"),)), ensure_ascii=False)
    assert "規格內容" not in out


def test_形狀不對的項目略過():
    assert pipeline._spec_lookup(5, (object(), _spec("R-300"))) == {"spec_codes": ["R-300"]}


# ─────────────────── _output_meta 的讀入與生成量 ───────────────────

def test_讀入與生成量帶進報告():
    meta = {"finish_reason": "stop", "completion_tokens": 300, "prompt_tokens": 4000,
            "prompt_tokens_total": 12000, "completion_tokens_total": 900}
    rec = pipeline._output_meta_record(meta, 8192)
    assert (rec["prompt_tokens"], rec["prompt_tokens_total"], rec["completion_tokens_total"]) == \
        (4000, 12000, 900)
    assert rec["completion_tokens"] == 300


def test_run_agent_沒有回填時是_None():
    rec = pipeline._output_meta_record({"finish_reason": "stop", "completion_tokens": 300}, 8192)
    assert (rec["prompt_tokens"], rec["prompt_tokens_total"], rec["completion_tokens_total"]) == \
        (None, None, None)


# ─────────────────── 走整條 review_mr ───────────────────

@pytest.fixture
def review(monkeypatch, tmp_path):
    fixtures, output = tmp_path / "fixtures", tmp_path / "out"
    fixtures.mkdir()
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))

    def _run(title, *, model_extra=None, usage=None, hang_spec_exec=False, dry_run=False):
        """usage:假的 run_agent 回填進 meta 的值(模擬端點回報的用量)。"""
        (fixtures / "mr_960.json").write_text(json.dumps({
            "title": title, "description": "例行維護", "sha": "e1a7" + "0" * 36,
            "files": [{"path": "sql/maintenance/x.sql", "diff": "@@ -1 +1 @@\n+" + SQL,
                       "full_content": SQL}],
        }, ensure_ascii=False), encoding="utf-8")
        model = {"summary": "x", "score": 100, "findings": [], **(model_extra or {})}

        async def fake_run_agent(cfg, profile, system, user, hub, meta=None, **kw):
            if meta is not None:
                meta.update(usage or {"finish_reason": "stop", "completion_tokens": 10})
            return json.dumps(model, ensure_ascii=False)

        monkeypatch.setattr(pipeline, "run_agent", fake_run_agent)
        if hang_spec_exec:
            async def hangs(*a, **kw):
                await asyncio.sleep(60)
            monkeypatch.setattr(pipeline, "run_spec_exec", hangs)
            monkeypatch.setattr(pipeline, "SPEC_EXEC_TIMEOUT_SECONDS", 1)
        return asyncio.run(pipeline.review_mr(load_config(), "960", dry_run=dry_run))
    return _run


def test_執行驗證逾時時_仍記得對到哪份規格(review):
    from orchestrator.config import PKG_ROOT
    # 下面「不放規格內容」的檢查要有意義,這段文字得真的在規格裡
    assert SPEC_TEXT_MARKER in (PKG_ROOT / "specs" / "R-201.md").read_text(encoding="utf-8")
    report = review("R-201 例行調整", hang_spec_exec=True)
    assert "_spec_exec" not in report                    # 執行驗證沒跑完
    assert report["_spec_lookup"] == {"spec_codes": ["R-201"]}
    assert SPEC_TEXT_MARKER not in json.dumps(report["_spec_lookup"], ensure_ascii=False)


def test_沒對到規格時是空清單(review):
    report = review("例行調整", hang_spec_exec=True)
    assert report["_spec_lookup"] == {"spec_codes": []}


def test_模型自己輸出的同名欄位會被蓋掉(review):
    fake = {"_spec_lookup": {"spec_codes": ["R-999"]}, "_output_meta": {"prompt_tokens": 1}}
    report = review("例行調整", model_extra=fake, hang_spec_exec=True,
                    usage={"finish_reason": "stop", "completion_tokens": 10, "prompt_tokens": 50})
    assert report["_spec_lookup"] == {"spec_codes": []}
    assert report["_output_meta"]["prompt_tokens"] == 50


def test_報告帶出讀入與生成的總量(review):
    meta = {"finish_reason": "stop", "completion_tokens": 10, "prompt_tokens": 50,
            "prompt_tokens_total": 150, "completion_tokens_total": 30}
    report = review("例行調整", usage=meta, hang_spec_exec=True)
    om = report["_output_meta"]
    assert (om["prompt_tokens"], om["prompt_tokens_total"], om["completion_tokens_total"]) == \
        (50, 150, 30)


def test_dry_run_也記對到哪份規格(review):
    report = review("R-201 例行調整", dry_run=True)
    assert report["_spec_lookup"] == {"spec_codes": ["R-201"]}


def test_整條_dry_run_帶上受影響_model_的規格(review, monkeypatch):
    """MR 本身對不到規格(只改 macro)時,review_mr 要把受影響 model 的規格交給 dry-run。"""
    async def no_check(hub, mr, dbt_cfg, dbt_ctx, *, find_spec):
        return None

    monkeypatch.setattr(pipeline, "check_affected_models", no_check)
    monkeypatch.setattr(pipeline, "specs_by_priority",
                        lambda affected, diff: (_spec("R-300"), _spec("R-400")))
    report = review("例行調整", dry_run=True)
    assert report["_spec_lookup"] == {"spec_codes": ["R-300", "R-400"]}


def test_dry_run_帶上受影響_model_的規格():
    class _Hub:
        async def call_json(self, name, args):
            return {}

        async def call(self, name, args):
            return ""

    report = asyncio.run(pipeline._dry_run_report(
        _Hub(), "900", {"files": []}, [], None, None, affected_specs=(_spec("R-300"),)))
    assert report["_spec_lookup"] == {"spec_codes": ["R-300"]}
