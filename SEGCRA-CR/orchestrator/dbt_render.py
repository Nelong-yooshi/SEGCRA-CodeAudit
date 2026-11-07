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
    (白名單只擋名稱格式;被引用的 model 是否存在**不**驗證,見下方已知限制。)
  * 樣板不得直接輸出函式或物件(例如 `{{ ref }}` 少了括號):其字串表示含記憶體
    位址與內部名稱。
  * 展開器跟真正執行時分得出來的地方,樣板可以拿來把語句藏在只有真正執行時才會進的分支:
    `invocation_id` 每份審查不同、同一份審查裡共用(dbt 是一次執行一個,見 _REVIEW_INVOCATION_ID);
    `this` / `ref()` / `source()` 在 dbt 是 Relation 物件,這裡是字串子類別(`_Relation`),型別
    判斷的結果與有哪些屬性都不一定一致,所以對它們做 `defined` / `undefined` 以外的 `is` 判斷、取
    沒有照實模擬的屬性或索引,一律展開失敗(見 _guard_relation_tests、_guard_relation_access)。
  * 呼叫端傳入的變數每次取用都給複本,樣板無法改到呼叫端的資料;`target` 唯讀。
  * 錯誤訊息不含檔案路徑、不含控制字元(防日誌注入),長度有上限。
  * 資源上限:原始碼、macro 總量、輸出長度,以及 `*` / `+` / `**` 的結果大小。
    這些只擋得住常見的放大手法(`~` 串接、format 寬度等仍可能耗盡記憶體),
    所以不可信內容必須走 `render_model_isolated()`。
  * 本模組不做任何網路、子行程指令、檔案寫入(測試以 import 白名單把關)。

設計底線:**寧可展開失敗,也不要產出看似成功、實則錯誤的 SQL**。
所以未定義的 dbt 內建(StrictUndefined)、沒有預設值又沒被提供的 var、
未設定 database 卻用到 ref/source,一律是硬失敗;絕不猜值。`{{ this }}` 跟 dbt 一樣兩種模式
都提供(這個 model 自己的表);沒有 database 或 model 名稱不合法時仍然算有定義(`this is defined`
為 True),但任何使用都失敗(見 _UnavailableRelation)。

已知限制:
  * `is_incremental()` 預設比照 compile 回 False,`{% if is_incremental() %}` 裡的 SQL 不在
    輸出裡。呼叫端可以再以 incremental=True 展開一次取得增量分支;`RenderResult.incremental_checked`
    說明樣板(含它呼叫的 macro)有沒有呼叫 `is_incremental()`,沒有就不必展開第二次。
    `{{ this }}` 是這個 model 自己的表名,規則與 ref() 相同(見 _make_relations)。
  * 記憶體上限依賴作業系統(Linux 的 RLIMIT_AS);Windows 上只有逾時與上述上限。
  * `ref()` / `source()` 的精確度取決於呼叫端有沒有給 `project`(DbtProjectInfo,
    由 dbt_relations.project_info_from_files() 從專案檔案整理):
      - **沒給**:不驗證被引用的 model 是否存在,source() 固定套用正式環境慣例
        (database 與 dbo),不讀 sources.yml;用到就帶 `relation_notice`
      - **有給**:找得到的照 dbt 的規則展開(source 讀 sources.yml 的 schema /
        identifier);找不到而且能確定不存在就展開失敗(dbt compile 一樣會失敗);
        專案用了套件、缺 seed / snapshot 名單、有 alias / 自訂 schema /
        generate_*_name / 樣板值等**只偵測、不模擬**,這些情況才帶 `relation_notice`
    資料庫名一律用呼叫端給的 database(約定的假名),不採用 sources.yml 宣告的資料庫。
    專案以 macro 覆寫 `ref` / `source`(dbt 允許)時,這裡會拒絕展開。
    表名落差不影響規則層判斷(規則檢查的是語法結構,不檢查表名),但呼叫端應呈現
    `relation_notice`,不要讓表名看起來像是已經過完整驗證。
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
import contextvars
import copy
import difflib
import pathlib
import re
import secrets
import types
from dataclasses import dataclass, field

import jinja2
from jinja2 import StrictUndefined, Undefined, nodes, pass_environment
from jinja2.defaults import DEFAULT_NAMESPACE
from jinja2.exceptions import UndefinedError
from jinja2.ext import Extension
from jinja2.filters import do_attr
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


def _is_list(value) -> bool:
    return isinstance(value, list)


def _as_is(value):
    return value


# dbt 加的濾器,照 dbt_common 的 TEXT_FILTERS(compile 展開 SQL 時用的那一組):
# as_text / as_bool / as_native / as_number 原樣回傳,is_list 判斷是不是 list。
# 少了它們,用到的 macro 與 model 會被當成展開器不支援,正常的寫法每次都要人看。
_DBT_TEXT_FILTERS = {
    "as_text": _as_is,
    "as_bool": _as_is,
    "as_native": _as_is,
    "as_number": _as_is,
    "is_list": _is_list,
}

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

# 沒有專案資訊(DbtProjectInfo)時,ref()/source() 展開出的表名有三個已知落差,
# PR review 要求接線時要讓審查者看得到,不能只寫在模組文件裡沒人會翻:
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
# 有專案資訊(DbtProjectInfo)時,只有真的無法確定才帶這句——落差已經逐項處理掉,
# 剩下的是:專案用了 dbt 套件、沒有 seed/snapshot 名單、屬性檔解析不了、
# 或有會改變表名的設定(alias、自訂 schema、generate_*_name、樣板值)。
_RELATION_UNCERTAIN_NOTICE = (
    "此檔引用的 ref()/source() 有無法確定的部分(專案用了 dbt 套件、缺少 seed/snapshot "
    "名單、屬性檔無法解析,或有 alias / 自訂 schema / 樣板值等會改變表名的設定),"
    "展開出的表名可能與 dbt compile 不同,建議人工核對。")


@dataclass(frozen=True)
class DbtProjectInfo:
    """專案層級的資訊(由 dbt_relations.project_info_from_files() 從專案檔案整理)。

    給了它,ref() / source() 才能確認被引用的對象存在,並套用 sources.yml 的
    schema / identifier;沒給就維持舊行為(不驗證、固定慣例、帶 _RELATION_NOTICE)。
    資料庫名**一律用呼叫端給的 database**(約定的假名),不採用 sources.yml 宣告的
    資料庫——展開後的 SQL 會出現在審查結果裡,不放正式資料庫名;規則也不看資料庫名。

    project_name       dbt_project.yml 的 name(ref('本專案', 'x') 的第一個參數)
    ref_names          ref() 找得到的名稱:model 檔名,加上呼叫端提供的 seed / snapshot /
                       Python model(呼叫端提供的只代表存在,一律列在 uncertain_models)
    sources            {(來源名, 表名): (schema 或 None, identifier 或 None)};None = 沒宣告
    uncertain_models   被引用時表名無法確定的 model / seed / snapshot(alias / schema / database、
                       ephemeral、停用、同名、版本、Python model 等;見 dbt_relations)
    uncertain_sources  schema / identifier 是樣板值、無法確定的來源表
    global_uncertain   整個專案層級的不確定(自訂 generate_*_name、macro 裡的 config() 等);
                       非 None 時所有 ref() 都視為無法確定
    refs_complete      ref() 找不到就一定不存在(已知 seed/snapshot、沒用套件、屬性檔都解析得了)
    sources_complete   source() 找不到就一定沒宣告(沒用套件、屬性檔都解析得了)
    naming             {model 名稱: {"alias", "schema", "database": 字串或 None}}:表名確定的 model
                       設的自訂值(None = 沒設)。合併順序照 dbt:dbt_project.yml(資料夾由淺到深)
                       → 屬性檔的 config: → model 檔的 config();見 dbt_relations
    schema_concat      自訂 schema 要不要接在 target 的 schema 後面(dbt-sqlserver 的
                       dbt_sqlserver_use_default_schema_concat):True / False;None = 無法確定
    schema_concat_unset  專案自己沒有決定(flags 與 vars 都沒寫):由部署端設定的預設值補上
                       (設定檔 dbt.sqlserver_schema_concat,見 dbt_context)
    """
    project_name: str | None = None
    ref_names: frozenset = frozenset()
    sources: dict = field(default_factory=dict)
    uncertain_models: frozenset = frozenset()
    uncertain_sources: frozenset = frozenset()
    global_uncertain: str | None = None
    refs_complete: bool = False
    sources_complete: bool = False
    naming: dict = field(default_factory=dict)
    schema_concat: bool | None = None
    schema_concat_unset: bool = False


class DbtRenderError(Exception):
    """展開失敗。呼叫端應視為待人工確認的問題,不可當成「掃過且沒問題」。"""


class _Return(Exception):
    """模仿 dbt 的 MacroReturn:讓 macro 能回傳 Python 物件而不只是字串。"""

    def __init__(self, value):
        self.value = value


def _return(value):
    raise _Return(value)


def _wrap(macro, on_call=None):
    """把 jinja Macro 包成 Python 函式:有 return() 就取物件,否則取渲染字串。
    on_call 給定時,每次被呼叫先執行它(記下「用到了 {{ this }}」等)。"""

    def call(*args, **kwargs):
        if on_call is not None:
            on_call()
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
    if isinstance(value, _UnavailableRelation):
        value._fail()           # 錯誤訊息寫給不出值的原因,不是「輸出了物件」
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

    def getattr(self, obj, attribute):
        _guard_relation_access(obj, attribute)
        return super().getattr(obj, attribute)

    def getitem(self, obj, argument):
        # `this['x']`、`map(attribute='x')`、`sort(attribute='x')` 都走這裡
        _guard_relation_access(obj, argument)
        return super().getitem(obj, argument)

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


def model_identity(project: "DbtProjectInfo | None", name: str, schema: str) -> tuple[str, str, bool]:
    """model 被引用時的 (表名, 綱要, 確不確定),照 dbt 的預設規則:

      * 表名:alias 有值(非空字串)就用它去掉頭尾空白,否則用 model 名稱(generate_alias_name)
      * 綱要:自訂 schema 沒設就是 target 的 schema(呼叫端給的 schema);設了就看要不要串接
        (generate_schema_name;dbt-sqlserver 的旗標見 DbtProjectInfo.schema_concat):要串接是
        `<schema>_<自訂>`,不串接是自訂值去掉頭尾空白
      * 不確定:沒有專案資訊、專案層級無法確定、這個 model 無法確定、不知道要不要串接,
        或算出來的表名 / 綱要不是英數與底線(會被拼進 SQL 識別字,不接受)。不確定時一律照
        model 名稱與 target 的 schema 展開,只是記成不確定、不讓展開失敗

    資料庫名不在這裡:一律用呼叫端給的 database(約定的假名,見 DbtProjectInfo)。"""
    if project is None or project.global_uncertain or name in project.uncertain_models:
        return name, schema, False
    naming = project.naming.get(name)
    if naming is None:
        return name, schema, False
    alias = naming["alias"].strip() if naming.get("alias") else name
    custom = naming.get("schema")
    if custom is None:
        return (alias, schema, True) if _IDENT.fullmatch(alias) else (name, schema, False)
    if project.schema_concat is None:
        return name, schema, False
    rel_schema = f"{schema}_{custom.strip()}" if project.schema_concat else custom.strip()
    if not (_IDENT.fullmatch(alias) and _IDENT.fullmatch(rel_schema)):
        return name, schema, False
    return alias, rel_schema, True


def _make_relations(database: str | None, schema: str, usage: dict,
                    project: "DbtProjectInfo | None" = None):
    """dbt 的 ref() / source(),比照正式環境展開成 "<database>"."<schema>"."<表>"。

    usage 是呼叫端給的共用字典:呼叫到 ref()/source() 記 used;有專案資訊但這次
    引用無法確定時記 uncertain。呼叫端據此決定要不要帶提醒(見 _RELATION_NOTICE)。

    有專案資訊時:
      * 找得到的 ref() / source() 照 dbt 的規則展開(source 的 schema 預設是來源名、
        表名預設是 table 名,sources.yml 有宣告就用宣告的)
      * 找不到、而且能確定真的不存在(refs_complete / sources_complete)時展開失敗——
        dbt compile 一樣會失敗,這種 MR 合進去會讓排程壞掉
      * 找不到但無法確定(專案用了套件等),或有會改變表名的設定 → 記 uncertain,
        仍以慣例展開(規則層不看表名,不影響判斷)
      * 找得到的 model 照 dbt 的規則套用 alias 與自訂 schema(model_identity)
    """

    def relation(name, rel_schema=schema) -> str:
        usage["used"] = True
        if database is None:
            raise DbtRenderError(
                "未設定 database,無法展開 ref()/source()。dbt 由 profile 決定資料庫名稱,"
                "這裡同樣不猜值——請由呼叫端傳入 database。")
        text = f'"{database}"."{_check_ident("綱要", rel_schema)}"."{_check_ident("表", name)}"'
        # 跟 this 一樣是 _Relation:型別判斷(`is string` 等)由 _guard_relation_tests 擋下
        return _Relation(text, database, rel_schema, name)

    def missing(complete: bool, message: str) -> None:
        """找不到被引用的對象:能確定不存在就失敗,否則記為無法確定。
        載入 macro 檔時不判定:dbt 根本不執行 macro 檔最外層的程式碼,在那裡失敗
        會冤枉開發者(那一階段的紀錄載入完也會歸零,見 build_env)。"""
        if complete and not usage.get("loading_macros"):
            raise DbtRenderError(message)
        usage["uncertain"] = True

    def check_ref(package, name) -> None:
        if package is not None and package != project.project_name:
            # 其他套件的 model:套件不在 repo 裡,看不到
            missing(project.refs_complete,
                    f"ref() 指向不存在的套件 {package}(dbt 會編譯失敗)")
        elif name in project.ref_names:
            if not model_identity(project, name, schema)[2]:
                usage["uncertain"] = True
        else:
            missing(project.refs_complete,
                    f"ref() 指向專案裡不存在的 model / seed / snapshot:{name}(dbt 會編譯失敗)")

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
        name = _check_ident("表", args[-1])      # 先過白名單,錯誤訊息才可以帶名稱
        if project is not None:
            check_ref(args[0] if len(args) == 2 else None, name)
            if len(args) == 1 or args[0] == project.project_name:
                alias, rel_schema, _ = model_identity(project, name, schema)
                return relation(alias, rel_schema)
        return relation(name)

    def source(source_name, table_name):
        _check_ident("來源", source_name)
        _check_ident("表", table_name)
        if project is None:
            return relation(table_name)
        key = (source_name, table_name)
        if key not in project.sources:
            missing(project.sources_complete,
                    f"source() 未在 sources.yml 宣告:{source_name}.{table_name}(dbt 會編譯失敗)")
            return relation(table_name, source_name)
        if key in project.uncertain_sources:
            usage["uncertain"] = True
        declared_schema, identifier = project.sources[key]
        # dbt 的預設:schema = 來源名、表名 = table 名;宣告了才用宣告的
        return relation(identifier or table_name, declared_schema or source_name)

    def this(name: str):
        """{{ this }}:這個 model 自己的表,展開規則與 ref(自己) 相同。

        沒有 database、或 model 名稱不能當表名時給 _UnavailableRelation:跟 dbt 一樣「有定義」,
        用到才大聲失敗(不猜值;沒用到的 model 照常展開)。不在這裡記「用到表名」:由 render_model
        判斷這次有沒有用到再記(見 _mark_this_used)。"""
        if not isinstance(name, str) or not _IDENT.fullmatch(name):
            return _UnavailableRelation("model 名稱不合法(只接受英數與底線),無法提供 {{ this }}")
        if database is None:
            return _UnavailableRelation("未設定 database,無法提供 {{ this }}")
        alias, rel_schema, _ = model_identity(project, name, schema)
        text = f'"{database}"."{_check_ident("綱要", rel_schema)}"."{alias}"'
        return _Relation(text, database, rel_schema, alias)

    return ref, source, this


def _mark_this_used(usage: dict, project: "DbtProjectInfo | None", name: str) -> None:
    """記下用到這個 model 的表名(比照 ref(自己)):表名會改變的設定記 uncertain。
    什麼時候算用到見 render_model;多記只會多帶一句提醒,不影響判斷。"""
    usage["used"] = True
    if project is not None and not model_identity(project, name, DEFAULT_SCHEMA)[2]:
        usage["uncertain"] = True


def _uses_this(tree) -> bool:
    """語法樹裡有沒有出現 `this` 這個名稱(含參數名稱;寧可多算)。"""
    return any(n.name == "this" for n in tree.find_all(nodes.Name))


# 對表名物件只放行這兩個 `is` 判斷:dbt 的 this / ref() / source() 是 Relation 物件,我們是字串
# (或給不出值的物件),其他判斷(string、sequence、mapping、iterable、callable…)的結果跟 dbt
# 不一定相同,樣板可以拿差異把語句藏起來。逐個模擬模擬不完,所以遇到就展開失敗、交人工。
_RELATION_TESTS_ALLOWED = frozenset({"defined", "undefined"})


def _invocation_id() -> str:
    """每次不同、格式跟 dbt 一樣(uuid4 的寫法)的 invocation_id。"""
    h = secrets.token_hex(16)
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-{'89ab'[int(h[16], 16) % 4]}{h[17:20]}-{h[20:32]}"


_INVOCATION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")

# 一份審查共用一個 invocation_id(dbt 也是一次執行一個):一般、增量兩次展開,以及受影響 model 的
# 前後展開都用同一個值。每次展開都不同的話,語句裡有 invocation_id 時同一句兩次展開的原文不同,
# 會被當成兩句(增量分支的命中合併看的是語句原文)。樣板一樣猜不到這個值。
# ContextVar 在每條執行緒(以及每個 async task)各有自己的值,同時跑的審查不會共用。
_REVIEW_INVOCATION_ID: contextvars.ContextVar = contextvars.ContextVar(
    "segcra_review_invocation_id", default=None)


def start_review_invocation() -> str:
    """一份審查開始時呼叫一次:之後這份審查的展開(render_model_isolated)都用同一個 invocation_id。
    沒有呼叫時(單獨展開一個 model),每次展開產生新的值。"""
    value = _invocation_id()
    _REVIEW_INVOCATION_ID.set(value)
    return value


def _checked_invocation_id(value) -> str:
    """呼叫端給的 invocation_id 要是 uuid4 的格式(會原樣出現在展開結果裡);沒給時產生新的。"""
    if value is None:
        return _invocation_id()
    if not isinstance(value, str) or not _INVOCATION_ID_RE.fullmatch(value):
        raise DbtRenderError("invocation_id 的格式不對(要是 uuid4 的寫法)")
    return value


# 表名物件(this / ref() / source())只放行這幾個屬性:我們照實模擬了它們的值。dbt 的 Relation
# 是另一種物件,其他屬性(字串的 startswith、mapping 的 keys…)有沒有、是什麼都跟我們不同,樣板
# 可以拿「有沒有這個屬性」當條件把語句藏起來,所以取其他屬性或取索引一律展開失敗、交人工。
_RELATION_ATTRS_ALLOWED = frozenset({"database", "schema", "identifier", "name", "table"})
_RELATION_ACCESS_ERROR = ("樣板對表名物件(this / ref() / source())取了展開器沒有照實模擬的屬性或"
                          "索引,展開結果可能與 dbt 不同")


def _guard_relation_access(obj, name) -> None:
    """取屬性(`this.x`、`attr`)與取索引(`this['x']`、`map(attribute='x')`)共用:放行的只有
    _RELATION_ATTRS_ALLOWED 裡的名稱(取索引時 Jinja 會退回取同名屬性,結果相同)。"""
    if isinstance(obj, (_Relation, _UnavailableRelation)) and not (
            isinstance(name, str) and name in _RELATION_ATTRS_ALLOWED):
        raise DbtRenderError(_RELATION_ACCESS_ERROR)


def _string_filter(original):
    """`string` 濾器:表名物件轉成一般字串,跟 dbt 一樣(Relation 經過 string 就是一般字串)。
    Jinja 內建的寫法遇到字串子類別會原樣回傳,轉完仍是表名物件,字串方法就會被上面的規則擋下。
    給不出值的 this 照樣失敗(它的 __str__ 會失敗)。"""
    def string(value):
        if isinstance(value, _Relation):
            return str.__str__(value)
        return original(value)
    return string


@pass_environment
def _attr_filter(environment, obj, name):
    """`attr` 濾器不經過 environment.getattr,屬性不存在時也不會檢查,所以另外擋。"""
    _guard_relation_access(obj, name)
    return do_attr(environment, obj, name)


def _guard_relation_tests(env) -> None:
    """把環境裡除了 defined / undefined 以外的 `is` 判斷包起來:判斷對象或參數是表名物件就展開失敗。"""
    for name, test in list(env.tests.items()):
        if name not in _RELATION_TESTS_ALLOWED:
            env.tests[name] = _relation_test_guard(name, test)


# 判斷的參數是容器時,往下最多看這麼多個值(容器可以很大,判斷本身不能因此變慢)
_RELATION_SCAN_LIMIT = 10_000


def _holds_relation(values) -> bool:
    """values 裡有沒有表名物件,清單、tuple、set、dict 的鍵與值都往下找(`'x' is in [ref('a')]`)。
    看了上限那麼多個值還沒看完時當成有:寧可擋下交人工,不讓大容器把表名物件藏起來。
    自己包含自己的容器也由上限擋住。"""
    stack, seen = list(values), 0
    while stack:
        value = stack.pop()
        seen += 1
        if seen > _RELATION_SCAN_LIMIT:
            return True
        if isinstance(value, (_Relation, _UnavailableRelation)):
            return True
        if isinstance(value, dict):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset)):
            stack.extend(value)
    return False


def _relation_test_guard(name: str, test):
    def guarded(*args, **kwargs):
        if _holds_relation((*args, *kwargs.values())):
            raise DbtRenderError(
                f"樣板對 this / ref() / source() 做了 `is {name}` 判斷。展開器的表名物件跟 dbt 的"
                f"型別不同,這個判斷的結果不一定跟 dbt 一致,無法確定展開結果。")
        return test(*args, **kwargs)
    guarded.__dict__.update(getattr(test, "__dict__", {}))    # 保留 jinja 的 pass_environment 等標記
    return guarded


class _UnavailableRelation:
    """給不出值的 {{ this }}。dbt 每次執行都有 this,所以 `this is defined` 要跟 dbt 一樣是 True:
    給未定義值的話,`{% if this is defined %}` 裡的語句兩種模式都展開不出來,規則層掃不到,也沒有
    任何提示。任何使用(輸出、取屬性、比較、串接、當條件、走訪)都失敗,錯誤訊息是固定文字,
    不帶檔名。"""
    __slots__ = ("_hint",)

    def __init__(self, hint: str):
        object.__setattr__(self, "_hint", hint)

    def _fail(self, *args, **kwargs):
        raise UndefinedError(self._hint)

    # 不定義 __call__ 與 __html__:有定義的話 `is callable`、`is escaped` 會是 True,dbt 是 False
    __str__ = __repr__ = __bool__ = __iter__ = __len__ = __getattr__ = __getitem__ = _fail
    __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = _fail
    __add__ = __radd__ = __mul__ = __rmul__ = __mod__ = __rmod__ = __contains__ = _fail
    __hash__ = object.__hash__


class _Relation(str):
    """{{ this }}:字串內容就是 "<database>"."<schema>"."<表>",任何字串操作的結果都是真的值;
    另外提供 dbt Relation 常用的屬性。"""

    def __new__(cls, text: str, database: str, schema: str, name: str):
        obj = super().__new__(cls, text)
        obj.database, obj.schema = database, schema
        obj.identifier = obj.name = obj.table = name
        return obj


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
    # 樣板(含它呼叫的 macro)有沒有呼叫 is_incremental();沒有就不必以增量模式再展開一次
    incremental_checked: bool = False

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
def _macro_dir_names(macro_dirs) -> list[str]:
    """檢查 macro 目錄設定,回傳以 / 分段的相對路徑。

    目錄設定由呼叫端給,不接受跳出專案或指向別處的路徑。判斷不依平台:
    Windows 上 `專案 / "/etc"` 會跳到磁碟根目錄,`is_absolute()` 卻是 False。"""
    names = []
    for name in ((macro_dirs,) if isinstance(macro_dirs, str) else macro_dirs):
        posix = name.replace("\\", "/") if isinstance(name, str) else ""
        # 空字串 / None 會變成空的路徑段,一併被下面的檢查擋下
        if (posix.startswith("/") or ":" in posix
                or any(part in ("", ".", "..") for part in posix.split("/"))):
            raise DbtRenderError(f"macro 目錄設定不合法:{name!r}")
        names.append(posix)
    return names


def _macro_files_from_dict(macro_files, dir_names: list[str]) -> list[tuple[str, str]]:
    """從 {路徑: 內容} 挑出 macro 目錄底下的 .sql,順序與從磁碟讀相同。

    這份資料來自待審的 repo(經 archive 解壓),路徑照樣檢查一次:不是「相對路徑、以 /
    分段、沒有 . 與 ..」的鍵,或內容不是字串,整份拒絕(不猜、不略過)。錯誤訊息不回顯路徑。
    順序:依 macro_dirs 的順序,各目錄內依路徑分段排序(與 sorted(rglob) 在 POSIX 上相同),
    同名 macro 由後載入的勝出,所以順序必須一致。"""
    if not isinstance(macro_files, dict):
        raise DbtRenderError("macro_files 必須是 {路徑: 內容} 的字典")
    for path, text in macro_files.items():
        if not isinstance(path, str) or not isinstance(text, str):
            raise DbtRenderError("macro_files 的路徑與內容都必須是字串")
        if ("\\" in path or ":" in path or path.startswith("/")
                or any(part in ("", ".", "..") for part in path.split("/"))):
            raise DbtRenderError("macro_files 含不合法的路徑")
    files = []
    for name in dir_names:
        prefix = name + "/"
        files += sorted(((p, t) for p, t in macro_files.items()
                         if p.startswith(prefix) and p.endswith(".sql")),
                        key=lambda item: item[0].split("/"))
    return files


def build_env(code_root=None, variables: dict | None = None,
              database: str | None = None, schema: str = DEFAULT_SCHEMA,
              macro_dirs=DEFAULT_MACRO_DIRS, adapter: str = DEFAULT_ADAPTER,
              project: DbtProjectInfo | None = None, macro_files: dict | None = None,
              model_name: str | None = None, incremental: bool = False,
              invocation_id: str | None = None):
    """建好含 dbt 樁與 macro 的 Environment。code_root 與 macro_files 都沒給時不載入任何 macro。

    macro_files 專案的檔案 {相對專案根的路徑: 內容}(從 GitLab 取回、不在磁碟上的專案用)。
                只載入 macro 目錄底下的 .sql;與 code_root 只能擇一。兩種來源走同一段
                載入邏輯(上限、共用命名空間、內建同名檢查、壞檔記錄),結果相同
    macro_dirs  專案的 macro 目錄(dbt 的 macro-paths;預設 macros/)
    adapter     dbt 轉接器名稱,決定 adapter.dispatch 先找哪個前綴的實作
    project     專案層級資訊;給了才驗證 ref()/source() 的對象、套用 sources.yml
    incremental `is_incremental()` 的回傳值;True 時需要 model_name
    model_name  這次展開的 model 名稱({{ this }} 的表名;沒給時不提供 {{ this }})
    invocation_id 這次展開的 invocation_id(uuid4 的寫法);沒給時產生新的

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
    _guard_relation_tests(env)
    env.filters["attr"] = _attr_filter
    env.filters["string"] = _string_filter(env.filters["string"])
    env.filters.update(_DBT_TEXT_FILTERS)

    # 是否用到 ref()/source() 記在這裡,render_model() 展開成功後讀出來決定
    # relation_notice 要不要帶上。掛在 env 上是既有作法(見 segcra_macro_phase),
    # 不改 build_env() 的回傳簽章,呼叫端(含既有測試)不受影響。
    if project is not None and not isinstance(project, DbtProjectInfo):
        raise DbtRenderError("project 必須是 DbtProjectInfo")
    env.segcra_relation_usage = {"used": False, "uncertain": False}
    ref, source, this = _make_relations(database, schema, env.segcra_relation_usage, project)
    env.globals["ref"] = ref
    env.globals["source"] = source
    if incremental and model_name is None:
        raise DbtRenderError("以增量模式展開需要 model 名稱({{ this }} 的表名)")
    # 跟 dbt 一樣兩種模式都有 {{ this }}(dbt 每次執行都有):只在增量模式提供的話,
    # `{% if this is defined %}` 裡的 SQL 在一般模式展開不出來、也不會觸發增量模式,規則層掃不到。
    if model_name is not None:
        env.globals["this"] = this(model_name)
    env.globals["config"] = _Config()
    env.globals["return"] = _return
    env.globals["var"] = _make_var(variables)
    env.globals["execute"] = True                   # compile 期為 True
    # 預設比照 compile 回 False;有沒有被呼叫記下來,呼叫端據此決定要不要以 True 再展開一次
    env.segcra_incremental = {"called": False}

    def is_incremental():
        env.segcra_incremental["called"] = True
        return incremental

    env.globals["is_incremental"] = is_incremental
    # dbt 每次執行都會產生新的 invocation_id(uuid4)。這裡也每份審查不同(見 _REVIEW_INVOCATION_ID):
    # 固定值的話,樣板可以拿 `{% if invocation_id != '固定值' %}` 把語句藏在只有真正執行時才會進的分支。
    env.globals["invocation_id"] = _checked_invocation_id(invocation_id)
    _check_ident("轉接器", adapter)
    # macro 在這之後才載入,dispatch 是呼叫時才查表,所以拿得到全部 macro
    env.globals["adapter"] = _Adapter(env.globals, adapter)
    # 沒給 database 時,target.database 不可展開成 "None" 這種看似成功的錯誤結果:
    # 給未定義值,讓用到它的樣板大聲失敗(與 `this` / `env_var` 同一個原則)。
    env.globals["target"] = types.MappingProxyType({
        "database": database if database is not None else StrictUndefined(name="target.database"),
        "schema": schema,
    })
    # `env_var` 刻意不定義:會把審查機的環境變數寫進 SQL。未定義則由 StrictUndefined 大聲失敗。
    # 有沒有用到 {{ this }}:定義裡寫到 this 的 macro 被呼叫時記下(見 render_model)
    env.segcra_this_called = {"called": False}

    macros: list[str] = []
    conflicts: list[str] = []
    problems: list[str] = []
    # 同樣的失敗,另外分類記下(見 macro_issue):給 check_macro_files 用
    env.segcra_macro_issues = []
    if code_root is None and macro_files is None:
        return env, macros, conflicts, problems
    if code_root is not None and macro_files is not None:
        raise DbtRenderError("code_root 與 macro_files 只能擇一")

    seen: dict[str, str] = {}
    dir_names = _macro_dir_names(macro_dirs)
    # files 的每一項是 (相對專案根的路徑, 來源);來源是磁碟上的檔案或已在記憶體的內容
    if macro_files is not None:
        files = _macro_files_from_dict(macro_files, dir_names)
    else:
        root = pathlib.Path(code_root)
        files = []
        for posix in dir_names:
            macro_dir = root / posix
            if not macro_dir.is_dir() or macro_dir.is_symlink():
                continue
            files += [(f.relative_to(root).as_posix(), f) for f in sorted(macro_dir.rglob("*.sql"))
                      if not f.is_symlink() and f.is_file()]   # 符號連結可能指向任意檔案
    # macro 目錄互相包含(例如 macros 與 macros/shared)時,同一個檔會被列到兩次;
    # 只載入一次,否則它定義的每個 macro 都會被記成「與自己同名」
    files = list(dict(files).items())
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
    env.segcra_relation_usage["loading_macros"] = True
    for rel, origin in files:
        try:
            text = origin if isinstance(origin, str) else origin.read_bytes().decode("utf-8")
        except UnicodeDecodeError as e:
            problems.append(f"{rel}:{_safe_error(e)}")
            env.segcra_macro_issues.append(macro_issue(rel, e))
            continue
        total += len(text)
        if total > MAX_MACRO_CHARS:
            raise DbtRenderError(f"macro 檔案合計超過 {MAX_MACRO_CHARS} 字元上限")
        try:
            # 模組層的 {% set %} 只寫進該模組自己的名稱空間,不會汙染 shared(已驗證);
            # 進入 shared 的只有下面逐一檢查過的 macro。
            tree = env.parse(text)
            uses_this = {m.name for m in tree.find_all(nodes.Macro) if _uses_this(m)}
            module = env.from_string(tree).make_module(vars=shared, shared=True)
        except DbtRenderError:
            raise                       # 資源上限等我們自己的硬失敗照舊往上拋
        except Exception as e:
            # 單一 macro 檔壞掉(語法錯誤、模組層執行失敗)不該讓整個專案都展不開:
            # 略過它並記錄。真的需要它的 model 仍會因為名稱未定義而大聲失敗,
            # 不會靜靜產出少了那段邏輯的 SQL。
            problems.append(f"{rel}:{_safe_error(e)}")
            env.segcra_macro_issues.append(macro_issue(rel, e))
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
            shared[name] = env.globals[name] = _wrap(obj, on_call=_mark_called(
                env.segcra_this_called) if name in uses_this else None)
    # macro 檔的模組層(例如檔案開頭的 {% set t = ref('x') %})在載入時就會跑到
    # ref()/source();那是 macro 檔的事,不代表 model 本身用了——歸零,之後只記
    # model 展開(含它呼叫到的 macro)時真的用到的。
    env.segcra_relation_usage["used"] = False
    env.segcra_relation_usage["uncertain"] = False
    env.segcra_relation_usage["loading_macros"] = False
    env.segcra_this_called["called"] = False
    env.segcra_incremental["called"] = False
    env.segcra_macro_phase = False
    return env, sorted(macros), conflicts, problems


def _mark_called(flag: dict):
    def mark():
        flag["called"] = True
    return mark


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
    插在縮排之後而非行首,是因為上一行的 `-%}` 會吃到本行第一個非空白字元為止。
    不插的行,其輸出沿用上一個哨兵的行號。

    以 `{%-` / `{{-` / `{#-` 開頭的行:它會吃掉前面的空白(含換行),哨兵若插在它
    前面就會擋住空白控制。改把左修剪拆成一個只輸出哨兵的標記,原標記去掉 `-`:
    `{{- x }}` → `{{- '哨兵' }}{{ x }}`。前面的空白照樣被吃掉、兩個標記之間沒有
    任何文字,輸出只多了哨兵;原標記的內容一個字都不動(不解析表達式)。
    開頭符號之後補一個空白:否則 `{{--x }}`(負號)會變成 `{{-x }}` 而被當成左修剪。
    哨兵只含英數與 `/*_`,放進單引號字串不會提早結束字串。
    """
    inside = _lines_inside_tag(src)
    out = []
    for idx, line in enumerate(src.split("\n")):
        body = line.lstrip()
        if idx in inside or not body:
            out.append(line)
            continue
        indent = line[:len(line) - len(body)]
        mark = _sentinel(token, idx + 1)
        if body.startswith(_LEFT_TRIM_TAGS):
            out.append(f"{indent}{{{{- '{mark}' }}}}{body[:2]} {body[3:]}")
        else:
            out.append(indent + mark + body)
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
        # 空白控制會把幾行接成一個輸出行,一行可能含多個哨兵。這行歸給「第一段內容
        # 之前的那個哨兵」:`FROM t` 後面接上 `{{- 巨集() }}` 時,這行仍屬 FROM 那行;
        # `-%}` 吃掉換行後才出現的內容,則屬內容所在那行。後續的行沿用最後一個哨兵。
        parts = pattern.split(line)          # 偶數位置是文字,奇數位置是哨兵行號
        owner = None
        for k, part in enumerate(parts):
            if k % 2:
                cur = int(part)
            elif owner is None and part.strip():
                owner = cur
        stripped.append(pattern.sub("", line))
        mapping[len(stripped)] = cur if owner is None else owner
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
                 adapter: str = DEFAULT_ADAPTER,
                 project: DbtProjectInfo | None = None,
                 macro_files: dict | None = None, incremental: bool = False,
                 invocation_id: str | None = None) -> RenderResult:
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
    project     專案層級資訊(DbtProjectInfo);給了才驗證 ref()/source() 的對象是否存在、
                套用 sources.yml,且只在真的無法確定時才帶 relation_notice
    macro_files 專案的檔案 {相對路徑: 內容},macro 從這裡讀而不讀磁碟(見 build_env);
                與 code_root 只能擇一
    incremental 以 `is_incremental()` 為 True 展開(增量分支);`{{ this }}`(這個 model
                自己的表名,檔名去掉副檔名)。預設 False,與 dbt compile 相同
    invocation_id 這次展開的 invocation_id(uuid4 的寫法);沒給時產生新的

    任何失敗都收斂成 ok=False 而不丟例外。
    """
    model_path = pathlib.Path(model_path)
    if code_root is not None:
        root = pathlib.Path(code_root)
    elif source is None and macro_files is None:
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
                                                     macro_dirs, adapter, project, macro_files,
                                                     model_path.stem, incremental,
                                                     invocation_id=invocation_id)
        # dbt 讀檔時會去掉頭尾空白再渲染(開頭空行不會出現在編譯結果裡)
        body = source.strip()
        tree = env.parse(body)
        clean = _render_capped(env.from_string(tree), MAX_OUTPUT_CHARS)
        # 哨兵渲染會再呼叫一次;只算乾淨的那一次
        incremental_checked = env.segcra_incremental["called"]
        # 用到這個 model 的表名:增量模式一律算;一般模式看 model 本身有沒有寫到 this(只看
        # 語法樹,寫在沒進去的分支裡也算),或呼叫了定義裡寫到 this 的 macro
        if database is not None and (incremental or _uses_this(tree)
                                     or env.segcra_this_called["called"]):
            _mark_this_used(env.segcra_relation_usage, project, model_path.stem)
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
        relation_notice=_relation_notice(env.segcra_relation_usage, project),
        incremental_checked=incremental_checked,
    )


def _relation_notice(usage: dict, project: DbtProjectInfo | None) -> str | None:
    """沒有專案資訊:用到 ref()/source() 就提醒(落差都沒處理)。
    有專案資訊:只有真的無法確定才提醒——能確定的已經照 dbt 的規則展開。"""
    if project is None:
        return _RELATION_NOTICE if usage["used"] else None
    return _RELATION_UNCERTAIN_NOTICE if usage["uncertain"] else None


# ------------------------------------------------------------------ macro 檔檢查
# macro 檔載入失敗的種類。只有 SYNTAX 是「dbt 一樣解析不了、整個專案編譯失敗」;其餘是 dbt 解析
# 得過、用到時才可能失敗,或展開器的限制,dbt 不一定有問題。
MACRO_SYNTAX = "syntax"              # Jinja 語法錯誤(未閉合的區塊、寫錯的運算式、不認得的標籤…)
MACRO_UNSUPPORTED = "unsupported"    # 不認得的濾器、重複定義的 block(dbt 解析得過,用到時才失敗)
MACRO_TOO_DEEP = "too_deep"          # 巢狀太深(Python 的遞迴上限不一定跟 dbt 相同)
MACRO_LOAD_FAILED = "load_failed"    # 解析得過,載入時失敗(模組層用到展開器沒提供的東西、讀檔失敗)


def macro_issue(rel: str, error: BaseException) -> dict:
    """macro 檔載入失敗的分類:{path, kind, line}。只有種類與行號,不帶錯誤訊息(含樣板內容)。

    **不認得的標籤算語法錯誤**:dbt 的 Jinja 環境(dbt_common 的 get_environment)只多了 `do`、
    `loopcontrols` 兩個 Jinja 內建擴充與 `materialization`、`docs`、`test` 三個標籤,展開器全都有
    (另外還認得 `snapshot`)。所以展開器不認得的標籤 dbt 也不認得,整個專案解析失敗。
    TemplateAssertionError(不認得的濾器、重複定義的 block)也是語法錯誤的子類別,但 dbt 解析得過、
    呼叫到那個 macro 時才失敗,所以分開給 major。dbt 自己加的濾器展開器都有(見 _DBT_TEXT_FILTERS)。"""
    line = 0
    if isinstance(error, jinja2.TemplateSyntaxError):
        lineno = getattr(error, "lineno", None)
        line = lineno if isinstance(lineno, int) and not isinstance(lineno, bool) and lineno > 0 else 0
        kind = MACRO_UNSUPPORTED if isinstance(error, jinja2.TemplateAssertionError) else MACRO_SYNTAX
    elif isinstance(error, RecursionError):
        kind = MACRO_TOO_DEEP
    else:
        kind = MACRO_LOAD_FAILED
    return {"path": rel, "kind": kind, "line": line}


@dataclass(frozen=True)
class MacroCheck:
    """check_macro_files 的結果。ok=False:檢查本身沒做完(逾時、資源上限、子行程失敗),
    不能當成沒問題。"""
    ok: bool
    issues: tuple = ()               # (macro_issue 的結果, ...)


def check_macro_files(*, macro_files: dict, database: str | None = None,
                      macro_dirs=DEFAULT_MACRO_DIRS, adapter: str = DEFAULT_ADAPTER,
                      project: DbtProjectInfo | None = None) -> MacroCheck:
    """照展開時的方式載入專案的 macro 檔(參數同 render_model),回傳每個載入失敗的檔與原因。
    **待審的 MR 內容請改用 check_macro_files_isolated()。**"""
    try:
        env, _, _, _ = build_env(database=database, macro_dirs=macro_dirs, adapter=adapter,
                                 project=project, macro_files=macro_files)
    except Exception:                  # noqa: BLE001 — 資源上限、與內建同名等:檢查沒做完
        return MacroCheck(ok=False)
    return MacroCheck(ok=True, issues=tuple(env.segcra_macro_issues))


def _macro_check_failure(message: str) -> MacroCheck:
    return MacroCheck(ok=False)


def check_macro_files_isolated(*, timeout_s: float = DEFAULT_ISOLATED_TIMEOUT_S,
                               max_memory_mb: int = DEFAULT_ISOLATED_MEMORY_MB,
                               **kwargs) -> MacroCheck:
    """在獨立子行程中做 check_macro_files(理由同 render_model_isolated)。不丟例外。"""
    result = run_isolated(check_macro_files, (), kwargs, timeout_s=timeout_s,
                          max_memory_mb=max_memory_mb, on_failure=_macro_check_failure)
    return result if isinstance(result, MacroCheck) else MacroCheck(ok=False)


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
    # 一份審查裡的展開共用一個 invocation_id(見 _REVIEW_INVOCATION_ID);呼叫端指定時照用
    review_id = _REVIEW_INVOCATION_ID.get()
    if review_id is not None:
        kwargs.setdefault("invocation_id", review_id)
    result = run_isolated(render_model, (model_path,), kwargs, timeout_s=timeout_s,
                          max_memory_mb=max_memory_mb, on_failure=_render_failure)
    if not isinstance(result, RenderResult):
        return _render_failure("DbtRenderError: 展開子行程回傳了非預期的資料")
    return result
