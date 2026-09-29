"""testdata_cache — 凍結/重用 generate_cases() 的產出,只給 eval 用,不碰正式審查路徑。

背景:`generate_cases()`(spec_exec 角色一,測資生成)是整條管線裡隨機性最大的一段
(見 `eval/BASELINE.md`「R-140 測資生成二選一」)。`--dump-reports`/`--from-reports`
存的是**整份審查報告**,把主審查(review LLM)與測資生成兩層隨機性一起凍住;
這裡只凍測資生成那一層——跑 golden set 時主審查照常即時呼叫,只有測資計畫固定,
這樣才能把「主審查有沒有跟著改動而變」跟「測資生成本身飄不飄」分開量,也能在
不等 15-20 分鐘/case 的情況下反覆跑 spec_exec 後半段(執行、仲裁、決策)的邏輯。

用法:
  1. python eval/freeze_testdata.py R-140       # 生成、驗證 0 缺口才存檔
  2. python eval/run_eval.py --frozen-testdata  # 有凍結檔的規格一律吃凍結版,
                                                 # 不呼叫測資生成的 LLM;沒凍結的規格照常呼叫

凍結檔存在 eval/testdata_cache/<規則碼>.json,以規格內容的雜湊當快取鍵——
規格改過,雜湊對不上,自動視為沒有凍結(不會用到過期的測資計畫)。

只在呼叫端明確套用 wrap() 時才生效(見 run_eval.py 的 --frozen-testdata),
不改 orchestrator/spec_exec.py 本身,正式審查管線的行為不受影響。
"""
import hashlib
import json
import time
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
CACHE_DIR = EVAL_DIR / "testdata_cache"


def _spec_hash(spec_text: str) -> str:
    return hashlib.sha256(spec_text.encode("utf-8")).hexdigest()[:12]


def _path(spec_code: str) -> Path:
    return CACHE_DIR / f"{spec_code}.json"


def load(spec_code: str, spec_text: str) -> dict | None:
    """規格內容雜湊對得上才回傳凍結的 plan;規格改過或沒凍結過都回 None
    (呼叫端接下來該照常呼叫真的 generate_cases())。"""
    p = _path(spec_code)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("spec_sha256") != _spec_hash(spec_text):
        return None
    return data["plan"]


def save(spec_code: str, spec_text: str, plan: dict, gaps: list[str],
        *, profile: str, force: bool = False) -> None:
    """存檔前檢查覆蓋度:預設拒絕凍結一份有缺口的測資計畫——不然凍住的
    等於把一次不完整的取樣當成「這條規則以後每次 golden set 都只驗這幾個案例」,
    而且不會再重新抽到比較好的版本。`force=True` 才能覆蓋,明確承擔後果。"""
    if gaps and not force:
        raise ValueError(
            f"這份測資計畫有 {len(gaps)} 個覆蓋缺口,預設不凍結:{'; '.join(gaps)}\n"
            "確定要凍結不完整的版本,加 --force。")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _path(spec_code).write_text(json.dumps({
        "spec_code": spec_code,
        "spec_sha256": _spec_hash(spec_text),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "profile": profile,
        "coverage_gaps": gaps,
        "case_count": len(plan["cases"]),
        "plan": plan,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def wrap(generate_cases_fn):
    """回傳一個簽章相容的替身:規格有凍結版就直接回傳(不呼叫 LLM、gaps/dropped
    皆為空),沒有就照常呼叫傳進來的真函式。只給呼叫端(eval 工具)主動套用,
    例如 `spec_exec.generate_cases = testdata_cache.wrap(spec_exec.generate_cases)`
    ——不改 spec_exec.py 本身,正式審查(pipeline.py → run_spec_exec)不受影響。"""
    async def wrapped(cfg, spec_code, spec_text, profile_name=None, **kw):
        cached = load(spec_code, spec_text)
        if cached is not None:
            return cached, [], []
        return await generate_cases_fn(cfg, spec_code, spec_text,
                                       profile_name=profile_name, **kw)
    return wrapped
