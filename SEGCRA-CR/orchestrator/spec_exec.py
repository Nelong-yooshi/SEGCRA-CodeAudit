"""spec_exec — 執行驗證(必跑的確定性閘門,三角色)。

MR 的 SQL 不只要「看起來對」,還要「跑起來對」。流程:

  0. 找 spec(確定性):從 MR 抓規則碼 R-xxx → 對應 specs/<code>.md;完全沒有
     R 編號、且 config 的 dbt.enabled 開啟時(dbt model 常見情境),改依 model
     檔名對應(見 dbt_impact.resolve_spec,#7 第 2 點)。
     **找不到 spec = major finding「無規格可驗」→ 一律 needs_human**
     (「必跑」的意思:不是沒 spec 就跳過,而是沒 spec 就不能自動放行)。
     SQL 是 dbt 樣板時不送進沙盒(原樣執行只會得到語法錯誤,不代表程式有錯),
     改回報「尚未支援樣板的執行驗證」→ 一律 needs_human。
  1. 角色一 測資生成 agent(LLM):讀 spec → 產出 schema DDL + 原子條件清單 +
     逐條件 true/false 兩向 + 邊界值的測資案例(嚴格 JSON)。
     生成後做**確定性形狀/覆蓋檢查**(缺的列入 coverage_gaps)。
  2. 角色二 執行(確定性程式,非 LLM——執行本身不可有幻覺):在 **MS SQL 沙盒**
     (scripts/sandbox.sh 起的 SQL Server 容器)裡,每個案例於獨立交易中建表灌資料、
     原樣執行 MR 的 T-SQL,比對「有無命中」與 expect_flagged,結束即 rollback。
     引擎必須與正式環境相同:不同引擎在型別轉換、日期運算、除法上的語意不同,
     換引擎驗出的「通過」不能代表正式環境的行為。
  3. 角色三 仲裁 agent(LLM):只對 mismatch 逐案出動,判定是**測資錯**
     (測資生成也可能幻覺 → 剔除該案並記錄)還是 **SQL 錯**(→ major finding)。

產出 {passed, conditions, coverage_gaps, case_results, dropped_cases, findings},
全程記錄進 report["_spec_exec"] 供稽核。
"""
import json
import os
import re
import uuid

from .agent import extract_json, run_agent
from .config import Config, PKG_ROOT

SPECS_DIR = PKG_ROOT / "specs"

# 執行窗固定(可重現):@start_date / @end_date 以 DECLARE 代入這兩天(半開區間)
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


async def find_spec(hub, mr: dict, *, by_path: bool = False) -> tuple[str | None, str | None]:
    """回傳 (rule_code, spec_text)。mock 模式讀本地 specs/;real 模式用
    gitlab get_file 抓 repo 內 specs/<code>.md。都找不到 → (code|None, None)。

    R 編號的比對邏輯不變(即使多個編號都對到既有規格,一律取第一個找到的)。
    by_path 為 True **且完全沒有** R 編號時,才改依 model 檔名對應規格:
      * by_path 由呼叫端依 config 的 dbt.enabled 傳入,**預設 False**——跟展開器
        同一個開關,合併後正式審查的行為完全不變,確認後才一起開
      * 只在沒有 R 編號時才試,避免蓋掉「MR 提到了編號、但規格真的不存在」
        這種需要如實回報成「無規格」的情況
    """
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
    if by_path and not codes:
        found = await _find_spec_by_path(hub, mr)
        if found is not None:
            return found
    return (codes[0] if codes else None), None


async def _find_spec_by_path(hub, mr: dict) -> tuple[str, str] | None:
    """依 model 檔名對應規格(dbt_impact.resolve_spec)。只由 find_spec() 在完全
    沒有 R 編號時呼叫,找不到回 None。

    查找順序與上面 R 編號那段相同:**先查本機 specs/,再問 GitLab**。管線呼叫時
    hub 一定存在,所以本機這段不能只在 hub 為 None 時才走,否則 mock 模式(demo、
    golden set)永遠找不到本機規格。
      * 本機:列得出 specs/ 底下實際有哪些檔,拿到 resolve_spec 的完整語意
        (大小寫落差不採用、多個候選不自行擇一)
      * GitLab:目前沒有列目錄的工具(見 docs/09-dbt展開與反查.md 的已知限制),
        改成逐一探測候選路徑——跟 R 編號的做法一致,不新增任何 API 面
    """
    from .dbt_impact import resolve_spec   # 延遲匯入,避免無關呼叫端多背一份相依

    paths = [f["path"] for f in mr.get("files", []) if f.get("path")]
    if not paths:
        return None

    available = ([f"specs/{p.name}" for p in SPECS_DIR.glob("*.md") if p.is_file()]
                 if SPECS_DIR.is_dir() else [])
    for path in paths:
        match = resolve_spec(path, available)
        if match.spec_path:
            local = SPECS_DIR / match.spec_path.rsplit("/", 1)[-1]
            if local.is_file():   # available 就是列這個目錄得來的,理論上必成立
                return local.stem, local.read_text(encoding="utf-8")

    if hub is None:
        return None
    for path in paths:
        for candidate in resolve_spec(path, ()).candidates:   # 只要候選路徑,不比對存在與否
            try:
                txt = await hub.call("gitlab__get_file", {"path": candidate}, truncate=False)
            except Exception:
                continue
            if txt and txt.lstrip().startswith("#"):
                return candidate.rsplit("/", 1)[-1][:-len(".md")], txt
    return None


# ------------------------------------------------------------------ 1. 測資生成
TESTGEN_SYSTEM = """你是測試資料工程師。給你一份異常交易規則的核定規格(spec),
你要產出可機器執行的測資計畫。**spec 是唯一真值**,不得自行增減條件。

# 核心做法:基準案例 + 單點變體
規格的篩選條件通常是「數個分支任一成立」,而每個分支內的各項要**同時**成立。
所以測資這樣造:
  * **基準案例(base)**:讓**某一個分支的每一項同時成立** → 規則必然命中。
  * **變體案例(variant)**:拿該分支的基準,**只改動一處**讓**指定的那一條**
    不成立,其餘一律照抄 → 該分支不成立 → 規則必然不命中。

**你不需要、也不要判斷「這筆該不該被標記」**:基準必然命中、變體必然不命中,
由規格的結構決定,我們自己填。你只要把資料造對。

為什麼不逐條造「讓這一條成立」的案例:只讓一條成立並不會讓規則命中(其他項
還不成立),那種案例的預期無法由規格推出來。

# 產出(嚴格 JSON,不要 markdown 圍欄、不要多餘文字)
{"schema_ddl": ["CREATE TABLE ...(照 spec 的資料表定義,T-SQL,欄位名/型別一字不差)"],
 "conditions": [{"id": "C1-1", "branch": "C1", "data_level": true,
                 "desc": "<spec 的一個原子條件,如:kiosk_outbound_cnt 介於 2-3>",
                 "check_sql": "<判斷這條在本案例資料上成不成立,見第 7 點>"}],
 "cases": [
   {"case_id": "C1-base", "kind": "base", "branch": "C1",
    "rows": [["<表名>", <值1>, <值2>, ...], ...],
    "checks": {"<指標>": "<你算出來的值>(要求 <區間>)"},
    "note": "<這個基準讓哪個分支成立>"},
   {"case_id": "C1-1-v", "kind": "variant", "condition_id": "C1-1",
    "rows": [["<表名>", ...], ...],
    "broke": "<你改了哪一處,如:最後一列的 balance_after 由 500 改為 5000>",
    "note": "<只讓 C1-1 不成立>"}]}

# 硬性要求
1. conditions:把 spec 的需求拆成**原子條件**(每個可獨立為真/假)。每條要標:
   * `branch`:屬於哪一個分支(spec 寫「條件 1 / 條件 2 / 條件 3 任一成立」就用
     "C1"/"C2"/"C3")。spec 沒有分支結構(全部 AND)時,一律標 branch="C1"。
     **全域必要條件**(排除空白帳號、時間窗、已出帳排除等)標 `branch: "*"`,
     表示它屬於每一個分支。**不要為全域條件開一個自己的分支**——「只讓全域條件
     成立」的資料不會讓規則命中(篩選條件都還不成立),那種基準的預期必然是錯的。
     實測就發生過:模型開了一個 GLOBAL 分支,它的基準與兩個變體全部失效。
   * `data_level`:這一條是不是**資料層面**可以驗的。「每帳戶每日至多輸出一筆」、
     「輸出欄位要有哪些」、「待補事項」這類**不是**(標 false,不要為它造變體);
     只有「某筆/某彙總值符不符合」這種才標 true。
2. 每個分支一個基準案例;每個 `data_level: true` 的條件**至少**一個變體案例。
   **區間類的條件(「介於 a-b」)要兩個變體**:一個剛好低於下界、一個剛好高於上界。
   一個變體只能驗一邊的門檻——實測只給一個時,「上界被寫寬一格」抓得到、
   「下界被寫寬一格」全部漏掉。兩個變體的 `condition_id` 相同,`case_id` 要不同
   (例如 `C1-1-lo`、`C1-1-hi`)。
   **不要**產生 `direction` 欄位,那是舊格式。
3. 基準的 `checks`:逐項寫出你算出來的值與要求區間,方便人工複核。
   注意**比值類門檻是別的指標算出來的**(例如出入帳總額比 = 出帳總額 ÷ 入帳總額),
   不能各自隨意填——先選定兩個總額,再確認比值落在區間內。
4. 變體只改一處,其餘照抄基準。`broke` 要寫明改了哪裡。
   **不要順手破壞別的條件**,也要留意改動後不要讓**另一個分支**反而成立
   (例如把次數由 2 改成 5 來破壞「介於 2-3」,但 5 落在另一個分支的「介於 4-10」)。
4b. **門檻類的條件要測在邊界上**,這一點決定測資抓不抓得到「區間抄錯一格」:
   * **變體要剛好跨過門檻一格**(最小單位),不要大幅偏離。區間是「介於 2-3」就用
     **4**,不要用 0;門檻是「≤ 1000」就用 **1001**,不要用 5000。差一格才驗得出
     「上界被寫寬一格」;差很多的話,寫 3 或寫 4 的程式行為都一樣,看不出差別。
   * **基準的數值盡量壓在端點上**(剛好等於門檻)。這樣「含端點」被寫成「不含端點」
     (`<=` 寫成 `<`)才看得出來。
   * **排除類的條件**(時間窗、小額排除、已出帳排除)除了變體,基準裡還要放一筆
     **剛好被排除掉、否則就會符合**的資料列(例如金額剛好等於門檻而規格寫「大於」
     才列計的那一筆、日期落在時間窗外的那一筆、已出現在排除清單裡的那個帳戶)。
     沒有這種資料列,整段排除邏輯被拿掉也不會改變結果。
   * **跨一格時要避開別的分支**:如果往上跨一格剛好落進另一個分支的區間
     (例如「介於 2-3」往上到 4,而另一個分支是「介於 4-10」),規則會因為那個分支
     而命中,變體的預期就錯了。這種情況改從**另一邊**跨(往下到 1),或同時讓
     那個分支的其他條件不成立。實測這一項造成 5 個變體的預期出錯。
   * **分類清單類的條件**(「op_code 屬於 OP01/OP02/OP03」)的基準資料要**每個代碼
     都用到至少一次**。只用其中一個的話,清單裡少列一個代碼也不會改變結果。
5. rows:每列 = [資料表名, 各欄位值...],順序與 schema_ddl 欄位順序一致;
   時間值用 "YYYY-MM-DD HH:MM:SS" 字串、日期用 "YYYY-MM-DD"、BIT 欄位用 1 或 0。
   只用 spec 資料表定義裡存在的表與欄位,不得發明欄位。
6. 時間窗:SQL 的 @start_date 會代入 '{win_start}'、@end_date 代入 '{win_end}'
   (半開區間)。資料的日期**一律**落在 {win_start} 當天,除非你要測的就是時間窗
   那一條。**日期寫錯整份測資都會被過濾掉**,規則不可能命中。
7. check_sql:每個條件附一個**只回一列一欄 0 或 1** 的查詢:
     SELECT CASE WHEN EXISTS (SELECT 1 FROM <表> WHERE <這個條件>) THEN 1 ELSE 0 END
   涉及彙總的條件要把彙總寫進去:
     SELECT CASE WHEN EXISTS (SELECT 1 FROM t GROUP BY account_id
                              HAVING SUM(amount) >= 100000) THEN 1 ELSE 0 END
   限制:**單一 SELECT、不得有分號或註解、不得出現 INSERT/UPDATE/DELETE/DROP/
   CREATE/ALTER/EXEC 等會改變狀態的字**(不符會被忽略,那個條件就失去這道檢查)。
   這些查詢會在沙盒上逐條執行,用來驗:基準讓該分支每一條都成立、
   變體**只有**被指定的那一條不成立。所以變體要精準。
8. 資料庫是 MS SQL(SQL Server),schema_ddl 一律用 T-SQL 型別
   (NVARCHAR(n) / DECIMAL(p,s) / DATETIME2 / DATE / BIT / INT / BIGINT)。
   字串型別一定要寫長度:T-SQL 的 NVARCHAR 不寫長度等於長度 1。"""


def testgen_system() -> str:
    """實際送出的測資生成 system prompt(代入時間窗)。eval 的凍結檔以它的雜湊
    判斷「這份測資是不是用現在的 prompt 生的」,所以兩邊必須拿同一份字串。"""
    return TESTGEN_SYSTEM.replace("{win_start}", WIN_START).replace("{win_end}", WIN_END)


# 條件的判斷式(#23 第 3 點)。模型產生的 SQL 要進沙盒,所以收得比 _DDL_OK 更緊:
# 只接受單一 SELECT、不得有分號與註解、不得出現任何會改變狀態的關鍵字。
#
# 這**不是**完整的 SQL 剖析,擋不住所有壞東西;它的作用是讓「判斷式」維持在
# 判斷式的形狀,而不是變成另一個可以執行任意語句的入口。沙盒本身的權限收斂
# (目前用 sa、容器可對外連線)是 #26 第 2 點的工作,兩者互補。
_CHECK_SQL_MAX = 600
# `xp_`/`sp_` 是**前綴**,不是完整的字:寫成 `\bxp_\b` 時結尾的 \b 要求 `_` 後面
# 是非英數字元,所以 `xp_cmdshell` 反而對不到(#27 review 指出;目前靠 exec 擋住)。
# 它們單獨列一組、不加結尾的 \b。
_CHECK_SQL_FORBIDDEN = re.compile(
    r"(?is)\b(insert|update|delete|drop|create|alter|truncate|merge|grant|revoke|"
    r"exec|execute|backup|restore|shutdown|openrowset|opendatasource|bulk|"
    r"commit|rollback|waitfor|begin|while|goto|declare|set)\b"
    r"|\b(xp_|sp_)")


def _wrap_check_sql(q: str) -> str:
    """把判斷式包進子查詢再送進沙盒。

    **為什麼**:`_check_sql_ok()` 的文字檢查擋不住「不用分號的第二個語句」——T-SQL
    不需要分號就能接下一句,所以 `SELECT 1 COMMIT`、
    `SELECT 1 WAITFOR DELAY '00:01:00'` 都會通過那道檢查(#27 review 指出)。
    `COMMIT` 會讓這個案例灌的資料留下來,**後面的案例就不是從乾淨狀態開始**,
    而每個案例互相獨立是執行驗證的前提。

    包成子查詢之後,多出來的語句在語法上就不合法(`FROM (SELECT 1 COMMIT) AS q(v)`
    會直接是語法錯誤),由資料庫擋掉,不必自己寫 SQL 剖析器。
    `TOP 1` 是因為判斷式本來就該只回一列一欄。
    """
    return f"SELECT TOP 1 * FROM ({q}) AS q(v)"


def _check_sql_ok(text) -> bool:
    """判斷式必須長得像判斷式:單一 SELECT、沒有分號、沒有註解、沒有狀態變更。"""
    if not isinstance(text, str) or not (0 < len(text) <= _CHECK_SQL_MAX):
        return False
    t = text.strip()
    if ";" in t or "--" in t or "/*" in t:
        return False
    if not re.match(r"(?i)^select\s", t):
        return False
    return not _CHECK_SQL_FORBIDDEN.search(t)


def _shape_check(plan: dict) -> tuple[dict, list[str], list[dict]]:
    """確定性檢查測資計畫:JSON 形狀 + 逐條件 true/false 覆蓋。
    回傳 (清洗後 plan, coverage_gaps, dropped_malformed)。"""
    gaps, dropped = [], []
    if not isinstance(plan, dict):
        return {"schema_ddl": [], "conditions": [], "cases": []}, ["整份計畫非 JSON 物件"], []
    ddl = [d for d in plan.get("schema_ddl", []) if isinstance(d, str) and _DDL_OK.match(d)]
    conds = []
    for c in plan.get("conditions", []):
        if not (isinstance(c, dict) and c.get("id") and c.get("desc")):
            continue
        # check_sql 是**可選**的(#23 第 3 點):沒給就不做標籤一致性檢查,行為同以往。
        # 給了但形狀不合法 → 丟掉那個欄位而不是丟掉整個條件:條件本身是好的,
        # 只是附帶的判斷式不能用,不該因此讓這個條件的案例全部消失。
        if "check_sql" in c and not _check_sql_ok(c.get("check_sql")):
            c = {k: v for k, v in c.items() if k != "check_sql"}
            dropped.append({"case": {"condition_id": c["id"]},
                            "reason": "條件的 check_sql 形狀不合法(非單一 SELECT、含分號/註解/狀態變更),已忽略"})
        conds.append(c)
    cond_ids = {c["id"] for c in conds}
    branches = _branches(conds)
    # 兩種計畫形狀並存:
    #   新(基準＋變體):案例有 kind="base"/"variant"。基準讓整個分支成立、
    #     必然命中;變體從基準只壞一條、必然不命中。
    #   舊(每條件兩向):案例有 direction="true"/"false"。現有的凍結檔是這種,
    #     要繼續讀得懂,不能因為換了設計就讓它們全部失效。
    new_shape = any(isinstance(c, dict) and c.get("kind") in ("base", "variant")
                    for c in plan.get("cases", []) or [])
    cases = []
    for c in plan.get("cases", []):
        ok, why = _case_ok(c, cond_ids, branches, new_shape)
        if ok:
            cases.append(_normalize_case(c, new_shape))
        else:
            dropped.append({"case": c, "reason": why})
    if not ddl:
        gaps.append("無合法 schema_ddl")
    if new_shape:
        # 覆蓋的定義跟著換:每個分支要有基準,每個條件要有變體。
        # 「每條件 true/false 兩向」對合取規則定義不出正確的預期——單獨成立
        # 一個條件不會讓規則命中,所以那種案例的 expect_flagged 必然是錯的
        # (實測 46 個案例 12 個錯,其中 10 個是這個原因)。
        for b_id in sorted(branches):
            if not any(c["kind"] == "base" and c.get("branch") == b_id for c in cases):
                gaps.append(f"分支 {b_id} 缺基準案例(整個分支都成立、規則應命中)")
        for c0 in conds:
            cid = c0["id"]
            if not _data_level(c0):
                # 「每帳戶每日至多一筆」、「輸出欄位」、「待補事項」這類不是資料層面
                # 可以驗的條件,造不出「只壞這一條」的變體。要求它會永遠補不完,
                # 而它本來就該由靜態審查管,不是執行驗證。
                continue
            if not any(c["kind"] == "variant" and c["condition_id"] == cid for c in cases):
                gaps.append(f"條件 {cid} 缺變體案例(從基準只壞這一條、規則應不命中)")
    else:
        for cid in sorted(cond_ids):
            dirs = {c["direction"] for c in cases if c["condition_id"] == cid}
            for d in ("true", "false"):
                if d not in dirs:
                    gaps.append(f"條件 {cid} 缺 {d} 向案例")
    if not conds:
        gaps.append("未拆出任何原子條件")
    return {"schema_ddl": ddl, "conditions": conds, "cases": cases}, gaps, dropped


def _data_level(cond: dict) -> bool:
    """這個條件能不能用資料驗。預設 True:舊的計畫沒有這個欄位,行為要照以往。"""
    return cond.get("data_level", True) is not False


def _is_new_shape(plan: dict) -> bool:
    """計畫是不是「基準＋變體」的形狀。

    兩個來源都算:案例帶 kind(已經生出案例),或條件帶 branch/data_level
    (只拿到骨架、還沒有案例時也要能判斷接下來該要什麼)。
    """
    if any(isinstance(c, dict) and c.get("kind") in ("base", "variant")
           for c in plan.get("cases") or []):
        return True
    return any(isinstance(c, dict) and ("branch" in c or "data_level" in c)
               for c in plan.get("conditions") or [])


GLOBAL_BRANCH = "*"   # 這個條件屬於**每一個**分支(排除空白帳號、時間窗…)


def _branches(conds: list[dict]) -> set[str]:
    """條件分成哪幾個 OR 分支。**不含 "*"** ——那不是一個分支。

    沒有宣告 branch 的計畫視為單一分支(空字串):純 AND 的規格(例如 R-140)
    本來就只有一個分支,基準＝全部條件成立,變體＝只壞一條,語意完全成立。

    全域條件標 `branch: "*"`,表示它屬於每一個分支。它**不該有自己的基準**:
    「只讓全域條件成立」的資料不會讓規則命中(篩選條件都還不成立),那種基準的
    預期必然是錯的。實測發生過:模型開了一個 GLOBAL 分支,它的基準與兩個變體
    全部失效,而且連帶讓「拿掉空白帳號排除」「拿掉 EarlyJob」兩個突變測不到。
    """
    out = {str(c.get("branch", "") or "") for c in conds}
    out.discard(GLOBAL_BRANCH)
    return out or {""}


def _rows_ok(rows) -> bool:
    return (isinstance(rows, list) and bool(rows)
            and all(isinstance(r, list) and len(r) >= 2 and isinstance(r[0], str)
                    and _IDENT.match(r[0]) for r in rows))


def _case_ok(c, cond_ids: set, branches: set, new_shape: bool) -> tuple[bool, str]:
    if not (isinstance(c, dict) and c.get("case_id") and _rows_ok(c.get("rows"))):
        return False, "形狀不合法(缺 case_id 或資料列,或表名非法)"
    if not new_shape:
        if c.get("condition_id") not in cond_ids:
            return False, "形狀不合法(condition_id 不在條件清單裡)"
        if c.get("direction") not in ("true", "false"):
            return False, "形狀不合法(direction 必須是 true/false)"
        if not isinstance(c.get("expect_flagged"), bool):
            return False, "形狀不合法(expect_flagged 必須是布林)"
        return True, ""
    kind = c.get("kind")
    if kind == "base":
        if str(c.get("branch", "") or "") not in branches:
            return False, "基準案例的 branch 不在條件宣告的分支裡"
        return True, ""
    if kind == "variant":
        if c.get("condition_id") not in cond_ids:
            return False, "變體案例的 condition_id 不在條件清單裡"
        return True, ""
    return False, "形狀不合法(kind 必須是 base 或 variant)"


def _normalize_case(c: dict, new_shape: bool) -> dict:
    """新形狀的 expect_flagged **由我們推導**,不看模型給的值。

    基準:整個分支成立 → 規則必然命中 → True
    變體:從基準只壞一條 → 該分支不成立 → 規則必然不命中 → False

    為什麼不信模型:今天 46 個案例裡 12 個的 expect_flagged 是錯的,而預期值
    錯了比沒有案例更糟——它會讓正確的實作被判成不符。基準與變體的預期是
    由構造決定的,根本不需要模型判斷。
    """
    if not new_shape:
        return c
    out = dict(c)
    out["expect_flagged"] = (c.get("kind") == "base")
    if c.get("kind") == "base":
        out["branch"] = str(c.get("branch", "") or "")
        out.setdefault("condition_id", f"__base__{out['branch']}")
        out["direction"] = "true"
    else:
        out["direction"] = "false"
    return out


def _covered(plan: dict) -> int:
    """已覆蓋的「條件 × 方向」數。plan 須已經過 _shape_check(案例都指向存在的條件)。"""
    return len({(c["condition_id"], c["direction"]) for c in plan["cases"]})


# 靠 json-repair 救回來的計畫,缺口清單要先講真正的原因。放在最前面是因為
# 看的人通常只看第一條:寫「缺條件 C5 的 false 向」會把人導向「改 prompt 讓模型
# 多生一個案例」,而真正該做的是讓輸出的 JSON 合法。
_MALFORMED_GAP = ("測資計畫的 JSON 不合法,靠修補救回——**案例可能已經遺失**,"
                  "下面列出的缺口不一定是模型沒生,也可能是被修補過程吃掉的。"
                  "先看輸出格式,不要照缺口清單去調 prompt。")


def _malformed_prompt(spec_code: str, spec_text: str) -> str:
    """JSON 不合法時的重試 prompt。

    刻意**不**列出缺口:這一次的缺口清單是從修補過的物件算出來的,不可信,
    拿它去要求模型「補某某條件」只會讓它在錯的方向上努力。
    """
    return (f"上一次的輸出不是合法的 JSON(少了括號或引號之類的語法錯誤)。"
            f"請重新輸出一份**語法完全正確**的 JSON,不要有任何說明文字、"
            f"不要用 ``` 圍欄。內容要求與先前相同:每個條件的 true/false 兩向都要有。"
            f"\n\n規則 {spec_code} 規格:\n{spec_text}")


# 一次補幾個「條件 × 方向」,以及最多補幾輪。
#
# 這兩個值取自**真端點的實測**(RETAIL_M1_EVAL / gemma4:31b / 2026-10-13,
# 23 個條件 = 46 個「條件×方向」,13 次呼叫、151 分鐘):
#
#   要求的批量 | 24      | 12      | 6              | 3
#   結果       | 截斷    | 截斷    | 2 次成功1 次截斷 | 5 次全成功
#   拿到的案例 | 0(丟棄)| 0(丟棄)| 6/6 照做       | 3/3 照做
#
# 模型**完全照做**:沒多給、沒漏給、沒改條件編號(成功的 11 次補缺,要 N 個就
# 回 N 個)。唯一的失效方式是截斷,不是不聽話。
#
# 一次裝得下幾個案例由 token 算:上限 8192,一個案例約 790 tokens,再扣掉思考
# (實測約 1500,但**每次不同**——同樣要 6 個,前兩次成功、第三次截斷)。
# 所以沒有「安全批量」,只能保守起步 + 被截斷就縮(見下方 length 分支)。
# 要用**最壞的思考量**算,不是平均:思考與案例內容搶同一個輸出上限,而它每次
# 都不同。10 次成功的呼叫倒推出來的思考量(總 tokens - 案例數 × 790):
#
#   要求 6 個 → 1481、3236
#   要求 5 個 → 3514、3316
#   要求 3 個 → 2675、2711、3851、4467、4900、5027
#
# 範圍 1481–5027、中位數 3415,而且**與要求的數量沒有明顯關係**(要 3 個那次
# 反而最高)。用最壞值 5027 算安全批量:
#
#   上限  8192 → (8192 - 5027) / 790 = 4 個
#   上限 12288 → (12288 - 5027) / 790 = 9 個
#
# 這解釋了所有觀察到的截斷,包括兩次我預測錯的:
#   批量 10 / 上限 12288 → 10 × 790 + 5027 = 12927 > 12288  ✗(實測截斷)
#   批量  6 / 上限  8192 →  6 × 790 + 5027 =  9767 >  8192  ✗(實測 3 次中 1 次截斷)
#
# 取 8 而不是 9:留 940 tokens 餘裕(只量過 10 次,5027 不保證是真正的上界),
# 而且一次截斷的代價是 13-20 分鐘。
#
# 「提高上限」真正的價值也在這裡:不是能一次要更多,而是**思考暴衝時不會把整次
# 呼叫浪費掉**——8192 下連 4 個以上都有風險,12288 下 8 個很安全。
#
# (此前的 24 是用「字元數 ÷ 3 = tokens」掃出來的,那個換算高估容量約 6 倍,
#  真端點上 24 與 12 連續兩次截斷、白跑 31 分鐘。見 tests/tools/sim_testgen.py。)
_FILL_BATCH = 5
# 輪數:46 個「條件×方向」每次補 3-6 個,至少要 8 輪;實測有 3 次截斷不產出,
# 所以要留餘裕。這次用 8 輪只補到 30/46(16 個缺口),13 次呼叫 151 分鐘——
# 補完整需要約 14 輪、估 3 小時。這就是大規則測資生成的真實成本,也是凍結
# 測資快取(eval/testdata_cache.py)不是優化而是必需品的原因。
#
# 輪數是**上限不是固定成本**:小規則(R-140,7 個條件)一次呼叫就生完,
# 完全不會進補缺模式,這個值對它沒有任何影響。
_FILL_ROUNDS = 14


def _skeleton_prompt(spec_code: str, spec_text: str) -> str:
    """只要 schema 與條件清單,**不要案例**。

    大規則的第一次生成會直接撞上輸出上限(實測 RETAIL_M1_EVAL 在正式設定下
    3/3 次截斷、0 個案例),於是連一份可以拿來補的計畫都沒有——補缺模式永遠
    不會啟動。骨架的輸出小得多,拿到之後再逐批補案例。
    """
    return (f"先**只**產出測資計畫的骨架,不要任何案例:\n"
            f'只輸出 {{"schema_ddl": [...], "conditions": [...], "cases": []}}。\n'
            f"conditions 要依規格完整拆解成原子條件(這一步最重要,之後的案例都掛在\n"
            f"它上面),每條附 **id、branch、data_level、desc、check_sql** 五個欄位:\n"
            f"  * branch:屬於哪一個分支(規格寫「條件 1/2/3 任一成立」就用 C1/C2/C3;\n"
            f"    全部都是 AND 的規格一律 C1)。**漏掉這個欄位,後面的案例會照錯的\n"
            f"    方式產生。**\n"
            f"  * data_level:這一條能不能用資料驗(「每帳戶每日至多一筆」、「輸出欄位」、\n"
            f"    「待補事項」這類標 false)。\n"
            f"**cases 一律留空陣列。**\n\n"
            f"規則 {spec_code} 規格:\n{spec_text}")


def _missing_pairs(plan: dict) -> list[tuple[str, str]]:
    """還缺哪些案例。只看 plan 自己列出的條件,與 _shape_check 同一把尺。

    新形狀回 ("base", 分支) 與 ("variant", 條件);舊形狀回 (條件, 方向)。
    兩者都是 (種類, 對象) 的二元組,所以補缺的批量邏輯不用改。
    **基準排在變體前面**:變體要從基準改出來,沒有基準就沒有東西可以改。
    """
    if not _is_new_shape(plan):
        have = {(c["condition_id"], c["direction"]) for c in plan["cases"]}
        return [(c["id"], d) for c in plan["conditions"] for d in ("true", "false")
                if (c["id"], d) not in have]
    cases = plan.get("cases") or []
    have_base = {str(c.get("branch", "") or "") for c in cases if c.get("kind") == "base"}
    have_var = {c.get("condition_id") for c in cases if c.get("kind") == "variant"}
    out = [("base", b) for b in sorted(_branches(plan.get("conditions") or []))
           if b not in have_base]
    out += [("variant", c["id"]) for c in plan.get("conditions") or []
            if _data_level(c) and c["id"] not in have_var]
    return out


def _base_rows_for(plan: dict, cond_id: str) -> tuple[str, list] | None:
    """某個條件所屬分支的基準案例(變體要從它改出來)。沒有就回 None。

    全域條件(branch="*")屬於每一個分支,沒有自己的基準——取任一個分支的基準即可。
    """
    branch = next((str(c.get("branch", "") or "") for c in plan.get("conditions") or []
                   if c.get("id") == cond_id), None)
    if branch is None:
        return None
    bases = [c for c in plan.get("cases") or [] if c.get("kind") == "base"]
    if branch == GLOBAL_BRANCH:
        return (bases[0].get("branch", ""), bases[0].get("rows") or []) if bases else None
    for c in bases:
        if str(c.get("branch", "") or "") == branch:
            return branch, c.get("rows") or []
    return None


def _fill_gaps_prompt(spec_code: str, spec_text: str, plan: dict,
                      missing: list[tuple[str, str]], batch: int) -> str:
    """只要缺的那幾個案例,不要整份重來。

    為什麼不整份重來:大規則一次生不完(實測 RETAIL_M1 要 46 個案例,模型自己停在
    14 個;調高上限到 4 倍也只多 3 個)。每次都要求「輸出完整計畫(包含已經有的
    案例)」等於每次都在重付一遍已經成功的部分的代價,而那正是裝不下的原因。

    只問缺的那幾個,單次輸出就小得多,也不必跟已經生好的部分搶輸出空間。
    """
    by_id = {c["id"]: c["desc"] for c in plan["conditions"]}
    todo = missing[:batch]
    if not _is_new_shape(plan):
        lines = [f"- 條件 {cid}({by_id.get(cid, '')}) 的 {d} 向" for cid, d in todo]
        extra = ""
    else:
        lines, bases_shown = [], {}
        for kind, target in todo:
            if kind == "base":
                members = [c["id"] for c in plan["conditions"]
                           if str(c.get("branch", "") or "") == target]
                lines.append(f"- 分支 {target} 的**基準案例**"
                             f"(要讓這些條件同時成立:{', '.join(members)})")
            else:
                lines.append(f"- 條件 {target}({by_id.get(target, '')}) 的**變體**"
                             f"——從該分支的基準只改一處,讓這一條不成立")
                got = _base_rows_for(plan, target)
                if got:
                    bases_shown[got[0]] = got[1]
        # 變體要「從基準改一處」,所以基準的資料列必須附上——不然沒有東西可以改,
        # 模型只能自己重新發明一組,那正是預期值出錯的來源。
        extra = ""
        for b_id, rows in bases_shown.items():
            extra += (f"\n分支 {b_id} 的基準資料(變體請照抄、只改要壞的那一處):\n"
                      + json.dumps(rows, ensure_ascii=False) + "\n")
    return (
        f"先前已經產出部分測資計畫,現在**只要補缺的案例**,不要重新輸出整份。\n\n"
        f"要補的({len(todo)} 個,共缺 {len(missing)} 個):\n" + "\n".join(lines) + "\n"
        + extra + "\n"
        f"請只輸出 JSON 物件 `{{\"cases\": [...]}}`,裡面只放上面列出的那幾個案例。\n"
        f"**編號必須用上面給的,不要自己改名或新增條件**;\n"
        f"資料表與欄位沿用先前的 schema(不要重新定義):\n"
        + "\n".join(plan["schema_ddl"]) + "\n\n"
        f"規則 {spec_code} 規格:\n{spec_text}")


def _merge_cases(base: dict, addition: dict) -> tuple[dict, int, list[dict]]:
    """把補生成的案例併回 base,回傳 (合併後, 實際併入數, 被拒絕的)。

    **只併,不覆寫。** 拒絕三種:
      * `condition_id` 不在 base 的條件清單裡 —— 模型自己發明了條件,併進去會讓
        覆蓋率看起來變好,但那個條件根本不在規格的拆解裡
      * 形狀不合法 —— 與 _shape_check 同一把尺
      * 已經有同一個「條件 × 方向」 —— 保留先前那個,補生成不該改寫已經驗過的案例

    schema_ddl 一律沿用 base:補生成的 prompt 已經把 schema 給它了,如果它還是
    回了不同的 DDL,那是它沒照做,**不能拿新的 DDL 去建表**——先前的案例是依
    base 的 schema 寫的,換了表結構那些案例就跑不起來了。
    """
    cond_ids = {c["id"] for c in base["conditions"]}
    branch_ids = _branches(base["conditions"])
    # 借 _shape_check 驗形狀:把補生成的案例掛在 base 的條件上一起檢查
    probe = {"schema_ddl": base["schema_ddl"], "conditions": base["conditions"],
             "cases": addition.get("cases", []) if isinstance(addition, dict) else []}
    cleaned, _, dropped = _shape_check(probe)
    # 「已經有了」的判斷依形狀而異:
    #   新形狀 → 基準看分支、變體看條件
    #   舊形狀 → 看「條件 × 方向」
    # 不可以一律用 condition_id:基準的 condition_id 是我們自己補的
    # `__base__<分支>`,不在條件清單裡,會被下面那條「條件不在原計畫裡」整批拒收
    # ——補缺模式就永遠拿不到基準(實測確認過)。
    def _key(c):
        if c.get("kind") == "base":
            return ("base", str(c.get("branch", "") or ""))
        if c.get("kind") == "variant":
            # 認 case_id:區間類的條件要**兩個**變體(剛好低於下界、剛好高於上界),
            # 一個變體只驗得到一邊的門檻。只認 condition_id 的話第二個會被當成
            # 「已經有了」拒收。
            return ("variant", c["condition_id"], c["case_id"])
        return (c["condition_id"], c["direction"])

    def _known(c):
        if c.get("kind") == "base":
            return str(c.get("branch", "") or "") in branch_ids
        return c["condition_id"] in cond_ids

    have = {_key(c) for c in base["cases"]}
    added = []
    for c in cleaned["cases"]:
        key = _key(c)
        if not _known(c) or key in have:
            dropped.append({"case": c,
                            "reason": "補生成:條件/分支不在原計畫裡,或已經有這一個案例"})
            continue
        have.add(key)
        added.append(c)
    return ({**base, "cases": [*base["cases"], *added]}, len(added), dropped)


def _retry_prompt(spec_code: str, spec_text: str, gaps: list[str],
                  n_conds: int, max_conds: int) -> str:
    """有合法案例但不夠好時的重試提示:講清楚上一次差在哪,讓每次重試的 prompt
    都不同(固定 seed 下同一個 prompt 只會得到同一份輸出)。仍要求輸出**完整**
    計畫,不是只補缺的部分——這裡收的是整份計畫,不做合併。"""
    issues = []
    if gaps:
        issues.append("上一次的測資計畫覆蓋不完整,缺:\n" + "\n".join(f"- {g}" for g in gaps))
    if n_conds < max_conds:
        issues.append(f"上一次只拆出 {n_conds} 個原子條件,先前的嘗試拆出過 {max_conds} 個;"
                      f"請依規格完整拆解,不要合併或省略條件。")
    return ("\n\n".join(issues)
            + "\n\n請重新輸出**完整的**測資計畫 JSON(包含已經有的案例),"
              "每個條件的 true/false 兩向都要有案例。\n\n"
            + f"規則 {spec_code} 規格:\n{spec_text}")


async def generate_cases(cfg: Config, spec_code: str, spec_text: str,
                         profile_name: str | None = None,
                         max_attempts: int = 3,
                         seed_plan: dict | None = None,
                         ) -> tuple[dict, list[str], list[dict]]:
    """角色一:LLM 依 spec 生成測資計畫,回傳 (plan, gaps, dropped)。

    只要有合法案例就接受、不管 coverage_gaps 是否為空,曾經是這裡的行為——
    但單次 LLM 取樣的覆蓋度本身會飄(同一份規格、同一個 prompt,不同次呼叫
    可能拆出不同數量的條件與案例)。只在「解析失敗/零案例」才重試,等於把
    這次取樣抽到的覆蓋度直接當結果收下,抽到不完整的就是不完整的結果。

    所以現在 coverage_gaps 非空也算「這次不夠好」,一樣觸發重試,而且重試的
    prompt 會列出上一次缺了哪些條件、哪個方向。固定 temperature/seed 時,同一個
    prompt 會得到逐位元組相同的輸出,不帶缺口的重試等於白跑。

    「最佳」看的是**已覆蓋的「條件 × 方向」數**,不是缺口數:缺口是對照模型
    自己拆出的條件算的,拆 12 個條件全覆蓋的那次是 0 缺口,會勝過拆齊 23 個、
    只缺幾個案例的那次。同理,0 缺口也要條件數不少於之前各次嘗試的最大值
    才提前結束,否則繼續試、最後仍收覆蓋數最多的一版。

    已知限制:第一次就抽到「拆得少、0 缺口」時沒有東西可比,仍會直接收下。
    要擋這個得拿規格本身的條件數比對,不是靠多次嘗試互比。

    仍然抽不到 0 缺口時,回傳嘗試過的最佳結果(而不是最後一次、也不是直接判定
    失敗)——覆蓋不完整要如實反映在 coverage_gaps 裡,由後續判斷是否需要人工
    確認,不能因為「retry 用完了」就悄悄退化成沒案例。

    `seed_plan` 給定時,**接著它的缺口繼續補**,不重新生成整份(也不重新要骨架)。

    為什麼需要:大規則一輪補不完。實測 RETAIL_M1_EVAL(46 個「條件×方向」)
    13 次呼叫 151 分鐘只補到 30 個——不是機制失效,是一次呼叫只裝得下 3-6 個
    案例,而輪數有上限。但補缺是**可累積**的:模型對補缺的要求完全照做
    (成功的 11 次:要 N 個就回 N 個,沒多給、沒漏給、沒改條件編號),所以
    接著上次的結果再跑一輪就會往前推進,兩三輪之後覆蓋必然收斂。

    這比「把單次做得更強」可靠:不依賴任何一次呼叫的運氣(截斷與否取決於那次
    思考燒掉多少 token),而且中途斷掉不會把已經生好的案例丟掉。"""
    system = testgen_system()
    user = f"規則 {spec_code} 的核定規格如下,請產出測資計畫 JSON:\n\n{spec_text}"
    profile = cfg.role_profile("testgen") if profile_name is None else cfg.profile(profile_name)
    llm_error = None
    truncated = 0
    malformed = 0
    filling = False          # 這一輪送出去的是「只補缺的」還是整份重來
    fill_batch = _FILL_BATCH # 補缺的批量;一旦被截斷就減半(見下方 length 分支)
    filled_total = 0         # 靠補生成多拿到幾個案例(寫進失敗原因,供判讀)
    best: tuple[dict, list[str], list[dict]] | None = None
    best_repaired = False
    max_conds = 0
    if seed_plan is not None:
        # 接著上次的結果補:把它當成已經拿到的最佳版,直接進補缺模式。
        seeded, seeded_gaps, seeded_dropped = _shape_check(seed_plan)
        if seeded["conditions"] and seeded["schema_ddl"]:
            missing = _missing_pairs(seeded)
            if not missing:
                return seeded, seeded_gaps, seeded_dropped   # 已經滿了,不用問模型
            best = (seeded, seeded_gaps, seeded_dropped)
            max_conds = len(seeded["conditions"])
            filling = True
            user = _fill_gaps_prompt(spec_code, spec_text, seeded, missing, fill_batch)
    # 補缺的輪數不佔整份生成的次數:它們是不同性質的呼叫(小輸出 vs 整份計畫),
    # 混在同一個預算裡會讓大規則永遠補不完——補一輪就用掉一次整份生成的機會。
    for attempt in range(max_attempts + _FILL_ROUNDS):
        if not filling and attempt >= max_attempts:
            break           # 整份生成的次數用完了,而且目前不在補缺模式
        meta: dict = {}
        try:
            raw = await run_agent(cfg, profile, system, user, hub=None,
                                  use_tools=False, verbose=False, meta=meta)
        except Exception as e:   # LLM 傳輸層失敗(連線/逾時)→ 降級為「測資生成失敗」,不炸管線
            llm_error = f"{type(e).__name__}: {e}"
            continue
        # 被 max_tokens 截斷 → 這次結果一律不可信,連看都不看就重試。
        # **這一關不能靠 _shape_check 代勞**:截斷的 JSON 會被 extract_json 的
        # json-repair 修補成合法物件,形狀檢查只會看到「案例比較少」,看不出
        # 它其實是被硬切的殘骸。實測 RETAIL_M1:finish_reason=length、
        # completion_tokens 剛好 8192、輸出斷在一個數字中間,但照樣解析成功,
        # 案例從應有的 46 個掉到 9 個——而那 9 個會被當成正常結果收下。
        if meta.get("finish_reason") == "length":
            truncated += 1
            if filling and best is not None:
                # **補缺的回應被截斷 → 這一批要得太多,下次少要一點。**
                # 不可以退回去要「精簡版的完整計畫」:那條路已知裝不下(大規則
                # 3/3 次截斷),等於把已經拿到的骨架與案例丟掉重來。
                fill_batch = max(1, fill_batch // 2)
                user = _fill_gaps_prompt(spec_code, spec_text, best[0],
                                         _missing_pairs(best[0]), fill_batch)
                continue
            # 還沒有任何可用計畫就被截斷 → 改要骨架(只有 schema 與條件,不要案例)。
            # 繼續要「精簡版的完整計畫」沒有用:大規則連精簡過的完整計畫都裝不下,
            # 實測 RETAIL_M1_EVAL 3/3 次都是這樣,最後 0 個案例。
            # 骨架拿到之後,案例靠補缺模式一批一批補。
            user = _skeleton_prompt(spec_code, spec_text)
            continue
        # JSON 不合法、靠 json-repair 救回來的輸出**不可信**,理由跟截斷同一類:
        # repair 不會失敗,它會把壞掉的 JSON 修補成一個**合法但語意錯誤**的物件。
        # 實測(#23)R-140 少打一個 `]`:
        #     "rows": [["txn_log", 1, ..., null], "expect_flagged": true, ...},
        # repair 把後面所有案例都塞進這一筆的 `rows`,那一筆因為形狀不對被剔除,
        # 後面的案例跟著消失——而報告上寫的是「條件 C5 缺 false 向案例」。
        # 一個標點符號的問題被回報成「模型覆蓋不足」,兩者的修法完全不同。
        parse_meta: dict = {}
        plan = extract_json(raw, parse_meta)
        repaired = bool(parse_meta.get("repaired"))
        if repaired:
            malformed += 1
        if filling and plan and best is not None and not repaired:
            # 這一輪問的是「只要缺的那幾個」,收到的是案例片段不是整份計畫,
            # 所以併回去而不是拿去跑 _shape_check 當新計畫(那會把已經生好的
            # 部分整個丟掉——正是大規則一直生不完的原因)。
            merged, n_added, merge_dropped = _merge_cases(best[0], plan)
            cleaned, gaps, _ = _shape_check(merged)
            best = (cleaned, gaps, [*best[2], *merge_dropped])
            filled_total += n_added
            if not gaps:
                return best
            if n_added == 0:
                # 補不進任何東西(模型改了條件編號、或只回了已有的方向)。
                # 再問同一批只會得到同樣的結果,換回整份重來。
                #
                # 條件數要看**這次模型回的**那份,不是合併後的:合併後一定等於
                # 先前的最佳值,那樣「你這次拆得比之前少」這個提示永遠不會出現,
                # 而那正是補缺失敗最常見的原因之一(模型把條件重新合併了)。
                attempted, _, _ = _shape_check(plan)
                filling = False
                user = _retry_prompt(spec_code, spec_text, gaps,
                                     len(attempted["conditions"]), max_conds)
                continue
            user = _fill_gaps_prompt(spec_code, spec_text, cleaned,
                                     _missing_pairs(cleaned), fill_batch)
            continue
        if plan:
            cleaned, gaps, dropped = _shape_check(plan)
            # 骨架(有條件、沒案例)也要收:它是補缺模式的起點。沒有這一條,
            # 大規則會停在「第一次就截斷 → 沒有計畫 → 補缺永遠不啟動」。
            if not cleaned["cases"] and cleaned["conditions"] and cleaned["schema_ddl"]:
                best, best_repaired = (cleaned, gaps, dropped), repaired
                max_conds = max(max_conds, len(cleaned["conditions"]))
                filling = True
                user = _fill_gaps_prompt(spec_code, spec_text, cleaned,
                                         _missing_pairs(cleaned), fill_batch)
                continue
            if cleaned["cases"]:
                n_conds = len(cleaned["conditions"])
                max_conds = max(max_conds, n_conds)
                # 修補過的結果不走「提前結束」:它的 0 缺口可能是假的
                # (案例被 repair 吃掉之後,剩下的對它自己列出的條件剛好齊全)。
                if not repaired and not gaps and n_conds >= max_conds:
                    return cleaned, gaps, dropped   # 完整覆蓋且沒有拆得比之前少,不用再試
                # 乾淨的結果一律勝過修補過的,即使覆蓋數比較少——
                # 修補過的「覆蓋數」本身就不可信,拿它跟乾淨的比是比錯東西。
                better = (best is None
                          or (best_repaired and not repaired)
                          or (best_repaired == repaired
                              and _covered(cleaned) > _covered(best[0])))
                if better:
                    best = (cleaned, gaps, dropped)
                    best_repaired = repaired
                if repaired:
                    user = _malformed_prompt(spec_code, spec_text)
                    continue
                # 有一份可用的計畫了 → 接下來只補缺的,不要整份重來。
                missing = _missing_pairs(cleaned)
                if missing and best is not None and cleaned is best[0]:
                    filling = True
                    user = _fill_gaps_prompt(spec_code, spec_text, cleaned,
                                             missing, fill_batch)
                else:
                    user = _retry_prompt(spec_code, spec_text, gaps, n_conds, max_conds)
                continue
        user = (f"上一次輸出無法解析或沒有任何合法案例,"
                f"請重新輸出**完整覆蓋每個條件 true/false 兩向**的 JSON。\n\n"
                f"規則 {spec_code} 規格:\n{spec_text}")
    if best is not None:
        if best_repaired:
            # 這份計畫是靠修補救回來的,它的 coverage_gaps **不能當成真的覆蓋缺口**:
            # 被 repair 吃掉的案例不會出現在任何地方,看起來就像模型沒生。
            # 把真正的原因放在最前面,免得看的人照著「缺某個條件」的方向去修 prompt。
            plan_, gaps_, dropped_ = best
            return plan_, [_MALFORMED_GAP, *gaps_], dropped_
        return best
    if truncated:
        # 這條失敗原因要講得夠具體才有用:「規則太大、輸出裝不下」跟「模型輸出
        # 亂七八糟」的修法完全不同——前者要調 max_output_tokens 或分批生成,
        # 後者是 prompt 或模型的問題。混在同一句話裡,看的人無從判斷。
        reason = (f"測資生成失敗({truncated}/{max_attempts} 次因超過輸出長度上限被截斷)。"
                  f"這條規則的測資計畫裝不進目前的 max_output_tokens;"
                  f"需調高 roles.testgen 所用 profile 的上限,或改成分批生成。")
    elif malformed:
        # 跟截斷分開講:截斷要調上限或分批,格式錯誤要修 prompt 或限制輸出格式,
        # 修法完全不同,混在同一句話裡看的人無從判斷。
        reason = (f"測資生成失敗({malformed}/{max_attempts} 次輸出的 JSON 不合法)。"
                  f"這是輸出格式的問題,不是覆蓋度不足;"
                  f"考慮改用 response_format 的 json_schema 限制輸出格式。")
    elif llm_error:
        reason = f"測資生成失敗(LLM 呼叫失敗:{llm_error})"
    else:
        reason = f"測資生成失敗({max_attempts} 次嘗試皆無合法輸出)"
    return {"schema_ddl": [], "conditions": [], "cases": []}, [reason], []


# ------------------------------------------------------------------ 2. 確定性執行
_SYSDATE = re.compile(r"\b(GETDATE|SYSDATETIME|GETUTCDATE|SYSUTCDATETIME)\s*\(\s*\)|\bCURRENT_TIMESTAMP\b",
                      re.I)
_SANDBOX_DB_PREFIX = "segcra_sbx_"
_LEADING_COMMENTS = re.compile(r"^(?:\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/))*", re.S)
_WRAPPED = re.compile(r"\s*(?:WITH\b[\s\S]*?\bINSERT\b|INSERT\b|CREATE\s+(?:OR\s+ALTER\s+)?VIEW\b)", re.I)


def _sandbox_params() -> dict:
    """沙盒連線設定:環境變數優先,沒設就讀 config/sandbox.env(不進版控)。"""
    env_file = PKG_ROOT / "config" / "sandbox.env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    return {"server": os.environ.get("SANDBOX_MSSQL_HOST", "127.0.0.1"),
            "port": int(os.environ.get("SANDBOX_MSSQL_PORT", "1433")),
            "user": os.environ.get("SANDBOX_MSSQL_USER", "sa"),
            "password": os.environ.get("SANDBOX_MSSQL_PASSWORD", ""),
            "login_timeout": 10, "timeout": 120}


def prepare_sql(sql: str) -> str:
    """把規則 SQL 準備成可在沙盒執行的形式。**SQL 本體原樣執行,不轉譯**:
    - INSERT INTO … SELECT、CREATE [OR ALTER] VIEW … AS SELECT → 只取 SELECT
      (驗的是篩選邏輯;報表表不在沙盒 schema 裡,VIEW 在 SQL Server 也必須是批次第一句)
    - @start_date / @end_date → 以 DECLARE 代入固定執行窗
    - 系統日期函式 → 換成窗尾,確保每次執行結果可重現"""
    body = sql.strip().rstrip(";")
    # 檔案開頭通常是註解,判斷語句類型前先跳過,否則 INSERT / VIEW 會被當成一般 SELECT
    head = _LEADING_COMMENTS.sub("", body)
    if _WRAPPED.match(head):
        try:
            import sqlglot
            st = sqlglot.parse_one(body, dialect="tsql")
            is_view = st.key == "create" and str(st.args.get("kind", "")).upper() == "VIEW"
            if (st.key == "insert" or is_view) and st.expression is not None:
                sel = st.expression
                # WITH … INSERT … 的 CTE 掛在 INSERT 上,要一起帶走(sqlglot 新版的欄位名是 with_)
                for key in ("with_", "with"):
                    if st.args.get(key) is not None and sel.args.get(key) is None:
                        sel.set(key, st.args[key])
                        break
                body = sel.sql(dialect="tsql")
        except Exception:
            pass
    body = _SYSDATE.sub(f"CAST('{WIN_END}' AS DATETIME2)", body)
    decl = []
    if re.search(r"@start_date\b", body, re.I):
        decl.append(f"@start_date DATE = '{WIN_START}'")
    if re.search(r"@end_date\b", body, re.I):
        decl.append(f"@end_date DATE = '{WIN_END}'")
    head = "SET NOCOUNT ON; SET DATEFORMAT ymd;\n"
    if decl:
        head += "DECLARE " + ", ".join(decl) + ";\n"
    return head + body


def _drop_stale_sandbox_dbs(admin) -> None:
    """清掉之前異常中斷留下的沙盒資料庫(每個只活幾秒,超過一小時必定是殘留)。"""
    try:
        cur = admin.cursor()
        cur.execute(f"SELECT name FROM sys.databases WHERE name LIKE '{_SANDBOX_DB_PREFIX}%' "
                    "AND create_date < DATEADD(HOUR, -1, SYSDATETIME())")
        for (name,) in cur.fetchall():
            if re.fullmatch(_SANDBOX_DB_PREFIX + r"[0-9a-f]{12}", name):
                cur.execute(f"ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                            f"DROP DATABASE [{name}]")
    except Exception:
        pass


def _label_expectation(conds: list[dict], case: dict) -> dict[str, bool]:
    """這個案例**照它自己的標籤**,被標的那一條應該是真還是假。

    direction="true"  → 被標的那一條成立
    direction="false" → 被標的那一條不成立

    **只驗被標的那一條,不對其他條件下任何判定。**原本的版本要求
    「direction=false 時只有被標的那一條不成立,其餘全部成立」,用意是讓反向案例
    能把失敗歸因到某一條。那個要求對純 AND 的規格成立,對「條件 1/2/3 任一成立」
    這種 OR 結構**在數學上不可能成立**——實測 RETAIL_M1_EVAL:

        C1-1:kiosk_outbound_cnt 介於 2–3
        C3-1:kiosk_outbound_cnt 介於 4–10

    兩者交集是空集合,所以「所有條件同時成立」永遠不可能,於是每一個案例都會
    被判標籤不符。真端點實測:30 個案例 **30 個**被判不符,含 22 個行為完全
    正確的——而 _label_gaps 會把標籤不符轉成覆蓋缺口,等於只要規格是 OR 結構,
    spec_exec 的結果就全盤報廢。100% 誤報。

    「有沒有隔離出那一條」改成記錄(見 execute_cases 的 label_cofail),不判定:
    要判定得先知道哪些條件屬於同一個 OR 分支,而計畫裡沒有這個結構。
    """
    target, direction = case["condition_id"], case["direction"]
    if case.get("kind") in ("base", "variant"):
        # 基準＋變體的形狀下,「其他條件都成立」**在分支內是做得到的**
        # (分支內是純 AND),所以隔離性可以回來判定:
        #   基準(branch B)  → B 的每一條都要成立
        #   變體(壞 T,在 B)→ T 不成立,B 的其他條件都要成立
        # 這正是原本因為 OR 結構互斥而不得不拔掉的那道檢查。
        if case["kind"] == "base":
            # 基準要讓「該分支的條件」**加上全域條件**全部成立——全域條件屬於
            # 每一個分支,少驗它們就等於沒驗到規則真正的前提。
            b_id = str(case.get("branch", "") or "")
            return {c["id"]: True for c in conds
                    if str(c.get("branch", "") or "") in (b_id, GLOBAL_BRANCH)}
        b_id = next((str(c.get("branch", "") or "") for c in conds
                     if c["id"] == target), "")
        if b_id == GLOBAL_BRANCH:
            # 全域條件的變體是從某個分支的基準改出來的,但計畫沒記是哪一個,
            # 所以只驗它自己有沒有被破壞,不對其他條件下判定。
            return {target: False}
        return {c["id"]: (c["id"] != target) for c in conds
                if str(c.get("branch", "") or "") in (b_id, GLOBAL_BRANCH)
                or c["id"] == target}
    return {target: direction == "true"}


def _eval_conditions(cursor, conds: list[dict]) -> dict[str, bool] | None:
    """在目前的交易裡逐條算出判斷式的真假;任何一條算不出來就整個放棄(回 None)。

    放棄而不是部分回報:少算一條就可能把「兩條同時不成立」看成「只有一條不成立」,
    那正是這道檢查要抓的東西,半套的結果比沒有更危險。
    """
    out = {}
    for c in conds:
        q = c.get("check_sql")
        if not q:
            return None
        try:
            cursor.execute(_wrap_check_sql(q))
            row = cursor.fetchone()
        except Exception:
            return None
        if not row:
            return None
        out[c["id"]] = bool(row[0])
    return out


def execute_cases(sql: str, plan: dict) -> dict:
    """角色二:確定性執行(非 LLM)。

    每次呼叫在沙盒建一個專用資料庫(與其他同時進行的審查互不干擾),每個案例在
    獨立交易裡建表、灌資料、執行 MR 的 SQL,比對「是否有輸出列」與 expect_flagged,
    結束一律 rollback(下一個案例從乾淨狀態開始)。最後刪掉整個資料庫。
    回傳 {case_results, mismatches, sql_error, testdata_error, sandbox_error, engine}。"""
    import pymssql
    params = _sandbox_params()
    prepared = prepare_sql(sql)
    results, mismatches = [], []
    out = {"case_results": results, "mismatches": mismatches, "sql_error": None,
           "testdata_error": None, "sandbox_error": None, "engine": None}

    if not params["password"]:
        out["sandbox_error"] = "未設定 SANDBOX_MSSQL_PASSWORD(見 config/sandbox.env.example)"
        return out
    try:
        admin = pymssql.connect(**params, autocommit=True)
    except Exception as e:
        out["sandbox_error"] = f"無法連線執行驗證沙盒 {params['server']}:{params['port']}:{str(e)[:160]}"
        return out

    dbname = _SANDBOX_DB_PREFIX + uuid.uuid4().hex[:12]
    con = None
    try:
        cur = admin.cursor()
        cur.execute("SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(64)), "
                    "CAST(SERVERPROPERTY('Edition') AS NVARCHAR(128))")
        ver, edition = cur.fetchone()
        out["engine"] = f"SQL Server {ver} ({edition})"
        _drop_stale_sandbox_dbs(admin)
        cur.execute(f"CREATE DATABASE [{dbname}]")
        con = pymssql.connect(**params, database=dbname, autocommit=False)

        for case in plan["cases"]:
            c = con.cursor()
            try:
                try:
                    for ddl in plan["schema_ddl"]:
                        c.execute(ddl)
                    for row in case["rows"]:
                        table, vals = row[0], row[1:]
                        ph = ",".join(["%s"] * len(vals))
                        c.execute(f"INSERT INTO {table} VALUES ({ph})", tuple(vals))
                except Exception as e:   # DDL / 灌資料失敗 = 測資側的錯,不是 SQL 的錯
                    out["testdata_error"] = f"建表/灌資料失敗於 {case['case_id']}:{str(e)[:200]}"
                    return out
                try:
                    c.execute(prepared)
                    rows = c.fetchall() if c.description else []
                except Exception as e:   # SQL 本身跑不起來(語法/欄位/方言)→ SQL 側的錯
                    out["sql_error"] = str(e)[:300]
                    return out
                # 標籤一致性(#23 第 3 點):在**同一個交易、同一份資料**上逐條算出
                # 每個條件的真假,確認這個案例真的在測它自己宣稱的那一條。
                # 覆蓋檢查原本完全相信模型貼的標籤,所以「標籤對、資料卻沒測到」
                # (反向案例同時讓兩條不成立)看起來跟正確的案例一模一樣。
                # 用 .get 而不是 plan["conditions"]:execute_cases 不只收
                # generate_cases() 的結果,eval/preflight.py 的沙盒探針與
                # eval/check_frozen_testdata.py 都自己組 plan。少了這個欄位會
                # KeyError,而所有例外都被包成 sandbox_error → preflight 回報
                # 「沙盒執行異常」、整輪跑批中止在「環境壞了」,實際上是我們的程式。
                # 公司端點上的起飛前檢查第一次就撞到這個。
                #
                # 沒有條件可比時一律不下標籤結論(傳 None):空的 conds 會讓
                # _eval_conditions 回 {},那樣 label_ok 會變成 True——一個
                # 「標籤驗過了」的假訊號,比沒有這一欄更糟。
                conds = plan.get("conditions") or []
                actual_conds = _eval_conditions(c, conds) if conds else None
            finally:
                try:
                    con.rollback()
                except Exception:
                    pass
            actual = len(rows) > 0
            r = {"case_id": case["case_id"], "condition_id": case["condition_id"],
                 "direction": case["direction"], "note": case.get("note", ""),
                 "expect_flagged": case["expect_flagged"], "actual_flagged": actual,
                 "actual_rows": [[str(v) for v in row] for row in rows[:5]],
                 "ok": actual == case["expect_flagged"]}
            if actual_conds is not None:
                want = _label_expectation(conds, case)
                wrong = sorted(k for k, v in want.items() if actual_conds.get(k) is not v)
                r["label_ok"] = not wrong
                if wrong:
                    # 不改 ok(那是「行為符不符合預期」),另外記一欄:
                    # 這個案例的資料湊不出它宣稱要測的那一條。實測抓到的就是這個:
                    # 模型的 note 寫「io_ratio = 95k/90k = 1.06」,資料列裡的指定
                    # 出帳卻只有 35,000 → 實際 0.39,落在規格要求的區間外。
                    r["label_mismatch"] = wrong
                # 其他分支有沒有整個成立——**用程式算,不靠模型遵守 prompt**。
                # 變體:別支成立 → 規則會命中 → 「不命中」的預期是錯的。
                # 基準:別支也成立 → 命中可能是別支造成的,證明不了「這一支自己會命中」。
                if case.get("kind") in ("base", "variant"):
                    others = _other_branch_satisfied(
                        conds, actual_conds, _case_branch(conds, case))
                    if others:
                        r["other_branch_satisfied"] = others
                        r["label_ok"] = False
                        r.setdefault("label_mismatch", []).extend(
                            f"分支{b} 整個成立" for b in others)
                # 其他條件只記錄、不判定:同時有幾條不成立,對 OR 結構的規格是
                # 正常的(不同分支本來就互斥),對純 AND 的規格才是雜訊的來源。
                cofail = sorted(k for k, v in actual_conds.items()
                                if v is False and k != case["condition_id"])
                if cofail:
                    r["label_cofail"] = cofail
            results.append(r)
            if not r["ok"]:
                mismatches.append({**r, "rows": case["rows"]})
        return out
    except Exception as e:
        out["sandbox_error"] = f"沙盒執行異常:{str(e)[:200]}"
        return out
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        try:
            admin.cursor().execute(f"ALTER DATABASE [{dbname}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                                   f"DROP DATABASE [{dbname}]")
        except Exception:
            pass
        admin.close()


# ------------------------------------------------------------------ 3. 仲裁
ARBITER_SYSTEM = """你是獨立仲裁者。一條規則 SQL 在一個測資案例上的實際行為與
測資預期不符,你要判定:是**測資錯**(測資生成 agent 誤讀規格/造錯資料/預期標錯)
還是 **SQL 錯**(實作與規格不符)。

# 原則(客觀稽核)
- **spec 是唯一真值**:只以 spec 條文判斷,不猜、不腦補規格沒寫的東西。
- SQL 在 MS SQL(SQL Server)上執行,判斷「SQL 為什麼給出這個結果」時一律依 T-SQL 語意。
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


def _other_branch_satisfied(conds: list[dict], actual: dict[str, bool],
                            own_branch: str) -> list[str]:
    """除了自己這一支,還有哪些分支**整個成立**(全域條件 `*` 不算一支)。

    為什麼要算這個(#47 review 第 1 點):
      * **變體**的預期是「規則不命中」,前提是沒有別的分支成立。但「剛好跨過門檻
        一格」常常會落進相鄰分支的區間——實測 `C1-1` 是「次數介於 2-3」,往上跨到 4,
        而 `C3-1` 正好是「4-10」→ 規則反而命中,預期就錯了(造成 5 個預期錯誤)。
      * **基準**的預期是「規則命中」,而我們想證明的是「**這一支自己**就會命中」。
        如果別的分支也整個成立,那命中可能是別支造成的,基準就沒有證明到它該證明的事。

    原本只寫在 prompt 裡要模型避開,但那是靠它遵守。每個條件都有 `check_sql`,
    所以可以直接算。
    """
    by_branch: dict[str, list[str]] = {}
    for c in conds:
        b_id = str(c.get("branch", "") or "")
        if b_id in (GLOBAL_BRANCH, own_branch):
            continue
        by_branch.setdefault(b_id, []).append(c["id"])
    out = []
    for b_id, ids in sorted(by_branch.items()):
        # `is True` 而不是看真假值:算不出來的條件(沒有 check_sql → 這裡是 None)
        # **不算成立**,所以那一支就不會被判定「整個成立」。寧可不報,不要報錯的
        # ——把沒驗到的說成驗過了比漏報更糟。
        if ids and all(actual.get(i) is True for i in ids):
            out.append(b_id)
    return out


def _case_branch(conds: list[dict], case: dict) -> str:
    """這個案例屬於哪一支(基準看 branch,變體看它要壞的那個條件所屬的分支)。"""
    if case.get("kind") == "base":
        return str(case.get("branch", "") or "")
    target = case.get("condition_id")
    return next((str(c.get("branch", "") or "") for c in conds
                 if c["id"] == target), "")


def _label_gaps(case_results: list[dict]) -> list[str]:
    """標籤與資料對不上的案例 = 該條件該向**沒有真的被驗到**(#23 第 3 點)。

    覆蓋檢查原本完全相信模型自己貼的條件編號與方向,所以「標籤對、資料卻沒測到」
    看起來跟正確的案例一模一樣。典型的壞法是反向案例同時讓兩條不成立——它的行為
    (不命中)完全符合預期,`ok` 是 True,但它證明不了是哪一條造成的。

    所以這裡**不動 `ok`**(那是「行為符不符合預期」),另外列入覆蓋缺口:
    那一向沒有被有效驗證,不可據以自動放行。與仲裁剔除、執行中止是同一個原則。
    """
    gaps = []
    for r in case_results:
        if r.get("label_ok") is not False:
            continue
        others = r.get("other_branch_satisfied")
        if others:
            # 這一種要分開講:不是「資料湊不出宣稱的那一條」,而是「同時讓別的分支
            # 整個成立」——變體的預期會因此錯掉,基準則證明不了是自己這一支命中。
            gaps.append(
                f"案例 {r['case_id']}({r.get('kind', '?')})同時讓分支 "
                f"{'、'.join(others)} 整個成立:"
                f"{'變體的「不命中」預期不成立' if r.get('kind') == 'variant' else '無法證明是這一支自己命中'},"
                f"該案例未經有效驗證")
            continue
        gaps.append(
            f"案例 {r['case_id']}(條件 {r['condition_id']} {r['direction']} 向)"
            f"的資料與標籤不符:{'、'.join(r.get('label_mismatch') or [])} 的真假"
            f"與這個標籤應有的不同,該向未經有效驗證")
    return gaps


def _unrun_gaps(plan: dict, case_results: list[dict]) -> list[str]:
    """執行中止後,還沒跑到的案例一律列入覆蓋缺口。

    `execute_cases` 的 per-case 迴圈裡,測資建不起來或 SQL 跑不起來會直接
    `return out`,放棄剩下的案例——這個行為本身是合理的(SQL 真的壞掉時,
    跑完 46 個案例只會得到 46 個相同錯誤)。**不合理的是報告沒有交代這件事**:
    `coverage_gaps` 維持原樣,報告看起來像「跑了 N 個案例、零缺口」,
    但實際上計畫裡多數案例根本沒執行。

    仲裁剔除案例時已經有對應處理(見下方「被剔除的案例 = 該條件該向未經驗證」),
    這裡補的是同一個道理的另一半:**沒跑到 = 沒驗到**,兩者都必須反映在
    coverage_gaps 上,人才讀得出「這次到底驗了多少」。

    決策層本來就會因為 major finding 擋下自動放行,所以這不是放行漏洞;
    修的是報告的誠實度——而那正是人用來判斷「這次驗證可不可信」的依據。
    """
    ran = {c.get("case_id") for c in case_results}
    missing = [c for c in plan.get("cases", []) if c.get("case_id") not in ran]
    return [f"案例 {c.get('case_id')}(條件 {c.get('condition_id')} "
            f"{c.get('direction')} 向)因執行中止而未驗證" for c in missing]


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
        # getattr:呼叫端可能傳測試用的替身 cfg(沒有 dbt 欄位),視為未開啟
        spec_code, spec_text = await find_spec(
            hub, mr, by_path=(getattr(cfg, "dbt", None) or {}).get("enabled") is True)

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

    from .dbt_render import is_dbt_template   # 延遲匯入,與上面 find_spec 的作法一致
    if is_dbt_template(sql):
        # 樣板原樣送進沙盒只會得到語法錯誤,接著回報成「SQL 無法在測資上執行、請修正
        # SQL」——錯誤地指控開發者的程式壞了,還白白燒掉測資生成的 LLM 呼叫。
        # 展開後再執行需要把 ref()/source() 對到沙盒每次建立的資料庫與測資表名,
        # 尚未支援(docs/09 已知限制),所以直接如實回報,不自動放行。
        # 保守的一側:純 SQL 的字串常量裡剛好出現 {{ / {% 也會被擋下 → needs_human。
        return {"passed": False, "spec_code": spec_code, "conditions": [],
                "coverage_gaps": [], "case_results": [], "dropped_cases": [],
                "dbt_template": True,
                "findings": [_finding(
                    path, "major", "執行驗證無法進行:dbt 樣板尚未支援執行驗證",
                    "此檔含 dbt / Jinja 樣板標記,原樣送進 MS SQL 沙盒只會得到語法錯誤,"
                    "不代表程式有誤。展開後的 SQL 要在沙盒執行,需要把 ref()/source() "
                    "對應到沙盒的資料庫與測資表名,目前尚未支援。未經執行驗證,"
                    "本 MR 不得自動放行。",
                    "請人工依核定規格核對實作邏輯。")]}

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
    if ex["sandbox_error"]:
        # 沙盒不可用 ≠ 驗證通過:照樣不得自動放行,並講清楚是環境問題而非程式問題
        findings.append(_finding(path, "major", "執行驗證無法完成(沙盒不可用)",
                                 f"{ex['sandbox_error']}。這是驗證環境的問題,不代表程式有錯;"
                                 "但未經執行驗證,本 MR 不得自動放行。",
                                 "啟動沙盒(scripts/sandbox.sh start)後重新觸發審查。"))
        return {"passed": False, "spec_code": spec_code, "engine": ex["engine"],
                "conditions": plan["conditions"], "coverage_gaps": coverage_gaps,
                "case_results": [], "dropped_cases": dropped_cases, "findings": findings}
    if ex["testdata_error"]:
        findings.append(_finding(path, "major", "執行驗證無法完成(測資無法建置)",
                                 f"測資生成 agent 產出的 schema/資料無法建置:"
                                 f"{ex['testdata_error']}。需人工執行驗證。"))
        return {"passed": False, "spec_code": spec_code, "engine": ex["engine"],
                "conditions": plan["conditions"],
                "coverage_gaps": coverage_gaps + _unrun_gaps(plan, ex["case_results"]),
                "case_results": ex["case_results"], "dropped_cases": dropped_cases,
                "findings": findings}
    if ex["sql_error"]:
        findings.append(_finding(path, "major", "執行驗證:SQL 無法在測資上執行",
                                 f"執行錯誤(語法/欄位):{ex['sql_error']}",
                                 "修正 SQL 使其可依規格的資料表定義在 MS SQL 上執行。"))
        return {"passed": False, "spec_code": spec_code, "engine": ex["engine"],
                "conditions": plan["conditions"],
                "coverage_gaps": coverage_gaps + _unrun_gaps(plan, ex["case_results"]),
                "case_results": ex["case_results"], "dropped_cases": dropped_cases,
                "findings": findings}

    # 標籤一致性的缺口要在仲裁之前就併進去:它講的是「這個案例有沒有驗到它宣稱的
    # 那一條」,跟仲裁要判的「測資錯還是 SQL 錯」是不同的問題,兩者可以同時成立。
    coverage_gaps = coverage_gaps + _label_gaps(ex["case_results"])

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
                                 f"執行驗證:{len(effective)} 個生成測試案例皆符合預期",
                                 f"測資由模型依規格 {spec_code} 產生,涵蓋每個條件成立與不成立"
                                 f"兩種情況,以及門檻邊界值;再於沙盒實際編譯執行。"
                                 f"執行本身是確定性的,但測資與驗證出自模型,AI 可能有幻覺。"))
    return {"passed": passed, "spec_code": spec_code, "engine": ex["engine"],
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
