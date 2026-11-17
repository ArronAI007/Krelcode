"""Krelcode 插件生态：发现、Plugin v2 组件、生命周期与 Tool 适配。"""

from .components import PluginCommand, PluginComponentError
from .loader import NamespacedTool, load_entrypoint, namespace_tool_name
from .manager import (
    PluginIssue,
    PluginLifecycleResult,
    PluginManager,
    PluginRecord,
)
from .manifest import PluginManifest, PluginManifestError, parse_manifest

__all__ = [
    "NamespacedTool",
    "PluginCommand",
    "PluginComponentError",
    "PluginIssue",
    "PluginLifecycleResult",
    "PluginManager",
    "PluginManifest",
    "PluginManifestError",
    "PluginRecord",
    "load_entrypoint",
    "namespace_tool_name",
    "parse_manifest",
]
