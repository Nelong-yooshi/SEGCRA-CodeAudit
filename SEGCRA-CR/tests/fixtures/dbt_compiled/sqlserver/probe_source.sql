-- 對照用:source() 在 dbt 裡編譯成什麼格式(範例 model 沒有用到 source)。
SELECT a.account_id
FROM "SAMPLE_DW"."dbo"."T_SAMPLE_TXN" t
JOIN "SAMPLE_DW"."dbo"."T_SAMPLE_ACCT" a ON a.account_id = t.account_id