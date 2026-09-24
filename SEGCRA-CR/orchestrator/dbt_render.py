"""dbt_render — 把 dbt model 展開成可分析的 T-SQL(確定性,不經 LLM)。

前處理的規則檢查與執行驗證吃的是純 SQL,但 repo 裡的 model 是 dbt 樣板:
`{{ ref() }}`、`{{ config() }}`、`{% set %}`,以及會展開成一整段條件的 macro。
不展開就掃,等於 macro 裡的門檻值與 op_code 白名單全是黑箱——規則層看不到
`amount > 1000`,執行驗證也拿不到可跑的 SQL。

入口:
  * `render_model_isolated()` —— **待審的 MR 內容一律走這個**。在獨立子行程中展開,
    逾時強制終止;支援的平台(Linux)另加記憶體上限。
  * `render_model()` —— 同一行程內展開。只給可信內容(本 repo 的範例、測試)使用。
兩者回傳相同的 RenderResult。**ok=False 時呼叫端必須把它當成一個待人工確認的
問題(needs_human),絕不能當成「掃過且沒問題」**:否則攻擊者只要讓樣板刻意展開
失敗,就能讓整份檔案逃過預掃。

流程:
  1. 讀入 <code_root>/<macro_dirs>/**.sql(預設 macros/,對應 dbt 的 macro-paths),
     把每個 macro 註冊成全域函式;所有 macro 檔共用同一個命名空間,跨檔互相呼叫
     與檔案順序無關(與 dbt 相同)。
     dbt 的 `{{ return(x) }}` 是靠丟例外實作的,這裡照做(_Return);否則
     get_config() 只會拿到渲染後的字串而不是 dict,`cfg['inward_codes']` 會炸。
  2. 比照 dbt:先去掉原始檔頭尾空白,再渲染,得到純 SQL。
  3. 建立「渲染行 → 原始行」對應,讓 finding 的行號能貼回原始檔。
     主作法是**哨兵**:在每行原始碼(縮排之後)插一個 SQL 註解標記後再渲染一次,
     從輸出把標記讀回來。標記含每次隨機產生的權杖,MR 內容無法偽造。這是確定性的,
     而且可以自我驗證(去掉標記後必須與乾淨渲染逐字相同)。對不上、或標記插錯位置
     導致那次渲染失敗時,退回 difflib 近似(樣板本身仍照常展開);行號一律加回被去掉
     的開頭行數、夾在檔案範圍內,查不到回 0(= 檔案層留言)。**寧可不指行,也不要
     指錯行**——行內留言貼錯位置是使用者一眼就看到的失誤,比漏報更傷信任。

與 `dbt compile` 逐字一致:輸出以 dbt 官方編譯結果為標準答案做逐字對照
(tests/dbt_reference/、tests/fixtures/dbt_compiled/),涵蓋範例 model 與
針對空白處理、BOM、CRLF、do / 迴圈控制、incremental、source 的探測檔。已比照 dbt:
  * 渲染前去掉原始檔頭尾空白(dbt 讀檔時 strip),不保留檔尾換行
  * 以位元組讀檔再以 UTF-8 解碼(與 dbt 相同;不做換行轉換,換行由 Jinja 統一)
  * 啟用 `do` 與迴圈控制(`break` / `continue`)擴充
  * `execute` 為 True、`is_incremental()` 為 False(與 compile 時相同)
  * `ref()` / `source()` 展開成 `"<database>"."<schema>"."<表>"`,database 由
    呼叫端提供,schema 預設 dbo(正式環境所有表都在單一資料庫的 dbo 底下)。
    執行驗證時沙盒須以同名資料庫承接,這部分由 spec_exec 處理。

資安:這裡渲染的是**待審的 MR 內容,不是我們自己的程式碼**。
  * `SandboxedEnvironment`,且要求 **jinja2 >= 3.1.6**(之前的版本有沙箱逃逸漏洞
    CVE-2024-56326、CVE-2025-27516);版本不符時拒絕展開(fail closed)。
  * **Jinja 環境不掛任何檔案載入器**:macro 檔由我們自己讀進來再交給 Jinja。
    否則 MR 可以用 `{% include 'config/gitlab.env' %}` 把審查機上的檔案(含憑證)
    讀進「展開後的 SQL」,再隨 prompt 或 MR 留言外洩(已實測可讀到)。
    dbt 本身也不支援在 model 裡 include 任意檔案。macro 目錄內的符號連結一律略過。
  * `env_var()` 刻意不提供:它會把審查機的環境變數(可能含密碼)寫進 SQL。
  * macro 不得與 dbt 內建 / Jinja 內建同名(例如自訂 `ref`、`var`):否則 MR 能
    繞過識別字白名單,或蓋掉呼叫端給的變數值(已實測可行)。遇到即拒絕展開。
  * `ref()` / `source()` 的名稱會被拼進 SQL 識別字,一律過白名單(英數與底線)。
    否則審查者在原始碼看到的是無害的 `{{ ref('...') }}`,展開後卻藏著另一段 SQL。
    dbt 本身遇到不存在的 model 會編譯失敗,這裡同樣拒絕。
  * 樣板不得直接輸出函式或物件(例如 `{{ ref }}` 少了括號):其字串表示含記憶體
    位址與內部名稱。
  * 呼叫端傳入的變數每次取用都給複本,樣板無法改到呼叫端的資料;`target` 唯讀。
  * 錯誤訊息不含檔案路徑、不含控制字元(防日誌注入),長度有上限。
  * 資源上限:原始碼、macro 總量、輸出長度,以及 `*` / `+` / `**` 的結果大小。
    這些只擋得住常見的放大手法(`~` 串接、format 寬度等仍可能耗盡記憶體),
    所以不可信內容必須走 `render_model_isolated()`。
  * 本模組不做任何網路、子行程指令、檔案寫入(測試以 import 白名單把關)。

設計底線:**寧可展開失敗,也不要產出看似成功、實則錯誤的 SQL**。
所以未定義的 dbt 內建(StrictUndefined)、沒有預設值又沒被提供的 var、
未設定 database 卻用到 ref/source、`{{ this }}`,一律是硬失敗;絕不猜值。

已知限制:
  * **incremental 分支不會被掃到**:`is_incremental()` 比照 compile 固定為 False,
    `{% if is_incremental() %}` 裡的 SQL 不會出現在輸出中。要審查該分支需另外以
    True 再展開一次(且需支援 `{{ this }}`)。
  * 記憶體上限依賴作業系統(Linux 的 RLIMIT_AS);Windows 上只有逾時與上述上限。
  * `source()` 依正式環境慣例展開(沿用 profile 的 database 與 dbo),未讀取
    sources.yml;若某來源另外指定 schema / database / identifier,結果會與 dbt 不同。
  * `ref()` 以 model 名稱為表名,不驗證被引用的 model 是否存在(沒有 manifest 可查),
    未讀取被引用 model 的 alias,也未套用專案自訂的 `generate_schema_name` /
    `generate_alias_name`;有自訂或指到不存在的 model 時,結果會與 dbt 不同。
    專案以 macro 覆寫 `ref` / `source`(dbt 允許)時,這裡會拒絕展開。
    以上三項不影響規則層判斷(規則檢查的是語法結構,不檢查表名/表是否存在),
    但用到 `ref()`/`source()` 時 `RenderResult.relation_notice` 會帶上提醒,
    呼叫端應呈現給審查者,不要讓表名看起來像是已經過完整驗證。
  * `adapter` 只提供 `dispatch`(依 `adapter` 參數先找 `<轉接器>__x`,再退回
    `default__x`);不讀 `dbt_project.yml` 的 dispatch 搜尋順序設定,也不分辨套件。
    `{{ this }}`、`run_query`、`modules`、`fromjson` 等其餘 dbt 內建未支援,
    用到即 render 失敗。
  * macro 同名時 dbt 用套件命名空間解析,這裡是單一平面命名空間。遇到同名
    只能擇一,結果**可能與 dbt 不同** → 記進 `macro_conflicts` 讓上游看得到。
  * `config.get()` 只讀得到同一份 model 檔內、排在前面的 `config()` 所設定的鍵;
    dbt 還會合併 `dbt_project.yml` 的設定,且不受先後順序影響。讀不到即硬失敗。
  * 載入失敗的 macro 檔會被略過並記進 `macro_problems`,其餘檔案照常可用。
    用到被略過檔案裡的 macro 時,該 model 會因名稱未定義而展開失敗(fail closed)。
"""
import copy
import difflib
import pathlib
import re
import secrets
import types
from dataclasses import dataclass, field

import jinja2
from jinja2 import StrictUndefined, Undefined
from jinja2.defaults import DEFAULT_NAMESPACE
from jinja2.ext import Extension
from jinja2.sandbox import SandboxedEnvironment

from .isolation import run_isolated

# 沙箱逃逸已修補的最低版本:3.1.5 修 CVE-2024-56326,3.1.6 修 CVE-2025-27516
MIN_JINJA_VERSION = (3, 1, 6)

# 資源上限(正常的 dbt model 與 macro 遠小於這些值)
MAX_SOURCE_CHARS = 1_000_000        # 單一 model 原始碼
MAX_MACRO_FILES = 2_000
MAX_MACRO_CHARS = 5_000_000         # macros/ 底下所有檔案合計
MAX_OUTPUT_CHARS = 5_000_000        # 展開結果
MAX_INT_BITS = 100_000              # 整數運算結果的位元數上限(約 3 萬位十進位)
MAX_ERROR_CHARS = 500
DIFFLIB_MAX_CELLS = 25_000_000      # difflib 為平方時間,行數乘積超過就不對行號

DEFAULT_ISOLATED_TIMEOUT_S = 30.0
DEFAULT_ISOLATED_MEMORY_MB = 1024

# 會吃掉「前面空白」的標記:行首是它們時不能插哨兵,否則會擋住空白控制,
# 讓帶哨兵的渲染與乾淨渲染不一致。
_LEFT_TRIM_TAGS = ("{%-", "{{-", "{#-")
_TAG_PAIRS = {"{{": "}}", "{%": "%}", "{#": "#}"}

# 識別字白名單:會被拼進 SQL 的資料庫/綱要/表名只接受英數與底線(擋注入)。
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 與 dbt-core 的 Jinja 環境相同的擴充
_JINJA_EXTENSIONS = ["jinja2.ext.do", "jinja2.ext.loopcontrols"]

# 正式環境所有表都在單一資料庫的 dbo 底下
DEFAULT_SCHEMA = "dbo"
# 正式環境的 dbt 轉接器;決定 adapter.dispatch 先找哪個前綴的實作
DEFAULT_ADAPTER = "sqlserver"
# dbt 的 macro-paths 預設值(專案可自訂,由呼叫端傳入)
DEFAULT_MACRO_DIRS = ("macros",)

# macro 不得使用的名稱:dbt 內建(含刻意不提供的 this / env_var / builtins)與 Jinja 內建。
RESERVED_NAMES = frozenset({
    "ref", "source", "config", "return", "var", "execute", "is_incremental",
    "invocation_id", "target", "this", "env_var", "builtins",
}) | frozenset(DEFAULT_NAMESPACE)

# 刻意留空:不預設任何 dbt var。
# dbt 的 var() 只看專案/CLI 變數,看不到樣板裡的 `{% set %}`;沒設定又沒預設值時
# dbt 會編譯失敗。這裡若擅自給預設,等於用一個猜來的值掩蓋 dbt 會報錯的情況,
# 而且渲染出來的 SQL 會帶著錯的日期跑進執行驗證。變數一律由呼叫端明確傳入。
_DEFAULT_VARS: dict = {}

_MISSING = object()
_PLAIN_SCALARS = (str, int, float, bool, type(None))

# ref()/source() 展開出的表名有三個已知落差,PR review 要求接線時要讓審查者看得到,
# 不能只寫在模組文件裡沒人會翻:
#   1. 不驗證被引用的 model 是否真的存在(沒有 manifest 可查)
#   2. source() 不讀 sources.yml,固定套用正式環境慣例(profile 的 database、dbo)
#   3. ref() 不套用被引用 model 的 alias,也不套用專案自訂的 generate_schema_name
# 三者都不影響規則層判斷(R001/R002/R003 等檢查的是語法結構,不檢查表名/表是否存在),
# 但展開出的表名可能與 dbt compile 的正式結果不同,呼叫端應標明「未經完整驗證」。
_RELATION_NOTICE = (
    "此檔用到 ref()/source():展開出的表名不保證與 dbt compile 逐字相同"
    "(不驗證被引用的 model 是否存在、不讀取 sources.yml、不套用 alias 與"
    "自訂 generate_schema_name)。若專案有用到這些進階寫法,表名可能有落差,"
    "建議人工核對。")


class DbtRenderError(Exception):
    """展開失敗。呼叫端應視為待人工確認的問題,不可當成「掃過且沒問題」。"""


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


def _jinja_version_ok(version: str) -> bool:
    parts = []
    for piece in version.split(".")[:3]:
        m = re.match(r"\d+", piece)
        parts.append(int(m.group()) if m else 0)
    parts += [0] * (3 - len(parts))
    return tuple(parts) >= MIN_JINJA_VERSION


def _is_plain(value, depth: int = 0) -> bool:
    """只含基本型別的值(可安全輸出)。函式、物件的字串表示含記憶體位址。"""
    if isinstance(value, _PLAIN_SCALARS):
        return True
    if depth >= 20:
        return False
    if isinstance(value, (list, tuple)):
        return all(_is_plain(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(_is_plain(k, depth + 1) and _is_plain(v, depth + 1)
                   for k, v in value.items())
    return False


def _finalize(value):
    if isinstance(value, Undefined):
        return value            # 交給 StrictUndefined 大聲失敗
    if not _is_plain(value):
        raise DbtRenderError(
            f"樣板直接輸出了 {type(value).__name__} 物件(例如 {{{{ ref }}}} 少了括號)。"
            f"不輸出,以免洩漏記憶體位址與內部名稱。")
    return value


def _too_big(value) -> bool:
    return isinstance(value, (str, list, tuple)) and len(value) > MAX_OUTPUT_CHARS


class _DbtBlocks(Extension):
    """dbt 自己加上去的區塊標籤,解析掉但不輸出任何內容。

    `{% materialization %}`(自訂實體化)、`{% test %}`(generic test)、
    `{% docs %}`、`{% snapshot %}` 都是 dbt 以 Jinja 擴充加的語法,Jinja 本身
    不認得。少了它們,macro 目錄裡只要有**一個**檔案用到(這兩者在真實專案都
    很常見),整包 macro 就解析失敗,連帶讓**所有** model 都展不開。

    dbt 自己也是以擴充處理這幾個標籤,而且同樣不會把內容編進 model 的 SQL,
    所以「解析掉並丟棄」與 dbt 的輸出一致。

    **只在載入 macro 檔時生效**:model 檔裡出現這些標籤並不是合法的 dbt 寫法,
    默默把那段丟掉會讓我們產出「比 dbt 少一段」的 SQL,還回報展開成功。
    所以 model 階段一律照 Jinja 原本的行為當成未知標籤,硬失敗。
    """

    tags = {"materialization", "test", "docs", "snapshot"}

    def parse(self, parser):
        tag = parser.stream.current.value
        if not getattr(self.environment, "segcra_macro_phase", False):
            raise jinja2.TemplateSyntaxError(
                f"Encountered unknown tag '{tag}'.", parser.stream.current.lineno)
        next(parser.stream)
        while parser.stream.current.type != "block_end":   # 標籤上的參數一律略過
            next(parser.stream)
        parser.parse_statements((f"name:end{tag}",), drop_needle=True)
        return []


class _DbtSandbox(SandboxedEnvironment):
    """在 Jinja 沙箱之上,再擋住以 * / + / ** 做出的超大結果。

    先估算結果大小再計算——等算出來才檢查就來不及了(大整數次方本身就會卡住 CPU)。
    整數以位元數估算:只限制指數不夠,`((9**256)**256)**256` 每一步指數都很小,
    結果卻有上億位。
    """

    intercepted_binops = frozenset({"*", "+", "**"})

    def call_binop(self, context, operator, left, right):
        both_int = isinstance(left, int) and isinstance(right, int)
        if operator == "**":
            if both_int and abs(left) > 1 and right > 0 and (
                    right > MAX_INT_BITS or abs(left).bit_length() * right > MAX_INT_BITS):
                raise DbtRenderError(f"次方運算結果超過 {MAX_INT_BITS} 位元上限")
        elif operator == "*":
            if both_int and left.bit_length() + right.bit_length() > MAX_INT_BITS:
                raise DbtRenderError(f"乘法結果超過 {MAX_INT_BITS} 位元上限")
            for seq, n in ((left, right), (right, left)):
                if (isinstance(seq, (str, list, tuple)) and isinstance(n, int)
                        and len(seq) * n > MAX_OUTPUT_CHARS):
                    raise DbtRenderError(f"重複運算的結果超過 {MAX_OUTPUT_CHARS} 字元上限")
        elif operator == "+":
            if (isinstance(left, (str, list, tuple)) and isinstance(right, type(left))
                    and len(left) + len(right) > MAX_OUTPUT_CHARS):
                raise DbtRenderError(f"串接結果超過 {MAX_OUTPUT_CHARS} 字元上限")
        result = super().call_binop(context, operator, left, right)
        if _too_big(result):
            raise DbtRenderError(f"運算結果超過 {MAX_OUTPUT_CHARS} 字元上限")
        return result


def _make_var(variables: dict):
    """dbt 的 var()。找不到又沒給預設值時**硬失敗**,不回 None。

    回 None 的話 Jinja 會把它渲染成字串 "None" 寫進 SQL(例如
    `WHERE d = None`),而且整個流程還會回報展開成功——這種錯最難抓。
    dbt 本身在這情況也是編譯失敗,行為一致。

    每次回傳複本:樣板(例如 `{% do var('codes').append(...) %}`)改不到呼叫端的
    資料,也不會因為哨兵那次渲染看到被改過的值而讓行號對應失準。
    """

    def var(name, default=_MISSING):
        if name in variables:
            return copy.deepcopy(variables[name])
        if default is _MISSING:
            raise DbtRenderError(
                f"var('{name}') 沒有預設值,呼叫端也沒有提供。"
                f"dbt 在這種情況會編譯失敗,這裡同樣不猜值。")
        return default

    return var


class _Config:
    """dbt 的 `config`:`{{ config(...) }}` 設定,`config.get(...)` 讀回。

    只認得**同一份 model 檔內、而且排在前面**的 config() 所設定的鍵。dbt 還會
    合併 dbt_project.yml 的 `models:` 設定(本模組不讀取),且它在解析期就取得
    全檔的 config,不受先後順序影響。所以讀不到的鍵一律硬失敗,連呼叫端給了
    預設值也不例外——用預設值頂替會掩蓋「dbt 其實有值而且不一樣」的情況,
    產出看似成功、實則與 dbt 不同的 SQL。
    """

    def __init__(self):
        self._values: dict = {}

    def __call__(self, *args, **kwargs):
        # dbt 允許 config(materialized='table') 與 config({'materialized': 'table'})
        for a in args:
            if isinstance(a, dict):
                self._values.update(a)
        self._values.update(kwargs)
        return ""                                   # config 不產生 SQL

    def get(self, name, default=_MISSING):
        if name in self._values:
            return copy.deepcopy(self._values[name])
        raise DbtRenderError(
            f"config.get('{name}') 讀不到值:這份檔案裡沒有(或還沒有)以 config() "
            f"設定過它。dbt 會再合併 dbt_project.yml 的設定,本模組不讀取,"
            f"因此不猜值。")

    def require(self, name):
        return self.get(name)


def _check_ident(kind: str, value) -> str:
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        shown = repr(value)
        if len(shown) > 60:
            shown = shown[:60] + "…"
        raise DbtRenderError(
            f"{kind}名稱不合法:{shown}。只接受英數與底線——"
            f"這個名稱會被拼進 SQL 識別字,不合法的字元可能造成注入。")
    return value


def _make_relations(database: str | None, schema: str, usage: dict):
    """dbt 的 ref() / source(),比照正式環境展開成 "<database>"."<schema>"."<表>"。

    usage 是呼叫端給的共用字典,呼叫到 ref()/source() 就記一筆,讓呼叫端知道
    這份 model 是否用了這兩個「不保證與 dbt 逐字相同」的展開(見 _RELATION_NOTICE)。
    """

    def relation(name) -> str:
        usage["used"] = True
        if database is None:
            raise DbtRenderError(
                "未設定 database,無法展開 ref()/source()。dbt 由 profile 決定資料庫名稱,"
                "這裡同樣不猜值——請由呼叫端傳入 database。")
        return f'"{database}"."{schema}"."{_check_ident("表", name)}"'

    def ref(*args, **kwargs):
        # ref('model') 或 ref('package', 'model')
        if len(args) not in (1, 2):
            raise DbtRenderError(f"ref() 參數個數不合法:{len(args)} 個")
        if kwargs:
            # dbt 的 ref('model', version=2) 指向的是另一個關聯(model_name_v2)。
            # 我們沒有 manifest 可以解析版本,忽略它會靜靜產出錯的表名 → 拒絕展開。
            names = ", ".join(sorted(kwargs))
            raise DbtRenderError(
                f"ref() 的關鍵字參數({names})會改變指向的關聯,本模組無法解析,拒絕展開。")
        for a in args[:-1]:
            _check_ident("套件", a)
        return relation(args[-1])

    def source(source_name, table_name):
        _check_ident("來源", source_name)
        return relation(table_name)

    return ref, source


class _Adapter:
    """只提供 dispatch 的 adapter 樁。

    dbt 的 `adapter.dispatch('x')` 會依轉接器挑實作:先找 `<轉接器>__x`,
    再退回 `default__x`。這裡照同一順序在已載入的 macro 裡找;找不到就報錯,
    不猜。其餘 adapter 方法(get_relation、run_query…)刻意不提供:那些要連
    資料庫,展開階段本來就不該有,未定義會大聲失敗。
    """

    def __init__(self, macros: dict, adapter: str):
        self._macros = macros
        self._adapter = adapter

    def dispatch(self, macro_name, macro_namespace=None):
        _check_ident("macro", macro_name)
        if macro_namespace is not None:
            # 單一平面命名空間找不到「指定套件的同名 macro」,擇一的結果可能與 dbt 不同
            _check_ident("套件", macro_namespace)
        for candidate in (f"{self._adapter}__{macro_name}", f"default__{macro_name}"):
            found = self._macros.get(candidate)
            if callable(found):
                return found
        raise DbtRenderError(
            f"adapter.dispatch('{macro_name}') 找不到實作:"
            f"需要 {self._adapter}__{macro_name} 或 default__{macro_name}。")


@dataclass
class RenderResult:
    """展開結果。

    ok=False 時 sql 為空字串。呼叫端必須把展開失敗當成待人工確認的問題
    (needs_human),不可當成「掃過且沒問題」,也不可自動放行。
    map_method 為 "none" 時 line_map 為空,所有 finding 都應標在檔案層(line 0)。
    """

    ok: bool
    sql: str = ""
    line_map: dict[int, int] = field(default_factory=dict)
    map_method: str = "none"          # sentinel | difflib | none
    macros: list[str] = field(default_factory=list)
    macro_conflicts: list[str] = field(default_factory=list)
    # 載入失敗而被略過的 macro 檔(路徑:原因)。ok 仍可能為 True——只用到其他
    # macro 的 model 照常展開;呼叫到被略過檔案裡的 macro 則會展開失敗。
    macro_problems: list[str] = field(default_factory=list)
    source_lines: int = 0
    rendered_lines: int = 0
    error: str | None = None
    # ok=True 且這份 model 用到 ref()/source() 時才會有值(見 _RELATION_NOTICE)。
    # 展開出的表名不保證與 dbt 逐字相同——見下方三個已知限制;呼叫端應在報告中
    # 一併呈現,而不是讓審查者誤以為表名已經過完整驗證。
    relation_notice: str | None = None

    def src_line(self, rendered_line: int) -> int:
        """渲染行(1-based)→ 原始行(1-based)。對應不到回 0 = 檔案層留言。"""
        return self.line_map.get(rendered_line, 0)


def _safe_error(e: BaseException) -> str:
    """錯誤訊息可能被貼進 MR 留言或寫進日誌:不含路徑、不含控制字元、有長度上限。"""
    if isinstance(e, _Return):
        text = "DbtRenderError: return() 只能在 macro 內使用"
    elif isinstance(e, MemoryError):
        text = "MemoryError: 展開時記憶體不足(超過上限),已中止"
    elif isinstance(e, OSError):
        text = f"{type(e).__name__}: 無法讀取檔案({e.strerror or '未知原因'})"
    else:
        text = f"{type(e).__name__}: {e}"
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:MAX_ERROR_CHARS]


def is_dbt_template(text: str) -> bool:
    """這份內容有沒有 Jinja 標記——沒有就不必走展開。

    只看開頭符號、不要求成對:未閉合的標記一樣要交給展開(會以語法錯誤失敗、
    被標為待確認),不可因為「看起來不是樣板」而略過。刻意不用正規表達式——
    `\\{\\{.*?\\}\\}` 在大量未閉合的 `{{` 上是平方時間,可被特製輸入拖垮。
    """
    return bool(text) and any(opener in text for opener in _TAG_PAIRS)


def _count_lines(text: str) -> int:
    """以 \\n 計算行數(與 GitLab 行號、哨兵編號一致);檔尾換行不算多一行。

    不用 splitlines():它會把 \\x0c、\\u2028 等字元也當成換行,與 GitLab 的行號不一致。
    """
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _render_capped(template, limit: int) -> str:
    """逐段產生輸出,超過上限立刻中止(不先把整份超大輸出做出來)。"""
    parts, size = [], 0
    for chunk in template.generate():
        size += len(chunk)
        if size > limit:
            raise DbtRenderError(f"展開結果超過 {limit} 字元上限")
        parts.append(chunk)
    return "".join(parts)


# ------------------------------------------------------- Jinja 環境與 dbt 樁
def build_env(code_root=None, variables: dict | None = None,
              database: str | None = None, schema: str = DEFAULT_SCHEMA,
              macro_dirs=DEFAULT_MACRO_DIRS, adapter: str = DEFAULT_ADAPTER):
    """建好含 dbt 樁與 macro 的 Environment。code_root 為 None 時不載入任何 macro。

    macro_dirs  專案的 macro 目錄(dbt 的 macro-paths;預設 macros/)
    adapter     dbt 轉接器名稱,決定 adapter.dispatch 先找哪個前綴的實作

    回傳 (env, macro 名稱清單, 同名衝突清單)。
    """
    if not _jinja_version_ok(jinja2.__version__):
        raise DbtRenderError(
            f"jinja2 {jinja2.__version__} 有已知的沙箱逃逸漏洞,需要 >= "
            f"{'.'.join(map(str, MIN_JINJA_VERSION))},拒絕展開。")
    if database is not None:
        _check_ident("資料庫", database)
    _check_ident("綱要", schema)
    variables = {**_DEFAULT_VARS, **(variables or {})}
    # 必須是沙箱,而且**不掛 loader**(理由見模組 docstring「資安」)。
    # 其餘設定比照 dbt-core 的 Jinja 環境,輸出才能與 dbt compile 逐字一致。
    env = _DbtSandbox(
        undefined=StrictUndefined,   # 沒樁到的 dbt 內建要大聲失敗,不可悄悄變空字串
        extensions=[*_JINJA_EXTENSIONS, _DbtBlocks],
        finalize=_finalize,
    )

    # 是否用到 ref()/source() 記在這裡,render_model() 展開成功後讀出來決定
    # relation_notice 要不要帶上。掛在 env 上是既有作法(見 segcra_macro_phase),
    # 不改 build_env() 的回傳簽章,呼叫端(含既有測試)不受影響。
    env.segcra_relation_usage = {"used": False}
    ref, source = _make_relations(database, schema, env.segcra_relation_usage)
    env.globals["ref"] = ref
    env.globals["source"] = source
    env.globals["config"] = _Config()
    env.globals["return"] = _return
    env.globals["var"] = _make_var(variables)
    env.globals["execute"] = True                   # compile 期為 True
    env.globals["is_incremental"] = lambda: False   # compile 期一律視為全量
    # dbt 每次執行都會產生新的 invocation_id;這裡是固定值,樣板若真的輸出它,
    # 結果會與 dbt compile 不同(已知限制,見模組 docstring)。
    env.globals["invocation_id"] = "segcra00"
    _check_ident("轉接器", adapter)
    # macro 在這之後才載入,dispatch 是呼叫時才查表,所以拿得到全部 macro
    env.globals["adapter"] = _Adapter(env.globals, adapter)
    # 沒給 database 時,target.database 不可展開成 "None" 這種看似成功的錯誤結果:
    # 給未定義值,讓用到它的樣板大聲失敗(與 `this` / `env_var` 同一個原則)。
    env.globals["target"] = types.MappingProxyType({
        "database": database if database is not None else StrictUndefined(name="target.database"),
        "schema": schema,
    })
    # `this` 與 `env_var` 刻意不定義:前者給錯值會靜靜產出錯的 SQL,後者會把審查機
    # 的環境變數寫進 SQL。未定義則由 StrictUndefined 大聲失敗。

    macros: list[str] = []
    conflicts: list[str] = []
    problems: list[str] = []
    if code_root is None:
        return env, macros, conflicts, problems

    root = pathlib.Path(code_root)
    seen: dict[str, str] = {}
    files = []
    for name in ((macro_dirs,) if isinstance(macro_dirs, str) else macro_dirs):
        # 目錄設定由呼叫端給,不接受跳出專案或指向別處的路徑。判斷不依平台:
        # Windows 上 `專案 / "/etc"` 會跳到磁碟根目錄,`is_absolute()` 卻是 False。
        posix = name.replace("\\", "/") if isinstance(name, str) else ""
        # 空字串 / None 會變成空的路徑段,一併被下面的檢查擋下
        if (posix.startswith("/") or ":" in posix
                or any(part in ("", ".", "..") for part in posix.split("/"))):
            raise DbtRenderError(f"macro 目錄設定不合法:{name!r}")
        macro_dir = root / posix
        if not macro_dir.is_dir() or macro_dir.is_symlink():
            continue
        files += [f for f in sorted(macro_dir.rglob("*.sql"))
                  if not f.is_symlink() and f.is_file()]   # 符號連結可能指向任意檔案
    if not files:
        return env, macros, conflicts, problems
    if len(files) > MAX_MACRO_FILES:
        raise DbtRenderError(f"macro 檔案數超過 {MAX_MACRO_FILES} 個上限")
    total = 0
    # 所有 macro 檔共用同一個命名空間:dbt 解析 macro 與檔案順序無關,逐檔各自
    # 展開會讓「呼叫排在後面檔案的 macro」找不到名稱(跨檔互相呼叫在真實專案很常見)。
    # 名稱在 macro 被呼叫時才解析,所以之後載入的 macro 也找得到。
    shared: dict = dict(env.globals)
    # dbt 專用的區塊標籤只在這個階段解析得掉(理由見 _DbtBlocks)
    env.segcra_macro_phase = True
    for f in files:
        rel = f.relative_to(root).as_posix()
        try:
            text = f.read_bytes().decode("utf-8")
        except UnicodeDecodeError as e:
            problems.append(f"{rel}:{_safe_error(e)}")
            continue
        total += len(text)
        if total > MAX_MACRO_CHARS:
            raise DbtRenderError(f"macro 檔案合計超過 {MAX_MACRO_CHARS} 字元上限")
        try:
            # 模組層的 {% set %} 只寫進該模組自己的名稱空間,不會汙染 shared(已驗證);
            # 進入 shared 的只有下面逐一檢查過的 macro。
            module = env.from_string(text).make_module(vars=shared, shared=True)
        except DbtRenderError:
            raise                       # 資源上限等我們自己的硬失敗照舊往上拋
        except Exception as e:
            # 單一 macro 檔壞掉(語法錯誤、模組層執行失敗)不該讓整個專案都展不開:
            # 略過它並記錄。真的需要它的 model 仍會因為名稱未定義而大聲失敗,
            # 不會靜靜產出少了那段邏輯的 SQL。
            problems.append(f"{rel}:{_safe_error(e)}")
            continue
        for name in dir(module):
            if name.startswith("_"):
                continue
            obj = getattr(module, name)
            if not callable(obj):
                continue
            if name in RESERVED_NAMES:
                raise DbtRenderError(
                    f"macro 名稱 {name!r}({rel})與 dbt / Jinja 內建同名。覆寫內建可繞過"
                    f"識別字檢查或蓋掉呼叫端的變數,拒絕展開。")
            if name in seen:
                # dbt 會用套件命名空間分辨,我們只有單一平面命名空間 →
                # 擇一的結果可能與 dbt 不同,必須讓上游看見。
                conflicts.append(f"{name}: {seen[name]} / {rel}")
            else:
                macros.append(name)
            seen[name] = rel
            shared[name] = env.globals[name] = _wrap(obj)
    env.segcra_macro_phase = False
    return env, sorted(macros), conflicts, problems


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
        closer = _TAG_PAIRS.get(src[i:i + 2])
        i += 2 if closer else 1
    return inside


def _sentinel(token: str, n: int) -> str:
    return f"/*__SEGCRA_{token}_L{n}__*/"


def _sentinel_re(token: str) -> re.Pattern:
    return re.compile(r"/\*__SEGCRA_" + re.escape(token) + r"_L(\d+)__\*/")


def _inject_sentinels(src: str, token: str) -> str:
    """在每行縮排之後插入哨兵。下列行不插:

    * 行首在 Jinja 標記內部(會破壞語法)
    * 純空白行(上一行結尾的 `-%}` 會吃掉它;插了就擋住空白控制)
    * 以 `{%-` / `{{-` / `{#-` 開頭(它會吃掉前面的空白;插了就擋住空白控制)
    插在縮排之後而非行首,是因為上一行的 `-%}` 會吃到本行第一個非空白字元為止。
    不插的行,其輸出沿用上一個哨兵的行號。
    """
    inside = _lines_inside_tag(src)
    out = []
    for idx, line in enumerate(src.split("\n")):
        body = line.lstrip()
        if idx in inside or not body or body.startswith(_LEFT_TRIM_TAGS):
            out.append(line)
        else:
            indent = line[:len(line) - len(body)]
            out.append(indent + _sentinel(token, idx + 1) + body)
    return "\n".join(out)


def _map_from_sentinels(marked: str, clean: str, token: str) -> dict[int, int] | None:
    """哨兵法建圖。去掉哨兵後必須與乾淨渲染逐字相同,否則視為失敗回 None。

    這個自我驗證是整套的安全閥:對不上就代表哨兵干擾了渲染(例如標記被夾進
    字串常量),此時寧可退回近似法,也不要交出一張看似完整、實則錯位的圖。
    """
    pattern = _sentinel_re(token)
    stripped: list[str] = []
    mapping: dict[int, int] = {}
    cur = 0
    for line in marked.split("\n"):
        hits = pattern.findall(line)
        if hits:
            cur = int(hits[-1])
        stripped.append(pattern.sub("", line))
        mapping[len(stripped)] = cur
    if "\n".join(stripped) != clean:
        return None
    return mapping


def _mask_tags(text: str) -> str:
    """去掉 Jinja 標記,只留固定文字。線性時間(不用正規表達式,理由同 is_dbt_template)。"""
    out: list[str] = []
    closer: str | None = None
    start, i, n = 0, 0, len(text)
    while i < n:
        if closer:
            if text.startswith(closer, i):
                i += 2
                closer, start = None, i
            else:
                i += 1
            continue
        closer = _TAG_PAIRS.get(text[i:i + 2])
        if closer:
            out.append(text[start:i])
            i += 2
        else:
            i += 1
    if closer is None:
        out.append(text[start:])
    return "".join(out)


def _map_from_difflib(src: str, clean: str) -> dict[int, int]:
    """退路:把樣板標記遮掉後對齊固定文字。近似,只在哨兵失效時使用。

    difflib 為平方時間:行數乘積超過上限就不對行號(回空對應 = 全部標在檔案層)。
    """
    src_lines, out_lines = src.split("\n"), clean.split("\n")
    if len(src_lines) * len(out_lines) > DIFFLIB_MAX_CELLS:
        return {}
    a = [_mask_tags(l).strip() for l in src_lines]
    b = [l.strip() for l in out_lines]
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

    `split("\\n")` 會因結尾換行多出一個空元素(渲染結果可能以換行結尾);
    difflib 在檔尾對齊時也可能算出「第 N+1 行」。不夾住的話,GitLab 會貼歪或拒收。
    """
    return {k: min(v, n_src) for k, v in line_map.items() if 1 <= k <= n_out}


# --------------------------------------------------------------------- 主入口
def render_model(model_path, code_root=None, variables: dict | None = None,
                 source: str | None = None, database: str | None = None,
                 schema: str = DEFAULT_SCHEMA, macro_dirs=DEFAULT_MACRO_DIRS,
                 adapter: str = DEFAULT_ADAPTER) -> RenderResult:
    """在同一行程內展開一份 dbt model。**待審的 MR 內容請改用 render_model_isolated()。**

    model_path  model 檔路徑(相對 code_root 或絕對路徑皆可)
    code_root   dbt 專案根(要能找到 macro 目錄)。未指定時:從磁碟讀檔則取 model 所在
                目錄;給定 source= 則不載入任何 macro(不猜目錄)
    variables   dbt var,例如 {"target_date": "2026-03-01"}
    source      直接給定原始碼(給「檔案不在磁碟上、來自 GitLab」的情境用)
    database    ref()/source() 展開用的資料庫名;model 有用到 ref/source 時必填
    schema      ref()/source() 展開用的綱要名,預設 dbo
    macro_dirs  macro 目錄(dbt 的 macro-paths;專案有自訂時由呼叫端傳入)
    adapter     dbt 轉接器名稱,決定 adapter.dispatch 先找哪個前綴的實作

    任何失敗都收斂成 ok=False 而不丟例外。
    """
    model_path = pathlib.Path(model_path)
    if code_root is not None:
        root = pathlib.Path(code_root)
    elif source is None:
        root = model_path.parent
    else:
        root = None
    try:
        if source is None:
            # 與 dbt 相同:位元組解碼,不做換行轉換(換行由 Jinja 統一成 \n)
            source = model_path.read_bytes().decode("utf-8")
        if len(source) > MAX_SOURCE_CHARS:
            raise DbtRenderError(f"原始碼超過 {MAX_SOURCE_CHARS} 字元上限")
        env, macros, conflicts, problems = build_env(root, variables, database, schema,
                                                     macro_dirs, adapter)
        # dbt 讀檔時會去掉頭尾空白再渲染(開頭空行不會出現在編譯結果裡)
        body = source.strip()
        clean = _render_capped(env.from_string(body), MAX_OUTPUT_CHARS)
    except Exception as e:
        return RenderResult(ok=False, error=_safe_error(e))

    # 被去掉的開頭空白佔了幾行:哨兵與 difflib 都以 body 編行號,要加回來才是原始檔行號
    offset = source[:len(source) - len(source.lstrip())].count("\n")
    n_src = _count_lines(source)
    n_out = _count_lines(clean)

    # 哨兵渲染與乾淨渲染用同一個 env,差別只在有沒有標記。它只負責行號對應:
    # 標記插錯位置(例如表達式中的字串含 "}}",掃描器誤判標記已結束)可能讓這次
    # 渲染語法錯誤——那不代表樣板本身壞了,退回 difflib 即可,不可讓整份檔案展開失敗。
    # 權杖每次隨機產生:MR 內容裡就算寫了長得像哨兵的註解,也無法冒充。
    token = secrets.token_hex(8)
    try:
        marked = _render_capped(env.from_string(_inject_sentinels(body, token)),
                                MAX_OUTPUT_CHARS * 2)
        line_map = _map_from_sentinels(marked, clean, token)
    except Exception:
        line_map = None
    method = "sentinel"
    if line_map is None:
        line_map = _map_from_difflib(body, clean)
        method = "difflib" if line_map else "none"
    line_map = {k: (v + offset if v else 0) for k, v in line_map.items()}

    return RenderResult(
        ok=True,
        sql=clean,
        line_map=_clamp(line_map, n_src, n_out),
        map_method=method,
        macros=macros,
        macro_conflicts=conflicts,
        macro_problems=problems,
        source_lines=n_src,
        rendered_lines=n_out,
        relation_notice=(_RELATION_NOTICE if env.segcra_relation_usage["used"] else None),
    )


# ------------------------------------------------------------------ 隔離展開
def _render_failure(message: str) -> RenderResult:
    return RenderResult(ok=False, error=message)


def render_model_isolated(model_path, *, timeout_s: float = DEFAULT_ISOLATED_TIMEOUT_S,
                          max_memory_mb: int = DEFAULT_ISOLATED_MEMORY_MB,
                          **kwargs) -> RenderResult:
    """在獨立子行程中展開(參數同 render_model)。**待審的 MR 內容一律走這個。**

    Jinja 沙箱防得了任意程式執行,防不了「一個跑不完的迴圈」或「一個吃光記憶體的
    字串」。這裡用行程邊界兜底(見 isolation.run_isolated):超過 timeout_s 秒強制
    終止;Linux 上另以 RLIMIT_AS 限制記憶體。任何異常一律收斂成 ok=False。

    呼叫端程式的進入點需有 `if __name__ == "__main__":` 保護(multiprocessing spawn 的要求)。
    """
    result = run_isolated(render_model, (model_path,), kwargs, timeout_s=timeout_s,
                          max_memory_mb=max_memory_mb, on_failure=_render_failure)
    if not isinstance(result, RenderResult):
        return _render_failure("DbtRenderError: 展開子行程回傳了非預期的資料")
    return result
