"""模型設定檔的位置:環境變數 SEGCRA_MODELS_CONFIG 指定時讀那個檔案 — 不呼叫 LLM。

部署端要換模型、上下文長度時,如果直接改 repo 裡的 config/models.yaml,換新版程式碼就會被蓋掉。
所以讓部署端把自己的設定檔放在程式目錄以外,用環境變數指過去;repo 那份維持開發用的預設值。
"""
import pytest

from orchestrator.config import PKG_ROOT, load_config, models_config_path

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
def test_空字串或只有空白當成沒設(monkeypatch, raw):
    monkeypatch.setenv("SEGCRA_MODELS_CONFIG", raw)
    assert models_config_path() == REPO_YAML


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
    with pytest.raises(FileNotFoundError) as info:
        load_config()
    assert "nope.yaml" in str(info.value)
