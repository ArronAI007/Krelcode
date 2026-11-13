"""Skill 领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Tuple


class SkillError(Exception):
    """Skill 不存在、不可调用或资源非法。"""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    directory: Path
    skill_file: Path
    source: str
    user_invocable: bool = True
    disable_model_invocation: bool = False
    argument_hint: str = ""
    allowed_tools: Tuple[str, ...] = ()
    version: str = ""
    license: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def location(self) -> str:
        return str(self.skill_file)


@dataclass(frozen=True)
class SkillResource:
    path: str
    kind: str
    size: int


@dataclass(frozen=True)
class SkillIssue:
    path: Path
    message: str


@dataclass(frozen=True)
class SkillReloadReport:
    skills: Tuple[Skill, ...]
    issues: Tuple[SkillIssue, ...]

    @property
    def count(self) -> int:
        return len(self.skills)
