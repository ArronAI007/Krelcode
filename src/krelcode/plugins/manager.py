"""Plugin ecosystem facade with discovery and lifecycle modules.

The public import path ``krelcode.plugins.manager`` is intentionally stable.
Discovery/loading, registry/trust operations and presentation are implemented
in focused mixins while ``PluginManager`` remains the single runtime object.
"""

from __future__ import annotations

from pathlib import Path

# Re-export the module's historical dependency surface for compatibility.
from .components import (
    PluginCommand,
    PluginComponentError,
    load_commands,
    load_hook_definitions,
    load_mcp_servers,
    safe_component_path,
)
from .dependencies import DependencyNode, resolve_dependencies
from .integrity import REGISTRY_MARKER_FILENAME, purge_generated_files
from .loader import NamespacedTool, load_entrypoint
from .manifest import (
    PluginManifest,
    PluginManifestError,
    discover_manifest_dirs,
    parse_manifest,
)
from .models import (
    MAX_INSTALL_BYTES,
    MAX_INSTALL_FILES,
    PluginIssue,
    PluginLifecycleResult,
    PluginRecord,
)
from .catalog import PluginCatalogMixin
from .presentation import PluginPresentationMixin
from .registry import (
    PluginRegistry,
    PluginRegistryError,
    RegistryEntry,
    ResolvedRegistryPackage,
    load_registry,
    resolve_package,
)
from .registry_ops import PluginRegistryMixin
from .semver import Version, VersionError, parse_constraint
from .signing import (
    PluginSignatureError,
    PluginVerification,
    read_signature,
    verify_plugin,
)
from .trust import (
    PluginRegistryConfig,
    PluginTrustError,
    PluginTrustPolicy,
    PluginTrustStore,
    TrustedPluginKey,
)


class PluginManager(
    PluginCatalogMixin,
    PluginRegistryMixin,
    PluginPresentationMixin,
):
    """Directory plugin manager with deterministic scopes and provenance."""

    @staticmethod
    def _validate_source_tree(source: Path) -> None:
        """Validate install bounds using the public facade constants.

        Reading the module globals here preserves the historical behavior where
        tests and embedders tune ``MAX_INSTALL_FILES``/``MAX_INSTALL_BYTES`` on
        ``krelcode.plugins.manager``.
        """
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
