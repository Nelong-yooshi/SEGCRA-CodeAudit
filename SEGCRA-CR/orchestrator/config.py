import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PKG_ROOT = Path(__file__).resolve().parent.parent

# dbt 展開用的資料庫名稱:約定的固定假名(#14 review)。只用來把 ref()/source() 拼成
# 完整表名給規則層掃描,不連線;固定不變,golden set 的標準答案才不會跟著飄。
# 改名時要同步更新 config/models.yaml 的註解與 docs/09(有測試確保三處一致)。
DBT_DATABASE_PLACEHOLDER = "DBT_PLACEHOLDER"


@dataclass
class ModelProfile:
    model: str
    num_ctx: int
    temperature: float
    max_output_tokens: int
    seed: int | None = None
    # 思考的 token 也算在 max_output_tokens 裡,所以這個欄位實際上是在分配輸出預算:
    # 實測 R-140 的測資生成,思考 5,054 字、JSON 本體 5,647 字——**將近一半的輸出
    # 上限花在我們看不到也用不到的推理上**。None = 不送這個參數,用端點的預設值
    # (會思考);設成 "none" 可以把整個上限讓給真正要的輸出。
    #
    # 不是所有端點都吃這個參數,所以預設不送:送了不支援的參數可能被整個請求退回,
    # 那會比「多花一點 token」嚴重得多。
    reasoning_effort: str | None = None


def _env_float(name: str, lo: float, hi: float) -> float:
    """讀一個數值型的環境變數覆蓋,壞值要**當場講清楚**。

    這兩個變數是手打在跑批指令前面的(`LLM_TEMPERATURE=0 LLM_SEED=42 ...`),
    打錯是常態。Python 內建的訊息是 `could not convert string to float: \'O\'`——
    它不會說是哪個環境變數、也不會說該填什麼,而這件事發生時人多半正在盯別的東西。
    更糟的是超出範圍的值(例如 temperature 打成 10)根本不會報錯,端點會自己夾住或
    照收,於是整輪數字是在一個沒人知道的設定下量出來的。
    """
    raw = os.environ[name]
    try:
        v = float(raw)
    except ValueError:
        raise ValueError(
            f"環境變數 {name} 的值不是數字:{raw!r}。"
            f"請給 {lo} 到 {hi} 之間的數,例如 {name}=0") from None
    if not (lo <= v <= hi):
        raise ValueError(
            f"環境變數 {name}={v} 超出合理範圍({lo}~{hi})。"
            f"這種值端點可能自己夾住也可能照收,兩種都會讓這輪的數字量在"
            f"沒人知道的設定下,所以這裡直接擋下")
    return v


# 端點接受的檔位。"none" 是「完全不要思考」,其餘是強度。
# 空字串另外處理:代表「取消覆蓋,回到不送這個參數」,與「送 none」不同——
# 前者用端點預設(會思考),後者明確要求不思考。
_EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high")


def _env_effort(name: str) -> str | None:
    """讀 reasoning_effort 的環境變數覆蓋。

    壞值要當場擋下來,理由跟 _env_float 一樣:這個變數是手打在跑批指令前面的,
    而打錯的後果很安靜——端點收到不認得的值可能整個請求退回(那還好,會看到錯誤),
    也可能默默忽略,於是你以為關掉了思考、實際上沒有,整輪的比較就白做了。
    """
    raw = os.environ[name].strip().lower()
    if not raw:
        return None
    if raw not in _EFFORT_LEVELS:
        raise ValueError(
            f"環境變數 {name} 的值不在可用檔位裡:{os.environ[name]!r}。"
            f"可用:{'、'.join(_EFFORT_LEVELS)};"
            f"或設成空字串代表不送這個參數(用端點預設)")
    return raw


def _env_seed(name: str) -> int | None:
    """讀 seed 的環境變數覆蓋。**空字串是有意義的值**:代表取消固定 seed。

    空字串與「沒設定這個變數」不同:沒設定 → 用 profile 的值;
    設成空字串 → 明確要求恢復隨機(出能力報告時用)。
    """
    raw = os.environ[name].strip()
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        raise ValueError(
            f"環境變數 {name} 的值不是整數:{raw!r}。"
            f"請給一個整數(例如 {name}=42),或設成空字串代表取消固定 seed") from None
    if v < 0:
        raise ValueError(f"環境變數 {name}={v} 不可為負數")
    return v


@dataclass
class Config:
    endpoint: str
    api_key: str
    default_profile: str
    profiles: dict
    roles: dict
    budget: dict
    policy: dict
    # 預設關閉(field 預設,不是只在 load_config() 補):任何直接建構 Config(...)
    # 而沒指定 dbt 的呼叫端(測試、其他腳本)一律落在「未接線」的安全狀態,
    # 不會因為漏寫這個欄位就意外開啟。
    dbt: dict = field(default_factory=lambda: {"enabled": False, "database": ""})

    def profile(self, name: str | None = None) -> ModelProfile:
        p = self.profiles[name or self.default_profile]
        prof = ModelProfile(**p)
        # 出能力報告(多次取樣看分布)要暫時關掉固定的 temperature/seed,
        # 不為了這件事另開 profile 或動 yaml —— 環境變數覆蓋就好。
        # LLM_SEED 設成空字串代表「取消固定 seed,恢復隨機」。
        if "LLM_TEMPERATURE" in os.environ:
            prof.temperature = _env_float("LLM_TEMPERATURE", 0.0, 2.0)
        if "LLM_SEED" in os.environ:
            prof.seed = _env_seed("LLM_SEED")
        # 量「關掉思考會不會讓覆蓋度變好」時,要用**同一份程式碼**跑開與關兩次,
        # 所以得有環境變數可以覆蓋,不能只靠改 yaml(改了 yaml 兩次跑的就不是同一版)。
        if "LLM_REASONING_EFFORT" in os.environ:
            prof.reasoning_effort = _env_effort("LLM_REASONING_EFFORT")
        return prof

    def role_profile(self, role: str) -> ModelProfile:
        """依角色(testgen / arbiter)取模型 profile;未設定則用預設 profile。"""
        return self.profile(self.roles.get(role) or self.default_profile)


def _load_dbt_section(raw: dict) -> dict:
    """dbt 展開接線的設定。**預設關閉**——合併後現有審查流程與評測結果不受影響,
    確認後才開(見 docs/09-dbt展開與反查.md)。

    `enabled` 故意只接受真正的布林值,不接受任何字串:YAML 裡 `enabled: "false"`
    是一個非空字串,Python 的 `bool("false")` 是 True——這種筆誤會讓「以為關著、
    其實開著」的功能默默上線,對審查流程是看不見的行為變化,所以當場擋下。

    `database` 由環境變數 SEGCRA_DBT_DATABASE 提供(設定檔裡的值只當後備):
    **repo 是公開的,正式環境的資料庫名不可寫進任何進版控的檔案**,與沙盒帳密
    (config/sandbox.env)同樣只存在部署機上。名稱會被拼進 SQL 識別字,載入時就
    過白名單——否則填錯只會讓每一份 model 在審查時才各自展開失敗,不易察覺。
    """
    section = raw.get("dbt") or {}
    if not isinstance(section, dict):
        raise ValueError("設定檔的 dbt 區塊必須是物件(key: value),"
                         f"目前是 {type(section).__name__}")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(
            f"設定檔 dbt.enabled 必須是布林值 true/false,目前是 {enabled!r}"
            f"({type(enabled).__name__})。字串 \"false\" 在 Python 裡會被當成"
            f"真值,為避免誤開,一律不接受字串。")
    database = os.environ.get("SEGCRA_DBT_DATABASE", section.get("database", ""))
    if not isinstance(database, str):
        raise ValueError(f"設定檔 dbt.database 必須是字串,目前是 {database!r}")
    if database:
        from .dbt_render import _IDENT   # 與展開器用同一份白名單,兩邊不會各寫一套
        if not _IDENT.fullmatch(database):
            raise ValueError(
                "dbt 的資料庫名稱只接受英數與底線(會被拼進 SQL 識別字);"
                "請檢查環境變數 SEGCRA_DBT_DATABASE。")
    if enabled and database != DBT_DATABASE_PLACEHOLDER:
        # 漏設或拼錯時展開不會報錯,只會產出錯的表名,報告看起來卻完全正常(#14
        # review)——開啟時就擋下,不讓審查帶著錯的設定啟動。訊息不回顯實際的值:
        # 填進來的可能正是不該外流的正式資料庫名。
        raise ValueError(
            f"dbt.enabled 開啟時,環境變數 SEGCRA_DBT_DATABASE 必須設為約定的假名 "
            f"{DBT_DATABASE_PLACEHOLDER}(見 docs/09-dbt展開與反查.md);目前未設定或不符。")
    return {"enabled": enabled, "database": database}


def load_config(path: Path | None = None) -> Config:
    raw = yaml.safe_load((path or PKG_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    # endpoint 可用環境變數覆蓋(如 Ollama 掛在遠端或 tunnel 上)
    endpoint = os.environ.get("OLLAMA_URL", raw.get("endpoint", "http://localhost:11434/v1"))
    return Config(
        endpoint=endpoint, api_key=raw.get("api_key", "ollama"),
        default_profile=raw["default_profile"], profiles=raw["profiles"],
        roles=raw.get("roles", {}), budget=raw["budget"],
        policy=raw.get("policy", {}), dbt=_load_dbt_section(raw),
    )


def estimate_tokens(text: str) -> int:
    """粗估 token 數(中文 ~1 token/1.5字、英文 ~1 token/4字,取保守值 len/3)。"""
    return max(1, len(text) // 3)
