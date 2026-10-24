"""進入點要有 `if __name__ == "__main__":` 保護(review 列的 dbt.enabled 開啟前條件)。

dbt 的展開、macro 反查、專案資訊整理與 lint 都在子行程裡做(multiprocessing 的 spawn)。spawn 會在
子行程重新匯入進入點的模組;進入點把「開始審查」寫在模組最外層的話,每個子行程都會把整支程式
從頭再跑一次。這種問題只在真的啟動時才出現,所以用靜態檢查鎖住。
"""
import ast
import pathlib

import pytest

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
# 一定要有 main 保護的進入點(會啟動審查或 lint 的服務與 CLI)
RUNNABLE = ["demo.py", "scripts/webhook_server.py", "tools/prescan.py", "eval/run_eval.py"]
SCRIPT_DIRS = ("scripts", "tools", "eval")


def _imports_pipeline(tree: ast.AST) -> bool:
    """有沒有 import 管線或工具的模組(用得到就可能開子行程)。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        else:
            continue
        if any(n.split(".")[0] in ("orchestrator", "toolbox") for n in names):
            return True
    return False


def _scripts() -> list[str]:
    """demo.py 與 scripts/、tools/、eval/ 底下 import 了管線或工具的檔案。自動找,不靠手動清單:
    每次審查都會開子行程,新增的腳本漏了保護就會整支重跑。"""
    found = ["demo.py"]
    for d in SCRIPT_DIRS:
        for path in sorted((PKG_ROOT / d).rglob("*.py")):
            if _imports_pipeline(ast.parse(path.read_text(encoding="utf-8"))):
                found.append(path.relative_to(PKG_ROOT).as_posix())
    return found


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


def _is_idempotent_setup(node: ast.AST) -> bool:
    """重跑也無害的設定:`os.environ.setdefault(...)`,以及
    `if hasattr(sys.stdout, "reconfigure"):`(把主控台輸出改成 UTF-8 的固定寫法;不能有 else,
    區塊裡只能是 reconfigure 本身,見 _is_reconfigure_block)。"""
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        func = node.value.func
        return (isinstance(func, ast.Attribute) and func.attr == "setdefault"
                and isinstance(func.value, ast.Attribute) and func.value.attr == "environ"
                and isinstance(func.value.value, ast.Name) and func.value.value.id == "os")
    if isinstance(node, ast.If) and isinstance(node.test, ast.Call):
        call = node.test
        return (isinstance(call.func, ast.Name) and call.func.id == "hasattr"
                and len(call.args) == 2 and _is_sys_stdout(call.args[0])
                and isinstance(call.args[1], ast.Constant) and call.args[1].value == "reconfigure"
                and not node.orelse and all(_is_reconfigure_block(s) for s in node.body))
    return False


def _is_sys_stdout(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "stdout"
            and isinstance(node.value, ast.Name) and node.value.id == "sys")


def _is_reconfigure_call(stmt: ast.AST) -> bool:
    """`sys.stdout.reconfigure(...)` 這一句,別的都不算。"""
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    func = stmt.value.func
    return isinstance(func, ast.Attribute) and func.attr == "reconfigure" and _is_sys_stdout(func.value)


def _is_reconfigure_block(stmt: ast.AST) -> bool:
    """`if hasattr(sys.stdout, "reconfigure"):` 底下只能是 `sys.stdout.reconfigure(...)`,或是包著它、
    例外時只 `pass` 的 `try`。區塊裡多寫任何會執行的東西(例如 `main()`)都不放行。"""
    if _is_reconfigure_call(stmt):
        return True
    return (isinstance(stmt, ast.Try) and not stmt.orelse and not stmt.finalbody
            and bool(stmt.body) and all(_is_reconfigure_call(s) for s in stmt.body)
            and all(all(isinstance(s, ast.Pass) for s in h.body) for h in stmt.handlers))


def _unguarded_calls(source: str) -> list[int]:
    """模組最外層、不在 main 保護裡、而且會執行東西的敘述所在的行號。
    允許:匯入、函式 / 類別定義、指派(常數、設定)、docstring、sys.path 的設定、
    重跑也無害的設定(見 _is_idempotent_setup)。"""
    lines = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Assign, ast.AnnAssign)) or _is_main_guard(node):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue                         # docstring
        if _is_sys_path_setup(node) or _is_idempotent_setup(node):
            continue
        lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("rel", _scripts())
def test_腳本的最外層不直接執行(rel):
    source = (PKG_ROOT / rel).read_text(encoding="utf-8")
    assert _unguarded_calls(source) == [], f"{rel} 的最外層有會直接執行的敘述"


@pytest.mark.parametrize("rel", RUNNABLE)
def test_進入點有main保護(rel):
    source = (PKG_ROOT / rel).read_text(encoding="utf-8")
    assert any(_is_main_guard(n) for n in ast.parse(source).body), f"{rel} 沒有 main 保護"


def test_自動找得到已知的進入點():
    """自動找的清單是空的(例如目錄改名)時,上面那條會靜靜地什麼都不檢查。"""
    assert set(RUNNABLE) <= set(_scripts())


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
    idem = ("import os, sys\nos.environ.setdefault('A', '1')\n"
            "if hasattr(sys.stdout, 'reconfigure'):\n    sys.stdout.reconfigure(encoding='utf-8')\n"
            "os.environ.update(A='1')\n"
            "if hasattr(sys.stdout, 'x'):\n    pass\nelse:\n    serve()\n"
            "if hasattr(os, 'x'):\n    serve()\n")
    assert _unguarded_calls(idem) == [5, 6, 10]       # 只放行 setdefault 與 stdout 的設定


@pytest.mark.parametrize("body", [
    "    sys.stdout.reconfigure(encoding='utf-8')\n",
    "    try:\n        sys.stdout.reconfigure(encoding='utf-8')\n    except Exception:\n        pass\n",
    "    try:\n        sys.stdout.reconfigure(encoding='utf-8')\n    except (OSError, ValueError):\n"
    "        pass\n    except Exception:\n        pass\n",
])
def test_stdout_設定的固定寫法放行(body):
    assert _unguarded_calls("import sys\nif hasattr(sys.stdout, 'reconfigure'):\n" + body) == []


@pytest.mark.parametrize("body", [
    "    sys.stdout.reconfigure(encoding='utf-8')\n    main()\n",            # 區塊裡多寫一行
    "    main()\n",
    "    try:\n        sys.stdout.reconfigure(encoding='utf-8')\n        main()\n"
    "    except Exception:\n        pass\n",
    "    try:\n        sys.stdout.reconfigure(encoding='utf-8')\n    except Exception:\n"
    "        main()\n",
    "    try:\n        sys.stdout.reconfigure(encoding='utf-8')\n    except Exception:\n"
    "        pass\n    else:\n        main()\n",
    "    try:\n        sys.stdout.reconfigure(encoding='utf-8')\n    finally:\n        main()\n",
    "    sys.stderr.reconfigure(encoding='utf-8')\n",
    "    other.stdout.reconfigure(encoding='utf-8')\n",
])
def test_stdout_設定的區塊裡夾帶別的就不放行(body):
    assert _unguarded_calls("import sys\nif hasattr(sys.stdout, 'reconfigure'):\n" + body) == [2]


@pytest.mark.parametrize("test", ["hasattr(sys.stdout, 'x')", "hasattr(sys.stdout)",
                                  "hasattr(sys.stderr, 'reconfigure')"])
def test_stdout_設定的條件要是固定寫法(test):
    src = f"import sys\nif {test}:\n    sys.stdout.reconfigure(encoding='utf-8')\n"
    assert _unguarded_calls(src) == [2]
