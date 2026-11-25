import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .file_perms import PermissionProblem, check_file as _check_file, check_path as _check_path
from .file_perms import permission_problem as _permission_problem  # noqa: F401  測試直接呼叫

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


# ─── 讀環境變數的規則(這些 helper 都在這裡) ───
# - 秒數、開關(env_seconds、env_bool):讀不懂、設了但是空的,**印一行警告(寫出變數名)並用程式的
#   預設值**。讀它們的是 webhook 伺服器在 import 的時候,填錯一個字就起不來的話所有 MR 都收不到審查;
#   退回的是程式的預設值,而等太短或沒送 timeout 時模型呼叫失敗也是交人工,不會因此放行。
# - 數值的覆蓋(_env_float、_env_seed、_env_effort):手打在跑批指令前面的,讀不懂當場擋下。


def env_seconds(name: str, default: float) -> float:
    """讀「秒數」型的環境變數覆蓋:要是**有限的正數**(可以有小數)。讀不懂、`inf`、`nan`、0 或負數、
    設了但是空的,都退回預設值並印一行警告(寫出變數名)。沒設才安靜用預設值。

    設了但是空的也說:多半是部署腳本沒把值帶進來。`inf` 要擋:有的值會放進請求內容
    (LLM_SEND_TIMEOUT),`inf` 連 JSON 都轉不出來。整數照原樣回整數。"""
    import math
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value > 0):
        print(f"[config] 警告:環境變數 {name} 的值不是有限的正數秒數({raw!r}),"
              f"改用預設 {default:g} 秒", flush=True)
        return default
    return int(value) if value.is_integer() else value


_FLAG_ON = ("1", "true", "yes", "on")
_FLAG_OFF = ("0", "false", "no", "off")


def env_bool(name: str, unset: str) -> bool:
    """開關型的環境變數。沒設時看 `unset`(寫在呼叫端,docs/10 的預設值欄對照得到);
    `1/true/yes/on` 開、`0/false/no/off` 關;**其他值(打錯字、設了但是空的)印一行警告並當成關**:
    打錯一個字就跟沒設一樣、又沒有任何輸出的話,症狀看起來跟這個設定無關。當成關 = 跟沒有這個設定
    一樣,不是改走別的路。"""
    raw = os.environ.get(name)
    if raw is None:
        return unset.strip().lower() in _FLAG_ON
    value = raw.strip().lower()
    if value in _FLAG_ON:
        return True
    if value not in _FLAG_OFF:
        print(f"[config] 警告:環境變數 {name} 的值讀不懂({raw!r}),當成沒開;"
              f"要開請設 1、true、yes 或 on", flush=True)
    return False


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


class ConfigError(ValueError):
    """設定不安全或不完整:停下來交給人處理,不退回別的做法(見 _env_text、check_config_file)。"""


def _env_text(name: str) -> str | None:
    """文字型的環境變數:沒設回 None;**設了但去掉前後空白後是空的,直接報錯**。

    設了變數就表示要用它;變數是空的,多半是部署腳本沒把值帶進來(例如 `read` 時直接按了
    Enter)。這時改用設定檔或預設值,等於用一份沒有人指定的設定繼續跑,而症狀(端點回 401、
    連線錯誤)看不出是哪個變數。前後的空白去掉。"""
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        raise ConfigError(f"環境變數 {name} 有設定但是空的;要用就給值,不用就把這個變數拿掉")
    return value


# ─── 讀環境變數的規則(這些 helper 都在這裡) ───
# - 文字、整數、路徑(_env_text、env_int、env_path):**設了但是空的就停**(ConfigError,寫出變數名)。
#   空的時退回預設值,等於用沒人指定的端點、資料庫、目錄繼續跑。
# - 秒數、開關(env_seconds 等):讀不懂退回程式的預設值並印一行警告;等太短或沒送 timeout 時
#   模型呼叫失敗也是交人工,不會因此放行。


def env_text(name: str, default: str) -> str:
    """文字型的環境變數,沒設時用 `default`;設了但是空的就停(見 _env_text)。預設值寫在呼叫端,
    設定參數的檢查(env_registry)才對得到文件。"""
    value = _env_text(name)
    return default if value is None else value


def env_int(name: str, default: int, *, lo: int = 1, hi: int | None = None) -> int:
    """整數型的環境變數:沒設用預設;設了但是空的、不是整數、超出範圍就停,訊息寫出變數名。"""
    raw = _env_text(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"環境變數 {name} 的值不是整數({raw[:40]!r})") from None
    if value < lo or (hi is not None and value > hi):
        limit = f"{lo} 到 {hi}" if hi is not None else f"至少 {lo}"
        raise ConfigError(f"環境變數 {name}={value} 超出範圍({limit})")
    return value


def env_path(name: str, default) -> Path:
    """路徑型的環境變數:沒設用預設(repo 裡的目錄)。有設時先展開 `~`;設了但是空的、不是絕對路徑
    就停(`Path("")` 是啟動目錄,相對路徑跟著啟動目錄走,讀寫到哪裡都是意外)。指到的檔案或目錄
    與一路往上的每一層,群組或其他人可寫就停(file_perms.check_path):知識庫裡的 binding 會壓掉
    finding,報告含 MR 的程式碼,誰改得到這些目錄不能是意外。`/` 開頭、`..` 不擋:能設環境變數的人
    本來就能寫任何路徑。"""
    raw = _env_text(name)
    if raw is None:
        return Path(default)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError(f"環境變數 {name} 要是絕對路徑(目前是相對路徑,會跟著啟動目錄走)")
    try:
        _check_path(path)
    except PermissionProblem as e:
        raise ConfigError(f"環境變數 {name} 指到的位置:{e}") from None
    return path


# 模型設定檔裡 api_key 唯一接受的值(或不寫):Ollama 不驗證,這只是佔位
_API_KEY_PLACEHOLDER = "ollama"


def check_config_file(path: Path, *, secret: bool) -> None:
    """讀設定檔前檢查權限,太寬就停(ConfigError)。規則在 file_perms.py,只有那一份:檔案本身、
    實際的檔案所在的目錄一路往上到 `/`、路徑上的連結所在的目錄;只看權限位元,不看擁有者。

    - `secret=True`(gitlab.env、sandbox.env:裡面是密碼):群組或其他人**可讀或可寫**就停
    - `secret=False`(模型設定檔:不放金鑰,但別人改得到的話,端點換掉 → 金鑰與每個 MR 的
      程式碼都送到他指定的地方;或把 policy 改寬):群組或其他人**可寫**就停"""
    try:
        _check_file(path, secret=secret)
    except PermissionProblem as e:
        raise ConfigError(str(e)) from None


@dataclass
class Config:
    # 都不放進 repr(str 也是用 repr):Config 被印出來、寫進例外訊息、pytest 斷言失敗印出來時,
    # 金鑰與端點都不跟著出去 —— 端點網址也可能帶通行碼(`gpu run` 設的 OLLAMA_URL、
    # 閘道常用的 http://user:pass@host/v1)
    endpoint: str = field(repr=False)
    api_key: str = field(repr=False)
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
    # 設了但是空的一樣停下來(見 _env_text):空的時改用設定檔的值,等於用沒人指定的設定跑
    env_database = _env_text("SEGCRA_DBT_DATABASE")
    database = env_database if env_database is not None else section.get("database", "")
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
    # dbt 專案在 repo 裡的目錄(dbt_project.yml 所在;空字串 = repo 根目錄)。會被接進
    # GitLab 打包下載的路徑,載入時就檢查,不等到審查時每個檔案各自失敗
    from .dbt_project import DbtProjectError, check_project_dir
    try:
        project_dir = check_project_dir(section.get("project_dir", ""))
    except DbtProjectError as e:
        raise ValueError(f"{e};只接受 repo 內的相對目錄(英數與 _ . -,以 / 分段)") from None
    # 部署端的 dbt-sqlserver 在專案沒寫 dbt_sqlserver_use_default_schema_concat 時,自訂
    # schema 要不要接在 target 的 schema 後面(這個預設在不同版本相反,見 docs/09)。
    # 只接受布林或不設:跟 enabled 一樣,字串 "false" 會被當成真值
    concat = section.get("sqlserver_schema_concat")
    if concat is not None and not isinstance(concat, bool):
        raise ValueError(
            f"設定檔 dbt.sqlserver_schema_concat 必須是布林值 true/false 或不設,目前是 {concat!r}"
            f"({type(concat).__name__})")
    # 只有設了才帶這些鍵(讀的一方用 .get):沒設時回傳的內容與接線前相同
    return {"enabled": enabled, "database": database,
            **({"project_dir": project_dir} if project_dir else {}),
            **({"sqlserver_schema_concat": concat} if concat is not None else {})}


def _yaml_api_key(raw: dict) -> str:
    """設定檔的 api_key 只接受不寫或佔位的 `ollama`,其他值一律停下來。

    那個檔案會進版控與離線包。以前照收:部署端忘了把金鑰拿掉時,服務照常運作、沒有任何提示,
    金鑰就一直留在檔案裡。訊息不寫出金鑰本身。"""
    value = raw.get("api_key", _API_KEY_PLACEHOLDER)
    if value != _API_KEY_PLACEHOLDER:
        raise ConfigError(
            "模型設定檔的 api_key 只能不寫或寫 ollama(佔位):金鑰改用環境變數 LLM_API_KEY 給,"
            "設定檔會進版控與離線包。改完把寫過金鑰的備份檔一起刪掉;"
            "金鑰曾經寫在檔案裡的話,換一把新的")
    return value


_DEFAULT_ENDPOINT = "http://localhost:11434/v1"


def _check_endpoint(value, source: str) -> str:
    """最後要用的模型端點:字串、去掉前後空白後不是空的、http 或 https、有主機。不符就停。

    設定檔的 `endpoint:` 空著時 YAML 讀到的是 None,openai 套件收到 `base_url=None` 會改用
    `OPENAI_BASE_URL`,沒有的話送到 api.openai.com —— MR 的程式碼和 LLM_API_KEY 一起送到外部。
    沒有 `http://` 的位址也不自動補:補錯了一樣是送到沒人指定的地方。訊息寫出來源,不寫出值
    (網址可能帶通行碼)。"""
    from urllib.parse import urlsplit
    text = value.strip() if isinstance(value, str) else ""
    try:
        parts = urlsplit(text)
        ok = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        ok = False
    if not ok:
        raise ConfigError(
            f"模型端點({source})不是有效的網址:要是 http:// 或 https:// 開頭、有主機名稱的位址"
            "(例如 http://localhost:11434/v1)。值不寫出來(可能帶通行碼)")
    return text


# 其他會讀的設定檔(有的話):密碼類,群組與其他人不能讀寫。load_config 是服務啟動時就會
# 呼叫的,在這裡一起檢查,權限不對時服務起不來,而不是等到第一份審查用到才失敗
_SECRET_FILES = ("gitlab.env", "sandbox.env")


def load_config(path: Path | None = None) -> Config:
    path = path or PKG_ROOT / "config" / "models.yaml"
    check_config_file(path, secret=False)
    for name in _SECRET_FILES:
        check_config_file(PKG_ROOT / "config" / name, secret=True)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    # endpoint 可用環境變數覆蓋(如 Ollama 掛在遠端或 tunnel 上;OpenAI 相容的端點都可以)
    env_endpoint = _env_text("OLLAMA_URL")
    if env_endpoint is not None:
        endpoint, source = env_endpoint, "環境變數 OLLAMA_URL"
    else:
        endpoint = raw["endpoint"] if "endpoint" in raw else _DEFAULT_ENDPOINT
        source = f"模型設定檔 {path} 的 endpoint"
    endpoint = _check_endpoint(endpoint, source)
    # 端點要驗證時,金鑰用環境變數給,不寫進設定檔(會進版控與離線包;見 _yaml_api_key)
    yaml_key = _yaml_api_key(raw)
    api_key = _env_text("LLM_API_KEY") or yaml_key
    return Config(
        endpoint=endpoint, api_key=api_key,
        default_profile=raw["default_profile"], profiles=raw["profiles"],
        roles=raw.get("roles", {}), budget=raw["budget"],
        policy=raw.get("policy", {}), dbt=_load_dbt_section(raw),
    )


def estimate_tokens(text: str) -> int:
    """粗估 token 數(中文 ~1 token/1.5字、英文 ~1 token/4字,取保守值 len/3)。"""
    return max(1, len(text) // 3)


def token_units(ch: str) -> int:
    """estimate_tokens_upper 的計算單位(1/3 token):一般字元 1;中日韓字、注音、韓文與全形符號 3
    (每字至少 1 token);BMP 以外的字元(emoji、罕用漢字)6。"""
    o = ord(ch)
    if o > 0xFFFF:
        return 6
    if (0x1100 <= o <= 0x11FF or 0x2E80 <= o <= 0x9FFF or 0xA960 <= o <= 0xA97F
            or 0xAC00 <= o <= 0xD7FF or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE4F
            or 0xFF00 <= o <= 0xFFEF):
        return 3
    return 1


def estimate_tokens_upper(text: str) -> int:
    """偏高的 token 估計:給「超過就會被端點靜靜截掉」的地方用,寧可高估。

    estimate_tokens 是 len/3,中文一個字常常就是一個以上的 token,會低估好幾倍;這裡依字元種類算
    (見 token_units)。只用在需要上限的地方,不取代 estimate_tokens(主審查的預算還在用它)。"""
    units = sum(token_units(ch) for ch in text)
    return -(-units // 3)
