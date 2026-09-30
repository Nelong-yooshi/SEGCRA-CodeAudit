"""突變測試工具(tests/tools/mutate.py)本身的測試。

這個工具判斷「被抓到」的依據是測試套件回傳碼非 0。只要複製出去的專案少了
測試需要的檔案、或逾時比正常跑一次還短,**每個突變都會被誤判成被抓到**,
而且沒有任何警訊——曾經因為漏複製 config/、memory/、eval/ 而發生過。
工具壞掉時沒人會發現,所以它的關鍵行為也要有測試守著。
"""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "mutate_tool", pathlib.Path(__file__).resolve().parent / "tools" / "mutate.py")
mutate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mutate)


def _touch(path: pathlib.Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_copy_project_copies_every_directory_tests_may_depend_on(tmp_path):
    """不是列舉「測試會用到哪些目錄」:新測試依賴到新目錄時也要自動帶上。"""
    src, dst = tmp_path / "src", tmp_path / "dst"
    for rel in ("orchestrator/a.py", "config/models.yaml", "memory/knowledge/k.md",
                "eval/run_eval.py", "specs/R-1.md", "some_new_dir/x.txt", "conftest.py"):
        _touch(src / rel)
    dst.mkdir()
    mutate.copy_project(src, dst)
    for rel in ("orchestrator/a.py", "config/models.yaml", "memory/knowledge/k.md",
                "eval/run_eval.py", "specs/R-1.md", "some_new_dir/x.txt", "conftest.py"):
        assert (dst / rel).is_file(), rel


def test_copy_project_excludes_credentials_and_build_artifacts(tmp_path):
    """憑證不可被複製到暫存目錄;範本(.env.example)要保留。"""
    src, dst = tmp_path / "src", tmp_path / "dst"
    for rel in ("config/sandbox.env", "config/gitlab.env", "config/sandbox.env.example",
                ".venv/lib/x.py", ".venv-dbt/lib/x.py", "orchestrator/__pycache__/a.pyc",
                "review_output/r.json", "eval/_output/o.json", ".git/HEAD", "x.duckdb"):
        _touch(src / rel)
    dst.mkdir()
    mutate.copy_project(src, dst)
    for rel in ("config/sandbox.env", "config/gitlab.env", ".venv", ".venv-dbt",
                "orchestrator/__pycache__", "review_output", "eval/_output", ".git",
                "x.duckdb"):
        assert not (dst / rel).exists(), rel
    assert (dst / "config/sandbox.env.example").is_file()


def test_effective_timeout_never_shorter_than_three_baselines():
    """逾時比正常跑一次還短時,存活的突變會因跑不完被誤判成被抓到。"""
    assert mutate._effective_timeout(240, 266) >= 266 * 3      # WSL 實測約 266 秒
    assert mutate._effective_timeout(240, 20) == 240           # 夠快就用使用者給的值


def test_every_mutation_target_appears_exactly_once():
    """每個突變點在目標檔案裡必須剛好出現一次,否則改的根本不是想改的那行
    (工具執行時也會檢查,但那要跑很久才看得到;這裡秒級就擋下)。"""
    root = pathlib.Path(__file__).resolve().parents[1] / "orchestrator"
    bad = []
    entries = [(label, entry if len(entry) == 3 else ("dbt_render.py", *entry))
               for label, entry in mutate.MUTANTS.items()]
    entries.append(("金絲雀", mutate.CANARY))
    for label, (module, old, _new) in entries:
        count = (root / module).read_text(encoding="utf-8").count(old)
        if count != 1:
            bad.append(f"{label}({module}:{count} 次)")
    assert bad == []


def test_tool_never_runs_its_own_tests_inside_mutants():
    """本檔的「突變點剛好出現一次」在突變後必然失敗;若被捲進每個突變的測試套件,
    **每個突變都會被誤判成被抓到**(曾發生過,164 個全部「被抓」)。"""
    assert "--ignore=tests/test_mutate_tool.py" in mutate.PYTEST_ARGS
    assert pathlib.Path(__file__).name == "test_mutate_tool.py"


def test_canary_is_behavior_neutral():
    """金絲雀只改註解:改完後程式仍可匯入、行為不變——它才有資格當「必須存活」的對照。"""
    module, old, new = mutate.CANARY
    assert old.lstrip().startswith("#") and new.lstrip().startswith("#")


def test_every_mutant_produces_valid_python():
    """突變後若是語法錯誤,模組連匯入都失敗,任何測試都會紅——看起來「被抓到」,
    其實什麼行為都沒驗到(「隔離:啟動失敗不收斂」曾經就是這樣)。"""
    import ast
    root = pathlib.Path(__file__).resolve().parents[1] / "orchestrator"
    bad = []
    entries = [(label, entry if len(entry) == 3 else ("dbt_render.py", *entry))
               for label, entry in mutate.MUTANTS.items()]
    entries.append(("金絲雀", mutate.CANARY))
    for label, (module, old, new) in entries:
        text = (root / module).read_text(encoding="utf-8")
        try:
            ast.parse(text.replace(old, new))
        except SyntaxError as e:
            bad.append(f"{label}(第 {e.lineno} 行)")
    assert bad == []
