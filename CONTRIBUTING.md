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

### 一個 PR 做一件完整的事

* 一個 PR 對應一個 issue(或 issue 裡明確切出的一塊)。改完、自己測過、說明寫好,再請人審。
* commit 可以很細,PR 不要。同一個 issue 的小修正累積在同一個分支,不要每改一點就開一個 PR。
* 還在做的先開 **Draft PR**,不要 request review;完成後再轉成 Ready for review。
* 跟 PR 主題無關的修正(順手發現的 bug、別處的測試)另開分支、另開 PR,或先記成 issue。
* PR 說明寫清楚:對應哪個 issue(`Closes #N`)、做了什麼、怎麼驗證、哪些刻意沒做。

### 審查之後的修改

* review 之後補的 commit,推完請留一則留言,列出補了哪些 commit、各對應哪一點意見,審查者才不會看到舊版本。
* 審查者提出、但不屬於這個 PR 範圍的事,開成新 issue 追蹤,不要塞進同一個 PR。

### 等待審查不是停工的理由

PR 還沒合併,不影響你開下一個分支繼續做。下一個分支從哪裡開:

| 情況 | 從哪裡開分支 | PR 的 base |
|---|---|---|
| 跟還沒合併的 PR 無關(大部分情況) | 最新的 `main` | `main` |
| 需要用到還沒合併的 PR 裡的東西(函式、schema、設定) | 那個 PR 的分支 | **那個 PR 的分支** |

疊在別的 PR 上(stacked PR)時:

* base 選下層 PR 的分支,diff 才會只有你新加的部分。PR 說明寫明「疊在 #N 上」。
* 下層合併後,把你的分支 rebase 到 `main`,base 改回 `main`(下層分支被刪除時,GitHub 會自動把 base 改回 `main`):

  ```bash
  git fetch origin
  git rebase --onto origin/main <下層分支> <你的分支>
  git push --force-with-lease
  ```

  下層如果是用 squash 合併的,一定要用 `--onto`,不然下層的 commit 會被重放一次。
* 最多疊一兩層。疊得更多,通常代表拆法有問題。

### 衝突

衝突是多人開發的日常:兩個人改到同一段,git 無法自己決定留哪個,要人判斷。**後合併的人負責解**:同步 `main`、在自己的分支上解完、跑過測試再推。

```bash
git fetch origin
git rebase origin/main        # 有衝突:改檔 → git add → git rebase --continue
git push --force-with-lease
```

分支如果有其他人一起在用,改用 `git merge origin/main`,不要 rebase。

減少衝突的方法:分工照模組或檔案切開、分支不要放太久、常同步 `main`、大範圍的格式化或改名獨立成一個 PR,不要混在功能 PR 裡。

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

## Discord 通知

PR 的狀態變化會自動送到 Discord 頻道,由 [`.github/workflows/discord-notify.yml`](.github/workflows/discord-notify.yml) 處理,依情境 tag 不同的人:

| 情境 | 會 tag 誰 |
|---|---|
| PR 開啟 / 重新開啟 / 草稿轉正式 | code review 群組 |
| 審查已核准 / 需要修改 / 有審查意見 | PR 作者 |
| PR 已合併 / 已關閉未合併 | PR 作者 |

草稿 PR、作者自審、沒有總評的行內留言、非成員的審查都不會發通知。

webhook URL 與 Discord 身分對照表放在 repo Settings 的 Secrets 與 Variables,不寫進原始碼。對照表沒設定時通知照送,只是顯示 GitHub 帳號、不會 @ 到人。設定項目見該檔開頭的註解。

## 黑盒評測

除了 `eval/` 的 golden set,本專案另有一套由 Segora 維護的黑盒評測,用來量審查管線在真實型態 MR 上的表現。

* **什麼時候跑**:PR 合併進 `main` 之後,由維護者的評測環境評最新的 `main`,通常隔天會有結果;另外每週會跑一次比較完整的題組。開著的 PR 不會自動評,合併前想先看,請找維護者。
* **結果在哪裡**:貼在對應的已合併 PR 上,包括一則就地更新的留言、`黑盒 X` 標籤,以及 `segcra/blackbox` 的 commit 狀態。這個狀態**不是必要檢查,不會擋合併**。
* **看得到什麼**:各類別的通過數,以及歷次的變化。題目內容、題號與埋設的缺陷都不公開。要改進的方向會寫在 issue 裡,請照 issue 描述的機制修正,不要針對分數猜題。
* **與 golden set 的分工**:golden set 是本專案自己維護、公開的回歸測試;黑盒是獨立的外部量測。兩者的分數不能直接比較。

本 repo 不含任何觸發黑盒評測的設定(沒有 GitHub Actions、self-hosted runner 或留言指令)。評測在外部環境執行,只評已合併進 `main` 的程式;開 PR、fork、在 PR 留言都不會觸發評測。

## 提交前檢查

* 不要提交任何憑證、token、密碼或內部主機資訊(`.gitignore` 已排除 `*.env`,但仍請自行確認)。
* 不要提交執行產物(`review_output/`、`.venv/`、`__pycache__/`)。
