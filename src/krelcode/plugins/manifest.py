"""插件清单（plugin.toml）的纯数据解析与校验。"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENTRYPOINT_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r":[A-Za-z_][A-Za-z0-9_]*$"
)


class PluginManifestError(ValueError):
    """插件清单无效；只影响对应插件。"""


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    entrypoint: str
    description: str = ""
    enabled: bool = True
    path: Path = Path("plugin.toml")

    @property
    def plugin_dir(self) -> Path:
        return self.path.parent


def parse_manifest(path: Path) -> PluginManifest:
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            raw: Any = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PluginManifestError(f"无法读取清单: {exc}") from exc
    if not isinstance(raw, dict):
        raise PluginManifestError("插件清单必须是 TOML table")
    name = str(raw.get("name") or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise PluginManifestError(
            "name 必须为 1-64 位字母、数字、点、下划线或连字符")
    version = str(raw.get("version") or "0.0.0").strip()
    entrypoint = str(raw.get("entrypoint") or "").strip()
    if not _ENTRYPOINT_RE.fullmatch(entrypoint):
        raise PluginManifestError("entrypoint 必须形如 module:attribute")
    description = " ".join(str(raw.get("description") or "").split())
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PluginManifestError("enabled 必须是布尔值")
    return PluginManifest(
        name=name,
        version=version or "0.0.0",
        entrypoint=entrypoint,
        description=description,
        enabled=enabled,
        path=path,
    )


def discover_manifest_dirs(root: Path) -> list[Optional[Path]]:
    """返回根目录下的清单路径；根目录自身是插件时也支持。"""
    root = Path(root)
    if (root / "plugin.toml").is_file():
        return [root / "plugin.toml"]
    if not root.is_dir():
        return []
    return [
        child / "plugin.toml"
        for child in sorted(root.iterdir(), key=lambda path: path.name)
        if child.is_dir() and (child / "plugin.toml").is_file()
    ]
