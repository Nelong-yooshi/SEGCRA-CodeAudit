/*
Sample / synthetic example — 合成範例,非任何實際業務規則。
Created: 2026-03-18
Description: 零售交易監控 — 特定條件異常帳戶清單產出 (含小額/行銷撥入排除與 A/B/C 三種情境篩選)
Change Log:
- 2026-05-22 [REFACT] Refactor with the clear-logical select and macros, remove cancellation logic
- 2026-05-08 [REFACT] Refactor with the clear-logical select
- 2026-03-18 [REFACT] Refactor from python script, Optimized with Conditional Aggregation
*/

-- dbt config
{{ config(
    materialized='incremental',
    alias='mrt_RETAIL_M1',
    tags=["retail M1", "daily_job"]
) }}

-- ======================================================================
-- 日期變數設定 (使用 dbt var 以支援排程器動態傳入)
-- ======================================================================
{% set target_date = var("target_date", "2026-02-01") %}


-- 1. T_TXN_BASE: 把所有表需要用到的欄位先 select 進來
WITH T_TXN_BASE AS (
    SELECT
        table_date
        , posted_date
        , txn_time
        , account_id
        , op_code
        , direction_flag
        , CAST(amount AS BIGINT)        AS amount
        , CAST(balance_after AS BIGINT) AS balance_after
    FROM {{ ref('txn_log_net') }}
    WHERE NULLIF(LTRIM(RTRIM(account_id)), '') IS NOT NULL
    AND TABLE_DATE = '{{ target_date }}'
)


-- 2. T_TXN_FULL: 算出所有特徵工程
, T_TXN_AGG1 AS (
    SELECT
        table_date
        , account_id
        , op_code
        , direction_flag
        , amount
        , balance_after
        , posted_date
        , txn_time
        -- 利用 ROW_NUMBER 標記出當天最後一筆交易，方便第 4 步直接抓取餘額
        , ROW_NUMBER()
            OVER(
                PARTITION BY account_id, table_date
                ORDER BY posted_date DESC, txn_time DESC
            )
          AS RN_LAST
    FROM T_TXN_BASE
)

, T_TXN_AGG2 AS (
    SELECT
        table_date
        , account_id
        -- [eod_balance] 當日結餘：利用剛剛的 RN_LAST = 1 來抓取最後一筆餘額
        , MAX(CASE WHEN RN_LAST = 1 THEN balance_after ELSE 0 END) AS eod_balance

        -- [inbound_cnt] & [inbound_amt] 指定入帳次數/總額(排除小額)
        , SUM(CASE WHEN {{ is_inward_large('RETAIL_M1') }} THEN 1 ELSE 0 END) AS inbound_cnt
        , ISNULL(
            SUM(CASE WHEN {{ is_inward_large('RETAIL_M1') }} THEN amount ELSE 0 END)
            , 0)
          AS inbound_amt

        -- [kiosk_outbound_cnt] & [kiosk_outbound_amt] 自助設備出帳次數/總額
        , SUM(CASE WHEN {{ is_kiosk_outward_all('RETAIL_M1') }} THEN 1 ELSE 0 END) AS kiosk_outbound_cnt
        , ISNULL(
            SUM(CASE WHEN {{ is_kiosk_outward_all('RETAIL_M1') }} THEN amount ELSE 0 END)
            , 0)
          AS kiosk_outbound_amt

        -- [outbound_amt] 指定出帳總額
        , ISNULL(
            SUM(CASE WHEN {{ is_outward_all('RETAIL_M1') }} THEN amount ELSE 0 END)
            , 0)
          AS outbound_amt
    FROM T_TXN_AGG1
    GROUP BY table_date, account_id
)

, T_TXN_FULL AS (
    SELECT
        *
        , CASE
            WHEN inbound_amt = 0 THEN 0
            ELSE ROUND(CAST(outbound_amt AS FLOAT) / CAST(inbound_amt AS FLOAT), 2)
          END AS io_ratio
    FROM T_TXN_AGG2
)

-- 3. T_TXN_FINAL: 篩選組合清楚的 OR，並計算總額比
, T_TXN_FINAL AS (
    SELECT
        table_date
        , account_id
        , eod_balance
        , inbound_cnt
        , inbound_amt
        , kiosk_outbound_cnt
        , kiosk_outbound_amt
        , outbound_amt
        , io_ratio
        , '' AS remark
    FROM T_TXN_FULL
    WHERE
        -- Condition A
        (
            kiosk_outbound_cnt BETWEEN 2 AND 3
            AND kiosk_outbound_amt BETWEEN 50000 AND 100000
            AND outbound_amt BETWEEN 90000 AND 110000
            AND inbound_amt BETWEEN 80000 AND 100000
            AND inbound_cnt = 2
            AND io_ratio BETWEEN 0.60 AND 1.20
            AND eod_balance <= 1000
        )
        OR
        -- Condition B
        (
            kiosk_outbound_cnt BETWEEN 3 AND 4
            AND kiosk_outbound_amt BETWEEN 50000 AND 100000
            AND outbound_amt BETWEEN 90000 AND 110000
            AND inbound_amt BETWEEN 80000 AND 120000
            AND inbound_cnt BETWEEN 3 AND 8
            AND io_ratio BETWEEN 0.60 AND 1.20
            AND eod_balance <= 1000
        )
        OR
        -- Condition C
        (
            kiosk_outbound_cnt BETWEEN 4 AND 10
            AND kiosk_outbound_amt BETWEEN 90000 AND 100000
            AND inbound_amt BETWEEN 80000 AND 100000
            AND inbound_cnt BETWEEN 2 AND 3
            AND io_ratio BETWEEN 1.00 AND 1.50
            AND eod_balance <= 1000
        )
)

-- ==================== 4. 最終輸出 ====================
SELECT
    f.table_date
    , f.account_id
    , f.eod_balance
    , f.inbound_cnt
    , f.inbound_amt
    , f.kiosk_outbound_cnt
    , f.kiosk_outbound_amt
    , f.outbound_amt
    , f.io_ratio
    , f.remark
FROM T_TXN_FINAL f
WHERE NOT EXISTS (
    SELECT 1
    FROM {{ ref('retail_m1_earlyjob') }} e
    WHERE e.account_id = f.account_id
    AND e.TABLE_DATE = DATEADD(DAY, -1, '{{ var("target_date") }}')
)
