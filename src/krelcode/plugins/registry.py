"""Trusted local plugin registry index with signed package resolution."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from .integrity import PluginIntegrityError, hash_bytes, safe_relative_path
from .manifest import PluginManifest, PluginManifestError, parse_manifest
from .semver import Version, VersionError, highest, parse_constraint
from .signing import (
    SIGNATURE_ALGORITHM,
    PluginSignatureError,
    canonical_json_bytes,
    sign,
    verify,
)
from .trust import PluginRegistryConfig, PluginTrustStore


REGISTRY_SCHEMA = 1
REGISTRY_INDEX_FILENAME = "registry.json"
REGISTRY_SIGNATURE_FILENAME = "registry.sig.json"
MAX_REGISTRY_INDEX_BYTES = 2 * 1024 * 1024
MAX_REGISTRY_ENTRIES = 2_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PluginRegistryError(ValueError):
    """Registry metadata is malformed, untrusted or points outside its root."""


@dataclass(frozen=True)
class RegistryEntry:
    name: str
    version: str
    path: str
    description: str = ""
    yanked: bool = False

    @property
    def parsed_version(self) -> Version:
        return Version.parse(self.version)


@dataclass(frozen=True)
class PluginRegistry:
    name: str
    root: Path
    entries: tuple[RegistryEntry, ...]
    index_sha256: str

    def versions(self, name: str) -> tuple[str, ...]:
        return tuple(
            entry.version for entry in self.entries
            if entry.name == name and not entry.yanked)

    def resolve(self, name: str, constraint: str = "*") -> RegistryEntry:
        parse_constraint(constraint)
        selected = highest(self.versions(name), constraint)
        if selected is None:
            raise PluginRegistryError(
                f"registry {self.name!r} 没有 {name}@{constraint} 的可用版本")
        matches = [
            entry for entry in self.entries
            if entry.name == name and Version.parse(entry.version) == selected
            and not entry.yanked
        ]
        if not matches:
            raise PluginRegistryError(f"无法解析 registry 条目: {name}@{constraint}")
        return sorted(matches, key=lambda entry: entry.path)[0]

    def package_path(self, entry: RegistryEntry) -> Path:
        relative = PurePosixPath(str(entry.path).replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise PluginRegistryError(f"registry 包路径越界: {entry.path!r}")
        candidate = self.root.joinpath(*relative.parts)
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise PluginRegistryError(f"registry 包路径不能包含符号链接: {current}")
        try:
            safe_relative_path(self.root, candidate)
        except PluginIntegrityError as exc:
            raise PluginRegistryError(str(exc)) from exc
        if not candidate.is_dir():
            raise PluginRegistryError(f"registry 包目录不存在: {candidate}")
        return candidate


@dataclass(frozen=True)
class RegistryIndexVerification:
    verified: bool
    key_id: str
    message: str
    index_sha256: str


def _read_json_object(path: Path, *, max_bytes: int = MAX_REGISTRY_INDEX_BYTES) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PluginRegistryError(f"registry 文件必须是普通文件: {path}")
    try:
        if path.stat().st_size > max_bytes:
            raise PluginRegistryError(f"registry 文件过大: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PluginRegistryError(f"无法读取 registry 文件 {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PluginRegistryError(f"registry 文件必须是 JSON object: {path}")
    return raw


def _registry_signature_payload(
    *,
    registry: str,
    key_id: str,
    index_sha256: str,
) -> dict[str, Any]:
    return {
        "schema": REGISTRY_SCHEMA,
        "kind": "registry-index",
        "registry": registry,
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": key_id,
        "index_sha256": index_sha256,
    }


def create_registry_signature(
    *,
    registry: str,
    index_bytes: bytes,
    key_id: str,
    seed: bytes,
) -> dict[str, Any]:
    """Release helper for registry.sig.json and interoperability tests."""

    payload = _registry_signature_payload(
        registry=registry,
        key_id=key_id,
        index_sha256=hash_bytes(index_bytes),
    )
    signature = sign(canonical_json_bytes(payload), seed)
    return {
        **payload,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def verify_registry_index(
    *,
    config: PluginRegistryConfig,
    index_bytes: bytes,
    trust_store: PluginTrustStore,
    require_signature: bool,
) -> tuple[RegistryIndexVerification, Optional[bytes]]:
    index_sha256 = hash_bytes(index_bytes)
    path = Path(config.path).expanduser() / REGISTRY_SIGNATURE_FILENAME
    if not path.exists():
        if require_signature:
            return RegistryIndexVerification(
                False, "", "registry index 缺少 registry.sig.json", index_sha256), None
        return RegistryIndexVerification(
            True, "", "unsigned registry index（偏好模式允许）", index_sha256), None
    raw = _read_json_object(path, max_bytes=64 * 1024)
    try:
        schema = int(raw.get("schema", 0) or 0)
        algorithm = str(raw.get("algorithm") or "")
        registry = str(raw.get("registry") or "")
        key_id = str(raw.get("key_id") or "")
        signed_sha = str(raw.get("index_sha256") or "")
        signature = base64.b64decode(str(raw.get("signature") or ""), validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        return RegistryIndexVerification(
            False, "", f"registry.sig.json 无效: {exc}", index_sha256), None
    if schema != REGISTRY_SCHEMA or raw.get("kind") != "registry-index":
        return RegistryIndexVerification(
            False, key_id, "registry 签名 schema/kind 不受支持", index_sha256), None
    if algorithm != SIGNATURE_ALGORITHM or len(signature) != 64:
        return RegistryIndexVerification(
            False, key_id, "registry 签名算法或长度无效", index_sha256), None
    if registry != config.name:
        return RegistryIndexVerification(
            False, key_id, "registry 签名中的名称不匹配", index_sha256), None
    if signed_sha != index_sha256:
        return RegistryIndexVerification(
            False, key_id, "registry index Hash 不匹配", index_sha256), None
    if not _SHA256_RE.fullmatch(signed_sha):
        return RegistryIndexVerification(
            False, key_id, "registry index Hash 格式无效", index_sha256), None
    trusted = trust_store.key(config.name, key_id)
    if trusted is None:
        return RegistryIndexVerification(
            False, key_id, "registry 签名公钥不受信任", index_sha256), None
    if trusted.revoked:
        return RegistryIndexVerification(
            False, key_id, "registry 签名公钥已撤销", index_sha256), None
    public_key = trust_store.public_key(config.name, key_id)
    payload = _registry_signature_payload(
        registry=registry, key_id=key_id, index_sha256=signed_sha)
    ok = public_key is not None and verify(
        canonical_json_bytes(payload), signature, public_key)
    return RegistryIndexVerification(
        ok,
        key_id,
        "registry index 签名有效" if ok else "registry index 签名验证失败",
        index_sha256,
    ), public_key


def load_registry(
    name: str,
    *,
    trust_store: PluginTrustStore,
    require_signature: bool = True,
) -> PluginRegistry:
    config = trust_store.registry(name)
    if config is None:
        raise PluginRegistryError(f"registry 不受信任或未配置: {name}")
    if not config.enabled:
        raise PluginRegistryError(f"registry 已禁用: {name}")
    root = Path(config.path).expanduser().resolve()
    index_path = root / REGISTRY_INDEX_FILENAME
    if index_path.is_symlink() or not index_path.is_file():
        raise PluginRegistryError(f"registry 缺少普通索引文件: {index_path}")
    try:
        index_bytes = index_path.read_bytes()
    except OSError as exc:
        raise PluginRegistryError(f"无法读取 registry index: {exc}") from exc
    verification, _ = verify_registry_index(
        config=PluginRegistryConfig(config.name, str(root), config.enabled),
        index_bytes=index_bytes,
        trust_store=trust_store,
        require_signature=require_signature,
    )
    if not verification.verified:
        raise PluginRegistryError(verification.message)
    try:
        raw = json.loads(index_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PluginRegistryError(f"registry.json 不是合法 UTF-8 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PluginRegistryError("registry.json 必须是 JSON object")
    if int(raw.get("schema", 0) or 0) != REGISTRY_SCHEMA:
        raise PluginRegistryError("registry.json schema 不受支持")
    if str(raw.get("name") or "") != config.name:
        raise PluginRegistryError("registry.json name 与信任配置不一致")
    values = raw.get("plugins")
    if not isinstance(values, list):
        raise PluginRegistryError("registry.json plugins 必须是数组")
    if len(values) > MAX_REGISTRY_ENTRIES:
        raise PluginRegistryError("registry 条目过多")
    entries: list[RegistryEntry] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        if not isinstance(value, dict):
            raise PluginRegistryError("registry plugin 条目必须是 object")
        yanked = value.get("yanked", False)
        if not isinstance(yanked, bool):
            raise PluginRegistryError("registry 条目 yanked 必须是布尔值")
        entry = RegistryEntry(
            name=str(value.get("name") or "").strip(),
            version=str(value.get("version") or "").strip(),
            path=str(value.get("path") or "").strip(),
            description=" ".join(str(value.get("description") or "").split())[:512],
            yanked=yanked,
        )
        if not entry.name or not entry.path:
            raise PluginRegistryError("registry 条目缺少 name/path")
        try:
            Version.parse(entry.version)
        except VersionError as exc:
            raise PluginRegistryError(str(exc)) from exc
        key = (entry.name, entry.version)
        if key in seen:
            raise PluginRegistryError(f"registry 条目重复: {entry.name}@{entry.version}")
        seen.add(key)
        entries.append(entry)
    return PluginRegistry(
        name=config.name,
        root=root,
        entries=tuple(sorted(entries, key=lambda item: (item.name, item.parsed_version, item.path))),
        index_sha256=verification.index_sha256,
    )


@dataclass(frozen=True)
class ResolvedRegistryPackage:
    registry: PluginRegistry
    entry: RegistryEntry
    manifest: PluginManifest
    path: Path


def resolve_package(
    registry: PluginRegistry,
    name: str,
    constraint: str = "*",
) -> ResolvedRegistryPackage:
    entry = registry.resolve(name, constraint)
    path = registry.package_path(entry)
    try:
        manifest = parse_manifest(path / "plugin.toml")
    except PluginManifestError as exc:
        raise PluginRegistryError(
            f"registry 包 manifest 无效 {entry.name}@{entry.version}: {exc}") from exc
    if manifest.name != entry.name or manifest.version != entry.version:
        raise PluginRegistryError(
            f"registry 条目与包 manifest 不一致: {entry.name}@{entry.version} != "
            f"{manifest.name}@{manifest.version}")
    return ResolvedRegistryPackage(registry, entry, manifest, path)
