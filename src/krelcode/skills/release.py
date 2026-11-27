"""Versioned Skill release manifests and bounded rollback."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional

from ..core.redact import SecretRedactor
from .models import Skill, SkillError

SKILL_RELEASE_VERSION = 1


@dataclass(frozen=True)
class SkillReleaseManifest:
    release_id: str
    name: str
    version: str
    source: str
    scope: str
    digest: str
    created_at: float
    skill_markdown: str
    previous_release_id: str = ""
    status: str = "released"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def as_dict(self, *, include_body: bool = True) -> dict[str, object]:
        record: dict[str, object] = {
            "type": "skill_release",
            "schema_version": SKILL_RELEASE_VERSION,
            "release_id": self.release_id,
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "scope": self.scope,
            "digest": self.digest,
            "created_at": self.created_at,
            "previous_release_id": self.previous_release_id,
            "status": self.status,
            "metadata": dict(self.metadata),
        }
        if include_body:
            record["skill_markdown"] = self.skill_markdown
        return record

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "SkillReleaseManifest":
        if raw.get("type") != "skill_release":
            raise ValueError("不是 Skill release 记录")
        if int(str(raw.get("schema_version") or 0)) != SKILL_RELEASE_VERSION:
            raise ValueError("Skill release schema 不兼容")
        metadata_raw = raw.get("metadata")
        return cls(
            release_id=str(raw.get("release_id") or ""),
            name=str(raw.get("name") or ""),
            version=str(raw.get("version") or ""),
            source=str(raw.get("source") or ""),
            scope=str(raw.get("scope") or "custom"),
            digest=str(raw.get("digest") or ""),
            created_at=float(str(raw.get("created_at") or 0.0)),
            skill_markdown=str(raw.get("skill_markdown") or ""),
            previous_release_id=str(raw.get("previous_release_id") or ""),
            status=str(raw.get("status") or "released"),
            metadata=(
                dict(metadata_raw)
                if isinstance(metadata_raw, Mapping) else {}
            ),
        )


class SkillReleaseStore:
    """Append-only Skill release ledger with SKILL.md rollback."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        path: Optional[Path] = None,
        redactor: Optional[SecretRedactor] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.path = (
            Path(path)
            if path is not None
            else self.workspace_root / ".krelcode" / "skills" / "releases.jsonl"
        )
        self.redactor = redactor
        self.last_error = ""

    def record(self, skill: Skill) -> SkillReleaseManifest:
        body = _read_skill_document(skill)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        existing = self.list(skill.name)
        created_at = time.time()
        release = SkillReleaseManifest(
            release_id=f"{skill.name}-{int(created_at * 1000)}-{digest[:8]}",
            name=skill.name,
            version=skill.version or digest[:12],
            source=skill.source,
            scope=_skill_scope(skill),
            digest=digest,
            created_at=created_at,
            skill_markdown=body,
            previous_release_id=existing[0].release_id if existing else "",
        )
        if not self._append(release):
            raise SkillError(f"无法写入 Skill release ledger: {self.last_error}")
        return release

    def list(self, name: str = "", *, limit: int = 100) -> tuple[SkillReleaseManifest, ...]:
        records = self._load()
        if name:
            records = [item for item in records if item.name == name]
        records.sort(key=lambda item: item.created_at, reverse=True)
        return tuple(records[:max(1, int(limit))])

    def rollback(
        self,
        skill: Skill,
        release_id: str = "",
    ) -> SkillReleaseManifest:
        records = list(self.list(skill.name))
        if not records:
            raise SkillError(f"Skill {skill.name!r} 没有 release 记录")
        target: Optional[SkillReleaseManifest] = None
        if release_id:
            target = next(
                (item for item in records if item.release_id == release_id),
                None,
            )
        else:
            current = hashlib.sha256(
                _read_skill_document(skill).encode("utf-8")).hexdigest()
            if records[0].digest == current and len(records) > 1:
                target = records[1]
            else:
                target = records[0]
        if target is None:
            raise SkillError(f"找不到 Skill release: {release_id}")
        _write_skill_document(skill, target.skill_markdown)
        return target

    def _append(self, release: SkillReleaseManifest) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                release.as_dict(),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if self.redactor is not None:
                line = self.redactor.redact(line)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except (OSError, TypeError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self.last_error = ""
        return True

    def _load(self) -> List[SkillReleaseManifest]:
        if not self.path.is_file():
            return []
        records: List[SkillReleaseManifest] = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        latest: dict[str, SkillReleaseManifest] = {}
        for line in lines:
            try:
                raw = json.loads(line)
                release = SkillReleaseManifest.from_dict(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            latest[release.release_id] = release
        records.extend(latest.values())
        return records


def _read_skill_document(skill: Skill) -> str:
    try:
        return skill.skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"读取 SKILL.md 失败: {exc}") from exc


def _write_skill_document(skill: Skill, content: str) -> None:
    target = skill.skill_file.resolve()
    root = skill.directory.resolve()
    if root not in target.parents:
        raise SkillError("拒绝写回 Skill 目录之外的 SKILL.md")
    temporary = target.with_suffix(".md.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(target)
    except OSError as exc:
        raise SkillError(f"回滚 SKILL.md 失败: {exc}") from exc


def _skill_scope(skill: Skill) -> str:
    source = skill.source.lower()
    if source.startswith("project:"):
        return "project"
    if source.startswith("global:") or source.startswith("builtin:"):
        return "user"
    if source.startswith("plugin:"):
        return "plugin"
    return "custom"


__all__ = [
    "SKILL_RELEASE_VERSION",
    "SkillReleaseManifest",
    "SkillReleaseStore",
]
