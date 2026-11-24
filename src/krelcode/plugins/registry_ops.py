"""Registry resolution, package provenance and trust mutation."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Optional

from .dependencies import DependencyNode, resolve_dependencies
from .manifest import PluginManifestError, parse_manifest
from .models import PluginLifecycleResult
from .registry import (
    PluginRegistry,
    PluginRegistryError,
    ResolvedRegistryPackage,
    RegistryEntry,
    load_registry,
    resolve_package,
)
from .semver import parse_constraint
from .signing import PluginSignatureError, PluginVerification
from .trust import (
    PluginRegistryConfig,
    PluginTrustError,
    TrustedPluginKey,
)

class PluginRegistryMixin:
    def _resolve_registry_bundle(
        self,
        registry: PluginRegistry,
        plugin_name: str,
        constraint: str,
    ) -> dict[str, ResolvedRegistryPackage]:
        root_constraint = constraint or "*"
        parse_constraint(root_constraint)
        constraints: dict[str, tuple[str, ...]] = {
            plugin_name: (root_constraint,)
        }
        selected: dict[str, ResolvedRegistryPackage] = {}

        def choose(name: str, requested: tuple[str, ...]) -> RegistryEntry:
            candidates: list[RegistryEntry] = []
            for entry in registry.entries:
                if entry.name != name or entry.yanked:
                    continue
                if all(
                    parse_constraint(value).matches(entry.version)
                    for value in requested
                ):
                    candidates.append(entry)
            if not candidates:
                rendered = ", ".join(sorted(set(requested)))
                raise PluginRegistryError(
                    f"registry {registry.name!r} 没有 {name}@{rendered} 的可用版本")
            return max(candidates, key=lambda entry: entry.parsed_version)

        # Fixed-point selection: a first broad constraint may select v2, while a
        # later dependency reveals <2. Recompute from fresh constraints so the
        # resolver can settle on a version satisfying the complete intersection.
        for _ in range(128):
            next_selected: dict[str, ResolvedRegistryPackage] = {}
            for name in sorted(constraints):
                entry = choose(name, constraints[name])
                package = resolve_package(registry, name, f"={entry.version}")
                next_selected[name] = package
            next_constraints: dict[str, list[str]] = {
                plugin_name: [root_constraint]
            }
            for name in sorted(next_selected):
                package = next_selected[name]
                for dependency in package.manifest.dependencies:
                    next_constraints.setdefault(dependency.name, []).append(
                        dependency.constraint)
            normalized = {
                name: tuple(sorted(set(values)))
                for name, values in next_constraints.items()
            }
            if len(normalized) > 2_000:
                raise PluginRegistryError("registry 依赖数量过多（>2000）")
            if normalized == constraints and next_selected == selected:
                selected = next_selected
                break
            constraints = normalized
            selected = next_selected
        else:
            raise PluginRegistryError("registry 依赖版本求解未收敛")

        for name, package in selected.items():
            verification = self._verify_manifest(package.manifest)
            if verification.status != "valid":
                raise PluginSignatureError(
                    f"registry 包 {name}@{package.entry.version} 签名无效: "
                    f"{verification.message or verification.status}")
            if verification.registry != registry.name:
                raise PluginSignatureError(
                    f"registry 包 {name} 的签名 registry 不匹配")
        nodes = {
            name: DependencyNode(
                name=name,
                version=package.manifest.version,
                required=package.manifest.dependencies,
                optional=package.manifest.optional_dependencies,
                conflicts=package.manifest.conflicts,
            )
            for name, package in selected.items()
        }
        resolution = resolve_dependencies(nodes)
        if resolution.blocked:
            details = "; ".join(
                f"{name}: {reason}" for name, reason in resolution.blocked.items())
            raise PluginRegistryError(f"registry 依赖解析失败: {details}")
        return selected

    async def install_from_registry(
        self,
        registry_name: str,
        plugin_name: str,
        *,
        constraint: str = "*",
        scope: str = "user",
    ) -> tuple[PluginLifecycleResult, ...]:
        return await asyncio.to_thread(
            self._install_from_registry_sync,
            registry_name,
            plugin_name,
            constraint,
            scope,
        )

    def _install_from_registry_sync(
        self,
        registry_name: str,
        plugin_name: str,
        constraint: str,
        scope: str,
    ) -> tuple[PluginLifecycleResult, ...]:
        registry = load_registry(
            registry_name,
            trust_store=self.trust_store,
            require_signature=True,
        )
        selected = self._resolve_registry_bundle(registry, plugin_name, constraint)
        nodes = {
            name: DependencyNode(
                name=name,
                version=package.manifest.version,
                required=package.manifest.dependencies,
                optional=package.manifest.optional_dependencies,
                conflicts=package.manifest.conflicts,
            )
            for name, package in selected.items()
        }
        order = resolve_dependencies(nodes).order
        results: list[PluginLifecycleResult] = []
        for name in order:
            package = selected[name]
            verification = self._verify_manifest(package.manifest)
            marker = {
                "registry": registry.name,
                "name": name,
                "version": package.manifest.version,
                "key_id": verification.key_id,
                "package_digest": verification.integrity_digest,
                "index_sha256": registry.index_sha256,
            }
            results.append(self._install_sync(
                package.path,
                scope,
                True,
                True,
                marker,
            ))
        return tuple(results)

    async def remove(self, name: str) -> PluginLifecycleResult:
        return await asyncio.to_thread(self._remove_sync, name)

    def _remove_sync(self, name: str) -> PluginLifecycleResult:
        normalized = str(name).strip()
        record = next(
            (item for item in self._records if item.name == normalized), None)
        if record is None:
            raise ValueError(f"未找到插件: {normalized}")
        required_by = sorted(
            manifest.name
            for manifest in self._discovered_manifests.values()
            if manifest.name != normalized
            and any(
                requirement.name == normalized
                for requirement in manifest.dependencies
            )
        )
        if required_by:
            raise ValueError(
                f"插件 {normalized} 仍被以下插件依赖，拒绝移除: "
                + ", ".join(required_by))
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

    # -- trust and registry --------------------------------------------------

    def verify(self, name: str) -> tuple[PluginManifest, PluginVerification]:
        normalized = str(name).strip()
        record = next(
            (item for item in self._records if item.name == normalized), None)
        if record is None:
            raise ValueError(f"未找到插件: {normalized}")
        try:
            manifest = parse_manifest(record.path / "plugin.toml")
        except PluginManifestError as exc:
            raise ValueError(f"插件清单无效: {exc}") from exc
        verification = self._verify_manifest(manifest)
        self._verification[normalized] = verification
        return manifest, verification

    def add_registry(self, name: str, path: str | Path) -> PluginRegistryConfig:
        registry_path = self._expand_path(path).resolve()
        if not registry_path.is_dir():
            raise ValueError(f"registry 目录不存在: {registry_path}")
        if not (registry_path / "registry.json").is_file():
            raise ValueError(f"registry 缺少 registry.json: {registry_path}")
        return self.trust_store.add_registry(name, registry_path)

    def remove_registry(self, name: str) -> PluginRegistryConfig:
        return self.trust_store.remove_registry(name)

    def add_trusted_key(
        self,
        registry: str,
        key_id: str,
        public_key: str,
        *,
        label: str = "",
    ) -> TrustedPluginKey:
        if self.trust_store.registry(registry) is None:
            raise PluginTrustError(f"请先添加 registry: {registry}")
        return self.trust_store.add_key(
            registry, key_id, public_key, label=label)

    def revoke_trusted_key(self, registry: str, key_id: str) -> TrustedPluginKey:
        return self.trust_store.set_revoked(registry, key_id, True)

