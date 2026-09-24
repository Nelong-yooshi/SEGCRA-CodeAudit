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
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

MOD = pathlib.Path("orchestrator") / "dbt_render.py"

REGEX = r'r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}"'
NL = "\n"     # 突變點含多行時用它接,字串裡不寫跳脫比較好讀

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
        "    try:\n        proc = ctx.Process(target=_worker, daemon=True,",
        "    if True:\n        proc = ctx.Process(target=_worker, daemon=True,"),
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
        "            out.append(indent + _sentinel(token, idx + 1) + body)",
        "            out.append(_sentinel(token, idx + 1) + line)"),
    "空白行也插哨兵": (
        "        if idx in inside or not body or body.startswith(_LEFT_TRIM_TAGS):",
        "        if idx in inside or body.startswith(_LEFT_TRIM_TAGS):"),
    "{%- 開頭的行也插哨兵": (
        "        if idx in inside or not body or body.startswith(_LEFT_TRIM_TAGS):",
        "        if idx in inside or not body:"),
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
    for item in src.iterdir():
        if item.name in (".git",):
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
})


def _run_baseline(src: pathlib.Path, python: str, timeout: int) -> str | None:
    """複製一份未突變的專案,跑一次測試套件,確認完全乾淨才能開始判斷突變。

    「被抓到」的判斷依據是測試套件回傳碼非 0——如果套件本身就有一條失敗或收集
    (collect)不起來的測試(例如漏複製了某個依賴的目錄),**每個突變都會被誤判
    成被抓到**,結果全部失去意義,而且不會有任何警訊。回傳 None 代表基準乾淨;
    否則回傳失敗原因(供 main() 直接中止並印出來)。
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="sgc_mut_baseline_"))
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        copy_project(src, tmp)
        try:
            proc = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                                  cwd=tmp, capture_output=True, text=True, env=env,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"基準套件跑超過 {timeout} 秒未完成"
        if proc.returncode != 0:
            lines = proc.stdout.splitlines()
            tail = "\n".join(lines[-30:]) if lines else proc.stdout
            return f"基準套件(未突變)沒有全部通過,回傳碼 {proc.returncode}:\n{tail}"
        return None
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
    src = pathlib.Path(args.src)
    only = [s for s in args.only.split(",") if s]
    selected = {k: v for k, v in MUTANTS.items() if not only or any(o in k for o in only)}
    selected = dict(list(selected.items())[args.start:])

    if not args.skip_baseline:
        print("先跑一次未突變的基準套件,確認乾淨才開始判斷突變...", flush=True)
        problem = _run_baseline(src, args.python, args.timeout)
        if problem is not None:
            print(f"[中止] {problem}", flush=True)
            print("基準套件本身沒過,突變測試的「被抓到」結果沒有意義,不繼續執行。",
                 flush=True)
            return 1
        print("基準套件乾淨,開始跑突變。", flush=True)

    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    survived, config_errors = [], []
    for label, entry in selected.items():
        module, old, new = entry if len(entry) == 3 else ("dbt_render.py", *entry)
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="sgc_mut_"))
        try:
            copy_project(src, tmp)
            path = tmp / "orchestrator" / module
            text = path.read_text(encoding="utf-8")
            if text.count(old) != 1:
                print(f"[設定錯誤] {label}:突變點出現 {text.count(old)} 次", flush=True)
                config_errors.append(label)
                continue
            path.write_text(text.replace(old, new), encoding="utf-8")
            try:
                proc = subprocess.run([args.python, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"],
                                      cwd=tmp, capture_output=True, text=True, env=env,
                                      encoding="utf-8", errors="replace", timeout=args.timeout)
            except subprocess.TimeoutExpired:
                print(f"[被抓 ✓] {label}:測試套件卡住超過 {args.timeout} 秒", flush=True)
                continue
            lines = proc.stdout.splitlines()
            summary = next((l for l in reversed(lines)
                            if "passed" in l or "failed" in l or "error" in l), proc.stdout[-160:])
            if proc.returncode == 0:
                print(f"[存活 ✗] {label}:{summary}", flush=True)
                survived.append(label)
            else:
                first = next((l for l in lines if l.startswith(("FAILED", "ERROR"))), "")
                print(f"[被抓 ✓] {label}:{first.split(' - ')[0][:110]}", flush=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print()
    print(f"共 {len(selected)} 個突變;存活 {len(survived)};設定錯誤 {len(config_errors)}")
    if survived:
        print("存活:", survived)
    if config_errors:
        print("設定錯誤:", config_errors)
    return 1 if survived or config_errors else 0


if __name__ == "__main__":
    sys.exit(main())
