-- 對照用:最上游的 model。改它會影響所有 ref 它的下游 model。
SELECT account_id, amount, op_code
FROM {{ source('raw', 'T_SAMPLE_TXN') }}
