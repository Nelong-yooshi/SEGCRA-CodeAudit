# SHARED_A_EVAL 單日累計入帳監控(評測用)

> 格式依 `examples/sample/RETAIL_M1_spec.md` 的骨架整理,並補上執行驗證所需的
> **資料表定義**段。
>
> ⚠️ 合成資料:表名、欄位、代碼、金額門檻皆為虛構,僅供評測使用(golden case `mr_862`)。
> 與另一份評測規格共用同一張門檻表(`macros/alert_threshold.sql`),兩份的門檻不同。

## 基本資料

| 欄位 | 值 |
|---|---|
| 序號 / 程式名稱 | EVAL862 / SHARED_A_EVAL |
| 資料取用 | txn_log |
| 程式負責人 | 資料工程團隊 |

## 說明

計算單一帳戶當日**入帳**累計金額;達 50,000 元(含)以上者列入異常清單。

## 資料表定義(釘死 schema;執行驗證以此建表)

```sql
CREATE TABLE txn_log(
  account_id INT, tx_time DATETIME2, op_code NVARCHAR(10), amount DECIMAL(14,2));
```

## 篩選條件(逐條可對測資判 true/false)

1. **交易範圍**:`op_code IN ('OP01','OP02','OP03')`
2. **聚合方式**:單日入帳累計金額(每帳戶,`SUM(amount)`)
3. **閾值**:50,000 元;「達(含)以上」= `>= 50000`(**恰 50,000 通報**)
4. **時間窗**:交易日整日,採半開區間(`tx_time >= @start_date AND tx_time < @end_date`)
5. **通報粒度**:每帳戶每交易日至多輸出 1 筆
6. **輸出欄位**:`account_id`、當日入帳累計金額(`total_amount`)

## 補充

- 門檻寫在共用的門檻表(`alert_threshold('SHARED_A_EVAL')`),程式不直接寫數字;核對時以展開後的值為準。
