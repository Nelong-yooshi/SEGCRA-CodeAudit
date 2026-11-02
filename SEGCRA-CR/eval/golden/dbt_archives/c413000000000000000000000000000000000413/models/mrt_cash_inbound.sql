-- R-140: 當日現金入帳累計達 500000(含)通報
SELECT
    t.account_id,
    SUM(t.amount) AS total_amount
FROM {{ source('raw', 'txn_log') }} t
WHERE t.op_code IN ('OP01','OP02','OP03')
  AND t.cash_flag = 'Y'
  AND COALESCE(t.rev_flag, 'N') <> 'Y'
  AND t.amount > 1000
  AND COALESCE(t.channel_name, '') <> 'PROMO'
  AND t.tx_time >= @start_date AND t.tx_time < @end_date
GROUP BY t.account_id
HAVING SUM(t.amount) >= 500000;
