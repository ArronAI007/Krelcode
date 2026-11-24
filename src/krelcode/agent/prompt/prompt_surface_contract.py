"""Executable safety contract for auxiliary model-visible Prompt surfaces.

Step A added a content-light catalogue.  Step B gives non-main surfaces a
provider-neutral execution envelope for Review, Compact, Prompt Hooks and MCP
Prompts.  The contract is deliberately independent from ``PromptAssembler``:
it does not assemble or rewrite prompts, but every auxiliary invocation can be
redacted, schema-checked, time-bounded and output-bounded in one place.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping, Optional, cast

from krelcode.core.redact import MASK, SecretRedactor, redact_obvious_secrets
from krelcode.agent.compact_summary import COMPACT_SUMMARY_JSON_SCHEMA
from krelcode.agent.tokens import estimate_text_tokens
from krelcode.agent.value_utils import value_or


PROMPT_SURFACE_CONTRACT_VERSION = "prompt-surface-contract-v1"
OUTPUT_MODE_TEXT = "text"
OUTPUT_MODE_JSON = "json"
OUTPUT_MODE_JSON_OR_TEXT = "json_or_text"
LIMIT_STRATEGY_TRUNCATE = "truncate"
LIMIT_STRATEGY_ERROR = "error"
_RESULT_STATUSES = frozenset({
    "ok", "schema_error", "timeout", "input_limit", "output_limit", "error",
})
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key", "_access_token", "_auth_token", "_refresh_token",
    "_client_secret", "_secret", "_password", "_passwd", "_credential",
    "_credentials", "_private_key", "_access_key", "_secret_access_key",
)
_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "access_token", "auth_token", "refresh_token",
    "client_secret", "secret", "password", "passwd", "credential",
    "credentials", "private_key", "access_key", "secret_access_key",
    "cookie", "set_cookie", "authorization", "proxy_authorization",
})

class PromptSurfaceContractError(RuntimeError):
    """Base error for a rejected auxiliary Prompt Surface execution."""


class PromptSurfaceSchemaError(PromptSurfaceContractError):
    """The structured output does not match the declared JSON Schema."""


class PromptSurfaceTimeoutError(PromptSurfaceContractError):
    """The surface exceeded its execution deadline."""


class PromptSurfaceLimitError(PromptSurfaceContractError):
    """The input or output exceeded a hard contract limit."""


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _schema_hash(schema: Mapping[str, object]) -> str:
    encoded = json.dumps(
        _jsonable(schema), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _schema_label(schema: Mapping[str, object]) -> str:
    return str(schema.get("$id") or schema.get("title") or "").strip()


def _value_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)


def _parse_json_output(text: str) -> object:
    value = str(text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        start = value.find("{")
        end = value.rfind("}")
        if start >= 0 and end >= start:
            return json.loads(value[start:end + 1])
        raise


def _is_sensitive_key(key: object) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    if not normalized or normalized.endswith("_env"):
        return False
    leaf = normalized.rsplit(".", 1)[-1]
    return leaf in _SENSITIVE_KEYS or leaf.endswith(_SENSITIVE_KEY_SUFFIXES)


def _mask_value(value: Any) -> Any:
    if value is None or value == "":
        return value
    if isinstance(value, Mapping):
        return {str(key): _mask_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_value(item) for item in value]
    return MASK


def _redact_value(value: Any, redact: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(item, redact) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, redact) for item in value)
    if isinstance(value, Mapping):
        output = {}
        for key, item in value.items():
            safe_key = redact(str(key))
            output[safe_key] = (
                _mask_value(item) if _is_sensitive_key(key)
                else _redact_value(item, redact)
            )
        return output
    return value


def _type_matches(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_schema(
    value: object,
    schema: Mapping[str, object],
    *,
    path: str = "$",
) -> tuple[str, ...]:
    """Validate the deterministic JSON Schema subset used by Surface contracts."""
    errors: list[str] = []
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        matches = 0
        branch_errors: list[tuple[str, ...]] = []
        for branch in one_of:
            if not isinstance(branch, Mapping):
                continue
            issues = _validate_schema(value, branch, path=path)
            branch_errors.append(issues)
            if not issues:
                matches += 1
        if matches != 1:
            detail = "; ".join(item[0] for item in branch_errors if item)
            errors.append(f"{path} oneOf 匹配 {matches} 个分支" + (f": {detail}" if detail else ""))
            return tuple(errors)
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        if not any(
            isinstance(branch, Mapping)
            and not _validate_schema(value, branch, path=path)
            for branch in any_of
        ):
            errors.append(f"{path} 不匹配 anyOf")
            return tuple(errors)
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            if isinstance(branch, Mapping):
                errors.extend(_validate_schema(value, branch, path=path))

    expected = schema.get("type")
    if isinstance(expected, str):
        if not _type_matches(value, expected):
            errors.append(f"{path} 必须是 {expected}")
            return tuple(errors)
    elif isinstance(expected, list):
        if not any(
            isinstance(item, str) and _type_matches(value, item)
            for item in expected
        ):
            errors.append(f"{path} 类型不匹配 {expected}")

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{path} 必须属于 {enum}")

    if isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"{path} 长度小于 {min_length}")
        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(f"{path} 长度超过 {max_length}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path} 小于 {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path} 超过 {maximum}")

    if isinstance(value, Mapping):
        required = schema.get("required")
        if isinstance(required, list):
            missing = [
                str(key) for key in required
                if isinstance(key, str) and key not in value
            ]
            if missing:
                errors.append(f"{path} 缺少必填字段: {', '.join(missing)}")
        properties = schema.get("properties")
        property_schemas = properties if isinstance(properties, Mapping) else {}
        for key, item in value.items():
            child = property_schemas.get(str(key))
            if isinstance(child, Mapping):
                errors.extend(_validate_schema(
                    item, child, path=f"{path}.{key}"))
                continue
            additional = schema.get("additionalProperties", True)
            if additional is False:
                errors.append(f"{path} 包含未声明字段: {key}")
            elif isinstance(additional, Mapping):
                errors.extend(_validate_schema(
                    item, additional, path=f"{path}.{key}"))

    if isinstance(value, (list, tuple)):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{path} 项目数小于 {min_items}")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"{path} 项目数超过 {max_items}")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                errors.extend(_validate_schema(
                    item, item_schema, path=f"{path}[{index}]"))
    return tuple(errors)


def validate_prompt_surface_schema(
    value: object,
    schema: Mapping[str, object],
    *,
    label: str = "output",
) -> None:
    issues = _validate_schema(value, schema)
    if issues:
        raise PromptSurfaceSchemaError(
            f"{label} Schema 校验失败: " + "; ".join(issues))


@dataclass(frozen=True)
class PromptSurfaceContract:
    """Static safety and wire-shape policy for one auxiliary Prompt surface."""

    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    timeout_seconds: float
    max_input_chars: int
    max_output_chars: int
    output_mode: str = OUTPUT_MODE_TEXT
    limit_strategy: str = LIMIT_STRATEGY_TRUNCATE
    redact_input: bool = True
    redact_output: bool = True
    version: str = PROMPT_SURFACE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.input_schema, Mapping):
            raise ValueError("Surface input_schema 必须是 object")
        if not isinstance(self.output_schema, Mapping):
            raise ValueError("Surface output_schema 必须是 object")
        if self.output_mode not in {
            OUTPUT_MODE_TEXT, OUTPUT_MODE_JSON, OUTPUT_MODE_JSON_OR_TEXT,
        }:
            raise ValueError(f"未知 Surface output_mode: {self.output_mode!r}")
        if self.limit_strategy not in {
            LIMIT_STRATEGY_TRUNCATE, LIMIT_STRATEGY_ERROR,
        }:
            raise ValueError(f"未知 Surface limit_strategy: {self.limit_strategy!r}")
        object.__setattr__(self, "timeout_seconds", min(
            3600.0, max(0.001, float(self.timeout_seconds))))
        object.__setattr__(self, "max_input_chars", max(
            1, int(self.max_input_chars)))
        object.__setattr__(self, "max_output_chars", max(
            1, int(self.max_output_chars)))
        object.__setattr__(self, "input_schema", dict(self.input_schema))
        object.__setattr__(self, "output_schema", dict(self.output_schema))

    def with_limits(
        self,
        *,
        timeout_seconds: Optional[float] = None,
        max_input_chars: Optional[int] = None,
        max_output_chars: Optional[int] = None,
        output_mode: Optional[str] = None,
        limit_strategy: Optional[str] = None,
        redact_input: Optional[bool] = None,
        redact_output: Optional[bool] = None,
    ) -> "PromptSurfaceContract":
        return PromptSurfaceContract(
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            timeout_seconds=value_or(timeout_seconds, self.timeout_seconds),
            max_input_chars=value_or(max_input_chars, self.max_input_chars),
            max_output_chars=value_or(max_output_chars, self.max_output_chars),
            output_mode=value_or(output_mode, self.output_mode),
            limit_strategy=value_or(limit_strategy, self.limit_strategy),
            redact_input=value_or(redact_input, self.redact_input),
            redact_output=value_or(redact_output, self.redact_output),
            version=self.version,
        )

    @property
    def input_schema_hash(self) -> str:
        return _schema_hash(self.input_schema)

    @property
    def output_schema_hash(self) -> str:
        return _schema_hash(self.output_schema)

    def as_record(self) -> dict[str, object]:
        """Return content-light contract metadata for commands/observations."""
        return {
            "version": self.version,
            "input_schema_id": _schema_label(self.input_schema),
            "input_schema_hash": self.input_schema_hash,
            "output_schema_id": _schema_label(self.output_schema),
            "output_schema_hash": self.output_schema_hash,
            "output_mode": self.output_mode,
            "timeout_seconds": self.timeout_seconds,
            "max_input_chars": self.max_input_chars,
            "max_output_chars": self.max_output_chars,
            "limit_strategy": self.limit_strategy,
            "redact_input": self.redact_input,
            "redact_output": self.redact_output,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.as_record(),
            "input_schema": _jsonable(self.input_schema),
            "output_schema": _jsonable(self.output_schema),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PromptSurfaceContract":
        input_schema = raw.get("input_schema")
        output_schema = raw.get("output_schema")
        timeout_value = raw.get("timeout_seconds")
        input_limit = raw.get("max_input_chars")
        output_limit = raw.get("max_output_chars")
        return cls(
            input_schema=(
                input_schema if isinstance(input_schema, Mapping) else {}),
            output_schema=(
                output_schema if isinstance(output_schema, Mapping) else {}),
            timeout_seconds=(
                float(timeout_value)
                if isinstance(timeout_value, (int, float, str, bytes))
                else 30.0),
            max_input_chars=(
                int(input_limit)
                if isinstance(input_limit, (int, float, str, bytes))
                else 1),
            max_output_chars=(
                int(output_limit)
                if isinstance(output_limit, (int, float, str, bytes))
                else 1),
            output_mode=str(raw.get("output_mode", OUTPUT_MODE_TEXT) or OUTPUT_MODE_TEXT),
            limit_strategy=str(
                raw.get("limit_strategy", LIMIT_STRATEGY_TRUNCATE)
                or LIMIT_STRATEGY_TRUNCATE),
            redact_input=bool(raw.get("redact_input", True)),
            redact_output=bool(raw.get("redact_output", True)),
            version=str(raw.get("version", PROMPT_SURFACE_CONTRACT_VERSION)
                        or PROMPT_SURFACE_CONTRACT_VERSION),
        )


@dataclass(frozen=True)
class PromptSurfaceResult:
    surface_id: str
    status: str
    value: object = ""
    text: str = ""
    schema_valid: bool = False
    timed_out: bool = False
    truncated: bool = False
    redaction_applied: bool = False
    elapsed_ms: int = 0
    error: str = ""
    contract: Optional[PromptSurfaceContract] = None

    def __post_init__(self) -> None:
        if self.status not in _RESULT_STATUSES:
            raise ValueError(f"未知 Surface result status: {self.status!r}")

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def token_estimate(self) -> int:
        return estimate_text_tokens(self.text)


SurfaceRunner = Callable[[object], Awaitable[object]]
SurfaceObserver = Callable[[str, PromptSurfaceResult], None]


class PromptSurfaceExecutor:
    """Apply one contract to one auxiliary call, fail-closed and content-light."""

    async def execute(
        self,
        surface_id: str,
        contract: PromptSurfaceContract,
        value: object,
        runner: SurfaceRunner,
        *,
        redactor: Optional[SecretRedactor] = None,
        observer: Optional[SurfaceObserver] = None,
    ) -> PromptSurfaceResult:
        started = time.monotonic()
        def redact(text: str) -> str:
            return redact_obvious_secrets(
                redactor.redact(text) if redactor is not None else text)
        safe_value = _redact_value(value, redact) if contract.redact_input else value
        original_input_text = _value_text(value)
        input_text = _value_text(safe_value)
        redaction_applied = (
            contract.redact_input and input_text != original_input_text)
        try:
            validate_prompt_surface_schema(
                safe_value, contract.input_schema, label="input")
        except PromptSurfaceSchemaError as exc:
            result = PromptSurfaceResult(
                surface_id=surface_id,
                status="schema_error",
                redaction_applied=redaction_applied,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=str(exc),
                contract=contract,
            )
            self._observe(observer, surface_id, result)
            return result
        if len(input_text) > contract.max_input_chars:
            result = PromptSurfaceResult(
                surface_id=surface_id,
                status="input_limit",
                redaction_applied=redaction_applied,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=(
                    f"输入 {len(input_text)} 字符，超过上限 "
                    f"{contract.max_input_chars}"),
                contract=contract,
            )
            self._observe(observer, surface_id, result)
            return result

        try:
            raw_output = await asyncio.wait_for(
                runner(safe_value), timeout=contract.timeout_seconds)
        except asyncio.TimeoutError:
            result = PromptSurfaceResult(
                surface_id=surface_id,
                status="timeout",
                timed_out=True,
                redaction_applied=redaction_applied,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"执行超过 {contract.timeout_seconds:g}s",
                contract=contract,
            )
            self._observe(observer, surface_id, result)
            return result
        except Exception as exc:
            raw_error = f"{type(exc).__name__}: {exc}"
            safe_error = redact(raw_error)
            result = PromptSurfaceResult(
                surface_id=surface_id,
                status="error",
                redaction_applied=(
                    redaction_applied or safe_error != raw_error),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=safe_error,
                contract=contract,
            )
            self._observe(observer, surface_id, result)
            return result

        original_output_text = _value_text(raw_output)
        if contract.redact_output:
            raw_output = _redact_value(raw_output, redact)
        output_text = _value_text(raw_output)
        redaction_applied = redaction_applied or (
            contract.redact_output and output_text != original_output_text)
        truncated = False
        if len(output_text) > contract.max_output_chars:
            if contract.limit_strategy == LIMIT_STRATEGY_ERROR:
                result = PromptSurfaceResult(
                    surface_id=surface_id,
                    status="output_limit",
                    redaction_applied=redaction_applied,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error=(
                        f"输出 {len(output_text)} 字符，超过上限 "
                        f"{contract.max_output_chars}"),
                    contract=contract,
                )
                self._observe(observer, surface_id, result)
                return result
            marker = "\n…[surface output truncated]"
            output_text = output_text[:max(
                0, contract.max_output_chars - len(marker))] + marker
            raw_output = output_text
            truncated = True

        parsed: object = raw_output
        schema_valid = True
        schema_error = ""
        if contract.output_mode in {OUTPUT_MODE_JSON, OUTPUT_MODE_JSON_OR_TEXT}:
            if isinstance(raw_output, str):
                try:
                    parsed = _parse_json_output(raw_output)
                except (TypeError, ValueError):
                    if contract.output_mode == OUTPUT_MODE_JSON:
                        schema_valid = False
                        schema_error = "输出不是合法 JSON"
                    else:
                        parsed = raw_output
        if schema_valid and not (
            contract.output_mode == OUTPUT_MODE_JSON_OR_TEXT
            and isinstance(parsed, str)
        ):
            try:
                validate_prompt_surface_schema(
                    parsed, contract.output_schema, label="output")
            except PromptSurfaceSchemaError as exc:
                schema_valid = False
                schema_error = str(exc)
        elif contract.output_mode == OUTPUT_MODE_JSON_OR_TEXT and isinstance(parsed, str):
            try:
                validate_prompt_surface_schema(
                    parsed, contract.output_schema, label="output")
            except PromptSurfaceSchemaError as exc:
                schema_valid = False
                schema_error = str(exc)

        status = "ok" if schema_valid else "schema_error"
        result = PromptSurfaceResult(
            surface_id=surface_id,
            status=status,
            value=parsed,
            text=output_text,
            schema_valid=schema_valid,
            timed_out=False,
            truncated=truncated,
            redaction_applied=redaction_applied,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=schema_error,
            contract=contract,
        )
        self._observe(observer, surface_id, result)
        return result

    @staticmethod
    def _observe(
        observer: Optional[SurfaceObserver],
        surface_id: str,
        result: PromptSurfaceResult,
    ) -> None:
        if observer is None:
            return
        try:
            observer(surface_id, result)
        except Exception:
            # Observability must never alter surface execution.
            return


def _string_schema(
    *, schema_id: str, max_length: int, min_length: int = 0,
) -> dict[str, object]:
    return {
        "$id": schema_id,
        "type": "string",
        "minLength": min_length,
        "maxLength": max_length,
    }


REVIEW_SURFACE_CONTRACT = PromptSurfaceContract(
    input_schema=_string_schema(
        schema_id="https://krelcode.dev/schemas/review-input-v1.json",
        max_length=160_000, min_length=1),
    output_schema=_string_schema(
        schema_id="https://krelcode.dev/schemas/review-output-v1.json",
        max_length=64_000, min_length=1),
    timeout_seconds=300.0,
    max_input_chars=160_000,
    max_output_chars=64_000,
    output_mode=OUTPUT_MODE_TEXT,
)

COMPACT_SURFACE_CONTRACT = PromptSurfaceContract(
    input_schema=_string_schema(
        schema_id="https://krelcode.dev/schemas/compact-input-v1.json",
        max_length=1_000_000, min_length=1),
    output_schema=COMPACT_SUMMARY_JSON_SCHEMA,
    timeout_seconds=180.0,
    max_input_chars=1_000_000,
    max_output_chars=64_000,
    output_mode=OUTPUT_MODE_JSON,
    limit_strategy=LIMIT_STRATEGY_ERROR,
)

HOOK_PROMPT_SURFACE_CONTRACT = PromptSurfaceContract(
    input_schema={
        "$id": "https://krelcode.dev/schemas/prompt-hook-input-v1.json",
        "type": "object",
        "additionalProperties": False,
        "required": ["prompt", "payload"],
        "properties": {
            "prompt": {"type": "string", "maxLength": 64_000},
            "payload": {"type": "object"},
        },
    },
    output_schema={
        "$id": "https://krelcode.dev/schemas/prompt-hook-output-v1.json",
        "oneOf": [
            {"type": "string", "maxLength": 64_000},
            {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "decision": {"type": "string", "maxLength": 80},
                    "permissionDecision": {"type": "string", "maxLength": 80},
                    "reason": {"type": "string", "maxLength": 8_000},
                    "message": {"type": "string", "maxLength": 8_000},
                    "additional_context": {"type": ["string", "array"]},
                    "additionalContext": {"type": ["string", "array"]},
                    "system_message": {"type": "string", "maxLength": 4_000},
                    "systemMessage": {"type": "string", "maxLength": 4_000},
                },
            },
        ],
    },
    timeout_seconds=60.0,
    max_input_chars=128_000,
    max_output_chars=64_000,
    output_mode=OUTPUT_MODE_JSON_OR_TEXT,
)

MCP_PROMPT_SURFACE_CONTRACT = PromptSurfaceContract(
    input_schema={
        "$id": "https://krelcode.dev/schemas/mcp-prompt-input-v1.json",
        "type": "object",
        "additionalProperties": False,
        "required": ["server", "name", "arguments"],
        "properties": {
            "server": {"type": "string", "minLength": 1, "maxLength": 200},
            "name": {"type": "string", "minLength": 1, "maxLength": 200},
            "arguments": {"type": "object"},
        },
    },
    output_schema={
        "$id": "https://krelcode.dev/schemas/mcp-prompt-response-v1.json",
        "type": "object",
        "additionalProperties": False,
        "required": ["messages"],
        "properties": {
            "description": {"type": "string", "maxLength": 8_000},
            "_meta": {"type": "object"},
            "messages": {
                "type": "array",
                "maxItems": 100,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["role", "content"],
                    "properties": {
                        "role": {"type": "string", "enum": ["user", "assistant"]},
                        "content": {},
                        "_meta": {"type": "object"},
                    },
                },
            },
        },
    },
    timeout_seconds=30.0,
    max_input_chars=1_000_000,
    max_output_chars=1_000_000,
    output_mode=OUTPUT_MODE_JSON,
    limit_strategy=LIMIT_STRATEGY_ERROR,
)

MCP_INSTRUCTIONS_SURFACE_CONTRACT = PromptSurfaceContract(
    input_schema=_string_schema(
        schema_id="https://krelcode.dev/schemas/mcp-instructions-input-v1.json",
        max_length=256_000),
    output_schema=_string_schema(
        schema_id="https://krelcode.dev/schemas/mcp-instructions-output-v1.json",
        max_length=256_000),
    timeout_seconds=30.0,
    max_input_chars=256_000,
    max_output_chars=256_000,
    output_mode=OUTPUT_MODE_TEXT,
)


def contract_for_hook(timeout_seconds: float) -> PromptSurfaceContract:
    return HOOK_PROMPT_SURFACE_CONTRACT.with_limits(
        timeout_seconds=timeout_seconds)


def observe_surface_result(
    registry: object,
    result: PromptSurfaceResult,
    *,
    turn_id: str = "",
    step: int = 0,
) -> bool:
    """Record one contract result without importing the Registry at module load."""
    observe = getattr(registry, "observe", None)
    if not callable(observe):
        return False
    try:
        return bool(observe(
            result.surface_id,
            content_hash=hashlib.sha256(
                result.text.encode("utf-8")).hexdigest(),
            tokens=result.token_estimate,
            turn_id=turn_id,
            step=step,
            contract_status=result.status,
            schema_valid=result.schema_valid,
            timed_out=result.timed_out,
            truncated=result.truncated,
            redaction_applied=result.redaction_applied,
            elapsed_ms=result.elapsed_ms,
        ))
    except Exception:
        return False


__all__ = [
    "COMPACT_SURFACE_CONTRACT",
    "HOOK_PROMPT_SURFACE_CONTRACT",
    "LIMIT_STRATEGY_ERROR",
    "LIMIT_STRATEGY_TRUNCATE",
    "MCP_INSTRUCTIONS_SURFACE_CONTRACT",
    "MCP_PROMPT_SURFACE_CONTRACT",
    "OUTPUT_MODE_JSON",
    "OUTPUT_MODE_JSON_OR_TEXT",
    "OUTPUT_MODE_TEXT",
    "PROMPT_SURFACE_CONTRACT_VERSION",
    "PromptSurfaceContract",
    "PromptSurfaceContractError",
    "PromptSurfaceExecutor",
    "PromptSurfaceLimitError",
    "PromptSurfaceResult",
    "PromptSurfaceSchemaError",
    "PromptSurfaceTimeoutError",
    "REVIEW_SURFACE_CONTRACT",
    "contract_for_hook",
    "observe_surface_result",
    "validate_prompt_surface_schema",
]
