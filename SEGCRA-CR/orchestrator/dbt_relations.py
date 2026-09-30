"""dbt_relations — 從專案檔案整理 ref() / source() 需要的專案層級資訊。

輸出 dbt_render.DbtProjectInfo,交給 render_model(project=...):ref() 才能確認被引用的
model 存在、source() 才能套用 sources.yml 的 schema / identifier(PR #10 review 列的
三個精確度落差)。

檔案來自待審的 commit,是不可信內容。原則:
  * **看不懂就往「無法確定」靠**,不往「確定不存在」靠——後者會讓展開失敗、
    冤枉開發者;前者只是保留「表名未經驗證」的提醒
  * 純解析、不丟例外、不寫檔、不連網;YAML 一律 safe_load
  * YAML 的別名(&a / *a)可以讓同一段內容被引用成千上萬次:1 MB 的檔案就能讓
    「來源 × 表」展開成上億筆。設定區塊走訪時記住走過的節點;屬性檔的來源、表、model
    合計超過 MAX_PROPERTY_ENTRIES 筆就判成看不懂(無法確定),不會被「別名炸彈」拖垮

不處理(一律視為無法確定,保留提醒):dbt 套件(packages.yml / dependencies.yml)
的 model 與 source、會改變表名的設定(alias、自訂 schema、generate_*_name、
樣板值)、model 版本(versions)。seed / snapshot 名單由呼叫端提供(打包下載只收
.sql / .yml,拿不到 seed 的 .csv);沒提供時 ref() 找不到也不能判定不存在。
"""
import re

import yaml

from .dbt_render import DbtProjectInfo

_PACKAGE_FILES = ("packages.yml", "dependencies.yml")
# 會改變 ref() 展開出的表名的設定鍵(dbt_project.yml 用 + 前綴,屬性檔不用)
_NAME_KEYS = frozenset({"alias", "schema", "database", "+alias", "+schema", "+database"})
# 專案自訂 schema / alias / database 的命名規則:我們不模擬,只偵測
_GENERATE_NAME_MACRO = re.compile(r"\{%-?\s*macro\s+generate_(?:schema|alias|database)_name\s*\(")
# model 檔內的 {{ config(alias=...) }} 之類。寧可多判(只是保留提醒),不可漏判
_CONFIG_CALL = re.compile(r"\bconfig\s*\(")
_NAME_SETTING = re.compile(r"\b(?:alias|schema|database)\s*=")
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
    other_ref_names     seed / snapshot 名稱;None = 不知道(ref() 找不到時不能判定不存在)
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
    project_name, global_uncertain, project_ok = _read_dbt_project(files.get("dbt_project.yml"))
    # 沒確認過根目錄的檔案,就當作「可能有套件」
    has_packages = (root_files_checked is not True
                    or any(name in files for name in _PACKAGE_FILES))

    models: set[str] = set()
    uncertain_models: set[str] = set()
    sources: dict = {}
    uncertain_sources: set = set()
    properties_ok = True
    budget = _Budget(MAX_PROPERTY_ENTRIES)
    for path, content in sorted(files.items(), key=lambda kv: str(kv[0])):
        if not isinstance(path, str) or not isinstance(content, str):
            properties_ok = False
            continue
        lower = path.lower()
        if _under(path, model_dirs) and lower.endswith(".sql"):
            name = path.rsplit("/", 1)[-1][:-4]
            models.add(name)
            if _CONFIG_CALL.search(content) and _NAME_SETTING.search(content):
                uncertain_models.add(name)
        elif _under(path, model_dirs) and lower.endswith((".yml", ".yaml")):
            try:
                _read_properties(content, sources, uncertain_sources, uncertain_models, budget)
            except _Unreadable:
                properties_ok = False
        elif _under(path, macro_dirs) and lower.endswith(".sql"):
            if _GENERATE_NAME_MACRO.search(content):
                global_uncertain = global_uncertain or "專案自訂了 generate_*_name 命名規則"

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


def _templated(value) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _load(text: str):
    try:
        return yaml.safe_load(text)
    except Exception:                       # noqa: BLE001 — 看不懂就是看不懂
        raise _Unreadable from None


def _read_dbt_project(text):
    """回傳 (專案名稱, 專案層級的不確定原因, 是否讀得懂)。沒有 dbt_project.yml 也算讀不懂。"""
    if text is None:
        return None, None, False
    try:
        raw = _load(text)
    except _Unreadable:
        return None, "dbt_project.yml 無法解析", False
    if not isinstance(raw, dict):
        return None, "dbt_project.yml 無法解析", False
    name = raw.get("name") if isinstance(raw.get("name"), str) else None
    for block in ("models", "seeds", "snapshots", "sources"):
        if _sets_names(raw.get(block)):
            return name, f"dbt_project.yml 的 {block} 設定了 alias / schema / database", True
    return name, None, True


def _sets_names(node) -> bool:
    """dbt_project.yml 的設定區塊裡,有沒有任何會改變表名的鍵(值不是子目錄的物件)。
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
            if _templated(schema) or _templated(identifier):
                uncertain_sources.add(key)
            # 樣板值不採用(展開不了),退回 dbt 的預設;資料庫名一律不讀(見 DbtProjectInfo)
            sources[key] = (None if _templated(schema) else schema,
                            None if _templated(identifier) else identifier)
    for model in _list(doc.get("models")):
        budget.spend()
        if not isinstance(model, dict) or not _plain_name(model.get("name")):
            raise _Unreadable
        config = model.get("config")
        # 逐一查固定的幾個鍵,不把整個 config 轉成集合:被別名共用的大 config 不會放大工作量
        if ((isinstance(config, dict) and _has_name_key(config))
                or _has_name_key(model) or "versions" in model):
            uncertain_models.add(model["name"])


def _has_name_key(mapping: dict) -> bool:
    return any(key in mapping for key in _NAME_KEYS)


def _list(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _Unreadable
    return value


def _plain_name(value) -> bool:
    return isinstance(value, str) and bool(value) and not _templated(value)
