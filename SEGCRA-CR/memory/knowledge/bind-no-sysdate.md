---
id: bind-no-sysdate
tier: binding
source: user-authored
scope:
- anomaly-rules
- core-banking
tags:
- 批次
- 時間
evidence: 1
ts: 1783612400
---
SQL 內不得使用 CURRENT_DATE / SYSDATE;日期切齊一律在批次參數層以 :start_date/:end_date(半開區間)傳入。
