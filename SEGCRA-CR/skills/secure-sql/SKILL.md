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
- **讀取執行環境**(預掃 R005):`DB_NAME()`、`@@SERVERNAME`、`HOST_NAME()`、`SUSER_NAME()`、
  `SERVERPROPERTY()`、`sys.databases`/`sys.objects`、`INFORMATION_SCHEMA` 等。規則程式不需要
  知道自己跑在哪個資料庫、主機或身分下;會讀的話,就能**在執行驗證的沙盒裡照規格跑、
  到正式環境才做別的事**(例如 `AND DB_NAME() NOT LIKE 'segcra%'`),執行驗證反而會通過。
  看到這類寫法要追問用途:找不到業務上的理由就是 major,不能因為「執行驗證通過」就放行。
- **危險 SQL 原語**(預掃 R006–R010):理由要寫明是哪一類,不要混在「語法錯誤」或
  「效能問題」裡報——類別錯了,看留言的人就不會知道這是資料外送或權限變更。
  - **外部資料源讀寫**:`OPENROWSET`、`OPENDATASOURCE`、`OPENQUERY`、`BULK INSERT`、
    四段式名稱(`伺服器.資料庫.schema.物件`)。規則 SQL 只該讀寫本資料庫裡的表;
    讀寫外部來源等於把資料送出去、或把外部輸入帶進來,而沙盒驗不到對方那一端。
  - **作業系統命令**:`xp_cmdshell`(常寫成 `master..xp_cmdshell`)。這是在資料庫主機上
    執行作業系統指令,不是查詢。
  - **權限授予或切換**:`GRANT`/`REVOKE`/`DENY`、`EXECUTE AS`、`ALTER (SERVER) ROLE`。
    換身分之後的行為沙盒驗不到。
  - **動態 SQL**:`EXEC(…)`、`sp_executesql`(前面接 `sys.`、`master.dbo.` 或方括號都算),以及 `EXEC` 後面直接接變數。實際執行的內容在編譯期看不到,規則層與
    沙盒都驗不到,而且可能藏有讀取環境的寫法(`EXEC('SELECT DB_' + 'NAME()')` 這種
    拆字串的寫法,文字比對看不到)。
  - **dbt 編譯期副作用**:macro 裡用 `run_query` / `{% call statement %}` 執行 DML。
    每次 compile 都會跑,卻不在編譯出來的 SQL 裡,執行驗證看不到它。
- **環境相依但正常也會用的寫法**(預掃 H006–H009,hint):檢查物件存不存在
  (`OBJECT_ID`/`COL_LENGTH`)、連線的語系與日期設定(`@@LANGUAGE`/`@@DATEFIRST`)、
  dbt 編譯期的環境資訊(`target.name`、`env_var()`、`{% if execute %}`)、
  現在時間與寫死的日期比較。這幾種**不要直接給 major**——開發與正式分流本來就會用到;
  但要回應:說明用途,或指出它會讓沙盒與正式環境走不同分支。

## 明確「不是」finding(避免誤報)
- **通報單輸出個資**(account_id、姓名、身分證號)交付調查部門屬**核定用途**,
  且隱私欄位已於前置加密 —— 這**不是**資安問題,不要當 finding 報。
  只需確認「僅輸出必要欄位」即可。真正該防的是上面的 hardcode 憑證與異常查詢。

## 判斷原則
- 「這段 SQL 若被有心人拿到,會洩漏什麼、能探測什麼?」——這才是資安視角
- 憑證外洩無論多小一律 blocker;可疑存取給 major 並要求說明用途
