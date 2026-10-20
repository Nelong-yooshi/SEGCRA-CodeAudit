"""冷讀的單獨入口:輸入 MR(fixtures/ 的 JSON 格式),每份輸出一行 JSON:判定與位置。

    .venv/bin/python tools/cold_read.py fixtures/mr_003.json [更多檔案 ...]

輸出不含模型挑出的原文片段(只有 flagged、位置、有沒有截斷),給評測只跑冷讀這支用。
要連模型端點(config/models.yaml 的 endpoint 與 cold_read profile)。任何一份失敗時結束碼是 1,
那一份輸出 `{"file": ..., "error": 例外類型}`,不帶例外內容。
"""
import argparse
import asyncio
import json
import pathlib
import sys

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))

from orchestrator.cold_read import cold_read, summary_json  # noqa: E402
from orchestrator.config import load_config  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="冷讀:判斷 MR 文字有沒有在對審查者說話(只輸出判定與位置)")
    ap.add_argument("files", nargs="+", help="MR 的 JSON 檔(fixtures/ 的格式)")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # pragma: no cover - 特殊終端
            pass
    cfg = load_config()
    code = 0
    for name in args.files:
        try:
            mr = json.loads(pathlib.Path(name).read_text(encoding="utf-8"))
            result = asyncio.run(cold_read(cfg, mr))
            line = {"file": name, **json.loads(summary_json(result))}
        except Exception as e:              # noqa: BLE001 — 一份失敗不影響其他份
            line = {"file": name, "error": type(e).__name__}
            code = 1
        print(json.dumps(line, ensure_ascii=False), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
