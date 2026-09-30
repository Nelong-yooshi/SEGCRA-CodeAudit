"""分層知識庫的測試 — binding / guideline / observed 三層與權威衝突,不呼叫 LLM。

為什麼值得單獨測:「使用者明確講過的規定,要蓋過從程式碼觀察到的習慣」這件事,是
知識治理的核心承諾,而它完全由 resolve_conflicts 這個純函式決定。但 golden set 只有
三個 binding 的 case(mr_301/302/303),**guideline 與 observed 兩層、以及跨層的權威
衝突,一個 case 都沒有**(eval/README「覆蓋缺口」列的一項)。

端到端測不到的原因也很實際:衝突解析的結果目前不會寫進 report,golden case 的斷言
看不到它,只能間接從「模型有沒有照較高權威那條做」去猜——那又變成在賭模型。
在這一層測就沒有這個問題。

驗的東西:
  1. 權威階梯:user-explicit(40) > user-authored(30) > auto-consolidated(20)
     > code-mined(10),同 conflict_key 時高者勝。
  2. **guideline vs observed 的跨層衝突**——#8 點名缺的那一項。
  3. 權威平手時取較新(ts 大者)。
  4. 落敗方會被記進 suppressed,而且記得下是誰打敗它(可稽核,不是靜默消失)。
  5. binding 一律注入、不看相關度;guideline/observed 要有相關度且受 top_k 限制
     —— 這是「恆常規範不會因為聊太久被擠出視窗」的機制保證。
  6. scope 隔離:別的系統的慣例不會跑進來。
  7. 被更高權威覆蓋的風格碼會**停用**(active_style_codes 不含它),
     落敗 binding 的 suppress 也不再生效。
"""
import time

import pytest

from toolbox import knowledge_store as ks
from toolbox.knowledge_store import Item, dumps


def _item(id, tier="guideline", source="user-authored", conflict_key=None,
          scope=("anomaly-rules",), code=None, suppress=(), text="內容", ts=1000):
    return Item(id=id, text=text, tier=tier, source=source, scope=list(scope),
                code=code, suppress=list(suppress), conflict_key=conflict_key,
                tags=[], evidence=1, ts=ts)


# ─────────────────── 權威階梯 ───────────────────

@pytest.mark.parametrize("winner_source,loser_source", [
    ("user-explicit", "user-authored"),
    ("user-explicit", "code-mined"),
    ("user-authored", "auto-consolidated"),
    ("auto-consolidated", "code-mined"),
])
def test_同一主題衝突時權威高者勝(winner_source, loser_source):
    items = [_item("loser", source=loser_source, conflict_key="k"),
             _item("winner", source=winner_source, conflict_key="k")]
    kept, suppressed = ks.resolve_conflicts(items)
    assert [it.id for it in kept] == ["winner"]
    assert suppressed[0]["id"] == "loser"
    assert suppressed[0]["beaten_by"] == "winner"


def test_guideline_覆蓋_observed():
    """#8 點名的缺口:跨層(guideline vs observed)的權威衝突。

    情境:團隊寫下的參考慣例說「NULL 比對要用 IS NULL」,但從歷史程式碼觀察到的習慣
    是「直接用 <> 比」。觀察到的習慣權威最低,不該蓋過人寫的慣例。
    """
    items = [
        _item("obs-null-legacy", tier="observed", source="code-mined",
              conflict_key="null-comparison", text="歷史程式碼多半直接用 <> 比對"),
        _item("guide-null-explicit", tier="guideline", source="user-authored",
              conflict_key="null-comparison", text="可能為 NULL 的欄位一律用 IS NULL"),
    ]
    kept, suppressed = ks.resolve_conflicts(items)
    assert [it.id for it in kept] == ["guide-null-explicit"]
    assert suppressed[0]["loser_authority"] < suppressed[0]["winner_authority"]


def test_使用者明確指示覆蓋觀察到的習慣():
    """最重要的一組:使用者當面講過的,蓋過機器自己學到的。"""
    items = [
        _item("obs-comma", tier="observed", source="code-mined",
              conflict_key="comma-style", code="S-COMMA"),
        _item("user-comma", tier="binding", source="user-explicit",
              conflict_key="comma-style"),
    ]
    kept, _ = ks.resolve_conflicts(items)
    assert [it.id for it in kept] == ["user-comma"]


def test_權威平手時取較新():
    items = [_item("old", source="user-authored", conflict_key="k", ts=1000),
             _item("new", source="user-authored", conflict_key="k", ts=2000)]
    kept, _ = ks.resolve_conflicts(items)
    assert [it.id for it in kept] == ["new"]


def test_沒有_conflict_key_的項目互不影響():
    """沒宣告衝突主題的項目各自獨立,不會被誤殺。"""
    items = [_item("a"), _item("b"), _item("c")]
    kept, suppressed = ks.resolve_conflicts(items)
    assert len(kept) == 3
    assert suppressed == []


def test_落敗方要留下可稽核的記錄():
    """靜默消失是不可接受的:誰被誰蓋掉、權威各是多少,都要查得到。"""
    items = [_item("loser", source="code-mined", conflict_key="k"),
             _item("winner", source="user-explicit", conflict_key="k")]
    _, suppressed = ks.resolve_conflicts(items)
    rec = suppressed[0]
    assert rec["key"] == "k"
    assert rec["loser_authority"] == 10 and rec["winner_authority"] == 40


# ─────────────────── 檢索:binding 恆常、其餘看相關度 ───────────────────

def test_binding_不看相關度一律注入():
    """binding 的承諾是「不論對話多長、多久以前講定,一律遵守」。

    所以它不參與相關度排序,也不受 top_k 限制——否則講定的規範會因為這次的 SQL
    剛好沒提到相關字眼而被擠掉,退回舊行為。
    """
    items = [_item("bind-x", tier="binding", text="完全無關的恆常規範內容")]
    r = ks.retrieve("select sum(amount) from transactions", scope=["anomaly-rules"],
                    items=items)
    assert [it.id for it in r["binding"]] == ["bind-x"]


def test_guideline_要有相關度才會被檢索到():
    items = [_item("guide-match", text="沖正 退匯 淨額 的處理方式"),
             _item("guide-unrelated", text="zzzzzzzzzz")]
    r = ks.retrieve("沖正與退匯要如何處理", scope=["anomaly-rules"], items=items)
    ids = [it.id for it in r["retrieved"]]
    assert "guide-match" in ids
    assert "guide-unrelated" not in ids


def test_top_k_限制檢索數量():
    items = [_item(f"g{i}", text="沖正 退匯 淨額") for i in range(10)]
    r = ks.retrieve("沖正 退匯 淨額", scope=["anomaly-rules"], items=items, top_k=3)
    assert len(r["retrieved"]) == 3


def test_scope_隔離_別的系統的慣例不會跑進來():
    items = [_item("anomaly-one", scope=["anomaly-rules"], tier="binding"),
             _item("crm-one", scope=["crm"], tier="binding"),
             _item("global-one", scope=["*"], tier="binding")]
    r = ks.retrieve("任意查詢", scope=["anomaly-rules"], items=items)
    ids = {it.id for it in r["binding"]}
    assert ids == {"anomaly-one", "global-one"}, "crm 的慣例不該出現在 anomaly-rules"


# ─────────────────── 覆蓋後的效果:風格碼與 suppress 會停用 ───────────────────

def _write_kb(tmp_path, items):
    for it in items:
        (tmp_path / f"{it.id}.md").write_text(dumps(it), encoding="utf-8")
    return tmp_path


def test_被覆蓋的風格碼要停用(tmp_path):
    """「學到才檢查」:觀察到的風格習慣若被使用者明確指示蓋過,那個檢查就不該再跑。

    否則會出現「你明明說過不要這樣,系統還一直報」——正是 binding 機制要消滅的體驗。
    """
    kb = _write_kb(tmp_path, [
        _item("obs-comma", tier="observed", source="code-mined",
              conflict_key="comma-style", code="S-COMMA"),
        _item("user-comma", tier="binding", source="user-explicit",
              conflict_key="comma-style"),
    ])
    assert "S-COMMA" not in ks.active_style_codes(["anomaly-rules"], kb_dir=kb)


def test_沒被覆蓋的風格碼正常生效(tmp_path):
    kb = _write_kb(tmp_path, [
        _item("obs-comma", tier="observed", source="code-mined", code="S-COMMA"),
    ])
    assert "S-COMMA" in ks.active_style_codes(["anomaly-rules"], kb_dir=kb)


def test_只有_binding_的_suppress_生效(tmp_path):
    """suppress(確定性濾除檢核點)只有 binding 層能用——參考慣例不該有這種殺傷力。"""
    kb = _write_kb(tmp_path, [
        _item("bind-netted", tier="binding", source="user-explicit", suppress=["H001"]),
        _item("guide-netted", tier="guideline", source="user-authored", suppress=["H002"]),
    ])
    codes = ks.binding_suppress_codes(["anomaly-rules"], kb_dir=kb)
    assert codes == {"H001"}, "guideline 的 suppress 不該生效"


def test_落敗_binding_的_suppress_不再生效(tmp_path):
    """被更高權威蓋掉的 binding,它要抑制的檢核點應該恢復回報。"""
    kb = _write_kb(tmp_path, [
        _item("old-netted", tier="binding", source="auto-consolidated",
              conflict_key="reversal", suppress=["H001"], ts=1000),
        _item("new-not-netted", tier="binding", source="user-explicit",
              conflict_key="reversal", ts=2000),
    ])
    assert ks.binding_suppress_codes(["anomaly-rules"], kb_dir=kb) == set()


def test_知識項可以存檔再讀回來且內容不變(tmp_path):
    """機器學到的項目與人寫的同格式(可 diff、可留人閘),往返一次不能走樣。"""
    original = _item("roundtrip", tier="binding", source="user-explicit",
                     conflict_key="k", code="S-COMMA", suppress=["H001"],
                     text="往返測試內容", ts=int(time.time()))
    kb = _write_kb(tmp_path, [original])
    loaded = {it.id: it for it in ks.load_items(kb)}["roundtrip"]
    for field in ("tier", "source", "conflict_key", "code", "suppress", "text", "scope"):
        assert getattr(loaded, field) == getattr(original, field), f"{field} 走樣了"
