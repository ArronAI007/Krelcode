"""Plugin discovery, component loading and basic package lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .components import (
    PluginCommand,
    PluginComponentError,
    load_commands,
    load_hook_definitions,
    load_mcp_servers,
    safe_component_path,
)
from .loader import NamespacedTool, load_entrypoint
from .manifest import (
    PluginManifest,
    PluginManifestError,
    discover_manifest_dirs,
    parse_manifest,
)
from .dependencies import DependencyNode, resolve_dependencies
from .integrity import REGISTRY_MARKER_FILENAME, purge_generated_files
from .registry import (
    PluginRegistry,
    PluginRegistryError,
    ResolvedRegistryPackage,
    RegistryEntry,
    load_registry,
    resolve_package,
)
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

MAX_INSTALL_FILES = 5_000
MAX_INSTALL_BYTES = 100 * 1024 * 1024


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
    scope: str = ""
    manifest_version: int = 1
    skills_count: int = 0
    agents_count: int = 0
    commands_count: int = 0
    hooks_count: int = 0
    mcp_servers_count: int = 0
    trust_status: str = ""
    signer: str = ""
    key_id: str = ""
    registry: str = ""
    integrity_digest: str = ""
    dependencies: Tuple[str, ...] = ()
    dependents: Tuple[str, ...] = ()
    optional_dependencies: Tuple[str, ...] = ()
    blocked_reason: str = ""

    @property
    def components_count(self) -> int:
        return (
            self.tools_count + self.skills_count + self.agents_count
            + self.commands_count + self.hooks_count + self.mcp_servers_count
        )


@dataclass(frozen=True)
class PluginLifecycleResult:
    action: str
    name: str
    scope: str
    path: Path
    message: str = ""


class PluginManager:
    """Directory plugin manager with deterministic scopes and provenance."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        home: Optional[Path] = None,
        extra_paths: Iterable[str | Path] = (),
        enabled: bool = True,
        state_path: Optional[Path] = None,
        trust_policy: Optional[PluginTrustPolicy] = None,
        trust_store: Optional[PluginTrustStore] = None,
        trust_store_path: Optional[Path] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.home = (Path(home).expanduser() if home else Path.home()).resolve()
        self.extra_paths = tuple(
            self._expand_path(path) for path in extra_paths if str(path).strip())
        self.enabled = enabled
        self.state_path = (
            Path(state_path).expanduser()
            if state_path is not None
            else self.home / ".krelcode" / "plugin-state.json"
        )
        self.trust_policy = trust_policy or PluginTrustPolicy()
        self.trust_store = trust_store or PluginTrustStore(
            trust_store_path, home=self.home)
        self._tools: tuple[NamespacedTool, ...] = ()
        self._records: tuple[PluginRecord, ...] = ()
        self._issues: tuple[PluginIssue, ...] = ()
        self._discovered_manifests: dict[str, PluginManifest] = {}
        self._manifests: dict[str, PluginManifest] = {}
        self._scopes: dict[str, str] = {}
        self._commands: tuple[PluginCommand, ...] = ()
        self._skill_paths: tuple[Path, ...] = ()
        self._agent_paths: tuple[Path, ...] = ()
        self._hook_definitions: tuple[Dict[str, Any], ...] = ()
        self._mcp_servers: dict[str, Dict[str, Any]] = {}
        self._verification: dict[str, PluginVerification] = {}
        self._registry_markers: dict[str, dict[str, Any]] = {}

    def _expand_path(self, path: str | Path) -> Path:
        value = Path(path).expanduser()
        return value if value.is_absolute() else (self.workspace_root / value)

    @property
    def user_root(self) -> Path:
        return self.home / ".krelcode" / "plugins"

    @property
    def project_root(self) -> Path:
        return self.workspace_root / ".krelcode" / "plugins"

    def roots(self) -> tuple[Path, ...]:
        roots = [self.user_root, self.project_root, *self.extra_paths]
        unique: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            resolved = root.resolve()
            if resolved not in seen:
                seen.add(resolved)
                unique.append(resolved)
        return tuple(unique)

    def _root_scope(self, root: Path) -> str:
        resolved = root.resolve()
        if resolved == self.user_root.resolve():
            return "user"
        if resolved == self.project_root.resolve():
            return "project"
        return "custom"

    @property
    def tools(self) -> tuple[NamespacedTool, ...]:
        return self._tools

    @property
    def commands(self) -> tuple[PluginCommand, ...]:
        return self._commands

    @property
    def skill_paths(self) -> tuple[Path, ...]:
        return self._skill_paths

    @property
    def agent_paths(self) -> tuple[Path, ...]:
        return self._agent_paths

    @property
    def hook_definitions(self) -> tuple[Dict[str, Any], ...]:
        return self._hook_definitions

    @property
    def mcp_servers(self) -> Mapping[str, Dict[str, Any]]:
        return dict(self._mcp_servers)

    @property
    def issues(self) -> tuple[PluginIssue, ...]:
        return self._issues

    def records(self) -> tuple[PluginRecord, ...]:
        return self._records

    def get_command(self, name: str) -> Optional[PluginCommand]:
        normalized = str(name).strip().lstrip("/")
        return next(
            (command for command in self._commands if command.name == normalized),
            None,
        )

    # -- state and discovery -------------------------------------------------

    def _load_state(self) -> Dict[str, bool]:
        if not self.state_path.is_file():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        plugins = raw.get("plugins") if isinstance(raw, dict) else None
        if not isinstance(plugins, dict):
            return {}
        result: Dict[str, bool] = {}
        for name, value in plugins.items():
            if isinstance(value, dict) and isinstance(value.get("enabled"), bool):
                result[str(name)] = bool(value["enabled"])
        return result

    def _write_state(self, state: Mapping[str, bool]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "plugins": {
                name: {"enabled": bool(enabled)}
                for name, enabled in sorted(state.items())
            },
        }
        fd, temporary = tempfile.mkstemp(
            prefix=".plugin-state-", suffix=".tmp",
            dir=str(self.state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def _verify_manifest(self, manifest: PluginManifest) -> PluginVerification:
        """Verify a package before any component code is imported."""

        try:
            metadata = read_signature(manifest.plugin_dir)
        except PluginSignatureError as exc:
            return PluginVerification("invalid", message=str(exc))
        public_key = None
        signer = ""
        revoked = False
        if metadata is not None:
            trusted = self.trust_store.key(metadata.registry, metadata.key_id)
            if trusted is not None:
                signer = trusted.label or trusted.key_id
                revoked = trusted.revoked
                if not revoked:
                    public_key = self.trust_store.public_key(
                        metadata.registry, metadata.key_id)
        return verify_plugin(
            manifest.plugin_dir,
            expected_name=manifest.name,
            expected_version=manifest.version,
            public_key=public_key,
            signer=signer,
            key_revoked=revoked,
        )

    def _record_for(
        self,
        manifest: PluginManifest,
        scope: str,
        status: str,
        verification: PluginVerification | None = None,
        *,
        dependents: Iterable[str] = (),
        error: str = "",
        blocked_reason: str = "",
        **counts: int,
    ) -> PluginRecord:
        check = verification or PluginVerification("not_checked")
        return PluginRecord(
            manifest.name,
            status,
            manifest.plugin_dir,
            version=manifest.version,
            description=manifest.description,
            entrypoint=manifest.entrypoint,
            error=error,
            scope=scope,
            manifest_version=manifest.manifest_version,
            trust_status=check.status,
            signer=check.signer,
            key_id=check.key_id,
            registry=check.registry,
            integrity_digest=check.integrity_digest,
            dependencies=tuple(item.describe() for item in manifest.dependencies),
            dependents=tuple(sorted(dependents)),
            optional_dependencies=tuple(
                item.describe() for item in manifest.optional_dependencies),
            blocked_reason=blocked_reason,
            **counts,
        )

    async def load(self) -> tuple[PluginRecord, ...]:
        if not self.enabled:
            self._clear_loaded()
            return ()
        try:
            self.trust_store.reload()
        except (PluginTrustError, PluginSignatureError) as exc:
            self._clear_loaded()
            self._issues = (PluginIssue(self.trust_store.path, str(exc)),)
            raise
        manifests: dict[str, PluginManifest] = {}
        scopes: dict[str, str] = {}
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
                scopes[manifest.name] = self._root_scope(root)

        state = self._load_state()
        effective_enabled: dict[str, bool] = {}
        verification: dict[str, PluginVerification] = {}
        blocked: dict[str, str] = {}
        untrusted: set[str] = set()
        for name, manifest in manifests.items():
            scope = scopes.get(name, "custom")
            effective_enabled[name] = manifest.enabled and state.get(
                f"{scope}:{name}", state.get(name, True))
            if not effective_enabled[name]:
                continue
            check = self._verify_manifest(manifest)
            verification[name] = check
            allowed, reason = self.trust_policy.allows(check)
            if not allowed:
                blocked[name] = reason
                untrusted.add(name)
                issues.append(PluginIssue(
                    manifest.path,
                    f"签名策略阻止加载: {reason}"))

        dependency_nodes = {
            name: DependencyNode(
                name=name,
                version=manifest.version,
                required=manifest.dependencies,
                optional=manifest.optional_dependencies,
                conflicts=manifest.conflicts,
            )
            for name, manifest in manifests.items()
            if effective_enabled[name]
        }
        disabled = {name for name, enabled in effective_enabled.items() if not enabled}
        if self.trust_policy.enforce_dependencies:
            resolution = resolve_dependencies(dependency_nodes, disabled=disabled)
            for name, reason in resolution.blocked.items():
                blocked.setdefault(name, reason)
            for diagnostic in resolution.diagnostics:
                manifest = manifests.get(diagnostic.plugin)
                path = manifest.path if manifest is not None else self.workspace_root
                message = diagnostic.message
                if not any(
                    issue.path == path and issue.message == message for issue in issues
                ):
                    issues.append(PluginIssue(path, message))
        else:
            resolution = resolve_dependencies(dependency_nodes)
            resolution = type(resolution)(
                order=tuple(sorted(dependency_nodes)),
                blocked={},
                diagnostics=(),
                dependents=resolution.dependents,
            )

        # Signature failures block every transitive dependent as well.
        changed = True
        while changed:
            changed = False
            for name, node in dependency_nodes.items():
                if name in blocked:
                    continue
                for requirement in node.required:
                    if requirement.name in blocked:
                        blocked[name] = (
                            f"依赖 {requirement.name} 无法加载: {blocked[requirement.name]}")
                        changed = True
                        break

        records: dict[str, PluginRecord] = {}
        for name in sorted(manifests):
            manifest = manifests[name]
            scope = scopes.get(name, "custom")
            dependents = resolution.dependents.get(name, ())
            if not effective_enabled[name]:
                records[name] = self._record_for(
                    manifest, scope, "disabled", dependents=dependents)
                continue
            check = verification.get(name, PluginVerification("not_checked"))
            if name in blocked:
                status = "untrusted" if name in untrusted else "blocked"
                records[name] = self._record_for(
                    manifest,
                    scope,
                    status,
                    check,
                    dependents=dependents,
                    error=blocked[name],
                    blocked_reason=blocked[name],
                )

        tools: list[NamespacedTool] = []
        commands: list[PluginCommand] = []
        skill_paths: list[Path] = []
        agent_paths: list[Path] = []
        hook_definitions: list[Dict[str, Any]] = []
        mcp_servers: dict[str, Dict[str, Any]] = {}
        loaded_manifests: dict[str, PluginManifest] = {}
        loaded_names: set[str] = set()

        for name in resolution.order:
            if name in blocked or name not in manifests:
                continue
            manifest = manifests[name]
            scope = scopes.get(name, "custom")
            check = verification.get(name, PluginVerification("not_checked"))
            dependency_failures = [
                requirement.name
                for requirement in manifest.dependencies
                if requirement.name not in loaded_names
            ]
            if dependency_failures:
                reason = f"依赖加载失败: {', '.join(sorted(dependency_failures))}"
                blocked[name] = reason
                records[name] = self._record_for(
                    manifest,
                    scope,
                    "blocked",
                    check,
                    dependents=resolution.dependents.get(name, ()),
                    error=reason,
                    blocked_reason=reason,
                )
                issues.append(PluginIssue(manifest.path, reason))
                continue
            try:
                if check.status == "valid":
                    purge_generated_files(manifest.plugin_dir)
                loaded_tools = (
                    await load_entrypoint(
                        manifest.plugin_dir,
                        manifest.entrypoint,
                        manifest.name,
                        manifest.version,
                    )
                    if manifest.entrypoint else []
                )
                plugin_commands = load_commands(manifest, scope=scope)
                plugin_hooks = load_hook_definitions(manifest)
                plugin_mcp = load_mcp_servers(manifest)
                plugin_skills = tuple(
                    safe_component_path(manifest.plugin_dir, path)
                    for path in manifest.skill_paths
                )
                plugin_agents = tuple(
                    safe_component_path(manifest.plugin_dir, path)
                    for path in manifest.agent_paths
                )
                for path in (*plugin_skills, *plugin_agents):
                    if not path.exists():
                        raise PluginComponentError(
                            f"组件路径不存在: {path.relative_to(manifest.plugin_dir)}")
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                issues.append(PluginIssue(manifest.path, message))
                records[name] = self._record_for(
                    manifest,
                    scope,
                    "error",
                    check,
                    dependents=resolution.dependents.get(name, ()),
                    error=message,
                )
                continue

            tools.extend(loaded_tools)
            commands.extend(plugin_commands)
            hook_definitions.extend(plugin_hooks)
            mcp_servers.update(plugin_mcp)
            skill_paths.extend(plugin_skills)
            agent_paths.extend(plugin_agents)
            loaded_manifests[name] = manifest
            loaded_names.add(name)
            records[name] = self._record_for(
                manifest,
                scope,
                "loaded",
                check,
                dependents=resolution.dependents.get(name, ()),
                tools_count=len(loaded_tools),
                skills_count=len(plugin_skills),
                agents_count=len(plugin_agents),
                commands_count=len(plugin_commands),
                hooks_count=len(plugin_hooks),
                mcp_servers_count=len(plugin_mcp),
            )

        self._tools = tuple(tools)
        self._records = tuple(records[name] for name in sorted(records))
        self._issues = tuple(issues)
        self._discovered_manifests = dict(manifests)
        self._manifests = loaded_manifests
        self._scopes = {name: scopes[name] for name in loaded_manifests}
        self._commands = tuple(commands)
        self._skill_paths = tuple(dict.fromkeys(skill_paths))
        self._agent_paths = tuple(dict.fromkeys(agent_paths))
        self._hook_definitions = tuple(hook_definitions)
        self._mcp_servers = mcp_servers
        self._verification = verification
        return self._records

    def _clear_loaded(self) -> None:
        self._tools = ()
        self._records = ()
        self._issues = ()
        self._discovered_manifests = {}
        self._manifests = {}
        self._scopes = {}
        self._commands = ()
        self._skill_paths = ()
        self._agent_paths = ()
        self._hook_definitions = ()
        self._mcp_servers = {}
        self._verification = {}
        self._registry_markers = {}

    async def reload(self) -> tuple[PluginRecord, ...]:
        return await self.load()

    # -- lifecycle -----------------------------------------------------------

    def _lifecycle_root(self, scope: str) -> Path:
        normalized = str(scope).strip().lower() or "user"
        if normalized == "user":
            return self.user_root
        if normalized == "project":
            return self.project_root
        raise ValueError("scope 必须是 user 或 project")

    @staticmethod
    def _validate_source_tree(source: Path) -> None:
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

    async def install(
        self,
        source: str | Path,
        *,
        scope: str = "user",
        replace: bool = False,
        require_signature: Optional[bool] = None,
    ) -> PluginLifecycleResult:
        result = await asyncio.to_thread(
            self._install_sync,
            Path(source),
            scope,
            replace,
            require_signature,
            None,
        )
        return result

    def _install_sync(
        self,
        source: Path,
        scope: str,
        replace: bool,
        require_signature: Optional[bool] = None,
        registry_metadata: Optional[Mapping[str, Any]] = None,
    ) -> PluginLifecycleResult:
        source = source.expanduser().resolve()
        manifest_path = source / "plugin.toml"
        if not manifest_path.is_file():
            raise ValueError(f"安装源缺少 plugin.toml: {source}")
        manifest = parse_manifest(manifest_path)
        self._validate_source_tree(source)
        verification = self._verify_manifest(manifest)
        allowed, reason = self.trust_policy.allows(verification)
        signature_required = (
            self.trust_policy.require_signature_for_install
            if require_signature is None else require_signature
        )
        if verification.status != "valid" and signature_required:
            raise PluginSignatureError(
                f"安装要求有效签名: {verification.message or verification.status}")
        if not allowed:
            raise PluginSignatureError(f"安装被签名策略阻止: {reason}")
        root = self._lifecycle_root(scope).resolve()
        root.mkdir(parents=True, exist_ok=True)
        destination = root / manifest.name
        is_update = destination.exists()
        if destination.exists() and not replace:
            raise ValueError(f"插件已安装: {manifest.name} ({destination})")
        previous_manifest: PluginManifest | None = None
        previous_verification: PluginVerification | None = None
        if replace and destination.exists():
            try:
                previous_manifest = parse_manifest(destination / "plugin.toml")
                previous_verification = self._verify_manifest(previous_manifest)
            except PluginManifestError as exc:
                raise ValueError(f"已安装插件清单无效，拒绝覆盖: {exc}") from exc
            try:
                if Version.parse(manifest.version) < Version.parse(previous_manifest.version):
                    raise ValueError(
                        f"拒绝降级 {previous_manifest.version} -> {manifest.version}")
            except VersionError as exc:
                raise ValueError(f"无法比较插件版本: {exc}") from exc
            if previous_verification.key_id:
                if verification.status != "valid":
                    raise PluginSignatureError("已签名插件更新必须保持有效签名")
                if (
                    previous_verification.registry != verification.registry
                    or previous_verification.key_id != verification.key_id
                ):
                    raise PluginSignatureError("插件更新不能静默更换签名者或 registry")
        staging = root / f".staging-{manifest.name}-{uuid.uuid4().hex}"
        trash = root / ".trash"
        backup: Optional[Path] = None
        try:
            shutil.copytree(source, staging, symlinks=False)
            marker = staging / REGISTRY_MARKER_FILENAME
            if registry_metadata is None:
                try:
                    marker.unlink()
                except FileNotFoundError:
                    pass
            else:
                marker_payload = {"schema": 1, **dict(registry_metadata)}
                marker.write_text(
                    json.dumps(marker_payload, ensure_ascii=False, sort_keys=True, indent=2)
                    + "\n",
                    encoding="utf-8",
                )
                os.chmod(marker, 0o600)
            staged_manifest = parse_manifest(staging / "plugin.toml")
            staged_verification = self._verify_manifest(staged_manifest)
            if staged_manifest.name != manifest.name or staged_manifest.version != manifest.version:
                raise PluginSignatureError("安装源在复制期间发生变化")
            if verification.status == "valid":
                if (
                    staged_verification.status != "valid"
                    or staged_verification.integrity_digest != verification.integrity_digest
                ):
                    raise PluginSignatureError("安装源在复制/校验期间发生变化")
            if registry_metadata is not None:
                expected_registry = str(registry_metadata.get("registry") or "")
                expected_name = str(registry_metadata.get("name") or manifest.name)
                expected_version = str(
                    registry_metadata.get("version") or manifest.version)
                if (
                    staged_verification.status != "valid"
                    or staged_verification.registry != expected_registry
                    or staged_manifest.name != expected_name
                    or staged_manifest.version != expected_version
                ):
                    raise PluginSignatureError("registry 包在安装期间身份发生变化")
            if destination.exists():
                trash.mkdir(parents=True, exist_ok=True)
                backup = trash / f"{manifest.name}-{uuid.uuid4().hex}"
                os.replace(destination, backup)
            os.replace(staging, destination)
        except Exception:
            if backup is not None and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        action = "update" if is_update else "install"
        return PluginLifecycleResult(
            action=action,
            name=manifest.name,
            scope=scope,
            path=destination,
            message=f"{action}ed {manifest.name}@{manifest.version}",
        )

    async def update(
        self,
        source: str | Path,
        *,
        scope: str = "user",
    ) -> PluginLifecycleResult:
        return await self.install(source, scope=scope, replace=True)

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
