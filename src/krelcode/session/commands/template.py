"""Standard, content-light contracts for slash-command prompt rendering.

The registry answers *which* command is available.  ``CommandTemplate`` answers
*how* a prompt-producing command crosses the boundary into an Agent turn:
argument policy, provenance, trust, tool allowlist, reminder declarations and a
stable contract hash.  It deliberately contains no terminal or Agent imports.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Mapping, Optional, Tuple

from .types import CommandSource, CommandSpec

COMMAND_TEMPLATE_VERSION = 1
DEFAULT_MAX_ARGUMENT_CHARS = 12_000

TRUST_MANAGED = "managed"
TRUST_USER = "user"
TRUST_PROJECT = "project"
TRUST_UNTRUSTED = "untrusted"
COMMAND_TRUST_LEVELS = (
    TRUST_MANAGED,
    TRUST_USER,
    TRUST_PROJECT,
    TRUST_UNTRUSTED,
)

REMINDER_COMMAND_SCOPE = "command:scoped-instructions"
REMINDER_COMMAND_UNTRUSTED = "command:untrusted-instructions"
REMINDER_COMMAND_TOOL_SCOPE = "command:tool-scope"

_REMINDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}$")


class CommandArgumentMode(str, Enum):
    """How a prompt template consumes user arguments."""

    NONE = "none"
    RAW = "raw"
    NAMED = "named"
    PASSTHROUGH = "passthrough"


class CommandEnvelope(str, Enum):
    """The boundary marker emitted around prompt-producing command content."""

    NONE = "none"
    CUSTOM_COMMAND = "custom_command"
    PLUGIN_COMMAND = "plugin_command"
    COMMAND_INVOCATION = "command_invocation"


@dataclass(frozen=True)
class CommandTemplate:
    """Provider-neutral rendering contract for one prompt command."""

    source: CommandSource
    trust: str = TRUST_MANAGED
    envelope: CommandEnvelope = CommandEnvelope.NONE
    argument_mode: CommandArgumentMode = CommandArgumentMode.NONE
    allowed_tools: Tuple[str, ...] = ()
    reminder_ids: Tuple[str, ...] = ()
    max_argument_chars: int = DEFAULT_MAX_ARGUMENT_CHARS
    version: int = COMMAND_TEMPLATE_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", CommandSource(self.source))
        object.__setattr__(self, "envelope", CommandEnvelope(self.envelope))
        object.__setattr__(
            self, "argument_mode", CommandArgumentMode(self.argument_mode))
        if self.trust not in COMMAND_TRUST_LEVELS:
            raise ValueError(f"未知命令信任级别: {self.trust}")
        if self.version != COMMAND_TEMPLATE_VERSION:
            raise ValueError(f"不支持的 CommandTemplate 版本: {self.version}")
        if self.max_argument_chars < 1:
            raise ValueError("max_argument_chars 必须大于 0")
        tools = _unique_names(self.allowed_tools)
        reminders = _stable_names(self.reminder_ids)
        for reminder_id in reminders:
            if not _REMINDER_ID_RE.fullmatch(reminder_id):
                raise ValueError(f"非法 reminder id: {reminder_id!r}")
        object.__setattr__(self, "allowed_tools", tools)
        object.__setattr__(self, "reminder_ids", reminders)

    @classmethod
    def for_source(
        cls,
        source: CommandSource,
        *,
        trust: str = "",
        scope: str = "",
        allowed_tools: Tuple[str, ...] = (),
        reminder_ids: Tuple[str, ...] = (),
    ) -> "CommandTemplate":
        """Build the default contract for a command source."""
        source = CommandSource(source)
        resolved_trust = trust or trust_for_source(source, scope=scope)
        envelope, argument_mode = _default_boundary(source)
        automatic: list[str] = []
        if source is CommandSource.CUSTOM or source is CommandSource.PLUGIN:
            automatic.append(REMINDER_COMMAND_SCOPE)
        elif source is CommandSource.SKILL and resolved_trust != TRUST_MANAGED:
            automatic.append(REMINDER_COMMAND_SCOPE)
        elif source is CommandSource.MCP:
            automatic.append(REMINDER_COMMAND_UNTRUSTED)
        if allowed_tools:
            automatic.append(REMINDER_COMMAND_TOOL_SCOPE)
        automatic.extend(reminder_ids)
        return cls(
            source=source,
            trust=resolved_trust,
            envelope=envelope,
            argument_mode=argument_mode,
            allowed_tools=tuple(allowed_tools),
            reminder_ids=tuple(automatic),
        )

    @classmethod
    def from_spec(cls, spec: CommandSpec) -> "CommandTemplate":
        """Read the declared contract, falling back for legacy registrations."""
        raw = spec.metadata.get("command_template")
        if isinstance(raw, Mapping):
            return cls(
                source=CommandSource(raw.get("source", spec.source.value)),
                trust=str(raw.get("trust") or trust_for_source(spec.source)),
                envelope=CommandEnvelope(raw.get("envelope", "none")),
                argument_mode=CommandArgumentMode(
                    raw.get("argument_mode", "none")),
                allowed_tools=tuple(raw.get("allowed_tools") or ()),
                reminder_ids=tuple(raw.get("reminder_ids") or ()),
                max_argument_chars=max(1, int(
                    raw.get("max_argument_chars")
                    or DEFAULT_MAX_ARGUMENT_CHARS)),
                version=int(raw.get("version") or COMMAND_TEMPLATE_VERSION),
            )
        scope = str(spec.metadata.get("scope", "") or "")
        allowed_tools = tuple(spec.metadata.get("allowed_tools") or ())
        reminders = tuple(spec.metadata.get("reminder_ids") or ())
        return cls.for_source(
            spec.source,
            scope=scope,
            allowed_tools=allowed_tools,
            reminder_ids=reminders,
        )

    def bind(
        self,
        *,
        allowed_tools: Optional[Tuple[str, ...]] = None,
        reminder_ids: Optional[Tuple[str, ...]] = None,
        argument_mode: Optional[CommandArgumentMode] = None,
    ) -> "CommandTemplate":
        """Return a copy with concrete command discovery declarations bound."""
        tools = self.allowed_tools if allowed_tools is None else tuple(allowed_tools)
        reminders = list(self.reminder_ids if reminder_ids is None else reminder_ids)
        if tools:
            reminders.append(REMINDER_COMMAND_TOOL_SCOPE)
        if self.source is CommandSource.MCP:
            reminders.append(REMINDER_COMMAND_UNTRUSTED)
        elif self.source in {
            CommandSource.CUSTOM,
            CommandSource.PLUGIN,
        } or (
            self.source is CommandSource.SKILL
            and self.trust != TRUST_MANAGED
        ):
            reminders.append(REMINDER_COMMAND_SCOPE)
        return replace(
            self,
            allowed_tools=tools,
            reminder_ids=tuple(reminders),
            argument_mode=(
                self.argument_mode
                if argument_mode is None else argument_mode
            ),
        )

    def render(
        self,
        *,
        name: str,
        body: str,
        arguments: str = "",
        provenance: Optional[Mapping[str, object]] = None,
        attributes: Optional[Mapping[str, object]] = None,
    ) -> "CommandRenderResult":
        """Render one command body under the fixed boundary contract."""
        raw_arguments = str(arguments or "").strip()
        if len(raw_arguments) > self.max_argument_chars:
            raise ValueError(
                f"命令参数过长（{len(raw_arguments)} > "
                f"{self.max_argument_chars} chars）")

        content = _apply_arguments(
            str(body or ""), raw_arguments, self.argument_mode)
        if not content.strip():
            raise ValueError("命令 Prompt 正文不能为空")
        rendered = _wrap_envelope(
            self.envelope,
            name=name,
            source=self.source.value,
            trust=self.trust,
            allowed_tools=self.allowed_tools,
            content=content,
            attributes=attributes or {},
        )
        metadata = {
            "type": "command_template",
            "version": self.version,
            "source": self.source.value,
            "trust": self.trust,
            "envelope": self.envelope.value,
            "argument_mode": self.argument_mode.value,
            "allowed_tools": list(self.allowed_tools),
            "reminder_ids": list(self.reminder_ids),
            "template_hash": self.template_hash,
        }
        if provenance:
            metadata.update({
                str(key): value
                for key, value in provenance.items()
                if _safe_provenance_value(value)
            })
        return CommandRenderResult(
            prompt=rendered,
            tool_allowlist=self.allowed_tools or None,
            reminder_ids=self.reminder_ids,
            template_hash=self.template_hash,
            source=self.source,
            trust=self.trust,
            metadata=metadata,
        )

    @property
    def template_hash(self) -> str:
        payload = json.dumps(
            self.as_record(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_record(self) -> dict[str, object]:
        """Stable content-free contract record used by diagnostics/hashes."""
        return {
            "version": self.version,
            "source": self.source.value,
            "trust": self.trust,
            "envelope": self.envelope.value,
            "argument_mode": self.argument_mode.value,
            "allowed_tools": list(self.allowed_tools),
            "reminder_ids": list(self.reminder_ids),
            "max_argument_chars": self.max_argument_chars,
        }


@dataclass(frozen=True)
class CommandRenderResult:
    """Rendered command prompt plus its content-light execution contract."""

    prompt: str
    tool_allowlist: Optional[Tuple[str, ...]]
    reminder_ids: Tuple[str, ...]
    template_hash: str
    source: CommandSource
    trust: str
    metadata: Mapping[str, object]

    @property
    def contract_record(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "trust": self.trust,
            "tool_allowlist": list(self.tool_allowlist or ()),
            "reminder_ids": list(self.reminder_ids),
            "template_hash": self.template_hash,
        }


def trust_for_source(source: CommandSource, *, scope: str = "") -> str:
    source = CommandSource(source)
    normalized_scope = str(scope or "").strip().lower()
    if source is CommandSource.BUILTIN:
        return TRUST_MANAGED
    if source is CommandSource.MCP:
        return TRUST_UNTRUSTED
    if source is CommandSource.PLUGIN:
        return TRUST_PROJECT if normalized_scope == "project" else TRUST_USER
    if source is CommandSource.CUSTOM:
        if normalized_scope == "project":
            return TRUST_PROJECT
        return TRUST_USER
    if source is CommandSource.SKILL:
        if normalized_scope.startswith("builtin"):
            return TRUST_MANAGED
        if normalized_scope.startswith(("project", "plugin:project")):
            return TRUST_PROJECT
        return TRUST_USER
    return TRUST_USER


def split_reminder_ids(value: object) -> Tuple[str, ...]:
    """Parse frontmatter reminder declarations without accepting free text."""
    if value is None:
        return ()
    if isinstance(value, str):
        items = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = [str(item) for item in value]
    else:
        items = [str(value)]
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if not _REMINDER_ID_RE.fullmatch(text):
            raise ValueError(f"非法 reminder id: {text!r}")
        if text not in result:
            result.append(text)
    return tuple(result)


def command_template_metadata(template: CommandTemplate) -> dict[str, object]:
    """Project a template into ``CommandSpec.metadata``."""
    return {"command_template": template.as_record()}


def _default_boundary(
    source: CommandSource,
) -> tuple[CommandEnvelope, CommandArgumentMode]:
    if source is CommandSource.CUSTOM:
        return CommandEnvelope.CUSTOM_COMMAND, CommandArgumentMode.RAW
    if source is CommandSource.PLUGIN:
        return CommandEnvelope.PLUGIN_COMMAND, CommandArgumentMode.RAW
    if source is CommandSource.SKILL:
        return CommandEnvelope.COMMAND_INVOCATION, CommandArgumentMode.RAW
    if source is CommandSource.MCP:
        return (
            CommandEnvelope.COMMAND_INVOCATION,
            CommandArgumentMode.PASSTHROUGH,
        )
    return CommandEnvelope.NONE, CommandArgumentMode.NONE


def _apply_arguments(
    body: str,
    arguments: str,
    mode: CommandArgumentMode,
) -> str:
    if mode in {CommandArgumentMode.NONE, CommandArgumentMode.NAMED}:
        return body.strip()
    if mode is CommandArgumentMode.PASSTHROUGH:
        # The source adapter already consumed arguments into structured text.
        return body.strip()
    has_placeholder = (
        "$ARGUMENTS" in body or "${ARGUMENTS}" in body)
    text = body.replace("${ARGUMENTS}", arguments).replace(
        "$ARGUMENTS", arguments)
    if arguments and not has_placeholder:
        text = text.rstrip() + "\n\n## User arguments\n\n" + arguments
    return text.strip()


def _wrap_envelope(
    envelope: CommandEnvelope,
    *,
    name: str,
    source: str,
    trust: str,
    allowed_tools: Tuple[str, ...],
    content: str,
    attributes: Mapping[str, object],
) -> str:
    safe_name = html.escape(str(name), quote=True)
    safe_source = html.escape(str(source), quote=True)
    safe_trust = html.escape(str(trust), quote=True)
    safe_tools = html.escape(",".join(allowed_tools), quote=True)
    body = content.rstrip()
    if envelope is CommandEnvelope.NONE:
        return body
    if envelope is CommandEnvelope.CUSTOM_COMMAND:
        return (
            f'<custom_command name="{safe_name}" source="{safe_source}" '
            f'trust="{safe_trust}" instruction="false">\n'
            f"{body}\n</custom_command>"
        )
    if envelope is CommandEnvelope.PLUGIN_COMMAND:
        plugin_name = html.escape(
            str(attributes.get("plugin") or name), quote=True)
        local_name = html.escape(
            str(attributes.get("name") or name), quote=True)
        return (
            f'<plugin_command plugin="{plugin_name}" name="{local_name}" '
            f'trust="{safe_trust}" instruction="false">\n'
            f"{body}\n</plugin_command>"
        )
    return (
        f'<command_invocation name="{safe_name}" source="{safe_source}" '
        f'trust="{safe_trust}" instruction="false" '
        f'allowed_tools="{safe_tools}">\n{body}\n</command_invocation>'
    )


def _unique_names(values: Tuple[str, ...] | list[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(
        str(value).strip() for value in values if str(value).strip()
    ))


def _stable_names(values: Tuple[str, ...] | list[str]) -> Tuple[str, ...]:
    return tuple(sorted(_unique_names(values)))


def _safe_provenance_value(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, (list, tuple)):
        return all(isinstance(item, (str, int, float, bool)) for item in value)
    return False


__all__ = [
    "COMMAND_TEMPLATE_VERSION",
    "COMMAND_TRUST_LEVELS",
    "CommandArgumentMode",
    "CommandEnvelope",
    "CommandRenderResult",
    "CommandTemplate",
    "REMINDER_COMMAND_SCOPE",
    "REMINDER_COMMAND_TOOL_SCOPE",
    "REMINDER_COMMAND_UNTRUSTED",
    "TRUST_MANAGED",
    "TRUST_PROJECT",
    "TRUST_UNTRUSTED",
    "TRUST_USER",
    "command_template_metadata",
    "split_reminder_ids",
    "trust_for_source",
]
