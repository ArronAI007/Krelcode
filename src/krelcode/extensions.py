"""Unified extension provenance registry.

The runtime keeps specialised registries for tools, commands, skills, agents,
hooks and MCP servers. ``ExtensionRegistry`` does not execute or replace them; it
is the cross-cutting catalogue that answers:

* which extension components exist for this process;
* which package/scope/path supplied each component;
* whether a component is enabled;
* which lower-priority declaration was shadowed.

Records intentionally contain metadata only. Tool schemas, prompts, hook commands
and MCP headers stay in their owning registries and never enter this catalogue.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Tuple


class ExtensionKind(str, Enum):
    PLUGIN = "plugin"
    TOOL = "tool"
    SKILL = "skill"
    AGENT = "agent"
    COMMAND = "command"
    HOOK = "hook"
    MCP = "mcp"


@dataclass(frozen=True)
class ExtensionRecord:
    """One provenance-aware extension component."""

    id: str
    kind: ExtensionKind
    name: str
    source: str
    origin: str = ""
    scope: str = ""
    plugin: str = ""
    version: str = ""
    enabled: bool = True
    priority: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        package = f" plugin={self.plugin}" if self.plugin else ""
        version = f"@{self.version}" if self.version else ""
        scope = f" scope={self.scope}" if self.scope else ""
        state = "" if self.enabled else " disabled"
        return (
            f"{self.kind.value}:{self.name} "
            f"source={self.source}{package}{version}{scope}{state}"
        )


@dataclass(frozen=True)
class ExtensionConflict:
    key: str
    kept: ExtensionRecord
    shadowed: ExtensionRecord


class ExtensionRegistry:
    """Deterministic, metadata-only catalogue for all extension surfaces."""

    def __init__(self, records: Iterable[ExtensionRecord] = ()) -> None:
        self._records: dict[str, ExtensionRecord] = {}
        self._conflicts: list[ExtensionConflict] = []
        for record in records:
            self.register(record)

    @staticmethod
    def key(kind: ExtensionKind | str, name: str) -> str:
        normalized_kind = (
            kind.value if isinstance(kind, ExtensionKind)
            else str(kind).strip().lower()
        )
        return f"{normalized_kind}:{str(name).strip()}"

    @property
    def conflicts(self) -> Tuple[ExtensionConflict, ...]:
        return tuple(self._conflicts)

    def clear(self) -> None:
        self._records.clear()
        self._conflicts.clear()

    def register(self, record: ExtensionRecord) -> bool:
        """Register one record; return True when it became the visible entry."""

        if not record.id or not record.name:
            raise ValueError("extension id/name 不能为空")
        key = self.key(record.kind, record.id)
        current = self._records.get(key)
        if current is None:
            self._records[key] = record
            return True
        kept, shadowed = (
            (record, current)
            if record.priority >= current.priority
            else (current, record)
        )
        self._records[key] = kept
        self._conflicts.append(ExtensionConflict(key, kept, shadowed))
        return kept is record

    def register_many(self, records: Iterable[ExtensionRecord]) -> None:
        for record in records:
            self.register(record)

    def remove_kind(self, kind: ExtensionKind | str) -> None:
        normalized = (
            kind.value if isinstance(kind, ExtensionKind)
            else str(kind).strip().lower()
        )
        self._records = {
            key: record for key, record in self._records.items()
            if record.kind.value != normalized
        }

    def list(
        self,
        kind: Optional[ExtensionKind | str] = None,
        *,
        include_disabled: bool = True,
        source: str = "",
    ) -> Tuple[ExtensionRecord, ...]:
        normalized_kind = None
        if kind is not None:
            normalized_kind = (
                kind.value if isinstance(kind, ExtensionKind)
                else str(kind).strip().lower()
            )
        query = source.strip().lower()
        items = [
            record for record in self._records.values()
            if (normalized_kind is None or record.kind.value == normalized_kind)
            and (include_disabled or record.enabled)
            and (not query or query in record.source.lower()
                 or query in record.plugin.lower())
        ]
        return tuple(sorted(
            items,
            key=lambda item: (
                item.kind.value,
                item.name.lower(),
                item.source.lower(),
                item.id,
            ),
        ))

    def get(
        self, kind: ExtensionKind | str, name: str,
    ) -> Optional[ExtensionRecord]:
        return self._records.get(self.key(kind, name))

    def counts(
        self, *, include_disabled: bool = True,
    ) -> Mapping[ExtensionKind, int]:
        counter = Counter(
            record.kind for record in self.list(
                include_disabled=include_disabled)
        )
        return {
            kind: counter.get(kind, 0)
            for kind in ExtensionKind
            if counter.get(kind, 0)
        }

    def status_lines(
        self,
        kind: Optional[ExtensionKind | str] = None,
        *,
        source: str = "",
    ) -> Tuple[str, ...]:
        records = self.list(kind=kind, source=source)
        counts = Counter(record.kind for record in records)
        summary = " · ".join(
            f"{item.value}={counts.get(item, 0)}"
            for item in ExtensionKind if counts.get(item, 0)
        ) or "0"
        conflicts = tuple(
            conflict for conflict in self.conflicts
            if any(record.id == conflict.kept.id for record in records)
        )
        filter_text = ""
        if kind is not None:
            filter_text += f" · kind={getattr(kind, 'value', kind)}"
        if source:
            filter_text += f" · source={source}"
        lines = [
            f"  [extensions] {len(records)} 个组件 · {summary}"
            f" · {len(conflicts)} 个来源冲突{filter_text}"
        ]
        for record in records:
            state = "enabled" if record.enabled else "disabled"
            detail = (
                f"  - [{record.kind.value}] {record.name} · {record.source}"
                f" · {state}"
            )
            if record.plugin:
                detail += f" · plugin={record.plugin}"
                if record.version:
                    detail += f"@{record.version}"
            if record.scope:
                detail += f" · scope={record.scope}"
            if record.origin:
                detail += f" · {record.origin}"
            lines.append(detail)
        for conflict in conflicts:
            lines.append(
                f"  ! {conflict.key}: 保留 {conflict.kept.source}，"
                f"忽略 {conflict.shadowed.source}"
            )
        return tuple(lines)


def _scope_from_source(source: str, default: str = "") -> str:
    normalized = str(source or "").strip().lower()
    if normalized.startswith("managed"):
        return "managed"
    if normalized.startswith("local"):
        return "local"
    if normalized.startswith("project"):
        return "project"
    if normalized.startswith("global") or normalized.startswith("user"):
        return "user"
    if normalized.startswith("plugin"):
        return "plugin"
    if normalized.startswith("builtin"):
        return "builtin"
    return default


def _priority_for_scope(scope: str) -> int:
    return {
        "builtin": 1000,
        "managed": 900,
        "local": 800,
        "project": 700,
        "user": 600,
        "plugin": 500,
        "custom": 450,
    }.get(str(scope).strip().lower(), 0)


def _plugin_for_path(path: Any, plugin_records: Iterable[Any]) -> Any:
    if not path:
        return None
    try:
        resolved = path.resolve()
    except (OSError, AttributeError):
        resolved = path
    for record in plugin_records:
        try:
            root = record.path.resolve()
        except (OSError, AttributeError):
            continue
        try:
            if resolved == root or root in resolved.parents:
                return record
        except (AttributeError, TypeError):
            continue
    return None


def collect_extension_records(
    *,
    plugins: Any = None,
    skills: Any = None,
    agents: Any = None,
    tools: Any = None,
    commands: Any = None,
    hooks: Any = None,
    mcp_servers: Optional[Mapping[str, Any]] = None,
    mcp_supervisor: Any = None,
) -> Tuple[ExtensionRecord, ...]:
    """Build a metadata-only catalogue from live specialised registries.

    The function is intentionally duck-typed so extension discovery does not
    become a second composition root or import every subsystem.
    """

    records: list[ExtensionRecord] = []
    plugin_records = tuple(plugins.records()) if plugins is not None else ()
    plugin_by_name = {record.name: record for record in plugin_records}

    for package in plugin_records:
        scope = str(getattr(package, "scope", "") or "custom")
        records.append(ExtensionRecord(
            id=package.name,
            kind=ExtensionKind.PLUGIN,
            name=package.name,
            source=f"plugin:{package.name}",
            origin=str(package.path),
            scope=scope,
            plugin=package.name,
            version=package.version,
            enabled=package.status == "loaded",
            priority=_priority_for_scope(scope),
            metadata={
                "status": package.status,
                "manifest_version": package.manifest_version,
                "trust_status": package.trust_status,
                "registry": package.registry,
                "signer": package.key_id,
                "dependencies": list(package.dependencies),
                "dependents": list(package.dependents),
            },
        ))

    if plugins is not None and tools is None:
        for tool in getattr(plugins, "tools", ()):
            plugin_name = str(getattr(tool, "plugin_name", "") or "")
            package = plugin_by_name.get(plugin_name)
            scope = str(getattr(package, "scope", "custom") if package else "custom")
            records.append(ExtensionRecord(
                id=tool.name,
                kind=ExtensionKind.TOOL,
                name=tool.name,
                source="plugin",
                origin=plugin_name,
                scope=scope,
                plugin=plugin_name,
                version=str(getattr(package, "version", "") if package else ""),
                priority=_priority_for_scope(scope),
            ))
        for command in (
            getattr(plugins, "commands", ()) if commands is None else ()
        ):
            package = plugin_by_name.get(command.plugin_name)
            scope = str(getattr(package, "scope", "custom") if package else "custom")
            records.append(ExtensionRecord(
                id=command.name,
                kind=ExtensionKind.COMMAND,
                name=command.name,
                source="plugin",
                origin=str(command.path),
                scope=scope,
                plugin=command.plugin_name,
                version=str(getattr(package, "version", "") if package else ""),
                priority=_priority_for_scope(scope),
                metadata={"local_name": command.local_name},
            ))

    if skills is not None:
        for skill in getattr(skills, "list", lambda: ())():
            package = _plugin_for_path(skill.directory, plugin_records)
            source = "plugin" if package is not None else str(skill.source or "skill")
            scope = str(getattr(package, "scope", "") if package else
                        _scope_from_source(source, "custom"))
            records.append(ExtensionRecord(
                id=skill.name,
                kind=ExtensionKind.SKILL,
                name=skill.name,
                source=source,
                origin=str(skill.skill_file),
                scope=scope,
                plugin=package.name if package is not None else "",
                version=skill.version or str(
                    getattr(package, "version", "") if package else ""),
                priority=_priority_for_scope(scope),
            ))

    if agents is not None:
        for definition in getattr(agents, "list", lambda: ())():
            package = _plugin_for_path(definition.path, plugin_records)
            source = "plugin" if package is not None else str(
                definition.source or "agent")
            scope = str(getattr(package, "scope", "") if package else
                        _scope_from_source(source, "custom"))
            records.append(ExtensionRecord(
                id=definition.name,
                kind=ExtensionKind.AGENT,
                name=definition.name,
                source=source,
                origin=str(definition.path or ""),
                scope=scope,
                plugin=package.name if package is not None else "",
                version=str(getattr(package, "version", "") if package else ""),
                priority=_priority_for_scope(scope),
            ))

    if tools is not None:
        for name in getattr(tools, "names", lambda: ())():
            descriptor = (
                tools.descriptor(name)
                if hasattr(tools, "descriptor") else None
            )
            source_value = getattr(
                getattr(descriptor, "source", None), "value",
                getattr(descriptor, "source", "builtin"),
            ) or "builtin"
            plugin_name = str(getattr(descriptor, "plugin_name", "") or "")
            package = plugin_by_name.get(plugin_name)
            scope = str(getattr(package, "scope", "") if package else
                        _scope_from_source(str(source_value), "builtin"))
            records.append(ExtensionRecord(
                id=name,
                kind=ExtensionKind.TOOL,
                name=name,
                source=str(source_value),
                origin=str(getattr(descriptor, "mcp_server", "") or ""),
                scope=scope,
                plugin=plugin_name,
                version=str(getattr(package, "version", "") if package else ""),
                priority=_priority_for_scope(scope),
                metadata={
                    "read_only": bool(getattr(descriptor, "read_only", False)),
                },
            ))

    if commands is not None:
        for spec in getattr(commands, "specs", ()):
            source_value = getattr(
                getattr(spec, "source", None), "value",
                getattr(spec, "source", "builtin"),
            ) or "builtin"
            metadata = getattr(spec, "metadata", {}) or {}
            plugin_name = str(metadata.get("plugin") or "")
            package = plugin_by_name.get(plugin_name)
            scope = str(getattr(package, "scope", "") if package else
                        _scope_from_source(str(source_value), "builtin"))
            records.append(ExtensionRecord(
                id=spec.name,
                kind=ExtensionKind.COMMAND,
                name=spec.name,
                source=str(source_value),
                origin=str(metadata.get("path") or ""),
                scope=scope,
                plugin=plugin_name,
                version=str(getattr(package, "version", "") if package else ""),
                priority=_priority_for_scope(scope),
            ))

    if hooks is not None:
        if hasattr(hooks, "specs"):
            hook_items = tuple(hooks.specs)
        else:
            hook_items = tuple(hooks)
    elif plugins is not None:
        hook_items = tuple(plugins.hook_definitions)
    else:
        hook_items = ()
    for index, item in enumerate(hook_items):
        if hasattr(item, "on"):
            trigger = str(item.on)
            source = str(item.source or "config")
            plugin_root = str(item.plugin_root or "")
            tools_value = tuple(item.tools)
        else:
            trigger = str(item.get("on", ""))
            source = str(item.get("_source") or "config")
            plugin_root = str(item.get("_plugin_root") or "")
            raw_tools = item.get("tools", item.get("tool", ()))
            tools_value = (
                (str(raw_tools),) if isinstance(raw_tools, str)
                else tuple(str(value) for value in raw_tools)
                if isinstance(raw_tools, (list, tuple)) else ()
            )
        package = None
        if source.startswith("plugin:"):
            package = plugin_by_name.get(source.split(":", 1)[1].split("@", 1)[0])
        scope = str(getattr(package, "scope", "") if package else
                    _scope_from_source(source, "config"))
        records.append(ExtensionRecord(
            id=f"{source}:{index}:{trigger}",
            kind=ExtensionKind.HOOK,
            name=trigger,
            source=source,
            origin=plugin_root,
            scope=scope,
            plugin=package.name if package is not None else "",
            version=str(getattr(package, "version", "") if package else ""),
            priority=_priority_for_scope(scope),
            metadata={"tools": tools_value},
        ))

    server_configs = dict(mcp_servers or {})
    statuses = {}
    if mcp_supervisor is not None:
        statuses = {
            str(item.get("name")): item
            for item in mcp_supervisor.status()
            if item.get("name")
        }
    for name in sorted(set(server_configs) | set(statuses)):
        config = server_configs.get(name) or {}
        plugin_name = str(config.get("_plugin") or "") if isinstance(config, dict) else ""
        package = plugin_by_name.get(plugin_name)
        if not plugin_name and name.startswith("plugin__"):
            plugin_name = name.split("__", 2)[1]
            package = plugin_by_name.get(plugin_name)
        source = "plugin" if plugin_name else "config"
        scope = str(getattr(package, "scope", "") if package else "config")
        status = statuses.get(name, {})
        records.append(ExtensionRecord(
            id=name,
            kind=ExtensionKind.MCP,
            name=name,
            source=source,
            origin=str(config.get("_source_path", "") if isinstance(config, dict) else ""),
            scope=scope,
            plugin=plugin_name,
            version=str(getattr(package, "version", "") if package else ""),
            enabled=bool(status.get("alive", True)) if status else True,
            priority=_priority_for_scope(scope),
            metadata={
                "transport": str(status.get("transport", "")),
                "alive": bool(status.get("alive", False)) if status else None,
            },
        ))

    return tuple(records)


def replace_runtime_extensions(
    registry: ExtensionRegistry,
    **registries: Any,
) -> Tuple[ExtensionRecord, ...]:
    """Replace the catalogue snapshot with records from live registries."""

    records = collect_extension_records(**registries)
    registry.clear()
    registry.register_many(records)
    return records
