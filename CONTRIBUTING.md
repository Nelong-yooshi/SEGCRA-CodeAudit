# 協作與開發規範 (Contributing)

本專案採用標準化軟體工程協作流程,以確保程式碼品質、版本控制安全與團隊開發效率。

## 開發流程 (Workflow)

為了保護主線程式碼的穩定性,本專案啟用分支保護規則,嚴格禁止直接推送到主分支。

1. **禁止直接 Push**:任何人皆不可直接對 `main` 分支執行 `git push`。
2. **建立開發分支**:開發任何新功能或修復錯誤前,請先從最新的 `main` 切換出新的獨立分支 (Branch)。
   * 範例:`git checkout -b feat/user-auth`
3. **發起 Pull Request (PR)**:開發與測試完成後,將分支推送到遠端,並發起 PR 請求合併至 `main`。
4. **程式碼審查 (Code Review)**:PR 必須經過指派的 Code Reviewer 審查並給予 Approve 後,才能執行合併。

> 審查者由 [`.github/CODEOWNERS`](.github/CODEOWNERS) 自動指派,PR 開啟後會自動加入審查名單。

## Issue 命名與追蹤規則

新增 Issue 時,請務必在標題開頭使用標準化標籤,以便團隊快速辨識任務類型並排定優先級:

* `[bug]`:系統錯誤回報、異常行為修復
* `[feat]`:開發全新功能 (Feature)
* `[doc]`:說明文件、註解或 README 的增刪修改 (Documentation)
* `[refactor]`:程式碼重構 (不改變既有功能的架構調整)
* `[test]`:新增或修改測試案例
* `[chore]`:建置過程、環境設定、輔助套件更新等日常雜務

**標題範例:**

* `[feat] 實作 Discord Webhook 推播通知`
* `[bug] 結帳頁面在 Safari 瀏覽器下破版`
* `[doc] 更新 API 串接規格書`

## Commit 訊息

沿用與 Issue 相同的標籤前綴,讓提交歷史與任務類型一致:

```
feat: 新增 webhook 自動觸發
fix: 修正邊界條件判斷
doc: 補充規格撰寫說明
```

## 提交前檢查

* 不要提交任何憑證、token、密碼或內部主機資訊(`.gitignore` 已排除 `*.env`,但仍請自行確認)。
* 不要提交執行產物(`review_output/`、`.venv/`、`__pycache__/`)。
