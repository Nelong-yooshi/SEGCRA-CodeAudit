"""進入點要有 `if __name__ == "__main__":` 保護(review 列的 dbt.enabled 開啟前條件)。

dbt 的展開、macro 反查、專案資訊整理都在子行程裡做(multiprocessing 的 spawn)。spawn 會在
子行程重新匯入進入點的模組;進入點把「開始審查」寫在模組最外層的話,每個子行程都會把整支程式
從頭再跑一次。這種問題只在真的啟動時才出現,所以用靜態檢查鎖住。
"""
import ast
import pathlib

import pytest

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
ENTRYPOINTS = ["demo.py", "scripts/webhook_server.py"]


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    names = [test.left, *test.comparators]
    return (len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and any(isinstance(n, ast.Name) and n.id == "__name__" for n in names)
            and any(isinstance(n, ast.Constant) and n.value == "__main__" for n in names))


def _is_sys_path_setup(node: ast.AST) -> bool:
    """`sys.path.insert(...)` / `sys.path.append(...)`:讓腳本找得到套件的設定,重跑也無害。"""
    call = node.value if isinstance(node, ast.Expr) else None
    func = call.func if isinstance(call, ast.Call) else None
    return (isinstance(func, ast.Attribute) and func.attr in ("insert", "append")
            and isinstance(func.value, ast.Attribute) and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name) and func.value.value.id == "sys")


def _unguarded_calls(source: str) -> list[int]:
    """模組最外層、不在 main 保護裡、而且會執行東西的敘述所在的行號。
    允許:匯入、函式 / 類別定義、指派(常數、設定)、docstring、sys.path 的設定。"""
    lines = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Assign, ast.AnnAssign)) or _is_main_guard(node):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue                         # docstring
        if _is_sys_path_setup(node):
            continue
        lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("rel", ENTRYPOINTS)
def test_進入點有main保護_最外層不直接執行(rel):
    source = (PKG_ROOT / rel).read_text(encoding="utf-8")
    assert any(_is_main_guard(n) for n in ast.parse(source).body), f"{rel} 沒有 main 保護"
    assert _unguarded_calls(source) == [], f"{rel} 的最外層有會直接執行的敘述"


def test_檢查本身抓得到沒有保護的寫法():
    bad = "import asyncio\n\ndef main():\n    pass\n\nmain()\n"
    assert _unguarded_calls(bad) == [6]
    assert not any(_is_main_guard(n) for n in ast.parse(bad).body)
    good = 'import asyncio\n\ndef main():\n    pass\n\nif __name__ == "__main__":\n    main()\n'
    assert _unguarded_calls(good) == [] and any(_is_main_guard(n) for n in ast.parse(good).body)
    reversed_form = 'if "__main__" == __name__:\n    pass\n'
    assert any(_is_main_guard(n) for n in ast.parse(reversed_form).body)
    other_if = 'import os\nif os.name == "nt":\n    print(1)\n'
    assert _unguarded_calls(other_if) == [2]
    setup = "import sys\nsys.path.insert(0, '.')\nsys.path.append('x')\nsys.exit(1)\nserve()\n"
    assert _unguarded_calls(setup) == [4, 5]          # 只放行 sys.path 的設定
