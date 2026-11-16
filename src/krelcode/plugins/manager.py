"""轻量插件发现与热重载；单个插件故障不会阻断 Agent。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .loader import NamespacedTool, load_entrypoint
from .manifest import (
    PluginManifest,
    PluginManifestError,
    discover_manifest_dirs,
    parse_manifest,
)


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


class PluginManager:
    """目录式插件管理器；只暴露现有 Tool 协议。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        home: Optional[Path] = None,
        extra_paths: Iterable[str | Path] = (),
        enabled: bool = True,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.home = Path(home).expanduser() if home else Path.home()
        self.extra_paths = tuple(
            self._expand_path(path) for path in extra_paths if str(path).strip())
        self.enabled = enabled
        self._tools: tuple[NamespacedTool, ...] = ()
        self._records: tuple[PluginRecord, ...] = ()
        self._issues: tuple[PluginIssue, ...] = ()

    def _expand_path(self, path: str | Path) -> Path:
        path = Path(path).expanduser()
        return path if path.is_absolute() else (self.workspace_root / path)

    def roots(self) -> tuple[Path, ...]:
        roots = [
            self.home / ".krelcode" / "plugins",
            self.workspace_root / ".krelcode" / "plugins",
            *self.extra_paths,
        ]
        unique: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            resolved = root.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append(resolved)
        return tuple(unique)

    @property
    def tools(self) -> tuple[NamespacedTool, ...]:
        return self._tools

    @property
    def issues(self) -> tuple[PluginIssue, ...]:
        return self._issues

    def records(self) -> tuple[PluginRecord, ...]:
        return self._records

    async def load(self) -> tuple[PluginRecord, ...]:
        if not self.enabled:
            self._tools = ()
            self._records = ()
            self._issues = ()
            return ()
        manifests: dict[str, PluginManifest] = {}
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

        records: list[PluginRecord] = []
        tools: list[NamespacedTool] = []
        for name in sorted(manifests):
            manifest = manifests[name]
            if not manifest.enabled:
                records.append(PluginRecord(
                    name, "disabled", manifest.plugin_dir,
                    version=manifest.version,
                    description=manifest.description,
                    entrypoint=manifest.entrypoint))
                continue
            try:
                loaded = await load_entrypoint(
                    manifest.plugin_dir,
                    manifest.entrypoint,
                    manifest.name,
                    manifest.version,
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                issues.append(PluginIssue(manifest.path, message))
                records.append(PluginRecord(
                    name, "error", manifest.plugin_dir,
                    version=manifest.version,
                    description=manifest.description,
                    entrypoint=manifest.entrypoint,
                    error=message))
                continue
            tools.extend(loaded)
            records.append(PluginRecord(
                name, "loaded", manifest.plugin_dir,
                version=manifest.version,
                description=manifest.description,
                entrypoint=manifest.entrypoint,
                tools_count=len(loaded)))
        self._tools = tuple(tools)
        self._records = tuple(records)
        self._issues = tuple(issues)
        return self._records

    async def reload(self) -> tuple[PluginRecord, ...]:
        return await self.load()

    def status_lines(self) -> tuple[str, ...]:
        if not self.enabled:
            return ("  [plugins] 已禁用",)
        lines = [
            f"  [plugins] {len(self._records)} 个插件 · "
            f"{len(self._tools)} 个工具 · {len(self._issues)} 个问题",
        ]
        for record in self._records:
            detail = f"  - {record.name}@{record.version} [{record.status}]"
            if record.tools_count:
                detail += f" · {record.tools_count} 个工具"
            detail += f" · {record.path}"
            lines.append(detail)
            if record.error:
                lines.append(f"      {record.error}")
        for issue in self._issues:
            lines.append(f"  ! {issue.path}: {issue.message}")
        if not self._records:
            lines.append("  插件目录: " + ", ".join(str(root) for root in self.roots()))
        return tuple(lines)
