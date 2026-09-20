-- 對照用:呼叫一個「依名稱動態取用 macro」的 macro,靜態分析無法確定實際呼叫對象。
SELECT account_id, {{ call_by_name('flag_small', 'ZETA') }} AS is_small
FROM {{ ref('stg_TXN') }}
