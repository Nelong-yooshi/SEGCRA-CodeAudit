---
id: bind-reversal-netted
tier: binding
source: user-explicit
scope:
- anomaly-rules
suppress:
- H001
conflict_key: reversal-handling
tags:
- 沖正
- 退匯
- 淨額
evidence: 1
ts: 1783612400
---
交易資料已由前置系統統一淨額(沖正/退匯已於進檔前處理),異常交易規則**無需**在 SQL 內再處理沖正/退匯;H001 檢核點對本組織資料一律不適用。
