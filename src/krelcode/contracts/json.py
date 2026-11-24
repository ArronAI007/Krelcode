"""Validated JSON aliases for tool and MCP protocol boundaries."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import TypeAlias, cast


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
ToolExecutionData: TypeAlias = JsonObject
ToolResultMetadata: TypeAlias = JsonObject
MCPToolPayload: TypeAlias = JsonObject


class JsonValueError(ValueError):
    """A value cannot be represented losslessly in a JSON wire payload."""


def ensure_json_value(value: object, *, path: str = "$") -> JsonValue:
    """Validate and normalize an external value to a finite JSON value."""
    if value is None or isinstance(value, (str, bool, int)):
        return cast(JsonScalar, value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JsonValueError(f"{path} 必须是有限数值")
        return value
    if isinstance(value, Mapping):
        output: JsonObject = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise JsonValueError(f"{path} 的对象键必须是字符串")
            output[raw_key] = ensure_json_value(
                item, path=f"{path}.{raw_key}")
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            ensure_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise JsonValueError(
        f"{path} 包含不可序列化类型: {type(value).__name__}")


def ensure_json_object(value: object, *, path: str = "$") -> JsonObject:
    """Validate a mapping as a JSON object with string keys."""
    normalized = ensure_json_value(value, path=path)
    if not isinstance(normalized, dict):
        raise JsonValueError(f"{path} 必须是 JSON 对象")
    return normalized


__all__ = [
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "JsonValueError",
    "MCPToolPayload",
    "ToolExecutionData",
    "ToolResultMetadata",
    "ensure_json_object",
    "ensure_json_value",
]
