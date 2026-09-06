"""ToolHub — 行程內工具註冊表:把 toolbox/ 的純 Python 函式轉成 OpenAI tool-calling
schema 並分派呼叫。不 spawn 子行程、無協定層。

工具名稱格式:`<group>__<tool>`(OpenAI tool name 不允許點號),與舊 MCP 版
完全相同(gitlab__get_mr_diff、sqltools__run_rules、memory__retrieve_knowledge…),
pipeline / prompt 不需任何改動。

schema 生成:從函式簽名 + docstring 自動產生——docstring 是給 LLM 的工具描述,
參數型別對映 string/integer/boolean/number/array,有預設值者為非必填。

要把工具層重新包成 MCP server(掛進 VS Code Copilot / Claude Code 等 host)時,
領域邏輯都在 toolbox/ 純模組,包一層薄轉接即可——見 README 附錄。
"""
import inspect
import json
import typing

from toolbox import gitlab, memory, sqltools

# 工具組:group 名 → 該組要暴露的函式(全名 = f"{group}__{fn.__name__}")
TOOL_GROUPS = {
    "gitlab": [gitlab.get_mr_diff, gitlab.get_file, gitlab.post_inline_comment,
               gitlab.post_summary, gitlab.set_commit_status, gitlab.create_rule_mr,
               gitlab.set_label],
    "sqltools": [sqltools.parse_ast, sqltools.lint, sqltools.run_rules],
    "memory": [memory.retrieve_knowledge, memory.get_conventions, memory.check_style,
               memory.learn_style_from_examples, memory.lookup_similar_reviews,
               memory.record_feedback],
}

_TYPE_MAP = {str: "string", int: "integer", bool: "boolean", float: "number"}


def _annotation_schema(ann) -> dict:
    """Python 型別註記 → JSON Schema 片段(未知型別退回 string)。"""
    if ann in _TYPE_MAP:
        return {"type": _TYPE_MAP[ann]}
    origin = typing.get_origin(ann)
    if origin in (list, set, tuple):
        args = typing.get_args(ann)
        return {"type": "array",
                "items": _annotation_schema(args[0]) if args else {"type": "string"}}
    if ann is dict or origin is dict:
        return {"type": "object"}
    return {"type": "string"}


def function_schema(full_name: str, fn) -> dict:
    """由函式簽名 + docstring 生成 OpenAI tool schema(有預設值 = 非必填)。"""
    props, required = {}, []
    for pname, p in inspect.signature(fn).parameters.items():
        props[pname] = _annotation_schema(p.annotation)
        if p.default is inspect.Parameter.empty:
            required.append(pname)
    return {"type": "function",
            "function": {"name": full_name,
                         "description": inspect.getdoc(fn) or "",
                         "parameters": {"type": "object", "properties": props,
                                        "required": required}}}


class ToolHub:
    def __init__(self, tool_result_max_chars: int = 6000, servers: list[str] | None = None):
        """servers = 要註冊哪幾組工具(None = 全部;[] = 都不註冊,只留本地工具)。"""
        self.max_chars = tool_result_max_chars
        self.group_names = list(TOOL_GROUPS) if servers is None else list(servers)
        self.tools: dict[str, dict] = {}         # full_name -> openai schema
        self.local_tools: dict[str, tuple] = {}  # full_name -> (fn, schema)
        self._functions: dict[str, object] = {}  # full_name -> toolbox 函式
        for group in self.group_names:
            for fn in TOOL_GROUPS[group]:
                full = f"{group}__{fn.__name__}"
                self._functions[full] = fn
                self.tools[full] = function_schema(full, fn)

    # 介面相容:舊版要起子行程所以是 async context manager;行程內版無事可做,
    # 但保留 async with 用法讓呼叫端(pipeline 等)一行不改。
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    def register_local(self, name: str, fn, description: str, parameters: dict):
        """註冊 orchestrator 本地工具(如 load_skill),與 toolbox 工具同列。"""
        self.local_tools[name] = (fn, None)
        self.tools[name] = {"type": "function",
                            "function": {"name": name, "description": description,
                                         "parameters": parameters}}

    @property
    def openai_tools(self) -> list[dict]:
        return list(self.tools.values())

    async def call(self, full_name: str, args: dict, truncate: bool = True) -> str:
        if full_name in self.local_tools:
            result = self.local_tools[full_name][0](**args)
        elif full_name in self._functions:
            try:
                result = self._functions[full_name](**args)
            except Exception as e:   # 與舊版(MCP server 端吞錯)一致:回錯誤字串不炸管線
                result = f"Error executing tool {full_name}: {e}"
        elif "__" in full_name:
            group = full_name.split("__", 1)[0]
            if group not in self.group_names:
                return f"(錯誤:未知的 server {group})"
            return f"(錯誤:未知的工具 {full_name})"
        else:
            return f"(錯誤:未知的工具 {full_name})"
        result = str(result)
        # 截斷只用於「餵給 LLM 的工具結果」;orchestrator 自用的結構化抓取(call_json)
        # 不可截斷,否則大型 MR 的 JSON 會被腰斬成解析失敗
        if truncate and len(result) > self.max_chars:
            result = result[: self.max_chars] + f"\n…(截斷,原長 {len(result)} 字元)"
        return result

    async def call_json(self, full_name: str, args: dict):
        """直接呼叫並解析 JSON(orchestrator 自用,如確定性預掃);不截斷。"""
        try:
            return json.loads(await self.call(full_name, args, truncate=False))
        except json.JSONDecodeError:
            return None
