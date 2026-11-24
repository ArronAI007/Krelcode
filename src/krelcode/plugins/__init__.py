"""Krelcode 插件生态：发现、Plugin v2 组件、生命周期与 Tool 适配。"""

from .components import PluginCommand, PluginComponentError
from .loader import NamespacedTool, load_entrypoint, namespace_tool_name
from .manager import PluginManager
from .manifest import PluginManifest, PluginManifestError, parse_manifest
from .models import PluginIssue, PluginLifecycleResult, PluginRecord
from .dependencies import (
    DependencyDiagnostic,
    DependencyNode,
    DependencyResolution,
    PluginDependency,
    resolve_dependencies,
)
from .integrity import PluginIntegrity, PluginIntegrityError, package_integrity
from .registry import (
    PluginRegistry,
    PluginRegistryError,
    RegistryEntry,
    load_registry,
    resolve_package,
)
from .semver import Constraint, Version, VersionError, parse_constraint
from .signing import (
    PluginSignatureError,
    PluginVerification,
    SignatureMetadata,
    create_signature_document,
    read_signature,
    sign,
    verify,
    verify_plugin,
)
from .trust import (
    PluginRegistryConfig,
    PluginTrustError,
    PluginTrustPolicy,
    PluginTrustStore,
    TrustedPluginKey,
)

__all__ = [
    "NamespacedTool",
    "Constraint",
    "DependencyDiagnostic",
    "DependencyNode",
    "DependencyResolution",
    "PluginCommand",
    "PluginComponentError",
    "PluginDependency",
    "PluginIntegrity",
    "PluginIntegrityError",
    "PluginIssue",
    "PluginLifecycleResult",
    "PluginManager",
    "PluginManifest",
    "PluginManifestError",
    "PluginRecord",
    "PluginRegistry",
    "PluginRegistryConfig",
    "PluginRegistryError",
    "PluginSignatureError",
    "PluginTrustError",
    "PluginTrustPolicy",
    "PluginTrustStore",
    "PluginVerification",
    "RegistryEntry",
    "SignatureMetadata",
    "TrustedPluginKey",
    "Version",
    "VersionError",
    "create_signature_document",
    "load_entrypoint",
    "load_registry",
    "namespace_tool_name",
    "package_integrity",
    "parse_manifest",
    "parse_constraint",
    "read_signature",
    "resolve_dependencies",
    "resolve_package",
    "sign",
    "verify",
    "verify_plugin",
]
