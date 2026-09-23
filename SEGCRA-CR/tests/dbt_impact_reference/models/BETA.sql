-- 對照用:透過 adapter.dispatch 間接呼叫(實際執行的是 default__fmt_amount 等)。
SELECT {{ adapter.dispatch('fmt_amount')('amount') }} AS amount_text
FROM {{ ref('stg_TXN') }}
