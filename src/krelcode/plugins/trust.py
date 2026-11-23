"""Persistent plugin trust policy, registry configuration and public keys."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .signing import (
    PluginSignatureError,
    PluginVerification,
    decode_public_key,
    encode_public_key,
)


TRUST_MODES = ("off", "prefer", "signed", "strict")
_TRUST_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class PluginTrustError(ValueError):
    """Trust store configuration is malformed or unsafe."""


@dataclass(frozen=True)
class PluginTrustPolicy:
    mode: str = "prefer"
    allow_local_unsigned: bool = True
    require_signature_for_install: bool = False
    enforce_dependencies: bool = True

    def __post_init__(self) -> None:
        normalized = self.mode.strip().lower()
        if normalized not in TRUST_MODES:
            raise PluginTrustError(
                f"plugins.trust_mode 必须是 {', '.join(TRUST_MODES)}")
        object.__setattr__(self, "mode", normalized)

    @classmethod
    def from_config(
        cls,
        section: Mapping[str, Any] | None,
        *,
        trust_section: Mapping[str, Any] | None = None,
    ) -> "PluginTrustPolicy":
        plugins = dict(section or {})
        trust = dict(trust_section or {})
        mode = str(trust.get("mode", plugins.get("trust_mode", "prefer")))
        allow_unsigned = trust.get(
            "allow_local_unsigned",
            plugins.get("allow_local_unsigned", True),
        )
        require_install = trust.get(
            "require_signature_for_install",
            plugins.get("require_signature_for_install", False),
        )
        enforce_dependencies = trust.get(
            "enforce_dependencies",
            plugins.get("enforce_dependencies", True),
        )
        for field, value in (
            ("allow_local_unsigned", allow_unsigned),
            ("require_signature_for_install", require_install),
            ("enforce_dependencies", enforce_dependencies),
        ):
            if not isinstance(value, bool):
                raise PluginTrustError(f"plugins.trust.{field} 必须是布尔值")
        return cls(
            mode=mode,
            allow_local_unsigned=allow_unsigned,
            require_signature_for_install=require_install,
            enforce_dependencies=enforce_dependencies,
        )

    def allows(self, verification: PluginVerification) -> tuple[bool, str]:
        if verification.status == "valid":
            return True, ""
        if verification.status == "unsigned":
            if self.mode == "strict":
                return False, "strict 模式要求所有插件具有可信签名"
            if self.mode == "off":
                return True, ""
            if self.allow_local_unsigned:
                return True, ""
            return False, "当前策略不允许未签名插件"
        if self.mode == "off":
            return True, ""
        return False, verification.message or f"签名状态不可信: {verification.status}"


@dataclass(frozen=True)
class TrustedPluginKey:
    key_id: str
    public_key: str
    registry: str
    label: str = ""
    revoked: bool = False

    def __post_init__(self) -> None:
        for field, value in (("key_id", self.key_id), ("registry", self.registry)):
            if not _TRUST_NAME_RE.fullmatch(str(value or "")):
                raise PluginTrustError(f"非法 {field}: {value!r}")
        decode_public_key(self.public_key)

    @property
    def fingerprint(self) -> str:
        raw = decode_public_key(self.public_key)
        return hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class PluginRegistryConfig:
    name: str
    path: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if not _TRUST_NAME_RE.fullmatch(str(self.name or "")):
            raise PluginTrustError(f"非法 registry 名称: {self.name!r}")
        if not str(self.path or "").strip():
            raise PluginTrustError("registry path 不能为空")


class PluginTrustStore:
    """A user-owned trust root; no private key material is ever stored here."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        home: Optional[Path] = None,
        load: bool = True,
    ) -> None:
        if path is None:
            root = Path(home).expanduser() if home else Path.home()
            path = root / ".krelcode" / "plugin-trust.json"
        self.path = Path(path).expanduser()
        self._keys: dict[str, dict[str, TrustedPluginKey]] = {}
        self._registries: dict[str, PluginRegistryConfig] = {}
        if load:
            self.reload()

    def reload(self) -> None:
        self._keys = {}
        self._registries = {}
        if not self.path.exists():
            return
        if self.path.is_symlink() or not self.path.is_file():
            raise PluginTrustError("plugin-trust.json 必须是普通文件")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PluginTrustError(f"无法读取信任库 {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise PluginTrustError("plugin-trust.json 必须是 schema=1 的 object")
        try:
            schema = int(raw.get("schema", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise PluginTrustError("plugin-trust.json schema 必须是整数") from exc
        if schema != 1:
            raise PluginTrustError("plugin-trust.json 必须是 schema=1 的 object")
        registries = raw.get("registries", {})
        keys = raw.get("keys", {})
        if not isinstance(registries, dict) or not isinstance(keys, dict):
            raise PluginTrustError("信任库 registries/keys 必须是 object")
        for name, value in registries.items():
            if not isinstance(value, dict):
                raise PluginTrustError(f"registry {name!r} 配置必须是 object")
            enabled = value.get("enabled", True)
            if not isinstance(enabled, bool):
                raise PluginTrustError(f"registry {name!r}.enabled 必须是布尔值")
            raw_path = Path(str(value.get("path") or "")).expanduser()
            if not raw_path.is_absolute():
                raw_path = (self.path.parent / raw_path).resolve()
            registry = PluginRegistryConfig(
                name=str(name),
                path=str(raw_path),
                enabled=enabled,
            )
            self._registries[registry.name] = registry
        for registry_name, registry_keys in keys.items():
            if not isinstance(registry_keys, dict):
                raise PluginTrustError(f"registry {registry_name!r} keys 必须是 object")
            for key_id, value in registry_keys.items():
                if not isinstance(value, dict):
                    raise PluginTrustError(f"key {key_id!r} 配置必须是 object")
                revoked = value.get("revoked", False)
                if not isinstance(revoked, bool):
                    raise PluginTrustError(f"key {key_id!r}.revoked 必须是布尔值")
                key = TrustedPluginKey(
                    key_id=str(key_id),
                    public_key=str(value.get("public_key") or ""),
                    registry=str(registry_name),
                    label=str(value.get("label") or ""),
                    revoked=revoked,
                )
                self._keys.setdefault(key.registry, {})[key.key_id] = key

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "registries": {
                name: {"path": item.path, "enabled": item.enabled}
                for name, item in sorted(self._registries.items())
            },
            "keys": {
                registry: {
                    key_id: {
                        "public_key": key.public_key,
                        "label": key.label,
                        "revoked": key.revoked,
                    }
                    for key_id, key in sorted(keys.items())
                }
                for registry, keys in sorted(self._keys.items())
            },
        }
        fd, temporary = tempfile.mkstemp(
            prefix=".plugin-trust-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def registries(self) -> tuple[PluginRegistryConfig, ...]:
        return tuple(self._registries[name] for name in sorted(self._registries))

    def registry(self, name: str) -> Optional[PluginRegistryConfig]:
        return self._registries.get(str(name).strip())

    def add_registry(self, name: str, path: str | Path, *, enabled: bool = True) -> PluginRegistryConfig:
        config = PluginRegistryConfig(str(name).strip(), str(path), enabled=enabled)
        self._registries[config.name] = config
        self._save()
        return config

    def remove_registry(self, name: str) -> PluginRegistryConfig:
        normalized = str(name).strip()
        if normalized not in self._registries:
            raise PluginTrustError(f"未找到 registry: {normalized}")
        removed = self._registries.pop(normalized)
        self._keys.pop(normalized, None)
        self._save()
        return removed

    def keys(self, registry: Optional[str] = None) -> tuple[TrustedPluginKey, ...]:
        if registry is not None:
            return tuple(self._keys.get(str(registry), {}).values())
        return tuple(
            key
            for registry_name in sorted(self._keys)
            for key in sorted(self._keys[registry_name].values(), key=lambda item: item.key_id)
        )

    def key(self, registry: str, key_id: str) -> Optional[TrustedPluginKey]:
        return self._keys.get(str(registry), {}).get(str(key_id))

    def public_key(self, registry: str, key_id: str) -> bytes | None:
        trusted = self.key(registry, key_id)
        if trusted is None or trusted.revoked:
            return None
        return decode_public_key(trusted.public_key)

    def add_key(
        self,
        registry: str,
        key_id: str,
        public_key: str,
        *,
        label: str = "",
    ) -> TrustedPluginKey:
        try:
            key = TrustedPluginKey(
                key_id=str(key_id).strip(),
                public_key=str(public_key).strip(),
                registry=str(registry).strip(),
                label=str(label).strip(),
            )
        except PluginSignatureError as exc:
            raise PluginTrustError(str(exc)) from exc
        existing = self.key(key.registry, key.key_id)
        if existing is not None and existing.public_key != key.public_key:
            raise PluginTrustError(
                f"可信公钥 {key.registry}/{key.key_id} 已存在且内容不同；"
                "请先撤销旧 key-id")
        self._keys.setdefault(key.registry, {})[key.key_id] = key
        self._save()
        return key

    def set_revoked(self, registry: str, key_id: str, revoked: bool = True) -> TrustedPluginKey:
        existing = self.key(registry, key_id)
        if existing is None:
            raise PluginTrustError(f"未找到可信公钥: {registry}/{key_id}")
        key = TrustedPluginKey(
            key_id=existing.key_id,
            public_key=existing.public_key,
            registry=existing.registry,
            label=existing.label,
            revoked=revoked,
        )
        self._keys[key.registry][key.key_id] = key
        self._save()
        return key

    def status_lines(self) -> tuple[str, ...]:
        lines = [f"  [plugin-trust] {self.path}"]
        registries = self.registries()
        if not registries:
            lines.append("  registry: （无）")
        for registry in registries:
            state = "enabled" if registry.enabled else "disabled"
            lines.append(f"  registry {registry.name} [{state}] · {registry.path}")
        keys = self.keys()
        if not keys:
            lines.append("  trusted keys: （无）")
        for key in keys:
            state = "revoked" if key.revoked else "active"
            suffix = f" · {key.label}" if key.label else ""
            lines.append(
                f"  key {key.registry}/{key.key_id} [{state}] · "
                f"sha256:{key.fingerprint}{suffix}")
        return tuple(lines)
