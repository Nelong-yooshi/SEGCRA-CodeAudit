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


def stale_reason(spec_code: str, spec_text: str, *, model: str) -> str | None:
    """凍結檔存在但對不上時,說明是哪一項對不上;對得上或根本沒凍結過回 None。

    **為什麼要講出來**:對不上時呼叫端會靜靜地改成現場生成——變慢(大規則實測
    54 分鐘)、而且每次結果不同(取樣會飄),但畫面上看不出跟「用凍結檔」有什麼差別。
    改了 TESTGEN prompt 之後整批凍結檔都會對不上,那正是最需要被告知的時候。
    """
    p = _path(spec_code)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return f"凍結檔讀不起來({type(e).__name__})"
    for label, got, want in (("規格內容", data.get("spec_sha256"), _sha(spec_text)),
                             ("TESTGEN prompt", data.get("prompt_sha256"), _prompt_hash()),
                             ("模型", data.get("model"), model)):
        if got != want:
            return f"{label}對不上(凍結檔 {got} / 現在 {want})"
    return None


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
        *, profile: str, model: str, force: bool = False,
        cost: dict | None = None) -> None:
    """存檔前檢查覆蓋度:預設拒絕凍結一份有缺口的測資計畫——不然凍住的
    等於把一次不完整的取樣當成「這條規則以後每次 golden set 都只驗這幾個案例」,
    而且不會再重新抽到比較好的版本。`force=True` 才能覆蓋,明確承擔後果。

    0 缺口也不代表每個條件都驗到了(缺口只看「每個條件有沒有兩向案例」,不看
    反向案例是不是真的只讓那一個條件不成立),存檔後仍要有人看過案例內容。

    `cost` 記這一份**生出來花了多少**:幾次模型呼叫、幾秒、資料列幾列、沙盒跑一輪
    幾秒。為什麼存進檔案而不是只印出來:
      * 這些數字決定「哪些規格適合在線上現場生」「上限該訂多少」,不該每次要用時
        都重跑一輪(大規格一輪 70 分鐘)
      * 黑盒評測要「記下每份規格花多久、有沒有成功」(#45 的討論),凍結檔裡有就
        不必另外維護一份紀錄
      * 它也讓「案例數 vs 偵測能力 vs 時間」這個取捨有據可循——實測 R-140 從
        19 個案例降到 8 個,突變偵測一樣 10/10
    """
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
        "row_count": sum(len(c.get("rows") or []) for c in plan["cases"]),
        "cost": cost or {},
        "plan": plan,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def wrap(generate_cases_fn):
    """回傳一個簽章相容的替身:規格有凍結版就不呼叫 LLM,改用凍結的 plan 重跑
    `_shape_check()` 的結果;沒有就照常呼叫傳進來的真函式。只給呼叫端(eval 工具)
    主動套用,例如 `spec_exec.generate_cases = testdata_cache.wrap(spec_exec.generate_cases)`
    ——不改 spec_exec.py 本身,正式審查(pipeline.py → run_spec_exec)不受影響。"""
    async def wrapped(cfg, spec_code, spec_text, profile_name=None, **kw):
        model = testgen_model(cfg, profile_name)
        cached = load(spec_code, spec_text, model=model)
        if cached is not None:
            return spec_exec._shape_check(cached)
        # 有凍結檔但對不上 → 說出來。靜靜地改成現場生成會讓人以為還在用凍結檔,
        # 而兩者的耗時(大規則 54 分鐘 vs 幾秒)與可重現性完全不同。
        why = stale_reason(spec_code, spec_text, model=model)
        if why:
            print(f"[testdata_cache] {spec_code} 的凍結檔不適用({why})"
                  f"——改為現場生成,這次會變慢、而且結果每次不同。"
                  f"要重新凍結:python eval/freeze_testdata.py {spec_code}", flush=True)
        return await generate_cases_fn(cfg, spec_code, spec_text,
                                       profile_name=profile_name, **kw)
    return wrapped
