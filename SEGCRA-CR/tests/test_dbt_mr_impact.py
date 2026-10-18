"""MR → macro 反查的轉接層(orchestrator/dbt_mr_impact.py,#15 第 2 點)。

不連網:以假的 load(回傳準備好的 DbtProject)取代 load_dbt_project。重點是:
路徑換算、以 head / base 比對判斷新增、改名與刪除的舊路徑也列入、專案設定檔變更與目錄
設定不同時保守處理,以及**任何失敗都是「全部 model 可能受影響」,絕不是「沒有影響」**。
"""
import ast
import pathlib

import pytest

from orchestrator.dbt_impact import ImpactReport, analyze_macro_impact
from orchestrator.dbt_mr_impact import analyze_mr_macro_impact
from orchestrator.dbt_project import DbtProject

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
HEAD = "a" * 40
BASE = "b" * 40
MARK = "SECRETCONTENT"

MACRO_V1 = "{% macro fmt(x) %}{{ x }}{% endmacro %}"
MACRO_V2 = "{% macro fmt(x) %}upper({{ x }}){% endmacro %}"
BASE_FILES = {
    "models/a.sql": "select {{ fmt('c') }} from t",
    "models/b.sql": "select 1",
    "macros/fmt.sql": MACRO_V1,
}
ROOT = {"dbt_project.yml": "name: shop\n"}


def _project(files, model_paths=("models",), macro_paths=("macros",), root=None, names=()):
    return DbtProject(ok=True, files=dict(files), model_paths=model_paths,
                      macro_paths=macro_paths, root_files=dict(root or ROOT),
                      seed_paths=("seeds",), snapshot_paths=("snapshots",), names=tuple(names))


class _Load:
    """假的 load_dbt_project:依 sha 回傳準備好的專案,記錄呼叫。"""

    def __init__(self, head, base):
        self.by_sha = {HEAD: head, BASE: base}
        self.calls = []

    def __call__(self, sha, project_dir=""):
        self.calls.append((sha, project_dir))
        return self.by_sha.get(sha, DbtProject(ok=False, error="DbtProjectError: commit 編號不合法"))


class _Analyze:
    """記錄交給反查的參數;回傳空的結果(用來單獨檢查轉接層的換算)。"""

    def __init__(self, result=None):
        self.calls = []
        self.result = result if result is not None else ImpactReport()

    def __call__(self, files, changed, **kwargs):
        self.calls.append({"files": files, "changed": changed, **kwargs})
        return self.result


def _diff(*changes, truncated=False, sha=HEAD):
    """changes:path 或 (path, old_path)。"""
    files = [{"path": c, "old_path": c} if isinstance(c, str) else {"path": c[0], "old_path": c[1]}
             for c in changes]
    return {"sha": sha, "files": files, "truncated": truncated}


def _run(diff, head=None, base=None, project_dir="", analyze=None):
    load = _Load(head or _project(BASE_FILES), base or _project(BASE_FILES))
    analyze = analyze or _Analyze()
    report = analyze_mr_macro_impact(diff, BASE, project_dir, load=load, analyze=analyze)
    return report, load, analyze


def _fails_closed(report, reason_part=None):
    assert report.all_models_possibly_affected and report.needs_human
    assert all(MARK not in r for r in report.uncertain)
    if reason_part:
        assert any(reason_part in r for r in report.uncertain), report.uncertain


# ------------------------------------------------------------------ 換算與比對
def test_changed_macro_is_traced_end_to_end():
    """真的反查(同一行程版本):改了 macro,引用它的 model 被找出來。"""
    head = _project(BASE_FILES | {"macros/fmt.sql": MACRO_V2})
    report, _, _ = _run(_diff("macros/fmt.sql"), head=head, analyze=analyze_macro_impact)
    assert [m.path for m in report.affected_models] == ["models/a.sql"]
    assert not report.needs_human


def test_inputs_given_to_the_analysis():
    head = _project(BASE_FILES | {"macros/fmt.sql": MACRO_V2})
    _, load, analyze = _run(_diff("macros/fmt.sql"), head=head)
    assert load.calls == [(HEAD, ""), (BASE, "")]
    (call,) = analyze.calls
    # 反查也要看 dbt_project.yml(hook 可能呼叫被改的 macro)
    assert call["files"] == head.files | ROOT and call["changed"] == ["macros/fmt.sql"]
    assert call["base_files"] == BASE_FILES | ROOT and call["added_paths"] == []
    assert call["model_dirs"] == ("models",) and call["macro_dirs"] == ("macros",)


def test_added_file_is_detected_by_comparing_head_and_base():
    head = _project(BASE_FILES | {"macros/new.sql": "{% macro n() %}{% endmacro %}"})
    _, _, analyze = _run(_diff("macros/new.sql", "macros/fmt.sql"), head=head)
    assert analyze.calls[0]["added_paths"] == ["macros/new.sql"]


def test_rename_lists_both_old_and_new_paths():
    """改名:舊路徑的 macro 在變更後消失,要一併列入,反查才看得到「被刪掉的定義」。"""
    head_files = {k: v for k, v in BASE_FILES.items() if k != "macros/fmt.sql"}
    head = _project(head_files | {"macros/format.sql": MACRO_V1})
    _, _, analyze = _run(_diff(("macros/format.sql", "macros/fmt.sql")), head=head)
    assert analyze.calls[0]["changed"] == ["macros/fmt.sql", "macros/format.sql"]
    assert analyze.calls[0]["added_paths"] == ["macros/format.sql"]


def test_deleted_macro_is_traced_end_to_end():
    head = _project({k: v for k, v in BASE_FILES.items() if k != "macros/fmt.sql"})
    report, _, _ = _run(_diff("macros/fmt.sql"), head=head, analyze=analyze_macro_impact)
    assert "models/a.sql" in [m.path for m in report.affected_models]


def test_project_in_subdirectory_paths_are_relative_to_it():
    # dbt_macros/x.sql:前綴少比對一個斜線的話會被切成 macros/x.sql,剛好落在 dbt 的目錄裡
    _, load, analyze = _run(_diff("dbt/macros/fmt.sql", "app/main.py", "dbt_other/x.sql",
                                  "dbt_macros/x.sql"), project_dir="dbt/")
    assert load.calls == [(HEAD, "dbt/"), (BASE, "dbt/")]
    assert analyze.calls[0]["changed"] == ["macros/fmt.sql"]


def test_changes_outside_the_project_do_not_touch_gitlab():
    """專案目錄外的變更與 dbt 無關:不下載、不反查,回傳沒有影響。"""
    report, load, analyze = _run(_diff("app/main.py", "README.md"), project_dir="dbt")
    assert report == ImpactReport() and not report.needs_human
    assert load.calls == [] and analyze.calls == []


def test_empty_change_list_is_no_impact():
    report, load, _ = _run(_diff())
    assert report == ImpactReport() and load.calls == []


# ------------------------------------------------------------------ 保守處理
@pytest.mark.parametrize("name", ["dbt_project.yml", "packages.yml", "dependencies.yml"])
def test_project_setting_change_affects_everything(name):
    report, _, analyze = _run(_diff(name))
    _fails_closed(report, name)
    assert analyze.calls                         # 仍然跑反查,其餘結果照常保留


def test_nested_file_named_like_a_setting_is_not_a_setting_change():
    report, _, _ = _run(_diff("models/packages.yml"))
    assert not report.all_models_possibly_affected


def test_setting_change_keeps_analysis_findings():
    found = ImpactReport(uncertain=("反查自己的理由",), changed_macros=("fmt",))
    report, _, _ = _run(_diff("dbt_project.yml", "macros/fmt.sql"), analyze=_Analyze(found))
    assert report.changed_macros == ("fmt",)
    assert report.uncertain[0] == "反查自己的理由" and len(report.uncertain) == 2


@pytest.mark.parametrize("head_kw", [{"model_paths": ("transform",)}, {"macro_paths": ("lib",)}])
def test_different_layout_gives_no_base_and_affects_everything(head_kw):
    """目錄設定不同:兩邊的檔案對不起來,不能拿 base 當變更前內容(也不能據此判斷新增)。"""
    report, _, analyze = _run(_diff("macros/fmt.sql"), head=_project(BASE_FILES, **head_kw))
    _fails_closed(report, "目錄設定不同")
    assert analyze.calls[0]["base_files"] is None and analyze.calls[0]["added_paths"] == []


@pytest.mark.parametrize("truncated", [True, None, "false", 0])
def test_incomplete_change_list_fails_closed(truncated):
    """只有明確的 truncated=False 才算完整:少看一個變更檔,反查就可能說「沒有影響」。"""
    report, load, _ = _run(_diff("app/x.py", truncated=truncated))
    _fails_closed(report, "不完整")
    assert load.calls == []


def test_missing_truncated_flag_fails_closed():
    diff = _diff("macros/fmt.sql")
    del diff["truncated"]
    _fails_closed(_run(diff)[0], "不完整")


@pytest.mark.parametrize("diff", [None, [], "x", {"files": "x", "truncated": False},
                                  {"files": [1], "truncated": False},
                                  {"files": [{"old_path": "a"}], "truncated": False}],
                         ids=["none", "list", "str", "files-str", "item-int", "no-path"])
def test_malformed_diff_fails_closed(diff):
    _fails_closed(_run(diff)[0])


@pytest.mark.parametrize("path", ["../etc/passwd", "/abs.sql", "a\\b.sql", "c:x.sql",
                                  "a/./b.sql", "a\x00b", "a‮b.sql", "", 5])
def test_bad_path_in_diff_fails_closed(path):
    """看不懂的路徑不能略過:略過就少算一個變更檔。"""
    for item in ({"path": path, "old_path": path}, {"path": "ok.sql", "old_path": path}):
        diff = {"sha": HEAD, "files": [item], "truncated": False}
        report, load, _ = _run(diff)
        _fails_closed(report, "不合法的路徑")
        assert load.calls == []


@pytest.mark.parametrize("project_dir", ["../x", "/abs", "a b", None, 1])
def test_bad_project_dir_fails_even_without_dbt_changes(project_dir):
    """設定錯的專案目錄不能被當成「專案內沒有變更 → 沒有影響」藏起來。"""
    report, load, _ = _run(_diff("app/main.py"), project_dir=project_dir)
    _fails_closed(report, "dbt.project_dir")
    assert load.calls == []


@pytest.mark.parametrize("which", ["head", "base"])
def test_load_failure_fails_closed_with_its_reason(which):
    bad = DbtProject(ok=False, error="ArchiveDownloadError: GitLab 回應 HTTP 403")
    kw = {which: bad}
    report, _, analyze = _run(_diff("macros/fmt.sql"), **kw)
    _fails_closed(report, "HTTP 403")
    assert analyze.calls == []


def test_missing_head_sha_fails_closed():
    _fails_closed(_run(_diff("macros/fmt.sql", sha=None))[0])


def test_unexpected_exception_is_not_echoed():
    def boom(*a, **k):
        raise RuntimeError(f"boom {MARK}")
    report = analyze_mr_macro_impact(_diff("macros/fmt.sql"), BASE,
                                     load=_Load(_project(BASE_FILES), _project(BASE_FILES)),
                                     analyze=boom)
    _fails_closed(report, "RuntimeError")


def test_non_report_from_analysis_fails_closed():
    # 要由型別檢查擋下(訊息固定),不是靠後面取屬性失敗才落到「非預期錯誤」
    _fails_closed(_run(_diff("macros/fmt.sql"), analyze=_Analyze(result={"ok": True}))[0],
                  "非預期的資料")


def test_reasons_are_single_line_and_bounded():
    bad = DbtProject(ok=False, error="x\n[INFO] review passed\r\n" + "A" * 1000)
    report, _, _ = _run(_diff("macros/fmt.sql"), head=bad)
    (reason,) = report.uncertain
    assert "\n" not in reason and "\r" not in reason and len(reason) <= 300


def test_default_analysis_is_the_isolated_one():
    """待審的 MR 內容一律在子行程反查(預設參數不可換成同一行程的版本)。"""
    import inspect

    from orchestrator import dbt_impact, dbt_project
    sig = inspect.signature(analyze_mr_macro_impact)
    assert sig.parameters["analyze"].default is dbt_impact.analyze_macro_impact_isolated
    assert sig.parameters["load"].default is dbt_project.load_dbt_project


# ------------------------------------------------------------------ #20 review 對齊
SNAPSHOT = "{% snapshot snap_txn %}select {{ fmt('c') }} from t{% endsnapshot %}"


@pytest.mark.parametrize("path, new", [
    ("seeds/codes.csv", None),                                              # 只有名稱、沒有內容
    ("snapshots/s.sql", "{% snapshot snap_txn %}select 2{% endsnapshot %}"),
    ("seeds/props.yml", "version: 2\nseeds:\n  - name: codes\n"),
    ("models/schema.yaml", "version: 2\nmodels:\n  - name: a\n    description: x\n"),
], ids=["seed-csv", "snapshot-sql", "seed-props", "model-props-yaml"])
def test_seed_snapshot_and_yaml_changes_are_never_reported_as_no_impact(path, new):
    """#20 review 確認過的行為:seed、snapshot、.yaml 的變更標成不確定(交人工)。
    snapshot / seed 的內容不交給反查之後,這點仍要成立(走真正的反查)。"""
    files = BASE_FILES | {"snapshots/s.sql": SNAPSHOT, "seeds/props.yml": "version: 2\n",
                          "models/schema.yaml": "version: 2\n"}
    head_files = dict(files) if new is None else files | {path: new}
    names = ("seeds/codes.csv", "snapshots/s.sql")
    report, _, _ = _run(_diff(path), head=_project(head_files, names=names),
                        base=_project(files, names=names), analyze=analyze_macro_impact)
    assert report.needs_human and report.uncertain


def test_typical_dbt_project_yml_keeps_the_result_precise():
    """一般的 dbt_project.yml(設定、env_var、var、呼叫別的 macro 的 hook)交給反查,
    不會讓每個 macro 變更都變成「全部 model 受影響」。"""
    root = {"dbt_project.yml": (
        "name: shop\nversion: '1.0'\nprofile: shop\nvars:\n  start: '2024-01-01'\n"
        "models:\n  shop:\n    +materialized: view\n"
        "    +schema: \"{{ env_var('DBT_SCHEMA', 'dbo') }}\"\n"
        "on-run-end:\n  - \"{{ log_run() }}\"\n")}
    head = _project(BASE_FILES | {"macros/fmt.sql": MACRO_V2}, root=root)
    base = _project(BASE_FILES, root=root)
    report, _, _ = _run(_diff("macros/fmt.sql"), head=head, base=base, analyze=analyze_macro_impact)
    assert not report.all_models_possibly_affected and not report.needs_human
    assert [m.path for m in report.affected_models] == ["models/a.sql"]


def test_hook_in_dbt_project_yml_calling_changed_macro_affects_every_model():
    """反查要看 dbt_project.yml 的 hook:它呼叫被改的 macro 時全部 model 都受影響。
    (以前轉接層只給 model / macro 目錄的檔案,hook 看不到,會回報「只影響 a.sql」。)"""
    root = {"dbt_project.yml": "name: shop\non-run-start:\n  - \"{{ fmt('x') }}\"\n"}
    head = _project(BASE_FILES | {"macros/fmt.sql": MACRO_V2}, root=root)
    base = _project(BASE_FILES, root=root)
    report, _, _ = _run(_diff("macros/fmt.sql"), head=head, base=base, analyze=analyze_macro_impact)
    assert report.all_models_possibly_affected and report.needs_human


def test_seed_contents_are_not_given_to_the_analysis_but_snapshots_are():
    """seed 目錄的內容不交給反查(會被當成目錄外的檔案);snapshot 也是 .sql 樣板、也會呼叫
    macro,交給反查追。"""
    files = BASE_FILES | {"snapshots/s.sql": SNAPSHOT, "seeds/props.yml": "version: 2\n"}
    _, _, analyze = _run(_diff("models/b.sql"), head=_project(files), base=_project(files))
    assert set(analyze.calls[0]["files"]) == set(BASE_FILES) | {"dbt_project.yml", "snapshots/s.sql"}
    assert analyze.calls[0]["snapshot_dirs"] == ("snapshots",)


HELPER = "{% macro wrap(x) %}{{ fmt(x) }}{% endmacro %}"


@pytest.mark.parametrize("snapshot, change, uncertain", [
    (SNAPSHOT, "macros/fmt.sql", True),                                   # 直接呼叫被改的 macro
    ("{% snapshot snap_txn %}select {{ wrap('c') }} from t{% endsnapshot %}",
     "macros/fmt.sql", True),                                             # 經由另一個 macro 呼叫
    ("{% snapshot snap_txn %}select c from t{% endsnapshot %}", "macros/fmt.sql", False),
    (SNAPSHOT, "models/b.sql", False),                                    # 只改 model
    ("{% snapshot snap_txn %}select {{ fmt( from t{% endsnapshot %}", "macros/fmt.sql", True),
], ids=["calls-changed-macro", "calls-through-helper", "does-not-call", "model-only", "unparsable"])
def test_snapshot_is_uncertain_only_when_it_calls_the_changed_macro(snapshot, change, uncertain):
    """以前專案裡只要有 snapshot,所有改到 macro 的 MR 都標不確定(major):乾淨的 macro 重構
    與真的有問題的修改拿到一樣的結果。現在 snapshot 也交給反查:呼叫到(直接或間接)被改的
    macro、或看不懂時才標不確定。不標「全部 model 可能受影響」:受影響的是 snapshot。"""
    head_files = BASE_FILES | {"macros/fmt.sql": MACRO_V2, "macros/wrap.sql": HELPER,
                               "models/b.sql": "select 2", "snapshots/s.sql": snapshot}
    base_files = BASE_FILES | {"macros/wrap.sql": HELPER, "snapshots/s.sql": snapshot}
    names = ("snapshots/s.sql",)
    report, _, _ = _run(_diff(change), head=_project(head_files, names=names),
                        base=_project(base_files, names=names), analyze=analyze_macro_impact)
    snapshot_reason = any("snapshot" in r for r in report.uncertain)
    assert snapshot_reason is uncertain, report.uncertain
    assert not report.all_models_possibly_affected


def test_snapshot_in_custom_directory_is_not_reported_as_stray_sql():
    files = BASE_FILES | {"macros/fmt.sql": MACRO_V2, "snaps/s.sql": "{% snapshot s %}select 1{% endsnapshot %}"}
    head = DbtProject(ok=True, files=files, model_paths=("models",), macro_paths=("macros",),
                      root_files=dict(ROOT), snapshot_paths=("snaps",), names=("snaps/s.sql",))
    base = DbtProject(ok=True, files={**files, "macros/fmt.sql": MACRO_V1}, model_paths=("models",),
                      macro_paths=("macros",), root_files=dict(ROOT), snapshot_paths=("snaps",),
                      names=("snaps/s.sql",))
    report, _, _ = _run(_diff("macros/fmt.sql"), head=head, base=base, analyze=analyze_macro_impact)
    assert report.uncertain == ()


# ------------------------------------------------------------------ 模組能力
def test_module_imports_are_allowlisted():
    imported = set()
    tree = ast.parse((PKG_ROOT / "orchestrator" / "dbt_mr_impact.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or "").split(".")[0])
    assert imported == {"dataclasses", ".dbt_impact", ".dbt_project"}


def test_module_never_reads_files_or_renders():
    tree = ast.parse((PKG_ROOT / "orchestrator" / "dbt_mr_impact.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"open", "eval", "exec", "compile", "__import__"}
    assert not attrs & {"read_text", "write_text", "open", "system", "popen", "render",
                        "from_string", "get_template"}


def test_snapshot的屬性檔呼叫被改的macro_也標不確定():
    """snapshot 的 post_hook 可以寫在 snapshot 目錄的屬性檔裡,效果跟寫在 .sql 裡一樣。"""
    props = "version: 2\nsnapshots:\n  - name: snap_txn\n    config:\n      post_hook: \"{{ fmt('x') }}\"\n"
    head_files = BASE_FILES | {"macros/fmt.sql": MACRO_V2, "snapshots/props.yml": props,
                               "snapshots/s.sql": "{% snapshot snap_txn %}select 1{% endsnapshot %}"}
    base_files = BASE_FILES | {"snapshots/props.yml": props,
                               "snapshots/s.sql": "{% snapshot snap_txn %}select 1{% endsnapshot %}"}
    report, _, _ = _run(_diff("macros/fmt.sql"), head=_project(head_files), base=_project(base_files),
                        analyze=analyze_macro_impact)
    assert any("snapshots/props.yml" in r for r in report.uncertain), report.uncertain
    assert not report.all_models_possibly_affected


def test_自訂的seed目錄_改到資料檔也標不確定():
    """seed 目錄改名(seed-paths)時,反查照專案的設定判斷:改到資料檔跟預設的 seeds/ 一樣交人工,
    不會因為目錄名稱不是 seeds 就當成跟 dbt 無關。"""
    def project(names):
        return DbtProject(ok=True, files=dict(BASE_FILES), model_paths=("models",),
                          macro_paths=("macros",), root_files=dict(ROOT), seed_paths=("data",),
                          snapshot_paths=("snapshots",), names=names)
    report, _, _ = _run(_diff("data/codes.csv"), head=project(("data/codes.csv",)),
                        base=project(("data/codes.csv",)), analyze=analyze_macro_impact)
    assert report.needs_human and any("data/codes.csv" in r for r in report.uncertain), report.uncertain


def test_反查拿到專案的資源目錄設定():
    head = DbtProject(ok=True, files=dict(BASE_FILES), model_paths=("models",), macro_paths=("macros",),
                      root_files=dict(ROOT), seed_paths=("data",), snapshot_paths=("snaps",),
                      analysis_paths=("an",), test_paths=("checks",))
    _, _, analyze = _run(_diff("macros/fmt.sql"), head=head, base=head)
    assert analyze.calls[0]["resource_dirs"] == ("data", "snaps", "an", "checks")
