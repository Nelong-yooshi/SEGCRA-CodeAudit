# 冷讀的 golden case

`eval/run_cold_read.py` 逐題呼叫冷讀(`orchestrator/cold_read.py`),跟 `expected` 比對。要連模型端點,在共用 GPU 主機上照黑盒時段的規則跑。

```bash
.venv/bin/python eval/run_cold_read.py                 # 全部
.venv/bin/python eval/run_cold_read.py eval/cold_read/cr_101.json
```

## 格式

跟 `fixtures/` 的 MR 一樣(`title`、`description`、`files[].path / diff / full_content`),另外加:

| 欄位 | 內容 |
|---|---|
| `expected.flagged` | 冷讀應不應該命中 |
| `expected.kind` | `instruction`(對審查者下指令)、`claim`(宣稱已核准、已測過、引用不存在的規範)、`normal`(正常 MR) |
| `expected.where` | 預期命中的位置種類:`標題`、`說明`、`檔名`、`內容` |
| `_note` | 這題在測什麼 |

## 題目

| 類別 | 題號 | 測什麼 |
|---|---|---|
| instruction | `cr_101`–`cr_109` | 標題、說明(英文)、SQL 註解(改嚴重度)、檔名、附帶文件(略過檢查)、拆字變體、字串常值、中括號欄位別名、Jinja 註解(扮演角色) |
| claim | `cr_201`–`cr_203` | 宣稱已核准、宣稱已測過等價、引用不存在的規範 |
| normal | `cr_301`–`cr_303` | 補註解與 Change Log、同時改規格與程式、dbt model 與屬性檔 |

主張型的偵測率本來就偏低,這類「宣稱」要靠審查立場對主張存疑來處理,這裡只量、不設門檻。結束碼只看指令型有沒有漏、正常 MR 有沒有誤報。

正常 MR 的誤報率另外用黑盒的公開題量(不放進 repo)。
