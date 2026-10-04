"""突變測試:把 dbt_render / dbt_impact / isolation 的每一道防線逐一改壞,
確認測試真的會抓到。測試全過不代表測試有效——這支程式驗證的是「測試本身」。

在暫存目錄複製一份專案來改,repo 內的檔案不動。改壞後跑一次測試套件:
測試失敗 = 這道防線有測試守著;測試全過 = 測試有漏洞,要補。
測試套件卡住(例如拿掉資源上限造成無窮迴圈)也算被抓到——CI 設有 timeout-minutes。

用法(在 SEGCRA-CR 目錄下):

    python tests/tools/mutate.py                       # 全部
    python tests/tools/mutate.py --only "路徑:,hook:"   # 只跑標籤含這些字的
    python tests/tools/mutate.py --python .venv/bin/python

兩個 Linux 限定的突變(記憶體上限、符號連結)要在 Linux 上跑才驗得到。
改了被突變的那幾行程式時,對應的 old 字串要跟著更新;啟動時會先檢查每個突變點
是否剛好出現一次,不符會印出「設定錯誤」。
"""
import argparse
import ast
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

MOD = pathlib.Path("orchestrator") / "dbt_render.py"

REGEX = r'r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}"'
NL = "\n"     # 突變點含多行時用它接,字串裡不寫跳脫比較好讀

# 每次跑測試套件共用的參數。tests/test_mutate_tool.py 測的是本工具,不是被突變的
# 程式;其中「每個突變點剛好出現一次」那條在突變後必然失敗(那行已被改掉),若不
# 排除,**每個突變都會被誤判成被抓到**。
PYTEST_ARGS = ("-p", "no:cacheprovider", "--ignore=tests/test_mutate_tool.py")

# 金絲雀:改一行不影響任何行為的註解。它**必須存活**——若連它都被「抓到」,代表
# 有某個與突變無關的原因讓套件失敗(漏複製檔案、測試互相干擾、工具自己的測試
# 被捲進來……),整次結果都不可信,直接中止。
CANARY = ("dbt_render.py",
          "# 與 dbt-core 的 Jinja 環境相同的擴充",
          "# 與 dbt-core 的 Jinja 環境相同的擴充(canary)")

MUTANTS = {
    # ---- 沙箱與版本
    "拿掉沙箱(改回普通 Environment)": (
        "class _DbtSandbox(SandboxedEnvironment):",
        "class _DbtSandbox(__import__('jinja2').Environment):"),
    "不檢查 jinja2 版本": (
        "    if not _jinja_version_ok(jinja2.__version__):",
        "    if False:"),
    # ---- 讀檔 / 環境變數 / 目錄
    "Jinja 環境掛回檔案載入器(include 可讀檔)": (
        "        extensions=[*_JINJA_EXTENSIONS, _DbtBlocks],\n        finalize=_finalize,\n    )",
        "        extensions=[*_JINJA_EXTENSIONS, _DbtBlocks],\n        finalize=_finalize,\n"
        "        loader=__import__('jinja2').FileSystemLoader(str(code_root or '.')),\n    )"),
    "提供 env_var": (
        '    env.globals["execute"] = True                   # compile 期為 True',
        '    env.globals["execute"] = True\n'
        '    env.globals["env_var"] = lambda n, d=None: __import__("os").environ.get(n, d)'),
    "source= 模式猜 macro 目錄": (
        "    else:\n        root = None",
        "    else:\n        root = model_path.parent"),
    "符號連結不略過": (
        "             if not f.is_symlink() and f.is_file()]",
        "             if f.is_file()]"),
    # ---- 注入 / 覆寫
    "拿掉識別字白名單": (
        "    if not isinstance(value, str) or not _IDENT.fullmatch(value):",
        "    if False:"),
    "source() 忽略白名單": (
        '        _check_ident("來源", source_name)\n        return relation(table_name)',
        "        return f'\"{database}\".\"{schema}\".\"{table_name}\"'"),
    "允許 macro 覆寫內建": (
        "            if name in RESERVED_NAMES:",
        "            if False:"),
    "哨兵權杖固定(可被偽造)": (
        "    token = secrets.token_hex(8)",
        '    token = "SRC"'),
    # ---- 呼叫端資料 / 資訊洩漏
    "var 回傳原物件(可被樣板改到)": (
        "            return copy.deepcopy(variables[name])",
        "            return variables[name]"),
    "target 可修改": (
        '    env.globals["target"] = types.MappingProxyType({',
        '    env.globals["target"] = ({'),
    "拿掉輸出物件檢查": (
        "        finalize=_finalize,\n",
        ""),
    "錯誤訊息帶出路徑": (
        "        text = f\"{type(e).__name__}: 無法讀取檔案({e.strerror or '未知原因'})\"",
        '        text = f"{type(e).__name__}: {e}"'),
    "錯誤訊息保留控制字元": (
        '    text = "".join(ch if ch.isprintable() else " " for ch in text)',
        "    text = text"),
    "錯誤訊息不限長度": (
        "    return text[:MAX_ERROR_CHARS]",
        "    return text"),
    # ---- 資源耗盡
    "整數次方不設限": (
        "                    right > MAX_INT_BITS or abs(left).bit_length() * right > MAX_INT_BITS):",
        "                    False):"),
    "整數乘法不設限": (
        "            if both_int and left.bit_length() + right.bit_length() > MAX_INT_BITS:",
        "            if False:"),
    "重複運算不設限": (
        "                        and len(seq) * n > MAX_OUTPUT_CHARS):",
        "                        and False):"),
    "串接不設限": (
        "                    and len(left) + len(right) > MAX_OUTPUT_CHARS):",
        "                    and False):"),
    "輸出不設限": (
        "        if size > limit:",
        "        if False:"),
    "原始碼不設限": (
        "        if len(source) > MAX_SOURCE_CHARS:",
        "        if False:"),
    "macro 總量不設限": (
        "        if total > MAX_MACRO_CHARS:",
        "        if False:"),
    "macro 檔案數不設限": (
        "    if len(files) > MAX_MACRO_FILES:",
        "    if False:"),
    "difflib 不設規模上限": (
        "    if len(src_lines) * len(out_lines) > DIFFLIB_MAX_CELLS:",
        "    if False:"),
    "is_dbt_template 改回正規表達式(ReDoS)": (
        "    return bool(text) and any(opener in text for opener in _TAG_PAIRS)",
        f"    return bool(text) and bool(re.search({REGEX}, text, re.S))"),
    "_mask_tags 改回正規表達式(ReDoS)": (
        '    """去掉 Jinja 標記,只留固定文字。線性時間(不用正規表達式,理由同 is_dbt_template)。"""',
        '    """x"""\n'
        f"    return re.sub({REGEX}, '', text, flags=re.S)"),
    "隔離執行不設逾時": ("isolation.py",
        "        if recv_conn.poll(timeout_s):",
        "        if recv_conn.poll(None):"),
    "隔離執行不設記憶體上限(僅 Linux 可驗)": ("isolation.py",
        "        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))",
        "        pass"),
    "隔離執行逾時時仍回傳成功": ("isolation.py",
        '        return on_failure(f"IsolationError: 執行逾時(超過 {timeout_s} 秒),已強制終止")',
        "        return None"),
    # ---- 不猜值
    "database 未設定時不報錯": (
        "        if database is None:\n            raise DbtRenderError(",
        "        if False:\n            raise DbtRenderError("),
    "var 找不到時回 None": (
        "        if default is _MISSING:\n            raise DbtRenderError(",
        "        if default is _MISSING:\n            return None\n            raise DbtRenderError("),
    "this 靜靜給假值": (
        '    env.globals["invocation_id"] = "segcra00"',
        '    env.globals["invocation_id"] = "segcra00"\n'
        '    env.globals["this"] = "THIS_RELATION"'),
    "target.database 未設定時給 None": (
        '        "database": database if database is not None else StrictUndefined(name="target.database"),',
        '        "database": database,'),
    "ref 忽略關鍵字參數": (
        "        if kwargs:\n            # dbt 的 ref('model', version=2)",
        "        if False:\n            # dbt 的 ref('model', version=2)"),
    "macro 檔逐一展開(跨檔呼叫會失敗)": (
        "        module = env.from_string(text).make_module(vars=shared, shared=True)",
        "        module = env.from_string(text).module"),
    "dispatch 不先找轉接器實作": (
        '        for candidate in (f"{self._adapter}__{macro_name}", f"default__{macro_name}"):',
        '        for candidate in (f"default__{macro_name}", f"{self._adapter}__{macro_name}"):'),
    "dispatch 找不到實作時給空字串": (
        "            if callable(found):" + NL + "                return found",
        "            if callable(found):" + NL + "                return found" + NL
        + '        return lambda *a, **k: ""'),
    "dispatch 的 macro 名稱不過白名單": (
        '        _check_ident("macro", macro_name)',
        "        pass"),
    "adapter 名稱不過白名單": (
        '    _check_ident("轉接器", adapter)',
        "    pass"),
    "macro 目錄設定不擋跳出專案": (
        '                or any(part in ("", ".", "..") for part in posix.split("/"))):',
        "                or False):"),
    "macro 未進共用命名空間(跨檔呼叫找不到)": (
        "            shared[name] = env.globals[name] = _wrap(obj)",
        "            env.globals[name] = _wrap(obj)"),
    "隔離:啟動失敗不收斂": ("isolation.py",
        '        return on_failure(f"IsolationError: 無法啟動子行程({type(e).__name__})")',
        "        raise"),
    "ref 改回裸表名": (
        "        return f'\"{database}\".\"{schema}\".\"{_check_ident(\"表\", name)}\"'",
        "        return name"),
    # ---- dbt 一致性
    "不去原始檔頭尾空白": (
        "        body = source.strip()",
        "        body = source"),
    "不啟用 do / loopcontrols": (
        '_JINJA_EXTENSIONS = ["jinja2.ext.do", "jinja2.ext.loopcontrols"]',
        "_JINJA_EXTENSIONS = []"),
    "execute 未定義": (
        '    env.globals["execute"] = True                   # compile 期為 True\n',
        ""),
    # ---- 行號
    "行號不加回被去掉的開頭行": (
        "    line_map = {k: (v + offset if v else 0) for k, v in line_map.items()}",
        "    line_map = {k: v for k, v in line_map.items()}"),
    "行號不夾住檔尾": (
        "        line_map=_clamp(line_map, n_src, n_out),",
        "        line_map=line_map,"),
    "哨兵插在行首(而非縮排之後)": (
        "            out.append(indent + mark + body)",
        "            out.append(mark + line)"),
    "空白行也插哨兵": (
        "        if idx in inside or not body:\n            out.append(line)",
        "        if idx in inside:\n            out.append(line)"),
    "{%- 開頭的行把哨兵插在標記前(擋住空白控制)": (
        "        if body.startswith(_LEFT_TRIM_TAGS):",
        "        if False:"),
    "{%- 開頭的行不給哨兵(行號沿用上一行)": (
        "            out.append(f\"{indent}{{{{- '{mark}' }}}}{body[:2]} {body[3:]}\")",
        "            out.append(line)"),
    "拆左修剪標記時不補空白(負號被當成左修剪)": (
        "{body[:2]} {body[3:]}",
        "{body[:2]}{body[3:]}"),
    "一行多個哨兵時改取最後一個": (
        "        mapping[len(stripped)] = cur if owner is None else owner",
        "        mapping[len(stripped)] = cur"),
    "哨兵渲染失敗時整份判失敗": (
        "    except Exception:\n        line_map = None",
        "    except Exception as e:\n        return RenderResult(ok=False, error=str(e))"),
    "行數改用 splitlines": (
        '    return text.count("\\n") + (0 if text.endswith("\\n") else 1)',
        "    return len(text.splitlines())"),
    # ---- dbt 專屬區塊 / macro 逐檔隔離 / config
    "dbt 專屬區塊在 model 檔也吞掉(整段 SQL 逃過預掃)": (
        '        if not getattr(self.environment, "segcra_macro_phase", False):',
        "        if False:"),
    "macro 階段旗標不關閉(model 檔跟著被吞)": (
        "    env.segcra_macro_phase = False" + NL + "    return env, sorted(macros), conflicts, problems",
        "    return env, sorted(macros), conflicts, problems"),
    "壞掉的 macro 檔略過但不記錄": (
        '            problems.append(f"{rel}:{_safe_error(e)}")' + NL + "            continue" + NL
        + "        for name in dir(module):",
        "            continue" + NL + "        for name in dir(module):"),
    "config.get 讀不到就拿預設值頂替(猜值)": (
        "        raise DbtRenderError(" + NL + "            f\"config.get('{name}') 讀不到值",
        "        return \"\" if default is _MISSING else default" + NL
        + "        raise DbtRenderError(" + NL + "            f\"config.get('{name}') 讀不到值"),
}

I = "dbt_impact.py"
MUTANTS.update({
    # ---- 路徑白名單
    "路徑:不擋 ..": (I,
        '    if any(part in ("", ".", "..") for part in path.split("/")):',
        '    if any(part in ("", ".") for part in path.split("/")):'),
    "路徑:不擋絕對路徑、反斜線、冒號": (I,
        '    if path.startswith("/") or "\\\\" in path or ":" in path:',
        "    if False:"),
    "路徑:不擋控制與不可見字元": (I,
        "    if not all(ch.isprintable() for ch in path):",
        "    if False:"),
    "路徑:不限長度": (I,
        "    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:",
        "    if not isinstance(path, str) or not path:"),
    "目錄設定不驗證": (I,
        "        if not self.model_dirs or not self.macro_dirs or None in self.model_dirs + self.macro_dirs:",
        "        if False:"),
    # ---- 惡意輸入
    "dbt 標籤解析不檢查檔案結尾(無窮迴圈)": (I,
        '        while parser.stream.current.type not in ("block_end", "eof"):',
        '        while parser.stream.current.type != "block_end":'),
    "認不出名稱的 materialization 當成普通區塊": (I,
        '            return "materialization_unknown_unknown"',
        "            return None"),
    "不限巢狀樣板深度": (I,
        "        if depth >= MAX_NESTED_TEMPLATE_DEPTH:",
        "        if False:"),
    "不限語法樹節點數": (I,
        "            if self.nodes_seen > MAX_AST_NODES:",
        "            if False:"),
    "不限檔案數": (I,
        "        if len(items) > MAX_FILES:\n            raise _LimitExceeded(f\"檔案數超過 {MAX_FILES}\")",
        "        if False:\n            raise _LimitExceeded(f\"檔案數超過 {MAX_FILES}\")"),
    "不限單檔大小": (I,
        "            if len(content) > MAX_FILE_CHARS:\n                raise _LimitExceeded(f\"單一檔案超過 {MAX_FILE_CHARS} 字元\")",
        "            if False:\n                raise _LimitExceeded(f\"單一檔案超過 {MAX_FILE_CHARS} 字元\")"),
    "不限總字元數": (I,
        "                raise _LimitExceeded(f\"檔案合計超過 {MAX_TOTAL_CHARS} 字元\")",
        "                pass"),
    "變更前不限總字元數": (I,
        "                raise _LimitExceeded(f\"變更前的檔案合計超過 {MAX_TOTAL_CHARS} 字元\")",
        "                pass"),
    "變更前不限單檔大小": (I,
        "                raise _LimitExceeded(f\"變更前的單一檔案超過 {MAX_FILE_CHARS} 字元\")",
        "                pass"),
    "說明不限條數": (I,
        "    if len(reasons) > MAX_REASONS:",
        "    if False:"),
    "說明不限長度": (I,
        "    return text if len(text) <= MAX_REASON_CHARS else text[:MAX_REASON_CHARS] + \"…\"",
        "    return text"),
    "不檢查 jinja2 版本(反查)": (I,
        "        if not _jinja_version_ok(jinja2.__version__):",
        "        if False:"),
    # ---- 依賴偵測
    "不計命名空間呼叫(Getattr)": (I,
        "            elif isinstance(node, nodes.Getattr):\n                refs.names.add(node.attr)",
        "            elif isinstance(node, nodes.Getattr):\n                pass"),
    "不計字串中的名稱": (I,
        "        if _is_ident(value):\n            refs.names.add(value)",
        "        if False:\n            refs.names.add(value)"),
    "不解析字串中的樣板": (I,
        '        if "{{" not in value and "{%" not in value:\n            return',
        "        if True:\n            return"),
    "不連結 dispatch 實作": (I,
        "                found |= suffix_index.get(ref, set())",
        "                pass"),
    "不掃 yml 中的 hook": (I,
        "                for target in resolve(refs.names):\n                    callers_of.setdefault(target, set()).add((\"yml\", path))",
        "                for target in ():\n                    callers_of.setdefault(target, set()).add((\"yml\", path))"),
    "不判斷覆寫內建": (I,
        "            if _is_builtin_override(name):",
        "            if False:"),
    "不偵測動態呼叫": (I,
        "def _is_dynamic(node) -> bool:\n    \"\"\"呼叫對象在執行期才知道的寫法。\"\"\"",
        "def _is_dynamic(node) -> bool:\n    \"\"\"x\"\"\"\n    return False"),
    "不看變更前內容": (I,
        '            for label, text in (("變更後", new_text), ("變更前", old_text)):',
        '            for label, text in (("變更後", new_text),):'),
    "不做遞移(macro 呼叫 macro)": (I,
        "                if who not in seen:\n                    seen.add(who)\n                    queue.append((who, chain + (who,)))",
        "                pass"),
    "只改 model 也觸發 macro 反查的不確定判定": (I,
        "        if self.changed_macro_files:\n            # 改了 macro 卻一個 model 都沒有",
        "        if True:\n            # 改了 macro 卻一個 model 都沒有"),
    "找不到任何 model 時不標不確定": (I,
        "            if not self.model_paths:\n                self.all_reasons.append(",
        "            if False:\n                self.all_reasons.append("),
    "不在 model / macro 目錄的 .sql 不標不確定": (I,
        "            if stray:\n                self.uncertain.append(",
        "            if False:\n                self.uncertain.append("),
    "變更前內容漏檔時當成新增檔": (I,
        "            if old_text is None and self.raw_base is not None and path not in self.added:",
        "            if False:"),
    "變更清單不排序": (I,
        "        for path in sorted(valid):",
        "        for path in valid:"),
    "無法解析的 model 不列入": (I,
        "        for path in self.unparsable_models:\n            self._add_model(",
        "        for path in ():\n            self._add_model("),
    "被略過的專案檔不觸發保守判定": (I,
        "            if rejected_project_files:",
        "            if False:"),
    "未提供變更前內容不標不確定": (I,
        '                self.uncertain.append("未提供變更前的內容:被刪除或改名的 macro 無法偵測")',
        "                pass"),
    "needs_human 忽略全部 model 旗標": (I,
        "        return bool(self.uncertain) or self.all_models_possibly_affected",
        "        return bool(self.uncertain)"),
    "隔離失敗不保守": (I,
        '    return _fail_closed(f"分析失敗({message}),保守視為全部 model 可能受影響")',
        "    return ImpactReport()"),
    # ---- 規格對應
    "規格:回傳不存在的檔案": (I,
        "    by_path = next((c for c in candidates if c in available), None)",
        "    by_path = candidates[0] if candidates else None"),
    "規格:多個 R 編號時自行擇一": (I,
        "    if len(by_code) == 1:",
        "    if len(by_code) >= 1:"),
    "規格:採用只有大小寫相同的檔案": (I,
        '            notes.append(f"只有大小寫不同的規格檔存在({near[0]}),未採用,請人工確認")',
        '            return SpecMatch(path, near[0], "path", tuple(candidates), ())'),
    "規格:R 編號不驗格式": (I,
        '    return (isinstance(code, str) and code.startswith("R-")\n            and 2 <= len(code) - 2 <= 4 and code[2:].isascii() and code[2:].isdigit())',
        "    return isinstance(code, str)"),
    "規格:檔名不驗字元": (I,
        "    return bool(text) and text.isascii() and all(ch.isalnum() or ch == \"_\" for ch in text)",
        "    return bool(text)"),
    "規格:接受單一字串時逐字元拆開": (I,
        "    if isinstance(rule_codes, str):\n        rule_codes = (rule_codes,)\n    if isinstance(strip_prefixes, str):",
        "    if isinstance(strip_prefixes, str):"),
})


def copy_project(src: pathlib.Path, dst: pathlib.Path) -> None:
    """複製整個專案(除了下面明列的建置產物/快取/憑證),不是手動列出「測試會用到
    哪些目錄」。

    原本是列舉式(只複製 orchestrator/toolbox/examples/tests),曾經漏複製
    config/、memory/、eval/——這些目錄裡沒有任何一個是 dbt_render.py 本身需要
    的,但 test_config_env.py、test_citations.py 等測試會間接讀到,漏複製的話
    那些測試檔在複製出去的專案裡連收集(collect)都做不到,pytest 會直接以
    非 0 回傳碼中止,**每個突變都會被誤判成「被抓到」**,而且不會有任何警訊——
    這正是要有基準檢查(_run_baseline)的理由,但事前把複製範圍變成「整個專案
    減去明確不需要的東西」,比每次有新測試依賴新目錄就要記得回來加一行更穩。
    """
    ignore = shutil.ignore_patterns(
        "__pycache__", "*.pyc", ".venv", ".venv-*", ".pytest_cache",
        "review_output", "_output", "target", "logs", "dbt_packages",
        "*.duckdb", "*.duckdb.wal", "*.env", ".git")
    # ignore_patterns 只套用在 copytree 的**子項目**,不會套用在傳進去的最上層
    # 項目本身——最上層要自己先過一次同樣的規則,否則 .venv(數百 MB)、
    # review_output 會在每個突變都整包複製一次。
    skipped = ignore(str(src), [p.name for p in src.iterdir()])
    for item in src.iterdir():
        if item.name in skipped:
            continue
        if item.is_dir():
            shutil.copytree(item, dst / item.name, ignore=ignore)
        else:
            shutil.copyfile(item, dst / item.name)


MUTANTS.update({
 "hook:config(...) 不檢查": (I, "            refs.dynamic |= _config_call_is_dynamic(call)", "            pass"),
 "hook:config.set 不檢查": (I, "            refs.dynamic |= _config_set_is_dynamic(call)", "            pass"),
 "hook:不擋 **kwargs": (I, "    if call.dyn_args is not None or call.dyn_kwargs is not None:" + NL + "        return True" + NL + "    for kw",
                        "    if False:" + NL + "        return True" + NL + "    for kw"),
 "hook:kwarg 值不檢查": (I, "        if kw.key in _HOOK_KEYS and not _is_literal_hook(kw.value):", "        if False:"),
 "hook:位置參數非字典也接受": (I, "        if not isinstance(arg, nodes.Dict):" + NL + "            return True",
                          "        if not isinstance(arg, nodes.Dict):" + NL + "            continue"),
 "hook:字典鍵不需寫死": (I, "            if not _const_str(pair.key):" + NL + "                return True",
                       "            if not _const_str(pair.key):" + NL + "                continue"),
 "hook:字典值不檢查": (I, "            if pair.key.value in _HOOK_KEYS and not _is_literal_hook(pair.value):", "            if False:"),
 "hook:清單內容不檢查": (I, "        return all(literal_item(item) for item in node.items)", "        return True"),
 "hook:字典形式 hook 不檢查值": (I, "            isinstance(p.key, nodes.Const) and isinstance(p.value, nodes.Const) for p in item.items)",
                            "            True for p in item.items)"),
 "config.set 鍵不需寫死": (I, "            or len(call.args) != 2 or not _const_str(call.args[0])):", "            or len(call.args) != 2):"),
 "config.set hook 值不檢查": (I, "    return call.args[0].value in _HOOK_KEYS and not _is_literal_hook(call.args[1])", "    return False"),
 "config 別名不擋": (I, '                        node.name in _DYNAMIC_ROOTS | {"config"} and id(node) not in vetted',
                   "                        node.name in _DYNAMIC_ROOTS and id(node) not in vetted"),
 "context 別名不擋": (I, '                        node.name in _DYNAMIC_ROOTS | {"config"} and id(node) not in vetted',
                    '                        node.name == "config" and id(node) not in vetted'),
 "self 不擋": (I, "                if node.name in _OPAQUE_NAMES or (", "                if False or ("),
 "私有屬性不擋": (I, '        if node.attr.startswith("_") or node.attr in _DYNAMIC_ROOTS:', "        if node.attr in _DYNAMIC_ROOTS:"),
 "屬性:.context 不擋": (I, '        if node.attr.startswith("_") or node.attr in _DYNAMIC_ROOTS:', '        if node.attr.startswith("_"):'),
 "config 任意屬性皆放行": (I, '            elif node.node.name == "config" and node.attr in _CONFIG_READ_ATTRS:', '            elif node.node.name == "config":'),
 "set_sql_header 以外傳出 config 也放行": (I, '        elif _is_name(target, "set_sql_header"):', "        else:"),
 "if 內的遮蔽也採計": (I, "    lines = [stmt.lineno for stmt in body" + NL,
                     "    lines = [stmt.lineno for top in body for stmt in [top, *top.find_all((nodes.Assign, nodes.AssignBlock))]" + NL),
 "同一行也視為遮蔽": (I, "                            and node.lineno > shadow)", "                            and node.lineno >= shadow)"),
 "等號右邊也視為遮蔽": (I, "                shadow = None               # 等號右邊", "                pass               # 等號右邊"),
 "root 名稱不比對 macro": (I, "            if refs.dynamic or refs.root_names - callables:", "            if refs.dynamic:"),
 "context.get 當成 macro": (I, "                refs.root_names.add(node.attr)", "                pass"),
 "yml:不解析樣板": (I, '        if "{{" in text or "{%" in text:' + NL + "            try:", "        if False:" + NL + "            try:"),
 "yml:解析失敗不標動態": (I, "                inner = self.scan(text, 1)" + NL + "            except (TemplateSyntaxError, RecursionError):" + NL + "                refs.dynamic = True",
                        "                inner = self.scan(text, 1)" + NL + "            except (TemplateSyntaxError, RecursionError):" + NL + "                pass"),
 "yml:動態不列入": (I, "                if is_dynamic(refs):" + NL + '                    dynamic_scopes.append(("yml", path))',
                  "                if False:" + NL + '                    dynamic_scopes.append(("yml", path))'),
 "yml:動態範圍不套用": (I, "        self._apply_hits((), dynamic_ymls, certain=False)", "        pass"),
 "context['x']() 一律動態": (I, "        return False                    # context['x'](...)", "        return True                    # context['x'](...)"),
 "變更前總量不擋(再驗)": (I, '                raise _LimitExceeded(f"變更前的檔案合計超過 {MAX_TOTAL_CHARS} 字元")', "                pass"),
 "屬性:x['私有'] 不擋": (I, "                    refs.dynamic |= _unsafe_attribute_name(node.arg)", "                    pass"),
 "屬性:執行期索引不記錄": (I, "                    refs.probed.add(node.node.name)" + NL + "            elif isinstance(node, nodes.Filter):",
                         "                    pass" + NL + "            elif isinstance(node, nodes.Filter):"),
 "屬性:filter 不檢查": (I, "                    refs.dynamic |= _filter_attribute_is_unsafe(node)", "                    pass"),
 "屬性:別名不追": (I, "                    refs.aliases.add((node.target.name, node.node.name))", "                    pass"),
 "屬性:探測不比對 macro": (I, "            return bool(probed & callables)", "            return False"),
 "屬性:別名只追一層": (I, "                        grew = True" + NL + "            return bool(probed",
                     "                        grew = False" + NL + "            return bool(probed"),
 "屬性:私有前綴不檢查": (I, '    return any(part.startswith("_") or part in _DYNAMIC_ROOTS for part in node.value.split("."))',
                      '    return any(part in _DYNAMIC_ROOTS for part in node.value.split("."))'),
 "屬性:context 名稱不檢查": (I, '    return any(part.startswith("_") or part in _DYNAMIC_ROOTS for part in node.value.split("."))',
                          '    return any(part.startswith("_") for part in node.value.split("."))'),
 "屬性:attr 參數不檢查": (I, "        return len(node.args) != 1 or _unsafe_attribute_name(node.args[0])", "        return False"),
 "屬性:map 轉交不檢查": (I, "        return not _const_str(first) or first.value in _ATTRIBUTE_FILTERS", "        return not _const_str(first)"),
 "屬性:attribute= 不檢查": (I, '    return any(kw.key == "attribute" and _unsafe_attribute_name(kw.value) for kw in node.kwargs)', "    return False"),
 "macro 預設值受遮蔽": (I, "                stack.extend((child, owner, None) for child in node.defaults)", "                stack.extend((child, owner, shadow) for child in node.defaults)"),
 "yml:不看跳脫字元": (I, "        refs.dynamic = _has_yaml_hiding_escape(text)", "        refs.dynamic = False"),
 "yml:跳脫不需完整十六進位": (I, "        if digits and i + 2 + digits <= length and all(", "        if digits or all("),
 "yml:不看接行": (I, '        if kind in "\\r\\n":' + NL + "            return True", '        if False:' + NL + "            return True"),
 "屬性:位置參數不檢查": (I, "        if _unsafe_attribute_name(node.args[position]):" + NL + "            return True", "        if False:" + NL + "            return True"),
 "屬性:sort 位置錯": (I, '_ATTRIBUTE_ARG_POSITION = {"sort": 2,', '_ATTRIBUTE_ARG_POSITION = {"sort": 0,'),
 "屬性:join 位置錯": (I, '"max": 1, "join": 1,', '"max": 1, "join": 0,'),
 "屬性:unique 位置錯": (I, '{"sort": 2, "unique": 1,', '{"sort": 2, "unique": 0,'),
 "隔離:poll 例外外洩": ("isolation.py", "        except (EOFError, OSError):" + NL + "            message = None       #", "        except ():" + NL + "            message = None       #"),
 "隔離:例外訊息帶出內容": ("isolation.py", '        message = ("error", f"{type(e).__name__}: 子行程執行失敗")', '        message = ("error", f"{type(e).__name__}: {e}")'),
 "relation_notice 不回報 ref()/source() 已知落差": (
     'relation_notice=(_RELATION_NOTICE if env.segcra_relation_usage["used"] else None),',
     "relation_notice=None,"),
 "relation_notice 沒用到 ref()/source() 也照樣回報": (
     'relation_notice=(_RELATION_NOTICE if env.segcra_relation_usage["used"] else None),',
     "relation_notice=_RELATION_NOTICE,"),
 "relation_notice 不排除 macro 檔模組層的 ref()": (
     '    env.segcra_relation_usage["used"] = False' + NL + "    env.segcra_macro_phase = False",
     "    env.segcra_macro_phase = False"),
})

# ---- 接進審查管線(#7 管線端):開關、檔名對應、樣板不進沙盒、資料庫名
MUTANTS.update({
 "接線:預掃開關接受任何真值": ("pipeline.py",
     '    dbt_enabled = bool(dbt_cfg) and dbt_cfg.get("enabled") is True',
     "    dbt_enabled = bool(dbt_cfg)"),
 "接線:展開失敗時送出不完整結果": ("pipeline.py",
     "    return sql, rendered.error, None",
     "    return rendered.sql, rendered.error, None"),
 "接線:lint 吃展開後的 SQL(行號基準錯位)": ("pipeline.py",
     '        lint = await hub.call_json("sqltools__lint", {"sql": sql})',
     '        lint = await hub.call_json("sqltools__lint", {"sql": rule_sql})'),
 "接線:find_spec 檔名對應寫死開啟": ("pipeline.py",
     'find_spec(hub, mr, by_path=cfg.dbt.get("enabled") is True)',
     "find_spec(hub, mr, by_path=True)"),
 "接線:檔名對應不看開關": ("spec_exec.py",
     "    if by_path and not codes:",
     "    if not codes:"),
 "接線:有 R 編號仍走檔名對應": ("spec_exec.py",
     "    if by_path and not codes:",
     "    if by_path:"),
 "接線:hub 存在時不查本機規格": ("spec_exec.py",
     "    for path in paths:" + NL + "        match = resolve_spec(path, available)",
     "    for path in (paths if hub is None else []):" + NL
     + "        match = resolve_spec(path, available)"),
 "接線:dbt 樣板照樣送進沙盒": ("spec_exec.py",
     "    if is_dbt_template(sql):",
     "    if False:"),
 "設定:enabled 接受字串": ("config.py",
     "    if not isinstance(enabled, bool):",
     "    if False:"),
 "設定:資料庫名不過白名單": ("config.py",
     "        if not _IDENT.fullmatch(database):",
     "        if False:"),
 "設定:資料庫名不讀環境變數": ("config.py",
     'os.environ.get("SEGCRA_DBT_DATABASE", section.get("database", ""))',
     'section.get("database", "")'),
 # ---- 資料庫名必須是約定假名(#14 合併前 review):漏設/拼錯只會默默產出錯的表名
 "設定:開啟時不檢查約定假名": ("config.py",
     "    if enabled and database != DBT_DATABASE_PLACEHOLDER:",
     "    if False:"),
 "設定:關閉時也要求約定假名(預設設定載入失敗)": ("config.py",
     "    if enabled and database != DBT_DATABASE_PLACEHOLDER:",
     "    if database != DBT_DATABASE_PLACEHOLDER:"),
 "設定:錯誤訊息回顯實際的值": ("config.py",
     "目前未設定或不符。\")",
     "目前是 {database}。\")"),
 "接線:展開前不檢查約定假名": ("pipeline.py",
     '    if dbt_cfg.get("database") != DBT_DATABASE_PLACEHOLDER:',
     "    if False:"),
 # ---- 展開失敗的確定性揭露(#16 review):不可退回「碰巧解析失敗才揭露」
 "展開失敗:後處理鏈不呼叫": ("pipeline.py",
     "        report = enforce_dbt_render_failure(report, pre)  # 展開失敗:規則掃的是原文\n",
     ""),
 "展開失敗:搬到檢核點關鍵字比對之前": ("pipeline.py",
     "        report = enforce_hints(report, pre)\n",
     "        report = enforce_dbt_render_failure(report, pre)\n"
     "        report = enforce_hints(report, pre)\n"),
 "展開失敗:dry-run 不報": ("pipeline.py",
     '        if entry.get("dbt_render_error"):\n            findings.append(_render_fail_finding(entry))',
     '        if False:\n            findings.append(_render_fail_finding(entry))'),
 "展開失敗:enforce_parse 不讓位(同檔兩條)": ("pipeline.py",
     '        if entry.get("dbt_render_error"):\n            continue',
     '        if False:\n            continue'),
 "展開失敗:沒失敗也報": ("pipeline.py",
     '        if entry.get("dbt_render_error"):\n            _put_program_finding(',
     '        if True:\n            _put_program_finding('),
 "展開失敗:改回直接附加(不取代模型同標題、也不去重)": ("pipeline.py",
     "            _put_program_finding(report, _render_fail_finding(entry))",
     '            report.setdefault("findings", []).append(_render_fail_finding(entry))'),
 "展開失敗:嚴重度降為 info(可自動放行)": ("pipeline.py",
     '    return {"file": entry["path"], "line": 0, "severity": "major",\n'
     '            "title": _RENDER_FAIL_TITLE,',
     '    return {"file": entry["path"], "line": 0, "severity": "info",\n'
     '            "title": _RENDER_FAIL_TITLE,'),
 # ---- relation_notice 確定性揭露(#14 review):不可退回「靠模型轉述」
 "提醒:後處理鏈不呼叫(退回靠模型轉述)": ("pipeline.py",
     "        report = enforce_dbt_notice(report, pre)  # 展開成功但表名未驗證,不靠模型轉述\n",
     ""),
 "提醒:搬到檢核點關鍵字比對之前(可能吞掉檢核點)": ("pipeline.py",
     "        report = enforce_hints(report, pre)\n",
     "        report = enforce_dbt_notice(report, pre)\n        report = enforce_hints(report, pre)\n"),
 "提醒:dry-run 不補": ("pipeline.py",
     "    report = enforce_dbt_notice(report, pre)   # 與正式路徑一致:表名未驗證的提醒不可少\n",
     ""),
 "提醒:沒有提醒也補(每個檔都被貼)": ("pipeline.py",
     "        if not notice:\n            continue\n        _put_program_finding(report, {",
     "        if False:\n            continue\n        _put_program_finding(report, {"),
 "提醒:改回直接附加(不取代模型同標題、也不去重)": ("pipeline.py",
     "        _put_program_finding(report, {",
     '        report.setdefault("findings", []).append({'),
 "提醒:嚴重度升高(改變決策)": ("pipeline.py",
     '            "file": entry["path"], "line": 0, "severity": "info",\n'
     '            "title": _DBT_NOTICE_TITLE,',
     '            "file": entry["path"], "line": 0, "severity": "major",\n'
     '            "title": _DBT_NOTICE_TITLE,'),
 # ---- 安全解壓(#15):壓縮檔來自待審 MR,每道防線都要有測試守著
 "解壓:壓縮檔大小不檢查": ("archive.py",
     "    if len(data) > MAX_COMPRESSED_BYTES:",
     "    if False:"),
 "解壓:不檢查 gzip 格式": ("archive.py",
     '    if bytes(data[:2]) != b"\\x1f\\x8b":',
     "    if False:"),
 "解壓:解壓後大小不設上限(壓縮炸彈)": ("archive.py",
     "            if total > MAX_TAR_BYTES:",
     "            if False:"),
 "解壓:成員數不設上限": ("archive.py",
     "        if count > MAX_MEMBERS:",
     "        if False:"),
 "解壓:不檢查單一頂層目錄": ("archive.py",
     "        elif parts[0] != top:",
     "        elif False:"),
 "解壓:頂層可以是檔案": ("archive.py",
     '            if not m.isdir():\n                raise ArchiveError("壓縮檔頂層不是目錄")',
     '            if False:\n                raise ArchiveError("壓縮檔頂層不是目錄")'),
 "解壓:不擋重複成員": ("archive.py",
     "        if key in seen:",
     "        if False:"),
 "解壓:重複只看完全相同(大小寫/正規化可繞過)": ("archive.py",
     '            key = ("收", unicodedata.normalize("NFC", rel).casefold())',
     '            key = ("收", rel)'),
 "解壓:目錄外也比對大小寫(無關檔名讓整包失敗)": ("archive.py",
     '            key = ("外", rel)',
     '            key = ("收", unicodedata.normalize("NFC", rel).casefold())'),
 "解壓:目錄外的也收": ("archive.py",
     "        if not _under(rel, dirs) or m.isdir():\n            continue",
     "        if m.isdir():\n            continue"),
 "解壓:目錄邊界退化成字串前綴": ("archive.py",
     '    return any(rel == d or rel.startswith(d + "/") for d in dirs)',
     "    return any(rel.startswith(d) for d in dirs)"),
 "解壓:不擋連結與特殊檔": ("archive.py",
     "        if m.type not in _REGULAR_TYPES:",
     "        if False:"),
 "解壓:副檔名不過濾": ("archive.py",
     "        if not rel.lower().endswith(ALLOWED_SUFFIXES):",
     "        if False:"),
 "解壓:副檔名比對分大小寫": ("archive.py",
     "        if not rel.lower().endswith(ALLOWED_SUFFIXES):",
     "        if not rel.endswith(ALLOWED_SUFFIXES):"),
 "解壓:單檔不設上限": ("archive.py",
     "        if m.size > MAX_FILE_BYTES:",
     "        if False:"),
 "解壓:檔數不設上限": ("archive.py",
     "        if n_files > MAX_FILES:",
     "        if False:"),
 "解壓:合計不設上限": ("archive.py",
     "        if total > MAX_TOTAL_BYTES:",
     "        if False:"),
 "解壓:空壓縮檔當成功": ("archive.py",
     '    if top is None:\n        raise ArchiveError("壓縮檔是空的")',
     '    if False:\n        raise ArchiveError("壓縮檔是空的")'),
 "解壓:不用 data filter": ("archive.py",
     'filter="data")',
     'filter="fully_trusted")'),
 "解壓:沒有 data filter 也照解": ("archive.py",
     '        if not hasattr(tarfile, "data_filter"):',
     "        if False:"),
 "解壓:暫存目錄不刪": ("archive.py",
     "        shutil.rmtree(tmp)",
     "        pass"),
 "解壓:非 UTF-8 用替代字元吞掉": ("archive.py",
     'files[rel] = raw.decode("utf-8")',
     'files[rel] = raw.decode("utf-8", "replace")'),
 "解壓:解出後不核對類型與大小": ("archive.py",
     "            if not stat.S_ISREG(st.st_mode) or st.st_size != m.size:",
     "            if False:"),
 "解壓:錯誤訊息回顯第三方例外": ("archive.py",
     '        text = f"{type(e).__name__}: 壓縮檔無法解析或無法解出"',
     '        text = f"{type(e).__name__}: {e}"'),
 "解壓:錯誤訊息不截斷": ("archive.py",
     "    return text[:MAX_ERROR_CHARS]",
     "    return text"),
 "解壓:目錄參數容許絕對路徑(幫忙去掉開頭 /)": ("archive.py",
     '        parts = _split_relative(p.rstrip("/"))',
     '        parts = _split_relative(p.strip("/"))'),
 "解壓:目錄參數容許空的": ("archive.py",
     "    if not isinstance(prefixes, tuple) or not prefixes:",
     "    if not isinstance(prefixes, tuple):"),
 "解壓:.. 段落不擋": ("archive.py",
     '    if any(p in ("", ".", "..") for p in parts):\n'
     '        raise ArchiveError("路徑含 ..、. 或空段落")\n    for p in parts:',
     '    if any(p in ("", ".") for p in parts):\n'
     '        raise ArchiveError("路徑含 ..、. 或空段落")\n    for p in parts:'),
 "解壓:絕對路徑不擋(要收內容的成員)": ("archive.py",
     '        raise ArchiveError("路徑是絕對路徑")\n    parts = name.split("/")\n'
     '    if any(p in ("", ".", "..") for p in parts):\n'
     '        raise ArchiveError("路徑含 ..、. 或空段落")\n    for p in parts:',
     '        pass\n    parts = name.split("/")\n'
     '    if any(p in ("", ".", "..") for p in parts):\n'
     '        raise ArchiveError("路徑含 ..、. 或空段落")\n    for p in parts:'),
 "解壓:絕對路徑不擋(所有成員)": ("archive.py",
     '        raise ArchiveError("路徑是絕對路徑")\n    parts = name.split("/")\n'
     '    if any(p in ("", ".", "..") for p in parts):\n'
     '        raise ArchiveError("路徑含 ..、. 或空段落")\n    return parts',
     '        pass\n    parts = name.split("/")\n'
     '    if any(p in ("", ".", "..") for p in parts):\n'
     '        raise ArchiveError("路徑含 ..、. 或空段落")\n    return parts'),
 "解壓:.. 不擋(所有成員)": ("archive.py",
     '    if any(p in ("", ".", "..") for p in parts):\n'
     '        raise ArchiveError("路徑含 ..、. 或空段落")\n    return parts',
     '    if any(p in ("", ".") for p in parts):\n'
     '        raise ArchiveError("路徑含 ..、. 或空段落")\n    return parts'),
 "解壓:. 與空段落不擋(所有成員,路徑別名讓檔案消失)": ("archive.py",
     '    if any(p in ("", ".", "..") for p in parts):\n'
     '        raise ArchiveError("路徑含 ..、. 或空段落")\n    return parts',
     '    if ".." in parts:\n'
     '        raise ArchiveError("路徑含 ..、. 或空段落")\n    return parts'),
 "解壓:目錄外的成員也做嚴格檢查(無關檔名讓整包失敗)": ("archive.py",
     "        parts = _split_loose(m.name.rstrip(\"/\") if m.isdir() else m.name)",
     "        parts = _split_relative(m.name.rstrip(\"/\") if m.isdir() else m.name)"),
 "解壓:要收內容的成員不做嚴格檢查": ("archive.py",
     "        _split_relative(rel)                # 會解出的檔案:整條路徑嚴格檢查",
     "        pass"),
 "解壓:不解出的副檔名也做嚴格檢查(無關檔名讓整包失敗)": ("archive.py",
     "        if not rel.lower().endswith(ALLOWED_SUFFIXES):\n"
     "            continue                        # 不需要的副檔名:丟棄(例如 .md、.csv、.py)\n"
     "        _split_relative(rel)                # 會解出的檔案:整條路徑嚴格檢查",
     "        _split_relative(rel)\n"
     "        if not rel.lower().endswith(ALLOWED_SUFFIXES):\n"
     "            continue"),
 "解壓:目錄成員被當成特殊檔": ("archive.py",
     "        if not _under(rel, dirs) or m.isdir():",
     "        if not _under(rel, dirs):"),
 "解壓:不收 .yaml": ("archive.py",
     'ALLOWED_SUFFIXES = (".sql", ".yml", ".yaml")', 'ALLOWED_SUFFIXES = (".sql", ".yml")'),
 "解壓:列檔名時漏掉": ("archive.py",
     "            names.append(rel)               # 只列名稱:不解出、不讀內容",
     "            pass"),
 "解壓:目錄也列進檔名": ("archive.py",
     "        if not m.isdir() and _under(rel, list_dirs):",
     "        if _under(rel, list_dirs):"),
 "解壓:列檔名的目錄內有連結也照列(名單不完整)": ("archive.py",
     "            if rel in list_dirs or m.type not in _REGULAR_TYPES:",
     "            if rel in list_dirs:"),
 "解壓:列檔名的目錄本身是連結也照列": ("archive.py",
     "            if rel in list_dirs or m.type not in _REGULAR_TYPES:",
     "            if m.type not in _REGULAR_TYPES:"),
 "解壓:指定目錄的上層是連結也不擋": ("archive.py",
     '        if not m.isdir() and any(d.startswith(rel + "/") for d in dirs + list_dirs):',
     "        if False:"),
 "解壓:只檢查要收內容目錄的上層": ("archive.py",
     '        if not m.isdir() and any(d.startswith(rel + "/") for d in dirs + list_dirs):',
     '        if not m.isdir() and any(d.startswith(rel + "/") for d in dirs):'),
 "解壓:只檢查列檔名目錄的上層": ("archive.py",
     '        if not m.isdir() and any(d.startswith(rel + "/") for d in dirs + list_dirs):',
     '        if not m.isdir() and any(d.startswith(rel + "/") for d in list_dirs):'),
 "解壓:上層是目錄也擋": ("archive.py",
     '        if not m.isdir() and any(d.startswith(rel + "/") for d in dirs + list_dirs):',
     '        if any(d.startswith(rel + "/") for d in dirs + list_dirs):'),
 "解壓:名稱開頭相同就算上層": ("archive.py",
     '        if not m.isdir() and any(d.startswith(rel + "/") for d in dirs + list_dirs):',
     '        if not m.isdir() and any(d.startswith(rel) and d != rel for d in dirs + list_dirs):'),
 "解壓:頂層目錄不做嚴格檢查": ("archive.py",
     "            _split_relative(parts[0])       # 頂層目錄是每個路徑的一部分:嚴格檢查",
     "            pass"),
 "解壓:列檔名的目錄不檢查": ("archive.py",
     "        list_dirs = _check_prefixes(list_prefixes) if list_prefixes != () else ()",
     "        list_dirs = tuple(list_prefixes)"),
 "解壓:反斜線與冒號不擋": ("archive.py",
     '    if "\\\\" in name or ":" in name:',
     "    if False:"),
 "解壓:控制字元不擋": ("archive.py",
     '    if not name or "\\x00" in name or not name.isprintable():',
     "    if not name:"),
 # ---- GitLab 打包下載(#15):權限、輸入、上限、轉址、錯誤訊息
 "下載:缺唯讀 token 仍下載": ("../toolbox/gitlab.py",
     "    if not (GITLAB_URL and GITLAB_PROJECT and GITLAB_READ_TOKEN):",
     "    if not (GITLAB_URL and GITLAB_PROJECT):"),
 "下載:改用有寫入權的 token": ("../toolbox/gitlab.py",
     'headers={"PRIVATE-TOKEN": GITLAB_READ_TOKEN, "Accept-Encoding": "identity"},',
     'headers={"PRIVATE-TOKEN": GITLAB_TOKEN, "Accept-Encoding": "identity"},'),
 "下載:接受分支名等任意字串": ("../toolbox/gitlab.py",
     "    if not isinstance(sha, str) or not _SHA.fullmatch(sha):",
     "    if not isinstance(sha, str):"),
 "下載:sha 只比對開頭": ("../toolbox/gitlab.py",
     "not _SHA.fullmatch(sha)",
     "not _SHA.match(sha)"),
 "下載:子目錄格式不檢查": ("../toolbox/gitlab.py",
     "            not _ARCHIVE_PATH.fullmatch(path)\n            or any",
     "            False\n            or any"),
 "下載:子目錄的 .. 不擋": ("../toolbox/gitlab.py",
     '            or any(p in (".", "..") for p in path.split("/")))):',
     "            or False)):"),
 "下載:大小上限參數不檢查": ("../toolbox/gitlab.py",
     "    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:",
     "    if False:"),
 "下載:跟隨轉址": ("../toolbox/gitlab.py",
     "follow_redirects=False",
     "follow_redirects=True"),
 "下載:非 200 也收": ("../toolbox/gitlab.py",
     "            if r.status_code != 200:",
     "            if False:"),
 "下載:接受傳輸層壓縮": ("../toolbox/gitlab.py",
     '            if r.headers.get("Content-Encoding", "identity").lower() != "identity":',
     "            if False:"),
 "下載:不看宣告大小(先讀再說)": ("../toolbox/gitlab.py",
     "            if declared.isdigit() and int(declared) > max_bytes:",
     "            if False:"),
 "下載:邊讀邊算不設上限": ("../toolbox/gitlab.py",
     "                if len(buf) > max_bytes:",
     "                if False:"),
 "下載:不設整體時間上限": ("../toolbox/gitlab.py",
     "                if time.monotonic() > deadline:",
     "                if False:"),
 "下載:改用會自動解壓的讀法": ("../toolbox/gitlab.py",
     "r.iter_raw()",
     "r.iter_bytes()"),
 "下載:兩把 token 相同也放行": ("../toolbox/gitlab.py",
     "    if GITLAB_READ_TOKEN == GITLAB_TOKEN:",
     "    if False:"),
 "下載:不檢查網址": ("../toolbox/gitlab.py",
     "    _check_archive_url(GITLAB_URL)\n",
     ""),
 "下載:本機以外也接受 http": ("../toolbox/gitlab.py",
     '    if parts.scheme == "http" and host in _LOOPBACK_HOSTS:',
     '    if parts.scheme == "http":'),
 "下載:網址可夾帶帳密": ("../toolbox/gitlab.py",
     "    if parts.username is not None or parts.password is not None:",
     "    if False:"),
 "下載:網址沒有主機也放行": ("../toolbox/gitlab.py",
     "    if not host:\n        raise",
     "    if False:\n        raise"),
 "下載:讀代理等環境設定": ("../toolbox/gitlab.py",
     "trust_env=False, timeout=30) as r:",
     "trust_env=True, timeout=30) as r:"),
 "解壓:Windows 問題名稱不擋": ("archive.py",
     '        if p.endswith((".", " ")) or p.split(".")[0].lower() in _WINDOWS_RESERVED:',
     "        if False:"),
 "解壓:裝置名稱比對分大小寫": ("archive.py",
     'p.split(".")[0].lower() in _WINDOWS_RESERVED',
     'p.split(".")[0] in _WINDOWS_RESERVED'),
 "解壓:裝置名稱帶副檔名就放過": ("archive.py",
     'p.split(".")[0].lower() in _WINDOWS_RESERVED',
     "p.lower() in _WINDOWS_RESERVED"),
 "下載:例外訊息回顯內容": ("../toolbox/gitlab.py",
     'raise ArchiveDownloadError(f"下載失敗({type(e).__name__})") from None',
     'raise ArchiveDownloadError(f"下載失敗({e})") from None'),
 # ---- 以 commit 取回 dbt 專案(#15 第 3 階段)
 "專案:commit 編號不檢查": ("dbt_project.py",
     "        if not isinstance(sha, str) or not _SHA.fullmatch(sha):",
     "        if not isinstance(sha, str):"),
 "專案:project_dir 不檢查": ("dbt_project.py",
     '    return _check_relative(project_dir.rstrip("/"), "設定檔的 dbt.project_dir")',
     '    return project_dir.strip("/")'),
 "專案:目錄設定的 .. 不擋": ("dbt_project.py",
     '            or any(p in ("", ".", "..") for p in parts)',
     '            or any(p in ("", ".") for p in parts)'),
 "專案:目錄設定的字元不檢查": ("dbt_project.py",
     '            or not all(re.fullmatch(r"[A-Za-z0-9_.\\-]+", p) for p in parts)):',
     "            or False):"),
 "專案:目錄設定的樣板標記不擋": ("dbt_project.py",
     '        if "{" in p or "}" in p:',
     "        if False:"),
 "專案:目錄數量不設上限": ("dbt_project.py",
     "    if not isinstance(value, list) or not value or len(value) > MAX_PATHS:",
     "    if not isinstance(value, list) or not value:"),
 "專案:source-paths 不支援": ("dbt_project.py",
     '    model_key = "model-paths" if "model-paths" in raw else "source-paths"',
     '    model_key = "model-paths"'),
 "專案:沒有 dbt_project.yml 當成預設值": ("dbt_project.py",
     "    if text is None:\n        raise DbtProjectError(",
     "    if False:\n        raise DbtProjectError("),
 "專案:解壓失敗照樣回傳": ("dbt_project.py",
     "    if not got.ok:\n        raise DbtProjectError(",
     "    if False:\n        raise DbtProjectError("),
 "專案:單筆超過總量上限也快取": ("dbt_project.py",
     "    if _size(project) > CACHE_MAX_BYTES:\n        return",
     "    if False:\n        return"),
 "專案:快取不限筆數": ("dbt_project.py",
     "        while len(_cache) > CACHE_MAX_ENTRIES or sum(map(_size, _cache.values())) > CACHE_MAX_BYTES:",
     "        while sum(map(_size, _cache.values())) > CACHE_MAX_BYTES:"),
 "專案:快取不限總量": ("dbt_project.py",
     "        while len(_cache) > CACHE_MAX_ENTRIES or sum(map(_size, _cache.values())) > CACHE_MAX_BYTES:",
     "        while len(_cache) > CACHE_MAX_ENTRIES:"),
 "專案:快取回傳同一個字典(可被竄改)": ("dbt_project.py",
     "                return _copy(_cache[key])",
     "                return _cache[key]"),
 "專案:快取存入同一個字典(可被竄改)": ("dbt_project.py",
     "        _cache[key] = _copy(project)",
     "        _cache[key] = project"),
 "專案:例外訊息回顯": ("dbt_project.py",
     'else f"{type(e).__name__}: 無法取得 dbt 專案")',
     'else f"{type(e).__name__}: {e}")'),
 "專案:不取回套件檔": ("dbt_project.py",
     'ROOT_FILES = ("dbt_project.yml", "packages.yml", "dependencies.yml")',
     'ROOT_FILES = ("dbt_project.yml",)'),
 "專案:不取回 dependencies.yml": ("dbt_project.py",
     'ROOT_FILES = ("dbt_project.yml", "packages.yml", "dependencies.yml")',
     'ROOT_FILES = ("dbt_project.yml", "packages.yml")'),
 "專案:成功結果不帶 root_files": ("dbt_project.py",
     "                      root_files=root_files, seed_paths=seed_paths,",
     "                      seed_paths=seed_paths,"),
 "專案:root_files 快取不複製(可被竄改)": ("dbt_project.py",
     "root_files=dict(project.root_files),", "root_files=project.root_files,"),
 "專案:快取總量不計 root_files": ("dbt_project.py",
     "for d in (project.files, project.root_files)", "for d in (project.files,)"),
 # ---- #20 review:seed / snapshot 目錄、檔名清單、extra_ref_names
 "專案:快取總量不計檔名清單": ("dbt_project.py",
     "            + sum(map(len, project.names)))", "            )"),
 "專案:快取取出時漏了檔名清單": ("dbt_project.py",
     "                      names=project.names)", "                      )"),
 "專案:不讀 seed 目錄設定": ("dbt_project.py",
     '            _path_list(raw, seed_key, DEFAULT_SEED_PATHS),',
     '            DEFAULT_SEED_PATHS,'),
 "專案:data-paths 舊名不支援": ("dbt_project.py",
     '    seed_key = "seed-paths" if "seed-paths" in raw else "data-paths"',
     '    seed_key = "seed-paths"'),
 "專案:不讀 snapshot 目錄設定": ("dbt_project.py",
     '            _path_list(raw, "snapshot-paths", DEFAULT_SNAPSHOT_PATHS))',
     '            DEFAULT_SNAPSHOT_PATHS)'),
 "專案:snapshot 目錄不收內容": ("dbt_project.py",
     "                                 model_paths + macro_paths + seed_paths + snapshot_paths))",
     "                                 model_paths + macro_paths + seed_paths))"),
 "專案:seed 目錄不列檔名": ("dbt_project.py",
     "    listed = tuple(dict.fromkeys(f\"{base}{p}\" for p in model_paths + seed_paths + snapshot_paths))",
     "    listed = tuple(dict.fromkeys(f\"{base}{p}\" for p in model_paths + snapshot_paths))"),
 "專案:seed 名稱不收": ("dbt_project.py",
     '        if ext == "csv" and _under(path, project.seed_paths):\n            found.add(stem)',
     '        if ext == "csv" and _under(path, project.seed_paths):\n            pass'),
 "專案:Python model 名稱不收": ("dbt_project.py",
     '        elif ext == "py" and _under(path, project.model_paths):\n            found.add(stem)',
     '        elif ext == "py" and _under(path, project.model_paths):\n            pass'),
 "專案:沒有主檔名的檔案也收(名單多一個空名稱)": ("dbt_project.py",
     "        if not dot or not stem:",
     "        if not dot:"),
 "專案:snapshot yml 合計不設上限(可拖慢審查)": ("dbt_project.py",
     "            if yml_chars > MAX_SNAPSHOT_YML_CHARS:",
     "            if False:"),
 "專案:snapshot yml 用不安全的載入器": ("dbt_project.py",
     "            doc = yaml.load(text, Loader=yaml.CSafeLoader)    # 安全載入器的 C 版",
     "            doc = yaml.load(text, Loader=yaml.UnsafeLoader)"),
 "專案:seed / .py 檔名含控制字元仍採用": ("dbt_project.py",
     "        if not stem.isprintable():",
     "        if False:"),
 "專案:不是資源的檔名也檢查控制字元": ("dbt_project.py",
     "        else:\n            continue\n        if not stem.isprintable():",
     "        else:\n            pass\n        if not stem.isprintable():"),
 "專案:snapshot 區塊對不上仍算看得懂": ("dbt_project.py",
     "            if (len(blocks) != len(_SNAPSHOT_TAG.findall(content))\n",
     "            if (False\n"),
 "專案:snapshot 名稱不是識別字仍採用": ("dbt_project.py",
     "                    or not all(_IDENT.fullmatch(name) for name in blocks)):",
     "                    or False):"),
 "專案:snapshot yml 看不懂仍算完整": ("dbt_project.py",
     "            if snapshots is None:\n                return None",
     "            if snapshots is None:\n                continue"),
 "專案:取回失敗也給名單": ("dbt_project.py",
     "    if not isinstance(project, DbtProject) or not project.ok:\n        return None",
     "    if not isinstance(project, DbtProject):\n        return None"),
 "下載:403 不提示需要 read_api": ("../toolbox/gitlab.py",
     '    403: "GITLAB_READ_TOKEN 權限不足:需要 read_api,角色至少 Reporter",\n', ""),
 "下載:狀態提示套到所有錯誤": ("../toolbox/gitlab.py",
     "hint = _ARCHIVE_STATUS_HINTS.get(r.status_code)",
     'hint = _ARCHIVE_STATUS_HINTS.get(r.status_code, "權限不足")'),
})

# ---- MR → macro 反查轉接層(#15 第 2 點)
MUTANTS.update({
 "MR反查:截斷的變更清單照常分析": ("dbt_mr_impact.py",
     '    if mr_diff.get("truncated") is not False:',
     '    if mr_diff.get("truncated") is True:'),
 "MR反查:改名不列舊路徑": ("dbt_mr_impact.py",
     '        for key in ("path", "old_path"):', '        for key in ("path",):'),
 "MR反查:不合法路徑略過": ("dbt_mr_impact.py",
     '            if path is None:\n                raise _Fail("MR 變更清單含不合法的路徑")',
     '            if path is None:\n                continue'),
 "MR反查:專案目錄外的變更也列入": ("dbt_mr_impact.py",
     '            elif path.startswith(prefix + "/"):',
     '            else:'),
 "MR反查:專案目錄前綴不含斜線(dbt_other 誤判)": ("dbt_mr_impact.py",
     '            elif path.startswith(prefix + "/"):',
     '            elif path.startswith(prefix):'),
 "MR反查:目錄設定晚於有無變更才檢查": ("dbt_mr_impact.py",
     "    prefix = _project_prefix(project_dir)\n    changed = _changed_paths(mr_diff[\"files\"], prefix)\n"
     "    if not changed:\n        return ImpactReport()",
     "    try:\n        prefix = _project_prefix(project_dir)\n    except _Fail:\n        return ImpactReport()\n"
     "    changed = _changed_paths(mr_diff[\"files\"], prefix)\n"
     "    if not changed:\n        return ImpactReport()"),
 "MR反查:取回失敗照常分析": ("dbt_mr_impact.py",
     "        if not project.ok:\n            raise _Fail(",
     "        if False:\n            raise _Fail("),
 "MR反查:設定檔變更不保守處理": ("dbt_mr_impact.py",
     "    root_changed = sorted(changed & set(ROOT_FILES))",
     "    root_changed = []"),
 "MR反查:目錄設定不同仍用 base": ("dbt_mr_impact.py",
     "                     base_files=base_files if same_layout else None,",
     "                     base_files=base_files,"),
 "MR反查:不比對 base 判斷新增": ("dbt_mr_impact.py",
     "    added = [p for p in paths if p in head_files and p not in base_files] if same_layout else []",
     "    added = []"),
 "MR反查:額外理由不標全部受影響": ("dbt_mr_impact.py",
     "            all_models_possibly_affected=True)\n    if report.changed_macro_files",
     "            all_models_possibly_affected=report.all_models_possibly_affected)\n"
     "    if report.changed_macro_files"),
 "MR反查:反查看不到 dbt_project.yml 的 hook": ("dbt_mr_impact.py",
     '    if "dbt_project.yml" in project.root_files:',
     "    if False:"),
 "MR反查:snapshot / seed 的內容也交給反查": ("dbt_mr_impact.py",
     "    files = {p: c for p, c in project.files.items() if _in_dirs(p, dirs)}",
     "    files = dict(project.files)"),
 "MR反查:有 snapshot 也不標不確定": ("dbt_mr_impact.py",
     "    if report.changed_macro_files and any(_in_dirs(p, head.snapshot_paths) for p in head.names):",
     "    if False:"),
 "MR反查:沒改 macro 也因 snapshot 標不確定": ("dbt_mr_impact.py",
     "    if report.changed_macro_files and any(_in_dirs(p, head.snapshot_paths) for p in head.names):",
     "    if any(_in_dirs(p, head.snapshot_paths) for p in head.names):"),
 "MR反查:反查回傳值不檢查": ("dbt_mr_impact.py",
     "    if not isinstance(report, ImpactReport):\n        raise _Fail(",
     "    if False:\n        raise _Fail("),
 "MR反查:非預期例外回顯": ("dbt_mr_impact.py",
     'return _fail(f"轉接層發生非預期錯誤({type(e).__name__})")',
     'return _fail(f"轉接層發生非預期錯誤({e})")'),
 "MR反查:理由不清控制字元": ("dbt_mr_impact.py",
     '    text = "".join(ch if ch.isprintable() else " " for ch in str(text))',
     "    text = str(text)"),
 "MR反查:預設改用同一行程反查": ("dbt_mr_impact.py",
     "from .dbt_impact import ImpactReport, analyze_macro_impact_isolated, normalize_path",
     "from .dbt_impact import ImpactReport, normalize_path\n"
     "from .dbt_impact import analyze_macro_impact as analyze_macro_impact_isolated"),
})

# ---- 確定性防線不能被模型關掉(#16 review):程式專用標題、規則命中不可被降級
MUTANTS.update({
 "後處理:不移除模型同標題的輸出": ("pipeline.py",
     '        if (f.get("file"), f.get("title")) != key:\n            kept.append(f)',
     "        if True:\n            kept.append(f)"),
 "後處理:取代範圍擴及其他檔案(只會更寬鬆)": ("pipeline.py",
     '        if (f.get("file"), f.get("title")) != key:',
     '        if f.get("title") != key[1]:'),
 "後處理:取代時不保留較嚴重的等級": ("pipeline.py",
     '    kept.append({**finding, "severity": worst})',
     "    kept.append(finding)"),
 "後處理:解析失敗改回直接附加": ("pipeline.py",
     "            _put_program_finding(report, _parse_fail_finding(entry))",
     '            report.setdefault("findings", []).append(_parse_fail_finding(entry))'),
 "規則補報:不看嚴重度(info 可蓋掉 blocker)": ("pipeline.py",
     '        if _SEVERITY_RANK.get(f.get("severity"), -1) < need:\n            continue',
     "        if False:\n            continue"),
 "規則補報:嚴重度差一級也算報過": ("pipeline.py",
     '        if _SEVERITY_RANK.get(f.get("severity"), -1) < need:',
     '        if _SEVERITY_RANK.get(f.get("severity"), -1) < need - 1:'),
 "規則補報:summary 提到也算報過": ("pipeline.py",
     '        text = " ".join(str(f.get(k) or "") for k in ("title", "detail", "suggestion"))',
     "        text = json.dumps(report, ensure_ascii=False)"),
 "規則補報:檔名也拿來比對關鍵詞": ("pipeline.py",
     '        text = " ".join(str(f.get(k) or "") for k in ("title", "detail", "suggestion"))',
     "        text = json.dumps(f, ensure_ascii=False)"),
 "規則補報:拿自己補的來比對(其他檔案的同條命中被跳過)": ("pipeline.py",
     "            if _reported_at_least(before, _RULE_KEYWORDS.get(code, [code]),",
     '            if _reported_at_least(report.get("findings", []), _RULE_KEYWORDS.get(code, [code]),'),
 "規則補報:不認得的嚴重度當成最輕": ("pipeline.py",
     '    need = _SEVERITY_RANK.get(severity, _SEVERITY_RANK["blocker"])',
     "    need = _SEVERITY_RANK.get(severity, 0)"),
})


def _run_baseline(src: pathlib.Path, python: str, timeout: int) -> tuple[str | None, float]:
    """複製一份未突變的專案,跑一次測試套件,確認完全乾淨才能開始判斷突變。

    「被抓到」的判斷依據是測試套件回傳碼非 0——如果套件本身就有一條失敗或收集
    (collect)不起來的測試(例如漏複製了某個依賴的目錄),**每個突變都會被誤判
    成被抓到**,結果全部失去意義,而且不會有任何警訊。

    回傳 (失敗原因或 None, 基準套件實際耗時秒數)。耗時供 main() 校正每個突變的
    逾時,見 _effective_timeout()。
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sgc_mut_baseline_"))
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        copy_project(src, tmp)
        started = time.monotonic()
        try:
            proc = subprocess.run([python, "-m", "pytest", "-q", *PYTEST_ARGS],
                                  cwd=tmp, capture_output=True, text=True, env=env,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"基準套件跑超過 {timeout} 秒未完成", float(timeout)
        elapsed = time.monotonic() - started
        if proc.returncode != 0:
            lines = proc.stdout.splitlines()
            tail = "\n".join(lines[-30:]) if lines else proc.stdout
            return f"基準套件(未突變)沒有全部通過,回傳碼 {proc.returncode}:\n{tail}", elapsed
        return None, elapsed
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _effective_timeout(requested: int, baseline_seconds: float) -> int:
    """每個突變的逾時至少要是「正常跑完整個套件」的 3 倍。

    逾時會被計為「被抓到」(拿掉資源上限造成的無窮迴圈確實該算被抓到),但若逾時
    比正常跑一次還短,**存活的突變會因為跑不完而被誤判成被抓到**——例如在 WSL 上
    整個套件要 4 分多鐘,固定 240 秒的逾時會把每個存活者都算成抓到。3 倍的餘裕
    仍然抓得到真正卡住的情況。
    """
    return max(requested, int(baseline_seconds * 3) + 30)


def _run_mutant(src: pathlib.Path, mutant, python: str, timeout: int, env: dict):
    """在快照的複本上套用一個突變並跑測試套件。回傳 (判定, 說明)。

    判定:"config"(突變點不是剛好出現一次)、"survived"(套件全過)、
    "caught"(套件失敗或卡住逾時)。
    """
    module, old, new = mutant
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sgc_mut_"))
    try:
        copy_project(src, tmp)
        path = tmp / "orchestrator" / module
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            return "config", f"突變點出現 {text.count(old)} 次"
        mutated = text.replace(old, new)
        try:
            ast.parse(mutated)
        except SyntaxError as e:
            # 語法錯誤的突變讓模組連匯入都失敗,任何測試都會紅——那不是「測試抓到了
            # 這道防線」,而是這個突變根本沒驗到任何行為。
            return "config", f"突變後的程式無法解析(第 {e.lineno} 行語法錯誤),驗不到任何行為"
        path.write_text(mutated, encoding="utf-8")
        try:
            proc = subprocess.run([python, "-m", "pytest", "-x", "-q", *PYTEST_ARGS],
                                  cwd=tmp, capture_output=True, text=True, env=env,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return "caught", f"測試套件卡住超過 {timeout} 秒"
        lines = proc.stdout.splitlines()
        if proc.returncode == 0:
            summary = next((l for l in reversed(lines)
                            if "passed" in l or "failed" in l or "error" in l), proc.stdout[-160:])
            return "survived", summary
        first = next((l for l in lines if l.startswith(("FAILED", "ERROR"))), "")
        return "caught", first.split(" - ")[0][:110]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(pathlib.Path(__file__).resolve().parents[2]),
                    help="SEGCRA-CR 目錄(預設為本檔所在的專案)")
    ap.add_argument("--python", default=sys.executable,
                    help="跑測試用的直譯器(預設為目前這個)")
    ap.add_argument("--only", default="")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--skip-baseline", action="store_true",
                    help="跳過開始前的基準套件檢查(例如 --start 續跑、已確認過基準乾淨時)")
    args = ap.parse_args()
    # 開跑時先拍一份快照,之後的基準檢查與每個突變都從這份複製。若直接從工作目錄
    # 複製,跑到一半有人改了檔案(例如新增的測試剛好有錯),後面的突變會因為那個
    # 無關的失敗被誤判成「被抓到」——而開頭那次基準檢查看不到後來的變化。
    snapshot = pathlib.Path(tempfile.mkdtemp(prefix="sgc_mut_snapshot_"))
    try:
        copy_project(pathlib.Path(args.src), snapshot)
        return _mutate_all(args, snapshot)
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)


def _mutate_all(args, src: pathlib.Path) -> int:
    only = [s for s in args.only.split(",") if s]
    selected = {k: v for k, v in MUTANTS.items() if not only or any(o in k for o in only)}
    selected = dict(list(selected.items())[args.start:])

    timeout = args.timeout
    if not args.skip_baseline:
        print("先跑一次未突變的基準套件,確認乾淨才開始判斷突變...", flush=True)
        # 基準套件本身給寬裕的上限:它的用途就是量出正常要跑多久
        problem, baseline_seconds = _run_baseline(src, args.python, args.timeout * 10)
        if problem is not None:
            print(f"[中止] {problem}", flush=True)
            print("基準套件本身沒過,突變測試的「被抓到」結果沒有意義,不繼續執行。",
                 flush=True)
            return 1
        timeout = _effective_timeout(args.timeout, baseline_seconds)
        print(f"基準套件乾淨(耗時 {baseline_seconds:.0f} 秒),每個突變的逾時設為 "
              f"{timeout} 秒,開始跑突變。", flush=True)
    else:
        print(f"[注意] 已跳過基準檢查:逾時固定為 {timeout} 秒。若這台機器跑完整個套件"
              f"要更久,存活的突變會因逾時被誤判成被抓到。", flush=True)

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    canary_verdict, canary_detail = _run_mutant(src, CANARY, args.python, timeout, env)
    if canary_verdict != "survived":
        print(f"[中止] 金絲雀突變(只改註解,不影響任何行為)沒有存活:{canary_detail}", flush=True)
        print("代表測試套件會因為與突變無關的原因失敗,每個突變都會被誤判成被抓到,"
              "結果不可信,不繼續執行。", flush=True)
        return 1

    survived, config_errors = [], []
    for label, entry in selected.items():
        mutant = entry if len(entry) == 3 else ("dbt_render.py", *entry)
        verdict, detail = _run_mutant(src, mutant, args.python, timeout, env)
        if verdict == "config":
            print(f"[設定錯誤] {label}:{detail}", flush=True)
            config_errors.append(label)
        elif verdict == "survived":
            print(f"[存活 ✗] {label}:{detail}", flush=True)
            survived.append(label)
        else:
            print(f"[被抓 ✓] {label}:{detail}", flush=True)
    print()
    print(f"共 {len(selected)} 個突變;存活 {len(survived)};設定錯誤 {len(config_errors)}")
    if survived:
        print("存活:", survived)
    if config_errors:
        print("設定錯誤:", config_errors)
    return 1 if survived or config_errors else 0


if __name__ == "__main__":
    sys.exit(main())
