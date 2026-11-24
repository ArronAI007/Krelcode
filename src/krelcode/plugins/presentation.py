"""Human-readable plugin status and dependency diagnostics."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .models import PluginIssue, PluginRecord
from .registry import PluginRegistryError, load_registry
from .trust import PluginTrustError, PluginTrustPolicy, PluginTrustStore


class PluginPresentationMixin:
    if TYPE_CHECKING:
        enabled: bool
        trust_store: PluginTrustStore
        trust_policy: PluginTrustPolicy
        _records: tuple[PluginRecord, ...]
        _issues: tuple[PluginIssue, ...]

        def roots(self) -> tuple[Path, ...]: ...

    def registry_lines(self) -> tuple[str, ...]:
        registries = self.trust_store.registries()
        if not registries:
            return ("  [plugins] 未配置可信 registry",)
        lines: list[str] = []
        for config in registries:
            keys = self.trust_store.keys(config.name)
            try:
                registry = load_registry(
                    config.name,
                    trust_store=self.trust_store,
                    require_signature=True,
                )
                detail = (
                    f"  registry {config.name} [trusted] · {len(registry.entries)} 条 · "
                    f"index sha256:{registry.index_sha256[:16]} · {config.path}")
            except (PluginRegistryError, PluginTrustError) as exc:
                state = "disabled" if not config.enabled else "untrusted"
                detail = f"  registry {config.name} [{state}] · {config.path}: {exc}"
            lines.append(detail)
            lines.append(f"      trusted keys={len(keys)}")
        return tuple(lines)

    def dependency_lines(self, name: Optional[str] = None) -> tuple[str, ...]:
        selected = list(self._records)
        if name:
            target = next((item for item in selected if item.name == name), None)
            if target is None:
                return (f"  [plugins] 未找到插件: {name}",)
            selected = [target]
        lines: list[str] = []
        for record in selected:
            required = ", ".join(record.dependencies) or "（无）"
            optional = ", ".join(record.optional_dependencies) or "（无）"
            dependents = ", ".join(record.dependents) or "（无）"
            lines.extend((
                f"  {record.name}@{record.version} [{record.status}]",
                f"    requires: {required}",
                f"    optional: {optional}",
                f"    dependents: {dependents}",
            ))
            if record.blocked_reason:
                lines.append(f"    blocked: {record.blocked_reason}")
        return tuple(lines) if lines else ("  [plugins] 没有插件依赖信息",)

    # -- presentation --------------------------------------------------------

    def status_lines(self) -> tuple[str, ...]:
        if not self.enabled:
            return ("  [plugins] 已禁用",)
        component_total = sum(record.components_count for record in self._records)
        lines = [
            f"  [plugins] {len(self._records)} 个插件 · "
            f"{component_total} 个组件 · {len(self._issues)} 个问题 · "
            f"trust={self.trust_policy.mode}",
        ]
        for record in self._records:
            detail = f"  - {record.name}@{record.version} [{record.status}]"
            if record.scope:
                detail += f" · {record.scope}"
            if record.trust_status:
                detail += f" · trust={record.trust_status}"
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
