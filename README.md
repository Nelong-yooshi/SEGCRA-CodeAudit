# Code Audit Module

本專案由 Segora Tech 團隊開發與維護。

自動化的 SQL merge request 程式碼審查模組。以地端 LLM 為核心,前後包覆確定性檢查——規則預掃、提示注入掃描、分層知識庫、依規格生成測資的沙盒執行驗證——對 merge request 產出可稽核的審查意見,並依確定性政策給出三態決策(自動放行 / 需人工 / 擋下),回寫至版本控制平台。

設計前提是**不信任中間的模型**:模型負責語意判斷,前後的確定性機制負責保證規則命中不消失、注入必擋、分數可重現、決策有依據。

## 快速開始

```bash
cd SEGCRA-CR
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 不需模型,驗證管線是否正常
.venv/bin/python demo.py --mr 001 --dry-run
```

完整說明(環境需求、規格撰寫、接上 GitLab、webhook 自動觸發、merge 閘門設定)見 [`SEGCRA-CR/README.md`](SEGCRA-CR/README.md)。

## 專案結構

| 路徑 | 內容 |
|---|---|
| [`SEGCRA-CR/`](SEGCRA-CR/) | 主體模組:審查管線、工具層、規格與範例 |
| [`SEGCRA-CR/docs/`](SEGCRA-CR/docs/) | 各道檢測與關卡的設計與實作細節 |
| [`SEGCRA-CR/examples/`](SEGCRA-CR/examples/) | 規格撰寫與程式對照的示範範例 |

## 協作規範

參與開發前請先閱讀 [`CONTRIBUTING.md`](CONTRIBUTING.md) — 分支策略、Pull Request 流程、Code Review 與 Issue 命名規則。

## 授權

本專案採專有授權 (Proprietary License),著作權歸 Segora Tech 所有。詳見 [`LICENSE`](LICENSE)。

授權洽詢:[info@segora.tech](mailto:info@segora.tech)
