---
name: sql-review
description: SQL MR 審查核心清單 — 每次審查常駐載入
trigger: always
---

# SQL 審查清單

審查程序:**先讀 sqltools 預掃結果**(AST/lint/rule-base),已被規則抓到的不要重複報,
把 LLM 的力氣花在規則抓不到的語意問題上。

## 正確性
- JOIN 造成的列數膨脹(fan-out):聚合前是否先去重?一對多 JOIN 後 SUM/COUNT 是否重複計算?
- NULL 語意:`NOT IN` + 子查詢的 NULL 陷阱、`<>` 比較遺漏 NULL、LEFT JOIN 後對右表欄位過濾等同 INNER JOIN
- 日期/時間邊界:`BETWEEN` 含尾端造成重複計入、時區假設、跨日/跨月邊界 off-by-one
- 隱式型別轉換:字串與數值比較、前導零帳號被轉數值

## 規格核對
- 任務中附有核定規格(spec)時,**逐項核對實作與規格**:比較運算子與規格用語必須一致
  (「達/以上」=含=`>=`;「超過」=不含=`>`)、時間窗、通報粒度、豁免條件、輸出欄位。
  不符即為 major「實作與核定規格不符」,並指出哪些邊界案例會漏報/誤報。

## 效能(涉及大表/索引/JOIN 時 → load_skill("perf-review") 深入)
- 明顯訊號:全表掃描、對索引欄位包函數、大表 JOIN 缺過濾;深入分析見 perf-review skill

## 資安(真正該防的)
- **硬編碼憑證**:SQL/連線字串出現明碼密碼、API key、token、私鑰 → blocker(見 `secure-sql`)
- **異常查詢**:撈 password/token/card_no 等敏感欄位、無條件 dump 整張敏感表、
  UNION 拼接非預期表(疑似外洩/探測)→ 載入 `secure-sql` 判斷正當性
- 動態 SQL 字串串接(注入風險)——參數化才能過

## 個資輸出(注意:通常不是問題)
- 通報單輸出個資(帳號、姓名、身分證號)交付調查部門屬**核定用途**,隱私欄位已於
  前置加密 → **不要**當 finding。只需確認「僅輸出必要欄位」。細節見 `secure-sql` / 團隊慣例。

## 交易與鎖
- UPDATE/DELETE 影響範圍是否可預估?有無先以 SELECT 驗證的註記?
- 長交易持鎖、鎖升級;批次更新無分批機制

## 可維護性
- 命名是否符合團隊慣例(見 memory conventions)
- magic number 未註解(尤其異常交易閾值——觸發 `anomaly-rules` skill)

## 判斷原則
- 每個 finding 必須:指到 **具體檔案與行**、說明 **為什麼是問題**(最好給反例情境)、給 **可直接套用的修改建議**
- 不確定的用 info/minor 提出疑問,不要武斷擋件
- 曾被人工駁回的同型 finding(見 memory lookup)→ 降級或不報
