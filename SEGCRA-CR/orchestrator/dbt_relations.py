"""dbt_relations — 從專案檔案整理 ref() / source() 需要的專案層級資訊。

輸出 dbt_render.DbtProjectInfo,交給 render_model(project=...):ref() 才能確認被引用的
model 存在、source() 才能套用 sources.yml 的 schema / identifier(PR #10 review 列的
三個精確度落差)。

檔案來自待審的 commit,是不可信內容。原則:
  * **看不懂就往「無法確定」靠**,不往「確定不存在」靠——後者會讓展開失敗、
    冤枉開發者;前者只是保留「表名未經驗證」的提醒
  * 純解析、不丟例外、不寫檔、不連網;YAML 一律用安全的載入器(有 libyaml 時用 C 版,
    行為相同、約快 4 倍)
  * YAML 的別名(&a / *a)可以讓同一段內容被引用成千上萬次:1 MB 的檔案就能讓
    「來源 × 表」展開成上億筆。設定區塊走訪時記住走過的節點;屬性檔的來源、表、model
    合計超過 MAX_PROPERTY_ENTRIES 筆就判成看不懂(無法確定),不會被「別名炸彈」拖垮

**表名照 dbt 的規則算**(model 的 alias / schema / database,`naming`):字串常值的設定照
dbt 的合併順序取值——dbt_project.yml(資料夾由淺到深)→ 屬性檔的 `config:` → model 檔的
`config()`,每個設定各自覆蓋。schema 的組法見 `_schema_concat`。

不處理(一律視為無法確定,保留提醒):dbt 套件(packages.yml / dependencies.yml)
的 model 與 source、樣板值或不是字串的 alias / schema / database、snapshot 的 target_schema /
target_database、generate_*_name、ephemeral(被引用時展開成 CTE,不是表名)、
停用(enabled 不是 true)、model 版本(versions)、同名的 model(含 model 與 seed /
snapshot 同名)、算出來表名相同的 model、Python model(內容不讀)、macro 裡呼叫的 config()、
config.set()。
偵測不到(會被當成確定):先把 config 指給別的變數再呼叫它的 set(`{% set c = config %}`
之後 `c.set(...)`)——名為 config 的區域變數很常見,只看字面分不出來。
seed / snapshot / Python model 的名稱由呼叫端提供(打包下載只收 .sql / .yml 的內容);
沒提供時 ref() 找不到也不能判定不存在。提供的只有名稱:存在可以確定,表名一律無法確定
(snapshot 的 target_schema、Python model 的 dbt.config() 都寫在看不到的內容裡)。
"""
import re

import yaml

from .dbt_render import DbtProjectInfo

_PACKAGE_FILES = ("packages.yml", "dependencies.yml")
# 會改變 ref() 展開出的表名的設定鍵(dbt_project.yml 用 + 前綴,屬性檔不用)
_NAME_KEYS = frozenset({"alias", "schema", "database", "+alias", "+schema", "+database"})
# snapshot 的 target_schema / target_database:dbt 用它們覆蓋 schema / database(#19 review)。
# 只用在指名到某一個 snapshot 的地方(屬性檔、config() 的參數),不用在 dbt_project.yml 的
# 整個專案層級:那裡設 +target_schema 很常見(舊版 snapshot 必填),而 snapshot 的名稱來自
# 呼叫端、本來就一律無法確定——拿來判整個專案,只會讓所有 model 都失去表名驗證
_ENTRY_NAME_KEYS = _NAME_KEYS | frozenset(
    key for name in ("target_schema", "target_database") for key in (name, "+" + name))
# 照 dbt 規則算出值的三個設定(model 的 config;dbt_project.yml 另外接受 + 前綴)
_NAMING = ("alias", "schema", "database")
# 只在指名某一個 snapshot 的地方出現時算無法確定(不模擬)
_TARGET_KEYS = frozenset({"target_schema", "target_database"})
# dbt-sqlserver 的旗標:自訂 schema 要不要接在 target 的 schema 後面(見 _schema_concat)
SCHEMA_CONCAT_KEY = "dbt_sqlserver_use_default_schema_concat"
_UNSET = object()
# 屬性檔 config: 裡只認不加前綴的 alias / schema / database;加了 + 的寫法與 snapshot 的
# target_* 一樣看不懂(不知道 dbt 認不認,寧可記成無法確定)
_OTHER_NAME_KEYS = _TARGET_KEYS | {"+" + k for k in (*_TARGET_KEYS, *_NAMING)}
_MATERIALIZED_KEYS = ("materialized", "+materialized")
_ENABLED_KEYS = ("enabled", "+enabled")
# 專案自訂 schema / alias / database 的命名規則:我們不模擬,只偵測
_GENERATE_NAME_MACRO = re.compile(r"\{%-?\s*macro\s+generate_(?:schema|alias|database)_name\s*\(")
# model 檔內的 {{ config(...) }}:一次線性掃描找出每個呼叫的括號內容(含巢狀)
_CONFIG_TOKENS = re.compile(r"\bconfig\s*\(|[()]")
# config() 的參數只認「名稱 = 常值」:字串、數字、布林 / None、由字串或數字組成的清單。
# 其餘寫法(字典、** 展開、變數、var()、樣板運算)一律看不懂 → 無法確定
_SCALAR = r"""'[^'\\]*'|"[^"\\]*"|-?\d+(?:\.\d+)?|True|False|None|true|false|none"""
_SIMPLE = r"""'[^'\\]*'|"[^"\\]*"|-?\d+(?:\.\d+)?"""
_LIST = rf"\[\s*(?:(?:{_SIMPLE})\s*(?:,\s*(?:{_SIMPLE})\s*)*,?\s*)?\]"
_CONFIG_ARG = re.compile(rf"\s*([A-Za-z_]\w*)\s*=\s*({_SCALAR}|{_LIST})\s*(?:,|$)")
# 不經過 config( 呼叫就改設定的寫法(#19 review):dbt 的 config.set(name, value) 等同
# config({name: value})。連同等價的取用方式一起認:config['set']、config|attr('set')。
# 不看第一個參數是什麼,一律當成可能改表名(專案裡很少用;寧可多判)。
_CONFIG_SET = re.compile(r"""\bconfig\s*(?:\.\s*set\b|\[\s*['"]set['"]\s*\]|\|\s*attr\b)""")
_NON_SPACE = re.compile(r"\S")
_LIBYAML = getattr(yaml, "__with_libyaml__", False) and hasattr(yaml, "CSafeLoader")
# 所有屬性檔合計最多處理幾筆來源 / 表 / model。真實專案遠低於此;超過多半是別名炸彈
MAX_PROPERTY_ENTRIES = 50_000


class _Unreadable(Exception):
    """某個屬性檔看不懂;整體改判「不完整」。"""


class _Budget:
    """屬性檔處理量的上限,所有檔案共用。"""

    def __init__(self, limit: int):
        self.left = limit

    def spend(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise _Unreadable


def project_info_from_files(files: dict, *, model_paths=("models",), macro_paths=("macros",),
                            other_ref_names=None, root_files_checked: bool = False) -> DbtProjectInfo:
    """files               {相對專案根目錄的路徑(/ 分隔): 內容}。通常是取回的 models / macros
                           加上專案根目錄的 dbt_project.yml、packages.yml、dependencies.yml
    model_paths         dbt_project.yml 的 model-paths
    macro_paths         dbt_project.yml 的 macro-paths
    other_ref_names     seed、snapshot,以及 files 裡看不到的 Python model(.py)名稱。
                        None = 不知道(ref() 找不到時不能判定不存在)。打包下載只收
                        .sql / .yml 的內容,呼叫端要從檔名清單取得完整名單才能給;
                        給了就代表呼叫端確認名單完整
                        (files 裡若有 model 目錄下的 .py 路徑,只取檔名,不讀內容)。
                        這些名稱只代表「存在」,表名一律無法確定:只有名稱、看不到內容,
                        而 snapshot 的 target_schema、Python model 的 dbt.config() 都寫在
                        內容裡;與 model 同名時 dbt 會編譯失敗(AmbiguousAliasError)
    root_files_checked  呼叫端確認 files 已包含專案根目錄的 packages.yml / dependencies.yml
                        (若存在的話)。預設 False:files 裡沒有這兩個檔,可能只是沒傳進來,
                        不能據此斷定「沒用套件」——否則套件裡的 model 會被誤判成不存在

    輸入的形狀不對(不是字典、目錄或名單不是字串)時回傳 DbtProjectInfo() 預設值:
    全部視為無法確定,不會把任何引用判成「確定不存在」。
    """
    if (not isinstance(files, dict) or isinstance(model_paths, str)
            or isinstance(macro_paths, str)
            or not all(isinstance(p, str) for p in (*model_paths, *macro_paths))):
        return DbtProjectInfo()
    if other_ref_names is not None and (isinstance(other_ref_names, str) or not all(
            isinstance(n, str) for n in other_ref_names)):
        return DbtProjectInfo()
    model_dirs = tuple(p.rstrip("/") for p in model_paths)
    macro_dirs = tuple(p.rstrip("/") for p in macro_paths)
    (project_name, global_uncertain, project_ok, all_sources_uncertain,
     models_block, schema_concat, concat_unset) = _read_dbt_project(files.get("dbt_project.yml"))
    # 沒確認過根目錄的檔案,就當作「可能有套件」
    has_packages = (root_files_checked is not True
                    or any(name in files for name in _PACKAGE_FILES))

    models: set[str] = set()
    model_files: dict[str, int] = {}        # 名稱 → 檔案數(同名的 model dbt 會編譯失敗)
    uncertain_models: set[str] = set()
    model_fqn: dict[str, list[str]] = {}     # 名稱 → dbt 的 fqn(專案名、資料夾、model 名)
    sql_naming: dict[str, dict] = {}         # 名稱 → model 檔 config() 設的值
    yml_naming: dict[str, dict] = {}         # 名稱 → 屬性檔 config: 設的值
    sources: dict = {}
    uncertain_sources: set = set()
    properties_ok = True
    budget = _Budget(MAX_PROPERTY_ENTRIES)
    for path, content in sorted(files.items(), key=lambda kv: str(kv[0])):
        if isinstance(path, str) and _under(path, model_dirs) and path.lower().endswith(".py"):
            # Python model:dbt 照樣可以 ref(),只取檔名(內容不讀,也可能沒傳)。內容裡的
            # dbt.config() 可能改名或停用,看不到 → 一律無法確定
            name = path.rsplit("/", 1)[-1][:-3]
            models.add(name)
            uncertain_models.add(name)
            model_files[name] = model_files.get(name, 0) + 1
            continue
        if not isinstance(path, str) or not isinstance(content, str):
            properties_ok = False
            continue
        lower = path.lower()
        if _under(path, model_dirs) and lower.endswith(".sql"):
            name = path.rsplit("/", 1)[-1][:-4]
            models.add(name)
            model_files[name] = model_files.get(name, 0) + 1
            model_fqn[name] = [project_name or "", *_model_dirs_of(path, model_dirs), name]
            naming = _config_naming(content)
            if naming is None:
                uncertain_models.add(name)
            else:
                sql_naming[name] = naming
        elif _under(path, model_dirs) and lower.endswith((".yml", ".yaml")):
            try:
                _read_properties(content, sources, uncertain_sources, uncertain_models, budget,
                                 yml_naming)
            except _Unreadable:
                properties_ok = False
        elif _under(path, macro_dirs) and lower.endswith(".sql"):
            if _GENERATE_NAME_MACRO.search(content):
                global_uncertain = global_uncertain or "專案自訂了 generate_*_name 命名規則"
            elif _config_may_rename(content):
                # model 呼叫這個 macro 時,macro 裡的 config() 一樣作用在那個 model 上;
                # 看不出是哪些 model 呼叫 → 整個專案都無法確定
                global_uncertain = global_uncertain or "macro 裡呼叫的 config() 可能改掉表名"
    # 同名的 model:dbt 編譯會失敗,不能當成「確定存在」
    uncertain_models.update(name for name, count in model_files.items() if count > 1)
    # 呼叫端提供的 seed / snapshot / Python model:只有名稱,表名無法確定(#19 review)。
    # 與 model 同名的也在這裡一併涵蓋
    uncertain_models.update(other_ref_names or ())
    if all_sources_uncertain:
        # dbt_project.yml 的 sources 區塊有停用等設定,或 dbt_project.yml 看不懂
        uncertain_sources.update(sources)
    if not properties_ok:
        # 看不懂的屬性檔裡可能就有某個 model 的 alias / schema / database:不知道是哪個 model
        global_uncertain = global_uncertain or "有屬性檔看不懂,model 的表名設定可能漏看"
    naming = _merge_naming(models, uncertain_models, model_fqn, models_block, yml_naming,
                           sql_naming)

    complete = properties_ok and project_ok and not has_packages
    return DbtProjectInfo(
        project_name=project_name,
        ref_names=frozenset(models | set(other_ref_names or ())),
        sources=sources,
        uncertain_models=frozenset(uncertain_models),
        uncertain_sources=frozenset(uncertain_sources),
        global_uncertain=global_uncertain,
        refs_complete=complete and other_ref_names is not None,
        sources_complete=complete,
        naming=naming,
        schema_concat=schema_concat,
        schema_concat_unset=concat_unset,
    )


def _model_dirs_of(path: str, model_dirs: tuple[str, ...]) -> list[str]:
    """model 檔在 model 目錄底下的資料夾(dbt 的 fqn 中間那幾段)。"""
    for d in model_dirs:
        if path.startswith(d + "/"):
            return path[len(d) + 1:].split("/")[:-1]
    return []


def _merge_naming(models: set, uncertain: set, fqn: dict, models_block, yml_naming: dict,
                  sql_naming: dict) -> dict:
    """表名確定的 model 各自的 alias / schema / database(None = 沒設),照 dbt 的合併順序:
    dbt_project.yml(資料夾由淺到深)→ 屬性檔的 config: → model 檔的 config(),每個設定各自
    覆蓋。任何一層看不懂、或明寫 null / None 想蓋掉上層的值(_overlay),就把那個 model 加進
    uncertain。

    算出來表名相同的 model(例如資料夾層級設了同一個 alias)dbt 會編譯失敗,也加進 uncertain。"""
    naming = {}
    for name in sorted(models):
        if name in uncertain or name not in fqn:
            continue
        merged = _project_naming(models_block, fqn[name])
        layers = (yml_naming.get(name, {}), sql_naming.get(name, {}))
        if merged is None or not all(_overlay(merged, layer) for layer in layers):
            uncertain.add(name)
            continue
        naming[name] = {k: merged.get(k) for k in _NAMING}
    # 撞名:alias(沒設就是 model 名稱)、schema、database 的自訂值都一樣。dbt 三個都會去掉
    # 頭尾空白;SQL Server 的名稱預設不分大小寫
    seen: dict = {}
    for name, n in naming.items():
        key = tuple(_fold(v) for v in (n["database"], n["schema"], n["alias"] or name))
        seen.setdefault(key, []).append(name)
    for names in seen.values():
        if len(names) > 1:
            uncertain.update(names)
            for name in names:
                naming.pop(name, None)
    return naming


def _fold(value):
    """比對撞名用:字串去掉頭尾空白、不分大小寫;None(沒設)照舊。"""
    return value.strip().lower() if isinstance(value, str) else value


def _project_naming(models_block, fqn: list[str]) -> dict | None:
    """dbt_project.yml 的 models 區塊對這個 model 設的 alias / schema / database。
    從最外層往 fqn(專案名、資料夾、model 名)走,越深的蓋過越淺的;看不懂回 None。"""
    merged: dict = {}
    node = models_block
    for part in [None, *fqn]:
        if part is not None:
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            break
        level = _level_naming(node)
        if level is None or not _overlay(merged, level):
            return None
    return merged


def _overlay(merged: dict, layer: dict) -> bool:
    """把這一層的設定蓋到 merged 上。明寫 null / None 想蓋掉上層設的值時,dbt 是清掉還是保留
    上層的值沒有逐一查證,回 False(無法確定);上層也沒設時 None 就是沒設。"""
    for key, value in layer.items():
        if value is None and merged.get(key) is not None:
            return False
        merged[key] = value
    return True


def _level_naming(node: dict) -> dict | None:
    """dbt_project.yml 某一層直接設的 alias / schema / database。`+` 前綴與不加前綴都算設定
    (值是物件的不加前綴的鍵是子目錄);兩種寫法同時出現而值不同、或值不是字串 / null 時看不懂。"""
    out: dict = {}
    for key in _NAMING:
        plain, plus = node.get(key, _UNSET), node.get("+" + key, _UNSET)
        if isinstance(plain, dict):
            plain = _UNSET                   # 名稱剛好叫 schema 的子目錄
        values = [v for v in (plain, plus) if v is not _UNSET]
        if not values:
            continue
        if len(values) == 2 and values[0] != values[1]:
            return None
        value = _literal(values[-1])
        if value is _UNSET:
            return None
        out[key] = value
    return out


def _literal(value):
    """設定檔裡的值:字串常值照用、null 是沒設;樣板值與其他型別看不懂(回 _UNSET)。"""
    if value is None:
        return None
    if isinstance(value, str) and not _templated(value):
        return value
    return _UNSET


def _schema_concat(raw: dict) -> tuple[bool | None, bool]:
    """自訂 schema 要不要接在 target 的 schema 後面,回傳 (結果, 專案沒有決定)。

    照 dbt-sqlserver 的 sqlserver__generate_schema_name:旗標(flags 的
    dbt_sqlserver_use_default_schema_concat)或舊寫法的同名 var 任一個是 true 就串接
    (`<target.schema>_<自訂>`),否則自訂值原樣用。旗標沒寫時用轉接器的預設值,而這個預設在
    不同版本相反(1.9.2 是 false、1.12 的預覽版是 true),所以**專案沒寫時回 (None, True)**,由呼叫端
    用部署端設定的預設補上。值不是布林、或 var 寫在專案層級的子區塊裡時無法確定(None, False)。"""
    flags, variables = raw.get("flags", {}), raw.get("vars", {})
    if not isinstance(flags, (dict, type(None))) or not isinstance(variables, (dict, type(None))):
        return None, False
    flags, variables = flags or {}, variables or {}
    if any(isinstance(v, dict) and SCHEMA_CONCAT_KEY in v for v in variables.values()):
        return None, False                   # 專案層級的 var:哪個 model 吃到哪個值不模擬
    var = variables.get(SCHEMA_CONCAT_KEY, _UNSET)
    if var is not _UNSET and not isinstance(var, bool):
        return None, False
    if var is True:
        return True, False
    flag = flags.get(SCHEMA_CONCAT_KEY, _UNSET)
    if flag is _UNSET:
        return None, True
    if not isinstance(flag, bool):
        return None, False
    return flag, False


def _under(path: str, dirs: tuple[str, ...]) -> bool:
    return any(path == d or path.startswith(d + "/") for d in dirs)


def _config_bodies(text: str) -> list[str] | None:
    """每個 config( 呼叫的括號內容(含巢狀括號)。一次線性掃描;有 config( 沒有對應的
    右括號時回傳 None(看不懂)。括號寫在字串裡會讓切點不準,但切錯的內容不會符合
    _CONFIG_ARG 的寫法,結果一樣往「無法確定」靠。"""
    bodies, start, depth = [], None, 0
    for m in _CONFIG_TOKENS.finditer(text):
        piece = m.group()
        if start is None:
            if piece not in ("(", ")"):     # config(
                start, depth = m.end(), 1
            continue
        if piece == ")":
            depth -= 1
            if depth == 0:
                bodies.append(text[start:m.start()])
                start = None
        else:
            depth += 1                      # ( 或巢狀的 config(
    if start is not None:
        return None
    return bodies


def _config_naming(text: str) -> dict | None:
    """model 檔裡的 config() 設的 alias / schema / database(字串常值;`None` 是沒設)。
    看不懂、或會讓表名無法確定時回 None。寧可多判(只是保留提醒),不可漏判:
    每個參數都必須是「名稱 = 常值」;alias / schema / database 要是字串或 None,名稱要是小寫
    (dbt 的設定名稱分大小寫);不能有 target_schema / target_database;materialized 不是
    ephemeral(被引用時展開成 CTE);enabled 不是 false(停用的 model 被引用會編譯失敗)。
    `config({'alias': 'x'})`、`config(**c)`、`config(materialized=var('m'))`、
    沒有右括號的 `config(`,都看不懂。
    `{% do config.set('alias', 'x') %}`(與 `config['set']`、`config|attr('set')`)不看
    參數,一律算看不懂。同一個檔有好幾個 config(),後面的蓋過前面的。"""
    if _CONFIG_SET.search(text):
        return None
    bodies = _config_bodies(text)
    if bodies is None:
        return None
    naming: dict = {}
    for body in bodies:
        pos = 0
        # 從 pos 往後找有沒有剩下的內容,不切片複製(切片會讓參數很多時變成平方時間)
        while _NON_SPACE.search(body, pos):
            m = _CONFIG_ARG.match(body, pos)
            if m is None:
                return None
            name, value = m.group(1), m.group(2)
            key = name.lower()
            if key in _TARGET_KEYS or (key == "materialized"
                                       and value.strip("'\"").lower() == "ephemeral"):
                return None
            if key == "enabled" and value.lower() != "true":
                return None                 # 停用的 model 被 ref() 時 dbt 會編譯失敗
            if key in _NAMING:
                literal = _arg_literal(value)
                if literal is _UNSET or name != key:
                    return None
                naming[key] = literal
            pos = m.end()
    return naming


def _arg_literal(value: str):
    """config() 參數的值:引號包起來的字串照用、None / none 是沒設,其他看不懂(_UNSET)。"""
    if value[:1] in ("'", '"'):
        return value[1:-1]
    if value in ("None", "none"):
        return None
    return _UNSET


def _config_may_rename(text: str) -> bool:
    """macro 檔裡的 config() 會不會改掉呼叫它的 model 的表名(看不出是哪些 model 呼叫,
    有設 alias / schema / database 或看不懂就算會改)。"""
    naming = _config_naming(text)
    return naming is None or bool(naming)


def _materialization_unknown(value) -> bool:
    """設定檔裡的 materialized 值:ephemeral、樣板值或不是字串,都可能讓表名不出現在 SQL。"""
    return not isinstance(value, str) or _templated(value) or value.strip().lower() == "ephemeral"


def _maybe_disabled(value) -> bool:
    """設定檔裡的 enabled 值:只有明確的 true 才算啟用;false、樣板值或其他型別都可能停用。"""
    return value is not True


def _source_maybe_disabled(entry: dict) -> bool:
    """sources.yml 的來源或表可能停用(停用的 source 被引用時 dbt 會編譯失敗)。"""
    config = entry.get("config")
    return ((isinstance(config, dict) and "enabled" in config and _maybe_disabled(config["enabled"]))
            or ("enabled" in entry and _maybe_disabled(entry["enabled"])))


def _templated(value) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _load(text: str):
    try:
        if _LIBYAML:
            return yaml.load(text, Loader=yaml.CSafeLoader)   # 安全載入器的 C 版
        return yaml.safe_load(text)
    except Exception:                       # noqa: BLE001 — 看不懂就是看不懂
        raise _Unreadable from None


def _read_dbt_project(text):
    """回傳 (專案名稱, ref() 的專案層級不確定原因, 是否讀得懂, source() 是否全部無法確定,
    models 區塊, 自訂 schema 是否串接, 串接與否專案沒決定)。
    沒有 dbt_project.yml 也算讀不懂;讀不懂時 sources 區塊的設定(例如停用)也看不到。

    models 區塊的 alias / schema / database 不在這裡判成整個專案無法確定:逐一照資料夾算
    (_project_naming);ephemeral 與停用仍然是整個專案無法確定。"""
    if text is None:
        return None, None, False, True, None, None, False
    try:
        raw = _load(text)
    except _Unreadable:
        return None, "dbt_project.yml 無法解析", False, True, None, None, False
    if not isinstance(raw, dict):
        return None, "dbt_project.yml 無法解析", False, True, None, None, False
    name = raw.get("name") if isinstance(raw.get("name"), str) else None
    concat, unset = _schema_concat(raw)
    models_block = raw.get("models")
    sources_uncertain = _sets_names(raw.get("sources"))
    for block in ("models", "seeds", "snapshots", "sources"):
        if _sets_names(raw.get(block), names=block != "models"):
            return (name, f"dbt_project.yml 的 {block} 設定了 alias / schema / database、"
                          "ephemeral 或停用", True, sources_uncertain, models_block, concat, unset)
    return name, None, True, sources_uncertain, models_block, concat, unset


def _sets_names(node, names: bool = True) -> bool:
    """dbt_project.yml 的設定區塊裡,有沒有任何會改變表名的設定(值不是子目錄的物件):
    alias / schema / database(names=False 時不看:models 區塊的這三個由 _project_naming
    逐一照資料夾算)、materialized 為 ephemeral / 樣板值,或 enabled 不是 true。
    記住走過的節點:YAML 別名讓同一個物件可以被引用很多次,不重複走。"""
    seen: set[int] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, dict):
            for key, value in cur.items():
                if names and key in _NAME_KEYS and not isinstance(value, dict):
                    return True
                if (key in _MATERIALIZED_KEYS and not isinstance(value, dict)
                        and _materialization_unknown(value)):
                    return True
                if key in _ENABLED_KEYS and not isinstance(value, dict) and _maybe_disabled(value):
                    return True
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(cur, list):
            stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return False


def _read_properties(text: str, sources: dict, uncertain_sources: set,
                     uncertain_models: set, budget: _Budget, naming: dict) -> None:
    doc = _load(text)
    if doc is None:
        return                              # 空的屬性檔
    if not isinstance(doc, dict):
        raise _Unreadable
    for src in _list(doc.get("sources")):
        budget.spend()
        if not isinstance(src, dict) or not _plain_name(src.get("name")):
            raise _Unreadable
        schema = src.get("schema")
        if schema is not None and not isinstance(schema, str):
            raise _Unreadable
        for table in _list(src.get("tables")):
            budget.spend()
            if not isinstance(table, dict) or not _plain_name(table.get("name")):
                raise _Unreadable
            identifier = table.get("identifier")
            if identifier is not None and not isinstance(identifier, str):
                raise _Unreadable
            key = (src["name"], table["name"])
            if (_templated(schema) or _templated(identifier)
                    or _source_maybe_disabled(src) or _source_maybe_disabled(table)):
                uncertain_sources.add(key)
            # 樣板值不採用(展開不了),退回 dbt 的預設;資料庫名一律不讀(見 DbtProjectInfo)
            sources[key] = (None if _templated(schema) else schema,
                            None if _templated(identifier) else identifier)
    # seed / snapshot 的屬性同樣可以設 alias 等,被 ref() 時一樣受影響(它們的表名本來就無法確定)
    for section in ("models", "seeds", "snapshots"):
        for model in _list(doc.get(section)):
            budget.spend()
            if not isinstance(model, dict) or not _plain_name(model.get("name")):
                raise _Unreadable
            name, config = model["name"], model.get("config")
            # 逐一查固定的幾個鍵,不把整個 config 轉成集合:被別名共用的大 config 不會放大工作量
            if section != "models":
                if (isinstance(config, dict) and _has_name_key(config)) or _has_name_key(model):
                    uncertain_models.add(name)
                continue
            # model:config: 裡的 alias / schema / database 照值算;寫在 config: 外面、版本、
            # snapshot 的鍵、ephemeral、停用,或同一個 model 在屬性檔出現兩次,都無法確定
            values = _config_values(config) if config is not None else {}
            if (values is None or _has_name_key(model) or "versions" in model
                    or (isinstance(config, dict) and _has_other_key(config)) or name in naming):
                uncertain_models.add(name)
                naming.setdefault(name, {})
                continue
            naming[name] = values


def _config_values(config) -> dict | None:
    """屬性檔 config: 區塊設的 alias / schema / database;不是物件或值看不懂回 None。"""
    if not isinstance(config, dict):
        return None
    out = {}
    for key in _NAMING:
        if key in config:
            value = _literal(config[key])
            if value is _UNSET:
                return None
            out[key] = value
    return out


def _has_other_key(mapping: dict) -> bool:
    """alias / schema / database 以外,會讓表名無法確定的鍵:snapshot 的 target_*、加了 +
    的寫法,或 _unusable 的那幾種。"""
    return any(key in mapping for key in _OTHER_NAME_KEYS) or _unusable(mapping)


def _has_name_key(mapping: dict) -> bool:
    """有會改變表名的鍵:alias / schema / database(含 + 前綴與 snapshot 的 target_*),
    或 _unusable 的那幾種。"""
    return any(key in mapping for key in _ENTRY_NAME_KEYS) or _unusable(mapping)


def _unusable(mapping: dict) -> bool:
    """被引用時沒有表名可用:materialized 為 ephemeral / 樣板值,或 enabled 不是 true。"""
    return (any(key in mapping and _materialization_unknown(mapping[key])
                for key in _MATERIALIZED_KEYS)
            or any(key in mapping and _maybe_disabled(mapping[key]) for key in _ENABLED_KEYS))


def _list(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _Unreadable
    return value


def _plain_name(value) -> bool:
    return isinstance(value, str) and bool(value) and not _templated(value)
