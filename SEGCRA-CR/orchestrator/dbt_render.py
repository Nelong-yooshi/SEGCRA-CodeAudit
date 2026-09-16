"""dbt_render — 把 dbt model 展開成可分析的 T-SQL(確定性,不經 LLM)。

前處理的規則檢查與執行驗證吃的是純 SQL,但 repo 裡的 model 是 dbt 樣板:
`{{ ref() }}`、`{{ config() }}`、`{% set %}`,以及會展開成一整段條件的 macro。
不展開就掃,等於 macro 裡的門檻值與 op_code 白名單全是黑箱——規則層看不到
`amount > 1000`,執行驗證也拿不到可跑的 SQL。

流程:
  1. 用 jinja2 載入 <code_root>/macros/**.sql,把每個 macro 註冊成全域函式。
     dbt 的 `{{ return(x) }}` 是靠丟例外實作的,這裡照做(_Return);否則
     get_config() 只會拿到渲染後的字串而不是 dict,`cfg['inward_codes']` 會炸。
  2. 渲染 model,得到純 SQL。
  3. 建立「渲染行 → 原始行」對應,讓 finding 的行號能貼回原始檔。
     主作法是**哨兵**:在每行原始碼前插一個 SQL 註解標記後再渲染一次,
     從輸出把標記讀回來。這是確定性的,而且可以自我驗證(去掉標記後必須與
     乾淨渲染逐字相同)。對不上時退回 difflib 近似;再不行就回 0(= 檔案層
     留言)。**寧可不指行,也不要指錯行**——行內留言貼錯位置是使用者一眼
     就看到的失誤,比漏報更傷信任。

資安:這裡渲染的是**待審的 MR 內容,不是我們自己的程式碼**。所以一律用
`SandboxedEnvironment`——普通的 `Environment` 會讓惡意樣板(例如
`{{ ''.__class__.__mro__[1].__subclasses__() }}`)在審查機上取得任意程式執行。
同理,呼叫端把 GitLab 來的內容交給這裡時,應該用 `source=` 參數傳「內容」,
不要傳一個由 MR 資料拼出來的路徑,否則等於開了任意檔案讀取。

設計底線:**寧可展開失敗,也不要產出看似成功、實則錯誤的 SQL**。
所以未定義的 dbt 內建(StrictUndefined)、以及沒有預設值又沒被提供的 var,
一律是硬失敗;絕不猜值。猜了會靜靜把 `None` 寫進 WHERE 條件裡,而上游
完全看不出來——那比展開失敗危險得多。

刻意與 `dbt compile` 不同的地方:`ref('x')` 在這裡展開成裸表名 `x`,dbt 會展開成
帶 database/schema 的完整名稱。因為後續要在沙盒用自建測資表執行,不能帶正式環境
前綴。與 dbt compile 對照時必須先正規化這一項。

已知限制:
  * `make_temp_relation` 這類覆寫 dbt 內建的 macro 會操作 adapter 的 Relation
    物件(`.identifier`、`.incorporate()`),純 jinja 樁做不出來。它們只在
    `dbt run` 期間由 dbt 自己呼叫,compile 與本模組都不會碰到;但若哪天有
    model 直接呼叫,會以 render 失敗回報(而非悄悄產出錯的 SQL)。
  * macro 同名時 dbt 用套件命名空間解析,這裡是單一平面命名空間。遇到同名
    只能擇一,結果**可能與 dbt 不同** → 記進 `macro_conflicts` 讓上游看得到,
    不靜靜吞掉。
"""
import difflib
import pathlib
import re
from dataclasses import dataclass, field

from jinja2 import FileSystemLoader, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

# 哨兵:SQL 區塊註解形式,只用於建立行號對應的那一次渲染,不會進到乾淨輸出。
_SENTINEL = "/*__SEGCRA_SRC_L{n}__*/"
_SENTINEL_RE = re.compile(r"/\*__SEGCRA_SRC_L(\d+)__\*/")

# 判斷一份檔案是不是 dbt 樣板(用來決定要不要展開);註解標記也算。
_TEMPLATE_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)

# 刻意留空:不預設任何 dbt var。
# dbt 的 var() 只看專案/CLI 變數,看不到樣板裡的 `{% set %}`;沒設定又沒預設值時
# dbt 會編譯失敗。這裡若擅自給預設,等於用一個猜來的值掩蓋 dbt 會報錯的情況,
# 而且渲染出來的 SQL 會帶著錯的日期跑進執行驗證。變數一律由呼叫端明確傳入。
_DEFAULT_VARS: dict = {}

_MISSING = object()


class DbtRenderError(Exception):
    """展開失敗。呼叫端應退回「未展開」的既有行為,不可當成「掃過且沒問題」。"""


class _Return(Exception):
    """模仿 dbt 的 MacroReturn:讓 macro 能回傳 Python 物件而不只是字串。"""

    def __init__(self, value):
        self.value = value


def _return(value):
    raise _Return(value)


def _wrap(macro):
    """把 jinja Macro 包成 Python 函式:有 return() 就取物件,否則取渲染字串。"""

    def call(*args, **kwargs):
        try:
            return macro(*args, **kwargs)
        except _Return as r:
            return r.value

    return call


def _make_var(variables: dict):
    """dbt 的 var()。找不到又沒給預設值時**硬失敗**,不回 None。

    回 None 的話 Jinja 會把它渲染成字串 "None" 寫進 SQL(例如
    `WHERE d = None`),而且整個流程還會回報展開成功——這種錯最難抓。
    dbt 本身在這情況也是編譯失敗,行為一致。
    """

    def var(name, default=_MISSING):
        if name in variables:
            return variables[name]
        if default is _MISSING:
            raise DbtRenderError(
                f"var('{name}') 沒有預設值,呼叫端也沒有提供。"
                f"dbt 在這種情況會編譯失敗,這裡同樣不猜值。")
        return default

    return var


@dataclass
class RenderResult:
    """展開結果。ok=False 時 sql 為空字串,呼叫端必須據此退回既有行為。"""

    ok: bool
    sql: str = ""
    line_map: dict[int, int] = field(default_factory=dict)
    map_method: str = "none"          # sentinel | difflib | none
    macros: list[str] = field(default_factory=list)
    macro_conflicts: list[str] = field(default_factory=list)
    source_lines: int = 0
    rendered_lines: int = 0
    error: str | None = None

    def src_line(self, rendered_line: int) -> int:
        """渲染行(1-based)→ 原始行(1-based)。對應不到回 0 = 檔案層留言。"""
        return self.line_map.get(rendered_line, 0)


def is_dbt_template(text: str) -> bool:
    """這份內容有沒有 Jinja 標記——沒有就不必走展開。"""
    return bool(_TEMPLATE_RE.search(text or ""))


# ------------------------------------------------------- Jinja 環境與 dbt 樁
def build_env(code_root: pathlib.Path, variables: dict | None = None):
    """建好含 dbt 樁與所有 macro 的 Environment。

    回傳 (env, macro 名稱清單, 同名衝突清單)。
    """
    variables = {**_DEFAULT_VARS, **(variables or {})}
    # 必須是 SandboxedEnvironment:這裡渲染的是**待審的 MR 內容**,不是我們的程式碼。
    # 用一般的 Environment 的話,`{{ ''.__class__.__mro__[1].__subclasses__() }}`
    # 這類樣板注入可以在審查機上取得任意程式執行(已實測可行)。
    env = SandboxedEnvironment(
        loader=FileSystemLoader(str(code_root)),
        undefined=StrictUndefined,   # 沒樁到的 dbt 內建要大聲失敗,不可悄悄變空字串
        keep_trailing_newline=True,
    )

    # dbt 內建的樁。ref 刻意回傳裸表名(見模組 docstring)。
    env.globals["ref"] = lambda name, *a, **k: name
    env.globals["source"] = lambda src, name: f"{src}_{name}"
    env.globals["config"] = lambda *a, **k: ""      # config 不產生 SQL
    env.globals["return"] = _return
    env.globals["var"] = _make_var(variables)
    env.globals["is_incremental"] = lambda: False   # compile 期一律視為全量
    env.globals["invocation_id"] = "segcra00"
    env.globals["this"] = "THIS_RELATION"
    env.globals["target"] = {"schema": "dbo", "name": "segcra"}

    macros: list[str] = []
    conflicts: list[str] = []
    seen: dict[str, str] = {}
    macro_dir = code_root / "macros"
    if macro_dir.is_dir():
        for f in sorted(macro_dir.rglob("*.sql")):
            rel = f.relative_to(code_root).as_posix()
            module = env.get_template(rel).module
            for name in dir(module):
                if name.startswith("_"):
                    continue
                obj = getattr(module, name)
                if not callable(obj):
                    continue
                if name in seen:
                    # dbt 會用套件命名空間分辨,我們只有單一平面命名空間 →
                    # 擇一的結果可能與 dbt 不同,必須讓上游看見。
                    conflicts.append(f"{name}: {seen[name]} / {rel}")
                else:
                    macros.append(name)
                seen[name] = rel
                env.globals[name] = _wrap(obj)
    return env, sorted(macros), conflicts


# ------------------------------------------------------------- 哨兵行號對應
def _lines_inside_tag(src: str) -> set[int]:
    """行首落在 Jinja 標記內部的行號(0-based)——這些行不能插哨兵,會破壞語法。

    例:`{{ config(` 跨到第 5 行才 `) }}`,中間幾行的行首都在標記內。
    """
    inside: set[int] = set()
    closer: str | None = None
    line, i, n = 0, 0, len(src)
    while i < n:
        if src[i] == "\n":
            line += 1
            if closer:
                inside.add(line)
            i += 1
            continue
        if closer:
            if src.startswith(closer, i):
                i += len(closer)
                closer = None
            else:
                i += 1
            continue
        two = src[i:i + 2]
        if two == "{{":
            closer = "}}"
        elif two == "{%":
            closer = "%}"
        elif two == "{#":
            closer = "#}"
        else:
            i += 1
            continue
        i += 2
    return inside


def _inject_sentinels(src: str) -> str:
    inside = _lines_inside_tag(src)
    parts = src.split("\n")
    out = []
    for idx, line in enumerate(parts):
        # 檔尾換行造成的空尾元素不是真的一行,不要給它哨兵(否則會產生一個
        # 指向「檔案第 N+1 行」的對應,而那一行並不存在)。
        trailing_blank = (idx == len(parts) - 1 and line == "")
        if idx in inside or trailing_blank:
            out.append(line)
        else:
            out.append(_SENTINEL.format(n=idx + 1) + line)
    return "\n".join(out)


def _map_from_sentinels(marked: str, clean: str) -> dict[int, int] | None:
    """哨兵法建圖。去掉哨兵後必須與乾淨渲染逐字相同,否則視為失敗回 None。

    這個自我驗證是整套的安全閥:對不上就代表哨兵干擾了渲染(例如標記被夾進
    字串常量),此時寧可退回近似法,也不要交出一張看似完整、實則錯位的圖。
    """
    stripped: list[str] = []
    mapping: dict[int, int] = {}
    cur = 0
    for line in marked.split("\n"):
        hits = _SENTINEL_RE.findall(line)
        if hits:
            cur = int(hits[-1])
        stripped.append(_SENTINEL_RE.sub("", line))
        mapping[len(stripped)] = cur
    if "\n".join(stripped) != clean:
        return None
    return mapping


_TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)


def _map_from_difflib(src: str, clean: str) -> dict[int, int]:
    """退路:把樣板標記遮掉後對齊固定文字。近似,只在哨兵失效時使用。"""
    a = [_TAG.sub("", l).strip() for l in src.split("\n")]
    b = [l.strip() for l in clean.split("\n")]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    mapping: dict[int, int] = {}
    last = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                mapping[j1 + k + 1] = i1 + k + 1
            last = i2
        else:
            anchor = i1 + 1 if i1 < len(a) else max(last, 1)
            for j in range(j1, j2):
                mapping[j + 1] = anchor
    return mapping


def _clamp(line_map: dict[int, int], n_src: int, n_out: int) -> dict[int, int]:
    """只保留真實存在的渲染行,且原始行號不得超出檔案實際行數。

    `split("\\n")` 會因檔尾換行多出一個空元素,不夾住的話會產生指向
    「第 N+1 行」的對應——GitLab 貼不上去,或貼到別的地方。
    """
    return {k: min(v, n_src) for k, v in line_map.items() if 1 <= k <= n_out}


# --------------------------------------------------------------------- 主入口
def render_model(model_path, code_root=None, variables: dict | None = None,
                 source: str | None = None) -> RenderResult:
    """展開一份 dbt model。

    model_path  model 檔路徑(相對 code_root 或絕對路徑皆可)
    code_root   dbt 專案根(要能找到 macros/);預設取 model 所在目錄
    variables   覆寫 dbt var,例如 {"target_date": "2026-03-01"}
    source      直接給定原始碼(給「檔案不在磁碟上、來自 GitLab」的情境用)

    任何失敗都收斂成 ok=False 而不丟例外——展開失敗只代表「無法展開」,
    呼叫端應照舊用未展開的內容處理,絕不能當成「掃過且沒問題」。
    """
    model_path = pathlib.Path(model_path)
    root = pathlib.Path(code_root) if code_root else model_path.parent
    try:
        if source is None:
            source = model_path.read_text(encoding="utf-8")
        env, macros, conflicts = build_env(root, variables)
        # 兩次渲染都用同一個 env 設定,差別只在有沒有哨兵。
        clean = env.from_string(source).render()
        marked = env.from_string(_inject_sentinels(source)).render()
    except Exception as e:
        return RenderResult(ok=False, error=f"{type(e).__name__}: {e}")

    n_src = len(source.splitlines())
    n_out = len(clean.splitlines())

    line_map = _map_from_sentinels(marked, clean)
    method = "sentinel"
    if line_map is None:
        line_map = _map_from_difflib(source, clean)
        method = "difflib"

    return RenderResult(
        ok=True,
        sql=clean,
        line_map=_clamp(line_map, n_src, n_out),
        map_method=method,
        macros=macros,
        macro_conflicts=conflicts,
        source_lines=n_src,
        rendered_lines=n_out,
    )
