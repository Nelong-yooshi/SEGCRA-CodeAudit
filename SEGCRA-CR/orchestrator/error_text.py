"""例外 → 可以寫進報告、MR 留言、模型對話的說法:**只有例外型別與 HTTP 狀態碼,不放原文**。

例外的原文是對方決定的:模型端點回 401 時,openai 套件會把錯誤內容的 message 放進例外訊息,
裡面可能有收到的金鑰(或其中幾碼)、內部主機名稱;httpx 的訊息帶完整網址。審查程式控制
不了這些內容,而報告會貼到 MR、模型對話會被模型照抄進 finding。主審查遇到同樣的情況用的
是固定說法(pipeline._MODEL_FAILURE_TEXT),這裡給其他地方用同一個原則。
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
