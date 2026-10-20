"""Agent loop — 本地模型(OpenAI-compatible)+ ToolHub 工具呼叫迴圈。"""
import json
import os
import re

from openai import AsyncOpenAI

from .config import Config, ModelProfile, estimate_tokens
from .tool_hub import ToolHub

MAX_ITERATIONS = 12

# 本地大模型單次生成可能超過 10 分鐘(openai client 預設 timeout 600s 且自動重試
# 2 次——對本地推理只是把同一個慢請求連跑三遍再炸 Timeout)。改為:單次請求
# 上限 1 小時、不自動重試;可用環境變數 LLM_TIMEOUT(秒)覆蓋。
REQUEST_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "3600"))


def output_truncated(meta: dict, max_output_tokens: int) -> bool:
    """run_agent 回填的 meta(最後一次回應)是不是被輸出長度上限切斷。

    端點回 `finish_reason == "length"`,或回報的 `completion_tokens` 已達 `max_output_tokens`,
    任一成立就算:不是每個端點碰到上限都照規格回 `length`,而用量達到上限本身就代表輸出被
    切斷。兩者都沒回報時判成沒有截斷——沒有依據就報,每一次呼叫都會被當成截斷。

    主審查與測資生成都用這一個判斷。"""
    if meta.get("finish_reason") == "length":
        return True
    used = meta.get("completion_tokens")
    return used is not None and used >= max_output_tokens


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
    client = AsyncOpenAI(base_url=cfg.endpoint, api_key=cfg.api_key,
                         timeout=REQUEST_TIMEOUT, max_retries=0)
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
    for i in range(MAX_ITERATIONS):
        dump_path = _dump(profile.model, messages, i)
        resp = await client.chat.completions.create(
            model=profile.model, messages=messages, **tool_kwargs,
            temperature=profile.temperature,
            max_tokens=profile.max_output_tokens,
            **seed_kwargs, **effort_kwargs,
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
        if meta is not None:
            # 每一輪都覆寫:呼叫端關心的是**最後一次**回應有沒有被截斷,
            # 中間輪次的工具呼叫回合即使碰到上限也會繼續跑下去。
            meta["finish_reason"] = resp.choices[0].finish_reason
            meta["completion_tokens"] = getattr(usage, "completion_tokens", None) if usage else None
        _dump_response(dump_path, msg.content or "", resp.choices[0].finish_reason,
                       getattr(usage, "completion_tokens", None) if usage else None,
                       prompt_tokens,
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
                result = f"(工具執行失敗:{e})"
            if trace is not None:
                trace.append({"step": i, "tool": tc.function.name, "args": args,
                              "result_preview": (result or "")[:400]})
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return messages[-1].get("content") or "(達到工具呼叫上限,未產出最終結果)"


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
