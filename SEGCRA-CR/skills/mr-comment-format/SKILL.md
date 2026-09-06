---
name: mr-comment-format
description: 審查輸出格式 — severity 分級、評分 rubric、GitLab 留言格式;產出最終報告時載入
trigger: on_demand
---

# 審查輸出格式

## Severity 分級
| 級別 | 定義 | 例子 |
|---|---|---|
| `blocker` | 合入將造成錯誤結果、資損或違規 | UPDATE/DELETE 無 WHERE、hardcode 憑證、明確的資損 |
| `major` | 產出會錯、或**違反核定規格任一需求項目** | NOT IN NULL 陷阱、時間窗邊界錯、閾值含等不符規格、fan-out 重複通報、通報粒度不符規格、大表全掃 |
| `minor` | 應改善但不影響正確性 | 命名不符慣例、缺註解、可讀性 |
| `info` | 純疑問或無法判定需人確認 | 詢問業務意圖、待確認的檢核點 |

## Severity 校準鐵則(避免低估)
分級看「**對正確性/規格的影響**」,不看「問題看起來大不大」:
- **會讓結果算錯或漏抓/誤抓的 → 至少 major**,即使成因看似小(如 `>` vs `>=`、
  GROUP BY 多帶一個欄位造成聯名戶 fan-out)。這類**不可**降為 minor/info。
- **違反核定規格任一條需求項目 → major**(規格是核定的,不符就是實作錯,不是「建議」)。
- `SELECT *` 這種**不影響正確性**的才是 minor;fan-out、邊界、含等這種**影響正確性**的是 major。
- 只有「無法從程式判定、需要問人」的才放 info(如沖正是否已淨額)。
- 判斷法:自問「這條若不改,規則上線後會不會算錯一筆?」會 → major 起跳。

## 評分 rubric(0–100)
- 起始 100;blocker 每個 −40、major −15、minor −5、info −0
- 有任何 blocker → 總分上限 59(不及格)
- 下限 0。分數只反映「此 MR 可否合入」,不評價作者。

## 引用(citations)
引用**只允許兩種來源**:任務中附上的核定規格(spec 檔),或團隊慣例(binding /
guideline 知識項)。其他來源(未附上的文件、憑印象的條文)一律不得引用——
管線會確定性剔除不在白名單內的引用。

## 最終輸出(嚴格 JSON,不要加 markdown 圍欄)
```json
{
  "score": 72,
  "verdict": "needs_changes",        // approve | needs_changes | reject
  "summary": "一段話總評,面向 MR 作者",
  "findings": [
    {
      "file": "sql/rules/daily_limit.sql",
      "line": 23,
      "severity": "major",
      "title": "閾值含等與核定規格不符",
      "detail": "為什麼是問題 + 具體情境",
      "suggestion": "可直接套用的修改(給 SQL 片段)",
      "citations": [{"source": "規格 R-201", "article": "需求項目3"}]
    }
  ]
}
```

## 訊噪比(嚴格遵守)
- 純風格/lint 類問題(行長、AS 別名、關鍵字大小寫、結尾換行、縮排)**不逐條列為 finding**;
  與團隊慣例無關的風格問題,整份報告最多彙總成一條 info(「建議以 formatter 統一風格」)
- lint 預掃結果是給你定位用的線索,不是抄進報告的素材
- 一份報告的價值由最重要的 3-5 條 finding 決定,不由條數決定

## 留言語氣
- 具體、對事不對人;每個 finding 給修改建議,不只指出問題
- 不寫空泛評語(「建議注意效能」不合格;「`WHERE TRUNC(tx_time)=...` 使 idx_tx_time 失效,
  改為半開區間比較」合格)
