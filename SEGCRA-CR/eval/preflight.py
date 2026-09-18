#!/usr/bin/env python3
"""起飛前檢查 — 跑批之前先花兩分鐘確認「環境是你以為的那個」。

**為什麼需要這支程式**:上一輪 golden set 跑了 18 小時,結束後才發現數據不能用:

  * `config/models.yaml` 寫 `seed: 42`,實際上從未生效(端點是一層叫 `ollama-gate`
    的代理,它的 `/v1` 路徑接受但忽略 `options.*`)→ 整輪「可重現」的前提是假的
  * `num_ctx` 宣告 32768,但 `agent.py` 從來沒把它送出去 → prompt 可能被靜默截斷,
    而模型不會說它只看到後半截
  * 4 個 case 其實是連線失敗,被計成成功(只檢查了檔案存不存在)
  * `/api/ps` 報 20.5GB 但本機 GPU 只有 12GB → 模型根本不在這台機器上跑

這些全部都可以在**開跑前兩分鐘內**測出來。所以這支程式的原則是:

  **量「實際發生的」,不是「設定檔寫的」。** 每一項都用實測去證實,
  而且任何一項「不可比」級別的失敗就中止,不要跑完 18 小時才發現。

三個模式(愈上面愈輕,都可以單獨跑):

  --offline   只做確定性檢查(git 狀態、套件版本)。不連網路,秒級,可掛 CI
  --quick     加上端點身分、模型 digest、沙盒往返。仍不呼叫模型,約數秒
  (預設)     加上三個模型探針(seed 是否生效 / context 是否被截 / 延遲中位數),
              會真的呼叫模型,約一到三分鐘

輸出一份**環境指紋**(`--out fingerprint.yaml`),`run_baseline.py` 會把它收進
baseline 目錄。指紋的用途是**結構上強制**「換了環境就不准互比」——
`--compare 舊的指紋` 會逐項比對那些一變就讓數字失去可比性的欄位。

離開碼:0 可以起飛(含 warn);1 中止(有 fail 或與舊指紋不可比)。
"""
import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PKG_ROOT = EVAL_DIR.parent
sys.path.insert(0, str(PKG_ROOT))

from orchestrator.config import estimate_tokens, load_config  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - 舊版 Python / 特殊終端
        pass

MARK = {"ok": "✓", "warn": "⚠", "fail": "✗", "skip": "–"}

# 這些欄位一變,兩份 baseline 的數字就不再可比——**不含 code.sha**:
# 程式碼改變正是回歸比較要量的東西,把它列進來等於永遠不能比。
INCOMPARABLE = [
    ("model", "digest", "模型權重換了(tag 可以被重新指向別的權重,只看名稱看不出來)"),
    ("endpoint", "url", "端點換了"),
    ("endpoint", "api_version", "端點版本換了"),
    ("params", "seed_effective", "可重現性的前提改變了"),
    ("params", "num_ctx_effective", "模型看得到的 context 長度改變了"),
    ("sandbox", "engine", "執行驗證的資料庫換了(方言語意會變)"),
    ("deps", "sqlglot", "AST 預掃的解析器換版,確定性規則的命中會變"),
    ("deps", "jinja2", "樣板展開的結果可能變"),
]


@dataclass
class Check:
    name: str
    level: str          # ok / warn / fail / skip
    detail: str
    data: dict = field(default_factory=dict)


# ─────────────────────── 一、確定性檢查(不連網路) ───────────────────────

def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=PKG_ROOT, capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


def check_code() -> Check:
    """記下這輪跑的到底是哪份程式碼。

    工作區有未 commit 的改動時只給 warn 而不中止——開發途中想先跑一輪是常態;
    但指紋裡會標 `dirty: true`,而 dirty 的結果**不該當成別人可以回頭對照的基準**
    (沒有人能重建那份程式碼)。
    """
    sha = _git("rev-parse", "--short", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    dirty_files = [l for l in _git("status", "--porcelain").splitlines() if l.strip()]
    data = {"sha": sha or "(不在 git 工作區)", "branch": branch,
            "dirty": bool(dirty_files), "dirty_count": len(dirty_files)}
    if not sha:
        return Check("程式碼版本", "warn", "不在 git 工作區,無法記錄 SHA", data)
    if dirty_files:
        return Check("程式碼版本", "warn",
                     f"{branch}@{sha},但有 {len(dirty_files)} 個未 commit 的改動"
                     f"——這輪的結果無法被別人重建,不要當共用基準", data)
    return Check("程式碼版本", "ok", f"{branch}@{sha}(工作區乾淨)", data)


def check_deps() -> Check:
    """套件版本進指紋。`sqlglot` 換版會改變 AST 預掃的命中,那是確定性的差異,
    但如果沒記下來,看到的人只會以為是模型變了。"""
    from importlib.metadata import PackageNotFoundError, version
    data, missing = {}, []
    for pkg in ("sqlglot", "jinja2", "sqlfluff", "openai", "pyyaml", "json-repair", "pymssql"):
        try:
            data[pkg] = version(pkg)
        except PackageNotFoundError:
            data[pkg] = None
            missing.append(pkg)
    if "pymssql" in missing:
        return Check("套件版本", "fail",
                     "缺 pymssql —— 執行驗證(spec_exec)會整組落在「沙盒不可用」,"
                     "那不是品質問題卻會讓每個 case 多一條 major", data)
    if missing:
        return Check("套件版本", "fail", f"缺套件:{', '.join(missing)}", data)
    return Check("套件版本", "ok",
                 f"sqlglot {data['sqlglot']} / jinja2 {data['jinja2']}", data)


# ─────────────────────── 二、端點與沙盒(連網路,但不呼叫模型) ───────────────────────

def _base_url(endpoint: str) -> str:
    """把 OpenAI 相容路徑削回主機根:http://h:11435/v1 → http://h:11435。

    Ollama 的原生 API(/api/version、/api/tags)掛在根上,不在 /v1 底下。
    """
    u = endpoint.rstrip("/")
    return u[:-3].rstrip("/") if u.endswith("/v1") else u


def _http_get(url: str, timeout: float = 10.0) -> tuple[int, dict, str]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), (e.read() or b"").decode("utf-8", "replace")


def check_endpoint(cfg) -> Check:
    """先問「我在跟誰講話」。

    這一項是上一輪所有誤判的源頭:大家以為 `:11435` 是 Ollama,實際上是一層
    Python 代理(`Server: ollama-gate Python/3.9.6`)。代理層會**接受但忽略**
    `options.*`(seed、num_ctx),所以參數沒生效卻也沒有任何錯誤訊息。
    只要把 Server header 記進指紋,下一個人一眼就看得到。
    """
    base = _base_url(cfg.endpoint)
    data = {"url": cfg.endpoint, "base": base, "server_header": None,
            "api_version": None, "is_proxy": None}
    try:
        status, headers, body = _http_get(f"{base}/api/version")
    except Exception as e:
        return Check("端點身分", "fail",
                     f"連不到 {base}:{type(e).__name__}: {str(e)[:120]}", data)
    data["server_header"] = headers.get("Server")
    if status == 200:
        try:
            data["api_version"] = json.loads(body).get("version")
        except Exception:
            data["api_version"] = body[:40]
    srv = (data["server_header"] or "").lower()
    # 純 Ollama 的 Server header 不會出現 python/gate 這類字樣;出現就代表中間有一層
    data["is_proxy"] = bool(srv) and ("python" in srv or "gate" in srv or "proxy" in srv)
    if data["is_proxy"]:
        return Check("端點身分", "warn",
                     f"{base} 前面有代理層(Server: {data['server_header']})——"
                     "它可能吞掉 seed / num_ctx,下面的探針會實測", data)
    return Check("端點身分", "ok",
                 f"{base}(version {data['api_version']}, Server: {data['server_header']})",
                 data)


def check_model(cfg, profiles: list[str]) -> Check:
    """比對 **digest** 而不是 tag。

    `gemma4:31b` 這種 tag 可以被重新指向別的權重,名稱一樣、行為不一樣。
    digest 一變就代表「舊 baseline 不可比」,這是最容易被忽略、後果最大的一種漂移。
    """
    base = _base_url(cfg.endpoint)
    wanted = {cfg.profile(p).model for p in profiles}
    data = {"wanted": sorted(wanted), "digest": {}, "available": []}
    try:
        status, _, body = _http_get(f"{base}/api/tags", timeout=15)
        tags = json.loads(body).get("models", []) if status == 200 else []
    except Exception as e:
        return Check("模型 digest", "warn",
                     f"問不到 /api/tags({type(e).__name__})——代理層可能沒開這條路徑,"
                     "這輪就無法偵測權重是否被換掉", data)
    data["available"] = sorted(m.get("name", "") for m in tags)
    for m in tags:
        if m.get("name") in wanted:
            data["digest"][m["name"]] = (m.get("digest") or "")[:16]
    missing = sorted(wanted - set(data["digest"]))
    if missing:
        return Check("模型 digest", "fail",
                     f"端點上找不到 {', '.join(missing)}(現有:"
                     f"{', '.join(data['available'][:6]) or '(空)'})", data)
    return Check("模型 digest", "ok",
                 " / ".join(f"{k}={v}" for k, v in sorted(data["digest"].items())), data)


PROBE_PLAN = {
    # 故意做成「一正一反」:只有正向案例的話,SQL 永遠回傳列也會通過,
    # 等於沒驗到比對邏輯。兩個方向都對,才證明整條(建表→灌資料→執行→比對→rollback)是活的。
    "schema_ddl": ["CREATE TABLE preflight_probe (id INT, amt INT)"],
    "cases": [
        {"case_id": "P1", "condition_id": "C1", "direction": "positive",
         "expect_flagged": True, "note": "超過門檻應被標記",
         "rows": [["preflight_probe", 1, 500]]},
        {"case_id": "P2", "condition_id": "C1", "direction": "negative",
         "expect_flagged": False, "note": "未達門檻不應被標記",
         "rows": [["preflight_probe", 2, 50]]},
    ],
}
PROBE_SQL = "SELECT id FROM preflight_probe WHERE amt > 100"


def check_sandbox() -> Check:
    """用真的沙盒跑一輪最小案例,而不是只 ping 一下連線。

    沙盒掛掉時 `run_spec_exec` 會給每個 case 補一條「沙盒不可用」的 major——
    看起來像品質退步,其實是環境問題。跑批前先確認,省下整輪的誤判。
    """
    from orchestrator.spec_exec import execute_cases
    t0 = time.monotonic()
    try:
        ex = execute_cases(PROBE_SQL, PROBE_PLAN)
    except Exception as e:
        return Check("執行驗證沙盒", "fail", f"{type(e).__name__}: {str(e)[:150]}", {})
    took = round(time.monotonic() - t0, 2)
    data = {"engine": ex.get("engine"), "seconds": took,
            "sql_error": ex.get("sql_error"), "testdata_error": ex.get("testdata_error"),
            "sandbox_error": ex.get("sandbox_error")}
    if ex.get("sandbox_error"):
        return Check("執行驗證沙盒", "fail", ex["sandbox_error"], data)
    if ex.get("sql_error") or ex.get("testdata_error"):
        return Check("執行驗證沙盒", "fail",
                     f"探針本身跑不起來(sql={ex.get('sql_error')} "
                     f"testdata={ex.get('testdata_error')})——沙盒行為和預期不同", data)
    bad = [r for r in ex["case_results"] if not r["ok"]]
    if bad or len(ex["case_results"]) != 2:
        return Check("執行驗證沙盒", "fail",
                     f"探針案例比對不符({len(bad)} 個不符)——比對邏輯或方言語意有問題", data)
    return Check("執行驗證沙盒", "ok", f"{ex['engine']},一正一反皆符合({took}s)", data)


# ─────────────────────── 三、模型探針(會真的呼叫模型) ───────────────────────

def _msg_fingerprint(msg) -> str:
    """把回覆整個當成指紋,而不是只看 `content`。

    上一輪踩過的坑:只比 `content` 會漏掉推理型模型的 `reasoning`,
    而 `reasoning` 不同就代表這次生成的路徑不同——那正是我們要偵測的漂移。
    """
    d = msg.model_dump(exclude_none=True) if hasattr(msg, "model_dump") else dict(msg)
    d.pop("role", None)
    for k in ("tool_calls", "function_call", "annotations", "audio"):
        d.pop(k, None)
    return json.dumps(d, ensure_ascii=False, sort_keys=True)


async def _ask(client, model: str, prompt: str, *, seed: int | None,
               temperature: float, max_tokens: int) -> tuple[str, float]:
    kw = {"seed": seed} if seed is not None else {}
    t0 = time.monotonic()
    resp = await client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": prompt}],
        temperature=temperature, max_tokens=max_tokens, **kw)
    return _msg_fingerprint(resp.choices[0].message), time.monotonic() - t0


async def check_seed(client, model: str) -> Check:
    """實測 `seed` 到底有沒有生效——**而且帶對照組**。

    上一輪我第一次測的時候只跑「同 seed 兩次」,如果兩次一樣就宣告生效。那是錯的:
    prompt 本身在 temperature=1.0 下若沒什麼隨機空間,不給 seed 也會一樣,
    結論會是假陽性。所以這裡三次呼叫:

      A、B  同一個 seed
      C     完全不給 seed(對照組)

    判讀:A==B 且 C!=A → 生效;A!=B → 不生效;A==B==C → 無法判定(這個 prompt
    量不出隨機性,換句話說也量不出 seed 有沒有用)。
    """
    prompt = ("隨機想一個台灣的地名,只回覆地名兩到四個字,不要解釋、不要標點。"
              "每次都要想一個不同的。")
    # max_tokens 要給夠:gemma4 會先燒 token 在內部推理上,給太小會回空字串,
    # 兩次空字串「相同」又是一次假陽性(上一輪踩過)。
    try:
        a, ta = await _ask(client, model, prompt, seed=42, temperature=1.0, max_tokens=800)
        b, tb = await _ask(client, model, prompt, seed=42, temperature=1.0, max_tokens=800)
        c, tc = await _ask(client, model, prompt, seed=None, temperature=1.0, max_tokens=800)
    except Exception as e:
        return Check("seed 是否生效", "fail",
                     f"探針呼叫失敗:{type(e).__name__}: {str(e)[:150]}", {})
    data = {"same_seed_identical": a == b, "control_differs": c != a,
            "seconds": [round(x, 1) for x in (ta, tb, tc)]}
    if a != b:
        data["effective"] = False
        return Check("seed 是否生效", "warn",
                     "❗同一個 seed 兩次結果不同 → **seed 無效**。"
                     "這輪沒有任何取得可重現結果的機制,只能跑多次看分布"
                     "(指紋已記 seed_effective: false)", data)
    if a == c:
        data["effective"] = None
        return Check("seed 是否生效", "warn",
                     "同 seed 一致,但**不給 seed 也一致** → 這個 prompt 量不出隨機性,"
                     "無法判定 seed 有沒有用(不要當成可重現)", data)
    data["effective"] = True
    return Check("seed 是否生效", "ok",
                 "同 seed 逐字一致、無 seed 對照組不同 → seed 真的生效。"
                 "**這與上一輪的結論相反,代表環境變了,舊 baseline 不可比**", data)


async def check_num_ctx(client, model: str, budget_tokens: int) -> Check:
    """實測「模型看得到多長的 prompt」——直接對上 `budget.diff`。

    `config/models.yaml` 宣告 `num_ctx: 32768`,但 `agent.py` 從來沒有把它送出去
    (這是我們自己的 bug,不是代理層的問題)。沒送的話端點會用它自己的預設值,
    Ollama 的預設遠小於 32768,而**超長的 prompt 是從開頭被截掉的**。

    後果很惡劣:`build_diff_section` 把 SQL 放在前面,系統指示與問題放在後面,
    所以被截掉的正是受審的 SQL——模型照樣會產出一份看起來完整的報告,
    只是它根本沒看到程式碼,而報告不會說。

    探針:在最前面藏一個隨機 marker,塞到 `budget.diff` 的長度,最後才問 marker 是什麼。
    答得出來 → 這個長度是安全的;答不出來 → 已經在靜默截斷。
    """
    marker = f"SEGCRA-{uuid.uuid4().hex[:10].upper()}"
    filler_line = "-- 以下為填充用的無意義註解,僅為把 prompt 撐到預算長度。\n"
    # estimate_tokens 是 len/3(與管線同一把尺),用它反推需要多少字元
    need_chars = budget_tokens * 3
    filler = filler_line * (need_chars // len(filler_line) + 1)
    prompt = (f"驗證碼:{marker}\n\n{filler}\n\n"
              "上面最開頭有一行「驗證碼:」。請只回覆那個驗證碼本身,不要其他任何文字。")
    sent = estimate_tokens(prompt)
    data = {"probe_tokens": sent, "budget_diff": budget_tokens, "marker": marker}
    try:
        out, took = await _ask(client, model, prompt, seed=None, temperature=0.0,
                               max_tokens=800)
    except Exception as e:
        return Check("context 長度", "fail",
                     f"探針呼叫失敗:{type(e).__name__}: {str(e)[:150]}", data)
    data["seconds"] = round(took, 1)
    data["recalled"] = marker in out
    if not data["recalled"]:
        data["num_ctx_effective"] = "< probe"
        return Check("context 長度", "warn",
                     f"❗送出約 {sent} tokens(= diff 預算 {budget_tokens})時,"
                     "模型記不得開頭的驗證碼 → prompt 正在被**靜默截斷**,"
                     "被截掉的就是受審的 SQL。這輪的品質數字要標註此事", data)
    data["num_ctx_effective"] = ">= probe"
    return Check("context 長度", "ok",
                 f"送出約 {sent} tokens 仍記得開頭的驗證碼 → diff 預算"
                 f"({budget_tokens})之內沒有被截斷", data)


async def check_latency(client, model: str, n: int = 3) -> Check:
    """量短呼叫的耗時中位數,只為了一個目的:**判斷計時數據能不能跨輪比較**。

    上一輪觀測到同一個呼叫 53s → 681s(輸出量相同),差 13 倍。那代表環境的推論
    速度在飄。如果不先記下這輪的基準速度,之後看到「這次跑比較久」就無法分辨是
    我們讓模型做更多事,還是環境本來就慢了。
    """
    times = []
    for _ in range(n):
        try:
            _, t = await _ask(client, model, "回覆「ok」兩個字,不要其他文字。",
                              seed=None, temperature=0.0, max_tokens=64)
        except Exception as e:
            return Check("推論延遲", "fail",
                         f"短呼叫失敗:{type(e).__name__}: {str(e)[:120]}",
                         {"seconds": times})
        times.append(round(t, 1))
    med = statistics.median(times)
    spread = max(times) / max(min(times), 0.01)
    data = {"seconds": times, "median": med, "spread": round(spread, 1)}
    if spread > 3:
        return Check("推論延遲", "warn",
                     f"中位數 {med}s,但最快最慢差 {spread:.1f} 倍({times})——"
                     "上游負載在飄,這輪的計時數據不適合跨輪比較", data)
    return Check("推論延遲", "ok", f"中位數 {med}s({times})", data)


# ─────────────────────── 指紋:組裝與比對 ───────────────────────

def build_fingerprint(checks: list[Check], cfg, profiles: list[str]) -> dict:
    """把檢查結果收斂成一份指紋。

    刻意記「實測到的」而非「設定檔寫的」:`params.seed_effective` 來自探針,
    不是來自 yaml。上一輪最痛的教訓就是兩者不一致而沒人知道。
    """
    by = {c.name: c for c in checks}

    def d(name: str) -> dict:
        return by[name].data if name in by else {}

    prof = {p: vars(cfg.profile(p)) for p in profiles}
    seed_chk = d("seed 是否生效")
    ctx_chk = d("context 長度")
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "code": d("程式碼版本"),
        "deps": d("套件版本"),
        "endpoint": d("端點身分"),
        "model": {"profiles": prof, **d("模型 digest")},
        "params": {
            # temperature / max_output_tokens 是我們真的送出去的(agent.py 有送)
            "temperature": {p: prof[p]["temperature"] for p in prof},
            "max_output_tokens": {p: prof[p]["max_output_tokens"] for p in prof},
            # num_ctx 在 yaml 裡有宣告,但 agent.py 沒送 → 記「宣告值」與「實測結果」兩欄,
            # 免得下一個人又以為宣告就等於生效
            "num_ctx_declared": {p: prof[p]["num_ctx"] for p in prof},
            "num_ctx_effective": ctx_chk.get("num_ctx_effective", "未測"),
            "seed_effective": seed_chk.get("effective", "未測"),
        },
        "sandbox": d("執行驗證沙盒"),
        "timing": d("推論延遲"),
        "preflight": {
            "verdict": verdict(checks),
            "checks": [{"name": c.name, "level": c.level, "detail": c.detail}
                       for c in checks],
        },
    }


def compare_fingerprint(new: dict, old: dict) -> list[str]:
    """回傳「讓兩份 baseline 不可比」的差異清單(空 = 可比)。

    刻意**不**比 `code.sha`:程式碼改變正是回歸比較要量的東西。
    比的是那些一變就讓數字失去意義的環境條件。
    """
    out = []
    for section, key, why in INCOMPARABLE:
        a = (new.get(section) or {}).get(key)
        b = (old.get(section) or {}).get(key)
        if a != b:
            out.append(f"{section}.{key}: 舊={b!r} → 新={a!r} —— {why}")
    return out


def verdict(checks: list[Check]) -> str:
    if any(c.level == "fail" for c in checks):
        return "abort"
    if any(c.level == "warn" for c in checks):
        return "go-with-caveats"
    return "go"


# ─────────────────────── 主流程 ───────────────────────

async def run(args) -> int:
    cfg = load_config()
    profiles = sorted({cfg.default_profile, *cfg.roles.values()})
    checks: list[Check] = [check_code(), check_deps()]

    if not args.offline:
        ep = check_endpoint(cfg)
        checks.append(ep)
        # 端點本身就連不到的話,問 digest 只會再吐一次同一個錯;標成 skip 才不會
        # 讓人以為「digest 這項只是小問題」(warn 讀起來像可以照跑)
        checks.append(check_model(cfg, profiles) if ep.level != "fail" else
                      Check("模型 digest", "skip", "端點連不到,無從比對權重"))
        # 沙盒與端點互不相干(一個是 GPU 上游、一個是本機 SQL Server),照查
        checks.append(check_sandbox())

    llm_ok = all(c.level != "fail" for c in checks)
    if args.offline or args.quick:
        pass
    elif not llm_ok:
        # 前面已經 fail 了就不要再花幾分鐘呼叫模型——反正這輪不能起飛
        checks.append(Check("模型探針", "skip", "前面的檢查已經中止,略過模型探針"))
    else:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(base_url=cfg.endpoint, api_key=cfg.api_key,
                             timeout=args.timeout, max_retries=0)
        model = cfg.profile(cfg.default_profile).model
        checks.append(await check_seed(client, model))
        checks.append(await check_num_ctx(client, model, cfg.budget["diff"]))
        checks.append(await check_latency(client, model))

    print("起飛前檢查" + ("(--offline:只做確定性檢查)" if args.offline
                        else "(--quick:不呼叫模型)" if args.quick else ""))
    print("=" * 62)
    for c in checks:
        print(f"{MARK[c.level]} {c.name}:{c.detail}")

    fp = build_fingerprint(checks, cfg, profiles)
    v = fp["preflight"]["verdict"]
    print("=" * 62)

    incomparable: list[str] = []
    if args.compare:
        old = _load_fingerprint(Path(args.compare))
        incomparable = compare_fingerprint(fp, old)
        if incomparable:
            print(f"⛔ 與 {args.compare} **不可比**:")
            for line in incomparable:
                print(f"   · {line}")
            print("   → 不要把這輪的數字拿去和那份 baseline 比;要比就重建 baseline。")
        else:
            print(f"✓ 環境條件與 {args.compare} 相同,兩份 baseline 可以比 delta。")

    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        _dump_fingerprint(p, fp)
        print(f"環境指紋已寫入 {p}")

    if v == "abort":
        print("\n⛔ 中止:上面有 ✗ 的項目。修好再跑,不要跑完十幾小時才發現數據不能用。")
        return 1
    if v == "go-with-caveats":
        print("\n⚠ 可以起飛,但上面 ⚠ 的限制**必須寫進報告**"
              "(尤其 seed / context 兩項會決定數字怎麼解讀)。")
    else:
        print("\n✓ 環境檢查全部通過,可以起飛。")
    return 1 if incomparable else 0


def _dump_fingerprint(p: Path, fp: dict) -> None:
    if p.suffix == ".json":
        p.write_text(json.dumps(fp, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
        return
    import yaml
    p.write_text(yaml.safe_dump(fp, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")


def _load_fingerprint(p: Path) -> dict:
    text = p.read_text(encoding="utf-8")
    if p.suffix == ".json":
        return json.loads(text)
    import yaml
    return yaml.safe_load(text)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="跑批前的環境檢查與指紋記錄(見 eval/BASELINE.md)")
    ap.add_argument("--offline", action="store_true",
                    help="只做確定性檢查(git/套件),完全不連網路")
    ap.add_argument("--quick", action="store_true",
                    help="做到端點/模型/沙盒,不呼叫模型(數秒)")
    ap.add_argument("--out", default=None, metavar="PATH",
                    help="把環境指紋寫到這裡(.yaml 或 .json)")
    ap.add_argument("--compare", default=None, metavar="PATH",
                    help="與舊指紋比對;有不可比的差異就以離開碼 1 回報")
    ap.add_argument("--timeout", type=float, default=900.0,
                    help="單次模型呼叫上限秒數(預設 900,實測合理上限約 750s)")
    sys.exit(asyncio.run(run(ap.parse_args())))
