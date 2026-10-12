"""get_mr_diff 帶出 GitLab diff_refs 的 base_sha / head_sha / start_sha(#15 接線與 #22 共用)。

不連網:real 模式以假的 _api / _api_pages 取代。重點是欄位名照 GitLab 原樣、
拿不到或不是完整 commit 編號時一律給 None(呼叫端不可以拿別的值代替),
以及原有欄位不受影響。
"""
import json

import pytest

from toolbox import gitlab

BASE = "a" * 40
HEAD = "b" * 40
START = "c" * 40
KEYS = ("base_sha", "head_sha", "start_sha")


def _real(monkeypatch, mr: dict, changes=None):
    monkeypatch.setattr(gitlab, "REAL_MODE", True)
    monkeypatch.setattr(gitlab, "_api", lambda method, path, **kw: mr)
    monkeypatch.setattr(gitlab, "_api_pages", lambda path, params=None: (changes or [], False))
    return json.loads(gitlab.get_mr_diff("7"))


def _mock(monkeypatch, tmp_path, mr: dict):
    monkeypatch.setattr(gitlab, "REAL_MODE", False)
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", tmp_path)
    (tmp_path / "mr_7.json").write_text(json.dumps(mr), encoding="utf-8")
    return json.loads(gitlab.get_mr_diff("7"))


def test_real模式原樣帶出三個sha(monkeypatch):
    out = _real(monkeypatch, {"title": "t", "sha": HEAD, "diff_refs": {
        "base_sha": BASE, "head_sha": HEAD, "start_sha": START}})
    assert (out["base_sha"], out["head_sha"], out["start_sha"]) == (BASE, HEAD, START)


def test_三個欄位各自對應_不會互相錯位(monkeypatch):
    out = _real(monkeypatch, {"title": "t", "diff_refs": {
        "base_sha": BASE, "head_sha": None, "start_sha": START}})
    assert out["base_sha"] == BASE and out["head_sha"] is None and out["start_sha"] == START


def test_sha256的專案也接受(monkeypatch):
    long = "d" * 64
    out = _real(monkeypatch, {"title": "t", "diff_refs": {
        "base_sha": long, "head_sha": long, "start_sha": long}})
    assert out["base_sha"] == long


@pytest.mark.parametrize("refs", [None, "abc", [], 5, {}], ids=["null", "字串", "list", "數字", "空"])
def test_diff_refs不是物件或是空的_三個都是None(monkeypatch, refs):
    out = _real(monkeypatch, {"title": "t", "diff_refs": refs})
    assert [out[k] for k in KEYS] == [None, None, None]


def test_沒有diff_refs這個欄位_三個都是None(monkeypatch):
    out = _real(monkeypatch, {"title": "t"})
    assert [out[k] for k in KEYS] == [None, None, None]


@pytest.mark.parametrize("bad", [
    "main", "abc123", "A" * 40, "a" * 39, "a" * 41, "a" * 40 + "\n", " " + "a" * 39,
    "../" + "a" * 37, "", 123, ["a" * 40], {"x": 1}, True,
], ids=["分支名", "短sha", "大寫", "39位", "41位", "結尾換行", "開頭空白", "路徑", "空字串",
        "數字", "list", "dict", "布林"])
def test_不是完整commit編號的值不帶出來(monkeypatch, bad):
    """下游會拿這個值去打包下載、抓檔案;不合格的值在這裡就變成 None,不往下傳。"""
    out = _real(monkeypatch, {"title": "t", "diff_refs": {
        "base_sha": bad, "head_sha": HEAD, "start_sha": START}})
    assert out["base_sha"] is None
    assert out["head_sha"] == HEAD and out["start_sha"] == START   # 其他欄位不受牽連


def test_real模式原有欄位不變(monkeypatch):
    out = _real(monkeypatch, {"title": "標題", "description": "說明", "sha": HEAD,
                              "diff_refs": {"base_sha": BASE, "head_sha": HEAD, "start_sha": START}},
                changes=[{"new_path": "a.sql", "old_path": "a.sql", "diff": "+x", "too_large": True}])
    assert out["title"] == "標題" and out["description"] == "說明" and out["sha"] == HEAD
    assert out["truncated"] is False
    assert out["files"] == [{"path": "a.sql", "old_path": "a.sql", "diff": "+x",
                             "unreviewable": True}]
    assert set(out) == {"title", "description", "sha", "files", "truncated", *KEYS}


def test_mock模式的fixture沒有diff_refs_三個都是None(monkeypatch, tmp_path):
    out = _mock(monkeypatch, tmp_path, {"title": "t", "files": []})
    assert [out[k] for k in KEYS] == [None, None, None]
    assert out["sha"] == "mock-7" and out["files"] == []            # 原有欄位不變


def test_mock模式的fixture有diff_refs就帶出來(monkeypatch, tmp_path):
    """接線的測試要能用 fixture 指定 base / head commit。"""
    out = _mock(monkeypatch, tmp_path, {"title": "t", "files": [], "diff_refs": {
        "base_sha": BASE, "head_sha": HEAD, "start_sha": START}})
    assert (out["base_sha"], out["head_sha"], out["start_sha"]) == (BASE, HEAD, START)


def test_mock模式同樣不帶出不合格的值(monkeypatch, tmp_path):
    out = _mock(monkeypatch, tmp_path, {"title": "t", "files": [], "diff_refs": {
        "base_sha": "main", "head_sha": HEAD, "start_sha": 1}})
    assert out["base_sha"] is None and out["head_sha"] == HEAD and out["start_sha"] is None


def test_內附的fixture都還讀得出來():
    """既有 fixture 沒有 diff_refs,不可以因為多帶欄位而壞掉。"""
    for f in sorted(gitlab.FIXTURES_DIR.glob("mr_*.json")):
        out = json.loads(gitlab.get_mr_diff(f.stem[3:]))
        assert all(k in out for k in KEYS)


def test_欄位名與post_inline_comment用的是同一組():
    import inspect
    src = inspect.getsource(gitlab.post_inline_comment)
    assert all(f'"{k}"' in src for k in gitlab._DIFF_REF_KEYS)
    assert gitlab._DIFF_REF_KEYS == KEYS
