"""Schema-neutral loaders for Plugin v2 component manifests."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from ..skills.parser import SkillParseError, parse_skill_document, split_allowed_tools
from .manifest import PluginManifest

MAX_COMPONENT_FILES = 500
MAX_COMMAND_BYTES = 256_000
_SAFE_COMMAND_PART = re.compile(r"[^A-Za-z0-9_.-]+")


class PluginComponentError(ValueError):
    """One invalid component declaration; the plugin can still fail isolated."""


@dataclass(frozen=True)
class PluginCommand:
    name: str
    local_name: str
    description: str
    prompt: str
    path: Path
    plugin_name: str
    argument_hint: str = ""
    allowed_tools: Tuple[str, ...] = ()
    reminder_ids: Tuple[str, ...] = ()
    scope: str = "user"

    def render(self, arguments: str = "") -> str:
        raw = arguments.strip()
        has_placeholder = (
            "$ARGUMENTS" in self.prompt or "${ARGUMENTS}" in self.prompt)
        text = self.prompt.replace("${ARGUMENTS}", raw).replace(
            "$ARGUMENTS", raw)
        if raw and not has_placeholder:
            text = text.rstrip() + "\n\n## User arguments\n\n" + raw
        trust = "project" if self.scope == "project" else "user"
        return (
            f'<plugin_command plugin="{self.plugin_name}" '
            f'name="{self.local_name}" trust="{trust}" instruction="false">\n'
            f"{text.rstrip()}\n</plugin_command>"
        )


def safe_component_path(plugin_dir: Path, relative: str) -> Path:
    root = Path(plugin_dir).resolve()
    path = (root / relative).resolve(strict=False)
    if path != root and root not in path.parents:
        raise PluginComponentError(f"组件路径越出插件目录: {relative!r}")
    try:
        current = path
        while current != root.parent:
            if current.is_symlink():
                raise PluginComponentError(f"组件路径不能是符号链接: {relative!r}")
            if current == root:
                break
            current = current.parent
    except OSError as exc:
        raise PluginComponentError(f"无法检查组件路径: {relative!r}: {exc}") from exc
    return path


def _split_reminder_ids(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    values = value if isinstance(value, (list, tuple, set, frozenset)) else (
        str(value).replace(";", ",").split(","))
    result: list[str] = []
    for item in values:
        for part in str(item).split():
            text = part.strip()
            if not text:
                continue
            if any(ch.isspace() for ch in text):
                raise PluginComponentError(f"非法 reminder id: {text!r}")
            if text not in result:
                result.append(text)
    return tuple(result)


def component_files(
    plugin_dir: Path,
    relatives: Tuple[str, ...],
    *,
    suffix: str = "",
) -> Tuple[Path, ...]:
    result: list[Path] = []
    for relative in relatives:
        path = safe_component_path(plugin_dir, relative)
        if not path.exists():
            raise PluginComponentError(f"组件路径不存在: {relative}")
        if path.is_dir():
            try:
                candidates = sorted(path.rglob(f"*{suffix}"), key=lambda p: str(p))
            except OSError as exc:
                raise PluginComponentError(f"无法扫描组件目录: {relative}: {exc}") from exc
        else:
            candidates = [path]
        for candidate in candidates:
            if suffix and candidate.suffix.lower() != suffix.lower():
                continue
            if not candidate.is_file():
                continue
            safe = safe_component_path(plugin_dir, str(candidate.relative_to(plugin_dir)))
            result.append(safe)
            if len(result) > MAX_COMPONENT_FILES:
                raise PluginComponentError(
                    f"组件文件过多（>{MAX_COMPONENT_FILES}）")
    return tuple(dict.fromkeys(result))


def load_commands(
    manifest: PluginManifest,
    *,
    scope: str = "user",
) -> Tuple[PluginCommand, ...]:
    commands: list[PluginCommand] = []
    for path in component_files(
        manifest.plugin_dir, manifest.command_paths, suffix=".md"
    ):
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise PluginComponentError(f"无法读取 Command: {path}: {exc}") from exc
        if size > MAX_COMMAND_BYTES:
            raise PluginComponentError(
                f"Command 文件过大（{size} > {MAX_COMMAND_BYTES}）: {path}")
        try:
            text = path.read_text(encoding="utf-8")
            metadata, body = parse_skill_document(text, fallback_name=path.stem)
        except (OSError, SkillParseError) as exc:
            raise PluginComponentError(f"无法解析 Command {path}: {exc}") from exc
        prompt = body.strip()
        if not prompt:
            raise PluginComponentError(f"Command 正文不能为空: {path}")
        relative = path.relative_to(manifest.plugin_dir).with_suffix("").as_posix()
        local_name = "__".join(
            _SAFE_COMMAND_PART.sub("_", part).strip("_") or "command"
            for part in relative.split("/")
        )
        # Keep command names stable and collision-resistant across plugins.
        local_name = str(metadata.get("name") or local_name).strip()
        description = " ".join(str(metadata.get("description") or "").split())
        if not description:
            description = next(
                (line.strip("# ").strip() for line in prompt.splitlines()
                 if line.strip()),
                f"Plugin command {local_name}",
            )[:320]
        commands.append(PluginCommand(
            name=f"plugin__{manifest.name}__{local_name}",
            local_name=local_name,
            description=description,
            prompt=prompt,
            path=path,
            plugin_name=manifest.name,
            argument_hint=str(metadata.get("argument-hint") or ""),
            allowed_tools=split_allowed_tools(metadata.get("allowed-tools")),
            reminder_ids=_split_reminder_ids(
                metadata.get("reminder-ids", metadata.get("reminders"))),
            scope=str(scope),
        ))
    return tuple(commands)


def _read_component_toml(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PluginComponentError(f"无法读取组件 TOML {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PluginComponentError(f"组件 TOML 必须是 table: {path}")
    return raw


def load_hook_definitions(manifest: PluginManifest) -> Tuple[Dict[str, Any], ...]:
    hooks: list[Dict[str, Any]] = []
    for path in component_files(manifest.plugin_dir, manifest.hook_paths, suffix=".toml"):
        raw = _read_component_toml(path)
        values = raw.get("hooks", raw.get("hook"))
        if isinstance(values, dict):
            values = [values]
        if not isinstance(values, list):
            raise PluginComponentError(f"Hook 文件必须包含 [[hooks]] 数组: {path}")
        for item in values:
            if not isinstance(item, dict):
                raise PluginComponentError(f"Hook 项必须是 table: {path}")
            normalized = dict(item)
            normalized["_source"] = f"plugin:{manifest.name}@{manifest.version}"
            normalized["_plugin_root"] = str(manifest.plugin_dir)
            hooks.append(normalized)
    return tuple(hooks)


def load_mcp_servers(manifest: PluginManifest) -> Dict[str, Dict[str, Any]]:
    servers: Dict[str, Dict[str, Any]] = {}
    for path in component_files(manifest.plugin_dir, manifest.mcp_paths, suffix=".toml"):
        raw = _read_component_toml(path)
        values = raw.get("mcp_servers", raw.get("servers", raw))
        if not isinstance(values, dict):
            raise PluginComponentError(f"MCP 文件必须包含 [servers.*]: {path}")
        for name, config in values.items():
            if not isinstance(config, dict):
                raise PluginComponentError(
                    f"MCP server {name!r} 配置必须是 table: {path}")
            safe_name = _SAFE_COMMAND_PART.sub("_", str(name)).strip("_") or "server"
            namespaced = f"plugin__{manifest.name}__{safe_name}"
            if namespaced in servers:
                raise PluginComponentError(f"插件 MCP 服务器重名: {namespaced}")
            value = dict(config)
            value.setdefault("_plugin", manifest.name)
            value.setdefault("_plugin_version", manifest.version)
            value.setdefault("_source_path", str(path))
            servers[namespaced] = value
    return servers
