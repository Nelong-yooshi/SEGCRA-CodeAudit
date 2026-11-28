"""模型設定檔的位置:環境變數 SEGCRA_MODELS_CONFIG 指定時讀那個檔案 — 不呼叫 LLM。

部署端要換模型、上下文長度時,如果直接改 repo 裡的 config/models.yaml,換新版程式碼就會被蓋掉。
所以讓部署端把自己的設定檔放在程式目錄以外,用環境變數指過去;repo 那份維持開發用的預設值。
"""
import os

import pytest

from orchestrator.config import PKG_ROOT, ConfigError, load_config, models_config_path

REPO_YAML = PKG_ROOT / "config" / "models.yaml"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SEGCRA_MODELS_CONFIG", raising=False)


def _deploy_yaml(tmp_path, model="deploy-model"):
    """repo 那份換掉 review 的模型名稱,其他照抄:跟部署端複製一份再改的做法一樣。"""
    text = REPO_YAML.read_text(encoding="utf-8")
    review = text.index("  review:")
    start = text.index("model:", review)
    end = text.index("\n", start)
    path = tmp_path / "deploy-models.yaml"
    path.write_text(text[:start] + f"model: {model}" + text[end:], encoding="utf-8")
    return path


def test_沒設時讀_repo_的設定檔():
    assert models_config_path() == REPO_YAML
    assert load_config().profile("review").model != "deploy-model"


def test_設了就讀那個檔案(monkeypatch, tmp_path):
    path = _deploy_yaml(tmp_path)
    monkeypatch.setenv("SEGCRA_MODELS_CONFIG", str(path))
    assert models_config_path() == path
    assert load_config().profile("review").model == "deploy-model"


@pytest.mark.parametrize("raw", ["", "   "])
def test_設了但是空的要停下來_不改讀_repo_那份(monkeypatch, raw):
    """空的多半是部署腳本沒把值帶進來;改讀 repo 那份等於用開發用的設定打部署端的端點。"""
    monkeypatch.setenv("SEGCRA_MODELS_CONFIG", raw)
    with pytest.raises(ConfigError, match="SEGCRA_MODELS_CONFIG"):
        models_config_path()
    with pytest.raises(ConfigError, match="SEGCRA_MODELS_CONFIG"):
        load_config()


def test_前後的空白不算(monkeypatch, tmp_path):
    path = _deploy_yaml(tmp_path)
    monkeypatch.setenv("SEGCRA_MODELS_CONFIG", f"  {path} ")
    assert load_config().profile("review").model == "deploy-model"


def test_直接給路徑時用給的(monkeypatch, tmp_path):
    """測試與評測會直接給路徑:不被環境變數蓋掉。"""
    monkeypatch.setenv("SEGCRA_MODELS_CONFIG", str(_deploy_yaml(tmp_path, "from-env")))
    other = tmp_path / "other"
    other.mkdir()
    assert load_config(_deploy_yaml(other, "from-arg")).profile("review").model == "from-arg"


def test_指定的檔案不存在時報錯_不退回_repo_的設定(monkeypatch, tmp_path):
    """打錯路徑時悄悄改用開發用的設定,會拿開發用的模型名稱去打部署端的端點,看不出原因。"""
    missing = tmp_path / "nope.yaml"
    monkeypatch.setenv("SEGCRA_MODELS_CONFIG", str(missing))
    with pytest.raises(ConfigError) as info:
        load_config()
    assert "nope.yaml" in str(info.value) and "SEGCRA_MODELS_CONFIG" in str(info.value)


@pytest.mark.skipif(os.name != "posix", reason="要真的 POSIX 權限位元(部署端是 Linux)")
def test_指到的檔案也檢查權限(monkeypatch, tmp_path):
    """部署端自己那份跟 repo 那份一樣:別人改得到就能把端點換掉。"""
    d = tmp_path / "deploy"
    d.mkdir(mode=0o755)
    path = _deploy_yaml(d)
    path.chmod(0o664)
    monkeypatch.setenv("SEGCRA_MODELS_CONFIG", str(path))
    with pytest.raises(ConfigError, match="chmod 644"):
        load_config()
    path.chmod(0o644)
    assert load_config().profile("review").model == "deploy-model"


def _expect(monkeypatch, value, *fragments):
    monkeypatch.setenv("SEGCRA_MODELS_CONFIG", str(value))
    with pytest.raises(ConfigError) as info:
        load_config()
    for f in ("SEGCRA_MODELS_CONFIG",) + fragments:
        assert f in str(info.value), (f, str(info.value))
    return str(info.value)


def test_指到目錄時報錯_寫出變數名與路徑(monkeypatch, tmp_path):
    d = tmp_path / "cfgdir"
    d.mkdir()
    _expect(monkeypatch, d, str(d), "讀不到")


@pytest.mark.parametrize("value", ["cfgdir/models.yaml", "config/models.yaml", "./models.yaml"])
def test_相對路徑報錯_不跟著啟動目錄走(monkeypatch, value):
    _expect(monkeypatch, value, "絕對路徑")


def test_波浪號先展開(monkeypatch, tmp_path):
    """寫在 docker -e 或 systemd 裡、加了引號時,shell 不會展開 ~。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    msg = _expect(monkeypatch, "~/segcra-config/models.yaml", "讀不到")
    assert "絕對路徑" not in msg and str(tmp_path) in msg


def test_空檔報錯_寫出變數名與路徑(monkeypatch, tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    _expect(monkeypatch, p, str(p), "空的")


def test_YAML_格式錯報錯_寫出變數名與路徑_不寫出內容(monkeypatch, tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("profiles: [sk-secret-in-yaml, {\n", encoding="utf-8")
    msg = _expect(monkeypatch, p, str(p), "YAML")
    assert "sk-secret-in-yaml" not in msg


def test_缺少必要的鍵報錯(monkeypatch, tmp_path):
    p = tmp_path / "partial.yaml"
    p.write_text("endpoint: http://localhost:11434/v1\n", encoding="utf-8")
    _expect(monkeypatch, p, str(p), "default_profile")


def test_內容不合規時也寫出是哪個檔案(monkeypatch, tmp_path):
    text = REPO_YAML.read_text(encoding="utf-8").replace("api_key: ollama", "api_key: EMPTY")
    p = tmp_path / "key.yaml"
    p.write_text(text, encoding="utf-8")
    _expect(monkeypatch, p, str(p), "api_key")


def test_直接給路徑時不寫環境變數名(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(p)
    assert str(p) in str(info.value) and "SEGCRA_MODELS_CONFIG" not in str(info.value)


def test_設定記下讀的是哪一份與內容的雜湊(monkeypatch, tmp_path):
    import hashlib
    path = _deploy_yaml(tmp_path)
    monkeypatch.setenv("SEGCRA_MODELS_CONFIG", str(path))
    src = load_config().source
    assert src == {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def test_報告記下這次用的是哪一份設定(monkeypatch, tmp_path):
    import asyncio
    import json

    import openai
    import httpx

    from orchestrator import pipeline
    from toolbox import gitlab
    fixtures, output = tmp_path / "fixtures", tmp_path / "out"
    fixtures.mkdir()
    output.mkdir()
    monkeypatch.setattr(gitlab, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(gitlab, "OUTPUT_DIR", output)
    monkeypatch.setenv("REVIEW_OUTPUT", str(output))
    (fixtures / "mr_971.json").write_text(json.dumps({
        "title": "t", "description": "d", "sha": "c0ff" + "0" * 36,
        "files": [{"path": "sql/x.sql", "diff": "@@ -1 +1 @@\n+SELECT 1", "full_content": "SELECT 1"}],
    }), encoding="utf-8")

    async def failing(*a, **k):
        raise openai.APIConnectionError(request=httpx.Request("POST", "http://m.invalid/v1"))
    monkeypatch.setattr(pipeline, "run_agent", failing)
    path = _deploy_yaml(tmp_path)
    cfg = load_config(path)
    report = asyncio.run(pipeline.review_mr(cfg, "971"))
    assert report["_models_config"]["path"] == str(path)
    assert len(report["_models_config"]["sha256"]) == 64
