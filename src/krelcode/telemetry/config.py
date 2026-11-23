"""Telemetry configuration and explicit user-consent persistence.

Telemetry is disabled unless the user explicitly enables it through
``krelcode telemetry enable`` (or an explicit CI environment override).
Project configuration can tune retention and batching, but cannot silently
turn collection on for every contributor who opens a repository.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional


TELEMETRY_MODES = frozenset({"off", "local", "remote"})
DEFAULT_SAMPLE_RATE = 1.0


class TelemetryConfigError(ValueError):
    """Telemetry configuration or consent state is invalid."""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _mode(value: Any) -> str:
    normalized = str(value or "off").strip().lower()
    aliases = {
        "0": "off", "false": "off", "no": "off", "disabled": "off",
        "1": "local", "true": "local", "yes": "local", "enabled": "local",
        "on": "local",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in TELEMETRY_MODES:
        raise TelemetryConfigError(
            "telemetry mode 必须是 off/local/remote")
    return normalized


def _sample_rate(value: Any, default: float = DEFAULT_SAMPLE_RATE) -> float:
    if value is None:
        return float(default)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise TelemetryConfigError("telemetry sample_rate 必须是 0..1") from exc
    if not 0.0 <= parsed <= 1.0:
        raise TelemetryConfigError("telemetry sample_rate 必须是 0..1")
    return parsed


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    if value is None:
        return max(minimum, int(default))
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TelemetryConfigError("telemetry 整数配置无效") from exc
    return max(minimum, parsed)


def _positive_float(value: Any, default: float, *, minimum: float = 1.0) -> float:
    if value is None:
        return max(minimum, float(default))
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise TelemetryConfigError("telemetry 浮点配置无效") from exc
    return max(minimum, parsed)


def validate_endpoint(value: str) -> str:
    endpoint = str(value or "").strip()
    if not endpoint:
        raise TelemetryConfigError("remote telemetry 需要 endpoint")
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise TelemetryConfigError("telemetry endpoint 必须是合法 URL")
    if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1", "localhost", "::1"}:
        raise TelemetryConfigError(
            "telemetry 仅允许 HTTPS；localhost HTTP 仅用于测试")
    if parsed.username or parsed.password:
        raise TelemetryConfigError("telemetry endpoint 不能包含凭据")
    return endpoint


@dataclass(frozen=True)
class TelemetryPreferences:
    mode: str = "off"
    endpoint: str = ""
    sample_rate: float = DEFAULT_SAMPLE_RATE
    updated_at: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "mode": self.mode,
            "endpoint": self.endpoint,
            "sample_rate": self.sample_rate,
            "updated_at": self.updated_at,
        }


class TelemetryPreferenceStore:
    """Private per-user consent file; rejects symlinks and unsafe permissions."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> Optional[TelemetryPreferences]:
        if self.path.is_symlink():
            raise TelemetryConfigError("拒绝读取 symlink telemetry consent")
        if not self.path.is_file():
            return None
        try:
            os.chmod(self.path, 0o600)
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TelemetryConfigError(
                f"telemetry consent 读取失败: {type(exc).__name__}") from exc
        if not isinstance(raw, Mapping):
            raise TelemetryConfigError("telemetry consent 必须是 JSON object")
        return TelemetryPreferences(
            mode=_mode(raw.get("mode")),
            endpoint=str(raw.get("endpoint", "") or "").strip(),
            sample_rate=_sample_rate(
                raw.get("sample_rate"), DEFAULT_SAMPLE_RATE),
            updated_at=_positive_int(
                raw.get("updated_at", 0), 0, minimum=0),
        )

    def save(
        self,
        *,
        mode: str,
        endpoint: str = "",
        sample_rate: float = DEFAULT_SAMPLE_RATE,
    ) -> TelemetryPreferences:
        normalized_mode = _mode(mode)
        normalized_endpoint = (
            validate_endpoint(endpoint) if normalized_mode == "remote" else "")
        preferences = TelemetryPreferences(
            mode=normalized_mode,
            endpoint=normalized_endpoint,
            sample_rate=_sample_rate(sample_rate),
            updated_at=int(time.time()),
        )
        if self.path.is_symlink():
            raise TelemetryConfigError("拒绝写入 symlink telemetry consent")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        temporary = self.path.with_name(self.path.name + ".tmp")
        if temporary.is_symlink():
            raise TelemetryConfigError("拒绝写入 symlink telemetry consent 临时文件")
        try:
            temporary.write_text(
                json.dumps(preferences.as_dict(), ensure_ascii=False, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise TelemetryConfigError("telemetry consent 写入失败") from exc
        return preferences

    def clear(self) -> bool:
        try:
            self.path.unlink(missing_ok=True)
            return True
        except OSError as exc:
            raise TelemetryConfigError("telemetry consent 删除失败") from exc


@dataclass(frozen=True)
class TelemetryConfig:
    mode: str = "off"
    endpoint: str = ""
    sample_rate: float = DEFAULT_SAMPLE_RATE
    queue_path: Path = Path.home() / ".krelcode" / "telemetry" / "queue.jsonl"
    install_id_path: Path = Path.home() / ".krelcode" / "telemetry" / "install_id"
    preferences_path: Path = Path.home() / ".krelcode" / "telemetry" / "consent.json"
    flush_interval: float = 60.0
    request_timeout: float = 10.0
    batch_size: int = 50
    max_events: int = 10_000
    max_bytes: int = 10_000_000
    retention_days: int = 30

    @property
    def enabled(self) -> bool:
        return self.mode in {"local", "remote"}

    @property
    def remote(self) -> bool:
        return self.mode == "remote"

    def validate(self) -> None:
        _mode(self.mode)
        _sample_rate(self.sample_rate)
        if self.mode == "remote":
            validate_endpoint(self.endpoint)

    @classmethod
    def from_mapping(
        cls,
        value: Optional[Mapping[str, Any]],
        *,
        home: Optional[Path] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "TelemetryConfig":
        raw = _mapping(value)
        home_path = Path(home or Path.home()).expanduser()
        root = home_path / ".krelcode" / "telemetry"
        preferences_store = TelemetryPreferenceStore(root / "consent.json")
        try:
            preferences = preferences_store.load()
        except TelemetryConfigError:
            preferences = None

        mode = preferences.mode if preferences is not None else "off"
        endpoint = (preferences.endpoint if preferences is not None else "") \
            or str(raw.get("endpoint", "") or "").strip()
        sample = (preferences.sample_rate if preferences is not None
                  else raw.get("sample_rate", DEFAULT_SAMPLE_RATE))
        env = os.environ if environ is None else environ
        override = str(env.get("KRELCODE_TELEMETRY", "") or "").strip()
        if override:
            if override.lower() in {"0", "false", "off", "no", "disabled"}:
                mode = "off"
            else:
                mode = _mode(override)

        config = cls(
            mode=mode,
            endpoint=endpoint,
            sample_rate=_sample_rate(sample),
            queue_path=root / "queue.jsonl",
            install_id_path=root / "install_id",
            preferences_path=root / "consent.json",
            flush_interval=_positive_float(
                raw.get("flush_interval", 60.0), 60.0),
            request_timeout=_positive_float(
                raw.get("request_timeout", 10.0), 10.0),
            batch_size=_positive_int(raw.get("batch_size", 50), 50),
            max_events=_positive_int(raw.get("max_events", 10_000), 10_000),
            max_bytes=_positive_int(raw.get("max_bytes", 10_000_000), 10_000_000),
            retention_days=_positive_int(raw.get("retention_days", 30), 30),
        )
        return config


__all__ = [
    "DEFAULT_SAMPLE_RATE",
    "TELEMETRY_MODES",
    "TelemetryConfig",
    "TelemetryConfigError",
    "TelemetryPreferenceStore",
    "TelemetryPreferences",
    "validate_endpoint",
]
