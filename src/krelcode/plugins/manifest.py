"""Plugin manifest parsing for Tool/Skill/Agent/Command/Hook/MCP components.

Version 1 keeps working unchanged::

    name = "demo"
    entrypoint = "plugin:tools"

Version 2 is a component manifest::

    manifest_version = 2
    name = "demo"
    version = "1.0.0"

    [tools]
    entrypoint = "plugin:tools"

    [skills]
    paths = ["skills"]

    [agents]
    paths = ["agents"]

    [commands]
    paths = ["commands"]

    [hooks]
    paths = ["hooks.toml"]

    [mcp]
    paths = ["mcp.toml"]

Dependency metadata can be declared either as a table or compact strings::

    [dependencies]
    core-utils = "^1.2.0"
    optional-docs = { version = "~2.1", optional = true }

    conflicts = ["legacy-tools@<1.0"]

All component paths are relative to the plugin root and are validated before any
file is loaded. Component loading remains fail-isolated per plugin.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Tuple

from .dependencies import PluginDependency
from .semver import Version, VersionError, parse_constraint

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENTRYPOINT_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r":[A-Za-z_][A-Za-z0-9_]*$"
)
_COMPONENT_KEYS = ("skills", "agents", "commands", "hooks", "mcp")
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PLUGIN_DEPENDENCIES = 128


class PluginManifestError(ValueError):
    """插件清单无效；只影响对应插件。"""


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    entrypoint: str
    description: str = ""
    enabled: bool = True
    path: Path = Path("plugin.toml")
    manifest_version: int = 1
    skill_paths: Tuple[str, ...] = ()
    agent_paths: Tuple[str, ...] = ()
    command_paths: Tuple[str, ...] = ()
    hook_paths: Tuple[str, ...] = ()
    mcp_paths: Tuple[str, ...] = ()
    dependencies: Tuple[PluginDependency, ...] = ()
    optional_dependencies: Tuple[PluginDependency, ...] = ()
    conflicts: Tuple[PluginDependency, ...] = ()

    @property
    def plugin_dir(self) -> Path:
        return self.path.parent

    @property
    def component_count(self) -> int:
        return sum(bool(value) for value in (
            self.entrypoint,
            self.skill_paths,
            self.agent_paths,
            self.command_paths,
            self.hook_paths,
            self.mcp_paths,
        ))


def _safe_relative_paths(raw: Any, field_name: str) -> Tuple[str, ...]:
    if raw is None:
        return ()
    values = (raw,) if isinstance(raw, str) else raw
    if not isinstance(values, (list, tuple)):
        raise PluginManifestError(f"{field_name}.paths 必须是字符串或字符串数组")
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            raise PluginManifestError(f"{field_name}.paths 不能包含空路径")
        path = PurePosixPath(text.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise PluginManifestError(
                f"{field_name}.paths 必须是插件内相对路径: {text!r}")
        normalized = path.as_posix()
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _paths_from_component(raw: Any, key: str) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return _safe_relative_paths(raw, key)
    if isinstance(raw, (list, tuple)):
        return _safe_relative_paths(raw, key)
    if not isinstance(raw, dict):
        raise PluginManifestError(f"[{key}] 必须是 table")
    value = raw.get("paths", raw.get("path", raw.get("files")))
    return _safe_relative_paths(value, key)


def _entrypoint_from_component(raw: Any, key: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, dict):
        raise PluginManifestError(f"[{key}] 必须是 table")
    value = raw.get("entrypoint")
    if value is None:
        return ""
    entrypoint = str(value).strip()
    if not _ENTRYPOINT_RE.fullmatch(entrypoint):
        raise PluginManifestError(
            f"{key}.entrypoint 必须形如 module:attribute")
    return entrypoint


def _dependency_from_text(text: str, *, optional: bool) -> PluginDependency:
    value = str(text).strip()
    if not value:
        raise PluginManifestError("依赖声明不能为空")
    if "@" in value:
        name, constraint = value.split("@", 1)
    else:
        name, constraint = value, "*"
    name = name.strip()
    constraint = constraint.strip() or "*"
    if not _NAME_RE.fullmatch(name):
        raise PluginManifestError(f"非法依赖名称: {name!r}")
    try:
        parse_constraint(constraint)
    except VersionError as exc:
        raise PluginManifestError(f"依赖 {name!r} 的版本约束无效: {exc}") from exc
    return PluginDependency(name, constraint, optional=optional)


def _parse_dependencies(raw: Any, field_name: str) -> Tuple[PluginDependency, ...]:
    if raw is None:
        return ()
    result: list[PluginDependency] = []
    if isinstance(raw, str):
        result.append(_dependency_from_text(raw, optional=False))
    elif isinstance(raw, (list, tuple)):
        result.extend(_dependency_from_text(str(value), optional=False) for value in raw)
    elif isinstance(raw, dict):
        for name, value in raw.items():
            dependency_name = str(name).strip()
            if not _NAME_RE.fullmatch(dependency_name):
                raise PluginManifestError(f"非法依赖名称: {dependency_name!r}")
            optional = False
            if isinstance(value, dict):
                constraint = str(value.get("version") or "*").strip() or "*"
                optional_value = value.get("optional", False)
                if not isinstance(optional_value, bool):
                    raise PluginManifestError(
                        f"{field_name}.{dependency_name}.optional 必须是布尔值")
                optional = optional_value
            else:
                constraint = str(value).strip() or "*"
            try:
                parse_constraint(constraint)
            except VersionError as exc:
                raise PluginManifestError(
                    f"依赖 {dependency_name!r} 的版本约束无效: {exc}") from exc
            result.append(PluginDependency(dependency_name, constraint, optional=optional))
    else:
        raise PluginManifestError(f"{field_name} 必须是字符串数组或 table")
    if len(result) > MAX_PLUGIN_DEPENDENCIES:
        raise PluginManifestError(
            f"{field_name} 依赖过多（>{MAX_PLUGIN_DEPENDENCIES}）")
    seen: set[str] = set()
    for dependency in result:
        if dependency.name in seen:
            raise PluginManifestError(f"依赖重复声明: {dependency.name}")
        seen.add(dependency.name)
    return tuple(result)


def parse_manifest(path: Path) -> PluginManifest:
    path = Path(path)
    if path.is_symlink():
        raise PluginManifestError("plugin.toml 不能是符号链接")
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise PluginManifestError("plugin.toml 过大")
    except OSError as exc:
        raise PluginManifestError(f"无法读取清单: {exc}") from exc
    try:
        with open(path, "rb") as handle:
            raw: Any = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PluginManifestError(f"无法读取清单: {exc}") from exc
    if not isinstance(raw, dict):
        raise PluginManifestError("插件清单必须是 TOML table")

    name = str(raw.get("name") or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise PluginManifestError(
            "name 必须为 1-64 位字母、数字、点、下划线或连字符")
    version = str(raw.get("version") or "0.0.0").strip() or "0.0.0"
    try:
        Version.parse(version)
    except VersionError as exc:
        raise PluginManifestError(f"version 不是合法 SemVer: {exc}") from exc
    description = " ".join(str(raw.get("description") or "").split())
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PluginManifestError("enabled 必须是布尔值")
    raw_manifest_version = raw.get("manifest_version", 1)
    if isinstance(raw_manifest_version, bool):
        raise PluginManifestError("manifest_version 必须是整数")
    try:
        manifest_version = int(raw_manifest_version)
    except (TypeError, ValueError) as exc:
        raise PluginManifestError("manifest_version 必须是整数") from exc
    if manifest_version not in (1, 2):
        raise PluginManifestError("仅支持 manifest_version = 1 或 2")

    legacy_entrypoint = str(raw.get("entrypoint") or "").strip()
    if legacy_entrypoint and not _ENTRYPOINT_RE.fullmatch(legacy_entrypoint):
        raise PluginManifestError("entrypoint 必须形如 module:attribute")
    tools_component = raw.get("tools", raw.get("tool"))
    tools_entrypoint = _entrypoint_from_component(tools_component, "tools")
    entrypoint = tools_entrypoint or legacy_entrypoint

    paths = {
        key: _paths_from_component(raw.get(key), key)
        for key in _COMPONENT_KEYS
    }
    if not entrypoint and not any(paths.values()):
        raise PluginManifestError(
            "插件至少需要 entrypoint 或一个 v2 组件路径")

    required_dependencies = list(_parse_dependencies(raw.get("dependencies"), "dependencies"))
    optional_dependencies = list(
        _parse_dependencies(raw.get("optional_dependencies"), "optional_dependencies"))
    if not optional_dependencies:
        optional_dependencies = [
            PluginDependency(item.name, item.constraint, optional=True)
            for item in required_dependencies if item.optional
        ]
        required_dependencies = [
            item for item in required_dependencies if not item.optional
        ]
    else:
        optional_dependencies = [
            PluginDependency(item.name, item.constraint, optional=True)
            for item in optional_dependencies
        ]
    required_names = {item.name for item in required_dependencies}
    for item in optional_dependencies:
        if item.name in required_names:
            raise PluginManifestError(f"依赖同时声明为必需和可选: {item.name}")
    conflicts = _parse_dependencies(raw.get("conflicts"), "conflicts")

    return PluginManifest(
        name=name,
        version=version,
        entrypoint=entrypoint,
        description=description,
        enabled=enabled,
        path=path,
        manifest_version=manifest_version,
        skill_paths=paths["skills"],
        agent_paths=paths["agents"],
        command_paths=paths["commands"],
        hook_paths=paths["hooks"],
        mcp_paths=paths["mcp"],
        dependencies=tuple(required_dependencies),
        optional_dependencies=tuple(optional_dependencies),
        conflicts=conflicts,
    )


def discover_manifest_dirs(root: Path) -> list[Optional[Path]]:
    """返回根目录下的清单路径；根目录自身是插件时也支持。"""
    root = Path(root)
    if (root / "plugin.toml").is_file():
        return [root / "plugin.toml"]
    if not root.is_dir():
        return []
    return [
        child / "plugin.toml"
        for child in sorted(root.iterdir(), key=lambda path: path.name)
        if child.is_dir() and not child.is_symlink()
        and (child / "plugin.toml").is_file()
        and not (child / "plugin.toml").is_symlink()
    ]
