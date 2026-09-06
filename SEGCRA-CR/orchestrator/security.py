"""確定性提示注入掃描器 — 在預掃層(不經 LLM)偵測 MR 文字中的注入攻擊。

設計原則:模型層防線(system prompt 安全邊界)可靠度與模型大小相關,實測小模型
會被壓制。本掃描器是**縱深防禦的最底層**:命中即由管線直接產生 blocker,
攻擊者拿不到自動放行,無論模型是否被攻陷。

pattern 來源:OWASP LLM01、PayloadsAllTheThings、常見越獄手法(2026)。
涵蓋中英雙語(攻擊可能用中文公文口吻)。
"""
import re
from base64 import b64decode

# 每條:(類別, 正規表示式)。命中任一即視為疑似注入。
_PATTERNS = [
    ("instruction_override",
     re.compile(r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+|previous\s+|"
                r"above\s+|prior\s+)*(previous\s+|prior\s+|above\s+)?"
                r"(instruction|prompt|rule|context|direction)", re.I)),
    ("instruction_override_zh",
     re.compile(r"(忽略|無視|忽視|略過|跳過|不要理會|不需|毋須)(前面|先前|上述|以上|之前|所有|"
                r"任何|一切|這些|那些)*(的)?(指示|指令|規則|提示|檢查|審查|步驟|限制|流程)")),
    ("fake_authority",
     re.compile(r"(important\s+new\s+instruction|new\s+instruction|system\s+(prompt|message|"
                r"override)|developer\s+mode|admin\s+mode|note\s+to\s+(ai|assistant|reviewer)|"
                r"you\s+are\s+now|as\s+an?\s+ai|dan\s+mode)", re.I)),
    ("fake_authority_zh",
     re.compile(r"(系統訊息|系統指令|最高優先|管理員|開發者模式|預審|人工審核|已核准|"
                r"主管(指示|要求|授權|預審)|直接(輸出|通過|放行|approve|核准)|請直接|"
                r"給\s*(100|滿分)|prompt\s*注入)")),
    ("output_coercion",
     re.compile(r"(respond\s+(only\s+)?with|reply\s+with|simply\s+(respond|say|output|print)|"
                r"output\s+(approve|only|exactly)|score\s*[:=]\s*100|verdict\s*[:=]\s*"
                r"[\"']?approve|print\s*\()", re.I)),
    ("prompt_leak",
     re.compile(r"((reveal|show|print|provide|repeat|output|leak|give)\s+(me\s+)?"
                r"(your\s+|the\s+)?(complete\s+|full\s+|entire\s+)?(system\s+)?"
                r"(prompt|instruction|configuration|rules)|"
                r"(text|content)s?\s+of\s+(the\s+|your\s+)?(system\s+)?prompt|"
                r"你的(系統)?(提示|指令|prompt))", re.I)),
    ("encoding_directive",
     re.compile(r"(decode|base64|from\s*hex|unescape|eval|atob)\s*[\(:]|"
                r"decode\s+and\s+execute", re.I)),
    ("html_entity_obfuscation",
     re.compile(r"(&#x?[0-9a-f]{2,};){3,}", re.I)),
    ("role_hijack",
     re.compile(r"(you\s+are\s+(the\s+)?system|pretend\s+(you|to\s+be)|"
                r"act\s+as\s+(if|though|an?\s+unrestricted)|new\s+persona)", re.I)),
]

# 可疑的 base64 blob:長度 ≥16 的 base64 字元串,解碼後含 ASCII 可讀指令關鍵詞
_B64 = re.compile(r"\b([A-Za-z0-9+/]{16,}={0,2})\b")
_B64_KEYWORDS = re.compile(r"ignore|approve|system|instruction|execute|echo|hacked", re.I)


def _decode_check(text: str) -> list[dict]:
    hits = []
    for m in _B64.finditer(text):
        blob = m.group(1)
        if len(blob) % 4:
            continue
        try:
            decoded = b64decode(blob).decode("utf-8", "ignore")
        except Exception:
            continue
        if _B64_KEYWORDS.search(decoded):
            hits.append({"category": "base64_hidden_payload",
                         "match": blob[:40], "decoded": decoded[:60]})
    return hits


def scan_injection(text: str) -> list[dict]:
    """回傳偵測到的注入訊號清單(空 = 未偵測到)。"""
    hits = []
    for category, pattern in _PATTERNS:
        m = pattern.search(text)
        if m:
            hits.append({"category": category, "match": m.group(0)[:60]})
    hits.extend(_decode_check(text))
    # 去重(同類別只留一筆)
    seen, out = set(), []
    for h in hits:
        if h["category"] in seen:
            continue
        seen.add(h["category"])
        out.append(h)
    return out


def scan_mr(mr: dict) -> list[dict]:
    """掃描整個 MR:標題 + 描述 + 每個檔案的 diff/內容(含程式註解)。"""
    parts = [mr.get("title", ""), mr.get("description", "")]
    for f in mr.get("files", []):
        parts.append(f.get("full_content") or f.get("diff", ""))
    return scan_injection("\n".join(parts))
