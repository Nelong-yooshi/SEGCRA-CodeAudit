-- 對照用:子目錄中的 model;ref 另一個 model(下游);把 macro 當成值傳遞後才呼叫。
{% set checker = flag_large %}
SELECT account_id
FROM {{ ref('mrt_ALPHA') }}
WHERE {{ checker('DELTA') }}
