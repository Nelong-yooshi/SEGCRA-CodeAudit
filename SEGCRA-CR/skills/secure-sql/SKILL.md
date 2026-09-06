---
name: secure-sql
description: SQL 資安審查 — hardcode 憑證、異常/可疑查詢、最小權限;涉及帳密/憑證/敏感存取時載入
trigger: on_demand
---

# SQL 資安審查

聚焦「程式本身的資安風險」。

## 一律 blocker
- **硬編碼憑證**:SQL 或連線字串中出現明碼密碼、API key、token、私鑰
  (`password='...'`、`api_key=...`、`user:pass@host`)。資安紅線,一律擋。
  → 應改用參數綁定 / 密鑰管理服務(KMS/Vault),憑證不進版本庫。
- **SQL 注入面**:以字串串接使用者輸入組 SQL(而非參數化)。

## 需判斷正當性(major/需提問)
- **撈憑證欄位**:`SELECT` 取出 `password`/`pwd`/`secret`/`token`/`card_no`/`cvv`
  等敏感欄位 —— 這通常不該出現在業務查詢,確認用途與最小必要;不正當即 major。
- **明碼比密碼**:`WHERE password = '...'` —— 密碼應以雜湊比對,且不得寫進 SQL。
- **異常大範圍存取**:無條件讀取整張敏感表(users/credentials/customers 全表 dump)、
  或 `UNION` 拼接非預期資料表(疑似資料外洩/探測);與規則業務目的不符即 major。
- **權限外操作**:規則腳本卻對權限表、系統表(pg_*, information_schema)寫入或授權。

## 明確「不是」finding(避免誤報)
- **通報單輸出個資**(account_id、姓名、身分證號)交付調查部門屬**核定用途**,
  且隱私欄位已於前置加密 —— 這**不是**資安問題,不要當 finding 報。
  只需確認「僅輸出必要欄位」即可。真正該防的是上面的 hardcode 憑證與異常查詢。

## 判斷原則
- 「這段 SQL 若被有心人拿到,會洩漏什麼、能探測什麼?」——這才是資安視角
- 憑證外洩無論多小一律 blocker;可疑存取給 major 並要求說明用途
