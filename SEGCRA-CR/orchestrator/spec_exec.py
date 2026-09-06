"""spec_exec — 執行驗證(必跑的確定性閘門,三角色)。

MR 的 SQL 不只要「看起來對」,還要「跑起來對」。流程:

  0. 找 spec(確定性):從 MR 抓規則碼 R-xxx → 對應 specs/<code>.md。
     **找不到 spec = major finding「無規格可驗」→ 一律 needs_human**
     (「必跑」的意思:不是沒 spec 就跳過,而是沒 spec 就不能自動放行)。
  1. 角色一 測資生成 agent(LLM):讀 spec → 產出 schema DDL + 原子條件清單 +
     逐條件 true/false 兩向 + 邊界值的測資案例(嚴格 JSON)。
     生成後做**確定性形狀/覆蓋檢查**(缺的列入 coverage_gaps)。
  2. 角色二 執行(確定性程式,非 LLM——執行本身不可有幻覺):每個案例在
     in-memory DuckDB 建表灌資料,對 MR 的 SQL 做參數代入 + postgres→duckdb
     transpile 後執行,比對「有無命中」與 expect_flagged。
  3. 角色三 仲裁 agent(LLM):只對 mismatch 逐案出動,判定是**測資錯**
     (測資生成也可能幻覺 → 剔除該案並記錄)還是 **SQL 錯**(→ major finding)。

產出 {passed, conditions, coverage_gaps, case_results, dropped_cases, findings},
全程記錄進 report["_spec_exec"] 供稽核。
"""
import json
import re

from .agent import extract_json, run_agent
from .config import Config, PKG_ROOT

SPECS_DIR = PKG_ROOT / "specs"

# 執行窗固定(可重現)::start_date / :end_date 代入這兩天(半開區間)
WIN_START, WIN_END = "2026-06-01", "2026-06-02"

_RULE_CODE = re.compile(r"\bR-\d{2,4}\b")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")   # 資料表名白名單(擋注入)
_DDL_OK = re.compile(r"^\s*CREATE\s+TABLE\s", re.I)


# ------------------------------------------------------------------ 0. 找 spec
def extract_rule_codes(mr: dict) -> list[str]:
    """確定性抓規則碼:SQL 內容 + 標題 + 描述 + 檔名。"""
    text = " ".join([mr.get("title", ""), mr.get("description", "")] +
                    [f.get("path", "") + " " + (f.get("full_content") or f.get("diff", ""))
                     for f in mr.get("files", [])])
    return sorted(set(_RULE_CODE.findall(text)))


async def find_spec(hub, mr: dict) -> tuple[str | None, str | None]:
    """回傳 (rule_code, spec_text)。mock 模式讀本地 specs/;real 模式用
    gitlab get_file 抓 repo 內 specs/<code>.md。都找不到 → (code|None, None)。"""
    codes = extract_rule_codes(mr)
    for code in codes:
        local = SPECS_DIR / f"{code}.md"
        if local.exists():
            return code, local.read_text(encoding="utf-8")
    if hub is not None:
        for code in codes:
            try:
                txt = await hub.call("gitlab__get_file",
                                     {"path": f"specs/{code}.md"}, truncate=False)
            except Exception:
                continue
            if txt and txt.lstrip().startswith("#"):
                return code, txt
    return (codes[0] if codes else None), None


# ------------------------------------------------------------------ 1. 測資生成
TESTGEN_SYSTEM = """你是測試資料工程師。給你一份異常交易規則的核定規格(spec),
你要產出可機器執行的測資計畫。**spec 是唯一真值**,不得自行增減條件。

# 產出(嚴格 JSON,不要 markdown 圍欄、不要多餘文字)
{"schema_ddl": ["CREATE TABLE ...(照 spec 的資料表定義,一字不差的欄位名/型別)"],
 "conditions": [{"id": "C1", "desc": "<spec 的一個原子條件,如:tx_type='WITHDRAW'>"}],
 "cases": [{"case_id": "C1-T", "condition_id": "C1", "direction": "true",
            "rows": [["<表名>", <值1>, <值2>, ...], ...],
            "expect_flagged": true, "note": "<這個案例在測什麼>"}]}

# 硬性要求
1. conditions:把 spec 的需求項目拆成**原子條件**(每個可獨立為真/假的判斷:
   交易範圍過濾、閾值比較、時段、幣別、豁免、時間窗 各自成條)。
2. 每個條件都要有 direction="true"(條件成立 → 應命中)與 direction="false"
   (只有此條件不成立 → 不應命中)**兩向案例**;涉及數值門檻的條件另加**邊界案例**
   (剛好等於門檻、差最小單位),依 spec 的含/不含決定 expect_flagged。
3. rows:每列 = [資料表名, 各欄位值...],值的順序與 schema_ddl 欄位順序一致;
   時間值用 "YYYY-MM-DD HH:MM:SS" 字串、日期用 "YYYY-MM-DD"、布林用 true/false。
4. 時間窗:SQL 的 :start_date 會代入 DATE '{win_start}'、:end_date 代入
   DATE '{win_end}'(半開區間)。「窗內」交易的時間一律落在 {win_start} 當天;
   測「窗外」時才用其他日期。
5. 每個案例只放**該案例需要的資料列**(案例之間互相獨立,各自在乾淨資料庫執行;
   expect_flagged=true 表示該案例的資料應使規則輸出至少一列)。
6. 只用 spec 資料表定義裡存在的表與欄位,不得發明欄位。"""


def _shape_check(plan: dict) -> tuple[dict, list[str], list[dict]]:
    """確定性檢查測資計畫:JSON 形狀 + 逐條件 true/false 覆蓋。
    回傳 (清洗後 plan, coverage_gaps, dropped_malformed)。"""
    gaps, dropped = [], []
    if not isinstance(plan, dict):
        return {"schema_ddl": [], "conditions": [], "cases": []}, ["整份計畫非 JSON 物件"], []
    ddl = [d for d in plan.get("schema_ddl", []) if isinstance(d, str) and _DDL_OK.match(d)]
    conds = [c for c in plan.get("conditions", [])
             if isinstance(c, dict) and c.get("id") and c.get("desc")]
    cond_ids = {c["id"] for c in conds}
    cases = []
    for c in plan.get("cases", []):
        ok = (isinstance(c, dict) and c.get("case_id")
              and c.get("condition_id") in cond_ids
              and c.get("direction") in ("true", "false")
              and isinstance(c.get("expect_flagged"), bool)
              and isinstance(c.get("rows"), list) and c["rows"]
              and all(isinstance(r, list) and len(r) >= 2 and isinstance(r[0], str)
                      and _IDENT.match(r[0]) for r in c["rows"]))
        (cases if ok else dropped).append(
            c if ok else {"case": c, "reason": "形狀不合法(缺欄位/表名非法/方向錯)"})
    if not ddl:
        gaps.append("無合法 schema_ddl")
    for cid in sorted(cond_ids):
        dirs = {c["direction"] for c in cases if c["condition_id"] == cid}
        for d in ("true", "false"):
            if d not in dirs:
                gaps.append(f"條件 {cid} 缺 {d} 向案例")
    if not conds:
        gaps.append("未拆出任何原子條件")
    return {"schema_ddl": ddl, "conditions": conds, "cases": cases}, gaps, dropped


async def generate_cases(cfg: Config, spec_code: str, spec_text: str,
                         profile_name: str | None = None) -> tuple[dict, list[str], list[dict]]:
    """角色一:LLM 依 spec 生成測資計畫,含一次重試;回傳 (plan, gaps, dropped)。"""
    system = TESTGEN_SYSTEM.replace("{win_start}", WIN_START).replace("{win_end}", WIN_END)
    user = f"規則 {spec_code} 的核定規格如下,請產出測資計畫 JSON:\n\n{spec_text}"
    profile = cfg.role_profile("testgen") if profile_name is None else cfg.profile(profile_name)
    llm_error = None
    for attempt in range(2):
        try:
            raw = await run_agent(cfg, profile, system, user, hub=None,
                                  use_tools=False, verbose=False)
        except Exception as e:   # LLM 傳輸層失敗(連線/逾時)→ 降級為「測資生成失敗」,不炸管線
            llm_error = f"{type(e).__name__}: {e}"
            continue
        plan = extract_json(raw)
        if plan:
            cleaned, gaps, dropped = _shape_check(plan)
            if cleaned["cases"]:
                return cleaned, gaps, dropped
        user = (f"上一次輸出無法解析或沒有任何合法案例,請重新只輸出符合格式的 JSON。\n\n"
                f"規則 {spec_code} 規格:\n{spec_text}")
    reason = (f"測資生成失敗(LLM 呼叫失敗:{llm_error})" if llm_error
              else "測資生成失敗(兩次皆無合法輸出)")
    return {"schema_ddl": [], "conditions": [], "cases": []}, [reason], []


# ------------------------------------------------------------------ 2. 確定性執行
def prepare_sql(sql: str) -> str:
    """參數代入 + INSERT→取其 SELECT + postgres→duckdb transpile。
    (參數代入需在 sqlglot 之前,否則 :name 會被改寫。)"""
    import sqlglot
    sql = sql.replace(":start_date", f"DATE '{WIN_START}'")
    sql = sql.replace(":end_date", f"DATE '{WIN_END}'")
    sql = re.sub(r"\bCURRENT_DATE\b", f"DATE '{WIN_END}'", sql, flags=re.I)
    try:
        st = sqlglot.parse_one(sql, dialect="postgres")
        if st.key == "insert" and st.expression is not None:
            st = st.expression
        sql = st.sql(dialect="postgres")
    except Exception:
        pass
    try:
        return sqlglot.transpile(sql, read="postgres", write="duckdb")[0]
    except Exception:
        return sql


def execute_cases(sql: str, plan: dict) -> dict:
    """角色二:確定性執行(非 LLM)。每案例在乾淨的 in-memory DuckDB 建 schema、
    灌該案例的 rows、跑 MR 的 SQL,比對「是否有輸出列」與 expect_flagged。
    回傳 {case_results, mismatches, sql_error, testdata_error}。"""
    import duckdb
    prepared = prepare_sql(sql)
    results, mismatches = [], []
    for case in plan["cases"]:
        con = duckdb.connect(":memory:")
        try:
            try:
                for ddl in plan["schema_ddl"]:
                    con.execute(ddl)
                for row in case["rows"]:
                    table, vals = row[0], row[1:]
                    ph = ",".join("?" * len(vals))
                    con.execute(f"INSERT INTO {table} VALUES ({ph})", vals)
            except Exception as e:   # DDL / 灌資料失敗 = 測資側的錯,不是 SQL 的錯
                return {"case_results": results, "mismatches": mismatches,
                        "sql_error": None,
                        "testdata_error": f"建表/灌資料失敗於 {case['case_id']}:{str(e)[:200]}"}
            try:
                rows = con.execute(prepared).fetchall()
            except Exception as e:   # SQL 本身跑不起來(語法/欄位)→ SQL 側的錯
                return {"case_results": results, "mismatches": mismatches,
                        "sql_error": str(e)[:300], "testdata_error": None}
        finally:
            con.close()
        actual = len(rows) > 0
        r = {"case_id": case["case_id"], "condition_id": case["condition_id"],
             "direction": case["direction"], "note": case.get("note", ""),
             "expect_flagged": case["expect_flagged"], "actual_flagged": actual,
             "actual_rows": [[str(v) for v in row] for row in rows[:5]],
             "ok": actual == case["expect_flagged"]}
        results.append(r)
        if not r["ok"]:
            mismatches.append({**r, "rows": case["rows"]})
    return {"case_results": results, "mismatches": mismatches,
            "sql_error": None, "testdata_error": None}


# ------------------------------------------------------------------ 3. 仲裁
ARBITER_SYSTEM = """你是獨立仲裁者。一條規則 SQL 在一個測資案例上的實際行為與
測資預期不符,你要判定:是**測資錯**(測資生成 agent 誤讀規格/造錯資料/預期標錯)
還是 **SQL 錯**(實作與規格不符)。

# 原則(客觀稽核)
- **spec 是唯一真值**:只以 spec 條文判斷,不猜、不腦補規格沒寫的東西。
- 逐步核對:這個案例的資料在 spec 之下「應該」命中嗎?→ 再看 SQL 為什麼給出相反結果。
- 測資與 SQL 都可能錯;不預設任何一方。無法確定時,傾向判測資錯並說明疑點
  (誤殺一個測資案例的代價低;誤指控 SQL 會產生假 finding)。

# 輸出(嚴格 JSON,不要其他文字)
{"who_is_wrong": "testdata" | "sql", "reason": "<依 spec 條文的具體推理>"}"""


async def arbitrate(cfg: Config, spec_text: str, sql: str, mismatch: dict,
                    profile_name: str | None = None) -> dict:
    profile = cfg.role_profile("arbiter") if profile_name is None else cfg.profile(profile_name)
    user = (f"## 核定規格(唯一真值)\n{spec_text}\n\n"
            f"## MR 的 SQL\n```sql\n{sql}\n```\n\n"
            f"## 不符案例\n"
            f"- case_id:{mismatch['case_id']}(條件 {mismatch['condition_id']},"
            f"{mismatch['direction']} 向)\n"
            f"- 測資說明:{mismatch.get('note','')}\n"
            f"- 資料列(表名, 值...):{json.dumps(mismatch['rows'], ensure_ascii=False)}\n"
            f"- 測資預期:{'應命中' if mismatch['expect_flagged'] else '不應命中'};"
            f"SQL 實際:{'命中' if mismatch['actual_flagged'] else '未命中'}"
            f"(輸出列:{mismatch['actual_rows']})\n\n請判定並輸出 JSON。")
    try:
        raw = await run_agent(cfg, profile, ARBITER_SYSTEM, user, hub=None,
                              use_tools=False, verbose=False)
    except Exception as e:   # LLM 傳輸層失敗 → 與「輸出無法解析」同樣保守處理,不炸管線
        return {"who_is_wrong": "sql",
                "reason": f"(仲裁 LLM 呼叫失敗:{type(e).__name__},保守列為 SQL 側待人工確認)",
                "arbiter_unparseable": True}
    d = extract_json(raw) or {}
    if d.get("who_is_wrong") not in ("testdata", "sql"):
        return {"who_is_wrong": "sql", "reason": "(仲裁輸出無法解析,保守列為 SQL 側待人工確認)",
                "arbiter_unparseable": True}
    return d


# ------------------------------------------------------------------ 主流程
def _finding(path: str, severity: str, title: str, detail: str, suggestion: str = "") -> dict:
    return {"file": path, "line": 0, "severity": severity, "title": title,
            "detail": detail, "suggestion": suggestion, "citations": []}


async def run_spec_exec(cfg: Config, hub, mr: dict,
                        spec_code: str | None = None,
                        spec_text: str | None = None) -> dict:
    """執行驗證主流程。spec_code/spec_text 可由 pipeline 先找好傳入(避免重找)。
    回傳 {passed, spec_code, conditions, coverage_gaps, case_results,
          dropped_cases, findings}。"""
    path = mr.get("files", [{}])[0].get("path", "(MR)")
    sql = ""
    for f in mr.get("files", []):
        sql = f.get("full_content") or _sql_from_diff(f.get("diff", "")) or sql

    if spec_text is None:
        spec_code, spec_text = await find_spec(hub, mr)

    if not spec_text:
        detail = (f"MR 涉及規則 {spec_code},但找不到對應的規格檔 specs/{spec_code}.md。"
                  if spec_code else "MR 中未找到規則碼(R-xxx),也無對應規格檔。")
        return {"passed": False, "spec_code": spec_code, "conditions": [],
                "coverage_gaps": [], "case_results": [], "dropped_cases": [],
                "no_spec": True,
                "findings": [_finding(path, "major", "無規格可驗,無法執行驗證",
                                      detail + "執行驗證是合入的必要條件:請先補上核定規格"
                                      "(specs/<規則碼>.md)再送審;本 MR 不得自動放行。",
                                      "依 README「spec 怎麼寫」補上規格檔。")]}
    if not sql:
        return {"passed": False, "spec_code": spec_code, "conditions": [],
                "coverage_gaps": [], "case_results": [], "dropped_cases": [],
                "findings": [_finding(path, "major", "執行驗證無法進行:MR 無可執行的 SQL",
                                      "無法從 diff/檔案內容還原 SQL。")]}

    findings, dropped_cases = [], []

    # 角色一:測資生成 + 確定性形狀/覆蓋檢查
    plan, coverage_gaps, malformed = await generate_cases(cfg, spec_code, spec_text)
    dropped_cases += [{"case_id": (d.get("case") or {}).get("case_id", "?"),
                       "by": "shape-check", "reason": d["reason"]} for d in malformed]
    if not plan["cases"]:
        findings.append(_finding(path, "major", "執行驗證無法完成(測資生成失敗)",
                                 "測資生成 agent 未能產出任何合法案例:" +
                                 "; ".join(coverage_gaps) + "。無法自動驗證,需人工執行驗證。"))
        return {"passed": False, "spec_code": spec_code,
                "conditions": plan["conditions"], "coverage_gaps": coverage_gaps,
                "case_results": [], "dropped_cases": dropped_cases, "findings": findings}

    # 角色二:確定性執行
    ex = execute_cases(sql, plan)
    if ex["testdata_error"]:
        findings.append(_finding(path, "major", "執行驗證無法完成(測資無法建置)",
                                 f"測資生成 agent 產出的 schema/資料無法建置:"
                                 f"{ex['testdata_error']}。需人工執行驗證。"))
        return {"passed": False, "spec_code": spec_code,
                "conditions": plan["conditions"], "coverage_gaps": coverage_gaps,
                "case_results": ex["case_results"], "dropped_cases": dropped_cases,
                "findings": findings}
    if ex["sql_error"]:
        findings.append(_finding(path, "major", "執行驗證:SQL 無法在測資上執行",
                                 f"執行錯誤(語法/欄位):{ex['sql_error']}",
                                 "修正 SQL 使其可依規格的資料表定義執行。"))
        return {"passed": False, "spec_code": spec_code,
                "conditions": plan["conditions"], "coverage_gaps": coverage_gaps,
                "case_results": ex["case_results"], "dropped_cases": dropped_cases,
                "findings": findings}

    # 角色三:仲裁(只對 mismatch 逐案出動)
    sql_faults = []
    for mm in ex["mismatches"]:
        verdict = await arbitrate(cfg, spec_text, sql, mm)
        if verdict["who_is_wrong"] == "testdata":
            dropped_cases.append({"case_id": mm["case_id"], "by": "arbiter",
                                  "reason": verdict["reason"]})
            # 被剔除的案例 = 該條件該向未經驗證 → 列入覆蓋缺口(不可據以自動放行)
            coverage_gaps.append(f"案例 {mm['case_id']}(條件 {mm['condition_id']} "
                                 f"{mm['direction']} 向)遭仲裁剔除(測資錯),該向未驗證")
            for r in ex["case_results"]:
                if r["case_id"] == mm["case_id"]:
                    r["dropped"] = True
        else:
            sql_faults.append({**mm, "arbiter_reason": verdict["reason"],
                               "arbiter_unparseable": verdict.get("arbiter_unparseable", False)})

    if sql_faults:
        detail = ";".join(
            f"案例 {f['case_id']}({f.get('note','')}):預期"
            f"{'命中' if f['expect_flagged'] else '不命中'}、實際"
            f"{'命中' if f['actual_flagged'] else '未命中'} — {f['arbiter_reason'][:150]}"
            for f in sql_faults)
        findings.append(_finding(path, "major", "執行驗證失敗:實作與規格行為不符",
                                 f"規則 {spec_code} 在依規格生成的測資上行為不符:{detail}",
                                 "對照規格與上列案例修正邏輯(含/不含、時段、過濾、粒度)。"))
    if coverage_gaps:
        findings.append(_finding(path, "info", "執行驗證覆蓋缺口",
                                 "以下條件未達 true/false 兩向覆蓋,該部分未經執行驗證:" +
                                 "; ".join(coverage_gaps)))

    effective = [r for r in ex["case_results"] if not r.get("dropped")]
    passed = bool(effective) and not sql_faults and not coverage_gaps
    if passed:
        findings.append(_finding(path, "info",
                                 f"執行驗證通過({len(effective)} 案例全數相符)",
                                 f"規則 {spec_code} 的每個原子條件 true/false 兩向與邊界案例"
                                 f"皆與規格預期一致。"))
    return {"passed": passed, "spec_code": spec_code,
            "conditions": plan["conditions"], "coverage_gaps": coverage_gaps,
            "case_results": ex["case_results"], "dropped_cases": dropped_cases,
            "findings": findings}


def _sql_from_diff(diff: str) -> str:
    lines = []
    for ln in diff.splitlines():
        if ln.startswith("+++") or ln.startswith("@@"):
            continue
        if ln.startswith("+"):
            lines.append(ln[1:])
    return "\n".join(lines)
