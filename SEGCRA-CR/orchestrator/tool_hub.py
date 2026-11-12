"""ToolHub — 行程內工具註冊表:把 toolbox/ 的純 Python 函式轉成 OpenAI tool-calling
schema 並分派呼叫。不 spawn 子行程、無協定層。

工具名稱格式:`<group>__<tool>`(OpenAI tool name 不允許點號),與舊 MCP 版
完全相同(gitlab__get_mr_diff、sqltools__run_rules、memory__retrieve_knowledge…),
pipeline / prompt 不需任何改動。

schema 生成:從函式簽名 + docstring 自動產生——docstring 是給 LLM 的工具描述,
參數型別對映 string/integer/boolean/number/array,有預設值者為非必填。

要把工具層重新包成 MCP server(掛進 VS Code Copilot / Claude Code 等 host)時,
領域邏輯都在 toolbox/ 純模組,包一層薄轉接即可——見 README 附錄。

**審查模型只拿得到唯讀工具**(#17,MODEL_TOOLS):MR 的標題、描述、SQL 註解都是作者寫的,
可以夾帶給模型的指令。寫判例、學風格、開 MR、留言、設 label、設 commit status 只由管線
自己用 call() 執行;模型那一端看到的工具清單(openai_tools)與執行入口
(call_from_model)都只認白名單。

**唯讀的工具在背景執行緒跑**(_invoke):工具都是同步函式,直接在事件迴圈裡呼叫時,外層的
`asyncio.wait_for`(預掃、主審查、整份審查的上限)要等它跑完才插得進來。寫入類留在事件迴圈裡,
見 _invoke 的說明。
"""
import functools
import inspect
import json
import typing

from toolbox import gitlab, memory, sqltools

from .isolation import run_in_thread
from .security import scan_mr

# 工具組:group 名 → 該組要暴露的函式(全名 = f"{group}__{fn.__name__}")
TOOL_GROUPS = {
    "gitlab": [gitlab.get_mr_diff, gitlab.get_mr_refs, gitlab.get_file, gitlab.post_inline_comment,
               gitlab.post_summary, gitlab.set_commit_status, gitlab.create_rule_mr,
               gitlab.set_label],
    "sqltools": [sqltools.parse_ast, sqltools.lint, sqltools.run_rules],
    "memory": [memory.retrieve_knowledge, memory.get_conventions, memory.check_style,
               memory.learn_style_from_examples, memory.lookup_similar_reviews,
               memory.record_feedback],
}

# 審查模型可以呼叫的工具(#17):全部唯讀。用名稱逐一列出,而不是「排除寫入類」——
# 之後新增的工具預設**不**開放給模型,要開放必須在這裡明寫並確認它不會寫入。
# 寫入類(post_inline_comment、post_summary、set_commit_status、create_rule_mr、set_label、
# learn_style_from_examples、record_feedback)只由管線自己呼叫。
MODEL_TOOLS = frozenset({
    "gitlab__get_mr_diff", "gitlab__get_file",
    "sqltools__parse_ast", "sqltools__lint", "sqltools__run_rules",
    "memory__retrieve_knowledge", "memory__get_conventions", "memory__check_style",
    "memory__lookup_similar_reviews",
    "load_skill",
})
MODEL_REFUSAL = "(拒絕:此工具不開放給審查模型)"

# 模型讀 MR 與檔案的範圍(review_mr 取完 MR 後用 set_review_scope 設定):只能讀這次審查的 MR,
# 檔案只能讀這次 MR 的版本或目標分支。MR 的標題、描述、註解可以引導模型去讀別的 MR、別的分支或
# 某個舊 commit,讀回來的東西沒有經過這次審查的任何一道檢查。
READ_TOOLS = frozenset({"gitlab__get_mr_diff", "gitlab__get_file"})
READ_REFUSAL = ("(拒絕:只能讀這次審查的 MR;檔案只能讀這次 MR 的版本或目標分支。"
                "請依手上的內容判斷。)")
# 讀回的內容跟 MR 本身一樣先過注入掃描,命中或掃描失敗就不交給模型
INJECTION_WITHHELD = "(讀回的內容疑似含有對審查工具的指令,不提供內容。)"
SCAN_FAILED_WITHHELD = "(讀回的內容沒有完成注入掃描,不提供內容。)"
# 被拒絕的讀取(refused_reads 裡的固定代碼;模型給的參數不記,那可能是 MR 作者寫的文字)
REFUSED_OTHER_MR = "other_mr"
REFUSED_OTHER_REF = "other_ref"
REFUSED_SCAN_FAILED = "scan_failed"
# 審查判例(record_feedback 記的人工處置)也是模型讀得到的文字:sql 欄位常是從某個 MR 貼過來的,
# 作者寫的註解一起在裡面。每筆先過同一套注入掃描,命中的那筆不交給模型。判例不是這次 MR 帶進來的,
# 不算成這次 MR 的命中(不 blocked),只記下來(withheld_history),讓維運的人去清判例檔。
HISTORY_TOOL = "memory__lookup_similar_reviews"
HISTORY_WITHHELD_NOTE = "有 {n} 筆判例疑似含有對審查工具的指令,不提供內容。"
HISTORY_WITHHELD = "(查回的判例疑似含有對審查工具的指令,不提供內容。)"
HISTORY_SCAN_FAILED = "(查回的判例沒有完成注入掃描,不提供內容。)"
# 模型給縮寫的 commit sha 時,至少要幾個字元才認(git 預設的縮寫長度)
MIN_SHA_PREFIX = 7
_HEX = frozenset("0123456789abcdef")

def _scan_each(targets: list[dict]) -> list[list[dict]]:
    """每個 MR 形狀的內容各掃一次(查回的判例逐筆掃用)。"""
    return [scan_mr(t) for t in targets]


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
        # 模型要求、但被拒絕的**已註冊**工具名稱(不記參數:參數可能是 MR 作者寫的文字)
        self.refused_calls: list[str] = []
        # 讀取範圍(set_review_scope);None 時不限制範圍,讀回的內容照樣掃描
        self.review_scope: dict | None = None
        # 被拒絕的讀取(REFUSED_* 代碼)與讀回內容的注入掃描命中({category, tool},不帶原文)
        self.refused_reads: list[str] = []
        self.tool_injection_hits: list[dict] = []
        # 沒有交給模型的判例({ts, categories};ts 讀不到是 None,掃描出錯時 categories 是 None)
        self.withheld_history: list[dict] = []
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

    def set_review_scope(self, mr_id, mr: dict) -> None:
        """模型能讀的範圍:這次審查的 MR(mr_id)、這次 MR 的版本(head、base、start 三個 sha
        與來源分支)與目標分支。mr 是 get_mr_diff 的結果。"""
        def text(v):
            return v if isinstance(v, str) and v else None
        head = text(mr.get("head_sha"))
        self.review_scope = {
            "mr_id": str(mr_id),
            "head": head,
            "shas": {s for s in (head, text(mr.get("base_sha")), text(mr.get("start_sha"))) if s},
            "target_branch": text(mr.get("target_branch")),
            "source_branch": text(mr.get("source_branch")),
        }

    def _resolve_ref(self, ref: str):
        """模型給的 ref → 實際要讀的 ref;不在範圍內回 None。

        來源分支換成釘住的 head:分支可能已經有新的 commit,那些還沒經過這次審查。釘不住時回
        空字串,get_file 會明講取不到。目標分支照原樣讀。sha 可以縮寫,但要對得到範圍內的一個。"""
        scope = self.review_scope
        if scope["source_branch"] and ref == scope["source_branch"]:
            return scope["head"] or ""
        if scope["target_branch"] and ref == scope["target_branch"]:
            return ref
        low = ref.lower()
        if len(low) >= MIN_SHA_PREFIX and set(low) <= _HEX:
            hits = {s for s in scope["shas"] if s.lower().startswith(low)}
            if len(hits) == 1:
                return hits.pop()
        return None

    def _scoped_args(self, full_name: str, args: dict):
        """讀取工具的參數 → (實際要用的參數, None),或 (None, 拒絕代碼)。"""
        scope = self.review_scope
        if scope is None:
            return args, None
        mr_arg = args.get("mr_id", "")
        if full_name == "gitlab__get_mr_diff" or mr_arg not in ("", None):
            if str(mr_arg) != scope["mr_id"]:
                return None, REFUSED_OTHER_MR
        ref = args.get("ref", "")
        if full_name == "gitlab__get_file" and ref not in ("", None):
            resolved = self._resolve_ref(str(ref))
            if resolved is None:
                return None, REFUSED_OTHER_REF
            return {**args, "ref": resolved}, None
        return args, None

    @staticmethod
    def _scan_target(full_name: str, args: dict, result: str) -> dict:
        """讀回的內容 → 給 scan_mr 的 MR 形狀。get_mr_diff 讀回的是一個 MR,照 MR 掃;
        其他(檔案內容、錯誤訊息)當成一個檔案的完整內容掃。"""
        if full_name == "gitlab__get_mr_diff":
            try:
                mr = json.loads(result)
            except (TypeError, ValueError):
                mr = None
            if isinstance(mr, dict):
                return mr
        path = args.get("path")
        return {"files": [{"path": path if isinstance(path, str) else "", "full_content": result}]}

    async def _screen(self, full_name: str, args: dict, result: str) -> str:
        """模型讀回的內容先過注入掃描,跟 MR 本身用同一套(scan_mr)。命中就不交給模型、記下類別;
        掃描自己出錯時也不交給模型(沒掃過的內容跟命中一樣不能給)。"""
        try:
            hits = await run_in_thread(
                functools.partial(scan_mr, self._scan_target(full_name, args, result)),
                name="segcra-tool-scan")
        except Exception:      # noqa: BLE001 — 錯誤內容可能帶著讀回的文字,不記
            self.refused_reads.append(REFUSED_SCAN_FAILED)
            return SCAN_FAILED_WITHHELD
        if hits:
            self.tool_injection_hits.extend(
                {"category": h.get("category"), "tool": full_name} for h in hits)
            return INJECTION_WITHHELD
        return result

    @staticmethod
    def _as_text(value) -> str:
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    @classmethod
    def _history_target(cls, rec) -> dict:
        """一筆判例 → 給 scan_mr 的 MR 形狀:sql 當成檔案內容(註解另外當成文字掃),其他欄位的
        名稱與值都當成描述。不是字串的值也轉成文字掃:交給模型的是整筆,哪個欄位都可能帶著文字。"""
        if not isinstance(rec, dict):
            return {"description": cls._as_text(rec)}
        prose = "\n".join(f"{k}\n{cls._as_text(v)}" for k, v in rec.items() if k != "sql")
        return {"description": prose,
                "files": [{"path": "", "full_content": cls._as_text(rec.get("sql", ""))}]}

    @staticmethod
    def _history_ts(rec):
        ts = rec.get("ts") if isinstance(rec, dict) else None
        return ts if isinstance(ts, int) and not isinstance(ts, bool) else None

    async def _screen_history(self, result: str) -> str:
        """查回的判例逐筆過注入掃描,命中的那筆不給、其他照給。輸出照 lookup_similar_reviews 的
        形狀重組(只留 results),沒有命中時跟原本的結果一樣。不是預期的形狀(例如錯誤訊息)時
        整段當成一筆掃。掃描自己出錯時整份不給。"""
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            data = None
        records = data.get("results") if isinstance(data, dict) else None
        shaped = isinstance(records, list)
        targets = [self._history_target(r) for r in records] if shaped else [{"description": result}]
        try:
            hits = await run_in_thread(functools.partial(_scan_each, targets), name="segcra-tool-scan")
        except Exception:      # noqa: BLE001 — 錯誤內容可能帶著判例的文字,不記
            self.withheld_history.append({"ts": None, "categories": None})
            return HISTORY_SCAN_FAILED
        if not shaped:
            if hits[0]:
                self.withheld_history.append(
                    {"ts": None, "categories": sorted({str(h.get("category")) for h in hits[0]})})
                return HISTORY_WITHHELD
            return result
        kept = []
        for rec, rec_hits in zip(records, hits):
            if rec_hits:
                self.withheld_history.append(
                    {"ts": self._history_ts(rec),
                     "categories": sorted({str(h.get("category")) for h in rec_hits})})
            else:
                kept.append(rec)
        out = {"results": kept}
        withheld = len(records) - len(kept)
        if withheld:
            out["withheld"] = HISTORY_WITHHELD_NOTE.format(n=withheld)
        return json.dumps(out, ensure_ascii=False)

    @property
    def openai_tools(self) -> list[dict]:
        """交給審查模型的工具清單:只含 MODEL_TOOLS(唯讀)。管線自己呼叫寫入類用 call()。"""
        return [schema for name, schema in self.tools.items() if name in MODEL_TOOLS]

    async def call_from_model(self, full_name: str, args: dict) -> str:
        """模型要求的工具呼叫一律走這裡:不在 MODEL_TOOLS 就不執行。

        openai_tools 已經不列寫入類工具,這是第二道:模型仍可能照 MR 內容的指示,
        要求它看不到的工具。被拒絕的已註冊工具記進 refused_calls(管線據此補 finding);
        不存在的名稱(模型自己編的)只回拒絕,不記錄。回給模型的是固定文字。"""
        if full_name not in MODEL_TOOLS:
            if full_name in self._functions or full_name in self.local_tools:
                self.refused_calls.append(full_name)
            return MODEL_REFUSAL
        if full_name == HISTORY_TOOL:
            # 整份掃描,不是截斷後的那段(理由同下)
            result = await self.call(full_name, args, truncate=False)
            return self._truncate(await self._screen_history(result))
        if full_name not in READ_TOOLS or not isinstance(args, dict):
            return await self.call(full_name, args)
        scoped, refused = self._scoped_args(full_name, args)
        if refused:
            self.refused_reads.append(refused)
            return READ_REFUSAL
        # 整份掃描,不是截斷後的那段:截斷的位置由內容長度決定,不能讓它把命中的地方切掉
        result = await self.call(full_name, scoped, truncate=False)
        return self._truncate(await self._screen(full_name, scoped, result))

    @staticmethod
    async def _invoke(full_name: str, fn, args: dict):
        """執行一個工具。唯讀的(MODEL_TOOLS)丟到背景執行緒,外層的時間上限插得進來;
        逾時時那條執行緒自己跑完自己結束,結果不用(見 isolation.run_in_thread)。

        寫入類(貼留言、設 label、設 commit status、寫判例)**留在事件迴圈裡直接呼叫**:
        丟到背景執行緒的話,外層逾時後它仍會做完,而 webhook 在逾時時會把 commit status 設成
        失敗——還在跑的「設成成功」可能在那之後才送到,把失敗蓋掉,合併閘門就放行了。
        留在事件迴圈裡,寫入一旦開始就做完,外層等它結束才處理逾時。"""
        if full_name in MODEL_TOOLS:
            return await run_in_thread(functools.partial(fn, **args), name="segcra-tool")
        return fn(**args)

    async def call(self, full_name: str, args: dict, truncate: bool = True) -> str:
        if full_name in self.local_tools:
            result = await self._invoke(full_name, self.local_tools[full_name][0], args)
        elif full_name in self._functions:
            try:
                result = await self._invoke(full_name, self._functions[full_name], args)
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
        return self._truncate(result) if truncate else result

    def _truncate(self, result: str) -> str:
        if len(result) > self.max_chars:
            result = result[: self.max_chars] + f"\n…(截斷,原長 {len(result)} 字元)"
        return result

    async def call_json(self, full_name: str, args: dict):
        """直接呼叫並解析 JSON(orchestrator 自用,如確定性預掃);不截斷。"""
        try:
            return json.loads(await self.call(full_name, args, truncate=False))
        except json.JSONDecodeError:
            return None
