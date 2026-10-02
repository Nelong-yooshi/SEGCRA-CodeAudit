"""testdata_cache — 凍結/重用 generate_cases() 的產出,只給 eval 用,不碰正式審查路徑。

背景:`generate_cases()`(spec_exec 角色一,測資生成)是整條管線裡隨機性最大的一段
(見 `eval/BASELINE.md`「R-140 測資生成二選一」)。`--dump-reports`/`--from-reports`
存的是**整份審查報告**,把主審查(review LLM)與測資生成兩層隨機性一起凍住;
這裡只凍測資生成那一層——跑 golden set 時主審查照常即時呼叫,只有測資計畫固定,
這樣才能把「主審查有沒有跟著改動而變」跟「測資生成本身飄不飄」分開量,也能在
不等 15-20 分鐘/case 的情況下反覆跑 spec_exec 後半段(執行、仲裁、決策)的邏輯。

用法:
  1. python eval/freeze_testdata.py R-140       # 生成、驗證 0 缺口才存檔
     python eval/check_frozen_testdata.py R-140 # 沙盒上確認寫錯的 SQL 都抓得到,再 commit
  2. python eval/run_eval.py --frozen-testdata  # 有凍結檔的規格一律吃凍結版,
                                                 # 不呼叫測資生成的 LLM;沒凍結的規格照常呼叫

凍結檔存在 eval/testdata_cache/<規則碼>.json。快取鍵是三樣東西,任一對不上
就視為沒有凍結(不會用到過期的測資計畫):
  * 規格內容的雜湊——規格改過,案例驗的就不是現在的規格
  * 測資生成 system prompt 的雜湊——prompt 改過,現在的生成器不會產出這批案例
  * 模型名稱——換了模型同理
沒記 prompt 雜湊或模型名稱的舊檔一律當作沒凍結,要重新凍結。

讀回時**再跑一次 `_shape_check()`**,回傳它算出的缺口與剔除案例,不是一律回空:
  * `--force` 凍結的檔本來就帶缺口,讀回歸零等於讓評測以為覆蓋完整
  * 凍結檔是任何 PR 都改得到的檔案,讀回時重驗形狀,手改壞的案例會被剔除並看得到

**凍結檔要進版控**(與 `eval/golden/`、`specs/` 同類,不是暫存檔):
  * 大家吃同一批案例,跑出來的數字才能互相比較;各自凍各自的等於各量各的
  * 「這條規則以後都用這批案例驗」值得有人看過——進版控才進得了 PR
  * 因此它也是**受測內容本身**,已列入 baseline 指紋的 `testset.frozen`
    (`eval/preflight.py:check_testset`):換了凍結檔,兩份 baseline 不可比。
    沒凍結任何規格時 `frozen_count` 為 0,不影響既有 baseline。

只在呼叫端明確套用 wrap() 時才生效(見 run_eval.py 的 --frozen-testdata),
不改 orchestrator/spec_exec.py 本身,正式審查管線的行為不受影響。
"""
import hashlib
import json
import time
from pathlib import Path

from orchestrator import spec_exec

EVAL_DIR = Path(__file__).resolve().parent
CACHE_DIR = EVAL_DIR / "testdata_cache"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _prompt_hash() -> str:
    return _sha(spec_exec.testgen_system())


def _path(spec_code: str) -> Path:
    return CACHE_DIR / f"{spec_code}.json"


def testgen_model(cfg, profile_name: str | None = None) -> str:
    """測資生成實際會用的模型名稱,取法與 `generate_cases()` 相同。"""
    prof = cfg.role_profile("testgen") if profile_name is None else cfg.profile(profile_name)
    return prof.model


def load(spec_code: str, spec_text: str, *, model: str) -> dict | None:
    """規格、prompt、模型三者都對得上才回傳凍結的 plan(原樣,未經形狀檢查);
    任一對不上、沒凍結過或檔案壞掉都回 None(呼叫端接下來該照常呼叫真的
    generate_cases())。"""
    p = _path(spec_code)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if (data.get("spec_sha256") != _sha(spec_text)
            or data.get("prompt_sha256") != _prompt_hash()
            or data.get("model") != model):
        return None
    return data.get("plan")


def save(spec_code: str, spec_text: str, plan: dict, gaps: list[str],
        *, profile: str, model: str, force: bool = False) -> None:
    """存檔前檢查覆蓋度:預設拒絕凍結一份有缺口的測資計畫——不然凍住的
    等於把一次不完整的取樣當成「這條規則以後每次 golden set 都只驗這幾個案例」,
    而且不會再重新抽到比較好的版本。`force=True` 才能覆蓋,明確承擔後果。

    0 缺口也不代表每個條件都驗到了(缺口只看「每個條件有沒有兩向案例」,不看
    反向案例是不是真的只讓那一個條件不成立),存檔後仍要有人看過案例內容。"""
    if gaps and not force:
        raise ValueError(
            f"這份測資計畫有 {len(gaps)} 個覆蓋缺口,預設不凍結:{'; '.join(gaps)}\n"
            "確定要凍結不完整的版本,加 --force。")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _path(spec_code).write_text(json.dumps({
        "spec_code": spec_code,
        "spec_sha256": _sha(spec_text),
        "prompt_sha256": _prompt_hash(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "profile": profile,
        "model": model,
        "coverage_gaps": gaps,
        "case_count": len(plan["cases"]),
        "plan": plan,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def wrap(generate_cases_fn):
    """回傳一個簽章相容的替身:規格有凍結版就不呼叫 LLM,改用凍結的 plan 重跑
    `_shape_check()` 的結果;沒有就照常呼叫傳進來的真函式。只給呼叫端(eval 工具)
    主動套用,例如 `spec_exec.generate_cases = testdata_cache.wrap(spec_exec.generate_cases)`
    ——不改 spec_exec.py 本身,正式審查(pipeline.py → run_spec_exec)不受影響。"""
    async def wrapped(cfg, spec_code, spec_text, profile_name=None, **kw):
        cached = load(spec_code, spec_text, model=testgen_model(cfg, profile_name))
        if cached is not None:
            return spec_exec._shape_check(cached)
        return await generate_cases_fn(cfg, spec_code, spec_text,
                                       profile_name=profile_name, **kw)
    return wrapped
