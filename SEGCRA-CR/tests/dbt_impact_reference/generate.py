"""產生 macro 反查的標準答案,供 tests/test_dbt_impact.py 對照。

為什麼需要:dbt_impact 是我們自己寫的靜態分析。依賴關係的標準答案由 dbt 官方的
`dbt parse` 產生(manifest.json 的 depends_on),不是我們自己寫的預期值。

產出:
  1. tests/fixtures/dbt_manifest/impact_deps.json
     本目錄對照專案中,每個 model / macro / hook 的直接依賴(dbt 記錄的版本)。
  2. orchestrator/dbt_builtin_macros.py
     dbt-core 與 dbt-sqlserver 內建 macro 的**名稱**(只有名稱,不含程式碼),
     用來判斷專案 macro 是否覆寫了 dbt 內建行為。

一律使用 sqlserver 轉接器(與正式環境相同;dispatch 會解析成 sqlserver__ 開頭的實作)。
`dbt parse` 不連資料庫,不需要 ODBC 連線。

用法(只在對照專案或 dbt 版本變動時手動重跑;CI 不需要 dbt):

    python tests/dbt_impact_reference/generate.py --dbt <dbt 執行檔路徑>

資安:與 tests/dbt_reference/generate.py 相同(假名、白名單環境變數、關閉使用統計、
不經 shell、臨時目錄用完即刪)。
"""
import argparse
import importlib.metadata
import importlib.util
import json
import pathlib
import shutil
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
PKG_ROOT = HERE.parents[1]
OUT_FIXTURE = PKG_ROOT / "tests" / "fixtures" / "dbt_manifest" / "impact_deps.json"
OUT_BUILTINS = PKG_ROOT / "orchestrator" / "dbt_builtin_macros.py"
PROJECT = "segcra_impact"
BUILTIN_PACKAGES = ("dbt", "dbt_sqlserver")

_spec = importlib.util.spec_from_file_location(
    "dbt_reference_generate", HERE.parent / "dbt_reference" / "generate.py")
reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reference)


def project_files() -> dict[str, str]:
    """對照專案的檔案:相對路徑(posix)→ 內容(統一為 LF)。測試以同一份內容做分析。"""
    files = {}
    for p in sorted(HERE.rglob("*")):
        if p.is_file() and p.suffix in (".sql", ".yml"):
            files[p.relative_to(HERE).as_posix()] = reference.read_canonical(p)
    return files


def build_project(tmp: pathlib.Path) -> None:
    reference._write_exact(tmp / "profiles.yml",
                           reference.read_canonical(HERE.parent / "dbt_reference" / "profiles.yml"))
    for rel, text in project_files().items():
        reference._write_exact(tmp / rel, text)


def _short(unique_id: str) -> str:
    """macro.<package>.<name> → <package>.<name>;model.<package>.<name> → <name>。"""
    kind, rest = unique_id.split(".", 1)
    return rest.split(".", 1)[1] if kind == "model" else rest


def extract_deps(manifest: dict) -> dict:
    models, macros, operations = {}, {}, {}
    for node in manifest["nodes"].values():
        if node["package_name"] != PROJECT:
            continue
        path = node["original_file_path"].replace("\\", "/")
        deps = node["depends_on"]
        entry = {"macros": sorted(_short(m) for m in deps["macros"])}
        if node["resource_type"] == "model":
            entry["name"] = node["name"]
            entry["refs"] = sorted(_short(n) for n in deps["nodes"] if n.startswith("model."))
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
    return {
        "dbt_version": manifest["metadata"]["dbt_version"],
        "adapter": manifest["metadata"]["adapter_type"],
        "models": dict(sorted(models.items())),
        "macros": dict(sorted(macros.items())),
        "operations": dict(sorted(operations.items())),
    }


def extract_builtins(manifest: dict) -> list[str]:
    return sorted({m["name"] for m in manifest["macros"].values()
                   if m["package_name"] in BUILTIN_PACKAGES})


# 內建 macro 的來源套件(manifest 的 package dbt 由 dbt-adapters 提供)
SOURCE_DISTRIBUTIONS = ("dbt-core", "dbt-adapters", "dbt-sqlserver")


def _describe(dist: str) -> str:
    """版本與授權一律從套件中繼資料讀取,不手寫(避免授權敘述與實際不符)。"""
    try:
        meta = importlib.metadata.metadata(dist)
    except importlib.metadata.PackageNotFoundError:
        raise SystemExit(f"找不到套件 {dist},無法確認版本與授權")
    license_ = (meta.get("License-Expression") or meta.get("License") or "").strip()
    if not license_:
        classifiers = [c.rsplit("::", 1)[-1].strip() for c in (meta.get_all("Classifier") or [])
                       if c.startswith("License ::")]
        license_ = ", ".join(classifiers)
    if not license_:
        raise SystemExit(f"套件 {dist} 的中繼資料沒有授權資訊,請人工確認後再產生")
    return f"{dist} {meta.get('Version')}({license_})"


def render_builtins_module(names: list[str]) -> str:
    body = "\n".join(f"    {name!r}," for name in names)
    sources = "、".join(_describe(d) for d in SOURCE_DISTRIBUTIONS)
    return (
        '"""dbt 內建 macro 名稱清單(由 tests/dbt_impact_reference/generate.py 自動產生,請勿手動修改)。\n'
        "\n"
        f"來源(版本與授權取自套件中繼資料):{sources}。\n"
        "只收錄 macro 名稱,不含任何程式碼。\n"
        "\n"
        "用途:專案 macro 與這些名稱相同時,代表覆寫了 dbt 內建行為——dbt 在執行任何 model 時\n"
        "都可能呼叫它,專案內卻找不到呼叫點。\n"
        '"""\n'
        "\n"
        "BUILTIN_MACROS = frozenset({\n"
        f"{body}\n"
        "})\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dbt", default="dbt", help="dbt 執行檔路徑(預設取 PATH 上的 dbt)")
    args = ap.parse_args()

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="segcra_dbtref_"))
    try:
        build_project(tmp)
        reference.run_dbt(args.dbt, tmp, ["parse", "--target", "sqlserver"])
        manifest = json.loads((tmp / "target" / "manifest.json").read_bytes().decode("utf-8"))
        deps = extract_deps(manifest)
        reference._write_exact(OUT_FIXTURE,
                               json.dumps(deps, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        reference._write_exact(OUT_BUILTINS, render_builtins_module(extract_builtins(manifest)))
        print(f"已寫入 {OUT_FIXTURE.relative_to(PKG_ROOT)}")
        print(f"已寫入 {OUT_BUILTINS.relative_to(PKG_ROOT)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
