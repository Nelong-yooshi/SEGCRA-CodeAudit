# 知識項格式說明(TEMPLATE)

本檔是說明文件,**不以 `---` 開頭,因此不會被知識庫載入**。
新增知識項時,建一個 `<id>.md`,內容為「YAML frontmatter + 本文」:

    ---
    id: bind-reversal-netted        # 檔名同 id
    tier: binding                   # binding | guideline | observed
    source: user-explicit           # user-explicit | user-authored | auto-consolidated | code-mined
    scope:                          # 生效範圍,可多個;"*" = 全部
    - anomaly-rules
    suppress:                       # (選填)binding 專用:要在預掃層確定性濾除的檢核點代碼
    - H001
    code: S-COMMA                   # (選填)對應的確定性風格檢查碼,有填才會啟用該掃描
    conflict_key: reversal-handling # (選填)同 key 的知識項互斥,權威高者勝
    tags:                           # 檢索用關鍵詞
    - 沖正
    - 淨額
    evidence: 1                     # 支持此項的範例數(observed 用)
    ts: 1783612400                  # unix timestamp(權威平手時較新者勝)
    ---
    (本文)用完整、明確的書面語寫這條規範,審查 prompt 會直接注入。

## 欄位語意
- **tier**
  - `binding`:恆常規範。scope 內**永遠注入** prompt,且 `suppress` 列的檢核點會在
    預掃層被確定性濾除(「講過的規定不再重複報」,不靠模型自律)。
  - `guideline`:大量參考慣例,依 scope + 相關度檢索 top_k 注入。
  - `observed`:從程式碼/歷史觀察學得的習慣,權威最低。
- **source(權威分級,同 conflict_key 衝突時高者勝)**
  - `user-explicit`(40):使用者明確指示 > `user-authored`(30):人工撰寫
    > `auto-consolidated`(20):學習迴路草擬且經人核准 > `code-mined`(10):程式觀察。
- **suppress**:填預掃檢核點代碼(H001 沖正、H002 粒度、H003 規格核對、
  H004 敏感查詢、H005 遮罩)。只有 `tier: binding` 的項目會生效。
- **code**:確定性風格檢查碼(目前支援 S-COMMA 前置逗號)。被更高權威覆蓋即停用。

## 本目錄附帶的示例
- `bind-reversal-netted.md`:使用者明確指示「資料已淨額」→ 抑制 H001 檢核點。
- `bind-no-sysdate.md`:人工撰寫的恆常規範(禁用 CURRENT_DATE/SYSDATE)。
