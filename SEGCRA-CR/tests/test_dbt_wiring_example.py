"""mock 模式的 dbt 專案來源,與內附的範例 examples/dbt_wiring/。

mock 模式沒有 GitLab 可以打包下載。fixture 目錄若放了某個 commit 的專案
(`<FIXTURES_DIR>/dbt_archives/<commit 編號>/`),接線就從那裡取得——打包成 GitLab 的形狀後,
走與真實模式相同的 load_dbt_project(解壓、路徑檢查、讀設定)。

兩件事要守住:
1. 內附的範例真的跑得起來(不替換任何下載函式),README 寫的結果是真的
2. **沒有放專案的地方行為不變**:預設的 fixtures/ 不會因此開始取回專案;golden set 只有放了
   專案的那幾個 case(eval/golden/dbt_archives/)會取回
"""
import asyncio
import gzip
import io
import json
import pathlib
import tarfile

import pytest

import orchestrator.pipeline as pipeline
from orchestrator import archive, dbt_context, dbt_project
from orchestrator.config import DBT_DATABASE_PLACEHOLDER, load_config
from orchestrator.dbt_affected import IMPACT_TITLE
from orchestrator.dbt_context import (_fixture_archive, _fixture_project_dir, _load_from_fixtures,
                                      prepare_dbt_context)
from orchestrator.dbt_project import DbtProjectError, load_dbt_project
from toolbox import gitlab

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = PKG_ROOT / "examples" / "dbt_wiring" / "fixtures"
BASE, HEAD_MODEL, HEAD_MACRO = "a0" * 20, "b1" * 20, "c2" * 20
CFG = {"enabled": True, "database": DBT_DATABASE_PLACEHOLDER}
LIMIT = archive.MAX_COMPRESSED_BYTES


@pytest.fixture(autouse=True)
def _fresh_cache():
    dbt_project.clear_cache()
    yield
    dbt_project.clear_cache()


@pytest.fixture
def example(monkeypatch, tmp_path):
    """把 fixture 目錄指到內附的範例(等同執行時設 FIXTURES_DIR);輸出寫到暫存目錄。"""
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", EXAMPLE)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("REVIEW_OUTPUT", str(tmp_path))
    assert gitlab.REAL_MODE is False


def _mr(mr_id: str) -> dict:
    return json.loads(gitlab.get_mr_diff(mr_id))


def _one_line(sql: str) -> str:
    return " ".join(sql.split())


# ─────────────────── 1. 內附的範例跑得起來 ───────────────────

def _dry_run(mr_id: str):
    cfg = load_config()
    cfg.dbt = dict(CFG)
    capture = {}
    report = asyncio.run(pipeline.review_mr(cfg, mr_id, dry_run=True, capture=capture))
    return report, capture["prescan_raw"]


def test_範例的檔案齊全():
    for sha in (BASE, HEAD_MODEL, HEAD_MACRO):
        assert (EXAMPLE / "dbt_archives" / sha / "dbt_project.yml").is_file(), sha
    for mr_id, sha in (("801", HEAD_MODEL), ("802", HEAD_MACRO)):
        mr = json.loads((EXAMPLE / f"mr_{mr_id}.json").read_text(encoding="utf-8"))
        assert mr["sha"] == sha and mr["diff_refs"] == {"base_sha": BASE, "head_sha": sha, "start_sha": BASE}
        assert all("full_content" not in f for f in mr["files"]), "範例要跟真實模式一樣只有 diff"


def test_範例_只改model_用完整內容與專案的macro展開(example):
    """diff 只有 `and id > 0` 這一行;送去跑規則的要是展開後的整句。"""
    ctx = prepare_dbt_context(_mr("801"), CFG)
    assert ctx is not None and ctx.error is None and ctx.impact is None
    assert sorted(ctx.macro_files) == ["macros/is_large.sql"]
    rendered = []

    class Hub:
        async def call_json(self, name, args):
            if name == "sqltools__run_rules":
                rendered.append(args["sql"])
            return []
    pre = asyncio.run(pipeline.prescan(Hub(), _mr("801")["files"], CFG, ctx))
    assert "dbt_render_error" not in pre[0]
    assert _one_line(rendered[0]) == (f'select id, amount from "{DBT_DATABASE_PLACEHOLDER}"."dbo"."txn_log" '
                                      "where amount > 1000 and id > 0")


def test_範例_只改model_整條dry_run沒有dbt的補報(example):
    report, pre = _dry_run("801")
    assert "dbt_render_error" not in pre[0] and "parse_error" not in pre[0]
    assert not [f for f in report["findings"] if f["title"] == IMPACT_TITLE]


def test_範例_只改macro_整條dry_run補出受影響的model(example):
    report, pre = _dry_run("802")
    assert pre[0].get("dbt_macro") is True and "parse_error" not in pre[0]
    (fd,) = [f for f in report["findings"] if f["title"] == IMPACT_TITLE]
    assert fd["severity"] == "major"
    assert "macros/is_large.sql" in fd["detail"] and "models/mrt_sample.sql[R003 major(展開後第 1 句)]" in fd["detail"]


def test_範例_反查的結果(example):
    ctx = prepare_dbt_context(_mr("802"), CFG)
    assert ctx.error is None and ctx.base_project is not None
    assert list(ctx.impact.changed_macro_files) == ["macros/is_large.sql"]
    assert [(m.path, m.certain) for m in ctx.impact.affected_models] == [("models/mrt_sample.sql", True)]
    assert not ctx.impact.all_models_possibly_affected and not ctx.impact.uncertain


# ─────────────────── 2. 沒有放專案的地方行為不變 ───────────────────

def test_預設的fixtures沒有dbt專案_既有的MR都不取回():
    default = PKG_ROOT / "fixtures"
    assert gitlab.FIXTURES_DIR == default, "這個測試要看的是預設目錄"
    assert not (default / "dbt_archives").exists()
    names = sorted(p.name for p in default.glob("mr_*.json"))
    assert names, "預設的 fixtures 應該有 MR"
    for name in names:
        mr = json.loads(gitlab.get_mr_diff(name[3:-5]))
        assert prepare_dbt_context(mr, CFG) is None, name


# golden set 裡放了 dbt 專案(eval/golden/dbt_archives/<commit>/)的 case
GOLDEN_WITH_DBT_PROJECT = ["mr_802"]


def test_golden_set只有放了專案的case會取回(monkeypatch):
    """評測時 FIXTURES_DIR 指向 eval/golden(eval/run_eval.py)。那裡的 dbt_archives/ 只放了指定
    case 用的專案;其餘 case 不可以因此開始取回專案(行為與接線前相同)。"""
    golden = PKG_ROOT / "eval" / "golden"
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", golden)
    cases = sorted(golden.glob("mr_*.json"))
    assert cases
    fetched = []
    for case in cases:
        mr = json.loads(gitlab.get_mr_diff(case.stem[3:]))
        ctx = prepare_dbt_context(mr, CFG)
        if ctx is not None:
            assert ctx.error is None, (case.name, ctx.error)
            fetched.append(case.stem)
    assert fetched == GOLDEN_WITH_DBT_PROJECT


def test_golden_set的dbt專案_每個commit目錄都有case用到():
    """放進 dbt_archives/ 卻沒有 case 用到的目錄,只會讓指紋變動、不會被測到。"""
    golden = PKG_ROOT / "eval" / "golden"
    used = set()
    for name in GOLDEN_WITH_DBT_PROJECT:
        mr = json.loads((golden / f"{name}.json").read_text(encoding="utf-8"))
        used |= {mr["sha"], *(mr.get("diff_refs") or {}).values()}
    present = {p.name for p in (golden / "dbt_archives").iterdir() if p.is_dir()}
    assert present and present <= used


def test_預設的fixtures沒有多出MR檔():
    """mock 的 create_rule_mr 用現有 mr_*.json 的最大編號加一當新編號;範例若放進預設目錄,
    自動產生的 MR 編號就會跳號。"""
    ids = sorted(int(p.stem[3:]) for p in (PKG_ROOT / "fixtures").glob("mr_*.json") if p.stem[3:].isdigit())
    assert ids and max(ids) < 100


def test_mock模式_fixture裡沒有那個commit_不取回(example):
    mr = {**_mr("801"), "sha": "d3" * 20}
    assert prepare_dbt_context(mr, CFG) is None


@pytest.mark.parametrize("sha", [None, "", "mock-801", "b1b1", "B1" * 20, 5, ["b1" * 20]])
def test_mock模式_sha不是完整commit編號_不取回(example, sha):
    assert prepare_dbt_context({**_mr("801"), "sha": sha}, CFG) is None


def test_mock模式_沒開dbt_不取回(example):
    for cfg in (None, {}, {"enabled": False, "database": DBT_DATABASE_PLACEHOLDER}):
        assert prepare_dbt_context(_mr("801"), cfg) is None


def test_mock模式_呼叫端自己給load_就用它的(example):
    calls = []

    def load(sha, prefix):
        calls.append(sha)
        return load_dbt_project(sha, prefix, download=_fixture_archive)
    ctx = prepare_dbt_context(_mr("801"), CFG, load=load)
    assert calls == [HEAD_MODEL] and ctx.error is None


def test_真實模式不看fixture目錄(example):
    """真實 GitLab 模式一律打包下載;fixture 目錄剛好有同名的 commit 也不用它。"""
    seen = []

    def load(sha, prefix):
        seen.append(sha)
        return dbt_project.DbtProject(ok=False, error="x")
    ctx = prepare_dbt_context({**_mr("801"), "truncated": False}, CFG, real_mode=True, load=load)
    assert seen == [HEAD_MODEL] and ctx.error == "x"
    # fixture 裡沒有的 commit:真實模式照樣去下載(不會因為 fixture 沒有就當成不適用)
    other = "d3" * 20
    ctx = prepare_dbt_context({**_mr("801"), "sha": other, "head_sha": other, "truncated": False}, CFG,
                              real_mode=True, load=load)
    assert seen == [HEAD_MODEL, other] and ctx is not None and ctx.error == "x"


# ─────────────────── 3. 路徑與上限 ───────────────────

@pytest.mark.parametrize("sha", ["../evil", "..", "a0" * 20 + "/..", "/abs", "a0" * 19 + "..", "", None, 5])
def test_commit編號不合格_到不了檔案系統(monkeypatch, tmp_path, sha):
    (tmp_path / "dbt_archives" / ("a0" * 20)).mkdir(parents=True)
    (tmp_path / "evil").mkdir()
    (tmp_path / "evil" / "dbt_project.yml").write_text("name: evil\n", encoding="utf-8")
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", tmp_path)
    assert _fixture_project_dir(sha) is None
    with pytest.raises(DbtProjectError):
        _fixture_archive(sha, max_bytes=LIMIT)


def test_fixture目錄是檔案不是目錄_不算有專案(monkeypatch, tmp_path):
    (tmp_path / "dbt_archives").mkdir()
    (tmp_path / "dbt_archives" / BASE).write_text("x", encoding="utf-8")
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", tmp_path)
    assert _fixture_project_dir(BASE) is None


def test_打包的形狀與GitLab相同_單一頂層目錄(example):
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(_fixture_archive(BASE, max_bytes=LIMIT)))) as tar:
        names = tar.getnames()
    top = names[0]
    assert "/" not in top and all(n == top or n.startswith(top + "/") for n in names)
    assert f"{top}/dbt_project.yml" in names and f"{top}/macros/is_large.sql" in names


def _nested(tmp_path):
    root = tmp_path / "dbt_archives" / BASE
    for rel, text in {"README.md": "x", "dbt_sub/dbt_project.yml": "name: sub\n",
                      "dbt_sub/models/m.sql": "select 1", "dbt_sub/macros/a.sql": "{% macro a() %}1{% endmacro %}",
                      "dbt_sub_other/models/z.sql": "select 2", "other/x.sql": "select 3"}.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")


def test_指定子目錄_只打包那個目錄_並保留那一層(monkeypatch, tmp_path):
    _nested(tmp_path)
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", tmp_path)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(
            _fixture_archive(BASE, "dbt_sub", max_bytes=LIMIT)))) as tar:
        files = sorted(n.split("/", 1)[1] for n in tar.getnames() if "/" in n)
    assert files == ["dbt_sub/dbt_project.yml", "dbt_sub/macros/a.sql", "dbt_sub/models/m.sql"]
    project = _load_from_fixtures(BASE, "dbt_sub")
    assert project.ok and sorted(project.files) == ["macros/a.sql", "models/m.sql"]


def test_超過大小上限_失敗(example):
    with pytest.raises(DbtProjectError, match="上限"):
        _fixture_archive(BASE, max_bytes=10)
    assert len(_fixture_archive(BASE, max_bytes=LIMIT)) > 10


def test_檔案數上限(monkeypatch, example):
    monkeypatch.setattr(dbt_context, "MAX_FIXTURE_FILES", 2)
    with pytest.raises(DbtProjectError, match="個檔案"):
        _fixture_archive(BASE, max_bytes=LIMIT)


def test_fixture的專案內容有問題_走取回失敗_不是當成沒有(monkeypatch, tmp_path):
    """目錄在、但裡面沒有 dbt_project.yml:要讓人看到「取不回來」,不可以靜靜變成不適用。"""
    (tmp_path / "dbt_archives" / HEAD_MODEL / "models").mkdir(parents=True)
    (tmp_path / "dbt_archives" / HEAD_MODEL / "models" / "m.sql").write_text("select 1", encoding="utf-8")
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", tmp_path)
    mr = {"sha": HEAD_MODEL, "files": [{"path": "models/m.sql", "diff": "+select 1"}]}
    ctx = prepare_dbt_context(mr, CFG)
    assert ctx is not None and ctx.error and "dbt_project.yml" in ctx.error


def test_mock模式的變更清單當成完整的_反查才做得了(example):
    mr = _mr("802")
    assert "truncated" not in mr                        # mock 的 get_mr_diff 沒有這個欄位
    ctx = prepare_dbt_context(mr, CFG)
    assert not ctx.impact.all_models_possibly_affected
    assert "truncated" not in mr                        # 不改到呼叫端的 MR


def test_mock模式_MR明講變更清單不完整_照樣保守處理(example):
    ctx = prepare_dbt_context({**_mr("802"), "truncated": True}, CFG)
    assert ctx.impact.all_models_possibly_affected


def test_base的專案不在fixture裡_反查保守處理(example):
    mr = _mr("802")
    mr["base_sha"] = "d3" * 20
    ctx = prepare_dbt_context(mr, CFG)
    assert ctx.error is None and ctx.impact.all_models_possibly_affected and ctx.base_project is None
