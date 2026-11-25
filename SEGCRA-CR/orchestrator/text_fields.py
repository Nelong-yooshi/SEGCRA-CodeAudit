"""MR 與報告裡的文字欄位(只用標準庫,toolbox 與 orchestrator 都可以匯入)。

- `as_text`:欄位來自 GitLab API、fixture 或模型輸出,型別不保證(GitLab 對沒填說明的 MR 回
  `null`)。不是字串時當成空字串:拿去 `" ".join(...)`、`"\n".join(...)` 時丟的 TypeError 會讓整份
  審查沒有輸出 —— MR 沒擋、沒放,也沒人知道。少看一個壞欄位的代價小得多,那種欄位本來也沒有字。
- `MR_CONTENT_FILE`:程式補的 finding 不對應到某一個檔案時的檔名。只有這一份:去重、校準靠檔名
  分辨時,各處寫法要完全一致。
"""

MR_CONTENT_FILE = "(MR 內容)"


def as_text(value) -> str:
    """只接受字串,其餘(None、數字、巢狀結構)當成空字串。"""
    return value if isinstance(value, str) else ""
