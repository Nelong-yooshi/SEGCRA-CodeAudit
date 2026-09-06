"""Skill 載入:啟動只載 description 索引,內文由 agent 以 load_skill 工具按需取得。"""
import re
from dataclasses import dataclass
from pathlib import Path

from .config import PKG_ROOT

SKILLS_DIR = PKG_ROOT / "skills"


@dataclass
class Skill:
    name: str
    description: str
    trigger: str  # always | on_demand
    body: str


def load_skills() -> dict[str, Skill]:
    skills = {}
    for f in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        text = f.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            continue
        meta = dict(
            (k.strip(), v.strip())
            for k, v in (line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
        )
        skills[meta["name"]] = Skill(
            name=meta["name"], description=meta.get("description", ""),
            trigger=meta.get("trigger", "on_demand"), body=m.group(2).strip(),
        )
    return skills


def skills_index(skills: dict[str, Skill]) -> str:
    lines = [f"- `{s.name}`:{s.description}" for s in skills.values() if s.trigger != "always"]
    return "\n".join(lines)


def always_skills(skills: dict[str, Skill]) -> str:
    return "\n\n".join(s.body for s in skills.values() if s.trigger == "always")
