-- 探測:adapter.dispatch(真實專案常見的跨轉接器寫法)
SELECT
    CONVERT(VARCHAR(30), amount) AS amount_text
    , CONVERT(VARCHAR(30), balance_after) AS balance_text
FROM "SAMPLE_DW"."dbo"."txn_log_net"