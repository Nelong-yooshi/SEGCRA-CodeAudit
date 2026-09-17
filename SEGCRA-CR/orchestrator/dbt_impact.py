"""dbt_impact — model 與規格的對應(Issue #7 第 2 點)、macro 反查(第 3 點)。

**純靜態分析,從不執行樣板**:只用 Jinja 解析器讀語法樹,不渲染、不呼叫任何 macro。
輸入(檔案路徑與內容)一律視為不可信的 MR 內容。

一、規格對應 resolve_spec()
  依 model 檔名找 `specs/<名稱>.md`(例:`mrt_RETAIL_M1.sql` → `specs/RETAIL_M1.md`),
  找不到才用 MR 中出現的 R 編號當備援(`specs/R-xxx.md`)。只回傳「呼叫端提供的
  既有規格清單」裡真的存在的路徑,不自行拼出任何檔案路徑去讀取。
  多個 R 編號對到不同規格時不擇一(回傳 None),交給人工確認。

二、macro 反查 analyze_macro_impact()
  MR 改到 macro 檔(含 config 註冊表)時,找出直接或間接呼叫到這些 macro 的 model。
  依賴判定比 dbt 本身更保守(以 dbt parse 的 manifest 為標準答案對照,見測試):
    * 名稱引用:直接呼叫、以命名空間呼叫(`pkg.macro()`)、當成值傳遞、字串中的名稱
      (例如 `context['macro']`)都算引用
    * `adapter.dispatch('x')` 引用所有 `<前綴>__x` 實作
    * 字串中的樣板(例如 `config(post_hook="{{ audit() }}")`)會被解析並納入
    * `dbt_project.yml` 與 model 目錄下的 yml 中出現的名稱(hook)視為引用
  下列情況**無法靜態確定影響範圍**,一律標為不確定(uncertain),並把可能受影響的
  model 保守列出,由人工確認——**絕不因為判斷不出來就當成沒有影響**:
    * 動態呼叫(`context[變數]()`、`adapter.dispatch(變數)` 等)
    * hook 在執行期才組出來(`config(post_hook="{" ~ "{ x() }}")`、`var(...)`、`**kwargs`)、
      `config` / `context` / `builtins` 被取別名或傳出去、`self` 與私有屬性
      (dbt 執行時不在沙箱內,可藉此取得任意 macro);已驗證 dbt 會把這類 hook 還原成
      macro 呼叫。常見的同名區域變數(`{% set config = {...} %}` 註冊表)不受影響
    * yml 中的樣板無法解析、含動態呼叫,或含可能藏住名稱的 YAML 跳脫(`\\x`、`\\u`、接行)
    * 覆寫 dbt 內建 macro(`generate_schema_name`、materialization…):影響全部 model
    * 被 `dbt_project.yml` 的 hook 參照:影響全部 model
    * 檔案解析失敗、路徑不合法、超過資源上限
    * 未提供變更前的內容(被刪除或改名的 macro 會找不到)
    * 改到 dbt 設定類檔案(yml、seeds、snapshots…)——本模組不分析其影響
  與 macro 反查無關的不確定因素(例如 MR 只改 model 時,某個未變更的 macro 檔無法解析)
  不會觸發上述判定。

  範圍刻意限於 Issue #7:只反查「macro 變更 → model」。model 變更對下游 model 的影響、
  規格檔變更的反查,不在本模組範圍。

資安:
  * 不執行樣板;Jinja 環境只用來解析,且為 SandboxedEnvironment、不掛任何檔案載入器
  * 路徑一律正規化並過白名單:拒絕絕對路徑、`..`、反斜線、冒號、控制字元與不可見字元
  * 自訂的 dbt 標籤解析器在檔案結尾必定停止(惡意 MR 故意不寫結束標籤時不會無窮迴圈)
  * 資源上限:檔案數、單檔與總字元數、語法樹節點數、字串內樣板的巢狀深度;
    超過即判定為不確定並保守列出全部 model(fail closed)
  * 語法樹以明確的堆疊走訪、反查以索引佇列走訪,皆不用遞迴;名稱掃描為線性時間,
    不使用正規表達式
  * 輸出的說明文字長度與數量有上限,且只含已過白名單的路徑與識別字
  * 不可信內容請用 analyze_macro_impact_isolated()(子行程、逾時、記憶體上限)

已知限制:
  * **本模組是審查輔助,不是資安邊界。** dbt 執行樣板時不在沙箱內,刻意以 Python 物件
    內省(例如把 macro 當參數傳給另一個 macro,再在裡面以執行期組出的名稱取屬性)
    隱藏呼叫,靜態分析無法完全判定。已擋下常見與低成本的手法(見上方不確定情況與測試);
    真正的防線是執行沙盒與資安審查
  * 以檔案為單位判定變更:macro 檔有變更時,檔內所有 macro 都視為已變更
  * 不讀取 `dbt_project.yml` 的 model-paths / macro-paths 設定,目錄由呼叫端指定
  * 不分析第三方套件(packages)內的 macro
  * 規格對應的檔名慣例(預設只去除 `mrt_` 前綴)需與甲方確認
"""
from dataclasses import dataclass, field

import jinja2
from jinja2 import TemplateSyntaxError, nodes
from jinja2.ext import Extension
from jinja2.sandbox import SandboxedEnvironment

from .dbt_builtin_macros import BUILTIN_MACROS
from .dbt_render import MIN_JINJA_VERSION, _jinja_version_ok
from .isolation import run_isolated

# 資源上限
MAX_FILES = 20_000
MAX_FILE_CHARS = 1_000_000
MAX_TOTAL_CHARS = 50_000_000
MAX_PATH_CHARS = 1_024
MAX_AST_NODES = 2_000_000
MAX_NESTED_TEMPLATE_DEPTH = 3
MAX_REASONS = 200
MAX_REASON_CHARS = 300

DEFAULT_MODEL_DIRS = ("models",)
DEFAULT_MACRO_DIRS = ("macros",)
DEFAULT_SPECS_DIR = "specs"
# 規格檔名慣例:Issue #7 範例 `mrt_RETAIL_M1.sql` → `specs/RETAIL_M1.md`。
# 其他前綴 / 後綴(例如 earlyjob model 對應哪份規格)尚未確認,不自行猜測。
DEFAULT_STRIP_PREFIXES = ("mrt_",)

DEFAULT_ISOLATED_TIMEOUT_S = 60.0
DEFAULT_ISOLATED_MEMORY_MB = 1024

# dbt 允許以 macro 覆寫的 context 函式(官方文件的 builtins 覆寫)
_CONTEXT_OVERRIDES = frozenset({"ref", "source"})
# 可依變數取用任意 macro 的物件
_DYNAMIC_ROOTS = frozenset({"context", "builtins"})
# Jinja 的樣板參照物件;dbt 執行時不在沙箱內,可經由它取得整個 context
_OPAQUE_NAMES = frozenset({"self"})
# hook 設定:值為字串,dbt 執行時會再當樣板渲染一次
_HOOK_KEYS = frozenset({"pre_hook", "post_hook", "pre-hook", "post-hook"})
# 以屬性名稱取值的 filter(可藉此取到私有屬性或 macro 物件的 context)
_ATTRIBUTE_FILTERS = frozenset({"attr", "map", "selectattr", "rejectattr", "sort", "groupby",
                                "sum", "unique", "min", "max", "join"})
# 屬性名稱也能以位置參數傳入:依 Jinja 3.1 各 filter 的參數順序(不含被過濾的值本身)
#   sort(reverse, case_sensitive, attribute)、unique / min / max(case_sensitive, attribute)、
#   join(d, attribute)、sum / groupby / selectattr / rejectattr(attribute, …)
_ATTRIBUTE_ARG_POSITION = {"sort": 2, "unique": 1, "min": 1, "max": 1, "join": 1,
                           "sum": 0, "groupby": 0, "selectattr": 0, "rejectattr": 0}
# config 物件只讀取設定、不會設定 hook 的方法
_CONFIG_READ_ATTRS = frozenset({"get", "require", "meta_get", "meta_require",
                                "persist_relation_docs", "persist_column_docs"})
# 設定類檔案:改了會影響 dbt 行為,但本模組不分析其影響範圍
_CONFIG_SUFFIXES = (".yml", ".yaml")
_RESOURCE_DIRS = frozenset({"seeds", "snapshots", "analyses", "tests"})
_JINJA_EXTENSIONS = ["jinja2.ext.do", "jinja2.ext.loopcontrols"]
_INVALID_PATH_LABEL = "(不合法的路徑)"


# ------------------------------------------------------------------ 資料結構
@dataclass(frozen=True)
class SpecMatch:
    """一個 model 對應到的規格。spec_path 為 None 時代表找不到或無法確定。"""

    model_path: str
    spec_path: str | None
    method: str                      # path | rule_code | ambiguous | none
    candidates: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AffectedModel:
    path: str
    name: str
    chain: tuple[str, ...]           # 由變更的 macro 一路到此 model 的呼叫鏈 / 判定原因
    certain: bool                    # False = 保守推定(動態呼叫、覆寫內建、hook、解析失敗…)


@dataclass(frozen=True)
class ImpactReport:
    changed_macro_files: tuple[str, ...] = ()
    changed_macros: tuple[str, ...] = ()
    changed_models: tuple[str, ...] = ()
    other_changed: tuple[str, ...] = ()
    affected_models: tuple[AffectedModel, ...] = ()
    unreferenced_macros: tuple[str, ...] = ()
    uncertain: tuple[str, ...] = ()
    all_models_possibly_affected: bool = False
    rejected_paths: int = 0

    @property
    def needs_human(self) -> bool:
        """有任何不確定因素就必須人工確認,不可自動放行。"""
        return bool(self.uncertain) or self.all_models_possibly_affected


# ------------------------------------------------------------------ 共用工具
def normalize_path(path) -> str | None:
    """正規化 repo 內相對路徑;不安全或不合法時回 None。

    拒絕:非字串、空字串、過長、絕對路徑、反斜線、冒號(磁碟代號 / 替代資料流)、
    `.` / `..` / 空的路徑段、控制字元與不可見字元(含雙向文字控制字元)。
    """
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
        return None
    if path.startswith("/") or "\\" in path or ":" in path:
        return None
    if not all(ch.isprintable() for ch in path):
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path


def _is_ident(text) -> bool:
    """Jinja / macro 識別字(ASCII)。"""
    return (isinstance(text, str) and bool(text) and text.isascii()
            and (text[0].isalpha() or text[0] == "_")
            and all(ch.isalnum() or ch == "_" for ch in text))


def _is_file_name(text: str) -> bool:
    """可用於規格檔名的 model 名稱:ASCII 英數與底線(可數字開頭)。"""
    return bool(text) and text.isascii() and all(ch.isalnum() or ch == "_" for ch in text)


def _is_rule_code(code) -> bool:
    """R-xxx 規則碼(R- 後接 2 至 4 位數字),與 spec_exec 既有格式相同。"""
    return (isinstance(code, str) and code.startswith("R-")
            and 2 <= len(code) - 2 <= 4 and code[2:].isascii() and code[2:].isdigit())


def _identifier_tokens(text: str) -> set[str]:
    """線性時間切出所有識別字(用於 yml 中的 hook 名稱)。"""
    tokens, start = set(), None
    for i, ch in enumerate(text):
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            if start is None:
                start = i
        elif start is not None:
            tokens.add(text[start:i])
            start = None
    if start is not None:
        tokens.add(text[start:])
    return {t for t in tokens if _is_ident(t)}


def _clip(text: str) -> str:
    return text if len(text) <= MAX_REASON_CHARS else text[:MAX_REASON_CHARS] + "…"


def _model_name(path: str) -> str:
    return path.rsplit("/", 1)[-1][:-len(".sql")]


# ------------------------------------------------------------------ 一、規格對應
def resolve_spec(model_path, available_specs, rule_codes=(), *,
                 specs_dir: str = DEFAULT_SPECS_DIR,
                 strip_prefixes=DEFAULT_STRIP_PREFIXES) -> SpecMatch:
    """找 model 對應的規格。

    model_path       model 檔的 repo 相對路徑
    available_specs  repo 中實際存在的規格檔路徑(例如 GitLab 列目錄的結果)
    rule_codes       MR 中出現的 R 編號(備援);可傳單一字串或多個
    """
    path = normalize_path(model_path)
    if path is None:
        return SpecMatch(_INVALID_PATH_LABEL, None, "none", (), ("model 路徑不合法,不做對應",))
    safe_dir = normalize_path(specs_dir)
    if safe_dir is None:
        return SpecMatch(path, None, "none", (), ("規格目錄設定不合法",))
    if isinstance(available_specs, str):
        available_specs = (available_specs,)
    if isinstance(rule_codes, str):
        rule_codes = (rule_codes,)
    if isinstance(strip_prefixes, str):
        strip_prefixes = (strip_prefixes,)
    available = {p for p in (normalize_path(s) for s in available_specs) if p}

    notes: list[str] = []
    candidates: list[str] = []
    name = _model_name(path) if path.endswith(".sql") else ""
    if _is_file_name(name):
        names = [name] + [name[len(p):] for p in strip_prefixes
                          if isinstance(p, str) and p and name.startswith(p) and len(name) > len(p)]
        for n in names:
            spec = f"{safe_dir}/{n}.md"
            if spec not in candidates:
                candidates.append(spec)
    else:
        notes.append("model 檔名不是 .sql,或含英數與底線以外的字元,無法依檔名對應規格")

    by_path = next((c for c in candidates if c in available), None)
    if by_path is None:
        lowered: dict[str, str] = {}
        for a in sorted(available):
            lowered.setdefault(a.lower(), a)
        near = [lowered[c.lower()] for c in candidates if c.lower() in lowered]
        if near:
            notes.append(f"只有大小寫不同的規格檔存在({near[0]}),未採用,請人工確認")

    codes = sorted({c for c in rule_codes if _is_rule_code(c)})
    by_code = sorted({f"{safe_dir}/{c}.md" for c in codes} & available)

    if by_path is not None:
        if by_code and by_code != [by_path]:
            notes.append(f"MR 中的 R 編號對應到其他規格({', '.join(by_code)}),以檔名對應為準,請人工確認")
        return SpecMatch(path, by_path, "path", tuple(candidates), tuple(notes))
    if len(by_code) == 1:
        return SpecMatch(path, by_code[0], "rule_code", tuple(candidates), tuple(notes))
    if len(by_code) > 1:
        notes.append(f"多個 R 編號對應到不同規格({', '.join(by_code)}),不自行擇一")
        return SpecMatch(path, None, "ambiguous", tuple(candidates), tuple(notes))
    notes.append("找不到對應的規格")
    return SpecMatch(path, None, "none", tuple(candidates), tuple(notes))


def resolve_specs(model_paths, available_specs, rule_codes=(), **kwargs) -> tuple[SpecMatch, ...]:
    available = (available_specs,) if isinstance(available_specs, str) else tuple(available_specs)
    codes = (rule_codes,) if isinstance(rule_codes, str) else tuple(rule_codes)
    return tuple(resolve_spec(p, available, codes, **kwargs) for p in model_paths)


# ------------------------------------------------------------------ Jinja 解析
class _DbtBlockTags(Extension):
    """讓 Jinja 解析器認得 dbt 專屬的區塊標籤,並保留區塊內容供掃描。

    `{% materialization x, default %}` → 視為名為 materialization_x_default 的 macro
    `{% test x(...) %}`                 → 視為名為 test_x 的 macro
    `{% snapshot %}` / `{% docs %}`      → 保留內容,不視為 macro
    """

    tags = {"materialization", "test", "snapshot", "docs"}

    def parse(self, parser):
        tag_token = next(parser.stream)
        tag = tag_token.value
        header = []
        # 必須檢查檔案結尾:TokenStream 在結尾會一直回傳 eof,不檢查就是無窮迴圈
        while parser.stream.current.type not in ("block_end", "eof"):
            header.append(next(parser.stream))
        if parser.stream.current.type == "eof":
            parser.fail(f"未結束的 {tag} 標籤", tag_token.lineno)
        body = parser.parse_statements((f"name:end{tag}",), drop_needle=True)
        name = self._macro_name(tag, header)
        if name is None:
            return nodes.Scope(body, lineno=tag_token.lineno)
        return nodes.Macro(name, [], [], body, lineno=tag_token.lineno)

    @staticmethod
    def _macro_name(tag, header):
        names = [t.value for t in header if t.type == "name"]
        if tag == "materialization" and (not names or not _is_ident(names[0])):
            # 認不出名稱的 materialization 仍然會改變 dbt 建表方式:保留 materialization_
            # 前綴,讓它照樣被判為覆寫內建(影響全部 model),不可當成普通區塊略過
            return "materialization_unknown_unknown"
        if tag not in ("test", "materialization") or not names or not _is_ident(names[0]):
            return None
        if tag == "test":
            return f"test_{names[0]}"
        strings = [t.value for t in header if t.type == "string"]
        if strings and _is_ident(strings[0]):
            adapter = strings[0]
        elif "default" in names[1:]:
            adapter = "default"
        else:
            adapter = "unknown"
        return f"materialization_{names[0]}_{adapter}"


class _LimitExceeded(Exception):
    pass


@dataclass
class _Refs:
    names: set = field(default_factory=set)
    dynamic: bool = False              # 動態呼叫,或字串中疑似樣板卻無法解析
    root_names: set = field(default_factory=set)   # 經 context / builtins 取用的名稱
    probed: set = field(default_factory=set)       # 以執行期名稱取項目或屬性的變數名稱
    aliases: set = field(default_factory=set)      # (別名, 原名稱):{% set a = b %}

    def merge(self, other: "_Refs") -> None:
        self.names |= other.names
        self.dynamic |= other.dynamic
        self.root_names |= other.root_names
        self.probed |= other.probed
        self.aliases |= other.aliases


@dataclass
class _FileScan:
    macros: dict = field(default_factory=dict)      # macro 名稱 → _Refs
    top: _Refs = field(default_factory=_Refs)       # 檔案層(非 macro 內)的引用


def _const_str(node) -> bool:
    return isinstance(node, nodes.Const) and isinstance(node.value, str)


def _is_dispatch_with_constant(call) -> bool:
    """adapter.dispatch('x') 或 adapter.dispatch(macro_name='x')。"""
    if call.args:
        return _const_str(call.args[0])
    return any(k.key == "macro_name" and _const_str(k.value) for k in call.kwargs)


def _is_dynamic(node) -> bool:
    """呼叫對象在執行期才知道的寫法。"""
    if isinstance(node, nodes.Getitem):
        return (isinstance(node.node, nodes.Name) and node.node.name in _DYNAMIC_ROOTS
                and not isinstance(node.arg, nodes.Const))
    if not isinstance(node, nodes.Call):
        return False
    target = node.node
    if isinstance(target, nodes.Name):
        return False
    if isinstance(target, nodes.Getattr):
        if target.attr == "dispatch":
            return not _is_dispatch_with_constant(node)
        if isinstance(target.node, nodes.Name) and target.node.name in _DYNAMIC_ROOTS:
            return not all(isinstance(a, nodes.Const) for a in node.args)
        return False
    if (isinstance(target, nodes.Call) and isinstance(target.node, nodes.Getattr)
            and target.node.attr == "dispatch"):
        return False                    # adapter.dispatch('x')(...):內層呼叫另行檢查
    if (isinstance(target, nodes.Getitem) and isinstance(target.node, nodes.Name)
            and target.node.name in _DYNAMIC_ROOTS and _const_str(target.arg)):
        return False                    # context['x'](...):名稱寫死,由 root_names 檢查是否為 macro
    return True                        # 呼叫 Getitem / Filter / 其他運算結果


def _is_name(node, name: str) -> bool:
    return isinstance(node, nodes.Name) and node.name == name


def _is_literal_hook(node) -> bool:
    """hook 值是否為寫死的字串、{sql: ..., transaction: ...} 或其清單(樣板內容另行解析)。"""
    def literal_item(item) -> bool:
        if _const_str(item):
            return True
        return isinstance(item, nodes.Dict) and all(
            isinstance(p.key, nodes.Const) and isinstance(p.value, nodes.Const) for p in item.items)
    if isinstance(node, (nodes.List, nodes.Tuple)):
        return all(literal_item(item) for item in node.items)
    return literal_item(node)


def _config_call_is_dynamic(call) -> bool:
    """config(...) 的 hook 若在執行期才組出來,dbt 執行時渲染出的 macro 呼叫靜態看不到。"""
    if call.dyn_args is not None or call.dyn_kwargs is not None:
        return True
    for kw in call.kwargs:
        if kw.key in _HOOK_KEYS and not _is_literal_hook(kw.value):
            return True
    for arg in call.args:
        if not isinstance(arg, nodes.Dict):
            return True
        for pair in arg.items:
            if not _const_str(pair.key):
                return True
            if pair.key.value in _HOOK_KEYS and not _is_literal_hook(pair.value):
                return True
    return False


def _config_set_is_dynamic(call) -> bool:
    """config.set('鍵', 值):鍵須寫死;設定 hook 時值也須寫死。"""
    if (call.dyn_args is not None or call.dyn_kwargs is not None or call.kwargs
            or len(call.args) != 2 or not _const_str(call.args[0])):
        return True
    return call.args[0].value in _HOOK_KEYS and not _is_literal_hook(call.args[1])


def _unsafe_attribute_name(node) -> bool:
    """以名稱取屬性時,名稱在執行期才知道,或指向私有屬性、context / builtins。"""
    if not _const_str(node):
        return True
    return any(part.startswith("_") or part in _DYNAMIC_ROOTS for part in node.value.split("."))


def _filter_reads_attributes(node) -> bool:
    return node.name in _ATTRIBUTE_FILTERS


def _filter_attribute_is_unsafe(node) -> bool:
    """attr / map(attribute=…) / selectattr … 取用的屬性名稱是否安全。"""
    if node.dyn_args is not None or node.dyn_kwargs is not None:
        return True
    if node.name == "attr":
        return len(node.args) != 1 or _unsafe_attribute_name(node.args[0])
    if node.name == "map" and node.args:
        # map('filter名稱', …):filter 名稱須寫死,且不可再轉交給取屬性的 filter
        first = node.args[0]
        return not _const_str(first) or first.value in _ATTRIBUTE_FILTERS
    position = _ATTRIBUTE_ARG_POSITION.get(node.name)
    if position is not None and len(node.args) > position:
        if _unsafe_attribute_name(node.args[position]):
            return True
    return any(kw.key == "attribute" and _unsafe_attribute_name(kw.value) for kw in node.kwargs)


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_YAML_HEX_ESCAPE_LENGTH = {"x": 2, "u": 4, "U": 8}


def _has_yaml_hiding_escape(text: str) -> bool:
    """YAML 雙引號字串中能還原出任意字元或接行的跳脫:`\\x7b`、`\\u005f`、`\\U0000007b`、行尾 `\\`。

    只認完整的十六進位跳脫(不完整的寫法 YAML 讀取時會報錯,dbt 無法執行),
    避免把 Windows 路徑(`C:\\Users`)或正規表達式(`\\d`)誤判為動態。線性時間。
    """
    length = len(text)
    for i in range(length - 1):
        if text[i] != "\\":
            continue
        kind = text[i + 1]
        if kind in "\r\n":
            return True
        digits = _YAML_HEX_ESCAPE_LENGTH.get(kind)
        if digits and i + 2 + digits <= length and all(
                ch in _HEX_DIGITS for ch in text[i + 2:i + 2 + digits]):
            return True
    return False


def _local_config_line(body, params, macro_line: int = 0):
    """同一範圍內,名為 config 的區域變數從哪一行之後生效(沒有則回傳 None)。

    例:`{% set config = {...} %}{{ return(config) }}`(常見的設定註冊表寫法)
    之後讀取的 config 是區域變數,不是 dbt 的 config 物件。
    只採計一定會生效的寫法:macro 參數,或 macro / 檔案本體「最外層」的 set。
    包在 if / for 等區塊內的 set 不一定執行(且 for 內的 set 不外漏),不採計。
    同一行內無法判斷先後,只有在更後面的行讀取才視為區域變數。
    """
    if any(p.name == "config" for p in params):
        return macro_line - 1
    lines = [stmt.lineno for stmt in body
             if isinstance(stmt, (nodes.Assign, nodes.AssignBlock))
             and _is_name(stmt.target, "config")]
    return min(lines) if lines else None


class _Scanner:
    def __init__(self):
        # 只用來解析(parse),從不 render;仍用沙箱、不掛 loader
        self.env = SandboxedEnvironment(extensions=_JINJA_EXTENSIONS + [_DbtBlockTags])
        self.nodes_seen = 0

    def scan(self, source: str, depth: int = 0) -> _FileScan:
        tree = self.env.parse(source)
        result = _FileScan()
        # config / context / builtins 只允許出現在已檢查過的位置;其他用法(例如
        # `{% set c = config %}`、當成參數傳出去)可能繞過檢查,一律視為動態。
        # 父節點一定先於子節點走訪,所以子節點被走訪時已登記。
        vetted: set[int] = set()
        stack = [(tree, None, _local_config_line(tree.body, ()))]
        while stack:
            node, owner, shadow = stack.pop()
            self.nodes_seen += 1
            if self.nodes_seen > MAX_AST_NODES:
                raise _LimitExceeded(f"語法樹節點數超過 {MAX_AST_NODES}")
            if isinstance(node, nodes.Macro):
                owner = node.name
                result.macros.setdefault(owner, _Refs())
                shadow = _local_config_line(node.body, node.args, node.lineno)
            refs = result.top if owner is None else result.macros[owner]
            if isinstance(node, nodes.Name) and node.ctx == "load":
                refs.names.add(node.name)
                shadowed = (node.name == "config" and shadow is not None
                            and node.lineno > shadow)
                if node.name in _OPAQUE_NAMES or (
                        node.name in _DYNAMIC_ROOTS | {"config"} and id(node) not in vetted
                        and not shadowed):
                    refs.dynamic = True
            elif isinstance(node, nodes.Getattr):
                refs.names.add(node.attr)
                self._check_getattr(node, refs, vetted)
            elif isinstance(node, nodes.Getitem):
                if isinstance(node.node, nodes.Name) and node.node.name in _DYNAMIC_ROOTS:
                    vetted.add(id(node.node))
                    if _const_str(node.arg):
                        refs.root_names.add(node.arg.value)
                elif _const_str(node.arg):
                    # Jinja 取不到項目時會改取同名屬性:x['__globals__']、macro['context']
                    refs.dynamic |= _unsafe_attribute_name(node.arg)
                elif not isinstance(node.arg, nodes.Const) and isinstance(node.node, nodes.Name):
                    refs.probed.add(node.node.name)
            elif isinstance(node, nodes.Filter):
                if _filter_reads_attributes(node):
                    refs.dynamic |= _filter_attribute_is_unsafe(node)
            elif isinstance(node, nodes.Assign) and isinstance(node.target, nodes.Name):
                if isinstance(node.node, nodes.Name):
                    refs.aliases.add((node.target.name, node.node.name))
            elif isinstance(node, nodes.Call):
                self._check_call(node, refs, vetted)
            elif _const_str(node):
                self._scan_string(node.value, refs, depth)
            if _is_dynamic(node):
                refs.dynamic = True
            if isinstance(node, (nodes.Assign, nodes.AssignBlock)) and _is_name(node.target, "config"):
                shadow = None               # 等號右邊(可能跨多行)讀到的仍是賦值前的 config
            if isinstance(node, nodes.Macro):
                # 參數預設值讀到的是 macro 外的 config,不受參數同名遮蔽
                stack.extend((child, owner, None) for child in node.defaults)
                stack.extend((child, owner, shadow) for child in (*node.args, *node.body))
            else:
                stack.extend((child, owner, shadow) for child in node.iter_child_nodes())
        return result

    @staticmethod
    def _check_getattr(node, refs: _Refs, vetted: set) -> None:
        if node.attr.startswith("_") or node.attr in _DYNAMIC_ROOTS:
            # 私有屬性、macro 物件的 .context:dbt 不在沙箱內,可能取到整個 context
            refs.dynamic = True
        if isinstance(node.node, nodes.Name):
            if node.node.name in _DYNAMIC_ROOTS:
                vetted.add(id(node.node))
                refs.root_names.add(node.attr)
            elif node.node.name == "config" and node.attr in _CONFIG_READ_ATTRS:
                vetted.add(id(node.node))

    @staticmethod
    def _check_call(call, refs: _Refs, vetted: set) -> None:
        target = call.node
        if _is_name(target, "config"):
            vetted.add(id(target))
            refs.dynamic |= _config_call_is_dynamic(call)
        elif (isinstance(target, nodes.Getattr) and _is_name(target.node, "config")
              and target.attr == "set"):
            vetted.add(id(target.node))
            refs.dynamic |= _config_set_is_dynamic(call)
        elif _is_name(target, "set_sql_header"):
            # dbt 內建:只會設定 sql_header(若專案覆寫它,會以覆寫內建處理)
            vetted.update(id(a) for a in call.args if _is_name(a, "config"))

    def scan_yml(self, text: str) -> _Refs:
        """yml(hook、設定):出現的識別字都當引用,並以 Jinja 解析整份檔案中的樣板。

        整份解析(而非逐段)才能正確處理跨段的 `{% if %}…{% endif %}`;YAML 的單一 `{`
        (flow mapping)對 Jinja 只是一般文字。解析失敗視為動態。
        YAML 雙引號字串的跳脫(`\\x7b`、`\\u005f`、行尾反斜線接行)會在 dbt 讀取時才
        還原,可能藏住樣板或 macro 名稱,出現時也視為動態。
        """
        refs = _Refs(names=_identifier_tokens(text))
        refs.dynamic = _has_yaml_hiding_escape(text)
        if "{{" in text or "{%" in text:
            try:
                inner = self.scan(text, 1)
            except (TemplateSyntaxError, RecursionError):
                refs.dynamic = True
            else:
                for part in (inner.top, *inner.macros.values()):
                    refs.merge(part)
        return refs

    def _scan_string(self, value: str, refs: _Refs, depth: int) -> None:
        if _is_ident(value):
            refs.names.add(value)
        if "{{" not in value and "{%" not in value:
            return
        if depth >= MAX_NESTED_TEMPLATE_DEPTH:
            refs.dynamic = True
            return
        try:
            inner = self.scan(value, depth + 1)
        except (TemplateSyntaxError, RecursionError):
            refs.dynamic = True
            return
        for part in (inner.top, *inner.macros.values()):
            refs.merge(part)

    def try_scan(self, text: str) -> _FileScan | None:
        try:
            return self.scan(text)
        except (TemplateSyntaxError, RecursionError):
            return None


# ------------------------------------------------------------------ 二、macro 反查
def _kind(path: str, model_dirs, macro_dirs) -> str:
    if path.endswith(".sql"):
        if any(path.startswith(d + "/") for d in macro_dirs):
            return "macro"
        if any(path.startswith(d + "/") for d in model_dirs):
            return "model"
    if path.endswith(_CONFIG_SUFFIXES) or path.split("/", 1)[0] in _RESOURCE_DIRS:
        return "dbt_config"
    return "other"


def _is_builtin_override(name: str) -> bool:
    if name in BUILTIN_MACROS or name in _CONTEXT_OVERRIDES or name.startswith("materialization_"):
        return True
    _, sep, suffix = name.partition("__")
    return bool(sep) and suffix in BUILTIN_MACROS


def _fail_closed(reason: str, model_paths=()) -> ImpactReport:
    affected = tuple(AffectedModel(p, _model_name(p), (_clip(reason),), False)
                     for p in sorted(model_paths))
    return ImpactReport(affected_models=affected, uncertain=(_clip(reason),),
                        all_models_possibly_affected=True)


def _walk(callers_of: dict, start, label):
    """由 start 的 macro 反向走訪引用者。

    回傳 (model 命中 [(路徑, 呼叫鏈)], yml 命中 [(路徑, 呼叫鏈)], 有被引用的起點名稱)。
    以索引走訪佇列(不用 list.pop(0),避免大型專案變成平方時間)。
    """
    queue = [(name, (label(name),)) for name in sorted(start)]
    seen = set(start)
    model_hits, yml_hits, referenced = [], [], set()
    index = 0
    while index < len(queue):
        name, chain = queue[index]
        index += 1
        for kind, who in sorted(callers_of.get(name, ())):
            if name in start:
                referenced.add(name)
            if kind == "macro":
                if who not in seen:
                    seen.add(who)
                    queue.append((who, chain + (who,)))
            elif kind == "model":
                model_hits.append((who, chain + (who,)))
            else:
                yml_hits.append((who, chain))
    return model_hits, yml_hits, referenced


def analyze_macro_impact(files, changed_paths, *, base_files=None,
                         model_dirs=DEFAULT_MODEL_DIRS,
                         macro_dirs=DEFAULT_MACRO_DIRS) -> ImpactReport:
    """反查 macro 變更影響到的 model。

    files          變更後的 dbt 專案檔案:相對於 dbt 專案根目錄的路徑 → 內容
    changed_paths  MR 變更的檔案路徑(相對於同一個根目錄)
    base_files     變更前的內容:路徑 → 內容(新增的檔案不必提供)。未提供時,
                   被刪除或改名的 macro 無法偵測,會標為不確定
    model_dirs / macro_dirs  model 與 macro 所在目錄

    任何無法確定的情況都收斂成 needs_human,不丟例外。
    """
    try:
        return _Analysis(files, changed_paths, base_files, model_dirs, macro_dirs).run()
    except _LimitExceeded as e:
        return _fail_closed(f"超過資源上限({e}),無法完成分析,保守視為全部 model 可能受影響")
    except RecursionError:
        return _fail_closed("語法結構過深,無法完成分析,保守視為全部 model 可能受影響")


class _Analysis:
    def __init__(self, files, changed_paths, base_files, model_dirs, macro_dirs):
        self.raw_files = files
        self.raw_changed = [changed_paths] if isinstance(changed_paths, str) else list(changed_paths)
        self.raw_base = base_files
        self.raw_model_dirs = (model_dirs,) if isinstance(model_dirs, str) else tuple(model_dirs)
        self.raw_macro_dirs = (macro_dirs,) if isinstance(macro_dirs, str) else tuple(macro_dirs)
        self.uncertain: list[str] = []
        self.all_reasons: list[str] = []
        self.rejected = 0
        self.affected: dict[str, AffectedModel] = {}
        self.scanner = _Scanner()

    # ---------------------------------------------------------------- 主流程
    def run(self) -> ImpactReport:
        if not _jinja_version_ok(jinja2.__version__):
            return _fail_closed(
                f"jinja2 {jinja2.__version__} 低於 {'.'.join(map(str, MIN_JINJA_VERSION))},拒絕分析")
        self.model_dirs = tuple(normalize_path(d) for d in self.raw_model_dirs)
        self.macro_dirs = tuple(normalize_path(d) for d in self.raw_macro_dirs)
        if not self.model_dirs or not self.macro_dirs or None in self.model_dirs + self.macro_dirs:
            return _fail_closed("model / macro 目錄設定不合法,無法分析")

        rejected_project_files = self._load_files()
        base = self._load_base()
        self._classify_changes()
        self._scan_project()
        changed_macros, change_problems = self._changed_macros(base)

        if self.changed_macro_files:
            if self.raw_base is None:
                self.uncertain.append("未提供變更前的內容:被刪除或改名的 macro 無法偵測")
            self.all_reasons.extend(change_problems)
            if rejected_project_files:
                self.all_reasons.append(
                    f"有 {rejected_project_files} 個專案檔案路徑或內容不合法而被略過,無法確認其中的呼叫關係")
            for path in self.unparsable_macro_files:
                self.all_reasons.append(f"macro 檔 {path} 無法解析,無法確認其中的呼叫關係")
            for name, paths in sorted(self.macro_files.items()):
                if len(paths) > 1:
                    self.uncertain.append(f"macro {name} 在多個檔案中重複定義({', '.join(sorted(paths))})")

        unreferenced: list[str] = []
        if changed_macros:
            unreferenced = self._trace(changed_macros)

        all_models = bool(self.all_reasons)
        if all_models:
            for path in self.model_paths:
                self._add_model(path, (self.all_reasons[0],), False)

        reasons = [_clip(r) for r in dict.fromkeys(self.uncertain + self.all_reasons)]
        if len(reasons) > MAX_REASONS:
            reasons = reasons[:MAX_REASONS] + [f"……另有 {len(reasons) - MAX_REASONS} 項"]
        return ImpactReport(
            changed_macro_files=tuple(self.changed_macro_files),
            changed_macros=tuple(sorted(changed_macros)),
            changed_models=tuple(self.changed_models),
            other_changed=tuple(self.other_changed),
            affected_models=tuple(self.affected[p] for p in sorted(self.affected)),
            unreferenced_macros=tuple(unreferenced),
            uncertain=tuple(reasons),
            all_models_possibly_affected=all_models,
            rejected_paths=self.rejected,
        )

    # ---------------------------------------------------------------- 輸入
    def _load_files(self) -> int:
        items = list(self.raw_files.items())
        if len(items) > MAX_FILES:
            raise _LimitExceeded(f"檔案數超過 {MAX_FILES}")
        self.project: dict[str, str] = {}
        rejected, total = 0, 0
        for raw_path, content in items:
            path = normalize_path(raw_path)
            if path is None or not isinstance(content, str):
                rejected += 1
                continue
            if len(content) > MAX_FILE_CHARS:
                raise _LimitExceeded(f"單一檔案超過 {MAX_FILE_CHARS} 字元")
            total += len(content)
            if total > MAX_TOTAL_CHARS:
                raise _LimitExceeded(f"檔案合計超過 {MAX_TOTAL_CHARS} 字元")
            self.project[path] = content
        self.rejected += rejected
        self.model_paths = sorted(p for p in self.project if self._kind(p) == "model")
        return rejected

    def _load_base(self) -> dict[str, str]:
        base: dict[str, str] = {}
        if self.raw_base is None:
            return base
        items = list(self.raw_base.items())
        if len(items) > MAX_FILES:
            raise _LimitExceeded(f"變更前檔案數超過 {MAX_FILES}")
        total = 0
        for raw_path, content in items:
            path = normalize_path(raw_path)
            if path is None or not isinstance(content, str):
                self.rejected += 1
                continue
            if len(content) > MAX_FILE_CHARS:
                raise _LimitExceeded(f"變更前的單一檔案超過 {MAX_FILE_CHARS} 字元")
            total += len(content)
            if total > MAX_TOTAL_CHARS:
                raise _LimitExceeded(f"變更前的檔案合計超過 {MAX_TOTAL_CHARS} 字元")
            base[path] = content
        return base

    def _kind(self, path: str) -> str:
        return _kind(path, self.model_dirs, self.macro_dirs)

    def _classify_changes(self) -> None:
        if len(self.raw_changed) > MAX_FILES:
            raise _LimitExceeded(f"變更檔案數超過 {MAX_FILES}")
        macro_files, models, others = set(), set(), set()
        valid = set()
        for raw_path in self.raw_changed:
            path = normalize_path(raw_path)
            if path is None:
                self.rejected += 1
                self.all_reasons.append("有變更檔案的路徑不合法,無法確認其影響")
            else:
                valid.add(path)
        # 排序後處理:同一個 MR 不論變更清單的順序,結果(含說明順序)都必須相同
        for path in sorted(valid):
            kind = self._kind(path)
            if kind == "macro":
                macro_files.add(path)
            elif kind == "model":
                models.add(path)
            else:
                others.add(path)
                if kind == "dbt_config":
                    self.uncertain.append(f"變更了 dbt 設定或資源檔 {path},本模組不分析其影響範圍")
        self.changed_macro_files = sorted(macro_files)
        self.changed_models = sorted(models)
        self.other_changed = sorted(others)

    # ---------------------------------------------------------------- 掃描
    def _scan_project(self) -> None:
        self.macro_refs: dict[str, _Refs] = {}
        self.macro_files: dict[str, set] = {}
        self.model_refs: dict[str, _Refs] = {}
        self.unparsable_macro_files: list[str] = []
        self.unparsable_models: list[str] = []
        for path in sorted(self.project):
            kind = self._kind(path)
            if kind not in ("macro", "model"):
                continue
            result = self.scanner.try_scan(self.project[path])
            if result is None:
                (self.unparsable_macro_files if kind == "macro" else self.unparsable_models).append(path)
                continue
            if kind == "macro":
                for name, refs in result.macros.items():
                    self.macro_refs.setdefault(name, _Refs()).merge(refs)
                    self.macro_files.setdefault(name, set()).add(path)
            else:
                refs = _Refs()
                for part in (result.top, *result.macros.values()):  # model 內自訂的 macro 一併計入
                    refs.merge(part)
                self.model_refs[path] = refs

    def _changed_macros(self, base: dict) -> tuple[set, list]:
        changed, problems = set(), []
        for path in self.changed_macro_files:
            new_text, old_text = self.project.get(path), base.get(path)
            if new_text is None and old_text is None:
                problems.append(f"macro 檔 {path} 已不存在且沒有變更前內容,無法得知被刪除的 macro")
                continue
            for label, text in (("變更後", new_text), ("變更前", old_text)):
                if text is None:
                    continue
                result = self.scanner.try_scan(text)
                if result is None:
                    problems.append(f"macro 檔 {path}({label})無法解析,無法得知變更了哪些 macro")
                    continue
                changed |= set(result.macros)
        return changed, problems

    # ---------------------------------------------------------------- 反查
    def _add_model(self, path: str, chain, certain: bool) -> None:
        current = self.affected.get(path)
        if current is None or (certain and not current.certain):
            self.affected[path] = AffectedModel(path, _model_name(path),
                                                tuple(_clip(c) for c in chain), certain)

    def _callers(self, changed_macros: set) -> tuple[dict, list]:
        known = set(self.macro_refs) | changed_macros
        suffix_index: dict[str, set] = {}
        for name in known:
            for cut in {name.find("__"), name.rfind("__")}:
                if cut > 0 and cut + 2 < len(name):
                    suffix_index.setdefault(name[cut + 2:], set()).add(name)

        def resolve(ref_names) -> set:
            found = set()
            for ref in ref_names:
                if ref in known:
                    found.add(ref)
                found |= suffix_index.get(ref, set())
            return found

        callables = known | BUILTIN_MACROS | _CONTEXT_OVERRIDES

        def is_dynamic(refs: _Refs) -> bool:
            # context.get(...)、context.items() 等:取用的不是 macro,可能藉此取到任意 macro
            if refs.dynamic or refs.root_names - callables:
                return True
            # 以執行期名稱對 macro(或其別名)取項目 / 屬性:flag[k]、{% set m = flag %}{{ m[k] }}
            probed, grew = set(refs.probed), True
            while grew:
                grew = False
                for alias, source in refs.aliases:
                    if alias in probed and source not in probed:
                        probed.add(source)
                        grew = True
            return bool(probed & callables)

        callers_of: dict[str, set] = {}
        dynamic_scopes = []
        for name, refs in self.macro_refs.items():
            for target in resolve(refs.names) - {name}:
                callers_of.setdefault(target, set()).add(("macro", name))
            if is_dynamic(refs):
                dynamic_scopes.append(("macro", name))
        for path, refs in self.model_refs.items():
            for target in resolve(refs.names):
                callers_of.setdefault(target, set()).add(("model", path))
            if is_dynamic(refs):
                dynamic_scopes.append(("model", path))
        for path in sorted(self.project):
            if path.endswith(_CONFIG_SUFFIXES) and (
                    path == "dbt_project.yml"
                    or any(path.startswith(d + "/") for d in self.model_dirs)):
                refs = self.scanner.scan_yml(self.project[path])
                for target in resolve(refs.names):
                    callers_of.setdefault(target, set()).add(("yml", path))
                if is_dynamic(refs):
                    dynamic_scopes.append(("yml", path))
        return callers_of, dynamic_scopes

    def _apply_hits(self, model_hits, yml_hits, certain: bool) -> None:
        for path, chain in model_hits:
            self._add_model(path, chain, certain)
        for yml_path, chain in yml_hits:
            if yml_path == "dbt_project.yml":
                self.all_reasons.append(
                    f"變更的 macro 被 dbt_project.yml 參照(可能是 hook):{' → '.join(chain)}")
                continue
            folder = yml_path.rsplit("/", 1)[0]
            self.uncertain.append(f"變更的 macro 被 {yml_path} 參照(可能是 hook 或設定)")
            for model in self.model_paths:
                if model.startswith(folder + "/"):
                    self._add_model(model, chain + (yml_path,), False)

    def _trace(self, changed_macros: set) -> list[str]:
        callers_of, dynamic_scopes = self._callers(changed_macros)

        def label(name: str) -> str:
            where = ", ".join(sorted(self.macro_files.get(name, set()))) or "已刪除"
            return f"{name}({where})"

        model_hits, yml_hits, referenced = _walk(callers_of, changed_macros, label)
        self._apply_hits(model_hits, yml_hits, certain=True)

        unreferenced = []
        for name in sorted(changed_macros):
            if _is_builtin_override(name):
                self.all_reasons.append(
                    f"變更的 macro {name} 覆寫了 dbt 內建行為,dbt 執行任何 model 時都可能呼叫")
            elif name not in referenced:
                unreferenced.append(name)

        # 動態呼叫:這些位置可能呼叫任何 macro,連同呼叫它們的 model 都保守列入
        for path in self.unparsable_models:
            self._add_model(path, ("model 無法解析,無法確認它呼叫了哪些 macro",), False)
            self.uncertain.append(f"model {path} 無法解析,無法確認它呼叫了哪些 macro")
        dynamic_macros = {who for kind, who in dynamic_scopes if kind == "macro"}
        dynamic_ymls = []
        for kind, who in dynamic_scopes:
            if kind == "model":
                self._add_model(who, ("model 內有動態呼叫或無法解析的樣板字串",), False)
            elif kind == "yml":
                dynamic_ymls.append((who, ("yml 內有動態呼叫、無法解析的樣板或跳脫字元",)))
        self._apply_hits((), dynamic_ymls, certain=False)
        if dynamic_macros:
            model_hits, yml_hits, _ = _walk(
                callers_of, dynamic_macros,
                lambda n: f"{n} 內有動態呼叫或無法解析的樣板字串,可能呼叫到變更的 macro")
            self._apply_hits(model_hits, yml_hits, certain=False)
        if dynamic_scopes:
            self.uncertain.append("專案中有動態呼叫 macro 的寫法,靜態分析無法確定實際呼叫對象")
        return unreferenced


# ------------------------------------------------------------------ 隔離執行
def _impact_failure(message: str) -> ImpactReport:
    return _fail_closed(f"分析失敗({message}),保守視為全部 model 可能受影響")


def analyze_macro_impact_isolated(files, changed_paths, *,
                                  timeout_s: float = DEFAULT_ISOLATED_TIMEOUT_S,
                                  max_memory_mb: int = DEFAULT_ISOLATED_MEMORY_MB,
                                  **kwargs) -> ImpactReport:
    """在獨立子行程中執行 analyze_macro_impact(參數相同)。**待審的 MR 內容一律走這個。**

    失敗(逾時、記憶體不足、異常)時回傳「全部 model 可能受影響」的保守結果。
    注意:此時 affected_models 為空(子行程失敗,無法得知 model 清單),但
    all_models_possibly_affected 為 True,呼叫端必須據此交人工確認。
    """
    changed = [changed_paths] if isinstance(changed_paths, str) else list(changed_paths)
    result = run_isolated(analyze_macro_impact, (dict(files), changed), kwargs,
                          timeout_s=timeout_s, max_memory_mb=max_memory_mb,
                          on_failure=_impact_failure)
    if not isinstance(result, ImpactReport):
        return _impact_failure("子行程回傳了非預期的資料")
    return result
