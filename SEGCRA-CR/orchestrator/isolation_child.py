"""isolation_child — 隔離子行程裡實際跑的那一側:讀參數、執行、把結果寫回去。

主行程那一側(啟動子行程、限制環境變數與時間、讀回結果)在 isolation.py。拆開是為了讓
tests/test_dbt_render.py 的 import 白名單管得到子行程裡跑的程式:子行程處理的是不可信的內容
(Jinja 沙箱萬一有逃逸漏洞,樣板就是在這裡執行的),這一支不能有網路、指令執行、寫檔的能力。
主行程那一側要啟動子行程,本來就需要 subprocess,所以不在白名單裡(理由寫在白名單旁邊)。

這裡放:
  * JSON 編解碼(_encode / _decode):參數與結果一律用 JSON 傳遞,不用 pickle——子行程被攻陷時,
    傳回來的資料也不能在主行程執行程式。只認得基本型別、清單、tuple、集合、dict 與呼叫端列出的
    dataclass(types=),欄位名稱要完全一致;其他一律當成失敗。主行程讀回結果時用同一份
  * 照「模組:名稱」找回函式(_resolve):要跑的函式與 dataclass 由主行程指定
  * 子行程的進入點(_child_main):先設記憶體上限,再讀參數、執行、寫回結果

import 白名單:dataclasses、json、math、pathlib(編解碼)、importlib(照名稱找回函式)、
sys(讀 stdin、寫 stdout)、resource(記憶體上限,僅 Unix)。
"""
import dataclasses
import importlib
import json
import math
import pathlib
import sys

# 傳遞的資料最多巢狀幾層
MAX_DEPTH = 64


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


# ------------------------------------------------------------------ 子行程的進入點
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
