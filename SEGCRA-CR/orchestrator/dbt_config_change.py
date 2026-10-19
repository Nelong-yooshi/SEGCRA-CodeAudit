"""dbt_config_change — 這次 MR 有沒有改到 dbt 的物化、排程或寫入相關設定。

只改 `{{ config(...) }}` 時,展開出來的 SQL 一個字都沒變,規則層和沙盒都看不出差別,效果卻
可能很大:拿掉排程選擇器用的 tag,model 從此不會被每日排程選到;incremental 改成 table,
每次全量重建、歷史資料被覆蓋;拿掉 unique_key,重跑同一天會重複寫入。同一組設定也可以寫在
dbt_project.yml(`+materialized:`)或屬性檔的 `config:` 底下,效果相同,一起檢查。

哪些檔案算 dbt 的檔案,看**位置**不看檔名或內容:呼叫端給 relative(路徑 → 相對 dbt 專案根目錄
的路徑,依 dbt_project.yml 的目錄設定;見 pipeline._dbt_relative)。說明文件裡的範例
dbt_project.yml、頂層剛好有 models: 的 CI 設定、一般 SQL 範本裡的 config( 都不算。

做法:用變更後的完整內容加上 diff,反推出變更前的內容,再把前後兩份的設定逐項比較。
  * model / macro 的 .sql:比較每個 config( 呼叫的參數(引號、空白、換行不同不算改);
    看不懂的寫法(`**` 展開、樣板運算)退回比較原文,有差異就算「看不出改了什麼」;
    config.set(...) 有變更(跨行的整段一起比)、改到的行落在跨行的 {% set %} 裡、或參數用到的
    變數在同一個檔案裡的 {% set %} 那一行被改到,也算「看不出改了什麼」
  * dbt_project.yml、屬性檔:看改到的那幾行位在哪個設定鍵底下(依縮排判斷;跨行的字串算它
    所屬的鍵)。只看 models / snapshots / seeds 底下:受關注的設定、config 底下或 + 開頭的
    其他設定分開列;欄位、測試、說明不算;其餘對不到已知鍵的行算「看不出改了什麼」。
    用 tab 縮排(YAML 不允許,無法判斷層級)、或檔案裡有別名 / merge key(`*名稱`、`<<:`,
    改到的值可能被別處引用)時,改到的行也算「看不出改了什麼」
  * 反推不出來時(diff 與完整內容對不上、沒有完整內容),只用 diff 裡看得到的內容判斷:
    config( 不在 diff 的範圍內就看不到——寧可多報,這種情況下改到的行出現受關注的設定名稱
    也算「看不出改了什麼」
  * 新增整個檔案:列出它設定的受關注項目(added);刪除整個檔案:記下來(deleted)。
    嚴重度由呼叫端決定(見 pipeline.enforce_dbt_config_change)

輸出只有設定名稱(固定清單)、行號與例外的類型名稱,不含設定值:值是 MR 作者寫的文字,會被
貼回 MR 留言。純文字處理,不執行樣板、不載入 YAML;處理中出錯時記成「看不出改了什麼」。
"""
import ast
import re
from dataclasses import dataclass

from .dbt_impact import normalize_path
from .dbt_project import DEFAULT_LAYOUT, LAYOUT_ROOT_FILES
from .dbt_relations import _CONFIG_SET, _config_bodies
from .dbt_render import is_dbt_template

# 受關注的設定(寫法統一成小寫、底線、不帶 + 前綴)。順序就是報告列出的順序
WATCHED = ("tags", "enabled", "materialized", "full_refresh", "on_schema_change",
           "unique_key", "incremental_strategy", "incremental_predicates",
           "pre_hook", "post_hook", "alias", "schema", "database")
# 新增的檔案設定了這些時,不只是列出來:全量重建與 hook 裡的 SQL 都不在展開結果裡
HIGH_RISK_NEW = ("full_refresh", "pre_hook", "post_hook")
# 沒有完整內容、只看 diff 的行時,出現這些名稱就算可能改到設定。tags、enabled、alias、
# schema、database 太常出現在一般 SQL(欄位名稱、WHERE enabled = 1),不列入
_SNIPPET_KEY = re.compile(
    r"(?<![@\w.])(materialized|full_refresh|on_schema_change|unique_key|incremental_strategy"
    r"|incremental_predicates|pre_hook|post_hook)\s*=")
# 同一組名稱寫成字串(字典的鍵、config.set 的參數):只看得到 diff 時,出現在 diff 的任何一行
# (含上下文)就算可能改到設定——跨行的 {% set %}、config.set( 的開頭可能不在 diff 的範圍內
_SNIPPET_QUOTED = re.compile(
    r"""['"](materialized|full_refresh|on_schema_change|unique_key|incremental_strategy"""
    r"""|incremental_predicates|pre_hook|post_hook|pre-hook|post-hook)['"]""")
_CONFIG_WORD = re.compile(r"\bconfig\b")
# config( 參數裡引用的變數名稱:去掉字串後的識別字,但不含參數名稱(後面接 =)、函式名稱
# (後面接 ()、屬性(前面是 .)
_STRING = re.compile(r"""'[^'\\\n]*'|"[^"\\\n]*\"""")
_NAME = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\b(?!\s*=(?!=))(?!\s*\()")
_NOT_NAMES = frozenset({"true", "false", "none", "True", "False", "None", "if", "else", "and",
                        "or", "not", "in", "is"})
_SET_START = re.compile(r"\{%-?\s*set\s+[A-Za-z_]\w*")
_ENDSET = re.compile(r"\{%-?\s*endset\b")
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SQL_SUFFIX = ".sql"                    # 跟 dbt 一樣分大小寫(dbt_context、dbt_impact 也是)
_YAML_SUFFIXES = (".yml", ".yaml")
_CONFIG_ROOTS = ("models", "snapshots", "seeds")
_NOT_CONFIG = ("columns", "tests", "data_tests")
# 不影響執行的鍵:改了不報
_INERT = ("description", "docs")
# 只看得到 diff 的行時,行的上層看不到;這些鍵在那種情況下也當成不影響執行
_INERT_SNIPPET = _INERT + ("name",)
# YAML 的「鍵:」行:縮排、清單的 -、鍵(可帶引號、+ 前綴)、冒號後的值
_YAML_KEY = re.compile(
    r"""^(?P<ind>[ ]*)(?P<dash>(?:-[ ]+)*)(?P<key>[+\w.\-]+|'[^'\n]*'|"[^"\n]*")[ ]*:(?:[ ]+(?P<val>.*))?$""")
_YAML_DASH = re.compile(r"^(?P<ind>[ ]*)-(?:[ ]|$)")
# 別名與 merge key:改到的值可能被別處引用
_YAML_ALIAS = re.compile(r"(?:^|[\s\[{,:-])\*[A-Za-z0-9_\-]|<<[ ]*:")
# 超過就不反推、不解析(只看 diff 的行):真實的 model 與設定檔遠小於此
MAX_TEXT_CHARS = 500_000
# 跨行的 config.set( / {% set %} 最多往下找幾行(找不到結尾就算到這裡)
MAX_SPAN_LINES = 200


@dataclass(frozen=True)
class ConfigChange:
    path: str
    keys: tuple[str, ...] = ()      # 改到的受關注設定(WATCHED 裡的名稱);新增的檔案是它設定的
    other: bool = False             # 改到其他設定
    unknown: bool = False           # 有改到設定的跡象,但看不出改了什麼
    line: int = 0                   # 變更後的行號(.sql 是第一個 config(,yml 是第一個改到的設定);0 = 不知道
    added: bool = False             # 新增整個檔案
    deleted: bool = False           # 刪除整個檔案
    error: str | None = None        # 處理中出錯時,例外的類型名稱


def default_relative(path, prefix: str = "") -> str | None:
    """不知道專案的目錄設定時:專案在 prefix(空字串 = repo 根目錄)、目錄是 dbt 的預設值。"""
    path = normalize_path(path)
    if path is None:
        return None
    if prefix:
        if not path.startswith(prefix + "/"):
            return None
        path = path[len(prefix) + 1:]
    return path if DEFAULT_LAYOUT.contains(path) else None


def is_dbt_yaml(rel: str) -> bool:
    """dbt 會讀設定的 yml(相對專案根目錄的路徑,已確定在 dbt 的目錄裡):dbt_project.yml 與
    各目錄裡的屬性檔。packages.yml、selectors.yml 這類根目錄的其他檔案不是(不在這裡比較)。"""
    return rel.endswith(_YAML_SUFFIXES) and (rel == "dbt_project.yml" or rel not in LAYOUT_ROOT_FILES)


def config_changes(files, head_text=None, relative=None) -> list[ConfigChange]:
    """MR 的變更檔裡,改到 dbt 設定的那些(沒改到的不列)。

    head_text  路徑 → 變更後完整內容的函式(dbt_context.DbtContext.head_text);沒給、或回
               None 時用檔案自己的 full_content
    relative   路徑 → 相對 dbt 專案根目錄的路徑;不是 dbt 會讀的檔回 None。沒給時用
               default_relative(專案在 repo 根目錄、dbt 的預設目錄)
    """
    relative = relative or default_relative
    found = []
    for f in files if isinstance(files, list) else []:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            continue
        try:
            change = _file_change(f, head_text, relative)
        except Exception as e:              # noqa: BLE001 — 看不懂就當成「看不出改了什麼」
            change = ConfigChange(f["path"], unknown=True, error=type(e).__name__)
        if change is not None:
            found.append(change)
    return found


@dataclass
class _Sides:
    """變更前後的內容。changed 是改到的行(從 0 起算);new_no 是變更後每一行的實際行號,
    old_at 是變更前每一行在變更後的位置(被刪掉的行:刪除處的行號)。"""
    old: list
    new: list
    old_changed: set
    new_changed: set
    new_no: list
    old_at: list


def _is_sql(rel: str) -> bool:
    return rel.endswith(_SQL_SUFFIX)


def _is_dbt_file(rel) -> bool:
    return rel is not None and (_is_sql(rel) or is_dbt_yaml(rel))


def _file_change(f: dict, head_text, relative) -> ConfigChange | None:
    path = f["path"]
    rel = relative(path)
    old_path = f.get("old_path")
    old_rel = relative(old_path) if isinstance(old_path, str) and old_path != path else rel
    if not _is_dbt_file(rel):
        # 改名搬出 dbt 的目錄:對 dbt 來說等於刪掉了這個檔案
        return ConfigChange(path, deleted=True) if _is_dbt_file(old_rel) else None
    is_sql = _is_sql(rel)
    new_text = head_text(path) if head_text is not None else None
    if not isinstance(new_text, str):
        new_text = f.get("full_content")
    hunks = _parse_hunks(f.get("diff"))
    if not _is_dbt_file(old_rel):
        # 從 dbt 的目錄外改名搬進來:對 dbt 來說是新增的檔案。內容沒變時 diff 是空的,
        # 不另外判斷就會讓帶著設定的 model 安靜進來
        if not (isinstance(new_text, str) and len(new_text) <= MAX_TEXT_CHARS):
            if hunks and _whole_file(hunks):
                new_text = "\n".join(text for tag, text in hunks[0][4] if tag == "+")
            else:
                return ConfigChange(path, unknown=True, added=True)    # 看不到內容
        return _added_change(path, is_sql, new_text)
    if not hunks:
        return None
    if _whole_file(hunks):
        if hunks[0][3] == 0:
            return ConfigChange(path, deleted=True)
        if not (isinstance(new_text, str) and new_text and len(new_text) <= MAX_TEXT_CHARS):
            new_text = "\n".join(text for tag, text in hunks[0][4] if tag == "+")
        return _added_change(path, is_sql, new_text)
    sides = None
    if isinstance(new_text, str) and len(new_text) <= MAX_TEXT_CHARS:
        sides = _reverse(new_text, hunks)
    full = sides is not None
    if not full:
        sides = _snippets(hunks)
    if is_sql:
        return _sql_change(path, sides, full)
    return _yaml_change(path, sides, full, "unknown" if rel == "dbt_project.yml" else "other")


# ─────────────────── diff ───────────────────

def _parse_hunks(diff):
    """unified diff → [(舊起始行, 舊行數, 新起始行, 新行數, [(標記, 內容)])];格式不對回 None。"""
    if not isinstance(diff, str) or not diff:
        return None
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    hunks, body = [], None
    for ln in lines:
        if ln.startswith("@@"):
            m = _HUNK.match(ln)
            if not m:
                return None
            body = []
            hunks.append((int(m[1]), 1 if m[2] is None else int(m[2]),
                          int(m[3]), 1 if m[4] is None else int(m[4]), body))
        elif body is None:
            if not ln.startswith(("diff ", "index ", "--- ", "+++ ", "new file", "deleted file")):
                return None
        elif ln.startswith("\\"):
            continue                         # \ No newline at end of file
        elif ln[:1] in (" ", "+", "-"):
            body.append((ln[:1], ln[1:].rstrip("\r")))
        elif ln.rstrip("\r") == "":
            body.append((" ", ""))           # 有些工具會把空白的內文行修成空行
        else:
            return None
    for old_start, old_count, new_start, new_count, body in hunks:
        if (sum(t != "+" for t, _ in body) != old_count
                or sum(t != "-" for t, _ in body) != new_count):
            return None
    return hunks or None


def _whole_file(hunks) -> bool:
    """新增或刪除整個檔案。"""
    return len(hunks) == 1 and (hunks[0][1] == 0 or hunks[0][3] == 0) and \
        (hunks[0][0] == 0 or hunks[0][2] == 0)


def _reverse(new_text: str, hunks):
    """用變更後的完整內容與 diff 反推變更前的內容;diff 的內文與完整內容對不上時回 None。"""
    new_lines = [ln.rstrip("\r") for ln in new_text.split("\n")]
    old, old_at, old_changed, new_changed, i = [], [], set(), set(), 0
    for _, _, new_start, new_count, body in hunks:
        start = new_start if new_count == 0 else new_start - 1
        if start < i or start > len(new_lines):
            return None
        old.extend(new_lines[i:start])
        old_at.extend(range(i + 1, start + 1))
        i = start
        for tag, text in body:
            if tag == "-":
                old_changed.add(len(old))
                old.append(text)
                old_at.append(i + 1)
                continue
            if i >= len(new_lines) or new_lines[i] != text:
                return None
            if tag == "+":
                new_changed.add(i)
            else:
                old.append(text)
                old_at.append(i + 1)
            i += 1
    old.extend(new_lines[i:])
    old_at.extend(range(i + 1, len(new_lines) + 1))
    return _Sides(old, new_lines, old_changed, new_changed,
                  list(range(1, len(new_lines) + 1)), old_at)


def _snippets(hunks):
    """沒有完整內容時:只用 diff 看得到的行拼成前後兩份(每段之間空一行,行號記 0)。"""
    s = _Sides([], [], set(), set(), [], [])
    for _, _, new_start, _, body in hunks:
        if s.old or s.new:
            s.old.append("")
            s.old_at.append(0)
            s.new.append("")
            s.new_no.append(0)
        no = new_start
        for tag, text in body:
            if tag == "-":
                s.old_changed.add(len(s.old))
            elif tag == "+":
                s.new_changed.add(len(s.new))
            if tag != "+":
                s.old.append(text)
                s.old_at.append(no)
            if tag != "-":
                s.new.append(text)
                s.new_no.append(no)
                no += 1
    return s


def _first_changed_line(sides: _Sides, counted_new, counted_old) -> int:
    """算進去的改動裡,變更後最前面的行號;只有刪除時是刪除處。0 = 不知道。"""
    numbers = [sides.new_no[i] for i in counted_new] + [sides.old_at[i] for i in counted_old]
    numbers = [n for n in numbers if n > 0]
    return min(numbers) if numbers else 0


# ─────────────────── 新增 / 刪除整個檔案 ───────────────────

def _added_change(path, is_sql, new_text: str) -> ConfigChange | None:
    """新增的檔案(或從 dbt 的目錄外搬進來的):列出它設定的受關注項目。"""
    lines = new_text.split("\n")
    if is_sql:
        if not (is_dbt_template(new_text) and _CONFIG_WORD.search(new_text)):
            return None
        values = _config_values(new_text)
        unknown = values is None or bool(_config_set_spans(lines))
        keys = () if values is None else tuple(k for k in WATCHED if k in values)
        other = values is not None and bool(set(values) - set(WATCHED))
        line = _first_config_line(lines)
    else:
        keys, other, unknown, counted = set(), False, _tab_indented(lines), []
        aliases = bool(_YAML_ALIAS.search(new_text))
        for idx, chain in enumerate(_yaml_chains(lines)):
            kind = _yaml_line(chain, lines[idx], True)
            if kind is None:
                if aliases and chain and not _inert(chain, True):
                    unknown = True           # 設定的值可能來自別名,看不出設定了什麼
                continue
            # 寫成一行 { } 的設定(config: {tags: [...]})逐項列出
            flow = {k for k, _ in _flow_pairs(lines[idx])} if "{" in lines[idx] else set()
            if flow & set(WATCHED):
                counted.append(idx)
                keys |= flow & set(WATCHED)
            # 新檔沒有「改之前」可以比:只列有值的設定;models:、config:、- name: 這類結構行不算
            if kind == "unknown" or _container(lines[idx]):
                continue
            counted.append(idx)
            if kind == "other":
                other = True
            else:
                keys.add(kind)
        keys = tuple(k for k in WATCHED if k in keys)
        line = counted[0] + 1 if counted else 0
    if not (keys or other or unknown):
        return None
    return ConfigChange(path, keys=keys, other=other, unknown=unknown, line=line, added=True)


# ─────────────────── model / macro 的 .sql ───────────────────

def _sql_change(path, sides: _Sides, full) -> ConfigChange | None:
    old_text, new_text = "\n".join(sides.old), "\n".join(sides.new)
    marked = is_dbt_template(old_text) or is_dbt_template(new_text)
    if not full:
        # 只看得到 diff 的幾行:config( 的開頭可能不在裡面,結尾的 }} 也算樣板的跡象
        marked = marked or any(t in old_text + new_text for t in ("}}", "%}"))
    if not marked:
        return None
    if full and not (_CONFIG_WORD.search(old_text) or _CONFIG_WORD.search(new_text)):
        return None
    keys, other, unknown = _compare_configs(old_text, new_text)
    if _span_texts(sides.old, _config_set_spans(sides.old)) != \
            _span_texts(sides.new, _config_set_spans(sides.new)):
        unknown = True
    # 改到的行落在跨行的 {% set %} 裡:那個變數可能被 config 用到,看不出改了什麼
    if _in_spans(sides.old_changed, _set_spans(sides.old)) or \
            _in_spans(sides.new_changed, _set_spans(sides.new)):
        unknown = True
    changed = [sides.old[i] for i in sides.old_changed] + [sides.new[i] for i in sides.new_changed]
    names = _config_names(old_text) | _config_names(new_text)
    if names and not unknown:
        # 參數用的是同一個檔案裡 {% set %} 的變數:改到 set 那一行,設定就可能變了
        set_line = re.compile(r"\{%-?\s*set\s+(?:" + "|".join(map(re.escape, sorted(names)))
                              + r")\b")
        unknown = any(set_line.search(line) for line in changed)
    if not full and not (keys or other or unknown) and not (_config_bodies(old_text)
                                                             and _config_bodies(new_text)):
        # diff 裡看不到完整的 config( 呼叫:改到的行可能落在它裡面;跨行的 {% set %} 與
        # config.set( 的開頭也可能不在 diff 裡,只看得到中間的字典鍵或參數
        unknown = (any(map(_SNIPPET_KEY.search, changed))
                   or any(map(_SNIPPET_QUOTED.search, sides.old + sides.new)))
    if not (keys or other or unknown):
        return None
    line = (_first_config_line(sides.new) if full
            else _first_changed_line(sides, sides.new_changed, sides.old_changed))
    return ConfigChange(path, keys=keys, other=other, unknown=unknown, line=line)


def _compare_configs(old_text: str, new_text: str):
    old, new = _config_values(old_text), _config_values(new_text)
    if old is None or new is None:
        old_bodies, new_bodies = _config_bodies(old_text), _config_bodies(new_text)
        if old_bodies is not None and new_bodies is not None and \
                [_squash(b) for b in old_bodies] == [_squash(b) for b in new_bodies]:
            return (), False, False
        return (), False, True
    changed = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
    return tuple(k for k in WATCHED if k in changed), bool(changed - set(WATCHED)), False


def _config_values(text: str) -> dict | None:
    """每個設定名稱 → 所有 config( 呼叫裡給它的值(語法樹的文字形式,排序過)。
    看不懂的寫法回 None。"""
    bodies = _config_bodies(text)
    if bodies is None:
        return None
    values: dict[str, list[str]] = {}
    for body in bodies:
        try:
            call = ast.parse(f"f({body})", mode="eval").body
        except (SyntaxError, ValueError):
            return None
        pairs = []
        for arg in call.args:
            if not (isinstance(arg, ast.Dict)
                    and all(isinstance(k, ast.Constant) and isinstance(k.value, str)
                            for k in arg.keys)):
                return None
            pairs += [(k.value, v) for k, v in zip(arg.keys, arg.values)]
        for kw in call.keywords:
            if kw.arg is None:
                return None
            pairs.append((kw.arg, kw.value))
        for key, value in pairs:
            values.setdefault(_norm_key(key), []).append(ast.dump(value))
    return {k: tuple(sorted(v)) for k, v in values.items()}


def _config_names(text: str) -> set[str]:
    """config( 參數裡引用的變數名稱(例如 materialized=m 的 m、**cfg 的 cfg)。"""
    bodies = _config_bodies(text) or []
    return {n for body in bodies for n in _NAME.findall(_STRING.sub("''", body))} - _NOT_NAMES


def _config_set_spans(lines) -> list[tuple[int, int]]:
    """config.set(...) 等寫法所在的行範圍(含頭尾):從那一行往下數括號,到括號全部關上為止
    (跨行的整段;找不到結尾就算到 MAX_SPAN_LINES 行)。"""
    spans, i = [], 0
    while i < len(lines):
        m = _CONFIG_SET.search(lines[i])
        if not m:
            i += 1
            continue
        depth, end, opened = 0, None, False
        for j in range(i, min(len(lines), i + MAX_SPAN_LINES)):
            text = lines[j][m.end():] if j == i else lines[j]
            for ch in text:
                if ch == "(":
                    depth, opened = depth + 1, True
                elif ch == ")":
                    depth -= 1
            if (opened and depth <= 0) or (not opened and ("}}" in text or "%}" in text)):
                end = j
                break
        end = min(len(lines), i + MAX_SPAN_LINES) - 1 if end is None else end
        spans.append((i, end))
        i = end + 1
    return spans


def _set_spans(lines) -> list[tuple[int, int]]:
    """跨行的 {% set %}:`{% set x = ... %}` 到 %} 為止;`{% set x %}...{% endset %}` 到
    endset 為止。只回傳跨了兩行以上的(單行的由 _sql_change 依變數名稱判斷)。"""
    spans, i = [], 0
    while i < len(lines):
        m = _SET_START.search(lines[i])
        if not m:
            i += 1
            continue
        rest = lines[i][m.end():]
        block = not rest.lstrip().startswith("=")
        end = None
        for j in range(i, min(len(lines), i + MAX_SPAN_LINES)):
            text = rest if j == i else lines[j]
            if (block and _ENDSET.search(text)) or (not block and "%}" in text):
                end = j
                break
        end = min(len(lines), i + MAX_SPAN_LINES) - 1 if end is None else end
        if end > i:
            spans.append((i, end))
        i = end + 1
    return spans


def _span_texts(lines, spans) -> list[str]:
    return [_squash("".join(lines[a:b + 1])) for a, b in spans]


def _in_spans(indexes, spans) -> bool:
    """indexes 裡有沒有任何一行落在 spans 裡。先標出每一行在不在範圍內再查(行數與範圍數都由
    MR 決定,兩兩比對會是平方時間)。"""
    if not indexes or not spans:
        return False
    inside = bytearray(max(b for _, b in spans) + 1)
    for a, b in spans:
        inside[a:b + 1] = b"\x01" * (b - a + 1)
    return any(i < len(inside) and inside[i] for i in indexes)


def _first_config_line(lines) -> int:
    for n, ln in enumerate(lines, 1):
        if re.search(r"\bconfig\s*\(", ln):
            return n
    return 0


def _squash(text: str) -> str:
    return "".join(text.split())


def _norm_key(key: str) -> str:
    return key.strip("'\"").lstrip("+").replace("-", "_").lower()


# ─────────────────── dbt_project.yml 與屬性檔 ───────────────────

def _inert(chain, full) -> bool:
    inert = _INERT if full else _INERT_SNIPPET
    return any(k in _NOT_CONFIG or k in inert for k in chain)


def _yaml_line(chain, line: str, full, outside: str | None = None) -> str | None:
    """一行改動算什麼:受關注設定的名稱、"other"(其他設定)、"unknown"(看不出改了什麼),
    或 None(不算:空行、註解、欄位、測試、說明)。

    outside:models / snapshots / seeds 以外的區段怎麼算(只在看得到完整內容時分得出來)。
      * "unknown":dbt_project.yml。on-run-start / on-run-end 是會執行的 SQL,vars 會改變 model 裡
        var() 的值,profile、目錄設定、dispatch 會改變整個專案的行為
      * "other":屬性檔。sources 的 identifier 改了,用到它的 model 就讀別的表;受關注的設定
        (例如 sources 的 schema、database)照樣算受關注的設定
      * None:不算(新增的檔案只列它在 models 等底下設定了什麼)"""
    if not chain:
        return None
    if full and chain[0] not in _CONFIG_ROOTS:
        if outside is None or _inert(chain, full):
            return None
        if outside == "unknown":
            return "unknown"
        return next((k for k in reversed(chain) if k in WATCHED), "other")
    if _inert(chain, full):
        return None
    # 最靠近這一行的受關注鍵(hook 底下的清單、跨行的字串都算那個 hook)
    hit = next((k for k in reversed(chain) if k in WATCHED), None)
    if hit is not None:
        return hit
    if "config" in chain or "meta" in chain or _plus_key(line):
        return "other"
    return "unknown"


def _tab_indented(lines) -> bool:
    return any("\t" in ln[:len(ln) - len(ln.lstrip())] for ln in lines)


def _yaml_change(path, sides: _Sides, full, outside: str | None = None) -> ConfigChange | None:
    keys, other, unknown = set(), False, False
    counted = ([], [])                       # 算進去的改動:(變更前的行, 變更後的行)
    aliases = bool(_YAML_ALIAS.search("\n".join(sides.old))
                   or _YAML_ALIAS.search("\n".join(sides.new)))
    flow = ([], [])                          # 寫成一行 { } 的設定:(變更前, 變更後) 的「鍵, 值」
    for side, (lines, changed) in enumerate(((sides.old, sides.old_changed),
                                            (sides.new, sides.new_changed))):
        tabbed = [i for i in sorted(changed) if _tab_indented([lines[i]])]
        if tabbed:
            unknown = True                   # YAML 不允許 tab 縮排,層級無法判斷
            counted[side].append(tabbed[0])
        chains = _yaml_chains(lines)
        for idx in sorted(changed):
            chain = chains[idx]
            kind = _yaml_line(chain, lines[idx], full, outside)
            if aliases and chain and not _inert(chain, full):
                # 檔案裡有別名 / merge key:改到的值可能被引用到 models 底下,不管這一行本身算什麼
                unknown = True
                counted[side].append(idx)
            if kind is None:
                continue
            counted[side].append(idx)
            # 同一行用 { } 寫了好幾個設定時,前後逐項比較(見下方)
            if "{" in lines[idx]:
                flow[side].extend(_flow_pairs(lines[idx]))
                if chain[-1] != "config":                # 這一行自己的鍵(config 只是外層)
                    flow[side].append((chain[-1], _squash(lines[idx])))
                continue
            if kind == "other":
                other = True
            elif kind == "unknown":
                unknown = True
            else:
                keys.add(kind)
    if sorted(flow[0]) != sorted(flow[1]):
        diff = {k for k, _ in set(flow[0]) ^ set(flow[1])} or {k for k, _ in flow[0] + flow[1]}
        keys |= diff & set(WATCHED)
        other = other or bool(diff - set(WATCHED))
    if not (keys or other or unknown):
        return None
    return ConfigChange(path, keys=tuple(k for k in WATCHED if k in keys), other=other,
                        unknown=unknown, line=_first_changed_line(sides, counted[1], counted[0]))


def _flow_pairs(line: str) -> list[tuple[str, str]]:
    """一行裡 { } 寫法的「鍵: 值」(鍵統一寫法、值去掉空白)。只切最外層的逗號;引號裡的
    逗號、括號不另外處理——切錯只會讓前後比較出差異,往「有改到」靠。"""
    start, end = line.find("{"), line.rfind("}")
    inner = line[start + 1:end] if end > start else line[start + 1:]
    parts, depth, cur = [], 0, []
    for ch in inner:
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    pairs = []
    for part in parts:
        key, sep, value = part.partition(":")
        if sep and key.strip():
            pairs.append((_norm_key(key.strip()), _squash(value)))
    return pairs


def _container(line: str) -> bool:
    """「鍵:」後面沒有值(底下才是內容)的行。"""
    m = _YAML_KEY.match(line)
    return bool(m) and re.sub(r"(^|\s)#.*$", "", m["val"] or "").strip() == ""


def _plus_key(line: str) -> bool:
    m = _YAML_KEY.match(line)
    return bool(m) and m["key"].startswith("+")


def _yaml_chains(lines) -> list:
    """每一行所在的鍵路徑(從最外層開始,寫法統一過);空行、註解是 None。跨行的字串(上一層的
    鍵已經有值,例如 `+post-hook: |` 底下)算它所屬的那個鍵。
    只依縮排判斷,不載入 YAML。一次走完(堆疊放還沒結束的上層鍵):逐行往上找會是平方時間,
    特製的大檔案可以拖住審查。"""
    chains, stack = [], []              # stack:(鍵所在的欄、鍵、能不能接下一層)
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            chains.append(None)
            continue
        m = _YAML_KEY.match(line)
        if m:
            # 清單項目的鍵(- name: x):上一層可以和 - 同一欄(tags:\n- a 的寫法)
            limit, same_ok = len(m["ind"]), bool(m["dash"])
        else:
            d = _YAML_DASH.match(line)
            limit, same_ok = (len(d["ind"]), True) if d else (len(line) - len(line.lstrip(" ")), False)
        while stack and not (stack[-1][0] < limit or (stack[-1][0] == limit and same_ok)):
            stack.pop()
        if stack and not stack[-1][2]:
            chains.append([key for _, key, _ in stack])    # 上一層的鍵已經有值:跨行的字串
            continue
        chain = [key for _, key, _ in stack]
        if m:
            key = _norm_key(m["key"])
            chain.append(key)
            stack.append((len(m["ind"]) + len(m["dash"]), key, _opens_children(m["val"])))
        chains.append(chain)
    return chains


def _opens_children(value) -> bool:
    """「鍵: 值」的值部分能不能接下一層:空的、只有註解 / 錨點 / 標籤,或是跨行的 [ ] { }。"""
    value = re.sub(r"(^|\s)#.*$", "", value or "").strip()
    value = re.sub(r"^(?:[&!][^\s]*\s*)+", "", value).strip()
    return value == "" or value[:1] in "[{"
