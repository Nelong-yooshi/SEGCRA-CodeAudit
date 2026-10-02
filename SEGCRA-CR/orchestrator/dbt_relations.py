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

不處理(一律視為無法確定,保留提醒):dbt 套件(packages.yml / dependencies.yml)
的 model 與 source、會改變表名的設定(alias、自訂 schema、generate_*_name、
樣板值)、ephemeral(被引用時展開成 CTE,不是表名)、停用(enabled 不是 true)、
model 版本(versions)、同名的 model、Python model(內容不讀)、macro 裡呼叫的 config()。
seed / snapshot / Python model 的名稱由呼叫端提供(打包下載只收 .sql / .yml 的內容);
沒提供時 ref() 找不到也不能判定不存在。
"""
import re

import yaml

from .dbt_render import DbtProjectInfo

_PACKAGE_FILES = ("packages.yml", "dependencies.yml")
# 會改變 ref() 展開出的表名的設定鍵(dbt_project.yml 用 + 前綴,屬性檔不用)
_NAME_KEYS = frozenset({"alias", "schema", "database", "+alias", "+schema", "+database"})
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
                        (files 裡若有 model 目錄下的 .py 路徑,只取檔名,不讀內容)
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
    project_name, global_uncertain, project_ok, all_sources_uncertain = _read_dbt_project(
        files.get("dbt_project.yml"))
    # 沒確認過根目錄的檔案,就當作「可能有套件」
    has_packages = (root_files_checked is not True
                    or any(name in files for name in _PACKAGE_FILES))

    models: set[str] = set()
    model_files: dict[str, int] = {}        # 名稱 → 檔案數(同名的 model dbt 會編譯失敗)
    uncertain_models: set[str] = set()
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
            if _config_may_rename(content):
                uncertain_models.add(name)
        elif _under(path, model_dirs) and lower.endswith((".yml", ".yaml")):
            try:
                _read_properties(content, sources, uncertain_sources, uncertain_models, budget)
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
    if all_sources_uncertain:
        # dbt_project.yml 的 sources 區塊有停用等設定,或 dbt_project.yml 看不懂
        uncertain_sources.update(sources)

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
    )


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


def _config_may_rename(text: str) -> bool:
    """config() 會不會讓被引用時的表名不是 model 名稱。寧可多判(只是保留提醒),不可漏判:
    每個參數都必須是「名稱 = 常值」,名稱不是 alias / schema / database,materialized 也
    不是 ephemeral(被引用時展開成 CTE),enabled 不是 false(停用的 model 被引用會編譯
    失敗)——否則一律判成可能會改。
    `config({'alias': 'x'})`、`config(**c)`、`config(materialized=var('m'))`、
    沒有右括號的 `config(`,都看不懂 → 會改。"""
    bodies = _config_bodies(text)
    if bodies is None:
        return True
    for body in bodies:
        pos = 0
        # 從 pos 往後找有沒有剩下的內容,不切片複製(切片會讓參數很多時變成平方時間)
        while _NON_SPACE.search(body, pos):
            m = _CONFIG_ARG.match(body, pos)
            if m is None:
                return True
            key, value = m.group(1).lower(), m.group(2)
            if key in _NAME_KEYS or (key == "materialized"
                                     and value.strip("'\"").lower() == "ephemeral"):
                return True
            if key == "enabled" and value.lower() != "true":
                return True                 # 停用的 model 被 ref() 時 dbt 會編譯失敗
            pos = m.end()
    return False


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
    """回傳 (專案名稱, ref() 的專案層級不確定原因, 是否讀得懂, source() 是否全部無法確定)。
    沒有 dbt_project.yml 也算讀不懂;讀不懂時 sources 區塊的設定(例如停用)也看不到。"""
    if text is None:
        return None, None, False, True
    try:
        raw = _load(text)
    except _Unreadable:
        return None, "dbt_project.yml 無法解析", False, True
    if not isinstance(raw, dict):
        return None, "dbt_project.yml 無法解析", False, True
    name = raw.get("name") if isinstance(raw.get("name"), str) else None
    sources_uncertain = _sets_names(raw.get("sources"))
    for block in ("models", "seeds", "snapshots", "sources"):
        if _sets_names(raw.get(block)):
            return (name, f"dbt_project.yml 的 {block} 設定了 alias / schema / database、"
                          "ephemeral 或停用", True, sources_uncertain)
    return name, None, True, sources_uncertain


def _sets_names(node) -> bool:
    """dbt_project.yml 的設定區塊裡,有沒有任何會改變表名的設定(值不是子目錄的物件):
    alias / schema / database、materialized 為 ephemeral / 樣板值,或 enabled 不是 true。
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
                if key in _NAME_KEYS and not isinstance(value, dict):
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
                     uncertain_models: set, budget: _Budget) -> None:
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
    # seed / snapshot 的屬性同樣可以設 alias 等,被 ref() 時一樣受影響
    for model in (*_list(doc.get("models")), *_list(doc.get("seeds")), *_list(doc.get("snapshots"))):
        budget.spend()
        if not isinstance(model, dict) or not _plain_name(model.get("name")):
            raise _Unreadable
        config = model.get("config")
        # 逐一查固定的幾個鍵,不把整個 config 轉成集合:被別名共用的大 config 不會放大工作量
        if ((isinstance(config, dict) and _has_name_key(config))
                or _has_name_key(model) or "versions" in model):
            uncertain_models.add(model["name"])


def _has_name_key(mapping: dict) -> bool:
    """有會改變表名的鍵:alias / schema / database、materialized 為 ephemeral / 樣板值,
    或 enabled 不是 true。"""
    return (any(key in mapping for key in _NAME_KEYS)
            or any(key in mapping and _materialization_unknown(mapping[key])
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
