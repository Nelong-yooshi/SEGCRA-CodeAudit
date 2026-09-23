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

# 診斷用:設成一個目錄路徑,就把每一次**實際送出的** messages 原文落檔。
# 預設空字串 = 完全不啟用,對正式流程沒有任何影響。
#
# 用途只有一個,但那是目前唯一沒有結論的問題:整條管線跑兩次結果不同,
# 到底是「模型取樣」還是「送出去的輸入本來就不一樣」。把兩次的 prompt diff 一下
# 就分得出來——prompt 相同 → 取樣;prompt 不同 → 輸入,而且 diff 直接指出哪一段在變。
# 詳見 eval/BASELINE.md §1 與 §7。
DUMP_PROMPTS = os.environ.get("SEGCRA_DUMP_PROMPTS", "")
_dump_seq = 0


def _dump(profile_model: str, messages: list, iteration: int) -> None:
    """把一次呼叫的完整 messages 寫成一個檔。失敗一律吞掉——診斷功能不該弄垮跑批。"""
    global _dump_seq
    if not DUMP_PROMPTS:
        return
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
    except Exception:
        pass


async def run_agent(cfg: Config, profile: ModelProfile, system: str, user: str,
                    hub: ToolHub, verbose: bool = True, use_tools: bool = True,
                    trace: list | None = None) -> str:
    """trace 給定時,把每次工具呼叫記錄下來(供產生逐字對話 transcript)。"""
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
    for i in range(MAX_ITERATIONS):
        _dump(profile.model, messages, i)
        resp = await client.chat.completions.create(
            model=profile.model, messages=messages, **tool_kwargs,
            temperature=profile.temperature,
            max_tokens=profile.max_output_tokens,
            **seed_kwargs,
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
                result = await hub.call(tc.function.name, args)
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


def extract_json(text: str) -> dict | None:
    """從模型輸出抽出第一個 JSON 物件(容忍 ```json 圍欄與前後雜訊)。
    嚴格解析失敗時退回 json-repair 修復模型常見的「幾乎合法」JSON
    (未跳脫引號、缺逗號、截斷)——LLM 結構化輸出的通病,整個管線共用。"""
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
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    # 嚴格解析失敗 → json-repair 盡力修復
    try:
        import json_repair
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            repaired = json_repair.loads(m.group(0))
            if isinstance(repaired, dict) and repaired:
                return repaired
    except Exception:
        pass
    return None
