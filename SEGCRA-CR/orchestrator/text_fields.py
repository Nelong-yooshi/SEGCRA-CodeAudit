"""MR 與報告裡的文字欄位(只用標準庫,toolbox 與 orchestrator 都可以匯入)。

- `as_text`:欄位來自 GitLab API、fixture 或模型輸出,型別不保證(GitLab 對沒填說明的 MR 回
  `null`)。不是字串時當成空字串:拿去 `" ".join(...)`、`"\n".join(...)` 時丟的 TypeError 會讓整份
  審查沒有輸出 —— MR 沒擋、沒放,也沒人知道。少看一個壞欄位的代價小得多,那種欄位本來也沒有字。
- `strip_hidden`:MR 說明裡 GitLab 畫面上看不到的部分(HTML 註解、Markdown 的連結定義行)。審核者
  看不到、模型看得到的文字,是對模型下指示最方便的地方;交給審查模型前拿掉。
- `MR_CONTENT_FILE`:程式補的 finding 不對應到某一個檔案時的檔名。只有這一份:去重、校準靠檔名
  分辨時,各處寫法要完全一致。
"""

import re

MR_CONTENT_FILE = "(MR 內容)"


def as_text(value) -> str:
    """只接受字串,其餘(None、數字、巢狀結構)當成空字串。"""
    return value if isinstance(value, str) else ""


# 圍欄程式碼區塊(``` 或 ~~~,最多縮排 3 格):裡面的 <!-- --> 畫面上照樣看得到,不拿
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# 連結定義(`[標籤]: 網址`,含常被拿來寫註解的 `[//]: # (…)`):GitLab 不顯示這一行
_LINK_DEF = re.compile(r"^ {0,3}\[[^\]\n]+\]:[ \t]*\S")


def _strip_comments(text: str) -> tuple[str, int]:
    """拿掉 <!-- … -->;沒有收尾的 <!-- 一路拿到最後(GitLab 的 HTML 區塊也是這樣一路藏到底)。"""
    out, n, pos = [], 0, 0
    while True:
        start = text.find("<!--", pos)
        if start < 0:
            out.append(text[pos:])
            return "".join(out), n
        out.append(text[pos:start])
        n += 1
        end = text.find("-->", start + 4)
        if end < 0:
            return "".join(out), n
        pos = end + 3


def strip_hidden(text) -> tuple[str, int]:
    """MR 說明 → (拿掉畫面上看不到的部分之後的文字, 拿掉幾段)。

    只處理圍欄程式碼區塊以外的部分。行內程式碼(`` `<!-- -->` ``)裡的註解畫面上看得到,這裡也會
    拿掉:結果只是模型少看一小段,審核者看到的不變。"""
    text = as_text(text)
    if "<!--" not in text and "]:" not in text:
        return text, 0
    lines = text.splitlines(keepends=True)
    out, buf, n, fence = [], [], 0, None

    def flush():
        nonlocal n
        kept, c = _strip_comments("".join(buf))
        n += c
        for line in kept.splitlines(keepends=True):
            if _LINK_DEF.match(line):
                n += 1
            else:
                out.append(line)
        buf.clear()

    for line in lines:
        m = _FENCE.match(line)
        if fence is None and m and not _open_comment(buf):
            flush()
            fence = m.group(1)
            out.append(line)
        elif fence is not None:
            out.append(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not line.strip()[len(m.group(1)):].strip():
                fence = None
        else:
            buf.append(line)
    flush()
    return "".join(out), n


def _open_comment(buf: list[str]) -> bool:
    """目前累積的文字裡有沒有開了還沒收尾的 <!--(那時候的 ``` 是註解內容,不是程式碼區塊)。"""
    text = "".join(buf)
    start = text.rfind("<!--")
    return start >= 0 and text.find("-->", start + 4) < 0
