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
        return ModelProfile(**p)

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
