-- SHARED_B_EVAL:單日累計入帳達門檻(含)以上通報(合成資料,僅供評測)
SELECT
    t.account_id,
    SUM(t.amount) AS total_amount
FROM txn_log t
WHERE t.op_code IN ('OP01', 'OP02', 'OP03')
  AND t.tx_time >= @start_date AND t.tx_time < @end_date
GROUP BY t.account_id
HAVING SUM(t.amount) >= {{ alert_threshold('SHARED_B_EVAL') }}
