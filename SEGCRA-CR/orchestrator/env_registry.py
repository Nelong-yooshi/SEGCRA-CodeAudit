"""程式裡讀到的環境變數有哪些 —— 用語法樹掃出來,給文件的一致性檢查用。

**為什麼不能用字串比對**:有六個參數是透過 `config.py` 的 helper 讀的
(`env_seconds("SPEC_EXEC_TIMEOUT_SECONDS", 1200)` 之類),那幾個檔案裡根本沒有
`os.environ` 字樣。只認 `os.environ` / `os.getenv` 的掃描會漏掉它們 —— 而漏掉的
正好包含「完全沒有文件」的那兩個時間上限,於是一致性檢查變成一條永遠綠的測試。
"""
from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
# 掃描範圍:會在正式執行時跑到的程式。tests/ 自己設的環境變數不算設定參數。
SCAN_DIRS = ("orchestrator", "toolbox", "scripts", "eval")
# 讀環境變數的 helper(定義在 config.py)。名稱前面可能有底線(模組內私有的版本)。
ENV_HELPERS = frozenset({"env_seconds", "env_float", "env_seed", "env_effort",
                         "env_int", "env_bool"})


def const_value(expr: str | None):
    """能在原始碼裡直接算出來的常數值;算不出來回 None。

    `ast.literal_eval` 不吃 `str(1024 * 1024)` 這種寫法,但那正是要對照文件的數字
    (單檔上限 1048576)。所以自己折一層:數字的四則運算與 `str(...)` 包一層。
    路徑、`section.get(...)` 這種由別的東西算出來的,照樣算不出來。
    """
    if expr is None:
        return None

    def fold(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp):
            a, b = fold(node.left), fold(node.right)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                for t, f in ((ast.Add, lambda: a + b), (ast.Sub, lambda: a - b),
                             (ast.Mult, lambda: a * b), (ast.FloorDiv, lambda: a // b)):
                    if isinstance(node.op, t):
                        return f()
            return None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("str", "int", "float") and len(node.args) == 1):
            inner = fold(node.args[0])
            return None if inner is None else {"str": str, "int": int, "float": float}[
                node.func.id](inner)
        return None

    try:
        return fold(ast.parse(expr, mode="eval").body)
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError):
        return None


@dataclass(frozen=True)
class EnvRead:
    name: str            # 環境變數名稱
    where: str           # 相對 repo 根目錄的路徑:行號
    default: str | None  # 原始碼裡寫的預設值(表達式原文);沒給是 None
    # setdefault 是「設給它」不是「讀不到時用這個」:評測在 run_eval.py 把 LLM_SEED
    # 設成 42,那不是程式的預設值。名稱照樣要收(它也是一個設定參數),但不拿來對照文件。
    is_setter: bool = False

    @property
    def default_value(self):
        """預設值算得出來的話回那個值,否則 None。"""
        return None if self.is_setter else const_value(self.default)


def _helper_name(func_src: str) -> str:
    return func_src.split(".")[-1].lstrip("_")


def scan_file(path: pathlib.Path, rel: str | None = None) -> list[EnvRead]:
    """一個檔案裡所有「讀環境變數」的地方。"""
    rel = rel or str(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    out = []
    for node in ast.walk(tree):
        # os.environ["X"](`from os import environ` 的 environ["X"] 也算:比對結尾就好,
        # 寫死 "os.environ" 的話,換個 import 寫法就整個掃不到)
        if (isinstance(node, ast.Subscript)
                and ast.unparse(node.value).split(".")[-1] == "environ"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            out.append(EnvRead(node.slice.value, f"{rel}:{node.lineno}", None))
            continue
        if not isinstance(node, ast.Call):
            continue
        func = ast.unparse(node.func)
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        name = node.args[0].value
        if not isinstance(name, str):
            continue
        default = ast.unparse(node.args[1]) if len(node.args) > 1 else None
        # 取結尾比對,不寫死 "os." 開頭:`from os import environ`、`import os as o`
        # 都還是同一件事,而寫死前綴的話換個 import 寫法就整個掃不到。
        tail = ".".join(func.split(".")[-2:])
        setter = tail == "environ.setdefault"
        if (tail == "environ.get" or func.split(".")[-1] == "getenv" or setter
                or _helper_name(func) in ENV_HELPERS):
            out.append(EnvRead(name, f"{rel}:{node.lineno}", default, setter))
    return out


def scan_repo(root: pathlib.Path | None = None) -> dict[str, list[EnvRead]]:
    """{環境變數名稱: [讀取的地方…]},照名稱排序。"""
    root = root or PKG_ROOT
    found: dict[str, list[EnvRead]] = {}
    for d in SCAN_DIRS:
        for f in sorted((root / d).rglob("*.py")):
            for read in scan_file(f, str(f.relative_to(root))):
                found.setdefault(read.name, []).append(read)
    return dict(sorted(found.items()))
