# RETAIL_M1_EVAL 零售交易監控:特定條件異常帳戶清單(評測專用)

> ⚠️ **評測專用,不是核定規格。** 本檔是照範例程式改寫、讓 golden set 正向對照
> (`mr_701`)乾淨的版本,刻意不叫 `RETAIL_M1`:`specs/<名稱>.md` 會依 model 檔名
> 直接拿去審同名的 model,用正式名稱放著,之後審 `mrt_RETAIL_M1.sql` 都會對到這份
> 改寫版,範本規格有要求、程式卻沒做的部分就不會再被檢查。
> `mr_701` 量的是「**規格與程式一致時不誤報**」,不代表範例程式符合範例規格。
>
> 本檔是 golden set 用的規格,骨架取自 `examples/sample/RETAIL_M1_spec.md`
> (「表格式規格 → md」的示範格式),並補上執行驗證所需的**資料表定義**段。
>
> ⚠️ 合成資料:表名、欄位、代碼、金額門檻皆為虛構,僅供評測使用。
>
> **與 `examples/sample/RETAIL_M1_spec.md` 的差異(刻意的,不是漏抄)**:範本的
> 「排除資料」段列了沖銷交易與 `channel_name = 'PROMO'` 兩項,本檔沒有——範本
> 對應的程式(`examples/sample/RETAIL_M1_code/`)其資料表根本沒有這兩個欄位
> (沖銷邏輯在 2026-05-22 那次重構已移除,見該檔 Change Log),寫進規格會變成
> 「規格要求了 schema 做不到的事」,測資生成只會生出必然失敗的案例。golden set
> 的正向對照必須規格與實作**由設計上就一致**,才量得到「乾淨程式不該誤報」。

## 基本資料

| 欄位 | 值 |
|---|---|
| 序號 / 程式名稱 | SAMPLE01 / RETAIL_M1 |
| 資料取用 | txn_log_net |
| 是否執行加分模組 | 否 |
| 是否執行 EarlyJob | 是 |
| 程式負責人 | 資料工程團隊 |

## 說明

對指定交易日的交易,依 `op_code` 與 `direction_flag` 分類計算出入帳指標,
符合篩選條件 1/2/3 **任一**者列入異常清單;前一日已由 EarlyJob 出過的帳戶排除。

## 資料表定義(釘死 schema;執行驗證以此建表)

```sql
CREATE TABLE txn_log_net(
  table_date DATE, posted_date DATE, txn_time DATETIME2,
  account_id NVARCHAR(20), op_code NVARCHAR(10), direction_flag NVARCHAR(1),
  amount DECIMAL(14,2), balance_after DECIMAL(14,2));
CREATE TABLE retail_m1_earlyjob(account_id NVARCHAR(20), table_date DATE);
```

## 排除資料

- **空白帳號**:`account_id` 去除前後空白後為空字串或 NULL 者不列計。
- **小額入帳**:單筆入帳金額**小於等於 1,000** 者不計入「指定入帳」指標
  (`amount > 1000` 才列計)。
- **EarlyJob 前一日已出帳者**:帳戶出現在 `retail_m1_earlyjob` 且其
  `table_date` 等於「交易日前一日」者,排除於最終輸出之外。

## op_code 與 direction_flag 分類

`direction_flag`:`'0'` = 入帳,`'1'` = 出帳。

- **指定入帳**:`direction_flag = '0'` 且 `op_code IN ('OP01','OP02','OP03')`
  且 `amount > 1000`。
- **自助設備出帳**:`direction_flag = '1'` 且 `op_code IN ('OP11','OP12')`。
- **指定出帳**:`direction_flag = '1'` 且 `op_code IN ('OP21','OP22','OP23')`。

## 衍生指標(每帳戶、每交易日彙總)

| 指標 | 定義 |
|---|---|
| `eod_balance` 當日結餘 | 當日最後一筆交易的 `balance_after`;「最後一筆」以 `posted_date DESC, txn_time DESC` 排序取第一筆 |
| `inbound_cnt` 指定入帳次數 | 符合「指定入帳」的筆數 |
| `inbound_amt` 指定入帳總額 | 符合「指定入帳」的 `amount` 合計;無符合者為 0 |
| `kiosk_outbound_cnt` 自助設備出帳次數 | 符合「自助設備出帳」的筆數 |
| `kiosk_outbound_amt` 自助設備出帳總額 | 符合「自助設備出帳」的 `amount` 合計;無符合者為 0 |
| `outbound_amt` 指定出帳總額 | 符合「指定出帳」的 `amount` 合計;無符合者為 0 |
| `io_ratio` 出入帳總額比 | `inbound_amt = 0` 時為 **0**;否則 `ROUND(outbound_amt / inbound_amt, 2)`(四捨五入至小數 2 位) |

## 篩選條件(「介於」= **含兩端**;條件 1/2/3 任一成立即列入)

1. **條件 1(七項全部同時成立)**:
   - 1-1 `kiosk_outbound_cnt` 介於 2–3
   - 1-2 `kiosk_outbound_amt` 介於 50,000–100,000
   - 1-3 `outbound_amt` 介於 90,000–110,000
   - 1-4 `inbound_amt` 介於 80,000–100,000
   - 1-5 `inbound_cnt` **等於 2**
   - 1-6 `io_ratio` 介於 0.60–1.20
   - 1-7 `eod_balance` **≤ 1,000**
2. **條件 2(七項全部同時成立)**:
   - 2-1 `kiosk_outbound_cnt` 介於 3–4
   - 2-2 `kiosk_outbound_amt` 介於 50,000–100,000
   - 2-3 `outbound_amt` 介於 90,000–110,000
   - 2-4 `inbound_amt` 介於 80,000–120,000
   - 2-5 `inbound_cnt` 介於 3–8
   - 2-6 `io_ratio` 介於 0.60–1.20
   - 2-7 `eod_balance` ≤ 1,000
3. **條件 3(六項全部同時成立)**:
   - 3-1 `kiosk_outbound_cnt` 介於 4–10
   - 3-2 `kiosk_outbound_amt` 介於 90,000–100,000
   - 3-3 `inbound_amt` 介於 80,000–100,000
   - 3-4 `inbound_cnt` 介於 2–3
   - 3-5 `io_ratio` 介於 1.00–1.50
   - 3-6 `eod_balance` ≤ 1,000

## 時間窗與參數

- 本規則以**單一交易日**為範圍:`table_date = @start_date`(不是區間)。
- EarlyJob 的比對日為交易日**前一日**:`DATEADD(DAY, -1, @start_date)`。

## 通報粒度與輸出欄位

- 每帳戶每交易日至多輸出 1 筆。
- 輸出:`table_date`、`account_id`、`eod_balance`、`inbound_cnt`、`inbound_amt`、
  `kiosk_outbound_cnt`、`kiosk_outbound_amt`、`outbound_amt`、`io_ratio`、`remark`。

## 待補(核定規格未載明,不從程式碼腦補)

- **「當日結餘」的取法**:規格只說「當日結餘」,未定義是當日最後一筆交易後的餘額
  還是日終批次餘額。(現行實作取當日最後一筆交易的 `balance_after`。)
- **出入帳總額比的除零與進位**:規格未載入帳總額為 0 時該視為 0 還是排除,
  也未載小數取幾位。(現行實作:分母 0 時取 0、四捨五入至小數 2 位。)
- **EarlyJob 排除的比對日**:規格只說排除 EarlyJob 已出過的帳戶,未載是比對
  前一日還是同日。(現行實作比對前一日。)
- **金額欄位的幣別/單位**:規格未載是否需限制單一幣別;資料表亦無幣別欄位。

## 補充

- 門檻的「介於」一律解為**含兩端**(`BETWEEN`);若某條實為「超過(不含)」,
  須在規格明寫,否則測資的邊界案例會判為不符。
- 1-5 的 `inbound_cnt` 是**等於 2**(不是介於),與 2-5、3-4 的區間寫法不同,
  這是核定內容,不是筆誤。
- 三個條件之間是 **OR**;條件內各項之間是 **AND**。
