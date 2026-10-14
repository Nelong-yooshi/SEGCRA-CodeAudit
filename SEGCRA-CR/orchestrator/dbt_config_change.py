"""dbt_config_change — 這次 MR 有沒有改到 dbt 的物化、排程或寫入相關設定。

只改 `{{ config(...) }}` 時,展開出來的 SQL 一個字都沒變,規則層和沙盒都看不出差別,效果卻
可能很大:拿掉排程選擇器用的 tag,model 從此不會被每日排程選到;incremental 改成 table,
每次全量重建、歷史資料被覆蓋;拿掉 unique_key,重跑同一天會重複寫入。同一組設定也可以寫在
dbt_project.yml(`+materialized:`)或屬性檔的 `config:` 底下,效果相同,一起檢查。

做法:用變更後的完整內容加上 diff,反推出變更前的內容,再把前後兩份的設定逐項比較。
  * model / macro 的 .sql:比較每個 config( 呼叫的參數(引號、空白、換行不同不算改);
    看不懂的寫法(`**` 展開、樣板運算)退回比較原文,有差異就算「看不出改了什麼」;
    config.set(...) 有變更、參數用到的變數在同一個檔案裡的 {% set %} 那一行被改到,也算
    「看不出改了什麼」
  * dbt_project.yml、屬性檔:看改到的那幾行位在哪個設定鍵底下(依縮排判斷)。只看
    models / snapshots / seeds 底下,欄位(columns)與測試底下的不算
  * 反推不出來時(diff 與完整內容對不上、沒有完整內容),只用 diff 裡看得到的內容判斷:
    config( 不在 diff 的範圍內就看不到——寧可多報,這種情況下改到的行出現受關注的設定名稱
    也算「看不出改了什麼」
新增、刪除整個檔案不算(不是「改了設定」)。

輸出只有設定名稱(固定清單)與行號,不含設定值:值是 MR 作者寫的文字,會被貼回 MR 留言。
純文字處理,不執行樣板、不載入 YAML、不丟例外。
"""
import ast
import re
from dataclasses import dataclass

from .dbt_relations import _CONFIG_SET, _config_bodies
from .dbt_render import is_dbt_template

# 受關注的設定(寫法統一成小寫、底線、不帶 + 前綴)。順序就是報告列出的順序
WATCHED = ("tags", "enabled", "materialized", "full_refresh", "on_schema_change",
           "unique_key", "incremental_strategy", "incremental_predicates",
           "pre_hook", "post_hook", "alias", "schema", "database")
# 沒有完整內容、只看 diff 的行時,出現這些名稱就算可能改到設定。tags、enabled、alias、
# schema、database 太常出現在一般 SQL(欄位名稱、WHERE enabled = 1),不列入
_SNIPPET_KEY = re.compile(
    r"(?<![@\w.])(materialized|full_refresh|on_schema_change|unique_key|incremental_strategy"
    r"|incremental_predicates|pre_hook|post_hook)\s*=")
_CONFIG_WORD = re.compile(r"\bconfig\b")
# config( 參數裡引用的變數名稱:去掉字串後的識別字,但不含參數名稱(後面接 =)、函式名稱
# (後面接 ()、屬性(前面是 .)
_STRING = re.compile(r"""'[^'\\\n]*'|"[^"\\\n]*\"""")
_NAME = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\b(?!\s*=(?!=))(?!\s*\()")
_NOT_NAMES = frozenset({"true", "false", "none", "True", "False", "None", "if", "else", "and",
                        "or", "not", "in", "is"})
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SQL_SUFFIX = ".sql"
_YAML_SUFFIXES = (".yml", ".yaml")
_PROJECT_FILES = ("dbt_project.yml", "dbt_project.yaml")
_CONFIG_ROOTS = ("models", "snapshots", "seeds")
_NOT_CONFIG = ("columns", "tests", "data_tests")
_ROOT_LINE = re.compile(r"(?m)^(?:models|snapshots|seeds)\s*:")
# YAML 的「鍵:」行:縮排、清單的 -、鍵(可帶引號、+ 前綴)、冒號後的值
_YAML_KEY = re.compile(
    r"""^(?P<ind>[ ]*)(?P<dash>(?:-[ ]+)*)(?P<key>[+\w.\-]+|'[^'\n]*'|"[^"\n]*")[ ]*:(?:[ ]+(?P<val>.*))?$""")
_YAML_DASH = re.compile(r"^(?P<ind>[ ]*)-(?:[ ]|$)")
# 超過就不反推、不解析(只看 diff 的行):真實的 model 與設定檔遠小於此
MAX_TEXT_CHARS = 500_000


@dataclass(frozen=True)
class ConfigChange:
    path: str
    keys: tuple[str, ...] = ()      # 改到的受關注設定(WATCHED 裡的名稱)
    other: bool = False             # 改到其他設定
    unknown: bool = False           # 有改到設定的跡象,但看不出改了什麼
    line: int = 0                   # 變更後第一個 config( 的行號;0 = 不知道


def config_changes(files, head_text=None) -> list[ConfigChange]:
    """MR 的變更檔裡,改到 dbt 設定的那些(沒改到的不列)。

    head_text  路徑 → 變更後完整內容的函式(dbt_context.DbtContext.head_text);沒給、或回
               None 時用檔案自己的 full_content
    """
    found = []
    for f in files if isinstance(files, list) else []:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            continue
        try:
            change = _file_change(f, head_text)
        except Exception:                   # noqa: BLE001 — 看不懂就當成「看不出改了什麼」
            change = ConfigChange(f["path"], unknown=True)
        if change is not None:
            found.append(change)
    return found


def _file_change(f: dict, head_text) -> ConfigChange | None:
    path = f["path"]
    name = path.replace("\\", "/").lower()
    is_sql, is_yaml = name.endswith(_SQL_SUFFIX), name.endswith(_YAML_SUFFIXES)
    if not (is_sql or is_yaml):
        return None
    hunks = _parse_hunks(f.get("diff"))
    if not hunks or _whole_file(hunks):
        return None
    new_text = head_text(path) if head_text is not None else None
    if not isinstance(new_text, str):
        new_text = f.get("full_content")
    sides = None
    if isinstance(new_text, str) and len(new_text) <= MAX_TEXT_CHARS:
        sides = _reverse(new_text, hunks)
    full = sides is not None
    if not full:
        sides = _snippets(hunks)
    old_lines, new_lines, old_changed, new_changed = sides
    if is_sql:
        return _sql_change(path, old_lines, new_lines, old_changed, new_changed, full)
    in_project = head_text is not None and head_text(path) is not None
    return _yaml_change(path, old_lines, new_lines, old_changed, new_changed, full,
                        in_project or name.rsplit("/", 1)[-1] in _PROJECT_FILES)


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
    """用變更後的完整內容與 diff 反推變更前的內容。
    回傳 (變更前的行, 變更後的行, 變更前被刪的行號集合, 變更後新增的行號集合)(從 0 起算);
    diff 的內文與完整內容對不上時回 None。"""
    new_lines = [ln.rstrip("\r") for ln in new_text.split("\n")]
    old, old_changed, new_changed, i = [], set(), set(), 0
    for _, _, new_start, new_count, body in hunks:
        start = new_start if new_count == 0 else new_start - 1
        if start < i or start > len(new_lines):
            return None
        old.extend(new_lines[i:start])
        i = start
        for tag, text in body:
            if tag == "-":
                old_changed.add(len(old))
                old.append(text)
                continue
            if i >= len(new_lines) or new_lines[i] != text:
                return None
            if tag == "+":
                new_changed.add(i)
            else:
                old.append(text)
            i += 1
    old.extend(new_lines[i:])
    return old, new_lines, old_changed, new_changed


def _snippets(hunks):
    """沒有完整內容時:只用 diff 看得到的行拼成前後兩份(每段之間空一行)。"""
    old, new, old_changed, new_changed = [], [], set(), set()
    for *_, body in hunks:
        if old or new:
            old.append("")
            new.append("")
        for tag, text in body:
            if tag == "-":
                old_changed.add(len(old))
            elif tag == "+":
                new_changed.add(len(new))
            if tag != "+":
                old.append(text)
            if tag != "-":
                new.append(text)
    return old, new, old_changed, new_changed


# ─────────────────── model / macro 的 .sql ───────────────────

def _sql_change(path, old_lines, new_lines, old_changed, new_changed, full) -> ConfigChange | None:
    old_text, new_text = "\n".join(old_lines), "\n".join(new_lines)
    marked = is_dbt_template(old_text) or is_dbt_template(new_text)
    if not full:
        # 只看得到 diff 的幾行:config( 的開頭可能不在裡面,結尾的 }} 也算樣板的跡象
        marked = marked or any(t in old_text + new_text for t in ("}}", "%}"))
    if not marked:
        return None
    if full and not (_CONFIG_WORD.search(old_text) or _CONFIG_WORD.search(new_text)):
        return None
    keys, other, unknown = _compare_configs(old_text, new_text)
    if _config_sets(old_text) != _config_sets(new_text):
        unknown = True
    changed = [old_lines[i] for i in old_changed] + [new_lines[i] for i in new_changed]
    names = _config_names(old_text) | _config_names(new_text)
    if names and not unknown:
        # 參數用的是同一個檔案裡 {% set %} 的變數:改到 set 那一行,設定就可能變了
        set_line = re.compile(r"\{%-?\s*set\s+(?:" + "|".join(map(re.escape, sorted(names)))
                              + r")\b")
        unknown = any(set_line.search(line) for line in changed)
    if not full and not (keys or other or unknown) and not (_config_bodies(old_text)
                                                             and _config_bodies(new_text)):
        # diff 裡看不到完整的 config( 呼叫:改到的行可能落在它裡面
        unknown = any(map(_SNIPPET_KEY.search, changed))
    if not (keys or other or unknown):
        return None
    return ConfigChange(path, keys=keys, other=other, unknown=unknown,
                        line=_first_config_line(new_lines) if full else 0)


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


def _config_sets(text: str) -> list[str]:
    """config.set(...) 等寫法所在的那幾行(去掉空白),比較前後有沒有變。"""
    return [_squash(line) for line in text.split("\n") if _CONFIG_SET.search(line)]


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

def _yaml_change(path, old_lines, new_lines, old_changed, new_changed, full,
                 known_dbt) -> ConfigChange | None:
    if not known_dbt and not (_ROOT_LINE.search("\n".join(old_lines))
                              or _ROOT_LINE.search("\n".join(new_lines))):
        return None
    keys, other = set(), False
    flow = ([], [])                          # 寫成一行 { } 的設定:(變更前, 變更後) 的「鍵, 值」
    for side, (lines, changed) in enumerate(((old_lines, old_changed), (new_lines, new_changed))):
        chains = _yaml_chains(lines)
        for idx in sorted(changed):
            chain = chains[idx]
            if chain is None:
                continue
            if full and (not chain or chain[0] not in _CONFIG_ROOTS):
                continue
            if any(k in _NOT_CONFIG for k in chain):
                continue
            # 只看這一行自己的鍵(清單項目、跨行的值則是它所屬的鍵);再往上是資料夾、model
            # 名稱,不是設定。同一行用 { } 寫了好幾個設定時,前後逐項比較(見下方)
            if "{" in lines[idx]:
                flow[side].extend(_flow_pairs(lines[idx]))
                if chain and chain[-1] != "config":      # 這一行自己的鍵(config 只是外層)
                    flow[side].append((chain[-1], _squash(lines[idx])))
                continue
            hit = {chain[-1]} & set(WATCHED) if chain else set()
            keys |= hit
            if not hit and ("config" in chain or _plus_key(lines[idx])):
                other = True
    if sorted(flow[0]) != sorted(flow[1]):
        diff = {k for k, _ in set(flow[0]) ^ set(flow[1])} or {k for k, _ in flow[0] + flow[1]}
        keys |= diff & set(WATCHED)
        other = other or bool(diff - set(WATCHED))
    if not (keys or other):
        return None
    return ConfigChange(path, keys=tuple(k for k in WATCHED if k in keys), other=other)


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


def _plus_key(line: str) -> bool:
    m = _YAML_KEY.match(line)
    return bool(m) and m["key"].startswith("+")


def _yaml_chains(lines) -> list:
    """每一行所在的鍵路徑(從最外層開始,寫法統一過);空行、註解、位在多行字串裡的行是 None。
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
            chains.append(None)             # 上一層的鍵已經有值:這行是多行字串的一部分
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
