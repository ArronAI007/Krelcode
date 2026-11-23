"""Content-free telemetry schema and privacy-safe normalizers."""

from __future__ import annotations

import platform
import re
from typing import Any, Mapping


TELEMETRY_SCHEMA_VERSION = 1
TELEMETRY_EVENT_TYPES = frozenset({
    "run_started",
    "run_finished",
    "run_cancelled",
    "tool_call",
    "llm_response",
    "context_compacted",
    "approval_requested",
    "permission_denied",
    "error",
    "telemetry_started",
})

_TOP_LEVEL_FIELDS = frozenset({
    "schema_version", "event_id", "ts", "install_id", "session_id", "event",
    "krelcode_version", "os_family", "arch", "python_minor", "provider",
    "model", "run_mode", "data",
})
_DATA_FIELDS = {
    "run_started": frozenset(),
    "run_finished": frozenset({
        "outcome", "duration_bucket", "tool_calls", "llm_calls",
        "input_tokens_bucket", "output_tokens_bucket", "total_tokens_bucket",
        "answer_chars_bucket",
    }),
    "run_cancelled": frozenset(),
    "tool_call": frozenset({
        "tool_category", "success", "duration_bucket"}),
    "llm_response": frozenset({
        "input_tokens_bucket", "output_tokens_bucket", "total_tokens_bucket",
        "cache_read_tokens_bucket", "cache_write_tokens_bucket",
    }),
    "context_compacted": frozenset({
        "before_tokens_bucket", "after_tokens_bucket", "strategy"}),
    "approval_requested": frozenset({"tool_category"}),
    "permission_denied": frozenset({"category"}),
    "error": frozenset({"error_category"}),
    "telemetry_started": frozenset({"mode"}),
}
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:-]{0,80}$")


class TelemetryPrivacyError(ValueError):
    """An event does not satisfy the content-free telemetry contract."""


def token_bucket(value: Any) -> str:
    """Coarsen token counts so events reveal scale, not exact prompts."""
    try:
        number = max(0, int(value or 0))
    except (TypeError, ValueError):
        number = 0
    if number == 0:
        return "0"
    for upper in (100, 500, 1_000, 5_000, 10_000, 50_000, 100_000):
        if number <= upper:
            return f"1-{upper}"
    return ">100000"


def duration_bucket(value_ms: Any) -> str:
    try:
        number = max(0, int(value_ms or 0))
    except (TypeError, ValueError):
        number = 0
    for upper in (100, 500, 1_000, 5_000, 15_000, 60_000, 300_000):
        if number <= upper:
            return f"0-{upper}ms"
    return ">300000ms"


def char_bucket(value: Any) -> str:
    return token_bucket(value)


def tool_category(name: str) -> str:
    normalized = str(name or "").strip()
    if normalized in {
        "read_file", "glob", "grep", "find_refs", "repo_map",
        "check_syntax", "get_time", "list_dir",
    }:
        return "filesystem_read"
    if normalized in {"write_file", "edit_file"}:
        return "filesystem_write"
    if normalized in {"run_command", "shell", "bash"}:
        return "shell"
    if normalized == "update_plan":
        return "plan"
    if normalized in {"ask_user", "user_input"}:
        return "user_input"
    if normalized in {"web_search", "web_fetch"}:
        return "web"
    if normalized == "tool_search":
        return "tool_search"
    if "__" in normalized:
        return "mcp"
    if normalized.startswith("skill"):
        return "skill"
    if normalized.startswith("agent") or normalized.startswith("subagent"):
        return "subagent"
    return "other"


def safe_model_name(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if _SAFE_VALUE.fullmatch(text):
        return text
    return "custom"


def safe_provider_name(value: str) -> str:
    text = str(value or "").strip().lower()
    return text if _SAFE_VALUE.fullmatch(text) else "custom"


def error_category(value: str) -> str:
    text = str(value or "")
    lowered = text.lower()
    known = (
        ("timeout", "timeout"),
        ("cancelled", "cancelled"),
        ("permission", "permission"),
        ("sandbox", "sandbox"),
        ("config", "configuration"),
        ("http", "provider_http"),
        ("rate limit", "provider_rate_limit"),
        ("authentication", "provider_auth"),
        ("tool", "tool"),
    )
    for needle, category in known:
        if needle in lowered:
            return category
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]{0,60})(?::|$)", text)
    if match:
        name = match.group(1).lower()
        return name if _SAFE_VALUE.fullmatch(name) else "unknown"
    return "unknown"


def runtime_environment() -> tuple[str, str, str]:
    return (
        platform.system().lower() or "unknown",
        platform.machine().lower() or "unknown",
        f"{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}",
    )


def _validate_scalar(value: Any, *, field: str) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return max(-1_000_000_000, min(1_000_000_000, value))
    if isinstance(value, float):
        return round(max(-1_000_000.0, min(1_000_000.0, value)), 3)
    if isinstance(value, str):
        if not _SAFE_VALUE.fullmatch(value):
            raise TelemetryPrivacyError(
                f"telemetry data.{field} 只能使用受控短标识")
        return value
    raise TelemetryPrivacyError(
        f"telemetry data.{field} 必须是 string/int/bool")


def validate_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one event without accepting free-form content."""
    if not isinstance(event, Mapping):
        raise TelemetryPrivacyError("telemetry event 必须是 object")
    unknown = set(event) - _TOP_LEVEL_FIELDS
    if unknown:
        raise TelemetryPrivacyError(
            "telemetry event 包含未允许字段: " + ", ".join(sorted(unknown)))
    missing = _TOP_LEVEL_FIELDS - set(event)
    if missing:
        raise TelemetryPrivacyError(
            "telemetry event 缺少字段: " + ", ".join(sorted(missing)))
    if event.get("schema_version") != TELEMETRY_SCHEMA_VERSION:
        raise TelemetryPrivacyError("telemetry schema_version 不兼容")
    event_type = str(event.get("event", "") or "")
    if event_type not in TELEMETRY_EVENT_TYPES:
        raise TelemetryPrivacyError("未知 telemetry event type")
    data_raw = event.get("data")
    if not isinstance(data_raw, Mapping):
        raise TelemetryPrivacyError("telemetry data 必须是 object")
    allowed_data = _DATA_FIELDS[event_type]
    extra_data = set(data_raw) - allowed_data
    if extra_data:
        raise TelemetryPrivacyError(
            "telemetry data 包含未允许字段: "
            + ", ".join(sorted(extra_data)))
    data = {
        str(key): _validate_scalar(value, field=str(key))
        for key, value in data_raw.items()
    }
    normalized: dict[str, Any] = {}
    for key, value in event.items():
        if key == "data":
            normalized[key] = data
        elif key in {"ts", "schema_version"}:
            normalized[key] = int(value)
        else:
            text = str(value)
            if key in {"event_id", "install_id", "session_id"}:
                if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", text):
                    raise TelemetryPrivacyError(
                        f"telemetry {key} 格式无效")
                normalized[key] = text
            elif not _SAFE_VALUE.fullmatch(text):
                raise TelemetryPrivacyError(
                    f"telemetry {key} 只能使用受控短标识")
            else:
                normalized[key] = text
    return normalized


__all__ = [
    "TELEMETRY_EVENT_TYPES",
    "TELEMETRY_SCHEMA_VERSION",
    "TelemetryPrivacyError",
    "char_bucket",
    "duration_bucket",
    "error_category",
    "runtime_environment",
    "safe_model_name",
    "safe_provider_name",
    "token_bucket",
    "tool_category",
    "validate_event",
]
