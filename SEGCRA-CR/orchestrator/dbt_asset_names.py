"""dbt 名稱是否符合 Dagster 的資產命名規則(部署端用 Dagster 載入 dbt 專案)。

Dagster 的資產名稱只接受 `^[A-Za-z0-9_]+$`(Dagster 1.13 的錯誤訊息:
`"…" is not a valid name in Dagster. Names must be in regex ^[A-Za-z0-9_]+$.`)。dagster-dbt 會先把
`-` 與 `.` 換成底線,所以這兩個字元也可以。不符合時 Dagster 整個載入失敗,影響的不只那一個 model。

會變成 Dagster 資產名稱、要檢查的(在 Dagster 1.13 與 dagster-dbt 上實測):
  * model 的檔名(model 目錄下的 .sql / .py,不含目錄與副檔名)
  * seed 的檔名(seed 目錄下的 .csv)
  * model 目錄下屬性檔裡 `sources:` 的 source 名稱與表名
  * snapshot 的名稱(snapshot 目錄下 .sql 的 `{% snapshot 名稱 %}`,與屬性檔的 `snapshots:`);
    名稱有中文時 dbt parse 就失敗
不受影響、不檢查的:目錄名稱、snapshot 檔名、macro 檔名與名稱、alias、欄位名稱、測試、說明文字、
dbt 以外的檔案。檔名有空白時 dbt parse 就失敗,也不符合上面的正規式。

部署端如果自訂了 Dagster 的資產命名,規則會跟著變。
"""
import re
from dataclasses import dataclass, field

from .dbt_config_change import MAX_TEXT_CHARS
from .dbt_project import DEFAULT_MODEL_PATHS, DEFAULT_SEED_PATHS, DEFAULT_SNAPSHOT_PATHS, _under
from .dbt_relations import MAX_PROPERTY_ENTRIES, _load, _templated

# dagster-dbt 把 - 與 . 換成底線之後,要符合 ^[A-Za-z0-9_]+$
_ASSET_NAME = re.compile(r"[A-Za-z0-9_.\-]+")
_SNAPSHOT_TAG = re.compile(r"\{%-?\s*snapshot\b")
_SNAPSHOT_BLOCK = re.compile(r"\{%-?\s*snapshot\s+([^\s%]+)\s*-?%\}")
_YAML_SUFFIXES = (".yml", ".yaml")


@dataclass(frozen=True)
class Layout:
    """dbt 專案的 model / seed / snapshot 目錄(相對專案根目錄)。"""
    model_paths: tuple[str, ...] = DEFAULT_MODEL_PATHS
    seed_paths: tuple[str, ...] = DEFAULT_SEED_PATHS
    snapshot_paths: tuple[str, ...] = DEFAULT_SNAPSHOT_PATHS


@dataclass
class FileNames:
    """一個變更檔的檢查結果。bad 是 (種類, 名稱);unknown 是看不出名稱的原因(固定文字)。"""
    path: str
    bad: list = field(default_factory=list)
    unknown: str | None = None


def valid_asset_name(name) -> bool:
    return isinstance(name, str) and bool(_ASSET_NAME.fullmatch(name))


def _stem(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    return name.rpartition(".")[0]


def check_file(path: str, rel: str, layout: Layout, text) -> FileNames | None:
    """一個沒有被刪掉的變更檔。rel 是相對 dbt 專案根目錄的路徑;text 是變更後的完整內容
    (拿不到是 None,只有檔名的檢查不需要它)。不是要檢查的檔案回 None。"""
    if _under(rel, layout.model_paths) and rel.endswith((".sql", ".py")):
        return _names(path, [("model", _stem(rel))])
    if _under(rel, layout.seed_paths) and rel.endswith(".csv"):
        return _names(path, [("seed", _stem(rel))])
    if _under(rel, layout.snapshot_paths) and rel.endswith(".sql"):
        return _with_text(path, text, _snapshot_blocks)
    if rel.endswith(_YAML_SUFFIXES) and (_under(rel, layout.model_paths)
                                         or _under(rel, layout.snapshot_paths)):
        return _with_text(path, text, _yaml_names)
    return None


def _names(path: str, found: list) -> FileNames:
    return FileNames(path, bad=[(kind, name) for kind, name in found
                                if not valid_asset_name(name)])


def _with_text(path: str, text, read) -> FileNames:
    if not isinstance(text, str):
        return FileNames(path, unknown="拿不到變更後的完整內容")
    if len(text) > MAX_TEXT_CHARS:
        return FileNames(path, unknown=f"內容超過 {MAX_TEXT_CHARS} 字元,不解析")
    try:
        found = read(text)
    except _Unclear as e:
        return FileNames(path, unknown=str(e))
    return _names(path, found)


class _Unclear(Exception):
    """看不出名稱;訊息是固定文字。"""


def _snapshot_blocks(text: str) -> list:
    names = [m.group(1) for m in _SNAPSHOT_BLOCK.finditer(text)]
    if len(names) != len(_SNAPSHOT_TAG.findall(text)):
        raise _Unclear("有 snapshot 區塊的寫法看不出名稱")
    if any(_templated(n) or "{" in n for n in names):
        raise _Unclear("snapshot 的名稱是樣板")
    return [("snapshot", n) for n in names]


def _yaml_names(text: str) -> list:
    try:
        doc = _load(text)
    except Exception:                       # noqa: BLE001 — 解析不了就是看不出名稱
        raise _Unclear("屬性檔解析不了") from None
    if doc is None:
        return []
    if not isinstance(doc, dict):
        raise _Unclear("屬性檔的形狀看不懂")
    # 別名(&a / *a)可以讓同一份表清單被很多個 source 引用,短短的內容展開成大量項目:
    # 跟取回專案時解析屬性檔用同一個上限,超過就當成看不出名稱
    found = []
    for src in _items(doc.get("sources")):
        found.append(("source", _name(src)))
        for table in _items(src.get("tables")):
            found.append(("source 的表", _name(table)))
            _check_budget(found)
        _check_budget(found)
    for snap in _items(doc.get("snapshots")):
        found.append(("snapshot", _name(snap)))
        _check_budget(found)
    return found


def _check_budget(found: list) -> None:
    if len(found) > MAX_PROPERTY_ENTRIES:
        raise _Unclear(f"屬性檔的項目超過 {MAX_PROPERTY_ENTRIES} 筆,不解析")


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
