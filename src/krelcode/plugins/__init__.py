"""Krelcode 轻量插件生态：目录发现、Tool 适配与热重载。"""

from .loader import NamespacedTool, load_entrypoint, namespace_tool_name
from .manager import PluginIssue, PluginManager, PluginRecord
from .manifest import PluginManifest, PluginManifestError, parse_manifest

__all__ = [
    "NamespacedTool",
    "PluginIssue",
    "PluginManager",
    "PluginManifest",
    "PluginManifestError",
    "PluginRecord",
    "load_entrypoint",
    "namespace_tool_name",
    "parse_manifest",
]
