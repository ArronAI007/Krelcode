"""Layered TOML configuration loading.

Merge order (lowest to highest precedence)::

    user -> project -> local -> managed

* user:    ``~/.krelcode/config.toml``
* project: ``<workspace>/krelcode.toml``
* local:   ``<workspace>/krelcode.local.toml`` (machine/user overrides)
* managed: ``KRELCODE_MANAGED_CONFIG`` or the platform-wide policy path

Dictionaries are merged recursively. Arrays and scalar values are replaced by the
higher-precedence layer, which keeps list semantics explicit instead of silently
duplicating hooks, plugin roots or MCP declarations.

An explicit ``--config`` path keeps the historical escape hatch: it is loaded as
an exclusive configuration source and is not merged with machine/project files.
"""

from __future__ import annotations

import copy
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .core.errors import ConfigError

CONFIG_SCOPES = ("user", "project", "local", "managed")
# Kept for compatibility with doctor/tests that inspect default project paths.
DEFAULT_PATHS = (
    Path("krelcode.toml"),
    Path("krelcode.local.toml"),
    Path.home() / ".krelcode" / "config.toml",
)


@dataclass(frozen=True)
class ConfigLayer:
    scope: str
    path: Path
    exists: bool
    data: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class ConfigBundle:
    """Merged configuration and the provenance of every participating layer."""

    data: Dict[str, Any]
    layers: Tuple[ConfigLayer, ...] = ()

    @property
    def sources(self) -> Tuple[Path, ...]:
        return tuple(layer.path for layer in self.layers if layer.exists)

    @property
    def errors(self) -> Tuple[ConfigLayer, ...]:
        return tuple(layer for layer in self.layers if layer.error)


def _managed_config_path(environ: Mapping[str, str]) -> Path:
    explicit = str(environ.get("KRELCODE_MANAGED_CONFIG") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if sys.platform == "darwin":
        return Path("/Library/Application Support/Krelcode/config.toml")
    if os.name == "nt":
        root = Path(environ.get("PROGRAMDATA") or r"C:\ProgramData")
        return root / "Krelcode" / "config.toml"
    return Path("/etc/krelcode/config.toml")


def default_config_layers(
    workspace: Path,
    *,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Tuple[ConfigLayer, ...]:
    """Return deterministic default layer paths, whether or not they exist."""

    workspace = Path(workspace).expanduser().resolve()
    home_path = Path(home).expanduser() if home is not None else Path.home()
    env = os.environ if environ is None else environ
    ordered = (
        ("user", home_path / ".krelcode" / "config.toml"),
        ("project", workspace / "krelcode.toml"),
        ("local", workspace / "krelcode.local.toml"),
        ("managed", _managed_config_path(env)),
    )
    return tuple(
        ConfigLayer(scope, path, path.is_file())
        for scope, path in ordered
    )


def _read_toml(path: Path) -> Dict[str, Any]:
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件必须是 TOML table: {path}")
    return raw


def merge_config(
    base: Optional[Mapping[str, Any]],
    overlay: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Deep merge two config mappings without mutating either input."""

    result: Dict[str, Any] = copy.deepcopy(dict(base or {}))
    for key, value in (overlay or {}).items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            result[key] = merge_config(current, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config_layers(
    workspace: Optional[Path] = None,
    *,
    explicit_path: Optional[Path] = None,
    home: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> ConfigBundle:
    """Read and merge config layers.

    ``explicit_path`` intentionally bypasses layering. This keeps automation and
    tests reproducible while normal CLI startup uses the full trust hierarchy.
    """

    if explicit_path is not None:
        path = Path(explicit_path).expanduser()
        if not path.is_file():
            return ConfigBundle({}, (ConfigLayer("explicit", path, False),))
        try:
            data = _read_toml(path)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            raise ConfigError(
                f"显式配置解析失败: {path}: "
                f"{type(exc).__name__}: {exc}") from exc
        return ConfigBundle(data, (ConfigLayer("explicit", path, True, data),))

    workspace_path = Path(workspace or Path.cwd()).expanduser().resolve()
    env = os.environ if environ is None else environ
    managed_explicit = bool(str(env.get("KRELCODE_MANAGED_CONFIG") or "").strip())
    loaded: list[ConfigLayer] = []
    merged: Dict[str, Any] = {}
    for layer in default_config_layers(
        workspace_path, home=home, environ=env
    ):
        if not layer.exists:
            if layer.scope == "managed" and managed_explicit:
                raise ConfigError(
                    f"managed 配置不存在: {layer.path}")
            loaded.append(layer)
            continue
        try:
            data = _read_toml(layer.path)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            message = f"{type(exc).__name__}: {exc}"
            if layer.scope == "managed":
                # Managed policy is authoritative: never silently bypass a
                # malformed organization-level configuration.
                raise ConfigError(
                    f"managed 配置解析失败: {layer.path}: {message}") from exc
            # Optional layers stay visible to doctor while lower-trust layers
            # continue to work. A broken user file cannot block project policy.
            loaded.append(ConfigLayer(
                layer.scope, layer.path, True, {}, error=message))
            continue
        loaded.append(ConfigLayer(layer.scope, layer.path, True, data))
        merged = merge_config(merged, data)
    return ConfigBundle(merged, tuple(loaded))


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Backward-compatible dictionary loader.

    Passing a path returns exactly that file. ``None`` now loads the complete
    user/project/local/managed hierarchy for the current working directory.
    """

    return load_config_layers(explicit_path=path).data
