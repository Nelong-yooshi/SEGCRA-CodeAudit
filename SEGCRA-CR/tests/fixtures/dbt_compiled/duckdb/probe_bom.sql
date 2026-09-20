-- 對照用:do、迴圈控制、macro 預設參數、有預設值的 var、execute
-- execute: True


    
    

    
    

    
    
    

SELECT account_id, amount, op_code
    , 
    CASE 
        WHEN balance_after = 0 OR balance_after IS NULL THEN 0
        ELSE CAST(amount AS FLOAT) / CAST(balance_after AS FLOAT)
    END
 AS ratio
    , 'fallback' AS v
FROM "SAMPLE_DW"."dbo"."T_SAMPLE_TXN"