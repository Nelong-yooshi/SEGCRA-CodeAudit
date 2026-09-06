"""toolbox — 行程內工具模組(純 Python 函式,無協定層)。

設計紀律:領域邏輯一律放在這裡的純模組;協定/轉接層(MCP、HTTP…)永遠是
薄的可選件,需要時另外包一層即可(見 README 附錄「如何把工具層包回 MCP」)。

模組:
  gitlab           MR 讀取與審查結果回寫(mock / real 雙模式)
  sqltools         確定性 SQL 分析(AST / lint / rule-base)
  memory           分層知識檢索 + 審查歷史 + 風格學習
  knowledge_store  分層知識庫(binding/guideline/observed)底層
  style_learn      風格觀察學習(如前置逗號)底層

各工具函式的 docstring 是給 LLM 的工具描述(tool_hub 會轉成 OpenAI schema),
修改時保持「一句話講清楚做什麼 + 參數語意」。
"""
