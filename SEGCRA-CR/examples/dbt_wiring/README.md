# dbt 接線的範例(合成資料)

讓 dbt 接線(docs/09「接進審查管線」)不必接 GitLab 就能實際跑一次。全部是合成資料,不對應任何實際專案。

```
fixtures/
├── mr_801.json                    只改 model 的 MR(diff 只有一行純 SQL)
├── mr_802.json                    只改 macro 的 MR
└── dbt_archives/
    ├── a0a0…a0/                   base commit 的 dbt 專案(兩個 MR 共用)
    ├── b1b1…b1/                   mr_801 的 head commit
    └── c2c2…c2/                   mr_802 的 head commit
```

MR 檔只有 diff、沒有 `full_content`,跟真實 GitLab 模式拿到的一樣;完整的檔案在 `dbt_archives/<commit 編號>/`。
mock 模式下,審查管線會把那個目錄打包成 GitLab 打包下載的形狀,走與真實模式**相同**的解壓與檢查
(`orchestrator/dbt_context.py`)。目錄名必須是完整的 commit 編號(40 位小寫十六進位),與 MR 檔的
`sha` / `diff_refs` 對應。

怎麼跑、預期看到什麼:見上層 README 的「開啟 dbt 接線」。

這些檔案刻意不放在預設的 `fixtures/`:那裡的 `mr_*.json` 編號會影響 mock 模式自動產生的 MR 編號。
