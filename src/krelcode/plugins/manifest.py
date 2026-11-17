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

All component paths are relative to the plugin root and are validated before any
file is loaded. Component loading remains fail-isolated per plugin.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Tuple

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENTRYPOINT_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r":[A-Za-z_][A-Za-z0-9_]*$"
)
_COMPONENT_KEYS = ("skills", "agents", "commands", "hooks", "mcp")


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


def parse_manifest(path: Path) -> PluginManifest:
    path = Path(path)
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
        if child.is_dir() and (child / "plugin.toml").is_file()
    ]
