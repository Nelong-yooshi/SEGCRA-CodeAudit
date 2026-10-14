"""dbt_affected — MR 改到 macro 時,檢查受影響的 model(#15 接線;#7 第 3 點)。

macro 反查(dbt_context 準備好的 ImpactReport)只說「哪些 model 受影響」。這裡對那些
**不在 MR 變更清單裡**的 model 做確定性的檢查,整理成一份結果給後處理補報:

  * 展開變更後的版本,跑規則;同一個 model 再用變更前的專案展開一次、跑規則,**只回報
    變更後多出來的規則命中**。不比較的話,受影響的 model 原本就有的問題(例如早就核准過的
    寫法)會在每一個改到 macro 的 MR 上重報一次,其中若有 blocker 還會把無關的 MR 擋下
  * 變更後展開失敗、或展開後無法解析:macro 的變更可能把它弄壞了,要看得到
  * 對應到的規格(依檔名;#7 驗收「只改 macro 的 MR,能對應到受影響的 model 與規格」)

這些 model 的內容不放進預掃結果、也不放進模型的 prompt:它們不在 diff 裡,放進預掃會讓
每個檢核點(H 系列)各補一條「模型未回應」,放進 prompt 會佔掉預算並多一個注入的入口。
它們對到的**規格**會放進 prompt(specs_by_priority;#38 第 2 點):規格是核定的內容,
不是待審的程式。

上限(都算「沒檢查完」,由後處理補 major,不可以當成沒有問題):
  * 每個 MR 最多檢查 MAX_CHECKED_MODELS 個受影響的 model(每個要開兩次子行程)
  * 整段檢查最多 MAX_SECONDS 秒
"""
import dataclasses
import re
import time
from collections import Counter
from dataclasses import dataclass, field

from .config import DBT_DATABASE_PLACEHOLDER
from .dbt_context import DbtContext
from .dbt_impact import ImpactReport
from .dbt_render import render_model_isolated

MAX_CHECKED_MODELS = 50
MAX_SPEC_LOOKUPS = 20          # 規格逐一向 GitLab 探測,數量另外設限
MAX_SECONDS = 120.0
# 這幾種嚴重度的規則命中算「問題」,會影響這條 finding 的嚴重度;hint 是給模型的檢核點,
# 多出來的只列出來給人看(受影響的 model 不在 diff 裡,沒有人會替它回應檢核點)
_COMPARED = ("blocker", "major", "minor")


@dataclass(frozen=True)
class CheckedModel:
    path: str                              # 相對 dbt 專案根目錄
    name: str
    certain: bool                          # 反查是否確定它受影響
    chain: tuple[str, ...] = ()
    spec: str | None = None                # 對應到的規格代號;None = 沒找到或沒查
    spec_looked_up: bool = False
    # 變更後多出來的規則命中 [(規則代號, 嚴重度)]
    new_hits: tuple[tuple[str, str], ...] = ()
    new_hints: tuple[str, ...] = ()        # 變更後多出來的檢核點(規則代號)
    # 變更前的版本展不開 / 解析不了,無法比較:new_hits 是變更後的全部命中
    no_baseline: bool = False
    error: str | None = None               # 變更後展開失敗或無法解析的原因


@dataclass(frozen=True)
class AffectedCheck:
    impact: ImpactReport
    models: tuple[CheckedModel, ...] = ()
    unchecked: tuple[str, ...] = ()        # 受影響但沒檢查到的 model(超過數量或時間上限)
    spec: tuple[str, str] | None = None    # 受影響的 model 全部對到同一份規格時:(代號, 內容)
    specs_found: dict = field(default_factory=dict)   # 代號 → 內容


@dataclass(frozen=True)
class SpecForModels:
    """一份規格,以及對到它的受影響 model(專案內的相對路徑)。"""
    code: str
    text: str
    models: tuple[str, ...]
    mentioned: bool = False                # 代號或 model 名稱出現在這次的 diff 裡


def specs_by_priority(check: AffectedCheck | None, diff_text: str) -> tuple[SpecForModels, ...]:
    """受影響的 model 對到的規格,依「這次改到的值會影響到哪幾份」排序(#38 第 2 點)。

    排序只決定預算不夠時先放哪幾份,不影響判斷:
      1. 規格代號或 model 名稱出現在這次的 diff 裡(含上下文行)。共用的註冊表 macro 常以
         規格代號當鍵(內附範例的 `'RETAIL_M1': {...}`),改到哪一段,diff 附近通常就有那個鍵
      2. 直接呼叫被改 macro 的 model(呼叫鏈較短)排在間接呼叫的前面
      3. 其餘照路徑
    名稱比對要前後都不是英數字或底線,短名稱(例如 `a`)不會因為出現在別的字裡就算。
    """
    if check is None:
        return ()
    groups: dict[str, list[CheckedModel]] = {}
    for m in check.models:
        if m.spec and m.spec in check.specs_found:
            groups.setdefault(m.spec, []).append(m)
    lowered = (diff_text or "").lower()

    def appears(word: str) -> bool:
        return bool(word) and re.search(
            rf"(?<![0-9a-z_]){re.escape(word.lower())}(?![0-9a-z_])", lowered) is not None

    specs = []
    for code, models in groups.items():
        models = sorted(models, key=lambda m: m.path)
        specs.append((
            (not (appears(code) or any(appears(m.name) for m in models)),
             min(len(m.chain) for m in models), models[0].path),
            SpecForModels(code=code, text=check.specs_found[code],
                          models=tuple(m.path for m in models),
                          mentioned=appears(code) or any(appears(m.name) for m in models))))
    return tuple(spec for _, spec in sorted(specs, key=lambda item: item[0]))


async def check_affected_models(hub, mr: dict, dbt_cfg: dict, ctx: DbtContext | None, *,
                                find_spec, render=render_model_isolated,
                                clock=time.monotonic) -> AffectedCheck | None:
    """回傳 None = 沒有 macro 反查的結果(沒開 dbt、mock 模式、MR 在專案裡只改到 model)。

    find_spec  spec_exec.find_spec(由呼叫端傳入,避免本模組依賴執行驗證)
    render / clock  測試用
    """
    if ctx is None or ctx.impact is None:
        return None
    try:
        return await _check(hub, mr, dbt_cfg, ctx, find_spec, render, clock)
    except Exception as e:                   # noqa: BLE001 — 審查不因為這一步中斷,但也不能當成沒事
        return AffectedCheck(impact=dataclasses.replace(
            ctx.impact, all_models_possibly_affected=True,
            uncertain=ctx.impact.uncertain + (
                f"檢查受影響的 model 時發生非預期錯誤({type(e).__name__}),沒有完成檢查",)))


async def _check(hub, mr: dict, dbt_cfg: dict, ctx: DbtContext, find_spec, render,
                 clock) -> AffectedCheck:
    impact = ctx.impact
    if ctx.project is None or impact.all_models_possibly_affected:
        # 範圍不明(專案取不回來、或反查判定全部 model 都可能受影響):逐一檢查沒有意義,
        # 由後處理補 major
        return AffectedCheck(impact=impact)
    in_mr = {rel for f in mr.get("files", []) if isinstance(f, dict)
             for rel in (ctx.relative(f.get("path")),) if rel is not None}
    todo = sorted((m for m in impact.affected_models if m.path not in in_mr),
                  key=lambda m: m.path)        # MR 自己改到的 model 已經在預掃裡檢查過
    deadline = clock() + MAX_SECONDS
    checked: list[CheckedModel] = []
    unchecked: list[str] = []
    specs: dict[str, str] = {}
    for index, model in enumerate(todo):
        if index >= MAX_CHECKED_MODELS or clock() > deadline:
            unchecked.append(model.path)
            continue
        new_hits, new_hints, no_baseline, error = await _compare(
            hub, model.path, dbt_cfg, ctx, render)
        # 查規格在真實 GitLab 模式是逐一發請求,同樣受數量與時間上限約束;沒查到的不算
        # 「沒有對應的規格」,而是沒查(下面不會採用規格)
        spec, looked_up = None, index < MAX_SPEC_LOOKUPS and clock() <= deadline
        if looked_up:
            spec = await _spec_for(hub, ctx, model.path, find_spec, specs)
        checked.append(CheckedModel(path=model.path, name=model.name, certain=model.certain,
                                    chain=model.chain, spec=spec, spec_looked_up=looked_up,
                                    new_hits=new_hits, new_hints=new_hints,
                                    no_baseline=no_baseline, error=error))
    # 規格只在「每個受影響的 model 都查過、而且都對到同一份」時才交給審查用;
    # 有的沒查、有的沒對到、或對到不同份,都不替審查挑一份
    # (沒查、沒對到的 spec 都是 None,會讓 codes 多一個 None 或只剩 None)
    codes = {m.spec for m in checked}
    code = next(iter(codes)) if len(codes) == 1 and not unchecked else None
    return AffectedCheck(impact=impact, models=tuple(checked), unchecked=tuple(unchecked),
                         spec=(code, specs[code]) if code else None, specs_found=specs)


async def _spec_for(hub, ctx: DbtContext, rel: str, find_spec, specs: dict) -> str | None:
    repo_path = f"{ctx.prefix}/{rel}" if ctx.prefix else rel
    try:
        code, text = await find_spec(hub, {"title": "", "description": "",
                                           "files": [{"path": repo_path}]}, by_path=True)
    except Exception:                        # noqa: BLE001 — 找不到規格不該讓審查中斷
        return None
    if not code or not text:
        return None
    specs.setdefault(code, text)
    return code


async def _compare(hub, rel: str, dbt_cfg: dict, ctx: DbtContext, render):
    """回傳 (變更後多出來的命中, 多出來的檢核點, 是否無法與變更前比較, 變更後的錯誤或 None)。

    比較的單位是「規則 + 嚴重度 + 命中所在的語句」,不是只數規則代號:macro 把
    `DELETE FROM a` 改成 `DELETE FROM b` 時 R001 的個數沒變,但那是另一句會被執行的 SQL,
    必須算成多出來的。文字層的規則沒有語句(空字串),就只比個數。"""
    head_text = ctx.project.files.get(rel)
    if head_text is None:
        return (), (), False, "變更後的專案裡找不到這個 model 的內容"
    head, head_error = await _hits(hub, rel, head_text, dbt_cfg, ctx.macro_files,
                                   ctx.project.macro_paths, ctx.info, render)
    if head_error is not None:
        return (), (), False, head_error
    base, base_error = None, "沒有變更前的專案"
    if ctx.base_project is not None:
        base_text = ctx.base_project.files.get(rel)
        if base_text is None:
            base_error = "變更前沒有這個 model"
        else:
            base, base_error = await _hits(hub, rel, base_text, dbt_cfg, ctx.base_macro_files,
                                           ctx.base_project.macro_paths, ctx.base_info, render)
    new = head if base_error is not None else head - base
    hits = tuple(sorted((rule, sev) for rule, sev, _ in new.elements() if sev in _COMPARED))
    hints = tuple(sorted(rule for rule, sev, _ in new.elements() if sev not in _COMPARED))
    return hits, hints, base_error is not None, None


async def _hits(hub, rel, text, dbt_cfg, macro_files, macro_dirs, info, render):
    """展開一個 model 並跑規則。回傳 (Counter{(規則, 嚴重度, 語句)}, None) 或 (None, 錯誤原因)。"""
    if dbt_cfg.get("database") != DBT_DATABASE_PLACEHOLDER:
        return None, "dbt 的資料庫名稱設定不符,無法展開"
    rendered = render(rel, source=text, database=dbt_cfg["database"], macro_files=macro_files,
                      macro_dirs=macro_dirs, project=info)
    if not rendered.ok:
        return None, f"展開失敗:{rendered.error}"
    if rendered.macro_conflicts:
        return None, f"dbt 專案裡有 {len(rendered.macro_conflicts)} 組同名的 macro,無法確定展開結果"
    rules = await hub.call_json("sqltools__run_rules", {"sql": rendered.sql})
    if isinstance(rules, dict):
        if rules.get("error"):
            return None, "展開後的 SQL 無法解析,規則沒有執行"
        rules = rules.get("hits")
    if not isinstance(rules, list):
        return None, "規則層回傳了非預期的資料"
    hits = Counter()
    for hit in rules:
        if (isinstance(hit, dict) and isinstance(hit.get("rule"), str)
                and isinstance(hit.get("severity"), str)):
            statement = hit.get("statement")
            hits[(hit["rule"], hit["severity"], statement if isinstance(statement, str) else "")] += 1
    return hits, None


# ------------------------------------------------------------------ 整理成一條 finding
IMPACT_TITLE = "dbt 變更對其他 model 的影響"
_SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
_MAX_LISTED = 20
_MAX_REASON_CHARS = 200


def _clip(text) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in str(text))
    return text[:_MAX_REASON_CHARS]


def _listed(items: list[str]) -> str:
    more = f" 等共 {len(items)} 個" if len(items) > _MAX_LISTED else ""
    return "、".join(items[:_MAX_LISTED]) + more


def impact_finding(check: AffectedCheck | None, prefix: str = "") -> dict | None:
    """把反查與檢查的結果整理成**一條** finding(逐個 model 各報一條會洗版)。沒有東西要說回 None。

    嚴重度:
      blocker  有 model 在變更後多出 blocker 級的規則命中(與變更前比較過)
      major    範圍不確定(反查標為不確定、全部 model 可能受影響)、有 model 沒檢查到、
               變更後展開失敗或無法解析、多出 major 級的命中、或無法與變更前比較而有命中
      minor    只多出 minor 級的命中
      info     有受影響的 model 或沒人呼叫的 macro,但檢查不出問題(清單給人看)
    內容只有路徑、名稱、規則代號與本模組 / 反查模組的固定文字;路徑與名稱都已由反查過白名單。
    """
    if check is None:
        return None
    impact = check.impact
    if not (impact.needs_human or impact.affected_models or impact.changed_macro_files
            or check.models or check.unchecked):
        return None
    severity = "info"

    def full(rel: str) -> str:
        return f"{prefix}/{rel}" if prefix else rel

    def raise_to(level: str) -> None:
        nonlocal severity
        if _SEVERITY_RANK[level] > _SEVERITY_RANK[severity]:
            severity = level

    parts = []
    if impact.changed_macro_files:
        parts.append(f"改到的 macro 檔:{_listed([full(p) for p in impact.changed_macro_files])}。")
    if impact.all_models_possibly_affected:
        raise_to("major")
        parts.append("影響範圍無法確定,**全部 model 都可能受影響**,沒有逐一檢查。")
    elif impact.affected_models:
        sure = sum(m.certain for m in impact.affected_models)
        parts.append(f"受影響的 model 共 {len(impact.affected_models)} 個"
                     f"(確定 {sure} 個、保守推定 {len(impact.affected_models) - sure} 個)。")
    if impact.uncertain:
        raise_to("major")
        reasons = [_clip(r) for r in impact.uncertain[:5]]
        more = f"(另有 {len(impact.uncertain) - 5} 項)" if len(impact.uncertain) > 5 else ""
        parts.append("無法確定的因素:" + ";".join(reasons) + more + "。")

    new, compared_blind, broken, hints = [], [], [], []
    for m in check.models:
        if m.new_hints:
            hints.append(f"{full(m.path)}[{'、'.join(sorted(set(m.new_hints)))}]")
        if not m.certain:
            raise_to("major")
        if m.error is not None:
            raise_to("major")
            broken.append(f"{full(m.path)}({_clip(m.error)})")
        elif m.new_hits:
            counts = Counter(m.new_hits)
            text = "、".join(f"{rule} {sev}" + (f" ×{n}" if n > 1 else "")
                            for (rule, sev), n in sorted(counts.items()))
            worst = max((sev for _, sev in m.new_hits), key=_SEVERITY_RANK.get)
            if m.no_baseline:
                # 沒有變更前的結果可以比:不知道是不是這次變更造成的,不以 blocker 擋下
                raise_to("major")
                compared_blind.append(f"{full(m.path)}[{text}]")
            else:
                raise_to(worst)
                new.append(f"{full(m.path)}[{text}]")
    if check.models:
        parts.append(f"已檢查 {len(check.models)} 個不在本次變更清單裡的受影響 model"
                     "(以變更前後的專案各展開一次,比較規則命中)。")
    if new:
        parts.append("**變更後多出的規則命中**:" + _listed(new) + "。")
    if compared_blind:
        parts.append("變更前的版本無法展開、無法比較,以下是變更後的全部命中:"
                     + _listed(compared_blind) + "。")
    if broken:
        parts.append("**變更後展開失敗或無法解析**:" + _listed(broken) + "。")
    if hints:
        parts.append("變更後多出的檢核點(這些 model 不在 diff 裡,模型沒有回應過):"
                     + _listed(hints) + "。")
    if check.unchecked:
        raise_to("major")
        parts.append("超過數量或時間上限,**沒有檢查**的受影響 model:"
                     + _listed([full(p) for p in check.unchecked]) + "。")
    mapped = [f"{full(m.path)} → {m.spec}" for m in check.models if m.spec]
    unmapped = [full(m.path) for m in check.models if m.spec_looked_up and not m.spec]
    if mapped:
        parts.append("對應的規格:" + _listed(mapped) + "。")
    if unmapped:
        parts.append("沒有對應到規格:" + _listed(unmapped) + "。")
    if impact.unreferenced_macros:
        parts.append(f"沒有任何 model 呼叫的 macro:{_listed(list(impact.unreferenced_macros))}。")
    if severity == "info":
        parts.append("沒有檢查出問題;清單供人工確認影響範圍。")
    return {"file": "(MR 內容)", "line": 0, "severity": severity, "title": IMPACT_TITLE,
            "detail": "".join(parts),
            "suggestion": ("逐一確認上列 model 的展開結果符合預期;必要時以 dbt compile 比對變更前後。"
                           if severity != "info" else ""),
            "citations": []}
