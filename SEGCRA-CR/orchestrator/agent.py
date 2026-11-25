"""Agent loop — 本地模型(OpenAI-compatible)+ ToolHub 工具呼叫迴圈。"""
import contextlib
import contextvars
import json
import os
import re
import time

from openai import AsyncOpenAI

from .config import Config, ModelProfile, env_bool, env_seconds, estimate_tokens
from .error_text import error_label
from .tool_hub import ToolHub

MAX_ITERATIONS = 12

# 本地大模型單次生成可能超過 10 分鐘(openai client 預設 timeout 600s 且自動重試
# 2 次——對本地推理只是把同一個慢請求連跑三遍再炸 Timeout)。改為不自動重試;單次請求
# 最多等 LLM_TIMEOUT(預設 1 小時),有環節的截止時間時取兩者較小的(見 request_timeout)。
REQUEST_TIMEOUT = env_seconds("LLM_TIMEOUT", 3600)

# LLM_TIMEOUT 是用戶端願意等多久。端點前面若有模型閘道(例如 LiteLLM),閘道對每次請求另有
# 自己的上限(可能只有幾十秒),超過就切斷,用戶端等再久也沒用。LiteLLM 會照請求
# 內容裡的 `timeout` 改用那個上限,所以 LLM_SEND_TIMEOUT=1 時把這次請求會等的時間放進請求
# 內容。預設不送,理由同 reasoning_effort:不認得這個欄位的端點可能整個退回請求。
SEND_TIMEOUT = env_bool("LLM_SEND_TIMEOUT", "")


# 這一環節的截止時間(time.monotonic())。主審查、冷讀、執行驗證外面各有自己的上限
# (`asyncio.wait_for`,見 pipeline._stage_limit),時間到是外層先取消,run_agent 不知道。
# 請求送出去的 timeout 若比剩下的時間長,用戶端斷線走了、閘道還照收到的值等,模型繼續生成
# 一個沒有人要的答案;端點一次只處理一個請求時,下一份審查就排在它後面。
_DEADLINE: contextvars.ContextVar = contextvars.ContextVar("segcra_llm_deadline", default=None)


class LlmNoTimeLeft(Exception):
    """這一環節剩下的時間不夠送下一次請求(見 llm_deadline 的 min_left):不送,讓呼叫端照「沒跑完」記。"""


@contextlib.contextmanager
def llm_deadline(seconds: float | None, min_left: float = 0.0):
    """在這個區塊裡呼叫的模型,每次請求最多等到「現在 + seconds」。巢狀時取較早的。
    `asyncio.wait_for` 建的 task 與 `run_in_thread` 都會帶上這個值,所以要在呼叫它們**之前**設。

    `min_left`:剩不到這麼多秒就不再送請求(run_agent 每次送出前比一次,丟 LlmNoTimeLeft)。
    越後面的請求拿到的時間越少,剩幾秒送出去的請求多半等不到結果,只是讓閘道多跑一次;
    門檻用跟「剩不到就不開始這一步」同一個值(pipeline 傳 MIN_STAGE_SECONDS 進來)。"""
    if seconds is None:
        yield
        return
    end = time.monotonic() + seconds
    current = _DEADLINE.get()
    if current is not None:
        end, min_left = min(current[0], end), max(current[1], min_left)
    token = _DEADLINE.set((end, min_left))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def _time_left() -> tuple[float, float] | None:
    """(這一環節還剩幾秒, 剩不到幾秒就不送);沒有截止時間時 None。"""
    current = _DEADLINE.get()
    if current is None:
        return None
    return current[0] - time.monotonic(), current[1]


def request_timeout() -> float:
    """這一次請求會等多久:LLM_TIMEOUT 與這一環節剩下的時間取小的,至少 1 秒。
    用戶端(openai 套件)與送給閘道的(LLM_SEND_TIMEOUT)用同一個值,兩邊同時到。"""
    left = _time_left()
    if left is None:
        return REQUEST_TIMEOUT
    return max(1.0, min(REQUEST_TIMEOUT, left[0]))


def is_timeout(error: BaseException) -> bool:
    """這個例外是不是「時間到了」:請求逾時,或剩下的時間不夠、沒有送出。"""
    import asyncio

    import openai
    return isinstance(error, (openai.APITimeoutError, asyncio.TimeoutError, TimeoutError, LlmNoTimeLeft))


def output_completeness(meta: dict, max_output_tokens: int) -> str:
    """run_agent 回填的 meta(最後一次回應)輸出完整度,分三種:

    - `"truncated"`:端點回 `finish_reason == "length"`,或回報的 `completion_tokens`
      已達 `max_output_tokens`——不是每個端點碰到上限都照規格回 `length`,用量達到上限
      本身就代表被切斷。
    - `"complete"` :端點回 `finish_reason == "stop"` 且用量未達上限——乾淨結束。
    - `"unknown"`  :其餘。端點**沒有回報**結束原因(None),或回了 `stop`/`length` 以外
      的值(`eos`、`content_filter`、`tool_calls`…)這類不算乾淨結束的狀態。

    **「無法確認(unknown)」要比照「被截斷」處理**:沒有依據說它完整,就不能當它完整——
    不然沒回報結束原因的端點會讓被切斷的輸出安靜地走完、甚至自動放行。採信的一方(可以
    據以自動放行、據以剔除案例)只給 `"complete"`。

    主審查、測資生成、仲裁、知識起草、裸模型對照組都用這一個判斷。"""
    finish = meta.get("finish_reason")
    used = meta.get("completion_tokens")
    if finish == "length" or (used is not None and used >= max_output_tokens):
        return "truncated"
    if finish == "stop":
        return "complete"
    return "unknown"


def output_truncated(meta: dict, max_output_tokens: int) -> bool:
    """輸出是不是**確定**被長度上限切斷(`output_completeness` 的 `"truncated"`)。

    只認確定截斷:沒回報結束原因(`"unknown"`)不算這一個。呼叫端若要把「無法確認」
    也比照截斷處理(主審查、測資生成、知識起草、裸模型對照組都要),改用
    `output_completeness`,才分得出「被截斷」與「沒回報」兩種訊息。"""
    return output_completeness(meta, max_output_tokens) == "truncated"


# 診斷用:設成一個目錄路徑,就把每一次**實際送出的** messages 原文落檔。
# 預設空字串 = 完全不啟用,對正式流程沒有任何影響。
#
# 用途只有一個,但那是目前唯一沒有結論的問題:整條管線跑兩次結果不同,
# 到底是「模型取樣」還是「送出去的輸入本來就不一樣」。把兩次的 prompt diff 一下
# 就分得出來——prompt 相同 → 取樣;prompt 不同 → 輸入,而且 diff 直接指出哪一段在變。
# 詳見 eval/BASELINE.md §1 與 §7。
DUMP_PROMPTS = os.environ.get("SEGCRA_DUMP_PROMPTS", "")
_dump_seq = 0


def _dump(profile_model: str, messages: list, iteration: int) -> str:
    """把一次呼叫的完整 messages 寫成一個檔,回傳檔名(供回應接在同一個檔後面)。

    失敗一律吞掉——診斷功能不該弄垮跑批。"""
    global _dump_seq
    if not DUMP_PROMPTS:
        return ""
    try:
        from pathlib import Path
        _dump_seq += 1
        d = Path(DUMP_PROMPTS)
        d.mkdir(parents=True, exist_ok=True)
        # 檔名帶 PID:per-case 是各自的子行程,同一個目錄不會互相蓋掉
        f = d / f"{os.getpid()}_{_dump_seq:03d}_iter{iteration}_{profile_model.replace(':','-')}.txt"
        parts = [f"# model={profile_model} iteration={iteration} messages={len(messages)}"]
        for m in messages:
            parts.append(f"\n===== role={m.get('role')} =====\n{m.get('content') or ''}")
            if m.get("tool_calls"):
                parts.append(f"----- tool_calls -----\n{json.dumps(m['tool_calls'], ensure_ascii=False, sort_keys=True, indent=2)}")
        f.write_text("\n".join(parts), encoding="utf-8")
        return str(f)
    except Exception:
        return ""


def _dump_response(path: str, content: str, finish_reason: str,
                   completion_tokens, prompt_tokens=None,
                   estimated_prompt_tokens=None) -> None:
    """把模型的回應接在同一個落檔後面。

    **為什麼回應也要落檔**:只落請求看不出最關鍵的事——模型到底有沒有照
    「只補缺的那幾個」做(可能改條件編號、可能整份重寫、可能只回一部分)。
    那是測資生成唯一只有真端點答得出來的問題,而它每次呼叫動輒數分鐘,
    沒落檔就得重跑一輪才能回答。純觀測,不改送進模型的任何內容。"""
    if not path:
        return
    try:
        from pathlib import Path
        with Path(path).open("a", encoding="utf-8") as fh:
            # prompt_tokens 與我們的估算一起記:estimate_tokens() 用的是 len/3,
            # 而實測測資那種「日期+數字」的 JSON 一個 token 不到 0.5 個字元
            # (completion_tokens=8192 只換到 3923 字元)。輸入側若也差這麼多,
            # budget.diff 的 8000 就不是真的 8000——這一欄就是為了量它。
            fh.write(f"\n\n===== response finish_reason={finish_reason} "
                     f"completion_tokens={completion_tokens} chars={len(content or '')} "
                     f"prompt_tokens={prompt_tokens} "
                     f"estimated_prompt_tokens={estimated_prompt_tokens} =====\n")
            fh.write(content or "")
    except Exception:
        pass


async def run_agent(cfg: Config, profile: ModelProfile, system: str, user: str,
                    hub: ToolHub, verbose: bool = True, use_tools: bool = True,
                    trace: list | None = None, meta: dict | None = None) -> str:
    """建立用戶端、跑完、**保證關掉**。實際的迴圈在 `_run_agent`。

    不關的話,底層的 httpx 連線池會留到物件被回收才收尾,而那通常發生在事件迴圈
    已經關掉之後 —— 於是每次 `asyncio.run` 結束都印一行
    `RuntimeError: Event loop is closed`(冷讀評測實測)。正式的 webhook 每份審查
    也是一個迴圈,同樣會留下沒收尾的連線。

    包成薄薄一層而不是在迴圈裡 try/finally:那個函式有七個 return,包起來要整段
    重排縮排,改動大、也更容易改錯。
    """
    client = AsyncOpenAI(base_url=cfg.endpoint, api_key=cfg.api_key,
                         timeout=REQUEST_TIMEOUT, max_retries=0)
    try:
        return await _run_agent(client, cfg, profile, system, user, hub,
                                verbose=verbose, use_tools=use_tools,
                                trace=trace, meta=meta)
    finally:
        # 測試與評測會換上自己的假用戶端,不一定有 close();收尾失敗也不該讓一份
        # 跑完的審查變成例外。
        closer = getattr(client, "close", None)
        if closer is not None:
            try:
                await closer()
            except Exception:        # noqa: BLE001 — 收尾不成功不影響審查結果
                pass


async def _run_agent(client, cfg: Config, profile: ModelProfile, system: str, user: str,
                     hub: ToolHub, verbose: bool = True, use_tools: bool = True,
                     trace: list | None = None, meta: dict | None = None) -> str:
    """trace 給定時,把每次工具呼叫記錄下來(供產生逐字對話 transcript)。

    meta 給定時,填入最後一次回應的 `finish_reason` 與 `completion_tokens`。

    **為什麼需要這個**:回應被 `max_tokens` 截斷時,端點回的是
    `finish_reason="length"`,但呼叫端只拿得到字串,看不到這件事——而
    `extract_json()` 的 json-repair 退路會把截斷的 JSON **修補成合法物件**,
    於是上層收到一份「看起來正常、只是比較短」的結果,完全無從察覺截斷。
    實測過:`RETAIL_M1` 的測資生成 `finish_reason=length`、
    `completion_tokens` 剛好等於上限 8192、輸出斷在一個數字中間,
    但 `extract_json` 照樣解析成功,只是案例從 46 個掉到 9 個。

    回傳型別刻意不改(5 個呼叫點全不受影響),沿用本檔既有的 `trace`
    那種「傳容器進來收集額外資訊」的慣例。

    已對真實端點驗證過兩個方向(不只單元測試的 mock):`max_tokens=16` 時
    回 `finish_reason='length'`、`completion_tokens=16`;`max_tokens=512`
    的短回覆回 `'stop'`、`completion_tokens=67`。"""
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    if verbose:
        print(f"[agent] model={profile.model} tools={'on' if use_tools else 'off'} "
              f"prompt≈{estimate_tokens(system + user)} tokens")

    tool_kwargs = ({"tools": hub.openai_tools, "tool_choice": "auto"}
                   if use_tools and hub else {})
    # seed 沒設定(None)時不傳這個參數,讓模型維持原本的隨機性(能力報告用)
    seed_kwargs = {"seed": profile.seed} if profile.seed is not None else {}
    # reasoning_effort 同理:沒設定就不送,用端點預設。送了不支援的參數可能讓整個
    # 請求被退回,比多花一點 token 嚴重得多,所以預設不送而不是預設關閉思考。
    effort_kwargs = ({"reasoning_effort": profile.reasoning_effort}
                     if profile.reasoning_effort else {})
    if meta is not None:
        # 一開始就寫 False:呼叫端用 meta.get("exhausted") 時,「沒有這個 key」
        # (舊版程式)與「跑完沒用光輪次」要分得出來。
        meta["exhausted"] = False
    for i in range(MAX_ITERATIONS):
        dump_path = _dump(profile.model, messages, i)
        # 每一輪都重算:這一環節剩下的時間越來越少(見 request_timeout)。用戶端的 timeout
        # 與送給閘道的(放在 extra_body,openai 套件會原樣併進請求內容)是同一個值
        left = _time_left()
        if left is not None and left[0] < left[1]:
            raise LlmNoTimeLeft(f"這一環節只剩 {max(left[0], 0):.0f} 秒,不到 {left[1]:g} 秒,不再送出請求")
        wait = request_timeout()
        timeout_kwargs = {"extra_body": {"timeout": wait}} if SEND_TIMEOUT else {}
        resp = await client.chat.completions.create(
            model=profile.model, messages=messages, **tool_kwargs,
            temperature=profile.temperature,
            max_tokens=profile.max_output_tokens,
            timeout=wait,
            **seed_kwargs, **effort_kwargs, **timeout_kwargs,
        )
        msg = resp.choices[0].message
        usage = getattr(resp, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", None) if usage else None
        if prompt_tokens is not None:
            sent_estimate = sum(estimate_tokens(m.get("content") or "") for m in messages)
            # 超過 context 上限時,端點可能不會報錯,只會默默截掉 prompt 開頭再繼續生成
            # (HTTP 200、無錯誤訊息)。用回應回報的 prompt_tokens 跟我們估計送出的量比對,
            # 差距過大就代表這次生成很可能讀到被截斷的 prompt,結果不可信。
            if prompt_tokens < sent_estimate * 0.5:
                print(f"[agent] 警告:估計送出 prompt ≈{sent_estimate} tokens,"
                      f"但端點回報只讀到 {prompt_tokens} tokens——context 可能被靜默截斷,"
                      f"這次生成結果可能不可信。")
        completion = getattr(usage, "completion_tokens", None) if usage else None
        if meta is not None:
            # 每一輪都覆寫:呼叫端關心的是**最後一次**回應有沒有被截斷,
            # 中間輪次的工具呼叫回合即使碰到上限也會繼續跑下去。
            meta["finish_reason"] = resp.choices[0].finish_reason
            meta["completion_tokens"] = completion
            meta["prompt_tokens"] = prompt_tokens
            # 另外**累加**:有工具呼叫時一輪會跑好幾次,每一次都把整串對話重讀一遍,
            # 所以總量遠大於最後一次。只記最後一次的話,工具用得多的 MR 反而看起來
            # 很便宜,跟模型端點那邊記的總量對不起來。
            #
            # 端點沒回 usage 的那一輪不加(不要把 None 當 0 加進去之後還記成有值);
            # 整份都沒有任何一輪回報時,鍵根本不存在 → 呼叫端讀到 None,
            # 跟 completion_tokens 沒回報時同一個意思。
            for key, value in (("prompt_tokens_total", prompt_tokens),
                               ("completion_tokens_total", completion)):
                if value is not None:
                    meta[key] = (meta.get(key) or 0) + value
            # 有幾輪沒回報,總量就少算幾輪的量,而光看總量看不出來。
            # 記下來,量測的人才知道這個數字完不完整。一輪只要有一個數字沒回報就算,
            # 因為少的那一側一樣會讓總量偏低。這個鍵從第一輪起就一定在,0 代表
            # 「每一輪都有回報」,不是「沒在記」。
            missing = prompt_tokens is None or completion is None
            meta["rounds_without_usage"] = meta.get("rounds_without_usage", 0) + int(missing)
        _dump_response(dump_path, msg.content or "", resp.choices[0].finish_reason,
                       completion, prompt_tokens,
                       sum(estimate_tokens(m.get("content") or "") for m in messages))
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            if not (msg.content or "").strip():
                # 推理型模型偶發:tokens 燒在思考上、content 空 → 推一把再試
                messages.append({"role": "user",
                                 "content": "請直接輸出最終報告 JSON,不要其他文字。"})
                continue
            return msg.content

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if verbose:
                print(f"[agent] tool #{i}: {tc.function.name}({str(args)[:120]})")
            try:
                # 模型要求的只走白名單入口:寫入類工具不執行(#17,見 tool_hub.MODEL_TOOLS)
                result = await hub.call_from_model(tc.function.name, args)
            except Exception as e:
                # 工具結果會進模型的對話,模型可能照抄進 finding 貼到 MR:不放例外原文(見 error_text)
                result = f"(工具執行失敗:{error_label(e)})"
            if trace is not None:
                trace.append({"step": i, "tool": tc.function.name, "args": args,
                              "result_preview": (result or "")[:400]})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    # 工具回合用完了,模型始終沒有產出最終回應。
    #
    # **不可以回傳最後一則工具結果**:那則內容是工具回來的、而工具看的是 MR,所以內容
    # 由 MR 決定。它是 JSON 物件時 extract_json 會把它當成審查報告收下,而最後一次模型
    # 呼叫的 finish_reason 是 "stop" —— 呼叫端從 meta 完全看不出發生了這件事。
    #
    # 兩道一起上:回固定文字(就算呼叫端忘了看 meta,也拿不到工具結果),並標記
    # exhausted(呼叫端據此比照截斷處理:不解析輸出、補一條 major、不得自動放行)。
    # 只有 run_agent 知道迴圈是跑完還是被 return 出去的,所以標記只能在這裡做。
    if meta is not None:
        meta["exhausted"] = True
    return "(達到工具呼叫上限,未產出最終結果)"


def decode_byte_fallback(text: str) -> str:
    """修復 sentencepiece/gemma byte-fallback:把字面的 <0xHH> 序列解回實際字元。
    gemma4 偶爾把全形空格等字元吐成 '<0xE3><0x80><0x80>' 文字而非該字元本身。"""
    if "<0x" not in text:
        return text

    def _sub(m):
        hexes = re.findall(r"<0x([0-9A-Fa-f]{2})>", m.group(0))
        try:
            return bytes(int(h, 16) for h in hexes).decode("utf-8")
        except Exception:
            return m.group(0)

    return re.sub(r"(?:<0x[0-9A-Fa-f]{2}>)+", _sub, text)


def extract_json(text: str, meta: dict | None = None) -> dict | None:
    """從模型輸出抽出第一個 JSON 物件(容忍 ```json 圍欄與前後雜訊)。

    嚴格解析失敗時退回 json-repair 修復模型常見的「幾乎合法」JSON
    (未跳脫引號、缺逗號、截斷)——LLM 結構化輸出的通病,整個管線共用。

    **給了 `meta` 就會回填 `repaired`(bool),說明這份結果是不是靠 repair 救回來的。**
    呼叫端需要知道這件事,因為 repair 會把壞掉的 JSON **修補成一個合法但語意錯誤的
    物件**,而不是失敗。實際案例(#23):測資生成少打一個 `]`,

        "rows": [["txn_log", 1, ..., null], "expect_flagged": true, ...},

    repair 把後面所有案例都塞進這一筆的 `rows` 裡,那一筆因為形狀不對被剔除,
    後面的案例跟著消失。報告上寫的卻是「條件 C5 缺 false 向案例」——
    **一個標點符號的問題被回報成模型覆蓋不足**,查了很久才發現。

    `meta` 預設 None,既有呼叫端不受影響。"""
    text = decode_byte_fallback(text)  # 修 gemma byte-fallback 亂碼(全形空格等)
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M)
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    parsed = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
                else:
                    if meta is not None:
                        meta["repaired"] = False
                    return parsed
    # 嚴格解析失敗 → json-repair 盡力修復
    try:
        import json_repair
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            repaired = json_repair.loads(m.group(0))
            if isinstance(repaired, dict) and repaired:
                if meta is not None:
                    meta["repaired"] = True
                return repaired
    except Exception:
        pass
    return None


def _reject_duplicate_keys(pairs: list) -> dict:
    """json.loads 的 object_pairs_hook:同一層出現重複鍵就拋錯。

    預設的 json.loads 遇到重複鍵默默取最後一個,於是 `{"who_is_wrong": "sql",
    "who_is_wrong": "testdata"}` 會被讀成 testdata——一份「寫了兩個結論」的輸出看起來
    像有單一結論。重複鍵是不確定,要擋下來。"""
    seen: dict = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError(f"duplicate key: {k!r}")
        seen[k] = v
    return seen


def strict_json_object(text: str | None) -> dict | None:
    """整段輸出(去掉 ```json 圍欄與前後空白後)**剛好是一個 JSON 物件、而且沒有重複鍵**
    才回那個 dict;其餘(前後還有文字、兩個物件、重複鍵、根本不是物件、解析失敗)一律回
    None。**不走 json-repair**。

    跟 `extract_json` 的差別:extract_json 從雜訊裡抽第一個 `{…}`、解析不出還會 repair,
    對「盡量讀出點東西」的場合(主審查)是對的;但仲裁與知識起草要的是「模型給了乾淨、
    唯一、完整的結論」才採信,前後有字、寫了兩個結論、同一個鍵寫兩次、靠 repair 補回來,
    都是不確定,全部不採信。"""
    if text is None:
        return None
    s = decode_byte_fallback(text)
    s = re.sub(r"^```(?:json)?|```$", "", s.strip(), flags=re.M).strip()
    try:
        obj = json.loads(s, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None
