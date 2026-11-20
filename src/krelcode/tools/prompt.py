"""Unified model-facing contract for one tool.

``ToolPromptSpec`` is the canonical source for everything the model may see
about a tool: name, provider description, JSON Schema, tool-specific operating
guidance and examples.  Runtime routing (source, permission, tier, run mode)
stays in ``ToolDescriptor``; provider adapters only translate this spec into
their wire format.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Mapping, Optional, Tuple


TOOL_PROMPT_SPEC_VERSION = "1"


def _string_tuple(value: object) -> Tuple[str, ...]:
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    values = (
        str(item).strip()
        for item in value
        if str(item).strip()
    )
    return tuple(dict.fromkeys(values))


def _json_copy(value: object) -> object:
    """Copy schema-like data without retaining caller-owned mutable values."""
    if isinstance(value, Mapping):
        return {str(key): _json_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_copy(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _examples(value: object) -> Tuple["ToolPromptExample", ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    normalized: list[ToolPromptExample] = []
    for item in value:
        if isinstance(item, ToolPromptExample):
            normalized.append(item)
        elif isinstance(item, Mapping):
            normalized.append(ToolPromptExample(
                title=str(item.get("title", "") or ""),
                input=item.get("input") or {},
                description=str(item.get("description", "") or ""),
            ))
        else:
            raise ValueError("ToolPromptSpec examples 必须包含对象")
    return tuple(normalized)


@dataclass(frozen=True)
class ToolPromptExample:
    """One model-facing call example with a content-light observability view."""

    title: str
    input: Mapping[str, object]
    description: str = ""

    def __post_init__(self) -> None:
        title = str(self.title or "").strip()
        if not title:
            raise ValueError("tool prompt example 必须包含非空 title")
        if not isinstance(self.input, Mapping):
            raise ValueError("tool prompt example.input 必须是对象")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", str(self.description or "").strip())
        object.__setattr__(self, "input", _json_copy(self.input))

    @property
    def input_hash(self) -> str:
        return _hash(self.input)

    def as_record(self) -> dict[str, object]:
        return {
            "title": self.title,
            "description_chars": len(self.description),
            "input_keys": tuple(sorted(str(key) for key in self.input)),
            "input_hash": self.input_hash,
        }


@dataclass(frozen=True)
class ToolPromptSpec:
    """Canonical model-facing definition for one tool."""

    name: str
    description: str
    parameters: Mapping[str, object]
    instructions: str = ""
    when_to_use: Tuple[str, ...] = ()
    when_not_to_use: Tuple[str, ...] = ()
    examples: Tuple[ToolPromptExample, ...] = ()
    aliases: Tuple[str, ...] = ()
    source: str = ""
    version: str = TOOL_PROMPT_SPEC_VERSION
    playbook_id: str = ""
    playbook_title: str = ""
    playbook_priority: int = 0
    playbook_version: str = ""

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        description = str(self.description or "").strip()
        if not name:
            raise ValueError("ToolPromptSpec 必须包含非空 name")
        if not description:
            raise ValueError(f"ToolPromptSpec {name} 必须包含非空 description")
        if not isinstance(self.parameters, Mapping):
            raise ValueError(f"ToolPromptSpec {name}.parameters 必须是 JSON 对象")
        schema_type = self.parameters.get("type")
        if schema_type not in (None, "object"):
            raise ValueError(f"ToolPromptSpec {name}.parameters.type 必须是 object")
        examples = _examples(self.examples)
        try:
            priority = int(self.playbook_priority or 0)
        except (TypeError, ValueError):
            priority = 0
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "parameters", _json_copy(self.parameters))
        object.__setattr__(
            self, "instructions", str(self.instructions or "").strip())
        object.__setattr__(self, "when_to_use", _string_tuple(self.when_to_use))
        object.__setattr__(
            self, "when_not_to_use", _string_tuple(self.when_not_to_use))
        object.__setattr__(self, "examples", examples)
        object.__setattr__(self, "aliases", tuple(
            item.casefold() for item in _string_tuple(self.aliases)))
        object.__setattr__(self, "source", str(self.source or "").strip())
        object.__setattr__(
            self, "version", str(self.version or TOOL_PROMPT_SPEC_VERSION).strip())
        object.__setattr__(
            self, "playbook_id", str(self.playbook_id or "").strip())
        object.__setattr__(
            self, "playbook_title", str(self.playbook_title or "").strip())
        object.__setattr__(self, "playbook_priority", priority)
        object.__setattr__(
            self, "playbook_version", str(self.playbook_version or "").strip())

    @classmethod
    def from_tool(
        cls,
        tool: object,
        *,
        source: str = "",
        fallback_description: Optional[str] = None,
    ) -> "ToolPromptSpec":
        """Build a spec from a tool, honoring an optional explicit override."""
        name = str(getattr(tool, "name", "") or "").strip()
        description = str(
            getattr(tool, "description", None)
            or fallback_description
            or ""
        ).strip()
        parameters = getattr(tool, "parameters", None)
        base = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "instructions": str(getattr(tool, "prompt", "") or ""),
            "when_to_use": getattr(tool, "when_to_use", ()),
            "when_not_to_use": getattr(tool, "when_not_to_use", ()),
            "examples": getattr(tool, "prompt_examples", ()),
            "aliases": getattr(tool, "aliases", ()),
            "source": source,
            "version": getattr(tool, "prompt_version", TOOL_PROMPT_SPEC_VERSION),
        }
        override = getattr(tool, "prompt_spec", None)
        if isinstance(override, cls):
            if override.name != name:
                raise ValueError(
                    f"ToolPromptSpec name {override.name!r} 与 Tool.name {name!r} 不一致")
            return override
        if isinstance(override, Mapping):
            for key in tuple(base):
                if key in override and override[key] not in (None, "", (), []):
                    base[key] = override[key]
        return cls(**base)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "ToolPromptSpec":
        """Normalize a legacy ``{name, description, parameters}`` mapping."""
        if not isinstance(value, Mapping):
            raise ValueError("ToolPromptSpec mapping 必须是对象")
        return cls(
            name=str(value.get("name", "") or ""),
            description=str(value.get("description", "") or ""),
            parameters=value.get("parameters") or value.get("input_schema") or {},
            instructions=str(value.get("instructions", "") or ""),
            when_to_use=_string_tuple(value.get("when_to_use", ())),
            when_not_to_use=_string_tuple(value.get("when_not_to_use", ())),
            examples=tuple(value.get("examples", ()) or ()),
            aliases=_string_tuple(value.get("aliases", ())),
            source=str(value.get("source", "") or ""),
            version=str(
                value.get("version", TOOL_PROMPT_SPEC_VERSION)
                or TOOL_PROMPT_SPEC_VERSION),
            playbook_id=str(value.get("playbook_id", "") or ""),
            playbook_title=str(value.get("playbook_title", "") or ""),
            playbook_priority=value.get("playbook_priority", 0),
            playbook_version=str(value.get("playbook_version", "") or ""),
        )

    @property
    def wire_hash(self) -> str:
        """Hash only bytes that providers serialize in their tool catalogue."""
        return _hash(self.as_tool_spec())

    @property
    def content_hash(self) -> str:
        """Hash every model-visible prompt field, including operating guidance."""
        return _hash({
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "instructions": self.instructions,
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "examples": [
                {"title": item.title, "input": item.input,
                 "description": item.description}
                for item in self.examples
            ],
            "aliases": self.aliases,
            "source": self.source,
            "version": self.version,
            "playbook_id": self.playbook_id,
            "playbook_title": self.playbook_title,
            "playbook_priority": self.playbook_priority,
            "playbook_version": self.playbook_version,
        })

    def with_playbook(
        self,
        *,
        playbook_id: str,
        playbook_title: str,
        playbook_priority: int,
        playbook_version: str,
        fallback_instructions: str = "",
    ) -> "ToolPromptSpec":
        """Attach family metadata and append catalog guidance once."""
        instructions = self.instructions
        fallback = str(fallback_instructions or "").strip()
        if fallback and fallback not in instructions:
            instructions = "\n\n".join(item for item in (instructions, fallback) if item)
        return replace(
            self,
            instructions=instructions,
            playbook_id=str(playbook_id or "").strip(),
            playbook_title=str(playbook_title or "").strip(),
            playbook_priority=playbook_priority,
            playbook_version=str(playbook_version or "").strip(),
        )

    def as_tool_spec(self) -> dict[str, object]:
        """Return the provider-neutral legacy tool spec derived from this source."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": _json_copy(self.parameters),
        }

    def to_dict(self) -> dict[str, object]:
        """Compatibility hook used by prompt-catalog hashing."""
        return self.as_tool_spec()

    def as_record(self) -> dict[str, object]:
        """Content-free diagnostics safe for logs, sessions and SDK events."""
        schema = self.parameters
        properties = schema.get("properties")
        required = schema.get("required")
        return {
            "name": self.name,
            "source": self.source,
            "version": self.version,
            "description_chars": len(self.description),
            "schema_chars": len(json.dumps(
                schema, ensure_ascii=False, sort_keys=True, default=str)),
            "parameter_count": len(properties) if isinstance(properties, Mapping) else 0,
            "required_parameter_count": (
                len(required) if isinstance(required, (list, tuple)) else 0),
            "instructions_chars": len(self.instructions),
            "when_to_use_count": len(self.when_to_use),
            "when_not_to_use_count": len(self.when_not_to_use),
            "example_count": len(self.examples),
            "example_records": [item.as_record() for item in self.examples],
            "aliases": self.aliases,
            "playbook_id": self.playbook_id,
            "playbook_title": self.playbook_title,
            "playbook_version": self.playbook_version,
            "wire_hash": self.wire_hash,
            "content_hash": self.content_hash,
        }


__all__ = [
    "TOOL_PROMPT_SPEC_VERSION",
    "ToolPromptExample",
    "ToolPromptSpec",
]
