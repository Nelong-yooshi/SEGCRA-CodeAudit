"""以固定種子產生多個隨機 dbt 專案,用 `dbt parse` 取得依賴的標準答案(差異測試)。

為什麼需要:手寫的對照專案只涵蓋「我們想得到的寫法」。隨機組合 macro 呼叫 macro、
命名空間呼叫、dispatch、當成值傳遞、post_hook 字串、專案 hook,再請 dbt 本身判定
依賴,可以找出我們沒想到的組合。反查結果必須涵蓋 dbt 判定的全部依賴。

產生的專案都保證 dbt 解析得過:macro 只呼叫編號比自己大的 macro(無循環),
被 dispatch 的 macro 只定義 default__ 實作且一律以 dispatch 呼叫。
名稱全為無意義的假名(m0、M0、rnd_project)。

產出:tests/fixtures/dbt_manifest/random_projects.json(每個專案的檔案內容 + dbt 依賴)

平常不需要重跑(CI 只讀 fixture,不需要 dbt);只有改了產生規則或升級 dbt 時才重跑:

    python tests/dbt_impact_reference/random_projects.py --dbt <dbt 執行檔的絕對路徑>
"""
import argparse
import importlib.util
import json
import pathlib
import random
import shutil
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
PKG_ROOT = HERE.parents[1]
OUT = PKG_ROOT / "tests" / "fixtures" / "dbt_manifest" / "random_projects.json"
PROJECT = "rnd_project"
SEED = 20260918
N_PROJECTS = 10

_spec = importlib.util.spec_from_file_location("dbt_impact_reference_generate", HERE / "generate.py")
_impact = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_impact)
reference = _impact.reference


def _call(target: int, style: str, dispatched: set) -> str:
    """產生一次 macro 呼叫;style 涵蓋 dbt 常見的幾種寫法。"""
    name = f"m{target}"
    if target in dispatched:
        return f"{{{{ adapter.dispatch('{name}')() }}}}"
    if style == "namespace":
        return f"{{{{ {PROJECT}.{name}() }}}}"
    if style == "value":                      # 當成值傳遞:dbt 自己的依賴紀錄看不到
        return f"{{% set f_{name} = {name} %}}{{{{ f_{name}() }}}}"
    return f"{{{{ {name}() }}}}"


def make_project(rng: random.Random) -> dict[str, str]:
    n_macros = rng.randint(3, 9)
    dispatched = set(rng.sample(range(n_macros), k=rng.randint(0, min(2, n_macros))))
    styles = ["direct", "namespace", "value"]
    macro_files: dict[str, list] = {}
    for i in range(n_macros):
        calls = [_call(j, rng.choice(styles), dispatched)
                 for j in range(i + 1, n_macros) if rng.random() < 0.3]
        body = " ".join(calls) or "1"
        name = f"default__m{i}" if i in dispatched else f"m{i}"
        path = f"macros/{rng.choice(['a', 'b', 'c/d'])}/f{rng.randint(0, 3)}.sql"
        macro_files.setdefault(path, []).append(f"{{% macro {name}() %}}{body}{{% endmacro %}}")

    files = {p: "\n".join(defs) + "\n" for p, defs in sorted(macro_files.items())}
    for k in range(rng.randint(2, 6)):
        parts = []
        if rng.random() < 0.3:                # macro 藏在 post_hook 字串裡
            hook = _call(rng.randrange(n_macros), "direct", dispatched).replace("'", "\\'")
            parts.append(f"{{{{ config(post_hook='{hook}') }}}}")
        calls = [_call(j, rng.choice(styles), dispatched)
                 for j in range(n_macros) if rng.random() < 0.25]
        parts.append(f"SELECT {k} AS c {' '.join(calls)}")
        folder = rng.choice(["models", "models/sub"])
        files[f"{folder}/M{k}.sql"] = "\n".join(parts) + "\n"

    hook_lines = ""
    if rng.random() < 0.3:                    # macro 藏在專案設定的 on-run-end
        hook = _call(rng.randrange(n_macros), "direct", dispatched).replace('"', '\\"')
        hook_lines = f'on-run-end:\n  - "{hook}"\n'
    files["dbt_project.yml"] = (
        f'name: {PROJECT}\nversion: "1.0.0"\nconfig-version: 2\nprofile: segcra_reference\n'
        f'model-paths: ["models"]\nmacro-paths: ["macros"]\n{hook_lines}'
        "flags:\n  send_anonymous_usage_stats: false\n")
    return files


def _short(unique_id: str) -> str:
    kind, rest = unique_id.split(".", 1)
    return rest.split(".", 1)[1] if kind == "model" else rest


def extract_deps(manifest: dict) -> dict:
    """從 manifest 取出 dbt 判定的依賴(只留本專案的)。"""
    models, macros, operations = {}, {}, {}
    for node in manifest["nodes"].values():
        if node["package_name"] != PROJECT:
            continue
        path = node["original_file_path"].replace("\\", "/")
        entry = {"macros": sorted(_short(m) for m in node["depends_on"]["macros"])}
        if node["resource_type"] == "model":
            models[path] = entry
        elif node["resource_type"] == "operation":
            operations[node["name"]] = entry
    for macro in manifest["macros"].values():
        if macro["package_name"] != PROJECT:
            continue
        macros[macro["name"]] = {
            "path": macro["original_file_path"].replace("\\", "/"),
            "macros": sorted(_short(m) for m in macro["depends_on"]["macros"]),
        }
    return {"models": dict(sorted(models.items())), "macros": dict(sorted(macros.items())),
            "operations": dict(sorted(operations.items()))}


def parse_project(dbt: str, files: dict[str, str]) -> dict:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sgc_rnd_"))
    try:
        reference._write_exact(tmp / "profiles.yml",
                               reference.read_canonical(PKG_ROOT / "tests" / "dbt_reference"
                                                        / "profiles.yml"))
        for rel, text in files.items():
            reference._write_exact(tmp / rel, text)
        reference.run_dbt(dbt, tmp, ["parse", "--target", "sqlserver"])
        manifest = json.loads((tmp / "target" / "manifest.json").read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return extract_deps(manifest)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbt", default="dbt", help="dbt 執行檔(請給絕對路徑)")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--count", type=int, default=N_PROJECTS)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    projects = []
    for i in range(args.count):
        files = make_project(rng)
        projects.append({"files": files, "deps": parse_project(args.dbt, files)})
        print(f"  專案 {i + 1}/{args.count} 已解析")
    payload = {"seed": args.seed, "project": PROJECT, "projects": projects}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"已寫入 {OUT.relative_to(PKG_ROOT)}")


if __name__ == "__main__":
    main()
