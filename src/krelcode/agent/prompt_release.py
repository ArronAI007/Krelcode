"""Immutable Prompt releases and model-profile calibration evidence.

Prompt text lives in :mod:`krelcode.agent.prompts`.  This module owns the
content-free identities that make a release reproducible and the calibration
records that connect a resolved model profile to a real behavior-benchmark
baseline.  Persisted records contain only IDs, hashes, patterns, status and
counts; they never contain Prompt bodies, answers, tool arguments or keys.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping, Sequence


PROMPT_RELEASE_SCHEMA_VERSION = 1
MODEL_PROFILE_CALIBRATION_SCHEMA_VERSION = 1


class ProfileCalibrationStatus(str, Enum):
    """Runtime meaning of a model-profile calibration lookup."""

    CALIBRATED = "calibrated"
    UNCALIBRATED = "uncalibrated"
    STALE = "stale"


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash_payload(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _content_hash(sections: Sequence[object]) -> str:
    rendered = "\n\n".join(
        str(getattr(section, "content", "") or "").strip()
        for section in sorted(
            sections,
            key=lambda item: (
                int(getattr(item, "priority", 0) or 0),
                str(getattr(item, "id", "") or ""),
            ),
        )
        if str(getattr(section, "content", "") or "").strip()
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _normalized_strings(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        str(value).strip().lower()
        for value in values
        if str(value).strip()
    ))


@dataclass(frozen=True)
class PromptSectionIdentity:
    """Content-free identity for one immutable Prompt section."""

    id: str
    content_hash: str
    priority: int
    required: bool

    def __post_init__(self) -> None:
        if not self.id or not self.content_hash:
            raise ValueError("Prompt section identity 缺少 id/hash")
        if len(self.content_hash) != 64:
            raise ValueError("Prompt section content_hash 必须是 SHA-256")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "content_hash": self.content_hash,
            "priority": self.priority,
            "required": self.required,
        }


@dataclass(frozen=True)
class PromptProfileIdentity:
    """Content-free identity for an append-only model Prompt profile."""

    name: str
    provider_patterns: tuple[str, ...]
    model_patterns: tuple[str, ...]
    base_prompt_hash: str
    section_hashes: tuple[PromptSectionIdentity, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_patterns",
            _normalized_strings(self.provider_patterns))
        object.__setattr__(
            self, "model_patterns",
            _normalized_strings(self.model_patterns))
        object.__setattr__(self, "section_hashes", tuple(self.section_hashes))
        if not self.name or not self.base_prompt_hash:
            raise ValueError("Prompt profile identity 缺少 name/base hash")
        ids = [section.id for section in self.section_hashes]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Prompt profile identity 包含重复 section: {self.name}")

    @property
    def profile_hash(self) -> str:
        return _hash_payload(self._hash_payload())

    def _hash_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "provider_patterns": list(self.provider_patterns),
            "model_patterns": list(self.model_patterns),
            "base_prompt_hash": self.base_prompt_hash,
            "sections": [
                section.as_dict()
                for section in sorted(self.section_hashes, key=lambda item: item.id)
            ],
        }

    def as_dict(self) -> dict[str, object]:
        payload = self._hash_payload()
        payload["profile_hash"] = self.profile_hash
        return payload


@dataclass(frozen=True)
class PromptRelease:
    """An immutable, content-addressed Prompt release manifest."""

    schema_version: int
    release_id: str
    frozen_at: str
    base_sections: tuple[PromptSectionIdentity, ...]
    profiles: tuple[PromptProfileIdentity, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_sections", tuple(self.base_sections))
        object.__setattr__(self, "profiles", tuple(self.profiles))
        if self.schema_version != PROMPT_RELEASE_SCHEMA_VERSION:
            raise ValueError(
                f"不支持的 Prompt release schema: {self.schema_version}")
        if not self.release_id or not self.frozen_at:
            raise ValueError("Prompt release 缺少 id/frozen_at")
        base_ids = [section.id for section in self.base_sections]
        if not base_ids or len(base_ids) != len(set(base_ids)):
            raise ValueError("Prompt release base section 不能为空或重复")
        profile_names = [profile.name for profile in self.profiles]
        if not profile_names or len(profile_names) != len(set(profile_names)):
            raise ValueError("Prompt release profile 不能为空或重复")

    @property
    def release_hash(self) -> str:
        return _hash_payload(self._hash_payload())

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "release_id": self.release_id,
            "frozen_at": self.frozen_at,
            "base_sections": [
                section.as_dict()
                for section in sorted(self.base_sections, key=lambda item: item.id)
            ],
            "profiles": [
                profile.as_dict()
                for profile in sorted(self.profiles, key=lambda item: item.name)
            ],
        }

    def profile(self, name: str) -> PromptProfileIdentity:
        normalized = str(name or "").strip().lower()
        for profile in self.profiles:
            if profile.name == normalized:
                return profile
        raise ValueError(f"Prompt release {self.release_id} 不包含 profile: {name}")

    def manifest(self) -> dict[str, object]:
        payload = self._hash_payload()
        payload.update({
            "kind": "krelcode_prompt_release",
            "release_hash": self.release_hash,
            "contains_prompt_content": False,
        })
        return payload

    def as_dict(self) -> dict[str, object]:
        return self.manifest()


def build_prompt_release(
    release_id: str,
    base_sections: Sequence[object],
    profiles: Sequence[object],
    *,
    frozen_at: str = "2026-11-21",
) -> PromptRelease:
    """Build a deterministic release identity from the production catalog."""

    base_hashes = tuple(
        PromptSectionIdentity(
            id=str(getattr(section, "id", "") or ""),
            content_hash=str(getattr(section, "hash", "") or ""),
            priority=int(getattr(section, "priority", 0) or 0),
            required=bool(getattr(section, "required", False)),
        )
        for section in base_sections
    )
    base_hash = _content_hash(base_sections)
    profile_identities: list[PromptProfileIdentity] = []
    for profile in profiles:
        additional = tuple(getattr(profile, "additional_sections", ()) or ())
        profile_identities.append(PromptProfileIdentity(
            name=str(getattr(profile, "name", "") or "").strip().lower(),
            provider_patterns=tuple(
                getattr(profile, "provider_patterns", ()) or ()),
            model_patterns=tuple(
                getattr(profile, "model_patterns", ()) or ()),
            base_prompt_hash=_content_hash((*base_sections, *additional)),
            section_hashes=tuple(
                PromptSectionIdentity(
                    id=str(getattr(section, "id", "") or ""),
                    content_hash=str(getattr(section, "hash", "") or ""),
                    priority=int(getattr(section, "priority", 0) or 0),
                    required=bool(getattr(section, "required", False)),
                )
                for section in additional
            ),
        ))
    return PromptRelease(
        schema_version=PROMPT_RELEASE_SCHEMA_VERSION,
        release_id=str(release_id).strip(),
        frozen_at=str(frozen_at).strip(),
        base_sections=base_hashes,
        profiles=tuple(profile_identities),
    )


def profile_identity(profile: object, base_sections: Sequence[object]) -> PromptProfileIdentity:
    """Build a standalone identity, primarily for compatibility callers."""

    release = build_prompt_release(
        "adhoc",
        base_sections,
        (profile,),
        frozen_at="adhoc",
    )
    return release.profile(str(getattr(profile, "name", "") or ""))


@dataclass(frozen=True)
class ModelProfileCalibration:
    """Verified benchmark evidence for one model-profile scope.

    A calibration is evidence metadata, never an instruction injection.  Family
    patterns are opt-in; a baseline created from a concrete benchmark run uses
    the exact provider/model pair so it cannot silently claim an entire family.
    """

    schema_version: int
    calibration_id: str
    profile_name: str
    profile_hash: str
    release_id: str
    release_hash: str
    contract_version: str
    contract_hash: str
    benchmark_schema_version: int
    suite_hash: str
    provider: str
    model: str
    verified_at: str
    trials: int
    provider_patterns: tuple[str, ...] = ()
    model_patterns: tuple[str, ...] = ()
    verified: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_patterns",
            _normalized_strings(self.provider_patterns))
        object.__setattr__(
            self, "model_patterns",
            _normalized_strings(self.model_patterns))
        object.__setattr__(self, "profile_name", self.profile_name.strip().lower())
        object.__setattr__(self, "provider", self.provider.strip().lower())
        object.__setattr__(self, "model", self.model.strip().lower())
        object.__setattr__(self, "trials", max(0, int(self.trials)))
        if self.schema_version != MODEL_PROFILE_CALIBRATION_SCHEMA_VERSION:
            raise ValueError(
                f"不支持的 calibration schema: {self.schema_version}")
        if not self.calibration_id or not self.profile_name:
            raise ValueError("calibration 缺少 id/profile")
        for name, value in (
            ("profile_hash", self.profile_hash),
            ("release_hash", self.release_hash),
            ("contract_hash", self.contract_hash),
            ("suite_hash", self.suite_hash),
        ):
            if len(str(value or "")) != 64:
                raise ValueError(f"calibration {name} 必须是 SHA-256")
        if not self.release_id or not self.contract_version:
            raise ValueError("calibration 缺少 release/contract 版本")
        if not self.verified or self.trials < 1:
            raise ValueError("calibration 必须是已验证、至少 1 trial 的证据")
        if not self.provider and not self.model and not (
            self.provider_patterns or self.model_patterns
        ):
            raise ValueError("calibration 缺少 provider/model 适用范围")

    @property
    def calibration_hash(self) -> str:
        return _hash_payload(self._hash_payload())

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "calibration_id": self.calibration_id,
            "profile_name": self.profile_name,
            "profile_hash": self.profile_hash,
            "release_id": self.release_id,
            "release_hash": self.release_hash,
            "contract_version": self.contract_version,
            "contract_hash": self.contract_hash,
            "benchmark_schema_version": self.benchmark_schema_version,
            "suite_hash": self.suite_hash,
            "provider": self.provider,
            "model": self.model,
            "provider_patterns": list(self.provider_patterns),
            "model_patterns": list(self.model_patterns),
            "verified_at": self.verified_at,
            "trials": self.trials,
            "verified": self.verified,
        }

    def scope_score(self, provider: str, model: str) -> int:
        provider = str(provider or "").strip().lower()
        model = str(model or "").strip().lower()
        if self.provider and self.model:
            if provider == self.provider and model == self.model:
                return 1_000_000 + len(model)
            return -1
        scores: list[int] = []
        if self.provider_patterns:
            matches = [
                len(pattern.replace("*", ""))
                for pattern in self.provider_patterns
                if fnmatch.fnmatchcase(provider, pattern)
            ]
            if not matches:
                return -1
            scores.append(max(matches))
        if self.model_patterns:
            matches = [
                len(pattern.replace("*", ""))
                for pattern in self.model_patterns
                if fnmatch.fnmatchcase(model, pattern)
            ]
            if not matches:
                return -1
            scores.append(max(matches))
        return sum(scores)

    def as_dict(self) -> dict[str, object]:
        payload = self._hash_payload()
        payload.update({
            "kind": "model_profile_calibration",
            "calibration_hash": self.calibration_hash,
            "contains_prompt_content": False,
        })
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ModelProfileCalibration":
        allowed = {
            "kind", "schema_version", "calibration_id", "profile_name",
            "profile_hash", "release_id", "release_hash", "contract_version",
            "contract_hash", "benchmark_schema_version", "suite_hash",
            "provider", "model", "provider_patterns", "model_patterns",
            "verified_at", "trials", "verified", "calibration_hash",
            "contains_prompt_content",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(
                "calibration 包含不受支持的字段: " + ", ".join(unknown))
        if str(raw.get("kind", "")) != "model_profile_calibration":
            raise ValueError("不是 model_profile_calibration")
        if raw.get("contains_prompt_content") is not False:
            raise ValueError("calibration 不允许包含 Prompt/答案/工具正文")
        if not isinstance(raw.get("verified"), bool):
            raise ValueError("calibration verified 必须是布尔值")
        provider_patterns = raw.get("provider_patterns") or ()
        model_patterns = raw.get("model_patterns") or ()
        if not isinstance(provider_patterns, (list, tuple)) or not isinstance(
            model_patterns, (list, tuple)
        ):
            raise ValueError("calibration patterns 必须是数组")
        calibration = cls(
            schema_version=int(raw.get("schema_version", 0) or 0),
            calibration_id=str(raw.get("calibration_id", "") or ""),
            profile_name=str(raw.get("profile_name", "") or ""),
            profile_hash=str(raw.get("profile_hash", "") or ""),
            release_id=str(raw.get("release_id", "") or ""),
            release_hash=str(raw.get("release_hash", "") or ""),
            contract_version=str(raw.get("contract_version", "") or ""),
            contract_hash=str(raw.get("contract_hash", "") or ""),
            benchmark_schema_version=int(
                raw.get("benchmark_schema_version", 0) or 0),
            suite_hash=str(raw.get("suite_hash", "") or ""),
            provider=str(raw.get("provider", "") or ""),
            model=str(raw.get("model", "") or ""),
            verified_at=str(raw.get("verified_at", "") or ""),
            trials=int(raw.get("trials", 0) or 0),
            provider_patterns=tuple(str(item) for item in provider_patterns),
            model_patterns=tuple(str(item) for item in model_patterns),
            verified=bool(raw.get("verified", False)),
        )
        stored_hash = str(raw.get("calibration_hash", "") or "")
        if stored_hash and stored_hash != calibration.calibration_hash:
            raise ValueError("calibration_hash 与内容不一致")
        return calibration

    @classmethod
    def certify(
        cls,
        *,
        profile_name: str,
        profile_hash: str,
        release_id: str,
        release_hash: str,
        contract_version: str,
        contract_hash: str,
        benchmark_schema_version: int,
        suite_hash: str,
        provider: str,
        model: str,
        verified_at: str = "",
        trials: int = 1,
        provider_patterns: Sequence[str] = (),
        model_patterns: Sequence[str] = (),
    ) -> "ModelProfileCalibration":
        verified_at = verified_at or datetime.now(timezone.utc).isoformat()
        normalized_provider = str(provider or "").strip().lower()
        normalized_model = str(model or "").strip().lower()
        provider_scope = normalized_provider or (
            _normalized_strings(provider_patterns)[0]
            if _normalized_strings(provider_patterns) else "*")
        model_scope = normalized_model or (
            _normalized_strings(model_patterns)[0]
            if _normalized_strings(model_patterns) else "*")
        calibration_id = (
            "profile-calibration:"
            f"{str(profile_name).strip().lower()}:"
            f"{provider_scope}:{model_scope}:"
            f"{str(suite_hash)[:12]}"
        )
        return cls(
            schema_version=MODEL_PROFILE_CALIBRATION_SCHEMA_VERSION,
            calibration_id=calibration_id,
            profile_name=profile_name,
            profile_hash=profile_hash,
            release_id=release_id,
            release_hash=release_hash,
            contract_version=contract_version,
            contract_hash=contract_hash,
            benchmark_schema_version=benchmark_schema_version,
            suite_hash=suite_hash,
            provider=provider,
            model=model,
            verified_at=verified_at,
            trials=trials,
            provider_patterns=tuple(provider_patterns),
            model_patterns=tuple(model_patterns),
            verified=True,
        )


@dataclass(frozen=True)
class ProfileCalibrationResolution:
    """Result of matching runtime identity to calibration evidence."""

    status: ProfileCalibrationStatus
    calibration: ModelProfileCalibration | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ProfileCalibrationStatus(self.status))

    @property
    def calibrated(self) -> bool:
        return self.status is ProfileCalibrationStatus.CALIBRATED

    @property
    def calibration_id(self) -> str:
        return self.calibration.calibration_id if self.calibration else ""

    @property
    def calibration_hash(self) -> str:
        return self.calibration.calibration_hash if self.calibration else ""

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "calibration_id": self.calibration_id,
            "calibration_hash": self.calibration_hash,
            "reason": self.reason,
        }


class PromptReleaseRegistry:
    """Resolve a profile against one release and an immutable calibration set."""

    def __init__(
        self,
        release: PromptRelease,
        calibrations: Sequence[ModelProfileCalibration] = (),
    ) -> None:
        self.release = release
        self.calibrations = tuple(calibrations)

    def resolve(
        self,
        profile_name: str,
        provider: str,
        model: str,
        *,
        contract_version: str,
        contract_hash: str,
        suite_hash: str = "",
    ) -> ProfileCalibrationResolution:
        try:
            profile = self.release.profile(profile_name)
        except ValueError as exc:
            return ProfileCalibrationResolution(
                ProfileCalibrationStatus.UNCALIBRATED, reason=str(exc))
        candidates = [
            (calibration.scope_score(provider, model), calibration)
            for calibration in self.calibrations
            if calibration.profile_name == profile.name
            and calibration.scope_score(provider, model) >= 0
        ]
        if not candidates:
            return ProfileCalibrationResolution(
                ProfileCalibrationStatus.UNCALIBRATED,
                reason=(
                    "没有覆盖当前 provider/model 的 Benchmark 校准；"
                    "普通对话继续运行，release gate fail-closed"),
            )
        candidates.sort(
            key=lambda item: (item[0], item[1].calibration_id),
            reverse=True,
        )
        calibration = candidates[0][1]
        mismatches: list[str] = []
        if calibration.release_id != self.release.release_id:
            mismatches.append("release_id")
        if calibration.release_hash != self.release.release_hash:
            mismatches.append("release_hash")
        if calibration.profile_hash != profile.profile_hash:
            mismatches.append("profile_hash")
        if calibration.contract_version != str(contract_version):
            mismatches.append("contract_version")
        if calibration.contract_hash != str(contract_hash):
            mismatches.append("contract_hash")
        if suite_hash and calibration.suite_hash != str(suite_hash):
            mismatches.append("suite_hash")
        if mismatches:
            return ProfileCalibrationResolution(
                ProfileCalibrationStatus.STALE,
                calibration,
                "校准与当前身份不一致: " + ", ".join(mismatches),
            )
        return ProfileCalibrationResolution(
            ProfileCalibrationStatus.CALIBRATED,
            calibration,
            "release/profile/contract 与 Benchmark 证据一致",
        )


def save_model_profile_calibration(
    path: Path,
    calibration: ModelProfileCalibration,
) -> None:
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(calibration.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def load_model_profile_calibration(path: Path) -> ModelProfileCalibration:
    source = Path(path).expanduser()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取 calibration: {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"calibration JSON 无效: {source}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("calibration 根节点必须是 object")
    return ModelProfileCalibration.from_dict(raw)


__all__ = [
    "MODEL_PROFILE_CALIBRATION_SCHEMA_VERSION",
    "ModelProfileCalibration",
    "PROMPT_RELEASE_SCHEMA_VERSION",
    "ProfileCalibrationResolution",
    "ProfileCalibrationStatus",
    "PromptProfileIdentity",
    "PromptRelease",
    "PromptReleaseRegistry",
    "PromptSectionIdentity",
    "build_prompt_release",
    "load_model_profile_calibration",
    "profile_identity",
    "save_model_profile_calibration",
]
