"""模型端點的金鑰與設定的安全 — 不呼叫 LLM。

原則:**安全的做法失敗時停下來交給人,不退回危險的做法**。

  1. 金鑰只能用環境變數 `LLM_API_KEY` 給;設定檔的 `api_key` 只接受不寫或佔位的 `ollama`,
     其他值載入設定就報錯(設定檔會進版控與離線包)
  2. 環境變數設了但是空的(`OLLAMA_URL`、`LLM_API_KEY`、`SEGCRA_DBT_DATABASE`):停下來說是
     哪個變數,不改用設定檔或預設值
  3. `Config` 印出來時,金鑰與端點都不帶(端點網址也可能帶通行碼)
  4. 模型或工具的錯誤原文不進報告、MR 留言與模型對話(只留例外型別與狀態碼)
  5. 讀設定檔前檢查權限,太寬就停(密碼檔 600、模型設定檔不能被別人改、目錄不能被別人改)
"""
import asyncio
import json
import os

import httpx
import openai
import pytest

from orchestrator import agent, spec_exec
from orchestrator.config import (PKG_ROOT, Config, ConfigError, _permission_problem,
                                 check_config_file, load_config)
from orchestrator.error_text import error_label
from tests.test_review_truncation import CLEAN_REPORT, STOP, review  # noqa: F401 — review 是 fixture

KEY = "sk-test-0123456789abcdef"
REAL_GENERATE = spec_exec.generate_cases        # review fixture 會換成假的;這裡留著真的


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("LLM_API_KEY", "OLLAMA_URL", "SEGCRA_DBT_DATABASE"):
        monkeypatch.delenv(name, raising=False)


def _yaml(tmp_path, api_key_line):
    text = (PKG_ROOT / "config" / "models.yaml").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if not ln.startswith("api_key:")]
    if api_key_line is not None:
        lines.insert(0, api_key_line)
    path = tmp_path / "models.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ─────────────────── 1. 金鑰只能用環境變數給 ───────────────────

def test_環境變數給的金鑰(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    assert load_config().api_key == KEY
    assert load_config(_yaml(tmp_path, "api_key: ollama")).api_key == KEY


@pytest.mark.parametrize("line", [None, "api_key: ollama"])
def test_設定檔不寫或寫佔位時_沒設環境變數就是佔位(tmp_path, line):
    assert load_config(_yaml(tmp_path, line)).api_key == "ollama"


@pytest.mark.parametrize("line", ["api_key: sk-in-yaml-1111", "api_key: ''", "api_key: 12345",
                                  "api_key: OLLAMA"])
def test_設定檔裡寫了別的值_載入設定就停(monkeypatch, tmp_path, line):
    """review 實測:yaml 寫 `api_key: sk-in-yaml-1111`、不設環境變數,服務照常運作、沒有提示,
    金鑰就一直留在會進版控與離線包的檔案裡。設了 LLM_API_KEY 也一樣停:檔案裡的那把還在。"""
    for env in (None, KEY):
        if env:
            monkeypatch.setenv("LLM_API_KEY", env)
        with pytest.raises(ConfigError) as info:
            load_config(_yaml(tmp_path, line))
        msg = str(info.value)
        assert "LLM_API_KEY" in msg and "sk-in-yaml" not in msg and "12345" not in msg


def test_repo_的設定檔沒有寫金鑰():
    load_config()


def test_前後的空白不帶進金鑰(monkeypatch):
    """貼上時多帶的空白會讓端點回 401,看不出是哪裡錯。"""
    monkeypatch.setenv("LLM_API_KEY", f"  {KEY} ")
    assert load_config().api_key == KEY


def test_模型呼叫用的是這把金鑰(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", KEY)
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        async def close(self):
            pass

    async def fake_run(client, *args, **kwargs):
        return "{}"

    monkeypatch.setattr(agent, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(agent, "_run_agent", fake_run)
    cfg = load_config()
    asyncio.run(agent.run_agent(cfg, cfg.profile(None), "s", "u", None, use_tools=False))
    assert seen["api_key"] == KEY


# ─────────────────── 2. 設了但是空的,停下來說是哪個變數 ───────────────────

@pytest.mark.parametrize("name", ["OLLAMA_URL", "LLM_API_KEY", "SEGCRA_DBT_DATABASE"])
@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_環境變數設了但是空的_停下來並寫出變數名(monkeypatch, name, raw):
    """設了變數就表示要用它;空的多半是部署腳本沒把值帶進來。改用設定檔或預設值,等於用一份
    沒有人指定的設定繼續跑(review 實測:LLM_API_KEY 空 → 用 yaml 的值,端點回 401 看不出原因;
    OLLAMA_URL 空 → APIConnectionError,看不出是哪個變數)。"""
    monkeypatch.setenv(name, raw)
    with pytest.raises(ConfigError) as info:
        load_config()
    assert name in str(info.value)


@pytest.mark.parametrize("name,value,field", [
    ("OLLAMA_URL", "  http://model-host:8000/v1 ", "endpoint"),
    ("LLM_API_KEY", f"\t{KEY}  ", "api_key"),
])
def test_環境變數前後的空白去掉(monkeypatch, name, value, field):
    monkeypatch.setenv(name, value)
    assert getattr(load_config(), field) == value.strip()


def test_沒設的變數照用設定檔(monkeypatch):
    cfg = load_config()
    assert cfg.endpoint and cfg.api_key == "ollama"


# 同一類:GitLab 的三個設定以前任一個沒給(或給了空的)就悄悄改走 mock 模式

_GITLAB_VARS = ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_PROJECT", "GITLAB_READ_TOKEN")


@pytest.fixture
def gitlab_env(monkeypatch):
    for name in _GITLAB_VARS:
        monkeypatch.delenv(name, raising=False)

    def set_(**values):
        for k, v in values.items():
            monkeypatch.setenv(k, v)
    return set_


def test_GitLab三個都不設是mock模式(gitlab_env):
    from toolbox import gitlab
    assert gitlab._real_mode_settings() == ("", "", "")


def test_GitLab三個都設是real模式_前後空白去掉(gitlab_env):
    from toolbox import gitlab
    gitlab_env(GITLAB_URL=" http://gl:8929 ", GITLAB_TOKEN="t", GITLAB_PROJECT="3")
    assert gitlab._real_mode_settings() == ("http://gl:8929", "t", "3")


@pytest.mark.parametrize("given", [{"GITLAB_URL": "http://gl-zz-host"},
                                   {"GITLAB_URL": "http://gl-zz-host", "GITLAB_TOKEN": "zz-tok-zz"},
                                   {"GITLAB_TOKEN": "zz-tok-zz", "GITLAB_PROJECT": "zz-proj-zz"}])
def test_GitLab只給了幾個_停下來說缺哪個(gitlab_env, given):
    from toolbox import gitlab
    gitlab_env(**given)
    with pytest.raises(gitlab.GitLabConfigError) as info:
        gitlab._real_mode_settings()
    msg = str(info.value)
    assert all(name in msg for name in ("GITLAB_URL", "GITLAB_TOKEN", "GITLAB_PROJECT"))
    assert "zz" not in msg, "不寫出值(token、主機)"


@pytest.mark.parametrize("name", _GITLAB_VARS)
@pytest.mark.parametrize("raw", ["", "  "])
def test_GitLab的變數設了但是空的_停下來並寫出變數名(gitlab_env, name, raw):
    from toolbox import gitlab
    gitlab_env(**{name: raw})
    with pytest.raises(gitlab.GitLabConfigError, match=name):
        gitlab._real_mode_settings() if name != "GITLAB_READ_TOKEN" else gitlab._env_text(name, "")


# ─────────────────── 3. 印出設定時不帶金鑰與端點 ───────────────────

@pytest.mark.parametrize("url", ["http://gate.example:8080/pass-AAAA1111/v1",
                                 "http://user:secret-CCCC@gate.example/v1"])
def test_印出設定時不帶金鑰也不帶端點(monkeypatch, url):
    """端點網址也可能帶憑證(`gpu run` 設的 OLLAMA_URL 帶通行碼、閘道常用 user:pass@)。
    pytest 斷言失敗時也會印出 Config,測試輸出常被貼進 PR。"""
    monkeypatch.setenv("OLLAMA_URL", url)
    monkeypatch.setenv("LLM_API_KEY", KEY)
    cfg = load_config()
    for text in (repr(cfg), str(cfg)):
        assert KEY not in text and "AAAA1111" not in text and "secret-CCCC" not in text
        assert "gate.example" not in text
    assert cfg.endpoint == url, "只是不印,值照樣用"


def test_直接建構_Config_時照常可以給():
    cfg = Config(endpoint="http://u:p@x/v1", api_key="k", default_profile="review", profiles={},
                 roles={}, budget={}, policy={})
    assert cfg.api_key == "k" and "api_key" not in repr(cfg) and "u:p@" not in repr(cfg)


# ─────────────────── 4. 錯誤原文不進報告、留言與模型對話 ───────────────────

def _auth_error_with_key() -> openai.AuthenticationError:
    """端點回 401,錯誤內容帶著收到的金鑰(review 實測:openai 會把 message 放進例外訊息)。"""
    req = httpx.Request("POST", "http://gate.example/v1/chat/completions")
    resp = httpx.Response(401, request=req)
    return openai.AuthenticationError(
        f"Error code: 401 - Invalid API key provided: Bearer {KEY}", response=resp,
        body={"message": f"Invalid API key provided: Bearer {KEY}"})


def test_錯誤的說法只有型別與狀態碼():
    e = _auth_error_with_key()
    assert KEY in str(e), "對照組:例外原文確實帶著金鑰"
    assert error_label(e) == "AuthenticationError,HTTP 401"
    assert error_label(ValueError("x at https://gitlab.internal/api")) == "ValueError"


def test_測資生成失敗時_報告與留言都沒有金鑰(monkeypatch, review):
    """整條 review_mr:測資生成的模型呼叫回 401(錯誤內容帶金鑰)。"""
    async def failing_agent(*a, **kw):
        raise _auth_error_with_key()

    monkeypatch.setattr(spec_exec, "generate_cases", REAL_GENERATE)
    monkeypatch.setattr(spec_exec, "run_agent", failing_agent)
    report, _, output = review(CLEAN_REPORT, STOP)
    text = json.dumps(report, ensure_ascii=False)
    assert "測資生成失敗(LLM 呼叫失敗:AuthenticationError,HTTP 401)" in text, text[:2000]
    assert KEY not in text
    for f in output.rglob("*"):
        if f.is_file():
            assert KEY not in f.read_text(encoding="utf-8", errors="replace"), f.name


def test_工具丟例外時_回給模型的字串沒有原文():
    from orchestrator.tool_hub import ToolHub
    hub = ToolHub()

    def leaky(path, ref=None):
        raise httpx.ConnectError("connect failed: https://gitlab.internal.example/api/v4/x")

    hub._functions["gitlab__get_file"] = leaky
    out = asyncio.run(hub.call("gitlab__get_file", {"path": "a.sql"}))
    assert out == "Error executing tool gitlab__get_file: ConnectError"


# ─────────────────── 5. 讀設定檔前檢查權限 ───────────────────

@pytest.mark.parametrize("mode,ok", [(0o600, True), (0o400, True), (0o640, False),
                                     (0o644, False), (0o660, False), (0o664, False),
                                     (0o666, False), (0o604, False)])
def test_密碼檔_群組或其他人能讀寫就停(mode, ok):
    problem = _permission_problem(mode, "/srv/segcra/config/gitlab.env", secret=True, is_dir=False)
    assert (problem is None) is ok
    if problem:
        assert f"{mode:o}" in problem and "chmod 600 /srv/segcra/config/gitlab.env" in problem


@pytest.mark.parametrize("mode,ok", [(0o600, True), (0o644, True), (0o444, True),
                                     (0o664, False), (0o666, False), (0o646, False)])
def test_模型設定檔_群組或其他人能改就停(mode, ok):
    """模型設定檔不再有金鑰,所以看的是能不能被改(改了端點,金鑰與每個 MR 的程式碼都送到
    別人指定的地方)。Ubuntu 預設 umask 002 建出來的 664 要擋下來。"""
    problem = _permission_problem(mode, "/srv/m.yaml", secret=False, is_dir=False)
    assert (problem is None) is ok
    if problem:
        assert "chmod 644 /srv/m.yaml" in problem


@pytest.mark.parametrize("mode,ok", [(0o755, True), (0o700, True), (0o775, False),
                                     (0o777, False), (0o1777, True), (0o757, False)])
def test_所在目錄_群組或其他人能改就停(mode, ok):
    """目錄可寫就能把整個檔案換掉。有 sticky bit(1777,像 /tmp)時其他帳號不能改名或刪掉
    不是自己的項目,換不掉,所以不算太寬。"""
    problem = _permission_problem(mode, "/srv/segcra/config", secret=True, is_dir=True)
    assert (problem is None) is ok
    if problem:
        assert "chmod 755 /srv/segcra/config" in problem


posix_only = pytest.mark.skipif(os.name != "posix", reason="要真的 POSIX 權限位元(部署端是 Linux)")


@posix_only
@pytest.mark.parametrize("mode,secret,ok", [(0o600, True, True), (0o644, True, False),
                                            (0o664, True, False), (0o666, True, False),
                                            (0o644, False, True), (0o664, False, False)])
def test_真的檔案權限(tmp_path, mode, secret, ok):
    d = tmp_path / "config"
    d.mkdir(mode=0o755)
    f = d / "x.env"
    f.write_text("A=1\n", encoding="utf-8")
    f.chmod(mode)
    if ok:
        check_config_file(f, secret=secret)
    else:
        with pytest.raises(ConfigError, match=str(f)):
            check_config_file(f, secret=secret)


@posix_only
def test_真的目錄可寫就停(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    d.chmod(0o775)
    f = d / "models.yaml"
    f.write_text("x: 1\n", encoding="utf-8")
    f.chmod(0o644)
    with pytest.raises(ConfigError, match="目錄"):
        check_config_file(f, secret=False)


@posix_only
def test_載入設定時_模型設定檔權限太寬就起不來(tmp_path):
    d = tmp_path / "config"
    d.mkdir(mode=0o755)
    path = d / "models.yaml"
    path.write_text((PKG_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"),
                    encoding="utf-8")
    path.chmod(0o664)
    with pytest.raises(ConfigError, match="chmod 644"):
        load_config(path)
    path.chmod(0o644)
    load_config(path)


@posix_only
@pytest.mark.parametrize("name", ["gitlab.env", "sandbox.env"])
def test_載入設定時_密碼檔權限太寬就起不來(monkeypatch, tmp_path, name):
    """密碼檔在載入設定時就檢查:服務啟動時就停,而不是等到第一份審查用到才失敗。"""
    from orchestrator import config
    d = tmp_path / "config"
    d.mkdir(mode=0o755)
    models = d / "models.yaml"
    models.write_text((PKG_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"),
                      encoding="utf-8")
    models.chmod(0o644)
    secret = d / name
    secret.write_text("X=1\n", encoding="utf-8")
    secret.chmod(0o644)
    monkeypatch.setattr(config, "PKG_ROOT", tmp_path)
    with pytest.raises(ConfigError, match=name):
        load_config(models)
    secret.chmod(0o600)
    load_config(models)


def test_檔案不存在時不檢查(tmp_path):
    check_config_file(tmp_path / "沒有這個檔.env", secret=True)


def test_讀密碼檔的地方都先檢查權限():
    """讀這三個檔案的地方共用同一個檢查函式(漏一處就等於沒做)。"""
    import inspect

    from orchestrator import config, knowledge_governance
    assert "check_config_file(f, secret=True)" in inspect.getsource(
        knowledge_governance._load_gitlab_env)
    assert "check_config_file(env_file, secret=True)" in inspect.getsource(
        spec_exec._sandbox_params)
    src = inspect.getsource(config.load_config)
    assert "check_config_file(path, secret=False)" in src
    assert config._SECRET_FILES == ("gitlab.env", "sandbox.env")
    sh = (PKG_ROOT / "scripts" / "sandbox.sh").read_text(encoding="utf-8")
    assert 'check_secret_file "$ENV_FILE"' in sh


# ─────────── 權限:連結的目標、上層目錄、sticky bit、sandbox.sh 用同一份規則 ───────────

def _mkdir(path, mode):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    return path


def _write(path, mode, text="x: 1\n"):
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


@posix_only
@pytest.mark.parametrize("name,mode,secret", [("models.yaml", 0o644, False), ("sandbox.env", 0o600, True)])
def test_連結指到可寫的目錄就停(tmp_path, name, mode, secret):
    shared = _mkdir(tmp_path / "shared", 0o777)
    target = _write(shared / name, mode)
    cfg = _mkdir(tmp_path / "app" / "config", 0o755)
    (tmp_path / "app").chmod(0o755)
    link = cfg / name
    link.symlink_to(target)
    with pytest.raises(ConfigError, match=str(shared)):
        check_config_file(link, secret=secret)


@posix_only
def test_上層目錄可寫就停_訊息寫出是哪一層(tmp_path):
    srv = _mkdir(tmp_path / "srv", 0o775)
    app = _mkdir(srv / "SEGCRA-CR", 0o775)
    cfg = _mkdir(app / "config", 0o755)
    f = _write(cfg / "models.yaml", 0o644)
    with pytest.raises(ConfigError) as e:
        check_config_file(f, secret=False)
    assert f"chmod 755 {app}" in str(e.value) and "上層" in str(e.value)
    app.chmod(0o755)
    with pytest.raises(ConfigError, match=f"chmod 755 {srv}"):
        check_config_file(f, secret=False)
    srv.chmod(0o755)
    check_config_file(f, secret=False)


@posix_only
def test_連結所在的目錄可寫也停(tmp_path):
    real_dir = _mkdir(tmp_path / "real", 0o755)
    target = _write(real_dir / "models.yaml", 0o644)
    link_dir = _mkdir(tmp_path / "linkdir", 0o777)
    link = link_dir / "models.yaml"
    link.symlink_to(target)
    with pytest.raises(ConfigError, match=str(link_dir)):
        check_config_file(link, secret=False)


@posix_only
def test_連結指到權限正確的位置照常通過(tmp_path):
    real_dir = _mkdir(tmp_path / "real", 0o755)
    target = _write(real_dir / "models.yaml", 0o644)
    link_dir = _mkdir(tmp_path / "linkdir", 0o755)
    link = link_dir / "models.yaml"
    link.symlink_to(target)
    check_config_file(link, secret=False)


# 連結一段一段走:只看 resolve() 的結果與原本的路徑,中間經過的連結所在的目錄不在這兩條路上。
# 部署常用一個 current 連結指到目前那一版,current 放在別人改得到的目錄,換掉它就換掉了設定
@posix_only
def test_連結指到連結_中間那層目錄可寫就停(tmp_path):
    deploy = _mkdir(tmp_path / "deploy", 0o755)
    real = _write(deploy / "models.yaml", 0o644)
    shared = _mkdir(tmp_path / "shared", 0o777)
    (shared / "current.yaml").symlink_to(real)
    cfg = _mkdir(tmp_path / "app" / "config", 0o755)
    (tmp_path / "app").chmod(0o755)
    link = cfg / "models.yaml"
    link.symlink_to(shared / "current.yaml")
    with pytest.raises(ConfigError) as e:
        check_config_file(link, secret=False)
    assert f"chmod 755 {shared}" in str(e.value) and "路徑上的連結所在的目錄" in str(e.value)
    shared.chmod(0o755)
    check_config_file(link, secret=False)


@posix_only
def test_路徑中間的目錄是連結_連結所在的目錄可寫就停(tmp_path):
    real2 = _mkdir(tmp_path / "real2" / "n2" / "cfg", 0o755)
    (tmp_path / "real2").chmod(0o755)
    (tmp_path / "real2" / "n2").chmod(0o755)
    _write(real2 / "models.yaml", 0o644)
    m = _mkdir(tmp_path / "m", 0o777)
    (m / "n").symlink_to(tmp_path / "real2" / "n2")
    app = _mkdir(tmp_path / "app", 0o755)
    (app / "config").symlink_to(m / "n" / "cfg")
    with pytest.raises(ConfigError, match=f"chmod 755 {m}"):
        check_config_file(app / "config" / "models.yaml", secret=False)
    m.chmod(0o755)
    check_config_file(app / "config" / "models.yaml", secret=False)


@posix_only
def test_連結循環時停_不會卡住(tmp_path):
    d = _mkdir(tmp_path / "loop", 0o755)
    (d / "a.yaml").symlink_to(d / "b.yaml")
    (d / "b.yaml").symlink_to(d / "a.yaml")
    from orchestrator import file_perms
    with pytest.raises(file_perms.PermissionProblem, match="連結超過"):
        file_perms.dirs_to_check(d / "a.yaml")


@posix_only
def test_沙盒腳本用的命令列_連結指到連結也停(tmp_path):
    """scripts/sandbox.sh 執行的是 file_perms.py 的 main(同一份規則),sandbox.env 一樣要擋。"""
    from orchestrator import file_perms
    deploy = _mkdir(tmp_path / "deploy", 0o755)
    real = _write(deploy / "sandbox.env", 0o600, "SANDBOX_MSSQL_PASSWORD=x\n")
    shared = _mkdir(tmp_path / "shared", 0o777)
    (shared / "current.env").symlink_to(real)
    cfg = _mkdir(tmp_path / "app", 0o755)
    (cfg / "sandbox.env").symlink_to(shared / "current.env")
    assert file_perms.main(["--secret", str(cfg / "sandbox.env")]) == 1
    shared.chmod(0o755)
    assert file_perms.main(["--secret", str(cfg / "sandbox.env")]) == 0


def test_一段一段解析_連結指到連結時記下每一層連結所在的目錄(tmp_path, monkeypatch):
    """不用真的連結(Windows 也跑得到):換掉 is_symlink 與 readlink,驗解析的走法。"""
    from orchestrator import file_perms
    root = tmp_path.resolve()
    links = {root / "app" / "config": root / "m" / "n" / "cfg", root / "m" / "n": root / "real2" / "n2",
             root / "app" / "cfg2": "../m/n/cfg"}
    monkeypatch.setattr(file_perms.Path, "is_symlink", lambda self: self in links)
    monkeypatch.setattr(file_perms.os, "readlink", lambda p: str(links[file_perms.Path(p)]))
    real, link_dirs = file_perms._resolve_steps(root / "app" / "config" / "models.yaml")
    assert real == root / "real2" / "n2" / "cfg" / "models.yaml"
    assert link_dirs == [root / "app", root / "m"]
    real, link_dirs = file_perms._resolve_steps(root / "app" / "cfg2" / "models.yaml")
    assert real == root / "real2" / "n2" / "cfg" / "models.yaml"      # 相對的連結接在連結所在的目錄後面
    assert link_dirs == [root / "app", root / "m"]
    assert set(file_perms.dirs_to_check(root / "app" / "config" / "models.yaml")) >= {
        root / "app", root / "m", root / "real2" / "n2" / "cfg"}


@posix_only
def test_有_sticky_bit_的上層目錄不算太寬(tmp_path):
    sticky = _mkdir(tmp_path / "tmpish", 0o1777)
    cfg = _mkdir(sticky / "cfg", 0o755)
    check_config_file(_write(cfg / "models.yaml", 0o644), secret=False)
    loose = _mkdir(tmp_path / "loose", 0o777)
    cfg2 = _mkdir(loose / "cfg", 0o755)
    with pytest.raises(ConfigError, match=str(loose)):
        check_config_file(_write(cfg2 / "models.yaml", 0o644), secret=False)


def test_沒有_sticky_bit_才算太寬_權限值本身():
    from orchestrator.file_perms import permission_problem
    assert permission_problem(0o41777, "/tmp", secret=True, is_dir=True) is None
    assert permission_problem(0o40777, "/x", secret=True, is_dir=True)


@posix_only
@pytest.mark.parametrize("mode,ok", [(0o644, False), (0o600, True)])
def test_sandbox_sh_實際執行_用同一份規則(tmp_path, mode, ok):
    import shutil
    import subprocess
    if shutil.which("python3") is None or shutil.which("bash") is None:
        pytest.skip("要有 python3 與 bash")
    d = _mkdir(tmp_path / "cfg", 0o755)
    env_file = _write(d / "sandbox.env", mode, "SANDBOX_MSSQL_PASSWORD=x\n")
    # 沒有的子指令:權限檢查在分派子指令之前,檢查過了才會走到「用法」那一行(不會碰 docker)
    r = subprocess.run(["bash", str(PKG_ROOT / "scripts" / "sandbox.sh"), "沒有這個子指令"],
                       env={**os.environ, "SANDBOX_ENV_FILE": str(env_file)},
                       capture_output=True, text=True, timeout=60)
    if ok:
        assert "權限太寬" not in r.stderr
    else:
        assert r.returncode != 0 and "chmod 600" in r.stderr, r.stderr
