"""Skill 领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..contracts.asset import AssetEnvelope, AssetScope


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
    owner: str = ""
    applicability: str = ""
    required_tools: Tuple[str, ...] = ()
    required_permissions: Tuple[str, ...] = ()
    required_environment: Tuple[str, ...] = ()
    input_contract: str = ""
    output_contract: str = ""
    acceptance_contract: Tuple[str, ...] = ()
    dependencies: Tuple[str, ...] = ()
    tests: Tuple[str, ...] = ()
    supported_models: Tuple[str, ...] = ()
    deprecated: bool = False
    asset: Optional[AssetEnvelope] = None

    def __post_init__(self) -> None:
        if self.asset is None:
            raw_derived = self.metadata.get("derived_from")
            derived_from = (
                tuple(str(item) for item in raw_derived)
                if isinstance(raw_derived, (list, tuple)) else ()
            )
            scope = (
                AssetScope.PROJECT
                if self.source.startswith("project:")
                else AssetScope.GLOBAL
                if self.source.startswith(("global:", "builtin:"))
                else AssetScope.USER
            )
            object.__setattr__(self, "asset", AssetEnvelope(
                asset_id=f"skill:{self.name}",
                asset_type="skill",
                scope=scope,
                owner=self.owner,
                tenant_id=str(self.metadata.get("tenant_id") or ""),
                organization_id=str(
                    self.metadata.get("organization_id") or ""),
                project_id=str(self.metadata.get("project_id") or ""),
                version=self.version,
                retention=str(
                    self.metadata.get("retention") or "project"),
                derived_from=derived_from,
                metadata={"skill_source": self.source},
            ))

    @property
    def release_ready(self) -> bool:
        return bool(
            self.description
            and self.applicability
            and self.acceptance_contract
            and self.tests
        )

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
