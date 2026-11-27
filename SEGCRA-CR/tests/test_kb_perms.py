"""知識庫與審查判例檔的權限:binding 會壓掉 finding、被駁回的判例會讓同型的 finding 降級或不報,
改得到這些檔的人就能讓 finding 消失。所以讀之前整個目錄樹都檢查(目錄與一路往上、底下每個子目錄
與每個 .md),有沒有設 MEMORY_KB / MEMORY_HISTORY 都一樣。規則用 file_perms 那一份。"""
import asyncio
import json
import os

import pytest

from orchestrator import pipeline
from orchestrator.config import ConfigError, load_config
from toolbox import knowledge_store as ks
from toolbox import memory

posix_only = pytest.mark.skipif(os.name != "posix", reason="要真的 POSIX 權限位元(部署端是 Linux)")

BINDING = "---\nid: kb-x\ntier: binding\nsource: user-explicit\nscope: ['*']\n---\n不得重複報 H001。\n"


def _mkdir(path, mode):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    return path


def _write(path, mode, text=BINDING):
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture
def kb(tmp_path):
    """755 的知識庫目錄,底下一個 644 的 binding;上層也是 755。"""
    tmp_path.chmod(0o755)
    root = _mkdir(tmp_path / "kb", 0o755)
    _write(root / "a.md", 0o644)
    return root


# ─────────────────── 知識庫 ───────────────────

@posix_only
def test_權限正確的知識庫照常讀(kb):
    assert [it.id for it in ks.load_items(kb)] == ["kb-x"]


@posix_only
def test_知識庫裡_666_的檔就停(kb):
    bad = _write(kb / "b.md", 0o666)
    with pytest.raises(ConfigError, match=f"chmod 644 {bad}"):
        ks.load_items(kb)


@posix_only
def test_知識庫裡_777_的子目錄就停(kb):
    sub = _mkdir(kb / "team", 0o777)
    _write(sub / "c.md", 0o644)
    with pytest.raises(ConfigError, match=f"chmod 755 {sub}"):
        ks.load_items(kb)


@posix_only
def test_知識庫目錄本身或上層可寫就停(kb):
    kb.chmod(0o775)
    with pytest.raises(ConfigError, match=f"chmod 755 {kb}"):
        ks.load_items(kb)
    kb.chmod(0o755)
    kb.parent.chmod(0o777)
    with pytest.raises(ConfigError, match=f"chmod 755 {kb.parent}"):
        ks.load_items(kb)


@posix_only
def test_沒設_MEMORY_KB_時預設路徑也檢查(kb, monkeypatch):
    """沒設時用 repo 的 memory/knowledge/:umask 002 下的 775 目錄、664 檔一樣要停。"""
    monkeypatch.setattr(ks, "KB_DIR", kb)
    _write(kb / "d.md", 0o664)
    with pytest.raises(ConfigError, match="知識庫的權限"):
        ks.load_items()


@posix_only
def test_知識庫裡的連結指到可寫的目錄就停(kb, tmp_path):
    shared = _mkdir(tmp_path / "shared", 0o777)
    target = _write(shared / "e.md", 0o644)
    (kb / "e.md").symlink_to(target)
    with pytest.raises(ConfigError, match=f"chmod 755 {shared}"):
        ks.load_items(kb)


@posix_only
def test_知識庫裡連結的子目錄指到可寫的地方就停(kb, tmp_path):
    shared = _mkdir(tmp_path / "shared", 0o777)
    _write(_mkdir(shared / "more", 0o755) / "f.md", 0o644)
    (kb / "more").symlink_to(shared / "more")
    with pytest.raises(ConfigError, match=f"chmod 755 {shared}"):
        ks.load_items(kb)


@posix_only
def test_知識庫的連結繞回上層_不會無限循環(kb):
    (kb / "loop").symlink_to(kb)
    assert [it.id for it in ks.load_items(kb)] == ["kb-x"]


@posix_only
def test_不是_md_的檔不檢查(kb):
    """讀的只有 .md(load_items 的 glob):README 之類的權限不影響知識庫。"""
    _write(kb / "notes.txt", 0o666, "x")
    assert [it.id for it in ks.load_items(kb)] == ["kb-x"]


# 服務自己寫的檔(學到的慣例、從知識庫 repo 拉回的 binding、判例)要在 umask 002 的主機上也是 644:
# 照 umask 建成 664 的話,下一次讀的時候就被上面的規則擋下
@pytest.fixture
def umask_002():
    old = os.umask(0o002)
    yield
    os.umask(old)


@posix_only
def test_umask_002_下寫進知識庫的檔是_644_照常讀得到(kb, umask_002):
    item = ks.Item(id="kb-new", text="新的規範", tier="binding")
    path = ks.save_item(item, kb)
    assert path.stat().st_mode & 0o777 == 0o644
    assert sorted(it.id for it in ks.load_items(kb)) == ["kb-new", "kb-x"]


@posix_only
def test_umask_002_下從知識庫_repo_拉回的檔是_644(kb, umask_002, monkeypatch):
    from orchestrator import knowledge_governance as kg
    gov = object.__new__(kg.KnowledgeRepo)
    monkeypatch.setattr(kg.KnowledgeRepo, "list_md", lambda self, subdir="knowledge": ["knowledge/g.md"])
    monkeypatch.setattr(kg.KnowledgeRepo, "get_file", lambda self, path, ref="main": BINDING)
    assert gov.sync_to_local(kb) == ["g.md"]
    assert (kb / "g.md").stat().st_mode & 0o777 == 0o644
    ks.load_items(kb)


# ─────────────────── 審查判例檔 ───────────────────

@pytest.fixture
def history(tmp_path, monkeypatch):
    tmp_path.chmod(0o755)
    d = _mkdir(tmp_path / "review_history", 0o755)
    path = d / "history.jsonl"
    monkeypatch.setattr(memory, "HISTORY_FILE", path)
    return path


@posix_only
def test_判例檔權限正確照常讀寫(history):
    assert json.loads(memory.record_feedback("金額門檻寫錯", "rejected"))["ok"] is True
    history.chmod(0o644)
    assert json.loads(memory.lookup_similar_reviews("金額門檻寫錯"))["results"]


@posix_only
def test_umask_002_下新建的判例檔是_644_下一次照常讀得到(history, umask_002):
    memory.record_feedback("金額門檻寫錯", "rejected")
    assert history.stat().st_mode & 0o777 == 0o644
    assert json.loads(memory.lookup_similar_reviews("金額門檻寫錯"))["results"]


@posix_only
def test_判例檔_666_就停(history):
    _write(history, 0o666, "")
    with pytest.raises(ConfigError, match=f"chmod 644 {history}"):
        memory.lookup_similar_reviews("t")
    with pytest.raises(ConfigError, match="審查判例檔的權限"):
        memory.record_feedback("t", "rejected")


@posix_only
def test_判例檔所在的目錄可寫就停_還沒建檔也一樣(history):
    history.parent.chmod(0o777)
    with pytest.raises(ConfigError, match=f"chmod 755 {history.parent}"):
        memory.record_feedback("t", "rejected")
    with pytest.raises(ConfigError, match=f"chmod 755 {history.parent}"):
        memory.lookup_similar_reviews("t")


# ─────────────────── 審查流程 ───────────────────
#
# 檢索知識是經 hub 呼叫的工具:工具丟的例外會被 hub 收成一段文字,看起來只是「沒有知識」。
# 所以審查流程自己先檢查一次,權限太寬就記成未完整(交人工)、不讀知識庫。換掉檢查本身,Windows 也跑得到

def _review(monkeypatch, tmp_path, mr_id="971"):
    from toolbox import gitlab
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / f"mr_{mr_id}.json").write_text(json.dumps({
        "title": "調整查詢", "description": "例行維護。", "sha": "e1a5" + "0" * 36,
        "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+SELECT 1",
                   "full_content": "SELECT account_id FROM txn_log"}],
    }, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir(exist_ok=True)
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))
    asked = []

    async def fake_main(cfg, profile, system, user, hub, verbose=True, use_tools=True,
                        trace=None, meta=None):
        if meta is not None:
            meta["finish_reason"] = "stop"
        return json.dumps({"summary": "沒問題", "score": 100, "findings": []}, ensure_ascii=False)

    real_call = pipeline.ToolHub.call_json

    async def spy(self, name, args):
        asked.append(name)
        return await real_call(self, name, args)

    monkeypatch.setattr(pipeline, "run_agent", fake_main)
    monkeypatch.setattr(pipeline.ToolHub, "call_json", spy)
    return asyncio.run(pipeline.review_mr(load_config(), mr_id)), asked


def _kb_incomplete(report):
    return [f for f in report["findings"] if f.get("stage") == "知識庫檢索"]


def test_知識庫權限太寬_審查記成未完整_不讀知識庫(monkeypatch, tmp_path, capsys):
    def too_open(kb_dir=None):
        raise ConfigError("知識庫的權限:目錄 /srv/kb/team 的權限太寬(777)")
    monkeypatch.setattr(ks, "check_kb_perms", too_open)
    report, asked = _review(monkeypatch, tmp_path)
    (fd,) = _kb_incomplete(report)
    assert fd["severity"] == "major" and "權限太寬" in fd["detail"]
    assert "/srv/kb" not in fd["detail"], "路徑寫在 log,不貼到 MR"
    assert "memory__retrieve_knowledge" not in asked
    assert report["decision"] != "auto_approved"
    assert "/srv/kb/team" in capsys.readouterr().out


def test_知識庫權限正確_照常檢索(monkeypatch, tmp_path):
    monkeypatch.setattr(ks, "check_kb_perms", lambda kb_dir=None: None)
    report, asked = _review(monkeypatch, tmp_path, mr_id="972")
    assert _kb_incomplete(report) == []
    assert "memory__retrieve_knowledge" in asked


def test_知識庫檢查本身出錯也當成權限有問題(monkeypatch):
    def broken(kb_dir=None):
        raise OSError("讀不到")
    monkeypatch.setattr(ks, "check_kb_perms", broken)
    assert pipeline._kb_perm_problem() == "OSError"
    monkeypatch.setattr(ks, "check_kb_perms", lambda kb_dir=None: None)
    assert pipeline._kb_perm_problem() is None
