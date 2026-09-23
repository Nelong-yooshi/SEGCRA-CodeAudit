-- 探測:adapter.dispatch(真實專案常見的跨轉接器寫法)
SELECT
    {{ adapter.dispatch('probe_fmt')('amount') }} AS amount_text
    , {{ adapter.dispatch('probe_fmt', 'segcra_reference')('balance_after') }} AS balance_text
FROM {{ ref('txn_log_net') }}
