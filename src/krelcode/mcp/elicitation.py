"""MCP elicitation：受限 Schema、应答校验和前端桥接契约。

Elicitation 允许 MCP 服务器请求用户补充结构化输入，但服务器本身是不可信
数据源。这里不复用完整 JSON Schema 实现，而是只接受规范所需的最小子集：
扁平 primitive 字段、有限枚举和边界明确的数值/字符串约束。任何嵌套结构、
引用、组合 Schema 或疑似凭据字段都在展示给用户前 fail-closed。
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import urlparse

from ..core.redact import SecretRedactor, redact_obvious_secrets
from .security import harden_untrusted_text, safe_label


MAX_ELICITATION_MESSAGE_CHARS = 2_000
MAX_ELICITATION_FIELDS = 12
MAX_ELICITATION_SCHEMA_BYTES = 16_384
MAX_ELICITATION_STRING_CHARS = 2_000
MAX_ELICITATION_ENUM_VALUES = 50
MAX_ELICITATION_ENUM_CHARS = 128
MAX_ELICITATION_TITLE_CHARS = 160
MAX_ELICITATION_DESCRIPTION_CHARS = 500

_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_ALLOWED_SCHEMA_KEYS = frozenset({
    "type", "title", "description", "enum", "enumNames", "format",
    "minimum", "maximum", "minLength", "maxLength",
})
_ALLOWED_OBJECT_SCHEMA_KEYS = frozenset({
    "type", "properties", "required", "additionalProperties", "$schema",
    "title", "description",
})
_ALLOWED_FORMATS = frozenset({"email", "uri", "date", "date-time"})
_PRIMITIVE_TYPES = frozenset({"string", "number", "integer", "boolean"})
_SENSITIVE_TOKENS = frozenset({
    "apikey", "accesstoken", "refreshtoken", "authtoken", "idtoken",
    "password", "passwd", "passphrase", "secret", "clientsecret",
    "privatekey", "authorization", "cookie", "credential", "credentials",
    "passcode", "cvv", "ssn", "creditcard", "cardnumber", "securitycode",
})
_SENSITIVE_PARTS = frozenset({
    "password", "passwd", "secret", "token", "authorization", "cookie",
    "credential", "credentials", "cvv", "ssn", "pin", "otp", "passcode",
})


class ElicitationValidationError(ValueError):
    """请求或用户应答不满足本地安全策略。"""


@dataclass(frozen=True)
class ElicitationField:
    name: str
    type: str
    title: str
    description: str = ""
    required: bool = False
    enum_values: Tuple[Any, ...] = ()
    enum_names: Tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    format: str = ""


@dataclass(frozen=True)
class ElicitationRequest:
    server: str
    message: str
    fields: Tuple[ElicitationField, ...]
    requested_schema: Mapping[str, Any] = field(repr=False)

    def prompt_text(self, *, redactor: Optional[SecretRedactor] = None) -> str:
        server = safe_label(self.server, max_chars=120, redactor=redactor)
        message = harden_untrusted_text(
            self.message,
            max_chars=MAX_ELICITATION_MESSAGE_CHARS,
            redactor=redactor,
            neutralize_instructions=True,
            truncation_marker="…[elicitation message truncated]",
        ).text.strip()
        lines = [
            f'MCP 服务器 "{server}" 请求输入（外部不可信数据）:',
            message,
            "",
            "不要在这里输入 API Key、密码、Token 或 Cookie。",
        ]
        if self.fields:
            lines.append("需要字段（请回复一个 JSON 对象）:")
            for item in self.fields:
                type_name = item.type
                if item.enum_values:
                    type_name += " enum"
                required = "必填" if item.required else "可选"
                lines.append(
                    f"- {item.name}: {type_name} ({required})"
                    + (f" · {item.title}" if item.title and item.title != item.name else ""))
                if item.description:
                    description = harden_untrusted_text(
                        item.description,
                        max_chars=MAX_ELICITATION_DESCRIPTION_CHARS,
                        redactor=redactor,
                        neutralize_instructions=True,
                        truncation_marker="…[field description truncated]",
                    ).text.strip()
                    if description:
                        lines.append(f"  {description}")
                if item.enum_values:
                    choices = ", ".join(
                        json.dumps(value, ensure_ascii=False)
                        for value in item.enum_values)
                    lines.append(f"  可选值: {choices}")
                limits: list[str] = []
                if item.min_length is not None:
                    limits.append(f"minLength={item.min_length}")
                if item.max_length is not None:
                    limits.append(f"maxLength={item.max_length}")
                if item.minimum is not None:
                    limits.append(f"minimum={item.minimum:g}")
                if item.maximum is not None:
                    limits.append(f"maximum={item.maximum:g}")
                if item.format:
                    limits.append(f"format={item.format}")
                if limits:
                    lines.append("  " + " · ".join(limits))
        else:
            lines.append("该请求没有字段；输入 accept 表示同意。")
        return "\n".join(lines)


@dataclass(frozen=True)
class ElicitationResult:
    action: str
    content: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def to_wire(self) -> Dict[str, Any]:
        if self.action == "accept":
            return {"action": "accept", "content": dict(self.content)}
        return {"action": self.action}


Elicitor = Callable[[ElicitationRequest], Any]


def parse_elicitation_request(
    params: Any,
    *,
    server: str,
) -> ElicitationRequest:
    """Validate ``elicitation/create`` params against the local subset."""
    if not isinstance(params, dict):
        raise ElicitationValidationError("params 必须是 object")
    unknown = sorted(set(params) - {"message", "requestedSchema", "_meta"})
    if unknown:
        raise ElicitationValidationError(
            f"不支持的 elicitation 参数: {', '.join(unknown)}")
    message = params.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ElicitationValidationError("message 必须是非空字符串")
    if len(message) > MAX_ELICITATION_MESSAGE_CHARS:
        raise ElicitationValidationError(
            f"message 超过 {MAX_ELICITATION_MESSAGE_CHARS} 字符上限")
    schema = params.get("requestedSchema")
    try:
        encoded = json.dumps(
            schema, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ElicitationValidationError("requestedSchema 无法序列化") from exc
    if len(encoded) > MAX_ELICITATION_SCHEMA_BYTES:
        raise ElicitationValidationError(
            f"requestedSchema 超过 {MAX_ELICITATION_SCHEMA_BYTES} 字节上限")
    if not isinstance(schema, dict):
        raise ElicitationValidationError("requestedSchema 必须是 object")
    if schema.get("type") != "object":
        raise ElicitationValidationError("requestedSchema.type 必须是 object")
    unknown_schema_keys = sorted(set(schema) - _ALLOWED_OBJECT_SCHEMA_KEYS)
    if unknown_schema_keys:
        raise ElicitationValidationError(
            "requestedSchema 使用了不支持的顶层关键字: "
            + ", ".join(unknown_schema_keys))
    if schema.get("additionalProperties") not in (None, False):
        raise ElicitationValidationError(
            "requestedSchema 不允许 additionalProperties=true")
    forbidden = {
        "$ref", "$defs", "definitions", "allOf", "anyOf", "oneOf", "not",
        "if", "then", "else", "patternProperties", "dependentSchemas",
    }
    present = sorted(forbidden.intersection(schema))
    if present:
        raise ElicitationValidationError(
            f"requestedSchema 不允许组合/引用字段: {', '.join(present)}")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ElicitationValidationError("requestedSchema.properties 必须是 object")
    if len(properties) > MAX_ELICITATION_FIELDS:
        raise ElicitationValidationError(
            f"elicitation 字段数不能超过 {MAX_ELICITATION_FIELDS}")
    required_raw = schema.get("required", [])
    if not isinstance(required_raw, list) or any(
            not isinstance(name, str) for name in required_raw):
        raise ElicitationValidationError("requestedSchema.required 必须是字符串数组")
    if len(required_raw) != len(set(required_raw)):
        raise ElicitationValidationError("requestedSchema.required 含重复字段")
    required = set(required_raw)
    unknown_required = sorted(required - set(properties))
    if unknown_required:
        raise ElicitationValidationError(
            f"required 引用了未知字段: {', '.join(unknown_required)}")

    fields: list[ElicitationField] = []
    for name, raw in properties.items():
        if not isinstance(name, str) or not _FIELD_NAME_RE.fullmatch(name):
            raise ElicitationValidationError(f"非法字段名: {name!r}")
        _reject_sensitive_field(name)
        if not isinstance(raw, dict):
            raise ElicitationValidationError(f"字段 {name!r} 的 Schema 必须是 object")
        field_type = raw.get("type")
        if field_type not in _PRIMITIVE_TYPES:
            raise ElicitationValidationError(
                f"字段 {name!r} 只允许 primitive type {sorted(_PRIMITIVE_TYPES)}")
        unknown_keys = sorted(set(raw) - _ALLOWED_SCHEMA_KEYS)
        if unknown_keys:
            raise ElicitationValidationError(
                f"字段 {name!r} 使用了不支持的 Schema 关键字: "
                f"{', '.join(unknown_keys)}")
        title = _bounded_text(
            raw.get("title", name), MAX_ELICITATION_TITLE_CHARS,
            f"字段 {name!r} title")
        description = _bounded_text(
            raw.get("description", ""), MAX_ELICITATION_DESCRIPTION_CHARS,
            f"字段 {name!r} description", allow_empty=True)
        _reject_sensitive_text(title)
        _reject_sensitive_text(description)

        enum_values, enum_names = _parse_enum(name, raw, field_type)
        minimum, maximum = _parse_number_bounds(name, raw, field_type)
        min_length, max_length = _parse_length_bounds(name, raw, field_type)
        format_name = str(raw.get("format") or "")
        if format_name:
            if field_type != "string" or format_name not in _ALLOWED_FORMATS:
                raise ElicitationValidationError(
                    f"字段 {name!r} 的 format 不受支持: {format_name!r}")
        fields.append(ElicitationField(
            name=name,
            type=field_type,
            title=title,
            description=description,
            required=name in required,
            enum_values=enum_values,
            enum_names=enum_names,
            minimum=minimum,
            maximum=maximum,
            min_length=min_length,
            max_length=max_length,
            format=format_name,
        ))
    return ElicitationRequest(
        server=str(server),
        message=message,
        fields=tuple(fields),
        requested_schema=dict(schema),
    )


def normalize_elicitation_result(
    value: Any,
    *,
    request: Optional[ElicitationRequest] = None,
) -> ElicitationResult:
    """Validate an elicitor callback result before sending it to the server."""
    if isinstance(value, ElicitationResult):
        action = value.action
        content = dict(value.content)
    elif isinstance(value, dict):
        action = str(value.get("action") or "")
        raw_content = value.get("content") or {}
        if not isinstance(raw_content, dict):
            raise ElicitationValidationError("elicitation content 必须是 object")
        content = dict(raw_content)
    else:
        raise ElicitationValidationError("elicitor 必须返回 ElicitationResult/dict")
    if action not in {"accept", "decline", "cancel"}:
        raise ElicitationValidationError(f"非法 elicitation action: {action!r}")
    if action != "accept":
        return ElicitationResult(action=action)
    if request is None:
        return ElicitationResult(action="accept", content=content)
    try:
        encoded = json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ElicitationValidationError(
            "elicitation content 无法序列化") from exc
    return parse_elicitation_answer(request, encoded)


def parse_elicitation_answer(
    request: ElicitationRequest,
    raw: str,
) -> ElicitationResult:
    """Parse a foreground answer without ever including values in errors."""
    text = str(raw or "").strip()
    action = text.casefold()
    if action == "decline":
        return ElicitationResult(action="decline")
    if action == "cancel":
        return ElicitationResult(action="cancel")
    if action == "accept" and not request.fields:
        return ElicitationResult(action="accept", content={})
    try:
        content = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ElicitationValidationError(
            "请输入合法 JSON object，或输入 decline/cancel") from exc
    if not isinstance(content, dict):
        raise ElicitationValidationError("应答必须是 JSON object")
    allowed = {item.name for item in request.fields}
    unknown = sorted(set(content) - allowed)
    if unknown:
        raise ElicitationValidationError(
            f"存在未知字段: {', '.join(unknown)}")
    fields = {item.name: item for item in request.fields}
    normalized: Dict[str, Any] = {}
    for name, item in fields.items():
        if name not in content:
            if item.required:
                raise ElicitationValidationError(f"缺少必填字段: {name}")
            continue
        normalized[name] = _validate_field_value(item, content[name])
    return ElicitationResult(action="accept", content=normalized)


def validate_answer_text(request: ElicitationRequest, raw: str) -> Tuple[bool, str]:
    """UserInputBroker validator adapter: invalid input keeps the prompt open."""
    try:
        parse_elicitation_answer(request, raw)
    except ElicitationValidationError as exc:
        return False, str(exc)
    return True, ""


def _parse_enum(
    name: str,
    raw: Mapping[str, Any],
    field_type: str,
) -> tuple[Tuple[Any, ...], Tuple[str, ...]]:
    raw_enum = raw.get("enum")
    raw_names = raw.get("enumNames")
    if raw_enum is None:
        if raw_names is not None:
            raise ElicitationValidationError(f"字段 {name!r} 的 enumNames 缺少 enum")
        return (), ()
    if not isinstance(raw_enum, list) or not raw_enum:
        raise ElicitationValidationError(f"字段 {name!r} 的 enum 必须是非空数组")
    if len(raw_enum) > MAX_ELICITATION_ENUM_VALUES:
        raise ElicitationValidationError(
            f"字段 {name!r} 的 enum 不能超过 {MAX_ELICITATION_ENUM_VALUES} 项")
    values: list[Any] = []
    for value in raw_enum:
        normalized = _validate_scalar_type(field_type, value, name)
        if field_type == "string":
            if len(normalized) > MAX_ELICITATION_ENUM_CHARS:
                raise ElicitationValidationError(
                    f"字段 {name!r} 的 enum 字符串过长")
            if any(
                unicodedata.category(char).startswith("C")
                for char in normalized
            ):
                raise ElicitationValidationError(
                    f"字段 {name!r} 的 enum 含控制/格式字符")
            if redact_obvious_secrets(normalized) != normalized:
                raise ElicitationValidationError(
                    f"字段 {name!r} 的 enum 疑似包含敏感凭据")
        if any(existing == normalized for existing in values):
            raise ElicitationValidationError(f"字段 {name!r} 的 enum 含重复值")
        values.append(normalized)
    names: list[str] = []
    if raw_names is not None:
        if not isinstance(raw_names, list) or len(raw_names) != len(values):
            raise ElicitationValidationError(
                f"字段 {name!r} 的 enumNames 必须与 enum 等长")
        names = [
            _bounded_text(value, MAX_ELICITATION_ENUM_CHARS,
                          f"字段 {name!r} enumNames")
            for value in raw_names
        ]
    return tuple(values), tuple(names)


def _parse_number_bounds(
    name: str,
    raw: Mapping[str, Any],
    field_type: str,
) -> tuple[Optional[float], Optional[float]]:
    if "minimum" not in raw and "maximum" not in raw:
        return None, None
    if field_type not in {"number", "integer"}:
        raise ElicitationValidationError(f"字段 {name!r} 的数值边界不适用")
    minimum = raw.get("minimum")
    maximum = raw.get("maximum")
    if minimum is not None:
        minimum = _finite_number(minimum, f"字段 {name!r} minimum")
    if maximum is not None:
        maximum = _finite_number(maximum, f"字段 {name!r} maximum")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ElicitationValidationError(f"字段 {name!r} 的数值边界反向")
    return minimum, maximum


def _parse_length_bounds(
    name: str,
    raw: Mapping[str, Any],
    field_type: str,
) -> tuple[Optional[int], Optional[int]]:
    if "minLength" not in raw and "maxLength" not in raw:
        return None, None
    if field_type != "string":
        raise ElicitationValidationError(f"字段 {name!r} 的长度边界不适用")
    minimum = raw.get("minLength")
    maximum = raw.get("maxLength")
    if minimum is not None and (
            isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0):
        raise ElicitationValidationError(f"字段 {name!r} minLength 非法")
    if maximum is not None and (
            isinstance(maximum, bool) or not isinstance(maximum, int)
            or maximum < 1 or maximum > MAX_ELICITATION_STRING_CHARS):
        raise ElicitationValidationError(
            f"字段 {name!r} maxLength 必须在 1-{MAX_ELICITATION_STRING_CHARS}")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ElicitationValidationError(f"字段 {name!r} 的长度边界反向")
    return minimum, maximum


def _validate_field_value(item: ElicitationField, value: Any) -> Any:
    normalized = _validate_scalar_type(item.type, value, item.name)
    if item.enum_values and normalized not in item.enum_values:
        raise ElicitationValidationError(f"字段 {item.name} 不在允许枚举中")
    if item.type == "string":
        length = len(normalized)
        if item.min_length is not None and length < item.min_length:
            raise ElicitationValidationError(f"字段 {item.name} 字符串过短")
        effective_max = (
            item.max_length if item.max_length is not None
            else MAX_ELICITATION_STRING_CHARS)
        if length > effective_max:
            raise ElicitationValidationError(f"字段 {item.name} 字符串过长")
        _validate_format(item.name, item.format, normalized)
    elif item.type in {"number", "integer"}:
        if item.minimum is not None and normalized < item.minimum:
            raise ElicitationValidationError(f"字段 {item.name} 小于 minimum")
        if item.maximum is not None and normalized > item.maximum:
            raise ElicitationValidationError(f"字段 {item.name} 大于 maximum")
    return normalized


def _validate_scalar_type(field_type: str, value: Any, name: str) -> Any:
    if field_type == "string":
        if not isinstance(value, str):
            raise ElicitationValidationError(f"字段 {name} 必须是字符串")
        return value
    if field_type == "boolean":
        if not isinstance(value, bool):
            raise ElicitationValidationError(f"字段 {name} 必须是布尔值")
        return value
    if field_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ElicitationValidationError(f"字段 {name} 必须是整数")
        return value
    if field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ElicitationValidationError(f"字段 {name} 必须是数字")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ElicitationValidationError(f"字段 {name} 必须是有限数字")
        return normalized
    raise ElicitationValidationError(f"字段 {name} 类型不受支持")


def _validate_format(name: str, format_name: str, value: str) -> None:
    if not format_name:
        return
    valid = True
    if format_name == "email":
        valid = bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))
    elif format_name == "uri":
        parsed = urlparse(value)
        valid = bool(parsed.scheme and (parsed.netloc or parsed.path))
    elif format_name == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            valid = False
    elif format_name == "date-time":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            valid = False
    if not valid:
        raise ElicitationValidationError(f"字段 {name} 不符合 {format_name} 格式")


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ElicitationValidationError(f"{label} 必须是数字")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ElicitationValidationError(f"{label} 必须是有限数字")
    return normalized


def _bounded_text(
    value: Any,
    limit: int,
    label: str,
    *,
    allow_empty: bool = False,
) -> str:
    text = str(value or "")
    if not allow_empty and not text:
        raise ElicitationValidationError(f"{label} 不能为空")
    if len(text) > limit:
        raise ElicitationValidationError(f"{label} 超过 {limit} 字符上限")
    return text


def _reject_sensitive_field(name: str) -> None:
    lowered = name.casefold()
    parts = {part for part in re.split(r"[^a-z0-9]+", lowered) if part}
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    if compact in _SENSITIVE_TOKENS or parts.intersection(_SENSITIVE_PARTS):
        raise ElicitationValidationError(
            f"字段 {name!r} 疑似要求敏感凭据；MCP elicitation 禁止收集秘密")


def _reject_sensitive_text(text: str) -> None:
    lowered = text.casefold()
    if any(token in lowered for token in (
        "api key", "api_key", "apikey", "password", "token", "secret",
        "cookie", "credential", "私钥", "密码", "令牌", "密钥", "凭据",
    )):
        raise ElicitationValidationError(
            "elicitation 字段标题/描述疑似要求敏感凭据")


__all__ = [
    "ElicitationField",
    "ElicitationRequest",
    "ElicitationResult",
    "ElicitationValidationError",
    "Elicitor",
    "MAX_ELICITATION_FIELDS",
    "MAX_ELICITATION_MESSAGE_CHARS",
    "MAX_ELICITATION_SCHEMA_BYTES",
    "MAX_ELICITATION_STRING_CHARS",
    "normalize_elicitation_result",
    "parse_elicitation_answer",
    "parse_elicitation_request",
    "validate_answer_text",
]
