-- 對照用:source() 在 dbt 裡編譯成什麼格式(範例 model 沒有用到 source)。
SELECT a.account_id
FROM {{ source('raw', 'T_SAMPLE_TXN') }} t
JOIN {{ source('raw', 'T_SAMPLE_ACCT') }} a ON a.account_id = t.account_id
