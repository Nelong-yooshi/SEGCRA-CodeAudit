"""dbt 的名稱與設定是否符合 Dagster 的命名規則(部署端用 Dagster 載入 dbt 專案)。

不符合時 Dagster 整個載入失敗,影響的不只那一個 model。規則分三種(Dagster 1.13 + dagster-dbt 0.29
實測,並對照兩者的原始碼:資產名稱來自 dagster_dbt.asset_utils.default_asset_key_fn,group 來自
default_group_from_dbt_resource_props,資料檢查的名稱是測試的名稱;檢查用的正規式在
dagster._core.definitions.utils):

| 種類 | 來源 | 可以用 |
|---|---|---|
| 資產名稱 | model、seed、snapshot 的名稱;source 名稱與表名;`schema` 設定;帶版本 model 的 `alias`(沒設定時是 `<名稱>_v<版本>`);`meta.dagster.asset_key` | 英數字、底線、`-`、`.` |
| group | 屬性檔 `groups:` 的名稱;`group` 設定;`meta.dagster.group` | 英數字、底線,可用 `/` 分段 |
| 資料檢查 | 測試自訂的 `name:`;test-paths 底下單一測試的檔名 | 英數字、底線、`.` |

設定可以寫在三個地方,都要看:SQL(與 Python model)的 `config()`、屬性檔(`config:`、`meta:`、
`groups:`、`versions:`),以及 `dbt_project.yml`(`+schema`、`+group`、`+meta`,可以設在整個資料夾)。

不受影響、不檢查的:目錄名稱、snapshot 檔名、macro、沒有版本的 `alias`、欄位名稱、自動產生的測試名稱、
`accepted_values` 的值、tags、exposure、group owner、說明文字。

值是樣板(`env_var()`、`var()`)、寫法看不懂、或不知道 model 有沒有版本(決定 `alias` 算不算)時,
不猜:回報看不出來的原因(固定文字),由呼叫端補 major。部署端如果自訂了 Dagster 的命名,規則會跟著變。
"""
import ast
import re
from dataclasses import dataclass, field

from .dbt_config_change import MAX_TEXT_CHARS
from .dbt_project import (_SNAPSHOT_BLOCK, _SNAPSHOT_TAG, DEFAULT_MODEL_PATHS, DEFAULT_SEED_PATHS,
                          DEFAULT_SNAPSHOT_PATHS, DEFAULT_TEST_PATHS, _under)
from .dbt_relations import _CONFIG_SET, MAX_PROPERTY_ENTRIES, _config_bodies, _load, _templated

ASSET = "asset"            # 資產名稱
GROUP = "group"            # group
CHECK = "check"            # 資料檢查的名稱

# dagster-dbt 把資產名稱的 - 與 . 換成底線之後,要符合 ^[A-Za-z0-9_]+$
_RULES = {
    ASSET: re.compile(r"[A-Za-z0-9_.\-]+"),
    GROUP: re.compile(r"[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*"),
    CHECK: re.compile(r"[A-Za-z0-9_.]+"),
}
RULE_TEXT = {
    ASSET: "資產名稱只能用英數字、底線、`-`、`.`",
    GROUP: "group 只能用英數字與底線,可以用 `/` 分段(`-`、`.` 都不行)",
    CHECK: "資料檢查的名稱只能用英數字、底線、`.`(`-` 不行)",
}

PROJECT_FILE = "dbt_project.yml"
_YAML_SUFFIXES = (".yml", ".yaml")
_RESOURCE_SECTIONS = ("models", "seeds", "snapshots", "sources", "data_tests", "tests")
_TEST_KEYS = ("data_tests", "tests")
# config() 裡跟名稱有關的參數;其他參數寫成什麼都不影響
_NAME_CONFIG_KEYS = frozenset({"schema", "group", "meta", "alias", "target_schema"})
_NAME_KEY_TOKEN = re.compile(r"\b(?:schema|group|meta|alias|target_schema)\b")
_GENERIC_TEST_BLOCK = re.compile(r"\{%-?\s*test\b")
_MAX_DEPTH = 64


@dataclass(frozen=True)
class Layout:
    """dbt 專案的目錄(相對專案根目錄),與哪些 model 帶版本。

    versioned:專案裡宣告了 `versions:` 的 model 名稱;None = 不知道(沒有取回專案)。"""
    model_paths: tuple[str, ...] = DEFAULT_MODEL_PATHS
    seed_paths: tuple[str, ...] = DEFAULT_SEED_PATHS
    snapshot_paths: tuple[str, ...] = DEFAULT_SNAPSHOT_PATHS
    test_paths: tuple[str, ...] = DEFAULT_TEST_PATHS
    versioned: frozenset | None = None


@dataclass
class FileNames:
    """一個變更檔的檢查結果。bad 是 (規則, 種類, 名稱);unknown 是看不出來的原因(固定文字)。"""
    path: str
    bad: list = field(default_factory=list)
    unknown: str | None = None


def valid_name(rule: str, name) -> bool:
    return isinstance(name, str) and bool(_RULES[rule].fullmatch(name))


def valid_asset_name(name) -> bool:
    return valid_name(ASSET, name)


def _stem(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return name.rpartition(".")[0]


class _Unclear(Exception):
    """看不出名稱或設定值;訊息是固定文字。"""


class _Found:
    """收集 (規則, 種類, 名稱),有筆數上限(擋 YAML 別名炸彈)。"""

    def __init__(self):
        self.items: list = []

    def add(self, rule: str, kind: str, value) -> None:
        name = _value(value)
        if name is not None:
            self.items.append((rule, kind, name))
        if len(self.items) > MAX_PROPERTY_ENTRIES:
            raise _Unclear(f"項目超過 {MAX_PROPERTY_ENTRIES} 筆,不解析")


def _value(value):
    """設定值 → 要檢查的字串;沒設定回 None。樣板值與看不懂的型別丟 _Unclear。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _Unclear("名稱或設定值的型別看不懂")
    if isinstance(value, str) and (_templated(value) or "{" in value):
        raise _Unclear("名稱或設定值是樣板(例如 env_var()、var())")
    return str(value)


def check_file(path: str, rel: str, layout: Layout, text) -> FileNames | None:
    """一個沒有被刪掉的變更檔。rel 是相對 dbt 專案根目錄的路徑;text 是變更後的完整內容
    (拿不到是 None,只有檔名的檢查不需要它)。不是要檢查的檔案回 None。"""
    if rel == PROJECT_FILE:
        return _with_text(path, text, lambda t: _project_file(t, layout))
    if _under(rel, layout.model_paths) and rel.endswith(".sql"):
        return _with_text(path, text, lambda t: _model_config(rel, t, layout, _config_dicts_sql),
                          names_first=[(ASSET, "model", _stem(rel))])
    if _under(rel, layout.model_paths) and rel.endswith(".py"):
        return _with_text(path, text, lambda t: _model_config(rel, t, layout, _config_dicts_py),
                          names_first=[(ASSET, "model", _stem(rel))])
    if _under(rel, layout.seed_paths) and rel.endswith(".csv"):
        return _names(path, [(ASSET, "seed", _stem(rel))])
    if _under(rel, layout.snapshot_paths) and rel.endswith(".sql"):
        return _with_text(path, text, _snapshot_sql)
    if _under(rel, layout.test_paths) and rel.endswith(".sql"):
        return _with_text(path, text, lambda t: _singular_test(rel, t))
    if rel.endswith(_YAML_SUFFIXES) and any(_under(rel, d) for d in (
            layout.model_paths, layout.seed_paths, layout.snapshot_paths, layout.test_paths)):
        return _with_text(path, text, _property_file)
    return None


def _names(path: str, found: list) -> FileNames:
    return FileNames(path, bad=[(rule, kind, name) for rule, kind, name in found
                                if not valid_name(rule, name)])


def _with_text(path: str, text, read, names_first: list | None = None) -> FileNames:
    """names_first:不需要內容就能查的名稱(檔名);內容拿不到或看不懂時照樣報。"""
    result = _names(path, names_first or [])
    if not isinstance(text, str):
        result.unknown = "拿不到變更後的完整內容"
        return result
    if len(text) > MAX_TEXT_CHARS:
        result.unknown = f"內容超過 {MAX_TEXT_CHARS} 字元,不解析"
        return result
    try:
        found = read(text)
    except _Unclear as e:
        result.unknown = str(e)
        return result
    except RecursionError:
        result.unknown = "巢狀太深,不解析"
        return result
    result.bad += _names(path, found).bad
    return result


# ─────────────────── SQL / Python 的 config() ───────────────────

def _config_dicts_sql(text: str) -> list[dict]:
    """SQL 裡每個 config() 的參數(只留跟名稱有關的)。"""
    if _CONFIG_SET.search(text) and _NAME_KEY_TOKEN.search(text):
        raise _Unclear("用 config.set 設定,看不出值")
    bodies = _config_bodies(text)
    if bodies is None:
        if _NAME_KEY_TOKEN.search(text):
            raise _Unclear("config() 的括號沒有對上")
        return []
    out = []
    for body in bodies:
        try:
            tree = ast.parse(f"_({body})", mode="eval")
        except (SyntaxError, ValueError, MemoryError):
            if _NAME_KEY_TOKEN.search(body):
                raise _Unclear("config() 的參數看不懂") from None
            continue
        out.append(_call_kwargs(tree.body))
    return out


def _config_dicts_py(text: str) -> list[dict]:
    """Python model 裡 dbt.config(...) 的參數。"""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, MemoryError):
        if _NAME_KEY_TOKEN.search(text):
            raise _Unclear("Python model 解析不了") from None
        return []
    return [_call_kwargs(node) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "config"]


def _call_kwargs(call) -> dict:
    """config(...) 的參數 → {名稱: 常值}。跟名稱有關的參數不是常值時丟 _Unclear。"""
    if not isinstance(call, ast.Call):
        raise _Unclear("config() 的參數看不懂")
    pairs = [(k.arg, k.value) for k in call.keywords]
    for arg in call.args:                         # config({'schema': 'x'})
        if not isinstance(arg, ast.Dict):
            raise _Unclear("config() 的參數看不懂")
        for key, value in zip(arg.keys, arg.values):
            if key is None:                       # {**x}
                raise _Unclear("config() 用 ** 展開參數,看不出值")
            pairs.append((_literal(key), value))
    out = {}
    for key, node in pairs:
        if key is None:                           # config(**kwargs)
            raise _Unclear("config() 用 ** 展開參數,看不出值")
        if not isinstance(key, str) or key not in _NAME_CONFIG_KEYS:
            continue
        value = _literal(node)
        if value is _NOT_LITERAL:
            raise _Unclear(f"config() 的 {key} 是樣板或運算式,看不出值")
        out[key] = value
    return out


_NOT_LITERAL = object()
_JINJA_NAMES = {"true": True, "false": False, "none": None,
                "True": True, "False": False, "None": None}


def _literal(node):
    """常值(Jinja 的 true / false / none 也算);不是常值回 _NOT_LITERAL。"""
    if isinstance(node, ast.Name) and node.id in _JINJA_NAMES:
        return _JINJA_NAMES[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        items = [_literal(e) for e in node.elts]
        return _NOT_LITERAL if any(i is _NOT_LITERAL for i in items) else items
    if isinstance(node, ast.Dict):
        if any(k is None for k in node.keys):
            return _NOT_LITERAL
        keys = [_literal(k) for k in node.keys]
        values = [_literal(v) for v in node.values]
        if any(x is _NOT_LITERAL for x in keys + values):
            return _NOT_LITERAL
        try:
            return dict(zip(keys, values))
        except TypeError:                         # 不能當 key 的型別
            return _NOT_LITERAL
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, (int, float))):
        return -node.operand.value
    return _NOT_LITERAL


def _model_versioned(rel: str, layout: Layout):
    """這個 model 檔有沒有版本:True / False / None(不知道)。帶版本的 model 檔預設叫
    `<名稱>_v<版本>`,但也可以用 defined_in 指定任意檔名,所以專案有帶版本的 model、
    而檔名對不上任何一個時算不知道。"""
    if layout.versioned is None:
        return None
    if not layout.versioned:
        return False
    stem = _stem(rel)
    if any(stem == n or stem.startswith(n + "_v") for n in layout.versioned):
        return True
    return None


def _model_config(rel: str, text: str, layout: Layout, read) -> list:
    found = _Found()
    for cfg in read(text):
        _from_config(cfg, found, alias=_model_versioned(rel, layout))
    return found.items


def _snapshot_sql(text: str) -> list:
    names = [m.group(1) for m in _SNAPSHOT_BLOCK.finditer(text)]
    if len(names) != len(_SNAPSHOT_TAG.findall(text)):
        raise _Unclear("有 snapshot 區塊的寫法看不出名稱")
    found = _Found()
    for n in names:
        if _templated(n) or "{" in n:
            raise _Unclear("snapshot 的名稱是樣板")
        found.add(ASSET, "snapshot", n)
    for cfg in _config_dicts_sql(text):
        _from_config(cfg, found, alias=False)
    return found.items


def _singular_test(rel: str, text: str) -> list:
    """test-paths 底下的單一測試:檔名就是資料檢查的名稱。generic test 的定義(`{% test %}`
    區塊,慣例放在 generic/ 底下)不是單一測試。"""
    if "/generic/" in f"/{rel}" or _GENERIC_TEST_BLOCK.search(text):
        return []
    return [(CHECK, "單一測試的檔名", _stem(rel))]


# ─────────────────── 設定值 ───────────────────

def _from_config(cfg, found: _Found, alias, where: str = "") -> None:
    """一份設定(config() 的參數、屬性檔的 config:、dbt_project.yml 的 +設定)。
    alias:True = 帶版本的 model(alias 是資產名稱);False = 不看;None = 不知道有沒有版本。"""
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        raise _Unclear("設定的形狀看不懂")
    for key, value in cfg.items():
        name = key[1:] if isinstance(key, str) and key.startswith("+") else key
        if name == "schema":
            found.add(ASSET, f"{where}schema 設定", value)
        elif name == "group":
            found.add(GROUP, f"{where}group 設定", value)
        elif name == "meta":
            _from_meta(value, found, where)
        elif name == "target_schema":
            text = _value(value)
            if text is not None and not valid_name(ASSET, text):
                raise _Unclear("snapshot 的 target_schema 不符合資產名稱的規則,"
                               "不確定會不會變成 Dagster 名稱")
        elif name == "alias" and alias is not False:
            text = _value(value)
            if alias is None and text is not None and not valid_name(ASSET, text):
                raise _Unclear("alias 不符合資產名稱的規則,而看不出這個 model 有沒有版本"
                               "(帶版本的 model,alias 會變成 Dagster 名稱)")
            if alias:
                found.add(ASSET, f"{where}帶版本 model 的 alias", value)


def _from_meta(meta, found: _Found, where: str = "") -> None:
    if meta is None:
        return
    if not isinstance(meta, dict):
        raise _Unclear("meta 的形狀看不懂")
    dagster = meta.get("dagster")
    if dagster is None:
        return
    if not isinstance(dagster, dict):
        raise _Unclear("meta.dagster 的形狀看不懂")
    key = dagster.get("asset_key")
    for part in key if isinstance(key, list) else [key]:
        found.add(ASSET, f"{where}meta.dagster.asset_key", part)
    found.add(GROUP, f"{where}meta.dagster.group", dagster.get("group"))


# ─────────────────── 屬性檔 ───────────────────

def _load_mapping(text: str, what: str):
    try:
        doc = _load(text)
    except Exception:                       # noqa: BLE001 — 解析不了就是看不出名稱
        raise _Unclear(f"{what}解析不了") from None
    if doc is not None and not isinstance(doc, dict):
        raise _Unclear(f"{what}的形狀看不懂")
    return doc


def _items(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise _Unclear("屬性檔的形狀看不懂")
    return value


def _name(entry: dict) -> str:
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise _Unclear("屬性檔有沒寫名稱的項目")
    if _templated(name):
        raise _Unclear("名稱是樣板")
    return name


def _property_file(text: str) -> list:
    doc = _load_mapping(text, "屬性檔")
    if doc is None:
        return []
    found = _Found()
    for group in _items(doc.get("groups")):
        found.add(GROUP, "groups 的名稱", _name(group))
    for model in _items(doc.get("models")):
        name = _name(model)
        _entry_settings(model, found)
        _versions(name, model, found)
        _tests_of(model, found)
    for kind in ("seeds", "snapshots"):
        for entry in _items(doc.get(kind)):
            name = _name(entry)
            if kind == "snapshots":
                found.add(ASSET, "snapshot", name)
            _entry_settings(entry, found)
            _tests_of(entry, found)
    for src in _items(doc.get("sources")):
        found.add(ASSET, "source", _name(src))
        _entry_settings(src, found)
        for table in _items(src.get("tables")):
            found.add(ASSET, "source 的表", _name(table))
            _entry_settings(table, found)
            _tests_of(table, found)
    return found.items


def _entry_settings(entry: dict, found: _Found) -> None:
    """屬性檔一個項目的 config:、meta:、group:(沒有版本的 alias 不影響,不看)。"""
    _from_config(entry.get("config"), found, alias=False)
    _from_meta(entry.get("meta"), found)
    found.add(GROUP, "group 設定", entry.get("group"))


def _versions(name: str, model: dict, found: _Found) -> None:
    """帶版本的 model:資產名稱是 alias,沒設定時是 `<名稱>_v<版本>`。"""
    versions = model.get("versions")
    if versions is None:
        return
    base = model.get("config")
    base_alias = base.get("alias") if isinstance(base, dict) else None
    for ver in _items(versions):
        v = _value(ver.get("v"))
        if v is None:
            raise _Unclear("versions 有沒寫版本號的項目")
        cfg = ver.get("config")
        if cfg is not None and not isinstance(cfg, dict):
            raise _Unclear("設定的形狀看不懂")
        cfg = cfg or {}
        alias = cfg.get("alias", ver.get("alias", base_alias))
        found.add(ASSET, "帶版本 model 的名稱", alias if alias is not None else f"{name}_v{v}")
        _from_config({k: val for k, val in cfg.items() if k != "alias"}, found, alias=False)
        _from_meta(ver.get("meta"), found)
        _tests_of(ver, found)


def _tests_of(entry: dict, found: _Found) -> None:
    """項目與欄位上的測試:自訂的 name 是資料檢查的名稱。"""
    for holder in [entry] + _items(entry.get("columns")):
        for key in _TEST_KEYS:
            tests = holder.get(key)
            if tests is None:
                continue
            if not isinstance(tests, list):
                raise _Unclear("測試的形狀看不懂")
            for test in tests:
                _test_name(test, found)


def _test_name(test, found: _Found) -> None:
    if isinstance(test, str):
        return                                   # 沒有自訂名稱:自動產生的名稱不受影響
    if not isinstance(test, dict):
        raise _Unclear("測試的形狀看不懂")
    if "test_name" in test:                      # - name: x / test_name: unique
        found.add(CHECK, "測試名稱", test.get("name"))
        return
    if len(test) != 1:
        raise _Unclear("測試的形狀看不懂")
    (args,) = test.values()                      # - unique: {name: x, ...}
    if args is None:
        return
    if not isinstance(args, dict):
        raise _Unclear("測試的形狀看不懂")
    found.add(CHECK, "測試名稱", args.get("name"))
    cfg = args.get("config")
    if cfg is not None and not isinstance(cfg, dict):
        raise _Unclear("測試的形狀看不懂")
    found.add(CHECK, "測試名稱", (cfg or {}).get("name"))


# ─────────────────── dbt_project.yml ───────────────────

def _project_file(text: str, layout: Layout) -> list:
    doc = _load_mapping(text, "dbt_project.yml ")
    if doc is None:
        return []
    found = _Found()
    for section in _RESOURCE_SECTIONS:
        # 整個資料夾的 +alias:帶版本的 model 才算;專案沒有帶版本的 model 時不看
        alias = (None if layout.versioned is None or layout.versioned else False) \
            if section == "models" else False
        _folder(doc.get(section), found, alias, f"dbt_project.yml 的 {section} ", 0)
    return found.items


def _folder(node, found: _Found, alias, where: str, depth: int) -> None:
    """dbt_project.yml 的一層:`+` 開頭的是設定;沒加 `+` 時,值是 mapping 就是子資料夾
    (或套件),否則是沒加 `+` 的設定。"""
    if node is None:
        return
    if not isinstance(node, dict):
        raise _Unclear("dbt_project.yml 的設定形狀看不懂")
    if depth > _MAX_DEPTH:
        raise _Unclear("dbt_project.yml 的資料夾層數太深,不解析")
    settings = {}
    for key, value in node.items():
        if not isinstance(key, str):
            raise _Unclear("dbt_project.yml 的設定形狀看不懂")
        if key.startswith("+") or not isinstance(value, dict) or (key == "meta"
                                                                  and not _is_folder(value)):
            settings[key] = value
        else:
            _folder(value, found, alias, where, depth + 1)
    _from_config(settings, found, alias, where)


def _is_folder(value) -> bool:
    """沒加 `+` 的 meta:值裡有 `+` 開頭的鍵,就是名叫 meta 的資料夾。"""
    return isinstance(value, dict) and any(isinstance(k, str) and k.startswith("+") for k in value)


def versioned_models(files: dict, layout: Layout) -> frozenset | None:
    """專案裡宣告了 `versions:` 的 model 名稱。files 是取回的專案內容 {相對路徑: 內容};
    有屬性檔看不懂時回 None(不知道)。"""
    names = set()
    for rel, text in files.items():
        if not (isinstance(rel, str) and rel.endswith(_YAML_SUFFIXES)
                and _under(rel, layout.model_paths) and isinstance(text, str)):
            continue
        if "versions" not in text:
            continue
        if len(text) > MAX_TEXT_CHARS:
            return None
        try:
            doc = _load_mapping(text, "屬性檔")
            for model in _items((doc or {}).get("models")):
                if model.get("versions") is not None:
                    names.add(_name(model))
        except (_Unclear, RecursionError):
            return None
    return frozenset(names)
