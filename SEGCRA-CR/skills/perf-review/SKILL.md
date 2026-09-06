---
name: perf-review
description: SQL 效能深入審查 — 索引、掃描、JOIN 成本;涉及大表查詢或效能疑慮時載入
trigger: on_demand
---

# SQL 效能審查

批次規則多在大交易表上跑,效能問題會拖垮整個 T+1 批次窗。

## 索引與掃描
- **全表掃描**:WHERE 過濾欄位無索引 → 建議評估索引(但索引異動需離峰、不鎖表)
- **索引失效**:對索引欄位包函數(`WHERE TRUNC(tx_time)=...`、`WHERE UPPER(acct)=...`)、
  隱式型別轉換(字串欄位比數值)、前綴萬用字元 `LIKE '%x'` → 索引用不到
- **覆蓋索引**:高頻規則可評估把 SELECT/WHERE/GROUP BY 欄位納入複合索引

## JOIN 與子查詢
- 大表 JOIN 缺過濾條件、笛卡兒積風險
- 相關子查詢逐列執行(N+1)→ 可改 JOIN 或 window function
- 重複掃描同一大表 → 可用 CTE/暫存表掃一次
- `SELECT *`:寬表傳輸成本、視圖疊加放大(此點正確性影響小,效能與維護性中等)

## 聚合
- 先過濾再聚合(WHERE 早於 GROUP BY 收斂資料量)
- `HAVING` 能改寫成 `WHERE` 的部分先做(HAVING 在聚合後才篩)
- `DISTINCT` / 大量 `GROUP BY` 欄位的排序成本

## 批次特性
- 影響逾十萬筆的 UPDATE/DELETE 應分批、可中斷續跑
- 索引建立用 `CREATE INDEX CONCURRENTLY`,離峰執行

## severity 拿捏
- 效能問題多為 **minor/info**(不影響正確性);但**確定會拖垮批次窗**(如千萬列全掃、
  笛卡兒積)可到 **major**。不要把效能建議標成 blocker(除非會導致批次逾時失敗)。
