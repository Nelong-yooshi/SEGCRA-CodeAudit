{{ config(materialized='incremental') }}
-- 對照用:incremental model 的 is_incremental() 區塊
SELECT account_id, posted_date
FROM {{ ref('txn_log_net') }}
{% if is_incremental() %}
WHERE posted_date > (SELECT MAX(posted_date) FROM {{ this }})
{% endif %}
