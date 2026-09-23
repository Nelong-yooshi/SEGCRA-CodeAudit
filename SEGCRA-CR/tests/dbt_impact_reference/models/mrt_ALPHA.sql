-- 對照用:model 直接呼叫 macro,macro 再查註冊表(比照範例 model 與 config.sql 的結構);
-- 並 ref 自己的 earlyjob model。
SELECT t.account_id
FROM {{ ref('stg_TXN') }} t
WHERE {{ flag_large('ALPHA') }}
AND NOT EXISTS (
    SELECT 1 FROM {{ ref('mrt_ALPHA_earlyjob') }} e WHERE e.account_id = t.account_id
)
