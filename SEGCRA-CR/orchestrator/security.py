"""確定性提示注入掃描器 — 在預掃層(不經 LLM)偵測 MR 文字中的注入攻擊。

設計原則:模型層防線(system prompt 安全邊界)可靠度與模型大小相關,實測小模型
會被壓制。本掃描器是**縱深防禦的最底層**:命中即由管線直接產生 blocker,
攻擊者拿不到自動放行,無論模型是否被攻陷(這個保證由 pipeline.py 的
`enforce_injection` + `apply_policy` 共同落實,兩處都不看模型輸出)。

比對前一律先正規化:攻擊者可以讓文字「人看起來一樣、字元不一樣」
(零寬字元、全形字母、Unicode 標籤字元、關鍵字用底線或換行拆開),
直接對原始文字跑正規表示式會全部漏掉——這是甲方 PR #10 review 指出的
「掃描器可以被 18 種寫法繞過」問題,已針對每種手法各自加上對應的正規化或
偵測規則(見下方個別註解),並補上第 4 點指出的誤判(`fake_authority_zh`
單獨比對常見詞、`output_coercion` 比對任何 `print(`,在異常交易規則的
註解與欄位名裡很常見)。

pattern 來源:OWASP LLM01、PayloadsAllTheThings、常見越獄手法(2026)。
涵蓋中英雙語(攻擊可能用中文公文口吻)。

**仍然擋不住的(規則式掃描器天生的上限,不是這次修的範圍)**:不含指令字眼的
社交工程(如「已故親人」手法,見 eval/README 已知缺口)、同義改寫、字串反轉、
hex 編碼——這些要靠 enforce_injection/apply_policy 的決策硬條件與模型本身,
不是這一層的責任範圍。
"""
import re
import unicodedata
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
    # 只在「對審查器下指令」的語境命中。單獨的「已核准」「管理員」「人工審核」「主管授權」
    # 在異常交易規則的註解與欄位名裡很常見,不能當成攻擊(甲方 PR #10 review 第 4 點)。
    ("fake_authority_zh",
     re.compile(r"(系統訊息|系統指令|最高優先|開發者模式|prompt\s*注入|"
                r"(AI|ai|模型|審查員|審查器|reviewer)\s*(請)?\s*(直接|逕行)?\s*"
                r"(輸出|給|回覆|核准|放行|通過|approve)|"
                r"(主管|管理員|資訊處)[^。\n]{0,20}(預審|已核准|核准通過|授權放行)[^。\n]{0,20}"
                r"(本|此|這個?)?\s*(MR|變更|合併請求)|"
                r"(本|此|這個?)\s*(MR|變更|合併請求)[^。\n]{0,20}(已由|已經)[^。\n]{0,20}"
                r"(預審|核准|放行)|"
                r"(不需|不用|無須|毋須)(再)?(進行)?(任何)?(檢查|審查))")),
    # T-SQL 有 PRINT 陳述式,單獨的 print( 不能當成攻擊(甲方 PR #10 review 第 4 點)
    ("output_coercion",
     re.compile(r"(respond\s+(only\s+)?with|reply\s+with|simply\s+(respond|say|output|print)|"
                r"output\s+(approve|only|exactly)|score\s*[:=]\s*100|verdict\s*[:=]\s*"
                r"[\"']?approve|print\s*\(\s*[\"']?\s*(approve|lgtm|score|verdict))", re.I)),
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

# 不可見字元:軟連字號(U+00AD)、蒙古文元音分隔符(U+180E)、零寬空格家族
# (U+200B-200F)、詞連接符家族(U+2060-2064)、BOM(U+FEFF)、Unicode 標籤字元
# (U+E0000-E007F)。攻擊者可以把這些字元插在關鍵字中間,人看起來一樣,但正規
# 表示式比對不到。用 \u/\U 逐碼點寫死,不貼真的隱形字元——不然這段程式碼
# 本身在 diff/編輯器裡就審不出來寫了什麼(這正是這段規則要防的攻擊手法
# 本身,不能拿它寫規則)。
_INVISIBLE = re.compile(
    "[\u00ad\u180e\u200b-\u200f\u2060-\u2064\ufeff\U000e0000-\U000e007f]")
# 雙向文字控制字元(Trojan Source 手法,U+202A-202E、U+2066-2069):
# 讓畫面上看到的順序跟實際內容不同
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")
# Unicode 標籤字元(U+E0020-E007E)可以夾帶人眼完全看不到、但模型讀得到的文字
# (藏在字串裡的隱形指令)
_TAG = re.compile("[\U000e0020-\U000e007e]")
# 同一個字混用拉丁字母與希臘(U+0370-03FF)/西里爾(U+0400-04FF)字母
# (例:用西里爾字母冒充拉丁字母,肉眼難分辨,如 U+0456 冒充 i、U+043E 冒充 o)
_MIXED_SCRIPT = re.compile(r"\b(?=\w*[A-Za-z])(?=\w*[\u0370-\u03ff\u0400-\u04ff])\w+\b")
# SQL 註解(用來取出自然語言部分)
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_COMMENT_MARK = re.compile(r"--|/\*|\*/")

# base64(含 URL-safe)。邊界用「前後不是 base64 字元」判斷,不用 \b:
# \b 在結尾的 "=" 補位後不成立(= 非單字字元),正規表示式會回溯、
# 把 padding 排除在捕捉之外——長度不再是 4 的倍數,明文非 3 倍數長度的
# payload(三分之二的情況)因此整個被跳過,等於偵測形同虛設。
_B64 = re.compile(r"(?<![A-Za-z0-9+/=_-])([A-Za-z0-9+/_-]{16,}={0,2})(?![A-Za-z0-9+/=_-])")
_B64_KEYWORDS = re.compile(r"ignore|approve|system|instruction|execute|echo|hacked", re.I)


def _normalize(text: str) -> str:
    """標籤字元還原成可見文字、移除不可見字元、全形與數學字母轉回一般字母
    (NFKC:𝗂𝗀𝗇𝗈𝗿𝗲 這類數學粗體、全形 ｉｇｎｏｒｅ 都會正規化回普通拉丁字母)。"""
    revealed = _TAG.sub(lambda m: chr(ord(m.group()) - 0xE0000), text)
    return unicodedata.normalize("NFKC", _INVISIBLE.sub("", revealed))


def _join_comments(text: str) -> str:
    """去掉註解符號並合併空白,讓跨多行 SQL 註解拆開寫的句子能被比對到
    (例如指令被拆成好幾行 `-- ignore` / `-- previous` / `-- instructions`)。"""
    return re.sub(r"\s+", " ", _COMMENT_MARK.sub(" ", text))


def _split_words(text: str) -> str:
    """把底線、連字號、點、駝峰拆成空白,去掉中文字之間的空白,讓
    `IgnorePreviousInstructions`、`ignore_previous_instructions` 這類拼寫變體
    也能被一般的 pattern 比對到。

    **只能用在自然語言**(標題、描述、檔名、SQL 註解):套在程式碼本體上,
    `override_rule_flag`、`developer_mode` 這類完全正常的欄位/變數名會被
    誤判成攻擊字樣——這正是甲方 PR #10 review 第 4 點提醒的誤判來源之一,
    所以呼叫端(`scan_mr`)刻意把「自然語言」跟「程式碼」分開處理,只對
    前者套用這個轉換。
    """
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    t = re.sub(r"[_\-.]+", " ", t)
    t = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", t)
    return re.sub(r"\s+", " ", t)


def _hidden_unicode(text: str) -> list[str]:
    """偵測「文字本身就是攻擊手法」的三種情況(不是靠關鍵字比對,是靠字元本身
    的性質):雙向控制字元、Unicode 標籤字元、拉丁/希臘/西里爾字母混用。"""
    kinds = []
    if _BIDI.search(text):
        kinds.append("雙向控制字元")
    if _TAG.search(text):
        kinds.append("Unicode 標籤字元")
    if _MIXED_SCRIPT.search(text):
        kinds.append("同一個字混用拉丁與希臘/西里爾字母")
    return kinds


def _match_patterns(views: list[str]) -> list[dict]:
    hits = []
    for category, pattern in _PATTERNS:
        for view in views:
            m = pattern.search(view)
            if m:
                hits.append({"category": category, "match": m.group(0)[:60]})
                break
    return hits


def _decode_check(text: str) -> list[dict]:
    hits = []
    for m in _B64.finditer(text):
        blob = m.group(1)
        s = blob.replace("-", "+").replace("_", "/")
        s += "=" * (-len(s) % 4)
        try:
            decoded = b64decode(s, validate=True).decode("utf-8")
        except Exception:
            continue
        # 解碼後的內容用同一套規則(含中文)再比對一次,不只看英文關鍵字——
        # 否則中文內容的 base64 payload(例如編碼過的中文假授權指令)會漏掉。
        plain = _normalize(decoded)
        if _B64_KEYWORDS.search(plain) or _match_patterns([plain, _join_comments(plain)]):
            hits.append({"category": "base64_hidden_payload",
                         "match": blob[:40], "decoded": decoded[:60]})
    return hits


def scan_injection(text: str, prose: str | None = None) -> list[dict]:
    """回傳偵測到的注入訊號清單(空 = 未偵測到)。

    text   要掃描的全部內容
    prose  其中的自然語言部分(標題、描述、檔名、註解);未提供時整段視為
           自然語言(單獨呼叫 `scan_injection(text)` 時的相容用法)
    """
    normalized = _normalize(text)
    views = [normalized, _join_comments(normalized)]
    prose_norm = normalized if prose is None else _normalize(prose)
    views.append(_split_words(_join_comments(prose_norm)))
    hits = _match_patterns(views)
    hits.extend(_decode_check(normalized))
    kinds = _hidden_unicode(text)
    if kinds:
        hits.append({"category": "hidden_unicode", "match": "、".join(kinds)})
    # 去重(同類別只留一筆)
    seen, out = set(), []
    for h in hits:
        if h["category"] in seen:
            continue
        seen.add(h["category"])
        out.append(h)
    return out


def scan_mr(mr: dict) -> list[dict]:
    """掃描整個 MR:標題 + 描述 + **檔名**(甲方 PR #10 review 指出「檔名沒有
    被掃描」)+ 每個檔案的 diff/內容(含程式註解)。"""
    files = mr.get("files", [])
    paths = [p for f in files for p in (f.get("path"), f.get("old_path")) if p]
    code = "\n".join(f.get("full_content") or f.get("diff", "") for f in files)
    comments = "\n".join(m.group(0) for m in _COMMENT.finditer(code))
    head = [mr.get("title", ""), mr.get("description", ""), *paths]
    return scan_injection("\n".join(head + [code]), prose="\n".join(head + [comments]))
