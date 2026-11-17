"""Plugin discovery, component loading and basic package lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .components import (
    PluginCommand,
    PluginComponentError,
    load_commands,
    load_hook_definitions,
    load_mcp_servers,
    safe_component_path,
)
from .loader import NamespacedTool, load_entrypoint
from .manifest import (
    PluginManifest,
    PluginManifestError,
    discover_manifest_dirs,
    parse_manifest,
)

MAX_INSTALL_FILES = 5_000
MAX_INSTALL_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class PluginIssue:
    path: Path
    message: str


@dataclass(frozen=True)
class PluginRecord:
    name: str
    status: str
    path: Path
    version: str = ""
    description: str = ""
    entrypoint: str = ""
    tools_count: int = 0
    error: str = ""
    scope: str = ""
    manifest_version: int = 1
    skills_count: int = 0
    agents_count: int = 0
    commands_count: int = 0
    hooks_count: int = 0
    mcp_servers_count: int = 0

    @property
    def components_count(self) -> int:
        return (
            self.tools_count + self.skills_count + self.agents_count
            + self.commands_count + self.hooks_count + self.mcp_servers_count
        )


@dataclass(frozen=True)
class PluginLifecycleResult:
    action: str
    name: str
    scope: str
    path: Path
    message: str = ""


class PluginManager:
    """Directory plugin manager with deterministic scopes and provenance."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        home: Optional[Path] = None,
        extra_paths: Iterable[str | Path] = (),
        enabled: bool = True,
        state_path: Optional[Path] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.home = (Path(home).expanduser() if home else Path.home()).resolve()
        self.extra_paths = tuple(
            self._expand_path(path) for path in extra_paths if str(path).strip())
        self.enabled = enabled
        self.state_path = (
            Path(state_path).expanduser()
            if state_path is not None
            else self.home / ".krelcode" / "plugin-state.json"
        )
        self._tools: tuple[NamespacedTool, ...] = ()
        self._records: tuple[PluginRecord, ...] = ()
        self._issues: tuple[PluginIssue, ...] = ()
        self._manifests: dict[str, PluginManifest] = {}
        self._scopes: dict[str, str] = {}
        self._commands: tuple[PluginCommand, ...] = ()
        self._skill_paths: tuple[Path, ...] = ()
        self._agent_paths: tuple[Path, ...] = ()
        self._hook_definitions: tuple[Dict[str, Any], ...] = ()
        self._mcp_servers: dict[str, Dict[str, Any]] = {}

    def _expand_path(self, path: str | Path) -> Path:
        value = Path(path).expanduser()
        return value if value.is_absolute() else (self.workspace_root / value)

    @property
    def user_root(self) -> Path:
        return self.home / ".krelcode" / "plugins"

    @property
    def project_root(self) -> Path:
        return self.workspace_root / ".krelcode" / "plugins"

    def roots(self) -> tuple[Path, ...]:
        roots = [self.user_root, self.project_root, *self.extra_paths]
        unique: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            resolved = root.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append(resolved)
        return tuple(unique)

    def _root_scope(self, root: Path) -> str:
        resolved = root.resolve()
        if resolved == self.user_root.resolve():
            return "user"
        if resolved == self.project_root.resolve():
            return "project"
        return "custom"

    @property
    def tools(self) -> tuple[NamespacedTool, ...]:
        return self._tools

    @property
    def commands(self) -> tuple[PluginCommand, ...]:
        return self._commands

    @property
    def skill_paths(self) -> tuple[Path, ...]:
        return self._skill_paths

    @property
    def agent_paths(self) -> tuple[Path, ...]:
        return self._agent_paths

    @property
    def hook_definitions(self) -> tuple[Dict[str, Any], ...]:
        return self._hook_definitions

    @property
    def mcp_servers(self) -> Mapping[str, Dict[str, Any]]:
        return dict(self._mcp_servers)

    @property
    def issues(self) -> tuple[PluginIssue, ...]:
        return self._issues

    def records(self) -> tuple[PluginRecord, ...]:
        return self._records

    def get_command(self, name: str) -> Optional[PluginCommand]:
        normalized = str(name).strip().lstrip("/")
        return next(
            (command for command in self._commands if command.name == normalized),
            None,
        )

    # -- state and discovery -------------------------------------------------

    def _load_state(self) -> Dict[str, bool]:
        if not self.state_path.is_file():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        plugins = raw.get("plugins") if isinstance(raw, dict) else None
        if not isinstance(plugins, dict):
            return {}
        result: Dict[str, bool] = {}
        for name, value in plugins.items():
            if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
                result[str(name)] = bool(value["enabled"])
        return result

    def _write_state(self, state: Mapping[str, bool]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "plugins": {
                name: {"enabled": bool(enabled)}
                for name, enabled in sorted(state.items())
            },
        }
        fd, temporary = tempfile.mkstemp(
            prefix=".plugin-state-", suffix=".tmp",
            dir=str(self.state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    async def load(self) -> tuple[PluginRecord, ...]:
        if not self.enabled:
            self._clear_loaded()
            return ()
        manifests: dict[str, PluginManifest] = {}
        scopes: dict[str, str] = {}
        issues: list[PluginIssue] = []
        configured_roots = {path.resolve() for path in self.extra_paths}
        for root in self.roots():
            if not root.exists():
                if root in configured_roots:
                    issues.append(PluginIssue(root, "插件目录不存在"))
                continue
            manifest_paths = discover_manifest_dirs(root)
            if not manifest_paths:
                issues.append(PluginIssue(root, "未发现 plugin.toml"))
            for manifest_path in manifest_paths:
                if manifest_path is None:
                    continue
                try:
                    manifest = parse_manifest(manifest_path)
                except PluginManifestError as exc:
                    issues.append(PluginIssue(manifest_path, str(exc)))
                    continue
                if manifest.name in manifests:
                    issues.append(PluginIssue(
                        manifest_path,
                        f"插件名 {manifest.name!r} 被更高优先级目录覆盖"))
                manifests[manifest.name] = manifest
                scopes[manifest.name] = self._root_scope(root)

        state = self._load_state()
        records: list[PluginRecord] = []
        tools: list[NamespacedTool] = []
        commands: list[PluginCommand] = []
        skill_paths: list[Path] = []
        agent_paths: list[Path] = []
        hook_definitions: list[Dict[str, Any]] = []
        mcp_servers: dict[str, Dict[str, Any]] = {}
        loaded_manifests: dict[str, PluginManifest] = {}

        for name in sorted(manifests):
            manifest = manifests[name]
            scope = scopes.get(name, "custom")
            effective_enabled = manifest.enabled and state.get(
                f"{scope}:{name}", state.get(name, True))
            if not effective_enabled:
                records.append(PluginRecord(
                    name, "disabled", manifest.plugin_dir,
                    version=manifest.version,
                    description=manifest.description,
                    entrypoint=manifest.entrypoint,
                    scope=scope,
                    manifest_version=manifest.manifest_version))
                continue
            try:
                loaded_tools = (
                    await load_entrypoint(
                        manifest.plugin_dir,
                        manifest.entrypoint,
                        manifest.name,
                        manifest.version,
                    )
                    if manifest.entrypoint else []
                )
                plugin_commands = load_commands(manifest)
                plugin_hooks = load_hook_definitions(manifest)
                plugin_mcp = load_mcp_servers(manifest)
                plugin_skills = tuple(
                    safe_component_path(manifest.plugin_dir, path)
                    for path in manifest.skill_paths
                )
                plugin_agents = tuple(
                    safe_component_path(manifest.plugin_dir, path)
                    for path in manifest.agent_paths
                )
                for path in (*plugin_skills, *plugin_agents):
                    if not path.exists():
                        raise PluginComponentError(
                            f"组件路径不存在: {path.relative_to(manifest.plugin_dir)}")
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                issues.append(PluginIssue(manifest.path, message))
                records.append(PluginRecord(
                    name, "error", manifest.plugin_dir,
                    version=manifest.version,
                    description=manifest.description,
                    entrypoint=manifest.entrypoint,
                    error=message,
                    scope=scope,
                    manifest_version=manifest.manifest_version))
                continue

            tools.extend(loaded_tools)
            commands.extend(plugin_commands)
            hook_definitions.extend(plugin_hooks)
            mcp_servers.update(plugin_mcp)
            skill_paths.extend(plugin_skills)
            agent_paths.extend(plugin_agents)
            loaded_manifests[name] = manifest
            records.append(PluginRecord(
                name, "loaded", manifest.plugin_dir,
                version=manifest.version,
                description=manifest.description,
                entrypoint=manifest.entrypoint,
                tools_count=len(loaded_tools),
                scope=scope,
                manifest_version=manifest.manifest_version,
                skills_count=len(plugin_skills),
                agents_count=len(plugin_agents),
                commands_count=len(plugin_commands),
                hooks_count=len(plugin_hooks),
                mcp_servers_count=len(plugin_mcp)))

        self._tools = tuple(tools)
        self._records = tuple(records)
        self._issues = tuple(issues)
        self._manifests = loaded_manifests
        self._scopes = {name: scopes[name] for name in loaded_manifests}
        self._commands = tuple(commands)
        self._skill_paths = tuple(dict.fromkeys(skill_paths))
        self._agent_paths = tuple(dict.fromkeys(agent_paths))
        self._hook_definitions = tuple(hook_definitions)
        self._mcp_servers = mcp_servers
        return self._records

    def _clear_loaded(self) -> None:
        self._tools = ()
        self._records = ()
        self._issues = ()
        self._manifests = {}
        self._scopes = {}
        self._commands = ()
        self._skill_paths = ()
        self._agent_paths = ()
        self._hook_definitions = ()
        self._mcp_servers = {}

    async def reload(self) -> tuple[PluginRecord, ...]:
        return await self.load()

    # -- lifecycle -----------------------------------------------------------

    def _lifecycle_root(self, scope: str) -> Path:
        normalized = str(scope).strip().lower() or "user"
        if normalized == "user":
            return self.user_root
        if normalized == "project":
            return self.project_root
        raise ValueError("scope 必须是 user 或 project")

    @staticmethod
    def _validate_source_tree(source: Path) -> None:
        count = 0
        total = 0
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"插件安装源不能包含符号链接: {path}")
            if path.is_file():
                count += 1
                total += path.stat().st_size
                if count > MAX_INSTALL_FILES:
                    raise ValueError(f"插件文件过多（>{MAX_INSTALL_FILES}）")
                if total > MAX_INSTALL_BYTES:
                    raise ValueError(
                        f"插件体积过大（>{MAX_INSTALL_BYTES // 1024 // 1024} MiB）")

    async def install(
        self,
        source: str | Path,
        *,
        scope: str = "user",
        replace: bool = False,
    ) -> PluginLifecycleResult:
        result = await asyncio.to_thread(
            self._install_sync, Path(source), scope, replace)
        return result

    def _install_sync(
        self, source: Path, scope: str, replace: bool,
    ) -> PluginLifecycleResult:
        source = source.expanduser().resolve()
        manifest_path = source / "plugin.toml"
        if not manifest_path.is_file():
            raise ValueError(f"安装源缺少 plugin.toml: {source}")
        manifest = parse_manifest(manifest_path)
        self._validate_source_tree(source)
        root = self._lifecycle_root(scope).resolve()
        root.mkdir(parents=True, exist_ok=True)
        destination = root / manifest.name
        if destination.exists() and not replace:
            raise ValueError(f"插件已安装: {manifest.name} ({destination})")
        staging = root / f".staging-{manifest.name}-{uuid.uuid4().hex}"
        trash = root / ".trash"
        backup: Optional[Path] = None
        try:
            shutil.copytree(source, staging, symlinks=False)
            if destination.exists():
                trash.mkdir(parents=True, exist_ok=True)
                backup = trash / f"{manifest.name}-{uuid.uuid4().hex}"
                os.replace(destination, backup)
            os.replace(staging, destination)
        except Exception:
            if backup is not None and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        action = "update" if replace else "install"
        return PluginLifecycleResult(
            action=action,
            name=manifest.name,
            scope=scope,
            path=destination,
            message=f"{action}ed {manifest.name}@{manifest.version}",
        )

    async def update(
        self,
        source: str | Path,
        *,
        scope: str = "user",
    ) -> PluginLifecycleResult:
        return await self.install(source, scope=scope, replace=True)

    async def remove(self, name: str) -> PluginLifecycleResult:
        return await asyncio.to_thread(self._remove_sync, name)

    def _remove_sync(self, name: str) -> PluginLifecycleResult:
        normalized = str(name).strip()
        record = next(
            (item for item in self._records if item.name == normalized), None)
        if record is None:
            raise ValueError(f"未找到插件: {normalized}")
        root = record.path.parent.resolve()
        allowed = {self.user_root.resolve(), self.project_root.resolve()}
        if root not in allowed or record.path.name != normalized:
            raise ValueError("只能移除 user/project 插件目录中的已安装插件")
        trash = root / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        destination = trash / f"{normalized}-{uuid.uuid4().hex}"
        os.replace(record.path, destination)
        return PluginLifecycleResult(
            action="remove",
            name=normalized,
            scope=record.scope,
            path=destination,
            message=f"moved {normalized} to {destination}",
        )

    async def set_enabled(self, name: str, enabled: bool) -> PluginLifecycleResult:
        normalized = str(name).strip()
        if not any(item.name == normalized for item in self._records):
            raise ValueError(f"未找到插件: {normalized}")
        record = next(item for item in self._records if item.name == normalized)
        state = self._load_state()
        state[f"{record.scope}:{normalized}"] = bool(enabled)
        state.pop(normalized, None)
        await asyncio.to_thread(self._write_state, state)
        action = "enable" if enabled else "disable"
        return PluginLifecycleResult(
            action=action,
            name=normalized,
            scope=record.scope,
            path=record.path,
            message=f"{action}d {normalized}",
        )

    # -- presentation --------------------------------------------------------

    def status_lines(self) -> tuple[str, ...]:
        if not self.enabled:
            return ("  [plugins] 已禁用",)
        component_total = sum(record.components_count for record in self._records)
        lines = [
            f"  [plugins] {len(self._records)} 个插件 · "
            f"{component_total} 个组件 · {len(self._issues)} 个问题",
        ]
        for record in self._records:
            detail = f"  - {record.name}@{record.version} [{record.status}]"
            if record.scope:
                detail += f" · {record.scope}"
            if record.components_count:
                detail += (
                    f" · tool={record.tools_count} skill={record.skills_count}"
                    f" agent={record.agents_count} command={record.commands_count}"
                    f" hook={record.hooks_count} mcp={record.mcp_servers_count}"
                )
            detail += f" · {record.path}"
            lines.append(detail)
            if record.error:
                lines.append(f"      {record.error}")
        for issue in self._issues:
            lines.append(f"  ! {issue.path}: {issue.message}")
        if not self._records:
            lines.append("  插件目录: " + ", ".join(str(root) for root in self.roots()))
        return tuple(lines)
