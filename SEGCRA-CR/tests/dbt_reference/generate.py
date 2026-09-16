"""產生 dbt compile 的標準答案,供 tests/test_dbt_render.py 逐字對照。

為什麼需要:dbt_render 是我們自己實作的樣板展開。測試若只拿自己寫的預期值比,
等於自己改自己的考卷;這裡用 dbt 官方的 `dbt compile` 產出答案,展開器必須與它
逐字一致。

對照對象(reference_sources(),測試與本腳本共用同一份定義):
  * 範例 model
  * 本目錄 models/probe_*.sql——針對特定樣板構造(空白處理、do / 迴圈控制、
    incremental、source)的探測檔
  * 由探測檔衍生的變體(VARIANTS):帶 BOM、CRLF 換行——Windows 編輯器常見的存檔格式
新增探測檔或變體後需重跑本腳本。

用法(只在範例 model / macro / 探測檔變動時手動重跑;CI 不需要 dbt):

    python tests/dbt_reference/generate.py --dbt <dbt 執行檔路徑> --target duckdb

--target:
  duckdb     不需伺服器與驅動,任何機器可跑(需 pip install dbt-duckdb)
  sqlserver  與正式環境同轉接器,但需要 ODBC Driver 18 與可連線的 SQL Server
             (帳密由 SEGCRA_REF_DB_USER / SEGCRA_REF_DB_PASSWORD 環境變數提供)
兩者的標準答案分別存在 tests/fixtures/dbt_compiled/<target>/,測試會逐一比對。

流程:
  1. 在臨時目錄組出完整 dbt 專案 = 本目錄的骨架(去識別化的 profile / sources / 樁 model)
     + examples/sample 的範例 model 與 macros(直接複製,repo 內不存第二份)
     + 衍生變體。所有檔案先統一為 LF 再寫入,結果不隨 git 的換行設定而變。
  2. `dbt compile --no-introspect`:只展開樣板,不連資料庫。
  3. 把編譯結果原樣寫到 tests/fixtures/dbt_compiled/<target>/。
  4. 刪除臨時目錄(dbt 的 target/、logs/ 不留在 repo)。

資安:
  * 名稱全為假名;帳密只在子行程環境變數內填入無意義的佔位值,不寫進任何檔案。
  * 只把必要的環境變數傳給 dbt(白名單),不整包轉交——避免使用者環境裡的其他
    憑證被 dbt 或其外掛讀到、或出現在錯誤輸出中。
  * 關閉 dbt 匿名使用統計(DO_NOT_TRACK + 專案 flags),不對外送資料。
  * 子行程以參數清單呼叫,不經 shell。
"""
import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
PKG_ROOT = HERE.parents[1]
SAMPLE = PKG_ROOT / "examples" / "sample" / "RETAIL_M1_code"
OUT_DIR = PKG_ROOT / "tests" / "fixtures" / "dbt_compiled"

MODEL = "mrt_RETAIL_M1"
# 範例 model 的 var("target_date") 沒有預設值,dbt 不帶 --vars 會編譯失敗
VARS = '{"target_date": "2026-02-01"}'

# 衍生變體:名稱 → (來源探測檔, 轉換)
VARIANTS = {
    "probe_bom": ("probe_control", "bom"),
    "probe_crlf": ("probe_whitespace", "crlf"),
}

# 傳給 dbt 子行程的環境變數白名單
_ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "LANG", "LC_ALL", "VIRTUAL_ENV",
    "SEGCRA_REF_DB_USER", "SEGCRA_REF_DB_PASSWORD",
)


def read_canonical(path: pathlib.Path) -> str:
    """讀檔並統一為 LF(不受 git autocrlf 影響);BOM 等其餘位元組原樣保留。"""
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def apply_variant(text: str, kind: str) -> str:
    if kind == "bom":
        return "﻿" + text
    if kind == "crlf":
        return text.replace("\n", "\r\n")
    raise ValueError(f"未知的變體:{kind}")


def reference_sources() -> dict[str, str]:
    """對照用原始碼:名稱 → 內容。測試以同一份內容展開並與標準答案比對。"""
    sources = {MODEL: read_canonical(SAMPLE / f"{MODEL}.sql")}
    for p in sorted((HERE / "models").glob("probe_*.sql")):
        sources[p.stem] = read_canonical(p)
    for name, (base, kind) in VARIANTS.items():
        sources[name] = apply_variant(sources[base], kind)
    return dict(sorted(sources.items()))


def _write_exact(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def build_project(tmp: pathlib.Path) -> None:
    for name in ("dbt_project.yml", "profiles.yml"):
        _write_exact(tmp / name, read_canonical(HERE / name))
    for p in (HERE / "models").iterdir():
        if p.suffix in (".sql", ".yml") and not p.stem.startswith("probe_"):
            _write_exact(tmp / "models" / p.name, read_canonical(p))
    for name, text in reference_sources().items():
        _write_exact(tmp / "models" / f"{name}.sql", text)
    for p in (SAMPLE / "macros").rglob("*.sql"):
        _write_exact(tmp / p.relative_to(SAMPLE), read_canonical(p))


def run_compile(dbt: str, tmp: pathlib.Path, target: str) -> None:
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.update({
        "DO_NOT_TRACK": "1",
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        # dbt 讀專案檔時不指定編碼;中文 Windows 預設 cp950,讀含中文註解的檔會失敗
        "PYTHONUTF8": "1",
        "DBT_LOG_PATH": str(tmp / "logs"),
    })
    # 未提供帳密時填佔位值,讓 profile 解析得過(duckdb 目標根本用不到)。
    env.setdefault("SEGCRA_REF_DB_USER", "unused")
    env.setdefault("SEGCRA_REF_DB_PASSWORD", "unused")
    cmd = [dbt, "compile", "--no-introspect", "--target", target,
           "--project-dir", str(tmp), "--profiles-dir", str(tmp),
           "--vars", VARS, "--select", *reference_sources()]
    proc = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        sys.stderr.write((proc.stdout or "")[-4000:] + (proc.stderr or "")[-4000:])
        raise SystemExit(f"dbt compile 失敗(exit {proc.returncode})")


def collect(tmp: pathlib.Path, target: str) -> list[pathlib.Path]:
    compiled = tmp / "target" / "compiled" / "segcra_reference" / "models"
    out = OUT_DIR / target
    written = []
    for name in reference_sources():
        # Jinja 會把展開結果的換行統一成 \n,所以結果本身不含 \r;檔案裡若出現 \r\n,
        # 是 dbt 在 Windows 以文字模式寫檔造成的。還原它,標準答案才不隨產生的平台而變。
        text = (compiled / f"{name}.sql").read_bytes().decode("utf-8").replace("\r\n", "\n")
        dest = out / f"{name}.sql"
        _write_exact(dest, text)
        written.append(dest)
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dbt", default="dbt", help="dbt 執行檔路徑(預設取 PATH 上的 dbt)")
    ap.add_argument("--target", choices=["duckdb", "sqlserver"], required=True)
    args = ap.parse_args()

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="segcra_dbtref_"))
    try:
        build_project(tmp)
        run_compile(args.dbt, tmp, args.target)
        for path in collect(tmp, args.target):
            print(f"已寫入 {path.relative_to(PKG_ROOT)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
