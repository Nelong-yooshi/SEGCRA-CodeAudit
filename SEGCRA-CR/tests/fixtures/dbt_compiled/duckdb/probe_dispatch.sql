-- 探測:adapter.dispatch(真實專案常見的跨轉接器寫法)
SELECT
    CAST(amount AS VARCHAR) AS amount_text
    , CAST(balance_after AS VARCHAR) AS balance_text
FROM "SAMPLE_DW"."dbo"."txn_log_net"