"""執行驗證的上游表:沿 ref() 推出上游 SQL model 與 snapshot 的表結構,補進規格的「資料表定義」。

執行驗證要先在沙盒建出規則 SQL 讀的表。ref() 的上游 model 由同名的測資表代替(見 dbt_exec),
表結構原本只能來自規格或 specs/tables/;上游 model 的輸出欄位不在資料字典裡,要人手寫。這裡改成
由 dbt 展開推出來:

  1. 以 collect 模式展開要驗的 model(dbt_exec.upstream_refs),找出規格裡沒有、要先建的上游。
  2. 沿 ref() 往上找,每個上游各展開一次(同樣的 collect 模式),排出相依順序。上游只能是 SQL
     model 或 snapshot;seed、Python model、其他套件、表名無法確定的,一律不建、寫出原因。
  3. 在沙盒建一個暫用的資料庫:先建規格(含 specs/tables/)裡的表,再依相依順序讓 SQL Server
     描述每個上游展開後的 SQL 會輸出哪些欄位與型別(sys.dm_exec_describe_first_result_set,
     不執行查詢),照著建出那張表,下一層就讀得到。snapshot 另外補 dbt 的管理欄位。
  4. 把推出的 CREATE TABLE 接到規格的「資料表定義」:測資生成與沙盒執行都照它。

只解決「表長什麼樣」:上游的邏輯照舊不在這次執行,測資直接放進上游表(跟規格寫的同名表一樣),
報告會寫明哪些表的結構是這樣推出來的。任何一步做不到就不補、寫出原因,執行驗證照舊回「尚未
支援」交人工,不猜。

snapshot 的管理欄位照 dbt-sqlserver 建 snapshot 表的寫法(`select *` 後面接 dbt_scd_id、
dbt_updated_at、dbt_valid_from、dbt_valid_to):dbt_scd_id 是 `CONVERT(VARCHAR(32), HashBytes('MD5', …), 2)`,
其他三個的型別都跟 strategy 的 updated_at 相同(timestamp 是 updated_at 那一欄;check 沒設
updated_at 時是 snapshot_get_time(),dbt-sqlserver 是 `CAST(SYSDATETIME() AS DATETIME2(6))`)。
會改變欄位名稱或多出欄位的設定(snapshot_table_column_names、dbt_valid_to_current、
hard_deletes = new_record)、會改表名的 alias、看不懂的設定,一律當成無法確定。
"""
from __future__ import annotations

import ast
import re
import uuid
from dataclasses import dataclass, field

import sqlglot
import yaml
from sqlglot import exp

from . import table_defs
from .dbt_exec import _tables_section, spec_tables, upstream_refs
from .dbt_render import model_alias
from toolbox.sqltools import DIALECT

# 一份審查最多建幾張上游表、往上追幾層。真實規則的上游是個位數;超過就不建(寫出原因)
MAX_UPSTREAM = 20
MAX_DEPTH = 8
MAX_COLUMNS = 300
SNAPSHOT_TIME_TYPE = "datetime2(6)"          # dbt-sqlserver 的 snapshot_get_time()
SCD_ID_TYPE = "varchar(32)"                  # dbt-sqlserver 的 snapshot_hash_arguments
SNAPSHOT_COLUMNS = ("dbt_scd_id", "dbt_updated_at", "dbt_valid_from", "dbt_valid_to")

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BLOCK = re.compile(r"\{%-?\s*snapshot\s+([^\s%]+)\s*-?%\}(.*?)\{%-?\s*endsnapshot\s*-?%\}", re.S)
_CONFIG_CALL = re.compile(r"\bconfig\s*\(")
_COLUMN_NAME = re.compile(r"[^\[\]\x00-\x1f]{1,128}")
_TYPE = re.compile(r"([a-z0-9]+)(\([0-9a-z, ]+\))?")
# 不影響表名與欄位的 snapshot 設定;其他的(alias、snapshot_table_column_names、dbt_valid_to_current、
# hard_deletes、enabled、不認得的)一律當成無法確定
_NEUTRAL_KEYS = frozenset({"strategy", "unique_key", "updated_at", "check_cols", "target_schema",
                           "target_database", "schema", "database", "tags", "meta", "docs",
                           "persist_docs", "grants", "invalidate_hard_deletes", "pre_hook",
                           "post_hook", "pre-hook", "post-hook", "quote_columns"})


class UpstreamError(Exception):
    """推不出上游表結構的原因(給人看的一句話)。"""


@dataclass
class Snapshot:
    name: str
    path: str
    body: str = ""
    table: str = ""                      # 表名:設了固定的 alias 是 alias,否則是區塊名稱
    updated_at: str | None = None        # 管理欄位的型別跟著它;None 表示用 snapshot_get_time()
    reason: str | None = None            # 表名或管理欄位無法確定的原因


@dataclass
class UpstreamResult:
    spec_text: str
    tables: list = field(default_factory=list)       # [{"name", "table", "kind", "file", "columns"}]
    snapshots: tuple = ()                            # 建出來、表名確定的 snapshot(展開時要帶)
    reason: str | None = None


# ------------------------------------------------------------------ snapshot
def _config_kwargs(body: str) -> dict | None:
    """snapshot 區塊裡 config(...) 的參數;沒有呼叫回 {},不只一次、參數不是常值、看不懂回 None。"""
    calls = list(_CONFIG_CALL.finditer(body))
    if not calls:
        return {}
    if len(calls) > 1:
        return None
    start = calls[0].end()
    depth, quote, i = 1, None, start
    while i < len(body) and depth:
        ch = body[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    if depth:
        return None
    try:
        call = ast.parse(f"f({body[start:i - 1]})", mode="eval").body
        if not isinstance(call, ast.Call) or call.args:
            return None
        out = {}
        for kw in call.keywords:
            if kw.arg is None:
                return None
            out[kw.arg] = ast.literal_eval(kw.value)
        return out
    except (SyntaxError, ValueError):
        return None


def _snapshot_reason(config: dict | None) -> tuple[str | None, str | None]:
    """(updated_at, 無法確定的原因)。config 是合併過專案設定、屬性檔、區塊 config() 的結果。"""
    if config is None:
        return None, "snapshot 的 config() 參數不是常值或寫法看不懂"
    unknown = sorted(str(k) for k in config if k not in _NEUTRAL_KEYS)
    if unknown:
        return None, f"snapshot 設了會改表名或欄位、或認不得的設定:{', '.join(unknown[:5])}"
    strategy = config.get("strategy")
    updated_at = config.get("updated_at")
    if updated_at is not None and not (isinstance(updated_at, str) and _IDENT.fullmatch(updated_at)):
        return None, "snapshot 的 updated_at 不是欄位名稱"
    if strategy == "timestamp":
        if updated_at is None:
            return None, "snapshot 用 timestamp,卻沒有寫 updated_at"
        return updated_at, None
    if strategy == "check":
        return updated_at, None
    return None, "snapshot 的 strategy 不是 timestamp 或 check(或沒有設定)"


def _project_config(root_files: dict, folders: list) -> dict | None:
    """dbt_project.yml 的 `snapshots:` 區段套到這個 snapshot 的設定(照 dbt:專案名底下一層層資料夾,
    越深的蓋掉越淺的;`+鍵` 或值不是物件的鍵是設定,值是物件的不帶 + 鍵是資料夾)。看不懂回 None。"""
    try:
        data = yaml.safe_load(root_files.get("dbt_project.yml") or "") or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("snapshots")
    if not block:
        return {}
    if not isinstance(block, dict):
        return None
    out: dict = {}
    node = block.get(data.get("name"))
    for folder in [None] + list(folders):
        if folder is not None:
            node = node.get(folder) if isinstance(node, dict) else None
        if node is None:
            break
        if not isinstance(node, dict):
            return None
        for key, value in node.items():
            if not isinstance(key, str):
                return None
            if key.startswith("+"):
                out[key[1:]] = value
            elif not isinstance(value, dict):
                out[key] = value
    return out


def _yml_config(files: dict, paths: tuple, name: str) -> dict | None:
    """屬性檔(yml)替這個 snapshot 設的 config;沒有回 {}。看不懂的 yml、用 yml 定義的 snapshot
    (有 `relation:`,dbt 1.9 起的寫法,沒有 SQL 區塊)回 None。"""
    out: dict = {}
    for path, text in sorted(files.items(), key=lambda kv: str(kv[0])):
        if not (isinstance(path, str) and path.lower().endswith((".yml", ".yaml"))
                and any(path == p or path.startswith(p.rstrip("/") + "/") for p in paths)):
            continue
        if not isinstance(text, str) or name not in text:
            continue
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
        entries = data.get("snapshots") if isinstance(data, dict) else None
        if entries is None:
            continue
        if not isinstance(entries, list):
            return None
        for e in entries:
            if not (isinstance(e, dict) and e.get("name") == name):
                continue
            config = e.get("config") or {}
            if e.get("relation") is not None or not isinstance(config, dict):
                return None
            out.update(config)
    return out


def _folders(path: str, paths: tuple) -> list:
    """snapshot 檔相對 snapshot 目錄的資料夾(不含檔名)。"""
    for p in paths:
        prefix = p.rstrip("/") + "/"
        if path.startswith(prefix):
            return path[len(prefix):].split("/")[:-1]
    return []


def find_snapshots(project) -> dict:
    """專案裡用 `{% snapshot %}` 區塊寫的 snapshot:{名稱: Snapshot}。同名的不只一個時記原因。

    設定照 dbt 的優先順序合併:dbt_project.yml 的 `snapshots:` < 屬性檔(yml)的 `config:` < 區塊裡的
    `config()`。合併後才判斷表名與管理欄位確不確定(_snapshot_reason)。"""
    out: dict = {}
    files = getattr(project, "files", None) or {}
    paths = tuple(getattr(project, "snapshot_paths", ()) or ())
    root_files = getattr(project, "root_files", None) or {}
    for path, text in sorted(files.items(), key=lambda kv: str(kv[0])):
        if not (isinstance(path, str) and isinstance(text, str) and path.lower().endswith(".sql")
                and any(path.startswith(p.rstrip("/") + "/") for p in paths)):
            continue
        for m in _BLOCK.finditer(text):
            name, body = m.group(1), m.group(2)
            if not _IDENT.fullmatch(name):
                continue
            if name in out:
                out[name] = Snapshot(name, path, reason=f"專案裡有不只一個叫 {name} 的 snapshot")
                continue
            layers = [("dbt_project.yml 的 snapshots: 區段看不懂",
                       _project_config(root_files, _folders(path, paths))),
                      ("屬性檔(yml)看不懂,或是用 yml 定義的 snapshot", _yml_config(files, paths, name)),
                      ("snapshot 的 config() 參數不是常值或寫法看不懂", _config_kwargs(body))]
            unreadable = [why for why, conf in layers if conf is None]
            if unreadable:
                updated_at, reason = None, unreadable[0]
            else:
                merged: dict = {}
                for _, conf in layers:
                    merged.update(conf)
                alias = merged.pop("alias", None)
                table = alias.strip() if isinstance(alias, str) else name
                updated_at, reason = _snapshot_reason(merged)
                if reason is None and alias is not None and not (isinstance(alias, str)
                                                                 and _IDENT.fullmatch(table)):
                    reason = "snapshot 的 alias 不是固定的表名"
            out[name] = Snapshot(name, path, body=body, table=table if not unreadable else name,
                                 updated_at=updated_at, reason=reason)
    return out


# ------------------------------------------------------------------ 規劃
def _model_file(project, name: str) -> str | None:
    """名稱是 name 的 SQL model 檔(model 目錄底下、檔名去掉 .sql);不是剛好一個回 None。"""
    files = getattr(project, "files", None) or {}
    paths = tuple(getattr(project, "model_paths", ()) or ())
    hits = [p for p in files
            if isinstance(p, str) and p.lower().endswith(".sql")
            and any(p.startswith(d.rstrip("/") + "/") for d in paths)
            and p.rsplit("/", 1)[-1][:-4] == name]
    return hits[0] if len(hits) == 1 else None


def _plan(target: tuple, missing: tuple, tables: dict, database: str, project_kwargs: dict,
          project, snapshots: dict, certain: tuple) -> list:
    """沿 ref() 排出要建的上游(相依順序):[(名稱, 表名, 種類, 檔案, 展開後的 SQL)]。"""
    info = project_kwargs.get("project")
    planned: dict = {}
    order: list = []

    def visit(name: str, depth: int, chain: tuple) -> None:
        if name in planned:
            return
        if name in chain:
            raise UpstreamError(f"上游的 ref() 繞回自己:{' → '.join(chain + (name,))}")
        if depth > MAX_DEPTH:
            raise UpstreamError(f"上游超過 {MAX_DEPTH} 層")
        if len(planned) >= MAX_UPSTREAM:
            raise UpstreamError(f"要建的上游超過 {MAX_UPSTREAM} 張")
        snap = snapshots.get(name)
        if snap is not None:
            if snap.reason:
                raise UpstreamError(f"snapshot {name} 的表結構無法確定:{snap.reason}")
            kind, path, source, table = "snapshot", snap.path, snap.body, snap.table
        else:
            path = _model_file(project, name)
            table = model_alias(info, name)
            if path is None or table is None:
                raise UpstreamError(f"ref() 指向的 {name} 不是表名確定的 SQL model 或 snapshot"
                                    f"(seed、Python model 等沿 ref() 建不出來)")
            kind, source = "model", (getattr(project, "files", {}) or {}).get(path, "")
        miss, why, sql = upstream_refs(path, source, tables, database, project_kwargs, certain)
        if miss is None:
            raise UpstreamError(f"上游 {name} 展開不了:{why}")
        for m in miss:
            visit(m, depth + 1, chain + (name,))
        planned[name] = True
        order.append((name, table, kind, path, sql))

    for m in missing:
        visit(m, 1, (target[0],))
    return order


# ------------------------------------------------------------------ 沙盒
class SandboxSession:
    """在沙盒建一個暫用的資料庫,建表、描述查詢的輸出欄位;用完整個刪掉。"""

    def __init__(self):
        from . import spec_exec
        params = spec_exec._sandbox_params()
        if not params.get("password"):
            raise UpstreamError("未設定 SANDBOX_MSSQL_PASSWORD,沒有沙盒可以推上游表結構")
        try:
            import pymssql
        except ImportError:
            raise UpstreamError("沒有安裝 pymssql,連不了沙盒") from None
        try:
            self._admin = pymssql.connect(**params, autocommit=True)
        except Exception as e:                       # noqa: BLE001
            raise UpstreamError(f"連不上執行驗證沙盒,推不出上游表結構({type(e).__name__})") from None
        self._db = spec_exec._SANDBOX_DB_PREFIX + uuid.uuid4().hex[:12]
        self._con = None
        try:
            spec_exec._drop_stale_sandbox_dbs(self._admin)
            self._admin.cursor().execute(f"CREATE DATABASE [{self._db}]")
            self._con = pymssql.connect(**params, database=self._db, autocommit=True)
        except Exception as e:                       # noqa: BLE001
            self.close()
            raise UpstreamError(f"沙盒建不出暫用的資料庫({type(e).__name__})") from None

    def execute(self, sql: str) -> None:
        self._con.cursor().execute(sql)

    def describe(self, sql: str) -> list:
        """[(欄位名或 None, 型別)];描述不了時丟 UpstreamError(訊息是 SQL Server 的錯誤)。"""
        cur = self._con.cursor()
        cur.execute("SELECT name, system_type_name, error_message "
                    "FROM sys.dm_exec_describe_first_result_set(%s, NULL, 0) ORDER BY column_ordinal",
                    (sql,))
        rows = cur.fetchall()
        for _, _, err in rows:
            if err:
                raise UpstreamError(str(err)[:200])
        return [(name, typ) for name, typ, _ in rows]

    def close(self) -> None:
        if getattr(self, "_con", None) is not None:
            try:
                self._con.close()
            except Exception:                        # noqa: BLE001
                pass
        try:
            self._admin.cursor().execute(f"ALTER DATABASE [{self._db}] SET SINGLE_USER WITH ROLLBACK "
                                         f"IMMEDIATE; DROP DATABASE [{self._db}]")
        except Exception:                            # noqa: BLE001
            pass
        try:
            self._admin.close()
        except Exception:                            # noqa: BLE001
            pass


def _spec_ddl(spec_text: str) -> list:
    """規格「資料表定義」的 CREATE TABLE(T-SQL),在沙盒先建出來讓上游的 SQL 讀得到。"""
    text = _tables_section(spec_text)
    try:
        statements = sqlglot.parse(text, dialect=DIALECT)
    except Exception:                                # noqa: BLE001
        if not spec_tables(spec_text):
            return []                                # 只寫了「待補」這類說明:規格自己沒有表
        raise UpstreamError("規格的「資料表定義」解析不了") from None
    return [st.sql(dialect=DIALECT) for st in statements
            if isinstance(st, exp.Create) and str(st.args.get("kind", "")).upper() == "TABLE"]


def _column_type(name: str, typ) -> str:
    m = _TYPE.fullmatch(typ.lower()) if isinstance(typ, str) else None
    if m is None or m.group(1).upper() not in table_defs._TYPES:
        raise UpstreamError(f"欄位 {name} 的型別 {typ} 不能拿來建測資表")
    return typ.lower()


def _columns(name: str, described: list) -> list:
    if not described:
        raise UpstreamError(f"上游 {name} 沒有輸出欄位")
    if len(described) > MAX_COLUMNS:
        raise UpstreamError(f"上游 {name} 的欄位超過 {MAX_COLUMNS} 個")
    out, seen = [], set()
    for col, typ in described:
        if not isinstance(col, str) or not _COLUMN_NAME.fullmatch(col):
            raise UpstreamError(f"上游 {name} 有沒有名稱(或名稱不能用)的欄位")
        if col.lower() in seen:
            raise UpstreamError(f"上游 {name} 有兩個叫 {col} 的欄位")
        seen.add(col.lower())
        out.append((col, _column_type(col, typ)))
    return out


def _snapshot_columns(snap: Snapshot, columns: list) -> list:
    if any(c.lower() in SNAPSHOT_COLUMNS for c, _ in columns):
        raise UpstreamError(f"snapshot {snap.name} 的查詢自己就有 dbt 的管理欄位名稱")
    if snap.updated_at is None:
        time_type = SNAPSHOT_TIME_TYPE
    else:
        found = [t for c, t in columns if c.lower() == snap.updated_at.lower()]
        if not found:
            raise UpstreamError(f"snapshot {snap.name} 的 updated_at({snap.updated_at})不是查詢輸出的欄位")
        time_type = found[0]
    return columns + [("dbt_scd_id", SCD_ID_TYPE), ("dbt_updated_at", time_type),
                      ("dbt_valid_from", time_type), ("dbt_valid_to", time_type)]


def _ddl(table: str, columns: list) -> str:
    return f"CREATE TABLE [dbo].[{table}] (" + ", ".join(f"[{c}] {t}" for c, t in columns) + ");"


# ------------------------------------------------------------------ 進入點
def _repo_tables(repo_defs) -> dict:
    """specs/tables/ 裡可以用的定義(完整欄位):{表名(小寫): TableDef}。不採用的(重名、解析不了)不算。"""
    return {k: d for k, d in (repo_defs or {}).items() if isinstance(d, table_defs.TableDef) and d.columns}


def _trim(columns: list, words: set, star: bool) -> tuple[list, list]:
    """要驗的 model 直接讀的上游表:欄位多時只留它讀得到名稱的欄位(跟 specs/tables/ 同一個門檻與規則,
    測資每一列要依序給每一欄的值,欄位太多會讓測資生成的輸出過長)。回傳 (留下的, 拿掉的名稱)。"""
    if len(columns) <= table_defs.TRIM_THRESHOLD or star:
        return columns, []
    kept = [(c, t) for c, t in columns if c.lower() in words]
    if not kept:
        return columns, []
    return kept, [c for c, _ in columns if c.lower() not in words]


# 給測資生成的上游說明(補在規格最後一段):每張上游的定義、整段的字元上限
MAX_DEFINITION_CHARS = 1500
MAX_CONTEXT_CHARS = 6000
CONTEXT_HEADING = "## 執行驗證的上游表說明(程式補上,不是需求)"


def _context_section(reads: list, defs: list) -> str:
    """給測資生成看的一段:要驗的 model 讀哪幾張表、直接讀的上游各自怎麼來、各表的資料要一致。

    測資生成只看得到規格與表結構。真模型實測時,它把資料放進看起來像明細的表,規則直接讀的加總表
    是空的(它不知道規則讀哪幾張、加總表就是明細的合計)。這段補上這兩件事;上游的定義只是說明,
    寫明不要拆成條件。reads 是表名,defs 是 [(名稱, 種類, 展開後的 SQL)](只有直接讀的上游)。"""
    lines = [CONTEXT_HEADING, "",
             "要驗的 model 直接讀這幾張表:" + "、".join(reads) + "。上游 model 不會執行,測資直接放進這些表。",
             "- 讓規則命中的案例,規則需要的每張表都要放到那筆資料。",
             "- 同一筆業務資料在各表要一致:下面是每張上游表怎麼從其他表算出來的,照它推算各表的值"
             "(例如加總表的金額要等於明細表的合計;snapshot 的 dbt_valid_to 是 NULL 的那列是目前有效的資料)。",
             "- 下面的定義只說明表怎麼來,不是需求,不要拆成條件。", ""]
    used = sum(len(x) for x in lines)
    for name, kind, sql in defs:
        body = sql.strip()
        if len(body) > MAX_DEFINITION_CHARS:
            body = body[:MAX_DEFINITION_CHARS] + "\n-- (以下省略)"
        block = [f"**{name}**({'dbt snapshot' if kind == 'snapshot' else 'dbt model'})", "```sql", body, "```", ""]
        size = sum(len(x) for x in block)
        if used + size > MAX_CONTEXT_CHARS:
            lines.append("(其餘上游的定義超過長度上限,沒有附上)")
            break
        lines += block
        used += size
    return "\n".join(lines).rstrip("\n") + "\n"


def build(path: str, source: str, spec_text: str, database: str, project_kwargs: dict, project,
          session_factory=None, repo_defs=None) -> UpstreamResult:
    """沿 ref() 推出上游表結構、補進規格。回傳 UpstreamResult:補過的規格(沒補就是原本的)、
    建了哪些表、展開要帶的 snapshot、推不出來的原因。

    repo_defs 是 specs/tables/ 的定義(table_defs.load 的結果)。上游 model 讀的原始表多半只有上游讀,
    不在要驗的 model 裡、也就不會被 table_defs.augment 補進規格;推結構時要用完整的定義(上游讀的
    欄位不一定是要驗的 model 讀的),所以這裡另外拿完整的一份,只在推結構的暫用資料庫裡建,不補進規格。
    補進規格的只有要驗的 model **直接** `ref()` 的上游(測資只放在這幾張);更上層的只在推結構時用。"""
    result = UpstreamResult(spec_text)
    if not project_kwargs.get("project") or not isinstance(spec_text, str):
        return result
    tables = spec_tables(spec_text)          # 規格的「資料表定義」只寫了「待補」時是空的,照樣往下
    repo ={k: d for k, d in _repo_tables(repo_defs).items() if k not in tables}
    known = {**{k: f"[dbo].[{d.name}]" for k, d in repo.items()}, **tables}
    snapshots = find_snapshots(project)
    certain = tuple(sorted((n, s.table) for n, s in snapshots.items() if s.reason is None))
    missing, why, _ = upstream_refs(path, source, known, database, project_kwargs, certain)
    if missing is None:
        # 展開本身就不行(render_for_exec 會寫原因);是 snapshot 表名無法確定時,把為什麼寫出來
        for name, snap in snapshots.items():
            if snap.reason and isinstance(why, str) and why.endswith(f":{name}"):
                return UpstreamResult(spec_text, reason=f"snapshot {name} 的表結構無法確定:{snap.reason}")
        return result
    if not missing:
        return result                      # 規格裡已經有全部的上游
    session = None
    try:
        order = _plan((path,), missing, known, database, project_kwargs, project, snapshots, certain)
        session = (session_factory or SandboxSession)()
        for ddl in _spec_ddl(spec_text):
            try:
                session.execute(ddl)
            except Exception as e:                   # noqa: BLE001
                raise UpstreamError(f"規格的表在沙盒建不出來({type(e).__name__})") from None
        # specs/tables/ 的表只建上游展開後讀得到名稱的那幾張,用完整欄位
        upstream_words = {w.lower() for _, _, _, _, sql in order for w in _IDENT.findall(sql)}
        for key in sorted(repo):
            if key in upstream_words:
                try:
                    session.execute(table_defs._ddl(repo[key], repo[key].columns))
                except Exception as e:               # noqa: BLE001
                    raise UpstreamError(f"specs/tables/ 的表 {repo[key].name} 在沙盒建不出來"
                                        f"({type(e).__name__})") from None
        target_words = {w.lower() for w in _IDENT.findall(source or "")}
        star = table_defs._STAR.search(source or "") is not None
        direct = set(missing)
        out_ddl = []
        for name, table, kind, file, sql in order:
            try:
                columns = _columns(name, session.describe(sql))
            except UpstreamError as e:
                raise UpstreamError(f"上游 {name} 的輸出欄位推不出來:{e}") from None
            if kind == "snapshot":
                columns = _snapshot_columns(snapshots[name], columns)
            try:
                session.execute(_ddl(table, columns))
            except Exception as e:                   # noqa: BLE001
                raise UpstreamError(f"上游 {name} 的表在沙盒建不出來({type(e).__name__})") from None
            entry = {"name": name, "table": table, "kind": kind, "file": file,
                     "columns": len(columns), "direct": name in direct, "dropped": []}
            if name in direct:
                kept, entry["dropped"] = _trim(columns, target_words, star)
                out_ddl.append(_ddl(table, kept))
            result.tables.append(entry)
    except UpstreamError as e:
        return UpstreamResult(spec_text, reason=str(e))
    finally:
        if session is not None:
            session.close()
    result.spec_text = table_defs._insert(spec_text, out_ddl, bool(tables))
    built = spec_tables(result.spec_text)
    reads = [built[k].split(".")[-1].strip("[]") for k in sorted(built) if k in target_words]
    defs = [(name, kind, sql) for name, _table, kind, _file, sql in order if name in direct]
    result.spec_text = result.spec_text.rstrip("\n") + "\n\n" + _context_section(reads, defs)
    result.snapshots = tuple((t["name"], t["table"]) for t in result.tables if t["kind"] == "snapshot")
    return result
