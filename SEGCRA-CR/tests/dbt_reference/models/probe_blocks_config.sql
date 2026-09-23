-- 探測:同檔含 dbt 專屬區塊的 macro 檔仍可用,且 config.get 讀得回 config() 設的值
{{ config(materialized='view', segcra_probe='PROBE_VALUE') }}
SELECT
    {{ probe_block_neighbour('amount') }} AS neighbour
    , '{{ config.get('segcra_probe') }}' AS probe_value
FROM {{ ref('txn_log_net') }}
