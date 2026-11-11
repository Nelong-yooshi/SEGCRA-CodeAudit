"""隔離子行程是安全邊界的一層:拿不到秘密、結果只用 JSON 傳回、形狀不對一律失敗。

  * 環境變數:token、密碼、模型通行碼不給;子行程會用到的設定照給
  * 傳遞格式:型別原樣來回;不認得的型別、不在清單的 dataclass、欄位不符都擋下
  * 被攻陷的子行程:送回來的資料不對(不是 JSON、形狀不對、過大、訊息夾控制字元)都收成失敗
"""
import dataclasses
import importlib
import os
import pathlib
import sys
import time

import pytest

from orchestrator import env_registry, isolation, isolation_child
from orchestrator.dbt_render import DbtProjectInfo, RenderResult
from orchestrator.isolation import CHILD_ENV_NAMES, child_env, run_isolated
from orchestrator.isolation_child import CodecError, _decode, _encode

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRETS = ("GITLAB_TOKEN", "GITLAB_READ_TOKEN", "GITLAB_URL", "SANDBOX_MSSQL_PASSWORD",
           "SANDBOX_MSSQL_USER", "LLM_API_KEY", "OPENAI_API_KEY", "OLLAMA_URL")


def _run(fn, *args, types=(), timeout_s=60, **kwargs):
    return run_isolated(fn, args, kwargs, timeout_s=timeout_s, max_memory_mb=1024,
                        on_failure=lambda m: ("失敗", m), types=types)


# 給子行程跑的函式(要在模組層級,子行程照名稱匯入)
def env_snapshot():
    return dict(os.environ)


def proc_environ():
    with open("/proc/self/environ", "rb") as f:
        return f.read().decode("utf-8", "replace")


def nap(seconds):
    time.sleep(seconds)
    return "醒了"


def give(value):
    return value


def make_point():
    return Point(1)


# 不算「本專案的檔案」的目錄。`.venv` 通常就建在專案根目錄底下,不排除的話第三方套件
# 會被當成本專案模組掃進去 —— 於是 pytest 自己讀的 PYTEST_ADDOPTS、FORCE_COLOR 變成
# 「沒有決定的環境變數」,本機跑全套就紅(CI 的 venv 在別的地方,所以 CI 看不到)。
_NOT_PROJECT = ("tests", ".venv", "venv", "site-packages", "__pypackages__")


def project_modules(names):
    """匯入這些模組之後,子行程裡載入了哪些本專案的檔案(相對路徑)。"""
    for name in names:
        importlib.import_module(name)
    root = pathlib.Path(__file__).resolve().parents[1]
    out = []
    for module in list(sys.modules.values()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        resolved = pathlib.Path(path).resolve()
        if (resolved.is_relative_to(root)
                and not set(resolved.parts) & set(_NOT_PROJECT)):
            out.append(resolved.relative_to(root).as_posix())
    return sorted(out)


def _original():
    return "改綁前的函式"


_original_ref = _original


def _original():                 # noqa: F811 — 刻意改綁:照名稱找回來的已經不是同一個函式
    return "被換掉的函式"


@dataclasses.dataclass(frozen=True)
class Point:
    x: int
    y: tuple = ()


@dataclasses.dataclass
class NotListed:
    a: int = 0


# ─────────────────── 環境變數 ───────────────────

def test_子行程拿不到秘密(monkeypatch):
    for name in SECRETS:
        monkeypatch.setenv(name, "fake-secret-value")
    env = _run(env_snapshot)
    assert isinstance(env, dict) and env, env
    assert not {k.upper() for k in env} & set(SECRETS)
    assert "fake-secret-value" not in env.values()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc/self/environ 只有 Linux")
def test_子行程啟動時的環境變數裡也沒有秘密(monkeypatch):
    """在子行程裡才刪掉不夠:/proc/self/environ 留著啟動時的那一份。"""
    monkeypatch.setenv("GITLAB_TOKEN", "fake-secret-value")
    assert "fake-secret-value" not in _run(proc_environ)


def test_子行程會用到的設定照給(monkeypatch):
    monkeypatch.setenv("SQL_DIALECT", "tsql")
    monkeypatch.setenv("LINT_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    env = _run(env_snapshot)
    assert env.get("SQL_DIALECT") == "tsql" and env.get("LINT_TIMEOUT_SECONDS") == "45"
    assert env.get("LC_ALL") == "C.UTF-8"


def test_環境變數的名稱不分大小寫():
    environ = {"Path": "p", "SystemRoot": "s", "lc_ctype": "c", "GitLab_Token": "t", "X": "x"}
    assert child_env(environ) == {"Path": "p", "SystemRoot": "s", "lc_ctype": "c"}


# 子行程會載入的模組讀到、但子行程用不到的環境變數(只在主行程的函式裡讀)
MAIN_PROCESS_ONLY = {
    # orchestrator/config.py:load_config 等讀設定的函式,只在主行程呼叫
    "LLM_TEMPERATURE", "LLM_SEED", "LLM_REASONING_EFFORT", "SEGCRA_DBT_DATABASE", "OLLAMA_URL",
}


def test_子行程會用到的模組讀的環境變數都有決定():
    """子行程實際載入的本專案模組(五個隔離函式所在的模組與它們匯入的),讀到的環境變數要嘛列在
    CHILD_ENV_NAMES(子行程照給),要嘛列在 MAIN_PROCESS_ONLY(子行程用不到)。新增一個讀環境
    變數的地方而沒有決定,這裡會擋下:漏給的話子行程悄悄用預設值,多給的可能是秘密。"""
    files = _run(project_modules, ["orchestrator.dbt_render", "orchestrator.dbt_impact",
                                   "orchestrator.dbt_context", "toolbox.sqltools"])
    assert isinstance(files, list) and "orchestrator/isolation_child.py" in files, files
    names = {read.name for rel in files for read in env_registry.scan_file(PKG_ROOT / rel, rel)}
    undecided = names - CHILD_ENV_NAMES - MAIN_PROCESS_ONLY
    assert undecided == set(), sorted(undecided)
    assert not MAIN_PROCESS_ONLY & CHILD_ENV_NAMES
    assert {"SQL_DIALECT", "LINT_TIMEOUT_SECONDS"} <= names     # 前提:真的有掃到


# ─────────────────── 傳遞格式 ───────────────────

COMPLEX = {
    "none": None, "bool": True, "int": -3, "float": 1.5, "str": "表\n\x00",
    "list": [1, [2]], "tuple": (1, ("a",)), "set": {1, 2}, "frozen": frozenset({"x"}),
    ("tuple", "key"): {1: "int key"}, "path": pathlib.Path("a/b.sql"),
    "dc": Point(1, (Point(2),)),
}


def _same(a, b):
    """值相等,而且每一層的型別都一樣(tuple 不能變成 list)。"""
    assert type(a) is type(b), (a, b)
    if isinstance(a, dict):
        assert set(a) == set(b)
        for k in a:
            _same(a[k], b[k])
    elif isinstance(a, (list, tuple)):
        assert len(a) == len(b)
        for x, y in zip(a, b):
            _same(x, y)
    else:
        assert a == b


def test_型別原樣來回():
    _same(_decode(_encode(COMPLEX), {isolation_child._type_name(Point): Point}), COMPLEX)


def test_經過子行程也一樣():
    _same(_run(give, COMPLEX, types=(Point,)), COMPLEX)


def test_專案資訊與展開結果經過子行程不變():
    info = DbtProjectInfo(project_name="p", ref_names=frozenset({"a"}),
                          sources={("raw", "t"): (None, "T")})
    result = RenderResult(ok=True, sql="SELECT 1", line_map={1: 1, 2: 3}, map_method="sentinel")
    assert _run(give, (info, result), types=(DbtProjectInfo, RenderResult)) == (info, result)


def test_字串子類別傳成一般字串():
    class Sub(str):
        pass
    out = _decode(_encode(Sub("x")), {})
    assert out == "x" and type(out) is str


@pytest.mark.parametrize("value", [object(), b"bytes", float("nan"), float("inf"), NotListed(),
                                   lambda: 1, {1: object()}],
                         ids=["object", "bytes", "nan", "inf", "unlisted", "lambda", "nested"])
def test_不能傳遞的值(value):
    with pytest.raises(CodecError):
        _encode(value) if not isinstance(value, NotListed) else _decode(_encode(value), {})


def test_巢狀太深():
    deep = []
    for _ in range(isolation_child.MAX_DEPTH + 5):
        deep = [deep]
    with pytest.raises(CodecError):
        _encode(deep)
    encoded = {"$list": []}
    for _ in range(isolation_child.MAX_DEPTH + 5):
        encoded = {"$list": [encoded]}
    with pytest.raises(CodecError):
        _decode(encoded, {})


POINT = isolation_child._type_name(Point)


@pytest.mark.parametrize("obj", [
    {"$dc": POINT, "f": {"x": 1}},                                  # 少了欄位(有預設值也不行)
    {"$dc": POINT, "f": {"x": 1, "y": {"$tuple": []}, "z": 2}},     # 多了欄位
    {"$dc": isolation_child._type_name(NotListed), "f": {"a": 1}},        # 不在清單
    {"$dc": "os:system", "f": {}},                                  # 不在清單的任何東西
    {"$dc": POINT, "f": {"x": 1, "y": {"$tuple": []}}, "extra": 1},
    {"$what": []}, {"$list": "x"}, {"$list": [], "$tuple": []}, {}, [1, 2],
    {"$dict": [[1]]}, {"$dict": [[{"$list": []}, 1]]},                # dict 形狀不對、鍵不能雜湊
    {"$set": [{"$list": []}]}, {"$path": 1},
])
def test_形狀不對的資料一律擋下(obj):
    with pytest.raises(CodecError):
        _decode(obj, {POINT: Point})


# ─────────────────── 子行程的行為 ───────────────────

def test_逾時強制終止():
    t = time.monotonic()
    assert _run(nap, 30, timeout_s=2) == ("失敗", "IsolationError: 執行逾時(超過 2 秒),已強制終止")
    assert time.monotonic() - t < 15


def test_fn_自己的輸出不混進結果():
    assert _run(print, "印到標準輸出") is None


def test_大的參數傳得過去():
    assert _run(len, "字" * 5_000_000) == 5_000_000


def test_照名稱找回來的不是同一個函式時不執行():
    out = _run(_original_ref)
    assert out[0] == "失敗" and "無法啟動子行程" in out[1]


def test_結果裡有沒列在_types_的_dataclass_算失敗():
    assert _run(make_point) == ("失敗", "IsolationError: 子行程回傳了非預期的資料")


def test_參數或結果無法傳遞時算失敗():
    assert _run(object) == ("失敗", "IsolationError: 子行程的參數或結果無法傳遞")
    # 參數裡有 dataclass、types 卻沒列:子行程解不開
    assert _run(give, Point(1)) == ("失敗", "IsolationError: 子行程的參數或結果無法傳遞")


# ─────────────────── 被攻陷的子行程 ───────────────────

@pytest.fixture
def evil(monkeypatch, tmp_path):
    """子行程換成送回指定位元組的程式(讀完輸入、照樣只拿到過濾後的環境變數)。
    內容放在檔案裡:直接寫進命令列的話,大的內容會超過 Windows 命令列的長度上限。"""
    def install(payload: bytes):
        path = tmp_path / "payload.bin"
        path.write_bytes(payload)
        monkeypatch.setattr(isolation, "_BOOTSTRAP", (
            "import sys\nsys.stdin.buffer.read()\n"
            f"sys.stdout.buffer.write(open({str(path)!r}, 'rb').read())\n"))
    return install


UNEXPECTED = ("失敗", "IsolationError: 子行程回傳了非預期的資料")


@pytest.mark.parametrize("payload", [
    b"not json", b"[1,2]", b'{"status": "ok"}', b'{"status": "maybe", "value": 1}',
    b'{"status": "ok", "value": 1, "extra": 2}', b'{"status": "error", "message": 3}',
    b'{"status": "error", "message": "x", "more": 1}', b'{"status": "ok", "value": NaN}',
    b'{"status": "ok", "value": {"$dc": "os:system", "f": {}}}', b"\xff\xfe",
    b'{"status": "ok", "value": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",
], ids=["not-json", "list", "no-value", "bad-status", "extra-key", "message-not-str",
        "error-extra-key", "nan", "unlisted-type", "not-utf8", "too-deep"])
def test_送回來的資料不對時算失敗(evil, payload):
    evil(payload)
    assert _run(len, "x") == UNEXPECTED


def test_送回來的錯誤訊息去掉控制字元_有長度上限(evil):
    evil(b'{"status": "error", "message": "a\\u001b[31mb\\nc' + b"x" * 1000 + b'"}')
    status, message = _run(len, "x")
    assert status == "失敗" and message.startswith("a [31mb c")
    assert len(message) == isolation.MAX_MESSAGE_CHARS and message.isprintable()


def test_送回來的資料過大時算失敗(evil, monkeypatch):
    monkeypatch.setattr(isolation, "MAX_RESULT_BYTES", 1000)
    evil(b'{"status": "ok", "value": "' + b"x" * 5000 + b'"}')
    assert _run(len, "x") == ("失敗", "IsolationError: 子行程回傳的資料超過上限")


def test_沒送出結果就結束(monkeypatch):
    monkeypatch.setattr(isolation, "_BOOTSTRAP", "import sys\nsys.stdin.buffer.read()\nsys.exit(7)\n")
    status, message = _run(len, "x")
    assert status == "失敗" and "異常結束" in message and "7" in message


def test_正常的結果照常收得到(evil):
    """前提:替身子行程送回合格的資料時是收得到的(不然上面那些失敗不代表什麼)。"""
    evil(b'{"status": "ok", "value": {"$tuple": [1, "a"]}}')
    assert _run(len, "x") == (1, "a")


# ─────────────────── 主行程那一側能做的事 ───────────────────
# isolation.py 不在 tests/test_dbt_render.py 的 import 白名單裡(它要啟動子行程)。
# 它用 subprocess 與 os 做的事由這裡鎖住:多做一件就失敗。

def _parent_tree():
    import ast
    return ast.parse((PKG_ROOT / "orchestrator" / "isolation.py").read_text(encoding="utf-8"))


def _attrs_of(tree, name):
    import ast
    return {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == name}


def test_主行程那一側只用_subprocess_起固定的子行程():
    """只呼叫一次 Popen,指令是 [sys.executable, "-I", "-c", _BOOTSTRAP],不經 shell;
    subprocess 其他用到的只有常數與逾時例外。"""
    import ast
    tree = _parent_tree()
    assert _attrs_of(tree, "subprocess") == {"Popen", "PIPE", "DEVNULL", "TimeoutExpired"}
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
    assert [c.func.attr for c in calls] == ["Popen"]
    (popen,) = calls
    cmd = popen.args[0]
    assert isinstance(cmd, ast.List) and ast.unparse(cmd) == "[sys.executable, '-I', '-c', _BOOTSTRAP]"
    keywords = {k.arg: ast.unparse(k.value) for k in popen.keywords}
    assert "shell" not in keywords and keywords.get("env") == "child_env()"
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                and (n.module or "") in ("subprocess", "os")]


def test_主行程那一側只用_os_讀環境變數():
    assert _attrs_of(_parent_tree(), "os") == {"environ"}


def test_子行程的進入點在子行程那一側():
    """_BOOTSTRAP 匯入的是 isolation_child 的進入點;isolation_child 不匯入主行程那一側。"""
    assert "from orchestrator.isolation_child import _child_main" in isolation._BOOTSTRAP
    assert not hasattr(isolation_child, "run_isolated") and not hasattr(isolation_child, "subprocess")
