"""Content-light catalogue for every model-visible Prompt surface.

``PromptFragmentRegistry`` owns the L0-L12 fragments of one assembled main
request.  It intentionally does not describe auxiliary model calls such as
compaction, review, Hooks, MCP Prompts, or extension commands.  This module
provides the missing cross-surface directory without retaining Prompt bodies or
changing Provider messages.

The registry is observability-only:

* descriptors contain provenance, authority, trust, stability, version, hash,
  token estimates, and bounded metadata;
* observations contain only render counters and content-light hashes;
* replacing a catalogue collection never mutates a live Provider request.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Mapping, Optional, Sequence, Tuple, cast

from krelcode.agent.evidence import EvidenceSource, InstructionSource, SourceRole
from krelcode.agent.prompt.fragments import PROMPT_STABILITY_LEVELS, PROMPT_TRUST_LEVELS
from krelcode.agent.prompt.prompt_surface_contract import PromptSurfaceContract
from krelcode.agent.tokens import estimate_text_tokens


PROMPT_SURFACE_SCHEMA_VERSION = 1


class PromptSurfaceKind(str, Enum):
    MAIN_AGENT = "main_agent"
    TOOL = "tool"
    WORKFLOW = "workflow"
    REMINDER = "reminder"
    SUBAGENT = "subagent"
    COMPACT = "compact"
    REVIEW = "review"
    HOOK = "hook"
    MCP_PROMPT = "mcp_prompt"
    CUSTOM_COMMAND = "custom_command"
    SKILL = "skill"
    PLUGIN_COMMAND = "plugin_command"
    OUTPUT_STYLE = "output_style"
    GENERATED_ARTIFACT = "generated_artifact"


CORE_SURFACE_KINDS = (
    PromptSurfaceKind.MAIN_AGENT,
    PromptSurfaceKind.TOOL,
    PromptSurfaceKind.WORKFLOW,
    PromptSurfaceKind.REMINDER,
    PromptSurfaceKind.SUBAGENT,
    PromptSurfaceKind.COMPACT,
    PromptSurfaceKind.REVIEW,
)

_INSTRUCTION_AUTHORITIES = frozenset(item.value for item in InstructionSource)
_EVIDENCE_AUTHORITIES = frozenset(item.value for item in EvidenceSource)
_MAX_METADATA_ITEMS = 40
_MAX_METADATA_DEPTH = 4
_MAX_TEXT = 500


def prompt_content_hash(content: object) -> str:
    """Return the canonical SHA-256 for one model-visible Prompt body."""
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def prompt_structure_hash(value: object) -> str:
    """Hash content-light structured metadata with deterministic ordering."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text(value: object, limit: int = _MAX_TEXT) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _integer(value: object) -> int:
    if not isinstance(value, (int, float, str, bytes, bytearray)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _enum_text(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


def _strings(value: object) -> Tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    return tuple(dict.fromkeys(
        _text(item, 160) for item in value if _text(item, 160)
    ))


def _metadata_value(value: object, *, depth: int = 0) -> object:
    if depth >= _MAX_METADATA_DEPTH:
        return _text(value, 160)
    if isinstance(value, Mapping):
        items = list(value.items())[:_MAX_METADATA_ITEMS]
        return {
            _text(key, 80): _metadata_value(item, depth=depth + 1)
            for key, item in items
            if _text(key, 80)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _metadata_value(item, depth=depth + 1)
            for item in list(value)[:_MAX_METADATA_ITEMS]
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _text(value)


def _bounded_metadata(value: Mapping[str, object] | None) -> Mapping[str, object]:
    normalized = _metadata_value(dict(value or {}))
    return normalized if isinstance(normalized, dict) else {}


@dataclass(frozen=True)
class PromptSurfaceDescriptor:
    """One catalogue entry; deliberately contains no Prompt body."""

    id: str
    kind: PromptSurfaceKind | str
    source: str
    owner: str
    version: str = ""
    role: SourceRole | str = SourceRole.INSTRUCTION
    authority: str = InstructionSource.MANAGED.value
    trust: str = "managed"
    stability: str = "session"
    scope: str = "managed"
    content_hash: str = ""
    token_estimate: int = 0
    description: str = ""
    available: bool = True
    capabilities: Tuple[str, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)
    contract: Optional[PromptSurfaceContract] = None

    def __post_init__(self) -> None:
        surface_id = _text(self.id, 200)
        if not surface_id:
            raise ValueError("PromptSurfaceDescriptor.id 不能为空")
        try:
            kind = PromptSurfaceKind(self.kind)
        except ValueError as exc:
            raise ValueError(f"未知 Prompt Surface kind: {self.kind!r}") from exc
        try:
            role = SourceRole(self.role)
        except ValueError as exc:
            raise ValueError(f"未知 Prompt Surface role: {self.role!r}") from exc
        authority = _text(self.authority, 80).lower()
        allowed = (
            _INSTRUCTION_AUTHORITIES
            if role is SourceRole.INSTRUCTION
            else _EVIDENCE_AUTHORITIES
        )
        if authority not in allowed:
            raise ValueError(
                f"Prompt Surface {surface_id} 的 authority={authority!r} "
                f"不属于 role={role.value}")
        trust = _text(self.trust, 40).lower()
        if trust not in PROMPT_TRUST_LEVELS:
            raise ValueError(
                f"Prompt Surface {surface_id} 的 trust={trust!r} 非法")
        stability = _text(self.stability, 40).lower()
        if stability not in PROMPT_STABILITY_LEVELS:
            raise ValueError(
                f"Prompt Surface {surface_id} 的 stability={stability!r} 非法")
        content_hash = _text(self.content_hash, 128)
        if not content_hash:
            raise ValueError(f"Prompt Surface {surface_id} 缺少 content_hash")
        object.__setattr__(self, "id", surface_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "source", _text(self.source, 240))
        object.__setattr__(self, "owner", _text(self.owner, 160))
        object.__setattr__(self, "version", _text(self.version, 120))
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "trust", trust)
        object.__setattr__(self, "stability", stability)
        object.__setattr__(self, "scope", _text(self.scope, 80))
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "token_estimate", _integer(self.token_estimate))
        object.__setattr__(self, "description", _text(self.description, 320))
        object.__setattr__(self, "available", bool(self.available))
        object.__setattr__(self, "capabilities", _strings(self.capabilities))
        object.__setattr__(self, "metadata", _bounded_metadata(self.metadata))
        if self.contract is not None and not isinstance(
            self.contract, PromptSurfaceContract,
        ):
            raise TypeError("Prompt Surface contract 类型非法")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": _enum_text(self.kind),
            "source": self.source,
            "owner": self.owner,
            "version": self.version,
            "role": _enum_text(self.role),
            "authority": self.authority,
            "trust": self.trust,
            "stability": self.stability,
            "scope": self.scope,
            "content_hash": self.content_hash,
            "token_estimate": self.token_estimate,
            "description": self.description,
            "available": self.available,
            "capabilities": list(self.capabilities),
            "metadata": dict(self.metadata),
            "contract": self.contract.as_dict() if self.contract else None,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PromptSurfaceDescriptor":
        return cls(
            id=str(raw.get("id", "") or ""),
            kind=str(raw.get("kind", "") or ""),
            source=str(raw.get("source", "") or ""),
            owner=str(raw.get("owner", "") or ""),
            version=str(raw.get("version", "") or ""),
            role=str(raw.get("role", SourceRole.INSTRUCTION.value) or ""),
            authority=str(raw.get("authority", "") or ""),
            trust=str(raw.get("trust", "") or ""),
            stability=str(raw.get("stability", "") or ""),
            scope=str(raw.get("scope", "") or ""),
            content_hash=str(raw.get("content_hash", "") or ""),
            token_estimate=_integer(raw.get("token_estimate")),
            description=str(raw.get("description", "") or ""),
            available=bool(raw.get("available", True)),
            capabilities=_strings(raw.get("capabilities")),
            metadata=(
                cast(Mapping[str, object], raw["metadata"])
                if isinstance(raw.get("metadata"), Mapping) else {}
            ),
            contract=(
                PromptSurfaceContract.from_dict(raw_contract)
                if isinstance((raw_contract := raw.get("contract")), Mapping)
                else None
            ),
        )


@dataclass(frozen=True)
class PromptSurfaceObservation:
    """Content-light evidence that one registered surface was rendered."""

    surface_id: str
    render_count: int = 0
    first_rendered_at: float = 0.0
    last_rendered_at: float = 0.0
    last_turn_id: str = ""
    last_step: int = 0
    last_content_hash: str = ""
    last_tokens: int = 0
    contract_status: str = ""
    schema_valid: bool = False
    timed_out: bool = False
    truncated: bool = False
    redaction_applied: bool = False
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "surface_id": self.surface_id,
            "render_count": self.render_count,
            "first_rendered_at": self.first_rendered_at,
            "last_rendered_at": self.last_rendered_at,
            "last_turn_id": self.last_turn_id,
            "last_step": self.last_step,
            "last_content_hash": self.last_content_hash,
            "last_tokens": self.last_tokens,
            "contract_status": self.contract_status,
            "schema_valid": self.schema_valid,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "redaction_applied": self.redaction_applied,
            "elapsed_ms": self.elapsed_ms,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PromptSurfaceObservation":
        rendered_at = raw.get("first_rendered_at")
        last_rendered_at = raw.get("last_rendered_at")
        return cls(
            surface_id=str(raw.get("surface_id", "") or ""),
            render_count=_integer(raw.get("render_count")),
            first_rendered_at=(
                float(rendered_at)
                if isinstance(rendered_at, (int, float, str, bytes)) else 0.0),
            last_rendered_at=(
                float(last_rendered_at)
                if isinstance(last_rendered_at, (int, float, str, bytes)) else 0.0),
            last_turn_id=str(raw.get("last_turn_id", "") or ""),
            last_step=_integer(raw.get("last_step")),
            last_content_hash=str(raw.get("last_content_hash", "") or ""),
            last_tokens=_integer(raw.get("last_tokens")),
            contract_status=_text(raw.get("contract_status"), 80),
            schema_valid=bool(raw.get("schema_valid", False)),
            timed_out=bool(raw.get("timed_out", False)),
            truncated=bool(raw.get("truncated", False)),
            redaction_applied=bool(raw.get("redaction_applied", False)),
            elapsed_ms=_integer(raw.get("elapsed_ms")),
        )

    def is_current(self, content_hash: str) -> bool:
        return bool(self.last_content_hash and self.last_content_hash == content_hash)


@dataclass(frozen=True)
class PromptSurfaceSnapshot:
    """Persistable content-light view of the full Prompt Surface catalogue."""

    schema_version: int = PROMPT_SURFACE_SCHEMA_VERSION
    generation: int = 0
    surfaces: Tuple[PromptSurfaceDescriptor, ...] = ()
    observations: Tuple[PromptSurfaceObservation, ...] = ()

    @property
    def content_hash(self) -> str:
        return prompt_structure_hash({
            "schema_version": self.schema_version,
            "surfaces": [item.as_dict() for item in self.surfaces],
        })

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generation": self.generation,
            "content_hash": self.content_hash,
            "surfaces": [item.as_dict() for item in self.surfaces],
            "observations": [item.as_dict() for item in self.observations],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PromptSurfaceSnapshot":
        surfaces = raw.get("surfaces")
        observations = raw.get("observations")
        return cls(
            schema_version=_integer(raw.get("schema_version")) or 1,
            generation=_integer(raw.get("generation")),
            surfaces=tuple(
                PromptSurfaceDescriptor.from_dict(item)
                for item in (surfaces if isinstance(surfaces, (list, tuple)) else ())
                if isinstance(item, Mapping)
            ),
            observations=tuple(
                PromptSurfaceObservation.from_dict(item)
                for item in (
                    observations if isinstance(observations, (list, tuple)) else ())
                if isinstance(item, Mapping)
            ),
        )


class PromptSurfaceRegistry:
    """Read-only catalogue plus content-light render observations.

    Catalogue collections are replaced atomically so extension/MCP refreshes do
    not expose a partially rebuilt directory.  Existing observations survive a
    replacement when the same surface id remains present.
    """

    def __init__(
        self,
        collections: Optional[Mapping[str, Sequence[PromptSurfaceDescriptor]]] = None,
    ) -> None:
        self._surfaces: dict[str, PromptSurfaceDescriptor] = {}
        self._collections: dict[str, tuple[str, ...]] = {}
        self._collection_by_id: dict[str, str] = {}
        self._observations: dict[str, PromptSurfaceObservation] = {}
        self._generation = 0
        if collections:
            self.replace_collections(collections)

    @property
    def generation(self) -> int:
        return self._generation

    def replace_collections(
        self,
        collections: Mapping[str, Sequence[PromptSurfaceDescriptor]],
    ) -> None:
        normalized: dict[str, tuple[PromptSurfaceDescriptor, ...]] = {}
        next_surfaces: dict[str, PromptSurfaceDescriptor] = {}
        next_collection_by_id: dict[str, str] = {}
        for raw_name, values in collections.items():
            name = _text(raw_name, 120) or "default"
            descriptors = tuple(values)
            if not descriptors:
                normalized[name] = ()
                continue
            normalized[name] = descriptors
            for descriptor in descriptors:
                if not isinstance(descriptor, PromptSurfaceDescriptor):
                    raise TypeError("Prompt Surface collection 只能包含 descriptor")
                previous = next_surfaces.get(descriptor.id)
                if previous is not None:
                    raise ValueError(
                        f"Prompt Surface id 重复: {descriptor.id} "
                        f"({next_collection_by_id[descriptor.id]} / {name})")
                next_surfaces[descriptor.id] = descriptor
                next_collection_by_id[descriptor.id] = name

        self._surfaces = next_surfaces
        self._collections = {
            name: tuple(item.id for item in values)
            for name, values in normalized.items()
        }
        self._collection_by_id = next_collection_by_id
        self._observations = {
            key: value for key, value in self._observations.items()
            if key in self._surfaces
        }
        self._generation += 1

    def register(
        self,
        surface: PromptSurfaceDescriptor,
        *,
        collection: str = "manual",
        replace_existing: bool = False,
    ) -> None:
        existing = self._surfaces.get(surface.id)
        if existing is not None and not replace_existing:
            raise ValueError(f"Prompt Surface 已存在: {surface.id}")
        if existing is not None:
            old_collection = self._collection_by_id.get(surface.id, "manual")
            self._collections[old_collection] = tuple(
                item for item in self._collections.get(old_collection, ())
                if item != surface.id)
        self._surfaces[surface.id] = surface
        self._collection_by_id[surface.id] = collection
        ids = list(self._collections.get(collection, ()))
        if surface.id not in ids:
            ids.append(surface.id)
        self._collections[collection] = tuple(ids)
        self._generation += 1

    def get(self, surface_id: str) -> Optional[PromptSurfaceDescriptor]:
        return self._surfaces.get(str(surface_id or "").strip())

    def list(
        self,
        *,
        kind: PromptSurfaceKind | str | None = None,
        source: str = "",
        query: str = "",
        available_only: bool = False,
    ) -> Tuple[PromptSurfaceDescriptor, ...]:
        try:
            expected_kind = PromptSurfaceKind(kind) if kind else None
        except ValueError:
            expected_kind = None
        source_query = str(source or "").strip().lower()
        text_query = str(query or "").strip().lower()
        values = []
        for surface in self._surfaces.values():
            if expected_kind is not None and surface.kind is not expected_kind:
                continue
            if source_query and source_query not in surface.source.lower():
                continue
            if text_query and not any(
                text_query in item.lower()
                for item in (surface.id, _enum_text(surface.kind), surface.source,
                             surface.owner, surface.description)
            ):
                continue
            if available_only and not surface.available:
                continue
            values.append(surface)
        return tuple(sorted(
            values,
            key=lambda item: (_enum_text(item.kind), item.id),
        ))

    def observation(self, surface_id: str) -> Optional[PromptSurfaceObservation]:
        return self._observations.get(str(surface_id or "").strip())

    def observations(self) -> Tuple[PromptSurfaceObservation, ...]:
        return tuple(sorted(
            self._observations.values(), key=lambda item: item.surface_id))

    def observations_for_turn(
        self,
        turn_id: str,
    ) -> Tuple[PromptSurfaceObservation, ...]:
        """Return content-light observations rendered for one Agent turn."""
        normalized = str(turn_id or "").strip()
        if not normalized:
            return ()
        return tuple(
            item for item in self.observations()
            if item.last_turn_id == normalized
        )

    def health(self) -> Mapping[str, object]:
        """Aggregate observation and contract health without Prompt content."""
        surfaces = self.list()
        observed = tuple(
            surface for surface in surfaces
            if surface.id in self._observations)
        stale = tuple(
            surface for surface in observed
            if not self._observations[surface.id].is_current(
                surface.content_hash))
        unavailable = tuple(surface for surface in surfaces if not surface.available)
        contract_failures = tuple(
            observation for observation in self._observations.values()
            if observation.contract_status not in ("", "ok"))
        render_count = sum(
            observation.render_count for observation in self._observations.values())
        by_kind: dict[str, dict[str, int]] = {}
        for kind in PromptSurfaceKind:
            selected = tuple(surface for surface in surfaces if surface.kind is kind)
            if not selected:
                continue
            selected_observed = tuple(
                surface for surface in selected
                if surface.id in self._observations)
            selected_stale = tuple(
                surface for surface in selected_observed
                if not self._observations[surface.id].is_current(
                    surface.content_hash))
            selected_contract_failures = tuple(
                surface for surface in selected
                if (
                    surface.id in self._observations
                    and self._observations[surface.id].contract_status
                    not in ("", "ok")
                )
            )
            by_kind[kind.value] = {
                "surfaces": len(selected),
                "observed": len(selected_observed),
                "unobserved": len(selected) - len(selected_observed),
                "stale": len(selected_stale),
                "unavailable": sum(1 for surface in selected if not surface.available),
                "contract_failures": len(selected_contract_failures),
            }
        return {
            "surfaces": len(surfaces),
            "observed": len(observed),
            "unobserved": len(surfaces) - len(observed),
            "stale": len(stale),
            "unavailable": len(unavailable),
            "contract_failures": len(contract_failures),
            "render_count": render_count,
            "by_kind": by_kind,
        }

    def observe(
        self,
        surface_id: str,
        *,
        content_hash: str = "",
        tokens: int = 0,
        turn_id: str = "",
        step: int = 0,
        rendered_at: Optional[float] = None,
        contract_status: str = "",
        schema_valid: bool = False,
        timed_out: bool = False,
        truncated: bool = False,
        redaction_applied: bool = False,
        elapsed_ms: int = 0,
    ) -> bool:
        surface = self.get(surface_id)
        if surface is None:
            return False
        now = float(rendered_at if rendered_at is not None else time.time())
        previous = self._observations.get(surface.id)
        self._observations[surface.id] = PromptSurfaceObservation(
            surface_id=surface.id,
            render_count=(previous.render_count if previous else 0) + 1,
            first_rendered_at=(
                previous.first_rendered_at if previous and previous.first_rendered_at else now),
            last_rendered_at=now,
            last_turn_id=_text(turn_id, 160),
            last_step=_integer(step),
            last_content_hash=(
                _text(content_hash, 128) if content_hash else surface.content_hash),
            last_tokens=_integer(tokens) if tokens else surface.token_estimate,
            contract_status=_text(contract_status, 80),
            schema_valid=bool(schema_valid),
            timed_out=bool(timed_out),
            truncated=bool(truncated),
            redaction_applied=bool(redaction_applied),
            elapsed_ms=_integer(elapsed_ms),
        )
        return True

    def collection_for(self, surface_id: str) -> str:
        return self._collection_by_id.get(str(surface_id or "").strip(), "")

    def collections(self) -> Mapping[str, Tuple[str, ...]]:
        return {
            name: tuple(ids) for name, ids in sorted(self._collections.items())
        }

    def kind_counts(self) -> Mapping[str, int]:
        counts: dict[str, int] = {}
        for surface in self._surfaces.values():
            counts[_enum_text(surface.kind)] = counts.get(_enum_text(surface.kind), 0) + 1
        return dict(sorted(counts.items()))

    def role_counts(self) -> Mapping[str, int]:
        counts: dict[str, int] = {}
        for surface in self._surfaces.values():
            counts[_enum_text(surface.role)] = counts.get(_enum_text(surface.role), 0) + 1
        return dict(sorted(counts.items()))

    def trust_counts(self) -> Mapping[str, int]:
        counts: dict[str, int] = {}
        for surface in self._surfaces.values():
            counts[surface.trust] = counts.get(surface.trust, 0) + 1
        return dict(sorted(counts.items()))

    def snapshot(self) -> PromptSurfaceSnapshot:
        return PromptSurfaceSnapshot(
            generation=self._generation,
            surfaces=self.list(),
            observations=self.observations(),
        )

    def summary_lines(self) -> List[str]:
        health = self.health()
        kinds = " · ".join(
            f"{name} {count}" for name, count in self.kind_counts().items()
        ) or "（空）"
        roles = " · ".join(
            f"{name} {count}" for name, count in self.role_counts().items()
        ) or "（空）"
        return [
            f"  [prompts] {health['surfaces']} surfaces"
            f" · observed {health['observed']}"
            f" · stale {health['stale']}"
            f" · generation {self._generation}",
            f"  kinds: {kinds}",
            f"  roles: {roles} · renders {health['render_count']}"
            f" · contract_failures {health['contract_failures']}"
            " · content-light=true",
        ]


def main_prompt_surface(
    system_prompt: str,
    *,
    prompt_snapshot: object | None = None,
    prompt_plan: object | None = None,
    profile: str = "",
    release: str = "",
    provider: str = "",
    model: str = "",
) -> PromptSurfaceDescriptor:
    """Describe the current main Agent request shell without its body."""
    source = prompt_plan or prompt_snapshot
    cache = getattr(source, "cache", None)
    fragments = tuple(getattr(source, "fragments", ()) or ())
    stable_ids = set(getattr(cache, "stable_fragments", ()) or ())
    stable = sum(1 for item in fragments if getattr(item, "id", "") in stable_ids)
    content_hash = str(getattr(cache, "full_hash", "") or "")
    if not content_hash:
        content_hash = prompt_content_hash(system_prompt)
    token_estimate = _integer(getattr(source, "total_tokens", 0))
    if not token_estimate and prompt_plan is not None:
        token_estimate = sum(
            _integer(getattr(fragment, "tokens", 0)) for fragment in fragments)
    if not token_estimate:
        token_estimate = estimate_text_tokens(system_prompt)
    return PromptSurfaceDescriptor(
        id="main:agent",
        kind=PromptSurfaceKind.MAIN_AGENT,
        source="agent-loop",
        owner="AgentLoop/PromptAssembler",
        version=release,
        role=SourceRole.INSTRUCTION,
        authority=InstructionSource.MANAGED.value,
        trust="managed",
        stability="session",
        scope="managed",
        content_hash=content_hash,
        token_estimate=token_estimate,
        description="Main Agent system shell assembled from L0-L10 fragments.",
        capabilities=("tools", "history", "working_context", "evidence_channel"),
        metadata={
            "profile": profile,
            "provider": provider,
            "model": model,
            "fragments": len(fragments),
            "stable_fragments": stable,
            "dynamic_fragments": max(0, len(fragments) - stable),
            "cache_boundary": _integer(getattr(cache, "offset", 0)),
            "stable_hash": str(getattr(cache, "stable_hash", "") or ""),
            "dynamic_hash": str(getattr(cache, "dynamic_hash", "") or ""),
        },
    )


def update_observation(
    registry: PromptSurfaceRegistry | None,
    surface_id: str,
    *,
    content_hash: str = "",
    tokens: int = 0,
    turn_id: str = "",
    step: int = 0,
    rendered_at: Optional[float] = None,
    contract_status: str = "",
    schema_valid: bool = False,
    timed_out: bool = False,
    truncated: bool = False,
    redaction_applied: bool = False,
    elapsed_ms: int = 0,
) -> bool:
    """Safe helper for runtime instrumentation; never affects Provider flow."""
    if registry is None:
        return False
    try:
        return registry.observe(
            surface_id,
            content_hash=content_hash,
            tokens=tokens,
            turn_id=turn_id,
            step=step,
            rendered_at=rendered_at,
            contract_status=contract_status,
            schema_valid=schema_valid,
            timed_out=timed_out,
            truncated=truncated,
            redaction_applied=redaction_applied,
            elapsed_ms=elapsed_ms,
        )
    except Exception:
        return False


__all__ = [
    "CORE_SURFACE_KINDS",
    "PROMPT_SURFACE_SCHEMA_VERSION",
    "PromptSurfaceDescriptor",
    "PromptSurfaceKind",
    "PromptSurfaceObservation",
    "PromptSurfaceRegistry",
    "PromptSurfaceSnapshot",
    "main_prompt_surface",
    "prompt_content_hash",
    "prompt_structure_hash",
    "update_observation",
]
