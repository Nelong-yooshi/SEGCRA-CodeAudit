import os
from dataclasses import dataclass
from pathlib import Path

import yaml

PKG_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ModelProfile:
    model: str
    num_ctx: int
    temperature: float
    max_output_tokens: int
    seed: int | None = None


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
        return prof

    def role_profile(self, role: str) -> ModelProfile:
        """依角色(testgen / arbiter)取模型 profile;未設定則用預設 profile。"""
        return self.profile(self.roles.get(role) or self.default_profile)


def load_config(path: Path | None = None) -> Config:
    raw = yaml.safe_load((path or PKG_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))
    # endpoint 可用環境變數覆蓋(如 Ollama 掛在遠端或 tunnel 上)
    endpoint = os.environ.get("OLLAMA_URL", raw.get("endpoint", "http://localhost:11434/v1"))
    return Config(
        endpoint=endpoint, api_key=raw.get("api_key", "ollama"),
        default_profile=raw["default_profile"], profiles=raw["profiles"],
        roles=raw.get("roles", {}), budget=raw["budget"],
        policy=raw.get("policy", {}),
    )


def estimate_tokens(text: str) -> int:
    """粗估 token 數(中文 ~1 token/1.5字、英文 ~1 token/4字,取保守值 len/3)。"""
    return max(1, len(text) // 3)
