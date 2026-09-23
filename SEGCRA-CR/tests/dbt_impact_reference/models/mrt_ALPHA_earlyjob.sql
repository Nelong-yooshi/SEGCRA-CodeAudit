-- 對照用:earlyjob model(被主 model ref)。
SELECT account_id
FROM {{ ref('stg_TXN') }}
