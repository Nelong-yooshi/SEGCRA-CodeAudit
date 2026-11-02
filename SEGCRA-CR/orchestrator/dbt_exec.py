"""執行驗證用的 dbt 展開:把只引用 source() 的 model 展開成可以在沙盒執行的純 SQL。

沙盒每次建一個新的資料庫,依規格「資料表定義」建出測資表(見 spec_exec.execute_cases)。
model 裡的 source() 要指向那幾張表,而不是正式環境的表名,所以另外展開一次:
  * source():sources.yml 有宣告、表名確定,而且規格裡有同名的表 → 展開成規格裡的寫法
  * ref()、{{ this }}:不支援(上游 model 與增量的目標表都不在沙盒裡)
  * 有 is_incremental() 的 model:不支援(增量那段要先放一份上一次執行後的目標表)
任何一項不成立就不展開、回報原因,執行驗證照舊回「尚未支援」交人工。不猜表名:
猜錯的話執行失敗或結果不符,會被說成程式的問題。

展開一律走 render_model_isolated(子行程隔離 + 逾時),與規則層用的展開相同。
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from .dbt_render import is_dbt_template, render_model_isolated
from toolbox.sqltools import DIALECT

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# 規格的「資料表定義」段落:從這個標題到下一個同層或更上層的標題
_TABLES_HEADING = re.compile(r"^##[ \t]*資料表定義[^\n]*$", re.M)
_NEXT_HEADING = re.compile(r"^#{1,2}[ \t]", re.M)
_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
# 規格整份都會進 prompt,本來就有上限;這裡另外擋一次,解析時間有界
MAX_TABLES_CHARS = 50_000

# 同名的表不只一張(不同 schema)時記成這個值:source() 對到它就展開失敗
AMBIGUOUS = None


def _tables_section(spec_text: str) -> str:
    m = _TABLES_HEADING.search(spec_text)
    if m is None:
        return ""
    rest = spec_text[m.end():]
    nxt = _NEXT_HEADING.search(rest)
    section = rest[:nxt.start()] if nxt else rest
    blocks = _FENCE.findall(section)
    return "\n".join(blocks) if blocks else section


def spec_tables(spec_text) -> dict:
    """規格「資料表定義」裡 CREATE TABLE 的表:{表名(小寫): 沙盒裡的寫法}。

    寫法每段加上方括號(`[dbo].[txn]`、`[txn]`),跟規格寫的一樣帶不帶 schema。
    名稱不是英數與底線、或帶資料庫名(三段式)的表不收;同名(不分大小寫)的表不只一張時
    值是 AMBIGUOUS。段落找不到或解析不了回空的 dict。"""
    if not isinstance(spec_text, str):
        return {}
    text = _tables_section(spec_text)
    if not text.strip() or len(text) > MAX_TABLES_CHARS:
        return {}
    try:
        statements = sqlglot.parse(text, dialect=DIALECT)
    except Exception:            # noqa: BLE001 — 解析不了就當成沒有表,不展開
        return {}
    out: dict = {}
    for st in statements:
        if not isinstance(st, exp.Create) or str(st.args.get("kind", "")).upper() != "TABLE":
            continue
        table = st.this.this if isinstance(st.this, exp.Schema) else st.this
        if not isinstance(table, exp.Table) or table.catalog:
            continue
        parts = [p for p in (table.db, table.name) if p]
        if not parts or not all(_IDENT.fullmatch(p) for p in parts):
            continue
        spelled = ".".join(f"[{p}]" for p in parts)
        key = table.name.lower()
        out[key] = spelled if out.get(key, spelled) == spelled else AMBIGUOUS
    return out


def render_for_exec(path: str, source: str, spec_text: str, database: str,
                    project_kwargs: dict) -> tuple[str | None, str | None]:
    """展開成沙盒執行用的 SQL。回傳 (SQL, None) 或 (None, 不能展開的原因)。

    project_kwargs 是展開時要帶的專案資訊(macro 與 DbtProjectInfo,見 pipeline._render_project);
    沒有專案資訊時 source() 無法確定,一律不能展開。"""
    if not project_kwargs.get("project"):
        return None, "沒有 dbt 專案資訊,無法確定 source() 指向的表"
    tables = spec_tables(spec_text)
    if not tables:
        return None, "規格的「資料表定義」段落沒有可以解析的 CREATE TABLE"
    rendered = render_model_isolated(path, source=source, database=database,
                                     sandbox_tables=tables, **project_kwargs)
    if not rendered.ok:
        return None, rendered.error or "展開失敗"
    if rendered.macro_conflicts:
        return None, f"dbt 專案裡有 {len(rendered.macro_conflicts)} 組同名的 macro,無法確定展開結果"
    if rendered.incremental_checked:
        return None, "增量 model(呼叫了 is_incremental())的執行驗證尚未支援"
    if is_dbt_template(rendered.sql):
        return None, "展開後的 SQL 仍含樣板標記"
    if not rendered.sql.strip():
        return None, "展開後沒有 SQL"
    return rendered.sql, None
