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
        '        _check_ident("來源", source_name)\n        _check_ident("表", table_name)\n'
        "        if project is None:\n            return relation(table_name)",
        "        if project is None:\n"
        "            return f'\"{database}\".\"{schema}\".\"{table_name}\"'"),
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
        "        return f'\"{database}\".\"{_check_ident(\"綱要\", rel_schema)}\"."
        "\"{_check_ident(\"表\", name)}\"'",
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
    '不掃 yml 中的 hook': ('dbt_impact.py',
     '            for target in resolve(refs.names):\n                callers_of.setdefault(target, set()).add((kind, path))',
     '            for target in ():\n                callers_of.setdefault(target, set()).add((kind, path))'),
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

# ---- 模型輸出格式不正確時不中斷審查(#21)
MUTANTS.update({
 "格式:不做正規化": ("pipeline.py",
     "        report = normalize_findings(report)    # 模型輸出的形狀不可信,先整理再往下走(#21)\n", ""),
 "格式:不補 major": ("pipeline.py",
     "        report = enforce_format(report)        # 模型輸出有格式錯誤 → major;須在 sanitize 之後\n", ""),
 "格式:major 排在 sanitize 之前(可被模型換掉)": ("pipeline.py",
     "        report = sanitize_findings(report)\n",
     "        report = enforce_format(report)\n        report = sanitize_findings(report)\n"
     "        report.pop(\"_format_issues\", None)\n"),
 "格式:補的是 minor": ("pipeline.py",
     '"severity": "major", "title": _FORMAT_TITLE,',
     '"severity": "minor", "title": _FORMAT_TITLE,'),
 "格式:finding 不是物件不計數": ("pipeline.py",
     "        if not isinstance(fd, dict):\n            dropped += 1\n            continue",
     "        if not isinstance(fd, dict):\n            continue"),
 "格式:severity 不是字串不計數": ("pipeline.py",
     "        if severity is not None and not isinstance(severity, str):\n            dropped += 1\n",
     "        if severity is not None and not isinstance(severity, str):\n"),
 "格式:severity 不是字串照樣往下傳": ("pipeline.py",
     "        if severity is not None and not isinstance(severity, str):\n            dropped += 1\n"
     "            continue",
     "        if False:\n            dropped += 1\n            continue"),
 "格式:標題格式錯的 finding 直接丟掉(決策變寬鬆)": ("pipeline.py",
     "        fixed += wrong\n        findings.append(fd)",
     "        if wrong:\n            dropped += 1\n            continue\n        findings.append(fd)"),
 "格式:空標題不補說明": ("pipeline.py",
     "                fd[key] = _FORMAT_NO_TITLE", "                pass"),
 "格式:標題缺漏也補說明(改變原本正常的輸入)": ("pipeline.py",
     "            if bad and key == \"title\" and not fd[key].strip():",
     "            if key == \"title\" and not fd[key].strip():"),
 "格式:標題是 null 不算格式錯誤": ("pipeline.py",
     '        null_title = "title" in fd and fd["title"] is None',
     '        null_title = False'),
 "格式:標題缺漏也算格式錯誤": ("pipeline.py",
     '        null_title = "title" in fd and fd["title"] is None',
     '        null_title = fd.get("title") is None'),
 "格式:巢狀太深轉不出字串就丟例外": ("pipeline.py",
     "    except RecursionError:\n        return \"\", True",
     "    except ZeroDivisionError:\n        return \"\", True"),
 "格式:null 也算格式錯誤(每個 MR 都多一條 major)": ("pipeline.py",
     "    if value is None:\n        return \"\", False\n    if isinstance(value, str):",
     "    if value is None:\n        return \"\", True\n    if isinstance(value, str):"),
 "格式:文字欄位型別不對不計數": ("pipeline.py",
     "        return str(value), True", "        return str(value), False"),
 "格式:字串清單不計數": ("pipeline.py",
     '        return "\\n".join(value), True', '        return "\\n".join(value), False'),
 "格式:file 型別不對不計數": ("pipeline.py",
     "        if file is not None and not isinstance(file, str):\n            wrong = True",
     "        if file is not None and not isinstance(file, str):\n            pass"),
 "格式:file 型別不對照樣往下傳": ("pipeline.py",
     '        fd["file"] = file if isinstance(file, str) else ""',
     '        fd["file"] = file if file is not None else ""'),
 "格式:line 轉不了就丟例外": ("pipeline.py",
     "    except (ValueError, OverflowError):\n        pass\n    return 0, True",
     "    except ZeroDivisionError:\n        pass\n    return 0, True"),
 "格式:line 轉不了不計數": ("pipeline.py",
     "        pass\n    return 0, True", "        pass\n    return 0, False"),
 "格式:line 是 null 也算格式錯誤": ("pipeline.py",
     '    if value is None or value == "":\n        return 0, False',
     '    if value is None or value == "":\n        return 0, True'),
 "格式:citations 不是清單不計數": ("pipeline.py",
     "            cites, wrong = [], True", "            cites = []"),
 "格式:citations 的項目不檢查": ("pipeline.py",
     "        kept = [c for c in cites if isinstance(c, dict)\n"
     "                and isinstance(c.get(\"source\", \"\"), (str, type(None)))]",
     "        kept = list(cites)"),
 "格式:citation 的 source 型別不檢查": ("pipeline.py",
     "        kept = [c for c in cites if isinstance(c, dict)\n"
     "                and isinstance(c.get(\"source\", \"\"), (str, type(None)))]",
     "        kept = [c for c in cites if isinstance(c, dict)]"),
 "格式:丟掉 citation 不計數": ("pipeline.py",
     "        wrong |= len(kept) != len(cites)\n", ""),
 "格式:findings 不是清單不記錄": ("pipeline.py",
     "    if bad_container or fixed or dropped:", "    if fixed or dropped:"),
 "格式:summary 不轉成字串": ("pipeline.py",
     '    report["summary"] = summary\n', ""),
 "格式:採用模型自己輸出的 _format_issues": ("pipeline.py",
     '    report.pop("_format_issues", None)\n', ""),
 "格式:major 回顯模型輸出": ("pipeline.py",
     '"被略過或修正的內容可能不完整,這次的審查結果需人工確認。",',
     '"被略過或修正的內容可能不完整,這次的審查結果需人工確認。" + str(report.get("findings")),'),
 "格式:major 用一般方式加入(同標題不取代)": ("pipeline.py",
     "    _put_program_finding(report, major)\n    return report\n\n\n# 引用來源裡指名的規則碼",
     "    report[\"findings\"].append(major)\n    return report\n\n\n# 引用來源裡指名的規則碼"),
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


# ---- 展開器從記憶體裡的專案檔案讀 macro(macro_files)
MUTANTS.update({
 "字典來源:給了也不用": (
     "    if code_root is None and macro_files is None:", "    if code_root is None:"),
 "字典來源:兩種來源同時給不拒絕": (
     "    if code_root is not None and macro_files is not None:", "    if False:"),
 "字典來源:不是字典不拒絕": (
     "    if not isinstance(macro_files, dict):", "    if False:"),
 "字典來源:路徑與內容的型別不檢查": (
     "        if not isinstance(path, str) or not isinstance(text, str):", "        if False:"),
 "字典來源:路徑分段不檢查(.. 與空段)": (
     "                or any(part in (\"\", \".\", \"..\") for part in path.split(\"/\"))):\n"
     "            raise DbtRenderError(\"macro_files 含不合法的路徑\")",
     "                or False):\n"
     "            raise DbtRenderError(\"macro_files 含不合法的路徑\")"),
 "字典來源:反斜線路徑不檢查": (
     "        if (\"\\\\\" in path or \":\" in path or path.startswith(\"/\")",
     "        if (\":\" in path or path.startswith(\"/\")"),
 "字典來源:磁碟代號與絕對路徑不檢查": (
     "        if (\"\\\\\" in path or \":\" in path or path.startswith(\"/\")",
     "        if (\"\\\\\" in path"),
 "字典來源:錯誤訊息回顯路徑": (
     "            raise DbtRenderError(\"macro_files 含不合法的路徑\")",
     "            raise DbtRenderError(f\"macro_files 含不合法的路徑:{path}\")"),
 "字典來源:目錄前綴不含斜線(macros_backup 也載入)": (
     "        prefix = name + \"/\"", "        prefix = name"),
 "字典來源:不限 .sql": (
     "                         if p.startswith(prefix) and p.endswith(\".sql\")),",
     "                         if p.startswith(prefix)),"),
 "字典來源:整串排序(與磁碟順序不同)": (
     "                        key=lambda item: item[0].split(\"/\"))",
     "                        key=lambda item: item[0])"),
 "字典來源:不排序(依字典的插入順序)": (
     "                        key=lambda item: item[0].split(\"/\"))",
     "                        key=lambda item: 0)"),
 "字典來源:render_model 沒把字典傳下去": (
     "macro_dirs, adapter, project, macro_files)", "macro_dirs, adapter, project)"),
 "字典來源:給了字典仍讀 model 旁邊的 macros": (
     "    elif source is None and macro_files is None:", "    elif source is None:"),
 "字典來源:目錄互相包含時同一個檔載入兩次(與自己同名)": (
     "    files = list(dict(files).items())\n", ""),
})

# ---- 取回 MR 那個 commit 的 dbt 專案並接進預掃(dbt_context)
MUTANTS.update({
 "專案準備:沒開 dbt 也取回": ("dbt_context.py",
     "    if not dbt_cfg or dbt_cfg.get(\"enabled\") is not True:", "    if not dbt_cfg:"),
 "專案準備:mock 模式也取回": ("dbt_context.py",
     "    if real_mode is not True:", "    if False:"),
 "專案準備:沒改到專案裡的 .sql 也取回": ("dbt_context.py",
     "    if not _needs_project(files, prefix, layout):\n        return None",
     "    if False:\n        return None"),
 "專案準備:目錄前綴不含斜線(dbt_sub_other 誤判)": ("dbt_context.py",
     "    return path[len(prefix) + 1:] if path.startswith(prefix + \"/\") else None",
     "    return path[len(prefix) + 1:] if path.startswith(prefix) else None"),
 "專案準備:head commit 編號不檢查": ("dbt_context.py",
     "    if not isinstance(sha, str) or not _SHA.fullmatch(sha):",
     "    if not isinstance(sha, str):"),
 "專案準備:head commit 對不上照做": ("dbt_context.py",
     "    if head_sha is not None and head_sha != sha:", "    if False:"),
 "專案準備:取回失敗當成成功": ("dbt_context.py",
     "    if not isinstance(project, DbtProject) or not project.ok:",
     "    if not isinstance(project, DbtProject):"),
 "專案準備:macro_files 連 model 一起收": ("dbt_context.py",
     "                   and any(path.startswith(d + \"/\") for d in project.macro_paths)}",
     "                   }"),
 "專案準備:非預期例外回顯": ("dbt_context.py",
     "_failed(prefix, f\"準備 dbt 專案時發生非預期錯誤({type(e).__name__})\", layout)",
     "_failed(prefix, f\"準備 dbt 專案時發生非預期錯誤({e})\", layout)"),
 "專案準備:不說根目錄設定檔已檢查(ref 找不到不敢判定)": ("dbt_context.py",
     "other_ref_names=extra, root_files_checked=True)",
     "other_ref_names=extra, root_files_checked=False)"),
 "專案準備:不給 seed / snapshot 名單": ("dbt_context.py",
     "other_ref_names=extra, root_files_checked=True)",
     "other_ref_names=None, root_files_checked=True)"),
 "專案接線:取回失敗仍當成沒事": ("pipeline.py",
     "            return sql, _DBT_FETCH_FAILED + dbt_ctx.error, True",
     "            return sql, None, True"),
 "專案接線:不用 head 的完整內容": ("pipeline.py",
     "    if head is not None and is_dbt_template(head):", "    if False:"),
 "專案接線:不是樣板的檔案也換成完整內容": ("pipeline.py",
     "    if head is not None and is_dbt_template(head):", "    if head is not None:"),
 "專案接線:專案取回失敗時不是 .sql 的檔也報失敗": ("pipeline.py",
     "        if rel.endswith(\".sql\") or is_dbt_template(sql):",
     "        if True:"),
 "專案接線:專案取回失敗時不是 .sql 的樣板退回沒有 macro": ("pipeline.py",
     "        if rel.endswith(\".sql\") or is_dbt_template(sql):",
     "        if rel.endswith(\".sql\"):"),
 "專案接線:專案取回失敗時片段看不出樣板的 .sql 當成沒事": ("pipeline.py",
     "        if rel.endswith(\".sql\") or is_dbt_template(sql):",
     "        if is_dbt_template(sql):"),
 "專案接線:專案外的檔案也給專案的 macro": ("pipeline.py",
     "f.get(\"path\", \"\"), sql, dbt_cfg, dbt_ctx if in_project else None)",
     "f.get(\"path\", \"\"), sql, dbt_cfg, dbt_ctx)"),
 "專案接線:不給專案的 macro 與專案資訊": ("pipeline.py",
     "    if dbt_ctx is not None and dbt_ctx.project is not None:\n        project = {",
     "    if False:\n        project = {"),
 "專案接線:不給專案資訊(ref 不驗證)": ("pipeline.py",
     "                   \"project\": dbt_ctx.info}", "                   }"),
 "專案接線:同名的 macro 不擋": ("pipeline.py",
     "    if rendered.ok and rendered.macro_conflicts:", "    if False:"),
 "專案接線:沒開 dbt 也看 dbt_ctx": ("pipeline.py",
     "        if dbt_enabled and not is_macro:",
     "        if not is_macro:"),
 "專案接線:review_mr 不取回專案": ("pipeline.py",
     "        dbt_ctx = prepare_dbt_context(mr, cfg.dbt)", "        dbt_ctx = None"),
 '專案接線:review_mr 沒把專案交給預掃': ('pipeline.py',
     '        pre = await prescan(hub, mr["files"], cfg.dbt, dbt_ctx, rendered)',
     '        pre = await prescan(hub, mr["files"], cfg.dbt, None, rendered)'),
 '專案接線:注入掃描不掃取回的完整內容': ('pipeline.py',
     'injection_hits = scan_mr(_mr_with_head_text(mr, dbt_ctx, rendered))',
     'injection_hits = scan_mr(_mr_with_head_text(mr, None, rendered))'),
 "專案接線:注入掃描只掃完整內容、漏掉原本的 diff": ("pipeline.py",
     "    return {**mr, \"files\": [*mr[\"files\"], *extra]} if extra else mr",
     "    return {**mr, \"files\": extra} if extra else mr"),
 "專案設定:project_dir 不檢查": ("config.py",
     "        project_dir = check_project_dir(section.get(\"project_dir\", \"\"))",
     "        project_dir = section.get(\"project_dir\", \"\")"),
})

# ---- MR 改到 macro 時檢查受影響的 model、dbt 的變更不可當成執行驗證通過
MUTANTS.update({
 "專案準備:macro 的判定不要求反查結果(不檢查也沒反查的空窗)": ("dbt_context.py",
     "and self.project is not None and self.impact is not None", "and self.project is not None"),
 "專案準備:只改到 model 也反查(多一次下載)": ("dbt_context.py",
     "    if all(path_kind(rel, project.model_paths, project.macro_paths, project.resource_paths) == \"model\"\n"
     "           for rel in changed):",
     "    if False:"),
 "專案準備:不取變更前的專案(無從比較)": ("dbt_context.py",
     "    base = load(mr.get(\"base_sha\"), prefix) if impact.affected_models else None",
     "    base = None"),
 "專案準備:只改屬性檔不取回": ("dbt_context.py",
     "_PROJECT_SUFFIXES = (\".sql\", \".yml\", \".yaml\")", "_PROJECT_SUFFIXES = (\".sql\",)"),
 "專案準備:改名前的舊路徑不算變更": ("dbt_context.py",
     "        for key in (\"path\", \"old_path\"):", "        for key in (\"path\",):"),
 "專案準備:取回失敗不標全部 model 可能受影響": ("dbt_context.py",
     "        all_models_possibly_affected=True))", "        all_models_possibly_affected=False))"),
 "未執行:專案取不回來不算改到 dbt": ("dbt_context.py",
     "        if self.error is not None or self.impact is not None:",
     "        if self.impact is not None:"),
 "未執行:有反查結果不算改到 dbt": ("dbt_context.py",
     "        if self.error is not None or self.impact is not None:",
     "        if self.error is not None:"),
 "未執行:head 是樣板但片段看不出來的不算": ("dbt_context.py",
     "            if (head is not None and is_dbt_template(head)) or is_dbt_template(text):",
     "            if is_dbt_template(text):"),
 "未執行:沒被收進來的檔片段有樣板也不算": ("dbt_context.py",
     "            if (head is not None and is_dbt_template(head)) or is_dbt_template(text):",
     "            if head is not None and is_dbt_template(head):"),
 "未執行:專案外的樣板也算": ("dbt_context.py",
     "            if not isinstance(f, dict) or self.relative(f.get(\"path\")) is None:",
     "            if not isinstance(f, dict):"),
 "受影響:範圍不明仍逐一展開": ("dbt_affected.py",
     "    if ctx.project is None or impact.all_models_possibly_affected:",
     "    if ctx.project is None:"),
 "受影響:MR 自己改到的 model 重複檢查": ("dbt_affected.py",
     "    todo = sorted((m for m in impact.affected_models if m.path not in in_mr),",
     "    todo = sorted((m for m in impact.affected_models),"),
 '受影響:數量上限不生效': ('dbt_affected.py',
     '        if index >= MAX_CHECKED_MODELS:\n            unchecked.append(model.path)',
     '        if False:\n            unchecked.append(model.path)'),
 '受影響:數量上限多檢查一個': ('dbt_affected.py',
     '        if index >= MAX_CHECKED_MODELS:\n            unchecked.append(model.path)',
     '        if index > MAX_CHECKED_MODELS:\n            unchecked.append(model.path)'),
 '受影響:查規格不限時間(卡住就一直等)': ('dbt_affected.py',
     '                spec = await _spec_for(hub, ctx, model.path, find_spec, specs,\n                                       deadline - clock())',
     '                spec = await _spec_for(hub, ctx, model.path, find_spec, specs,\n                                       MAX_SECONDS)'),
 '受影響:查規格不受數量上限約束': ('dbt_affected.py',
     '        if index < MAX_SPEC_LOOKUPS and clock() < deadline:',
     '        if clock() < deadline:'),
 "受影響:對到不同規格時挑一份": ("dbt_affected.py",
     "if len(codes) == 1 and not unchecked else None", "if len(codes) >= 1 and not unchecked else None"),
 "受影響:有 model 沒檢查到仍採用規格": ("dbt_affected.py",
     "if len(codes) == 1 and not unchecked else None", "if len(codes) == 1 else None"),
 '受影響:查規格不是依路徑': ('dbt_affected.py',
     'lambda: find_spec(hub, target, by_path=True)',
     'lambda: find_spec(hub, target, by_path=False)'),
 "受影響:查規格的路徑少了專案目錄": ("dbt_affected.py",
     "    repo_path = f\"{ctx.prefix}/{rel}\" if ctx.prefix else rel", "    repo_path = rel"),
 "受影響:查規格的例外往外丟": ("dbt_affected.py",
     "    except Exception:                        # noqa: BLE001 — 找不到規格不該讓審查中斷",
     "    except ZeroDivisionError:"),
 "受影響:規格沒有內容也算對到": ("dbt_affected.py",
     "    if not code or not text:", "    if not code:"),
 '受影響:只數規則個數、不比語句': ('dbt_affected.py',
     '    elif head.statements is not None and head.statements == base.statements:',
     '    elif False:'),
 '受影響:不與變更前比較(原本就有的也重報)': ('dbt_affected.py',
     '    if base_error is not None:\n        new = head.counts',
     '    if True:\n        new = head.counts'),
 '受影響:無法比較當成比較過(可能誤擋)': ('dbt_affected.py',
     '            base_error is not None, None)',
     '            False, None)'),
 '受影響:變更後展開失敗當成沒事': ('dbt_affected.py',
     '    if head_error is not None:\n        return (), False, head_error',
     '    if head_error is not None:\n        return (), False, None'),
 "受影響:專案有同名 macro 仍採用展開結果": ("dbt_affected.py",
     "    if rendered.macro_conflicts:", "    if False:"),
 "受影響:展開後無法解析當成沒有命中": ("dbt_affected.py",
     "        if rules.get(\"error\"):\n            return None, \"展開後的 SQL 無法解析,規則沒有執行\"",
     "        if False:\n            return None, \"展開後的 SQL 無法解析,規則沒有執行\""),
 "受影響:規則層回傳的形狀不對當成沒有命中": ("dbt_affected.py",
     "    if not isinstance(rules, list):\n        return None, \"規則層回傳了非預期的資料\"",
     "    if not isinstance(rules, list):\n        rules = []"),
 "受影響:資料庫名稱不符仍展開": ("dbt_affected.py",
     "    if dbt_cfg.get(\"database\") != DBT_DATABASE_PLACEHOLDER:\n        return None, \"dbt 的資料庫名稱設定不符,無法展開\"",
     "    if False:\n        return None, \"dbt 的資料庫名稱設定不符,無法展開\""),
 '受影響:檢核點算成問題': ('dbt_affected.py',
     '            new_hits=tuple((rule, sev) for rule, sev, _ in new if sev in _COMPARED),',
     '            new_hits=tuple((rule, sev) for rule, sev, _ in new),'),
 "受影響:沒有東西要說也補一條": ("dbt_affected.py",
     "            or check.models or check.unchecked):\n        return None",
     "            or check.models or check.unchecked):\n        pass"),
 "受影響:嚴重度會被後面較輕的蓋掉": ("dbt_affected.py",
     "        if _SEVERITY_RANK[level] > _SEVERITY_RANK[severity]:", "        if True:"),
 "受影響:全部 model 可能受影響不提高嚴重度": ("dbt_affected.py",
     "    if impact.all_models_possibly_affected:\n        raise_to(\"major\")",
     "    if impact.all_models_possibly_affected:\n        pass"),
 "受影響:反查不確定不提高嚴重度": ("dbt_affected.py",
     "    if impact.uncertain:\n        raise_to(\"major\")", "    if impact.uncertain:\n        pass"),
 "受影響:保守推定的 model 不提高嚴重度": ("dbt_affected.py",
     "        if not m.certain:\n            raise_to(\"major\")", "        if not m.certain:\n            pass"),
 "受影響:展開失敗不提高嚴重度": ("dbt_affected.py",
     "            raise_to(\"major\")\n            broken.append(", "            broken.append("),
 '受影響:無法比較的 blocker 也擋下': ('dbt_affected.py',
     '            raise_to("major" if m.no_baseline or worst == "blocker" else worst)',
     '            raise_to("major" if worst == "blocker" else worst)'),
 '受影響:多出的命中不提高嚴重度': ('dbt_affected.py',
     '            raise_to("major" if m.no_baseline or worst == "blocker" else worst)',
     '            pass'),
 "受影響:沒檢查到的 model 不提高嚴重度": ("dbt_affected.py",
     "    if check.unchecked:\n        raise_to(\"major\")", "    if check.unchecked:\n        pass"),
 "受影響:清單不截斷": ("dbt_affected.py",
     "    return \"、\".join(items[:_MAX_LISTED]) + more", "    return \"、\".join(items) + more"),
 "受影響:原因不截斷": ("dbt_affected.py",
     "    return text[:_MAX_REASON_CHARS]", "    return text"),
 "受影響:原因不清控制字元": ("dbt_affected.py",
     "    text = \"\".join(ch if ch.isprintable() else \" \" for ch in str(text))",
     "    text = str(text)"),
 "受影響:路徑不帶專案目錄": ("dbt_affected.py",
     "        return f\"{prefix}/{rel}\" if prefix else rel", "        return rel"),
 "受影響:review_mr 不檢查受影響的 model": ("pipeline.py",
     "        affected = await check_affected_models(hub, mr, cfg.dbt, dbt_ctx, find_spec=find_spec)",
     "        affected = None"),
 "受影響:review_mr 不補影響的 finding": ("pipeline.py",
     "        report = enforce_dbt_impact(report, affected, dbt_ctx)  # macro 等變更影響的其他 model\n", ""),
 "受影響:影響的 finding 用一般方式加入(同標題不取代)": ("pipeline.py",
     "        _put_program_finding(report, impact)",
     "        report.setdefault(\"findings\", []).append(impact)"),
 "受影響:MR 有規則碼時也改用受影響 model 的規格": ("pipeline.py",
     "        if spec_code is None and spec_text is None and affected is not None and affected.spec:",
     "        if spec_text is None and affected is not None and affected.spec:"),
 "受影響:不採用受影響 model 的規格": ("pipeline.py",
     "        if spec_code is None and spec_text is None and affected is not None and affected.spec:",
     "        if False:"),
 "受影響:macro 檔仍當成 model 展開": ("pipeline.py",
     "        if is_macro:\n            entry[\"dbt_macro\"] = True\n        elif dbt_error is not None:",
     "        if dbt_error is not None:"),
 "受影響:macro 檔照報無法解析": ("pipeline.py",
     "            if rules.get(\"error\") and not is_macro:", "            if rules.get(\"error\"):"),
 "受影響:沒開 dbt 也把 macro 檔另外處理": ("pipeline.py",
     "        is_macro = dbt_enabled and dbt_ctx is not None and dbt_ctx.is_macro(f.get(\"path\"))",
     "        is_macro = dbt_ctx is not None and dbt_ctx.is_macro(f.get(\"path\"))"),
 "未執行:執行驗證拿到的是只有片段的 MR": ("pipeline.py",
     "        spec_result = await run_spec_exec(cfg, hub, _mr_for_spec_exec(mr, dbt_ctx),\n"
     "                                          spec_code, spec_text)",
     "        spec_result = await run_spec_exec(cfg, hub, mr,\n"
     "                                          spec_code, spec_text)"),
 "未執行:不是樣板的檔也帶完整內容": ("pipeline.py",
     "if head is not None and is_dbt_template(head) else f)", "if head is not None else f)"),
 "未執行:review_mr 不補": ("pipeline.py",
     "        report = enforce_dbt_unexecuted(report, mr, dbt_ctx, cfg.dbt)  # dbt 的變更沒被執行驗證到\n", ""),
 "未執行:執行驗證沒通過也補(重複)": ("pipeline.py",
     "    if (report.get(\"_spec_exec\") or {}).get(\"passed\") is not True:", "    if False:"),
 "未執行:沒改到 dbt 也補": ("pipeline.py",
     "    if not _mr_touches_dbt(mr, dbt_ctx, dbt_cfg):",
     "    if False:"),
 "未執行:沒開 dbt 也補": ("pipeline.py",
     "    if not dbt_cfg or dbt_cfg.get(\"enabled\") is not True:\n        return False\n    files = mr.get(",
     "    if False:\n        return False\n    files = mr.get("),
 "未執行:沒有取回專案時不看樣板標記(mock 模式的混合 MR)": ("pipeline.py",
     "    return any(is_dbt_template(f.get(\"full_content\") or f.get(\"diff\") or \"\")",
     "    return any(False and is_dbt_template(f.get(\"full_content\") or f.get(\"diff\") or \"\")"),
 "未執行:有取回專案時不問它": ("pipeline.py",
     "    if dbt_ctx is not None and dbt_ctx.touches_dbt(files):",
     "    if False:"),
 "未執行:review_mr 沒把 dbt 設定傳進去": ("pipeline.py",
     "report = enforce_dbt_unexecuted(report, mr, dbt_ctx, cfg.dbt)",
     "report = enforce_dbt_unexecuted(report, mr, dbt_ctx)"),
 "受影響:檢查時的非預期錯誤讓審查中斷": ("dbt_affected.py",
     "    except Exception as e:                   # noqa: BLE001 — 審查不因為這一步中斷,但也不能當成沒事",
     "    except ZeroDivisionError as e:"),
 "受影響:檢查出錯不標範圍不明": ("dbt_affected.py",
     "            ctx.impact, all_models_possibly_affected=True,",
     "            ctx.impact, all_models_possibly_affected=ctx.impact.all_models_possibly_affected,"),
 "受影響:檢查出錯時回顯例外內容": ("dbt_affected.py",
     "發生非預期錯誤({type(e).__name__}),沒有完成檢查",
     "發生非預期錯誤({e}),沒有完成檢查"),
 "未執行:補的是 minor": ("pipeline.py",
     "\"severity\": \"major\", \"title\": _DBT_UNEXECUTED_TITLE,",
     "\"severity\": \"minor\", \"title\": _DBT_UNEXECUTED_TITLE,"),
 "專案準備:專案資訊整理失敗仍繼續(沒有專案資訊)": ("dbt_context.py",
     "    if summary is None:\n        return _failed(prefix, \"dbt 專案的屬性檔解析逾時或失敗\", project.layout)",
     "    if summary is None:\n        summary = (DbtProjectInfo(), {})"),
 "專案準備:專案資訊不在子行程裡整理": ("dbt_context.py",
     "        return _prepare(mr, prefix, load, summarize or _summarize_isolated, layout)",
     "        return _prepare(mr, prefix, load, summarize or _summarize, layout)"),
 "專案準備:子行程回傳的形狀不檢查": ("dbt_context.py",
     "    if (isinstance(result, tuple) and len(result) == 2\n            and isinstance(result[0], DbtProjectInfo) and isinstance(result[1], dict)):",
     "    if result is not None:"),
 "專案準備:變更前的專案資訊整理失敗仍拿來比較": ("dbt_context.py",
     "    base_summary = summarize(base) if isinstance(base, DbtProject) and base.ok else None",
     "    base_summary = (summarize(base) or (DbtProjectInfo(), {})) if isinstance(base, DbtProject) and base.ok else None"),
})

# ---- mock 模式的 dbt 專案來源(fixture 目錄)
MUTANTS.update({
 "範例專案:fixture 有放專案也不用": ("dbt_context.py",
     "        if not isinstance(mr, dict) or _fixture_project_dir(mr.get(\"sha\")) is None:\n            return None",
     "        if True:\n            return None"),
 "範例專案:fixture 沒放專案也往下走(變成取回失敗)": ("dbt_context.py",
     "        if not isinstance(mr, dict) or _fixture_project_dir(mr.get(\"sha\")) is None:",
     "        if not isinstance(mr, dict):"),
 "範例專案:呼叫端給的 load 被換掉": ("dbt_context.py",
     "        if load is load_dbt_project:\n            load = _load_from_fixtures",
     "        if True:\n            load = _load_from_fixtures"),
 "範例專案:不用 fixture 的專案來源(去打真的下載)": ("dbt_context.py",
     "        if load is load_dbt_project:\n            load = _load_from_fixtures",
     "        if False:\n            load = _load_from_fixtures"),
 "範例專案:變更清單沒有標成完整(反查做不了)": ("dbt_context.py",
     "        mr = {**mr, \"truncated\": mr.get(\"truncated\", False)}",
     "        mr = dict(mr)"),
 "範例專案:MR 明講不完整也當成完整": ("dbt_context.py",
     "        mr = {**mr, \"truncated\": mr.get(\"truncated\", False)}",
     "        mr = {**mr, \"truncated\": False}"),
 "範例專案:真實模式也去看 fixture": ("dbt_context.py",
     "    if real_mode is not True:\n        # mock 模式",
     "    if True:\n        # mock 模式"),
 "範例專案:commit 編號不檢查就接進路徑": ("dbt_context.py",
     "    if not (isinstance(sha, str) and _SHA.fullmatch(sha)):",
     "    if not isinstance(sha, str):"),
 "範例專案:不是目錄也算有專案": ("dbt_context.py",
     "    return path if path.is_dir() else None", "    return path if path.exists() else None"),
 "範例專案:指定子目錄時整包都打": ("dbt_context.py",
     "            if path and not rel.startswith(path + \"/\"):\n                continue",
     "            if False:\n                continue"),
 "範例專案:子目錄的前綴不含斜線(dbt_sub_other 也打進去)": ("dbt_context.py",
     "            if path and not rel.startswith(path + \"/\"):",
     "            if path and not rel.startswith(path):"),
 "範例專案:打包後的大小不檢查": ("dbt_context.py",
     "    if len(data) > max_bytes:", "    if False:"),
 "範例專案:檔案數不檢查": ("dbt_context.py",
     "    if len(members) > MAX_FIXTURE_FILES:", "    if False:"),
 "範例專案:頂層目錄不帶子目錄名(與 GitLab 的形狀不同仍要讀得到)": ("dbt_context.py",
     "            info = tarfile.TarInfo(f\"{top}/{rel}\")", "            info = tarfile.TarInfo(rel)"),
})


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
 'yml:動態不列入': ('dbt_impact.py',
     '            if is_dynamic(refs):\n                dynamic_scopes.append((kind, path))',
     '            if False:\n                dynamic_scopes.append((kind, path))'),
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
     '        return _RELATION_NOTICE if usage["used"] else None',
     "        return None"),
 "relation_notice 沒用到 ref()/source() 也照樣回報": (
     '        return _RELATION_NOTICE if usage["used"] else None',
     "        return _RELATION_NOTICE"),
 "relation_notice 不排除 macro 檔模組層的 ref()": (
     '    env.segcra_relation_usage["used"] = False' + NL,
     ""),
 # ---- ref()/source() 精確度(有專案資訊時):找得到照 dbt 展開、確定不存在就失敗、
 #      無法確定才提醒
 "關聯:確定不存在也放行": (
     '        if complete and not usage.get("loading_macros"):',
     "        if False:"),
 "關聯:載入 macro 檔時也判定不存在(冤枉開發者)": (
     '        if complete and not usage.get("loading_macros"):',
     "        if complete:"),
 "關聯:載入 macro 後旗標不重設": (
     '    env.segcra_relation_usage["loading_macros"] = False' + NL,
     ""),
 "關聯:ref 不查專案": (
     "        if project is not None:\n            check_ref(",
     "        if False:\n            check_ref("),
 "關聯:其他套件當成本專案": (
     "        if package is not None and package != project.project_name:",
     "        if False:"),
 "關聯:改表名的設定不標不確定": (
     "            if project.global_uncertain or name in project.uncertain_models:",
     "            if False:"),
 "關聯:source 不套用 sources.yml": (
     "        return relation(identifier or table_name, declared_schema or source_name)",
     "        return relation(table_name)"),
 "關聯:source 預設 schema 用 dbo 而非來源名": (
     "declared_schema or source_name)",
     "declared_schema or schema)"),
 "關聯:source 樣板值不標不確定": (
     "        if key in project.uncertain_sources:",
     "        if False:"),
 "關聯:有專案資訊仍用舊提醒": (
     '    return _RELATION_UNCERTAIN_NOTICE if usage["uncertain"] else None',
     '    return _RELATION_NOTICE if usage["used"] else None'),
 "關聯:project 型別不檢查": (
     "    if project is not None and not isinstance(project, DbtProjectInfo):",
     "    if False:"),
 "專案解析:輸入形狀不檢查": ("dbt_relations.py",
     "    if (not isinstance(files, dict) or isinstance(model_paths, str)",
     "    if (False or isinstance(model_paths, str)"),
 "專案解析:沒確認根目錄也當作沒用套件": ("dbt_relations.py",
     "    has_packages = (root_files_checked is not True",
     "    has_packages = (False"),
 "專案解析:根目錄確認旗標接受任何真值": ("dbt_relations.py",
     "    has_packages = (root_files_checked is not True",
     "    has_packages = (not root_files_checked"),
 "專案解析:套件不影響完整性": ("dbt_relations.py",
     "    complete = properties_ok and project_ok and not has_packages",
     "    complete = properties_ok and project_ok"),
 "專案解析:沒有 seed 名單也算完整": ("dbt_relations.py",
     "        refs_complete=complete and other_ref_names is not None,",
     "        refs_complete=complete,"),
 "專案解析:屬性檔看不懂仍算完整": ("dbt_relations.py",
     "            except _Unreadable:\n                properties_ok = False",
     "            except _Unreadable:\n                pass"),
 "專案解析:檔內 config 改表名不偵測": ("dbt_relations.py",
     "            if _config_may_rename(content):",
     "            if False:"),
 "專案解析:屬性檔 config 改表名不偵測": ("dbt_relations.py",
     "(isinstance(config, dict) and _has_name_key(config))",
     "False"),
 "專案解析:generate_*_name 不偵測": ("dbt_relations.py",
     "            if _GENERATE_NAME_MACRO.search(content):",
     "            if False:"),
 "專案解析:dbt_project.yml 的改表名設定不偵測": ("dbt_relations.py",
     "        if _sets_names(raw.get(block)):",
     "        if False:"),
 "專案解析:目錄(物件值)也當成設定": ("dbt_relations.py",
     "                if key in _NAME_KEYS and not isinstance(value, dict):",
     "                if key in _NAME_KEYS:"),
 "專案解析:別名炸彈不記走過的節點": ("dbt_relations.py",
     "        if id(cur) in seen:\n            continue",
     "        if False:\n            continue"),
 "專案解析:樣板值照樣採用": ("dbt_relations.py",
     "            sources[key] = (None if _templated(schema) else schema,",
     "            sources[key] = (schema,"),
 "專案解析:屬性檔沒有處理量上限": ("dbt_relations.py",
     "        if self.left < 0:\n            raise _Unreadable",
     "        if False:\n            raise _Unreadable"),
 "專案解析:處理量上限差一筆": ("dbt_relations.py",
     "        if self.left < 0:", "        if self.left < -1:"),
 "專案解析:處理量上限每個檔各自計算": ("dbt_relations.py",
     "uncertain_models, budget)", "uncertain_models, _Budget(MAX_PROPERTY_ENTRIES))"),
 "專案解析:來源不計入處理量": ("dbt_relations.py",
     "        budget.spend()\n        if not isinstance(src", "        if not isinstance(src"),
 "專案解析:表不計入處理量": ("dbt_relations.py",
     "            budget.spend()\n            if not isinstance(table",
     "            if not isinstance(table"),
 "專案解析:model 不計入處理量": ("dbt_relations.py",
     "        budget.spend()\n        if not isinstance(model", "        if not isinstance(model"),
 "專案解析:設定鍵檢查走訪整個 config": ("dbt_relations.py",
     "    return (any(key in mapping for key in _ENTRY_NAME_KEYS)",
     "    return (bool(_ENTRY_NAME_KEYS & set(mapping))"),
 # ---- #19 review:會被誤判成「確定」的寫法(字典 / ** / ephemeral / Python model)
 "專案解析:config.set 不偵測": ("dbt_relations.py",
     '    if _CONFIG_SET.search(text):\n        return True\n',
     ''),
 "專案解析:config.set 中間有空白就認不得": ("dbt_relations.py",
     'r"""\\bconfig\\s*(?:\\.\\s*set\\b|',
     'r"""\\bconfig(?:\\.set\\b|'),
 "專案解析:config 的中括號取用不偵測": ("dbt_relations.py",
     '|\\[\\s*[\'"]set[\'"]\\s*\\]|\\|\\s*attr\\b)',
     '|\\|\\s*attr\\b)'),
 "專案解析:config 的 attr 過濾器不偵測": ("dbt_relations.py",
     '|\\[\\s*[\'"]set[\'"]\\s*\\]|\\|\\s*attr\\b)',
     '|\\[\\s*[\'"]set[\'"]\\s*\\])'),
 "專案解析:config 這個字出現就算(註解與區域變數也算)": ("dbt_relations.py",
     'r"""\\bconfig\\s*(?:\\.\\s*set\\b|',
     'r"""\\bconfig\\b|\\bconfig\\s*(?:\\.\\s*set\\b|'),
 "專案解析:snapshot 的 target_schema 不算改表名": ("dbt_relations.py",
     '    key for name in ("target_schema", "target_database") for key in (name, "+" + name))',
     '    key for name in ("target_database",) for key in (name, "+" + name))'),
 "專案解析:snapshot 的 target_database 不算改表名": ("dbt_relations.py",
     '    key for name in ("target_schema", "target_database") for key in (name, "+" + name))',
     '    key for name in ("target_schema",) for key in (name, "+" + name))'),
 "專案解析:dbt_project.yml 的 +target_schema 讓整個專案無法確定": ("dbt_relations.py",
     "                if key in _NAME_KEYS and not isinstance(value, dict):",
     "                if key in _ENTRY_NAME_KEYS and not isinstance(value, dict):"),
 "專案解析:呼叫端提供的名稱當成確定": ("dbt_relations.py",
     '    uncertain_models.update(other_ref_names or ())\n',
     ''),
 "專案解析:config 看不懂的寫法當成沒改名": ("dbt_relations.py",
     "            if m is None:\n                return True",
     "            if m is None:\n                break"),
 "專案解析:config 的 ephemeral 不偵測": ("dbt_relations.py",
     '(key == "materialized"', "(False"),
 "專案解析:config 沒有右括號時當成看得懂": ("dbt_relations.py",
     "    if bodies is None:\n        return True",
     "    if bodies is None:\n        return False"),
 "專案解析:config 不計巢狀括號": ("dbt_relations.py",
     "            depth += 1                      # ( 或巢狀的 config(",
     "            pass"),
 "專案解析:Python model 不收進名單": ("dbt_relations.py",
     "            models.add(name)\n            uncertain_models.add(name)\n",
     "            uncertain_models.add(name)\n"),
 "專案解析:Python model 判成確定(內容沒讀)": ("dbt_relations.py",
     "            models.add(name)\n            uncertain_models.add(name)\n",
     "            models.add(name)\n"),
 "專案解析:同名的 model 不偵測": ("dbt_relations.py",
     "    uncertain_models.update(name for name, count in model_files.items() if count > 1)",
     "    pass"),
 "專案解析:macro 裡的 config() 不偵測": ("dbt_relations.py",
     "            elif _config_may_rename(content):", "            elif False:"),
 "專案解析:config 的 enabled=false 不偵測": ("dbt_relations.py",
     '            if key == "enabled" and value.lower() != "true":', "            if False:"),
 "專案解析:屬性檔的 enabled 不偵測": ("dbt_relations.py",
     "            or any(key in mapping and _maybe_disabled(mapping[key]) for key in _ENABLED_KEYS))",
     "            or False)"),
 "專案解析:dbt_project.yml 的 enabled 不偵測": ("dbt_relations.py",
     "                if key in _ENABLED_KEYS and not isinstance(value, dict) and _maybe_disabled(value):",
     "                if False:"),
 "專案解析:停用的 source 不偵測": ("dbt_relations.py",
     "                    or _source_maybe_disabled(src) or _source_maybe_disabled(table)):",
     "                    or False):"),
 "專案解析:source 的 enabled 只看 config 外層": ("dbt_relations.py",
     '    return ((isinstance(config, dict) and "enabled" in config and _maybe_disabled(config["enabled"]))',
     "    return (False"),
 "專案解析:dbt_project.yml 的 sources 區塊不影響 source()": ("dbt_relations.py",
     "    if all_sources_uncertain:\n        # dbt_project.yml",
     "    if False:\n        # dbt_project.yml"),
 "專案解析:dbt_project.yml 看不懂時 source() 仍算確定": ("dbt_relations.py",
     '        return None, "dbt_project.yml 無法解析", False, True\n    if not isinstance(raw, dict):',
     '        return None, "dbt_project.yml 無法解析", False, False\n    if not isinstance(raw, dict):'),
 "專案解析:屬性檔的 seeds / snapshots 不讀": ("dbt_relations.py",
     '    for model in (*_list(doc.get("models")), *_list(doc.get("seeds")), *_list(doc.get("snapshots"))):',
     '    for model in _list(doc.get("models")):'),
 "專案解析:屬性檔的 materialized 不偵測": ("dbt_relations.py",
     "            or any(key in mapping and _materialization_unknown(mapping[key])",
     "            or any(False"),
 "專案解析:dbt_project.yml 的 materialized 不偵測": ("dbt_relations.py",
     "                if (key in _MATERIALIZED_KEYS and not isinstance(value, dict)",
     "                if (False and not isinstance(value, dict)"),
 "專案解析:樣板的 materialized 當成確定": ("dbt_relations.py",
     '    return not isinstance(value, str) or _templated(value) or value.strip().lower() == "ephemeral"',
     '    return not isinstance(value, str) or value.strip().lower() == "ephemeral"'),
 "專案解析:config 掃描逐次切片(平方時間)": ("dbt_relations.py",
     "        while _NON_SPACE.search(body, pos):",
     "        while body[pos:].strip():"),
 "專案解析:有 libyaml 也用純 Python 載入器": ("dbt_relations.py",
     "        if _LIBYAML:", "        if False:"),
})

# ---- 審查模型只拿得到唯讀工具(#17):清單、執行入口、被拒絕時的揭露
MUTANTS.update({
 "工具:寫判例開放給模型": ("tool_hub.py",
     '    "memory__lookup_similar_reviews",\n    "load_skill",\n})',
     '    "memory__lookup_similar_reviews", "memory__record_feedback",\n    "load_skill",\n})'),
 "工具:清單不過濾(全部交給模型)": ("tool_hub.py",
     "        return [schema for name, schema in self.tools.items() if name in MODEL_TOOLS]",
     "        return list(self.tools.values())"),
 "工具:執行入口不擋": ("tool_hub.py",
     "        if full_name not in MODEL_TOOLS:\n            if full_name in self._functions",
     "        if False:\n            if full_name in self._functions"),
 "工具:被拒絕不記錄": ("tool_hub.py",
     "                self.refused_calls.append(full_name)",
     "                pass"),
 "工具:編造的名稱也記錄(雜訊 finding)": ("tool_hub.py",
     "            if full_name in self._functions or full_name in self.local_tools:\n"
     "                self.refused_calls.append(full_name)",
     "            if True:\n"
     "                self.refused_calls.append(full_name)"),
 "工具:審查迴圈直接呼叫(繞過白名單)": ("agent.py",
     "                result = await hub.call_from_model(tc.function.name, args)",
     "                result = await hub.call(tc.function.name, args)"),
 "工具:被拒絕不補 finding": ("pipeline.py",
     "        report = enforce_tool_refusals(report, hub.refused_calls)  # 模型要求了寫入工具\n",
     ""),
 "工具:被拒絕只補 info(可自動放行)": ("pipeline.py",
     '    finding = {"file": "(MR 內容)", "line": 0, "severity": "major",',
     '    finding = {"file": "(MR 內容)", "line": 0, "severity": "info",'),
 "工具:改回直接附加(模型同標題的 info 可取代)": ("pipeline.py",
     "    _put_program_finding(report, finding)\n    return report",
     '    report.setdefault("findings", []).insert(0, finding)\n    return report'),
 "工具:MR 編號不檢查(get_mr_diff 可打別的端點)": ("../toolbox/gitlab.py",
     "    mr_id = _check_mr_id(mr_id)\n    if REAL_MODE:",
     "    if REAL_MODE:"),
 "工具:mock 讀 fixture 不檢查 MR 編號(路徑穿越)": ("../toolbox/gitlab.py",
     '    f = FIXTURES_DIR / f"mr_{_check_mr_id(mr_id)}.json"',
     '    f = FIXTURES_DIR / f"mr_{mr_id}.json"'),
 "工具:MR 編號接受 Unicode 數字": ("../toolbox/gitlab.py",
     "    if not (mr_id.isascii() and mr_id.isdigit() and len(mr_id) <= 10):",
     "    if not (mr_id.isdigit() and len(mr_id) <= 10):"),
 "工具:MR 編號長度不限": ("../toolbox/gitlab.py",
     "    if not (mr_id.isascii() and mr_id.isdigit() and len(mr_id) <= 10):",
     "    if not (mr_id.isascii() and mr_id.isdigit()):"),
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
     "    if not isinstance(value, list) or (not value and not allow_empty) or len(value) > MAX_PATHS:",
     "    if not isinstance(value, list) or (not value and not allow_empty):"),
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
 'MR反查:額外理由不標全部受影響': ('dbt_mr_impact.py',
     '            all_models_possibly_affected=True)\n    return report',
     '            all_models_possibly_affected=report.all_models_possibly_affected)\n    return report'),
 "MR反查:反查看不到 dbt_project.yml 的 hook": ("dbt_mr_impact.py",
     '    if "dbt_project.yml" in project.root_files:',
     "    if False:"),
 "MR反查:snapshot / seed 的內容也交給反查": ("dbt_mr_impact.py",
     "    files = {p: c for p, c in project.files.items() if _in_dirs(p, dirs)}",
     "    files = dict(project.files)"),
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

# ---- get_mr_diff 帶出 diff_refs 的三個 sha(#15 接線與 #22 共用)
MUTANTS.update({
 "diff_refs:real 模式不帶出": ("../toolbox/gitlab.py",
     '"sha": mr.get("sha", ""), **_diff_refs(mr), "files": files,',
     '"sha": mr.get("sha", ""), "files": files,'),
 "diff_refs:mock 模式不帶出": ("../toolbox/gitlab.py",
     '"sha": mr.get("sha", f"mock-{mr_id}"), **_diff_refs(mr),',
     '"sha": mr.get("sha", f"mock-{mr_id}"),'),
 "diff_refs:不是完整 commit 編號也帶出": ("../toolbox/gitlab.py",
     "isinstance(v := refs.get(k), str) and _SHA.fullmatch(v) else None",
     "isinstance(v := refs.get(k), str) else None"),
 "diff_refs:只比對開頭(結尾可夾帶)": ("../toolbox/gitlab.py",
     "isinstance(v := refs.get(k), str) and _SHA.fullmatch(v) else None",
     "isinstance(v := refs.get(k), str) and _SHA.match(v) else None"),
 "diff_refs:不是物件也照用": ("../toolbox/gitlab.py",
     "    refs = refs if isinstance(refs, dict) else {}",
     "    refs = refs or {}"),
 "diff_refs:少帶 start_sha": ("../toolbox/gitlab.py",
     '_DIFF_REF_KEYS = ("base_sha", "head_sha", "start_sha")',
     '_DIFF_REF_KEYS = ("base_sha", "head_sha")'),
})

# ---- R005 讀取執行環境(#26 第 7 項):規則層命中後,模型一定看得到 secure-sql 的說明
MUTANTS.update({
 "R005:命中時不強制載入 secure-sql": ("pipeline.py",
     '_SECURE_SQL_RULES = ("R004", "R005", "H004")',
     '_SECURE_SQL_RULES = ("R004", "H004")'),
 "R005:強制載入的判斷寫死成不載": ("pipeline.py",
     "        if \"secure-sql\" in skills and _needs_secure_sql(pre):",
     "        if False:"),
 # 以下四項是 #27 review 補齊的,各自對應一個真實的漏抓:
 # 三段式名稱(master.sys.databases)、系統目錄只列幾個名字、夾在函式名與括號之間
 # 的註解、不帶括號的身分函式。
 "R005:系統目錄的 lookbehind 連點一起排除(三段式名稱漏抓)": ("../toolbox/sqltools.py",
     r'r"(?i)(?<!\w)\[?sys\]?" + _GAP',
     r'r"(?i)(?<![\w.])\[?sys\]?" + _GAP'),
 "R005:系統目錄只認列舉的那幾個名字": ("../toolbox/sqltools.py",
     r'+ _GAP + r"\." + _GAP + r"\[?\w+\]?"',
     r'+ _GAP + r"\." + _GAP + r"\[?(databases|objects|tables)\]?"'),
 "R005:函式名與括號之間只容許空白(夾註解就漏抓)": ("../toolbox/sqltools.py",
     r'_GAP = r"(?:\s|/\*(?:[^*]|\*(?!/))*\*/|--[^\n]*\n)*"',
     r'_GAP = r"\s*"'),
 "R005:不抓不帶括號的身分函式": ("../toolbox/sqltools.py",
     r'(SYSTEM_USER|CURRENT_USER|SESSION_USER)\b"',
     r'(SYSTEM_USER)\b"'),
 # review 的小建議:「沙盒」範圍太廣,同一個檔案裡其他 major 提到沙盒就會讓 R005
 # 被當成已經報過。
 "R005:已報判定關鍵詞含「沙盒」(範圍過廣)": ("pipeline.py",
     '"R005": ["執行環境", "DB_NAME", "HOST_NAME", "SUSER_NAME", "SERVERNAME", "sys."],',
     '"R005": ["執行環境", "DB_NAME", "HOST_NAME", "SUSER_NAME", "SERVERNAME", "sys.", "沙盒"],'),
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
     '            if i not in covered and rank >= need[i]:',
     '            if i not in covered:'),
 "規則補報:嚴重度差一級也算報過": ("pipeline.py",
     '            if i not in covered and rank >= need[i]:',
     '            if i not in covered and rank >= need[i] - 1:'),
 "規則補報:summary 提到也算報過": ("pipeline.py",
     '        text = " ".join(str(f.get(k) or "") for k in ("title", "detail", "suggestion"))',
     "        text = json.dumps(report, ensure_ascii=False)"),
 "規則補報:檔名也拿來比對關鍵詞": ("pipeline.py",
     '        text = " ".join(str(f.get(k) or "") for k in ("title", "detail", "suggestion"))',
     "        text = json.dumps(f, ensure_ascii=False)"),
 "規則補報:不認得的嚴重度當成最輕": ("pipeline.py",
     '    need = [_SEVERITY_RANK.get(h.get("severity", "major"), _SEVERITY_RANK["blocker"])',
     '    need = [_SEVERITY_RANK.get(h.get("severity", "major"), 0)'),
 # ---- 同一個檔案有多處相同規則的命中(#28 review)
 "規則補報:同檔有一條報過就全部不補(一條抵多處)": ("pipeline.py",
     '    return [h for i, h in enumerate(hits) if i not in covered]',
     '    return [] if covered else list(hits)'),
 "規則補報:引用同一句的兩條 finding 各抵一處": ("pipeline.py",
     '        (quoted if refs else generic).append((rank, refs or range(len(hits))))',
     '        generic.append((rank, range(len(hits))))'),
 "規則補報:沒引用語句的 finding 先對應(會多補)": ("pipeline.py",
     '    for n, (rank, options) in enumerate(quoted + generic):   # 有引用語句的先對應',
     '    for n, (rank, options) in enumerate(generic + quoted):'),
 "規則補報:語句比對分大小寫與空白": ("pipeline.py",
     '    return " ".join(str(text or "").split()).lower()',
     '    return str(text or "")'),
 "規則補報:多處命中時說明不寫數量": ("pipeline.py",
     '                    "detail": detail + _count_note(code, len(hits), len(missing), n),',
     '                    "detail": detail,'),
 "補報:只有一處命中時也加數量說明(既有文字被改動)": ("pipeline.py",
     '    if total <= 1:',
     '    if total <= 0:'),
 "規則補報:補的說明回顯命中的語句": ("pipeline.py",
     '                detail = h.get("message", "")',
     '                detail = h.get("message", "") + str(h.get("statement", ""))'),
})

# ---- #16 review 後續:補報只認同一個檔案、檢核點只認同檔 finding 的回應
MUTANTS.update({
 "同檔:不比對檔案(別的檔案報過就算)": ("pipeline.py",
     '        if f.get("file") != path:\n            continue\n',
     ""),
 "檢核點:比對報告全文(summary 也算回應)": ("pipeline.py",
     '            missing, exact = _coverage(before, entry["path"], _HINT_KEYWORDS.get(code, [code]),\n                                       [{**h, "severity": "info"} for h in hits])',
     '            missing, exact = ([] if any(k in json.dumps(report, ensure_ascii=False)\n                                        for k in _HINT_KEYWORDS.get(code, [code])) else hits), True'),
 "檢核點:回應要求同等嚴重(info 不算)": ("pipeline.py",
     '                                      [{**h, "severity": "info"} for h in hits])',
     '                                      [{**h, "severity": "minor"} for h in hits])'),
 "檢核點:拿自己補的來比對(吞掉同檔其他檢核點)": ("pipeline.py",
     '            missing, exact = _coverage(before, entry["path"], _HINT_KEYWORDS.get(code, [code]),',
     '            missing, exact = _coverage(report.get("findings", []), entry["path"], _HINT_KEYWORDS.get(code, [code]),'),
 "檢核點:同檔同代碼只看第一處(其餘不補)": ("pipeline.py",
     '                                      [{**h, "severity": "info"} for h in hits])',
     '                                      [{**hits[0], "severity": "info"}])'),
 "檢核點:多處命中時說明不寫數量": ("pipeline.py",
     '                    "detail": h.get("message", "") + _count_note(code, len(hits), len(missing), n),',
     '                    "detail": h.get("message", ""),'),
 "檢核點:排回程式補報之後(R004 被當成 H004 的回應)": ("pipeline.py",
     "        report = enforce_hints(report, pre)\n"
     "        report = enforce_rules(report, pre)    # rule-base 命中不因模型省略而消失\n"
     "        report = enforce_parse(report, pre)    # 預掃解析失敗 = 確定性規則沒跑,必須看得見\n",
     "        report = enforce_rules(report, pre)    # rule-base 命中不因模型省略而消失\n"
     "        report = enforce_parse(report, pre)    # 預掃解析失敗 = 確定性規則沒跑,必須看得見\n"
     "        report = enforce_hints(report, pre)\n"),
})


# ---- 依專案的目錄設定判斷 dbt 檔、snapshot 反查、逐句比較與時間上限、展開後的注入掃描
MUTANTS.update({
 'dbt範圍:目錄設定不生效(專案目錄裡的檔案都算)': ('dbt_project.py',
     '        return rel in LAYOUT_ROOT_FILES or _under(rel, self.dirs)',
     '        return True'),
 'dbt範圍:selectors.yml 不算 dbt 的檔案': ('dbt_project.py',
     'LAYOUT_ROOT_FILES = ROOT_FILES + ("selectors.yml",)',
     'LAYOUT_ROOT_FILES = ROOT_FILES'),
 'dbt範圍:analyses / tests 不取回': ('dbt_project.py',
     'for p in analysis_paths + test_paths +\n',
     'for p in\n'),
 'dbt範圍:不讀 test-paths': ('dbt_project.py',
     '            _path_list(raw, "test-paths", DEFAULT_TEST_PATHS, allow_empty=True),',
     '            DEFAULT_TEST_PATHS,'),
 'dbt範圍:context 不依目錄設定判斷': ('dbt_context.py',
     '        if rel is None or (self.layout is not None and not self.layout.contains(rel)):',
     '        if rel is None:'),
 'dbt範圍:不先讀設定檔(只改 dbt 以外的檔也下載整個專案)': ('dbt_context.py',
     '    if not _needs_project(files, prefix, layout):\n        return None',
     '    if not _needs_project(files, prefix):\n        return None'),
 'dbt範圍:讀不到設定檔就當成沒有 dbt 專案': ('dbt_context.py',
     '    except Exception:                       # noqa: BLE001 — 讀不到就退回原本的判斷\n        return None, False',
     '    except Exception:                       # noqa: BLE001 — 讀不到就退回原本的判斷\n        return None, True'),
 'dbt範圍:沒有 dbt_project.yml 靜靜變成不適用': ('dbt_context.py',
     '        return _failed(prefix, "head commit 讀不到 dbt_project.yml(專案目錄設定錯誤,或唯讀 token 看不到這個專案)")',
     '        return None'),
 'dbt範圍:不合法的路徑當成不在專案裡': ('dbt_context.py',
     '                found.append(_INVALID_PATH)\n                continue',
     '                continue'),
 'dbt範圍:context 的專案資訊不帶目錄設定': ('dbt_context.py',
     '    ctx = DbtContext(prefix=prefix, project=project, info=info, macro_files=macro_files,\n                     layout=project.layout)',
     '    ctx = DbtContext(prefix=prefix, project=project, info=info, macro_files=macro_files)'),
 'dbt範圍:狀態不完整不補失敗原因': ('dbt_context.py',
     '            object.__setattr__(self, "error", "dbt 專案的狀態不完整(沒有內容,也沒有失敗原因)")',
     '            pass'),
 'dbt範圍:反查不過濾 dbt 以外的檔案': ('dbt_mr_impact.py',
     '    changed = {p for p in changed if head.layout.contains(p) or base.layout.contains(p)}',
     '    changed = set(changed)'),
 '下載:讀單一檔案 404 也丟例外': ('../toolbox/gitlab.py',
     '            if r.status_code == 404:\n                return None',
     '            if False:\n                return None'),
 '下載:讀單一檔案不檢查路徑': ('../toolbox/gitlab.py',
     '    if not isinstance(path, str) or not _ARCHIVE_PATH.fullmatch(path) or any(',
     '    if not isinstance(path, str) or any('),
 '下載:讀單一檔案不檢查 commit 編號': ('../toolbox/gitlab.py',
     '    if not (isinstance(sha, str) and _SHA.fullmatch(sha)):',
     '    if not isinstance(sha, str):'),
 '下載:讀單一檔案不檢查唯讀 token': ('../toolbox/gitlab.py',
     '    _check_read_access("讀取檔案")',
     '    pass'),
 '範例專案:符號連結靜默跳過': ('dbt_context.py',
     '    members = sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink())',
     '    members = sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())'),
 '範例專案:多層目錄的頂層名稱含斜線': ('dbt_context.py',
     '(f"-{path.replace(\'/\', \'-\')}" if path else "")',
     '(f"-{path}" if path else "")'),
 '專案接線:注入掃描遇到非物件的項目就中斷': ('pipeline.py',
     'if isinstance(f, dict) and (text := dbt_ctx.head_text(f.get("path"))) is not None]',
     'if (text := dbt_ctx.head_text(f.get("path"))) is not None]'),
 '專案接線:注入掃描不掃展開後的 SQL': ('pipeline.py',
     'injection_hits = scan_mr(_mr_with_head_text(mr, dbt_ctx, rendered))',
     'injection_hits = scan_mr(_mr_with_head_text(mr, dbt_ctx))'),
 '專案接線:預掃不收集展開後的 SQL': ('pipeline.py',
     '            elif rendered is not None:\n                rendered.append(',
     '            elif False:\n                rendered.append('),
 '受影響:推論出來的 blocker 照樣擋下': ('dbt_affected.py',
     '            raise_to("major" if m.no_baseline or worst == "blocker" else worst)',
     '            raise_to("major" if m.no_baseline else worst)'),
 '受影響:語句數量不同仍逐句比': ('dbt_affected.py',
     '    elif head.statements is not None and head.statements == base.statements:',
     '    elif head.statements is not None:'),
 '受影響:展開不傳剩下的時間': ('dbt_affected.py',
     '                      timeout_s=min(DEFAULT_ISOLATED_TIMEOUT_S, seconds))',
     '                      timeout_s=DEFAULT_ISOLATED_TIMEOUT_S)'),
 '受影響:時間用完仍開始展開': ('dbt_affected.py',
     '    if seconds <= 0:\n        raise _OutOfTime',
     '    if False:\n        raise _OutOfTime'),
 '受影響:展開被時間截斷當成展開失敗': ('dbt_affected.py',
     '        if clock() >= deadline:\n            raise _OutOfTime',
     '        if False:\n            raise _OutOfTime'),
 '受影響:沒查規格的不列出、不升級': ('dbt_affected.py',
     '    if not_looked:\n        raise_to("major")',
     '    if False:\n        raise_to("major")'),
 '受影響:沒查規格只列出、不升級': ('dbt_affected.py',
     '    if not_looked:\n        raise_to("major")',
     '    if not_looked:\n        pass'),
 '受影響:展開失敗的清單又截斷': ('dbt_affected.py',
     '"**變更後展開失敗或無法解析**:" + "、".join(broken)',
     '"**變更後展開失敗或無法解析**:" + _listed(broken)'),
 '受影響:多出的命中清單又截斷': ('dbt_affected.py',
     '"**變更後多出的規則命中**:" + "、".join(new)',
     '"**變更後多出的規則命中**:" + _listed(new)'),
 '受影響:命中不寫第幾句': ('dbt_affected.py',
     '        if known:\n            text +=',
     '        if False:\n            text +='),
 '受影響:查規格用預設的執行緒池(審查結束時被卡住的那條拖住)': ('dbt_affected.py',
     '        code, text = await _wait_in_thread(lambda: find_spec(hub, target, by_path=True), seconds)',
     '        code, text = await asyncio.wait_for(\n            asyncio.to_thread(asyncio.run, find_spec(hub, target, by_path=True)), seconds)'),
 'snapshot:呼叫被改的 macro 不標不確定': ('dbt_impact.py',
     '        for path, chain in snapshot_hits:\n            self.uncertain.append(',
     '        for path, chain in []:\n            self.uncertain.append('),
 'snapshot:看不懂的 snapshot 不標不確定': ('dbt_impact.py',
     '        for path in self.unparsable_snapshots:\n            self.uncertain.append(',
     '        for path in []:\n            self.uncertain.append('),
 'snapshot:不追 snapshot 的呼叫': ('dbt_impact.py',
     '                callers_of.setdefault(target, set()).add(("snapshot", path))',
     '                pass'),
 'snapshot:轉接層不給 snapshot 的內容': ('dbt_mr_impact.py',
     '    dirs = project.model_paths + project.macro_paths + project.snapshot_paths',
     '    dirs = project.model_paths + project.macro_paths'),
 'snapshot:自訂目錄的 snapshot 當成目錄外的 .sql': ('dbt_impact.py',
     '        if path.endswith(".sql") and _in_dirs(path, self.snapshot_dirs):\n            return "snapshot"',
     '        if False:\n            return "snapshot"'),
 '受影響:相同的兩句命中都對到第一句': ('dbt_affected.py',
     '                          if keys[i] == statement and (hit["rule"], i) not in taken), None)',
     '                          if keys[i] == statement), None)'),
 '專案:analysis / test 目錄不允許空清單': ('dbt_project.py',
     '            _path_list(raw, "test-paths", DEFAULT_TEST_PATHS, allow_empty=True),',
     '            _path_list(raw, "test-paths", DEFAULT_TEST_PATHS),'),
 'snapshot:snapshot 的屬性檔不追': ('dbt_impact.py',
     '            if _in_dirs(path, self.snapshot_dirs):\n                kind = "snapshot"',
     '            if False:\n                kind = "snapshot"'),
 'dbt範圍:資源目錄仍看預設名稱': ('dbt_impact.py',
     '        in_resource = _in_dirs(path, resource_dirs)',
     '        in_resource = path.split("/", 1)[0] in _RESOURCE_DIRS'),
 'dbt範圍:path_kind 不用資源目錄設定': ('dbt_impact.py',
     '    return _kind(path, model_dirs, macro_dirs, resource_dirs)',
     '    return _kind(path, model_dirs, macro_dirs)'),
 'dbt範圍:反查分類不用資源目錄設定': ('dbt_impact.py',
     '        return _kind(path, self.model_dirs, self.macro_dirs, self.resource_dirs)',
     '        return _kind(path, self.model_dirs, self.macro_dirs)'),
 'dbt範圍:資源目錄設定不檢查': ('dbt_impact.py',
     '            if None in resource_dirs or "" in resource_dirs:',
     '            if False:'),
 'dbt範圍:反查不給資源目錄設定': ('dbt_mr_impact.py',
     '                     snapshot_dirs=head.snapshot_paths, resource_dirs=head.resource_paths)',
     '                     snapshot_dirs=head.snapshot_paths)'),
 'dbt範圍:資源目錄漏了 test': ('dbt_project.py',
     '        return self.seed_paths + self.snapshot_paths + self.analysis_paths + self.test_paths',
     '        return self.seed_paths + self.snapshot_paths + self.analysis_paths'),
 'dbt範圍:判斷 macro 不用資源目錄設定': ('dbt_context.py',
     '                and path_kind(rel, self.project.model_paths, self.project.macro_paths,\n                              self.project.resource_paths) == "macro")',
     '                and path_kind(rel, self.project.model_paths, self.project.macro_paths) == "macro")'),
 'dbt範圍:只改 model 的判斷不用資源目錄設定': ('dbt_context.py',
     '    if all(path_kind(rel, project.model_paths, project.macro_paths, project.resource_paths) == "model"',
     '    if all(path_kind(rel, project.model_paths, project.macro_paths) == "model"'),
})


# ---- 程式補的 finding 寫行號(#28 review 後續)
MUTANTS.update({
 "行號:引用時語句前面的註解也要抄到才算": ("pipeline.py",
     '    statements = [_flat(_COMMENT.sub(" ", str(h.get("statement") or ""))) for h in hits]',
     '    statements = [_flat(str(h.get("statement") or "")) for h in hits]'),
 "行號:補的 finding 一律寫 0": ("pipeline.py",
     '    return line if exact and isinstance(line, int) and not isinstance(line, bool) else 0',
     '    return 0'),
 "行號:不確定是哪一處也寫行號": ("pipeline.py",
     '    return line if exact and isinstance(line, int) and not isinstance(line, bool) else 0',
     '    return line if isinstance(line, int) and not isinstance(line, bool) else 0'),
 "行號:比數量對應時也當成確定": ("pipeline.py",
     '                guessed = guessed or n >= len(quoted)',
     '                guessed = False'),
 "行號:不加行號": ("pipeline.py",
     '        _attach_statement_lines(pre, mr["files"])   # 給程式補的 finding 用;prompt 已組好,不受影響\n',
     ''),
 "行號:組 prompt 之前就加(prompt 會變)": ("pipeline.py",
     '        user = USER_TEMPLATE.format(\n            title=mr["title"]',
     '        _attach_statement_lines(pre, mr["files"])\n        user = USER_TEMPLATE.format(\n            title=mr["title"]'),
 "行號:前 200 字相同的不同語句也寫": ("pipeline.py",
     '            if not found or len({full for _, full in found}) > 1:',
     '            if not found:'),
 "行號:同一句出現多次都寫第一句": ("pipeline.py",
     '                h["line"] = found[used[key]][0]',
     '                h["line"] = found[0][0]'),
 "行號:dbt 樣板也算": ("pipeline.py",
     '                or is_dbt_template(text)):',
     '                ):'),
 "行號:檔案太大也算": ("pipeline.py",
     '        if (not isinstance(text, str) or not text or len(text) > MAX_LINE_SOURCE_CHARS\n',
     '        if (not isinstance(text, str) or not text\n'),
 "行號:切出來的數量對不上也寫": ("pipeline.py",
     '    if len(starts) != len(statements):\n        return None',
     '    if False:\n        return None'),
 "行號:只有註解的語句也算": ("pipeline.py",
     '                      if st is not None and not isinstance(st, exp.Semicolon)]',
     '                      if st is not None]'),
 "行號:檔案與預掃結果對不上也寫": ("pipeline.py",
     '        if (not isinstance(f, dict) or entry.get("path") != f.get("path")',
     '        if (not isinstance(f, dict)'),
})


# ---- 非 SQL 檔只留嚴重命中;只改 dbt 設定的變更
MUTANTS.update({
 '非SQL:照舊送語法樹規則與 lint': ('pipeline.py',
     '        if kind is not None:\n            entry.update(',
     '        if False:\n            entry.update('),
 '非SQL:不跑規則(憑證也不抓)': ('pipeline.py',
     '            entry.update(await _non_sql_entry(hub, sql, kind))',
     '            entry.update(non_sql=kind, rules=[], lint=[])'),
 '非SQL:副檔名分大小寫': ('pipeline.py',
     '    name = str(path or "").lower()\n    if name.endswith(".sql"):',
     '    name = str(path or "")\n    if name.endswith(".sql"):'),
 '非SQL:說明文件也當成其他檔案': ('pipeline.py',
     '    return "doc" if name.endswith(_DOC_SUFFIXES) else "other"',
     '    return "other"'),
 '非SQL:其他檔案也當成說明文件': ('pipeline.py',
     '    return "doc" if name.endswith(_DOC_SUFFIXES) else "other"',
     '    return "doc"'),
 '非SQL:留下 minor 與檢核點': ('pipeline.py',
     '            and _SEVERITY_RANK.get(h.get("severity"), -1) >= _SEVERITY_RANK["major"]',
     '            and _SEVERITY_RANK.get(h.get("severity"), -1) >= -1'),
 '非SQL:說明文件也留語法樹規則的命中': ('pipeline.py',
     '            and (kind != "doc" or not h.get("statement"))]',
     ']'),
 '非SQL:其他檔案只給 info': ('pipeline.py',
     '                severity="major", title=_NON_SQL_OTHER_TITLE,',
     '                severity="info", title=_NON_SQL_OTHER_TITLE,'),
 '非SQL:不補 finding': ('pipeline.py',
     '        report = enforce_non_sql(report, pre)  # 非 SQL 檔沒跑 SQL 規則:說明文件 info,其他 major\n',
     ''),
 '非SQL:dry-run 不補': ('pipeline.py',
     '    report = enforce_non_sql(report, pre)      # 與正式路徑一致\n',
     ''),
 '非SQL:直接附加(模型同標題的 info 可取代)': ('pipeline.py',
     '請改用 .sql 副檔名。")\n        _put_program_finding(report, item)',
     '請改用 .sql 副檔名。")\n        report["findings"].append(item)'),
 '設定變更:不補 finding': ('pipeline.py',
     '        report = enforce_dbt_config_change(report, mr, dbt_ctx)  # 只改設定時展開結果不變\n',
     ''),
 '設定變更:dry-run 不補': ('pipeline.py',
     '    report = enforce_dbt_config_change(report, mr, dbt_ctx)\n    report = enforce_dbt_notice(',
     '    report = enforce_dbt_notice('),
 '設定變更:看不出改了什麼只給 info': ('pipeline.py',
     '        major = bool(change.keys or change.unknown)',
     '        major = bool(change.keys)'),
 '設定變更:直接附加(模型同標題的 info 可取代)': ('pipeline.py',
     '比對變更前後。" if major else "")}\n        _put_program_finding(report, item)',
     '比對變更前後。" if major else "")}\n        report["findings"].append(item)'),
 '設定變更:不用取回的完整內容': ('pipeline.py',
     '    head_text = dbt_ctx.head_text if dbt_ctx is not None else None',
     '    head_text = None'),
 '設定變更:處理中出錯時當成沒改': ('dbt_config_change.py',
     '            change = ConfigChange(f["path"], unknown=True)',
     '            change = None'),
 '設定變更:新增整個檔案也算': ('dbt_config_change.py',
     '    if not hunks or _whole_file(hunks):',
     '    if not hunks:'),
 '設定變更:不反推(一律只看 diff)': ('dbt_config_change.py',
     '        sides = _reverse(new_text, hunks)',
     '        sides = None'),
 '設定變更:diff 與完整內容不比對': ('dbt_config_change.py',
     '            if i >= len(new_lines) or new_lines[i] != text:',
     '            if i >= len(new_lines):'),
 '設定變更:只看 diff 時不認 }}': ('dbt_config_change.py',
     '        marked = marked or any(t in old_text + new_text for t in ("}}", "%}"))',
     '        marked = marked'),
 '設定變更:沒有樣板標記也看': ('dbt_config_change.py',
     '    if not marked:\n        return None',
     '    if False:\n        return None'),
 '設定變更:config.set 不看': ('dbt_config_change.py',
     '    if _config_sets(old_text) != _config_sets(new_text):',
     '    if False:'),
 '設定變更:只看 diff 時不看設定名稱': ('dbt_config_change.py',
     '        unknown = any(map(_SNIPPET_KEY.search, changed))',
     '        unknown = False'),
 '設定變更:T-SQL 變數也算設定名稱': ('dbt_config_change.py',
     'r"(?<![@\\w.])(materialized',
     'r"(materialized'),
 '設定變更:看不懂的寫法沒變也算': ('dbt_config_change.py',
     '                [_squash(b) for b in old_bodies] == [_squash(b) for b in new_bodies]:',
     '                False:'),
 '設定變更:受關注的設定也只標其他': ('dbt_config_change.py',
     '    return tuple(k for k in WATCHED if k in changed), bool(changed - set(WATCHED)), False',
     '    return (), bool(changed), False'),
 '設定變更:不寫行號': ('dbt_config_change.py',
     'line=_first_config_line(new_lines) if full else 0)',
     'line=0)'),
 '設定變更:不是 dbt 的 yml 也看': ('dbt_config_change.py',
     '    if not known_dbt and not (',
     '    if False and not ('),
 '設定變更:yml 不限 models 底下': ('dbt_config_change.py',
     '            if full and (not chain or chain[0] not in _CONFIG_ROOTS):',
     '            if False:'),
 '設定變更:欄位與測試底下也算': ('dbt_config_change.py',
     '            if any(k in _NOT_CONFIG for k in chain):',
     '            if False:'),
 '設定變更:往上找到的資料夾名稱也算設定': ('dbt_config_change.py',
     '            hit = {chain[-1]} & set(WATCHED) if chain else set()',
     '            hit = set(chain) & set(WATCHED)'),
 '設定變更:一行 { } 的寫法整行都算': ('dbt_config_change.py',
     '            if "{" in lines[idx]:\n                flow[side]',
     '            if False:\n                flow[side]'),
 '設定變更:多行字串裡的字也算': ('dbt_config_change.py',
     '        if stack and not stack[-1][2]:',
     '        if False:'),
 '設定變更:跨行的 [ ] 不算上一層': ('dbt_config_change.py',
     '    return value == "" or value[:1] in "[{"',
     '    return value == ""'),
 '設定變更:清單項目與上一層同縮排不算': ('dbt_config_change.py',
     '            limit, same_ok = (len(d["ind"]), True) if d else',
     '            limit, same_ok = (len(d["ind"]), False) if d else'),
 '設定變更:參數用的變數在 set 那一行被改到也不算': ('dbt_config_change.py',
     '        unknown = any(set_line.search(line) for line in changed)',
     '        unknown = False'),
 '設定變更:參數名稱也當成變數': ('dbt_config_change.py',
     '_NAME = re.compile(r"(?<![\\w.])([A-Za-z_]\\w*)\\b(?!\\s*=(?!=))(?!\\s*\\()")',
     '_NAME = re.compile(r"(?<![\\w.])([A-Za-z_]\\w*)\\b(?!\\s*\\()")'),
 '設定變更:函式名稱也當成變數': ('dbt_config_change.py',
     '_NAME = re.compile(r"(?<![\\w.])([A-Za-z_]\\w*)\\b(?!\\s*=(?!=))(?!\\s*\\()")',
     '_NAME = re.compile(r"(?<![\\w.])([A-Za-z_]\\w*)\\b(?!\\s*=(?!=))")'),
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

    判定:"config"(突變點不是剛好出現一次)、"survived"(套件全過)、"caught"(有測試失敗)、
    "timeout"(套件卡住逾時)、"error"(匯入或收集錯誤)。後兩種**不算抓到**:逾時可能只是
    機器慢,收集錯誤代表測試根本沒跑到,都不能證明測試守著這道防線,要另外列出來看。
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
            return "timeout", f"測試套件卡住超過 {timeout} 秒"
        return _verdict(proc.returncode, proc.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _verdict(returncode: int, stdout: str) -> tuple[str, str]:
    """pytest 的結果 → (判定, 說明)。回傳碼 1 才是「有測試失敗」;收集錯誤(ERROR collecting)、
    中斷或用法錯誤(2–5)都不算抓到。"""
    lines = stdout.splitlines()
    if returncode == 0:
        summary = next((l for l in reversed(lines)
                        if "passed" in l or "failed" in l or "error" in l), stdout[-160:])
        return "survived", summary
    if returncode != 1 or any(l.startswith("ERROR collecting") for l in lines):
        first = next((l for l in lines if l.startswith("ERROR")), lines[-1] if lines else "")
        return "error", f"回傳碼 {returncode}:{first[:110]}"
    first = next((l for l in lines if l.startswith(("FAILED", "ERROR"))), "")
    return "caught", first.split(" - ")[0][:110]



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

    survived, config_errors, timeouts, errors = [], [], [], []
    for label, entry in selected.items():
        mutant = entry if len(entry) == 3 else ("dbt_render.py", *entry)
        verdict, detail = _run_mutant(src, mutant, args.python, timeout, env)
        if verdict == "config":
            print(f"[設定錯誤] {label}:{detail}", flush=True)
            config_errors.append(label)
        elif verdict == "survived":
            print(f"[存活 ✗] {label}:{detail}", flush=True)
            survived.append(label)
        elif verdict == "timeout":
            print(f"[逾時 ?] {label}:{detail}", flush=True)
            timeouts.append(label)
        elif verdict == "error":
            print(f"[收集錯誤 ?] {label}:{detail}", flush=True)
            errors.append(label)
        else:
            print(f"[被抓 ✓] {label}:{detail}", flush=True)
    print()
    caught = len(selected) - len(survived) - len(config_errors) - len(timeouts) - len(errors)
    print(f"共 {len(selected)} 個突變;抓到 {caught};存活 {len(survived)};逾時 {len(timeouts)};"
          f"收集錯誤 {len(errors)};設定錯誤 {len(config_errors)}")
    for name, items in (("存活", survived), ("逾時(不算抓到)", timeouts),
                        ("收集錯誤(不算抓到)", errors), ("設定錯誤", config_errors)):
        if items:
            print(f"{name}:", items)
    return 1 if survived or config_errors or timeouts or errors else 0


# ---- 測資生成的覆蓋度重試(spec_exec)
# 這道防線的失效是**靜默**的:覆蓋不完整的測資照樣跑得完、報告看起來正常,
# 只是執行驗證少驗了幾個條件。沒有突變點的話,測試是不是真的守著它無從得知。
S = "spec_exec.py"
MUTANTS.update({
    "測資:有缺口也直接收下(回到舊行為)": (S,
        "                if not gaps and n_conds >= max_conds:",
        "                if True:"),
    "測資:不留覆蓋最多的一版(只認最後一次)": (S,
        "                if best is None or _covered(cleaned) > _covered(best[0]):\n"
        "                    best = (cleaned, gaps, dropped)",
        "                best = (cleaned, gaps, dropped)"),
    # review 第 4 點:缺口是對照模型自己拆的條件算的,拆得少的 0 缺口版本不能
    # 勝過拆得齊、只缺一點的版本;重試 prompt 不帶缺口,固定 seed 下等於白跑。
    "測資:0 缺口就提前結束,不看條件數有沒有變少": (S,
        "                if not gaps and n_conds >= max_conds:",
        "                if not gaps:"),
    "測資:最佳版本改回比缺口數": (S,
        "                if best is None or _covered(cleaned) > _covered(best[0]):",
        "                if best is None or len(gaps) < len(best[1]):"),
    "測資:重試 prompt 不帶上一次的缺口": (S,
        "        issues.append(\"上一次的測資計畫覆蓋不完整,缺:\\n\" + \"\\n\".join(f\"- {g}\" for g in gaps))",
        "        issues.append(\"上一次的測資計畫覆蓋不完整。\")"),
    "測資:重試用完就判失敗(丟掉已抽到的最佳版)": (S,
        "    if best is not None:\n        return best",
        "    if False:\n        return best"),
    # 執行中止後,沒跑到的案例必須列入覆蓋缺口。少了它,報告會顯示「跑了 N 個
    # 案例、零缺口」,讀起來像全覆蓋,實際上多數案例根本沒執行——而 coverage_gaps
    # 正是人用來判斷「這次到底驗了多少」的欄位。兩個早退點各一個突變點。
    "執行中止:SQL 錯時沒跑到的案例不算缺口": (S,
        '"執行錯誤(語法/欄位):{ex[\'sql_error\']}",' + NL
        + '                                 "修正 SQL 使其可依規格的資料表定義在 MS SQL 上執行。"))' + NL
        + '        return {"passed": False, "spec_code": spec_code, "engine": ex["engine"],' + NL
        + '                "conditions": plan["conditions"],' + NL
        + '                "coverage_gaps": coverage_gaps + _unrun_gaps(plan, ex["case_results"]),',
        '"執行錯誤(語法/欄位):{ex[\'sql_error\']}",' + NL
        + '                                 "修正 SQL 使其可依規格的資料表定義在 MS SQL 上執行。"))' + NL
        + '        return {"passed": False, "spec_code": spec_code, "engine": ex["engine"],' + NL
        + '                "conditions": plan["conditions"],' + NL
        + '                "coverage_gaps": coverage_gaps,'),
    # 截斷偵測:被 max_tokens 硬切的 JSON 會被 json-repair 修補成合法物件,
    # 形狀檢查只看得到「案例比較少」。拿掉這一關,殘骸會被當成正常結果收下。
    "測資:不理會輸出被截斷(殘骸當結果收下)": (S,
        '        if meta.get("finish_reason") == "length":',
        "        if False:"),
    "執行中止:測資建不起來時沒跑到的案例不算缺口": (S,
        'f"{ex[\'testdata_error\']}。需人工執行驗證。"))' + NL
        + '        return {"passed": False, "spec_code": spec_code, "engine": ex["engine"],' + NL
        + '                "conditions": plan["conditions"],' + NL
        + '                "coverage_gaps": coverage_gaps + _unrun_gaps(plan, ex["case_results"]),',
        'f"{ex[\'testdata_error\']}。需人工執行驗證。"))' + NL
        + '        return {"passed": False, "spec_code": spec_code, "engine": ex["engine"],' + NL
        + '                "conditions": plan["conditions"],' + NL
        + '                "coverage_gaps": coverage_gaps,'),
})


if __name__ == "__main__":
    sys.exit(main())
