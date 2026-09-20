{{ config(post_hook="{{ audit_hook() }}") }}
-- 對照用:以專案命名空間呼叫 macro;post_hook 字串裡也藏了一個 macro。
SELECT account_id, {{ segcra_impact.flag_small('GAMMA') }} AS is_small
FROM {{ ref('stg_TXN') }}
