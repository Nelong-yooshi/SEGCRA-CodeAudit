-- 探測:同檔含 dbt 專屬區塊的 macro 檔仍可用,且 config.get 讀得回 config() 設的值

SELECT
    NEIGHBOUR(amount) AS neighbour
    , 'PROBE_VALUE' AS probe_value
FROM "SAMPLE_DW"."dbo"."txn_log_net"