"""isolation — 在獨立子行程中執行「處理不可信內容」的函式。

用途:dbt 樣板展開(dbt_render)、macro 檔檢查、macro 反查(dbt_impact)、專案資訊整理
(dbt_context)與 lint(toolbox.sqltools)都要處理待審 MR 的內容。Jinja 沙箱與各模組內的上限
擋得住常見手法,但擋不住「一個跑不完的迴圈」或「一個吃光記憶體的字串」;沙箱萬一有逃逸
漏洞,樣板也不該拿得到任何秘密。這裡用行程邊界處理,它是安全邊界的一層:

  * 超過 timeout_s 秒強制終止子行程
  * 支援的平台(Linux)以 RLIMIT_AS 限制子行程記憶體;Windows 上只有逾時
  * 子行程只拿到它需要的環境變數(CHILD_ENV_NAMES):GitLab token、沙盒密碼、模型端點的通行碼
    一開始就不給。用 subprocess 啟動並明確指定環境變數——在子行程裡才刪掉不夠(Linux 的
    /proc/self/environ 仍留著啟動時的那一份),改主行程的環境變數則會影響同時在跑的其他審查
  * 參數與結果一律用 JSON 傳遞(_encode / _decode),不用 pickle:子行程被攻陷時,傳回來的
    資料也不能在主行程執行程式。只認得基本型別、清單、tuple、集合、dict 與呼叫端列出的
    dataclass(types=),欄位名稱要完全一致;其他一律當成失敗
  * 子行程的任何異常(含 MemoryError、啟動失敗、被系統終止、回傳的資料不對)一律收斂成
    失敗結果,訊息不含例外內容(避免帶出路徑或輸入內容)

各模組共用這一份實作,避免各寫一套、各有漏洞。

另外放「把同步呼叫丟到背景執行緒」的共用寫法(run_in_thread):執行驗證的沙盒與查受影響
model 的規格都用它,外層的時間上限才插得進來。

限制:
  * fn 必須是模組層級、可以用「模組:名稱」找回來的函式(子行程照名稱匯入)
  * 參數與回傳值只能是上面列的型別;dataclass 要由呼叫端列在 types
  * 子行程讀得到磁碟上的檔案(跟主行程同一個帳號);這裡只處理環境變數與回傳資料
"""
import asyncio
import contextvars
import dataclasses
import importlib
import json
import math
import os
import pathlib
import subprocess
import sys
import threading

# 子行程保留的環境變數(其餘一律不給)。只有系統執行需要的、與子行程裡的模組在匯入時會讀的
# 設定;秘密(token、密碼、通行碼)不在這裡。新增要刻意改這裡(tests/test_isolation.py 會檢查
# 子行程會用到的模組讀的環境變數都列在這裡或明寫不需要)。
CHILD_ENV_NAMES = frozenset({
    # 系統:找得到執行檔與系統函式庫、暫存目錄、語系、時區、使用者目錄(sqlfluff 的使用者設定)
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "LANG", "LANGUAGE", "TZ",
    # 子行程裡的模組在匯入時讀的設定(toolbox.sqltools)
    "SQL_DIALECT", "LINT_TIMEOUT_SECONDS",
})
CHILD_ENV_PREFIXES = ("LC_",)

# 子行程送回來的資料最多幾個位元組(展開結果的上限是 dbt_render.MAX_OUTPUT_CHARS,遠小於此)
MAX_RESULT_BYTES = 256 * 1024 * 1024
# 傳遞的資料最多巢狀幾層
MAX_DEPTH = 64
MAX_MESSAGE_CHARS = 300


def child_env(environ=None) -> dict:
    """子行程的環境變數:只留 CHILD_ENV_NAMES 與 CHILD_ENV_PREFIXES 開頭的(名稱不分大小寫)。"""
    environ = os.environ if environ is None else environ
    return {k: v for k, v in environ.items()
            if k.upper() in CHILD_ENV_NAMES or k.upper().startswith(CHILD_ENV_PREFIXES)}


# ------------------------------------------------------------------ JSON 編碼
class CodecError(ValueError):
    """資料不能(或不該)經 JSON 傳遞。"""


def _type_name(cls) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _init_fields(cls) -> list:
    fields = dataclasses.fields(cls)
    if any(not f.init for f in fields):
        raise CodecError(f"{cls.__qualname__} 有不經建構式設定的欄位,無法傳遞")
    return [f.name for f in fields]


def _encode(value, depth: int = 0):
    """轉成可以 json.dumps 的值。容器與 dataclass 用單一鍵的物件標記型別,解回來型別不變。"""
    if depth > MAX_DEPTH:
        raise CodecError("巢狀太深")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CodecError("不能傳遞 NaN / Infinity")
        return float(value)
    if isinstance(value, str):
        return str.__str__(value)
    nxt = depth + 1
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {"$dc": _type_name(type(value)),
                "f": {name: _encode(getattr(value, name), nxt) for name in _init_fields(type(value))}}
    if isinstance(value, pathlib.PurePath):
        return {"$path": str(value)}
    if isinstance(value, dict):
        return {"$dict": [[_encode(k, nxt), _encode(v, nxt)] for k, v in value.items()]}
    for kind, tag in ((list, "$list"), (tuple, "$tuple"), (frozenset, "$frozenset"), (set, "$set")):
        if isinstance(value, kind):
            return {tag: [_encode(v, nxt) for v in value]}
    raise CodecError(f"不能傳遞的型別:{type(value).__name__}")


_SEQUENCES = {"$list": list, "$tuple": tuple, "$frozenset": frozenset, "$set": set}


def _hashable(value):
    try:
        hash(value)
    except TypeError:
        raise CodecError("集合的元素或 dict 的鍵不能雜湊") from None
    return value


def _decode(obj, types: dict, depth: int = 0):
    """_encode 的反向。只建出基本型別、容器與 types 裡的 dataclass({型別名稱: 類別});
    形狀不對一律丟 CodecError。"""
    if depth > MAX_DEPTH:
        raise CodecError("巢狀太深")
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if not isinstance(obj, dict):
        raise CodecError("不認得的資料")
    nxt = depth + 1
    keys = set(obj)
    if keys == {"$dc", "f"} and isinstance(obj["$dc"], str) and isinstance(obj["f"], dict):
        cls = types.get(obj["$dc"])
        if cls is None:
            raise CodecError("不在允許清單的型別")
        names = _init_fields(cls)
        if set(obj["f"]) != set(names):
            raise CodecError("欄位不符")
        return cls(**{name: _decode(obj["f"][name], types, nxt) for name in names})
    if len(keys) != 1:
        raise CodecError("不認得的資料")
    (tag,) = keys
    payload = obj[tag]
    if tag == "$path" and isinstance(payload, str):
        return pathlib.Path(payload)
    if tag == "$dict" and isinstance(payload, list):
        out = {}
        for pair in payload:
            if not (isinstance(pair, list) and len(pair) == 2):
                raise CodecError("dict 的形狀不對")
            out[_hashable(_decode(pair[0], types, nxt))] = _decode(pair[1], types, nxt)
        return out
    if tag in _SEQUENCES and isinstance(payload, list):
        items = [_decode(v, types, nxt) for v in payload]
        if tag in ("$set", "$frozenset"):
            items = [_hashable(v) for v in items]
        return _SEQUENCES[tag](items)
    raise CodecError("不認得的資料")


def _reject_constant(name):
    raise CodecError("不能傳遞 NaN / Infinity")


def _dumps(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def _loads(data: bytes):
    return json.loads(data.decode("utf-8"), parse_constant=_reject_constant)


def _resolve(name: str):
    """「模組:名稱」→ 物件。"""
    module, _, qualname = name.partition(":")
    obj = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _function_name(fn) -> str:
    """fn 的「模組:名稱」;照名稱找不回同一個函式時丟 CodecError(lambda、區域函式、__main__)。"""
    module, qualname = getattr(fn, "__module__", None), getattr(fn, "__qualname__", None)
    if not (isinstance(module, str) and isinstance(qualname, str)) or module == "__main__":
        raise CodecError("fn 必須是模組層級的函式")
    name = f"{module}:{qualname}"
    try:
        found = _resolve(name)
    except Exception:                        # noqa: BLE001 — 找不回來就不能交給子行程
        raise CodecError("fn 必須是模組層級的函式") from None
    if found is not fn:
        raise CodecError("fn 必須是模組層級的函式")
    return name


# ------------------------------------------------------------------ 子行程
# 子行程的進入點:-I(不讀 PYTHON* 環境變數、不加使用者的 site-packages 與目前目錄),
# 先讀第一行(主行程的 sys.path 等),才找得到本模組
_BOOTSTRAP = ("import json, sys\n"
              "header = json.loads(sys.stdin.buffer.readline().decode('utf-8'))\n"
              "sys.path[:0] = header['path']\n"
              f"from {__name__} import _child_main\n"
              "_child_main(header)\n")


def _set_memory_limit(max_memory_mb) -> None:
    try:
        import resource   # 僅 Unix 有

        limit = int(max_memory_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError, TypeError):
        pass              # Windows 等平台:只剩逾時與各模組內的上限


def _child_main(header: dict) -> None:
    """子行程:先設記憶體上限,再讀參數、執行,結果以 JSON 寫到 stdout。
    fn 自己的輸出(print)改寫到 stderr(主行程丟掉),不混進結果。"""
    out = sys.stdout.buffer
    sys.stdout = sys.stderr
    _set_memory_limit(header.get("mem"))
    try:
        fn = _resolve(header["fn"])
        types = {name: _resolve(name) for name in header["types"]}
        payload = _decode(_loads(sys.stdin.buffer.read()), types)
        value = _encode(fn(*payload["args"], **payload["kwargs"]))
        data = _dumps({"status": "ok", "value": value})
    except MemoryError:
        data = _dumps({"status": "error", "message": "MemoryError: 執行時記憶體不足(超過上限),已中止"})
    except CodecError:
        data = _dumps({"status": "error", "message": "IsolationError: 子行程的參數或結果無法傳遞"})
    except BaseException as e:   # 不帶例外內容,只留類型
        data = _dumps({"status": "error", "message": f"{type(e).__name__}: 子行程執行失敗"})
    out.write(data)
    out.flush()


def _clean_message(message) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(message))
    return text[:MAX_MESSAGE_CHARS]


def run_isolated(fn, args: tuple = (), kwargs: dict | None = None, *,
                 timeout_s: float, max_memory_mb: int, on_failure, types: tuple = ()):
    """在子行程中執行 fn(*args, **kwargs)。

    成功時回傳 fn 的結果;任何失敗(逾時、異常、資源耗盡、回傳格式不對)時回傳
    on_failure(錯誤訊息)。on_failure 只在本行程執行。
    types:參數與結果裡會出現的 dataclass(其他 dataclass 一律不收)。
    """
    allowed = {_type_name(cls): cls for cls in types}
    try:
        header = {"path": [p for p in sys.path if isinstance(p, str)], "fn": _function_name(fn),
                  "types": sorted(allowed), "mem": int(max_memory_mb)}
        body = _dumps(_encode({"args": tuple(args), "kwargs": dict(kwargs or {})}))
        request = _dumps(header) + b"\n" + body
        proc = subprocess.Popen([sys.executable, "-I", "-c", _BOOTSTRAP],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=child_env(), close_fds=True)
    except BaseException as e:
        # 啟動失敗(參數不能傳遞、系統資源不足、環境不允許開子行程…)也要收斂成失敗結果:
        # 公開的 API 都承諾不丟例外,呼叫端只依結果判斷。
        return on_failure(f"IsolationError: 無法啟動子行程({type(e).__name__})")

    received = {"chunks": [], "size": 0, "too_big": False}

    def read():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    return
                received["size"] += len(chunk)
                if received["size"] > MAX_RESULT_BYTES:
                    received["too_big"] = True
                    return
                received["chunks"].append(chunk)
        except (OSError, ValueError):
            return               # 子行程被終止、管線關掉

    def write():
        try:
            proc.stdin.write(request)
            proc.stdin.close()
        except (OSError, ValueError):
            pass                 # 子行程還沒讀完就結束了:結果由讀的那一側判斷

    reader = threading.Thread(target=read, name="isolation-read", daemon=True)
    writer = threading.Thread(target=write, name="isolation-write", daemon=True)
    try:
        reader.start()
        writer.start()
        reader.join(timeout_s)
        timed_out = reader.is_alive()
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass
        reader.join(5)
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    if timed_out:
        return on_failure(f"IsolationError: 執行逾時(超過 {timeout_s} 秒),已強制終止")
    if received["too_big"]:
        return on_failure("IsolationError: 子行程回傳的資料超過上限")
    data = b"".join(received["chunks"])
    if not data:
        return on_failure(
            f"IsolationError: 子行程異常結束(結束代碼 {proc.returncode}),"
            f"可能是資源耗盡或執行環境無法啟動子行程")
    try:
        message = _loads(data)
        if not (isinstance(message, dict) and message.get("status") in ("ok", "error")):
            raise CodecError("形狀不對")
        if message["status"] == "error":
            if set(message) != {"status", "message"} or not isinstance(message["message"], str):
                raise CodecError("形狀不對")
            return on_failure(_clean_message(message["message"]))
        if set(message) != {"status", "value"}:
            raise CodecError("形狀不對")
        return _decode(message["value"], allowed)
    except Exception:            # noqa: BLE001 — 解不開、形狀不對都是失敗
        return on_failure("IsolationError: 子行程回傳了非預期的資料")


# ------------------------------------------------------------------ 背景執行緒

async def run_in_thread(fn, *args, name: str = "segcra-background"):
    """把同步函式丟到背景執行緒跑,讓外層的 await(與 asyncio.wait_for)能準時回來。

    同步的呼叫(pymssql 的沙盒、在別的事件迴圈裡跑的查詢)在跑的時候,如果直接在事件迴圈裡
    呼叫,迴圈整個卡住,外面的 `asyncio.wait_for` **插不進去**:上限 1 秒、沙盒卡 6 秒,結果
    跑了 6 秒、沒有逾時、結果照常回傳。

    * 不用 `asyncio.to_thread`(預設執行緒池):`asyncio.run` 結束時會等池子裡的工作做完,
      等於逾時又被拖回去。自己開 daemon 執行緒,逾時就讓它自己跑完自己結束,外層不等它
    * `contextvars` 要帶進去:審查釘住的 commit(toolbox.gitlab 的 review_ref)存在 ContextVar
      裡,新開的執行緒不會繼承,讀到的會是預設值
    * fn 丟的例外原樣交回 await 端
    * 逾時之後外層不等這個執行緒,它可能在事件迴圈關掉之後才跑完:那時交回結果會丟
      `RuntimeError: Event loop is closed`,發生在 daemon 執行緒裡沒有人接,log 每逾時一次就多
      一段看起來很嚴重的 traceback。結果本來就不要了,**安靜地**放掉
    """
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    ctx = contextvars.copy_context()

    def settle(apply):
        try:
            loop.call_soon_threadsafe(apply)
        except RuntimeError:                  # 迴圈已經關了
            pass

    def runner():
        try:
            out = ctx.run(fn, *args)
        except BaseException as e:            # noqa: BLE001 — 原樣交回給 await 端
            # **要先綁到自己的變數**:`except ... as e` 的 e 在區塊結束時就被刪掉,而這個
            # lambda 是丟給事件迴圈稍後才跑的。直接捕捉 e 的話,跑的時候會丟 NameError,
            # fut 永遠不會有結果,await 就**永遠掛著**。
            exc = e
            settle(lambda: fut.done() or fut.set_exception(exc))
        else:
            settle(lambda: fut.done() or fut.set_result(out))

    threading.Thread(target=runner, name=name, daemon=True).start()
    return await fut
