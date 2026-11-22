"""例外 → 可以寫進報告、MR 留言、模型對話的說法:**只有例外型別與 HTTP 狀態碼,不放原文**。

例外的原文是對方決定的:模型端點回 401 時,openai 套件會把錯誤內容的 message 放進例外訊息,
裡面可能有收到的金鑰(或其中幾碼)、內部主機名稱;httpx 的訊息帶完整網址。審查程式控制
不了這些內容,而報告會貼到 MR、模型對話會被模型照抄進 finding。主審查遇到同樣的情況用的
是固定說法(pipeline._MODEL_FAILURE_TEXT),這裡給其他地方用同一個原則。

各模組只把**自己的**例外類別(訊息是自己寫的固定文字)與幾種固定說法傳進 safe_error;其他例外
一律只留型別與狀態碼。例外:dbt 展開的錯誤照舊放原文(那是 MR 作者要看的,見 dbt_render)。
"""
from __future__ import annotations


def _status_code(e: BaseException) -> int | None:
    code = getattr(e, "status_code", None)                 # openai.APIStatusError
    if code is None:
        code = getattr(getattr(e, "response", None), "status_code", None)   # httpx.HTTPStatusError
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def error_label(e: BaseException) -> str:
    """`AuthenticationError,HTTP 401` 這種說法。"""
    code = _status_code(e)
    return type(e).__name__ + (f",HTTP {code}" if code is not None else "")


def safe_error(e: BaseException, *, own=(), fixed=None, other: str | None = None,
               keep_message: bool = False, max_chars: int = 300) -> str:
    """例外 → 可以貼出去的一行字。依序:

    - `own`:自己的例外類別,訊息是自己寫的固定文字 → `型別: 訊息`
    - `fixed`:`{例外類別: 整句說法或 callable(e)}`,第一個符合的
    - `keep_message=True`:`型別: 原文`(只給訊息本來就要給人看的地方,例如 dbt 展開錯誤)
    - 其他:`error_label(e)`,有 `other` 時接成 `型別: other`

    結果去掉控制字元、限制長度。"""
    if own and isinstance(e, own):
        text = f"{type(e).__name__}: {e}"
    else:
        text = None
        for cls, say in (fixed or {}).items():
            if isinstance(e, cls):
                text = say(e) if callable(say) else say
                break
        if text is None:
            if keep_message:
                text = f"{type(e).__name__}: {e}"
            else:
                text = error_label(e) + (f": {other}" if other else "")
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:max_chars]
