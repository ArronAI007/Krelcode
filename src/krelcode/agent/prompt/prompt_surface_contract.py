"""Agent-specific Prompt Surface contracts over the shared contracts layer.

Shared safety types, MCP wire-safety contracts and the executor Protocol live in
``krelcode.contracts.prompt_surface`` so protocol adapters do not import Agent.
The concrete executor plus Review, Compact and Hook policies remain here; the
historical module path remains a compatibility facade.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Mapping, Optional

from krelcode.agent.context.compact_summary import COMPACT_SUMMARY_JSON_SCHEMA
from krelcode.core.redact import MASK, SecretRedactor, redact_obvious_secrets
from krelcode.contracts.prompt_surface import (
    LIMIT_STRATEGY_ERROR,
    LIMIT_STRATEGY_TRUNCATE,
    MCP_INSTRUCTIONS_SURFACE_CONTRACT,
    MCP_PROMPT_SURFACE_CONTRACT,
    OUTPUT_MODE_JSON,
    OUTPUT_MODE_JSON_OR_TEXT,
    OUTPUT_MODE_TEXT,
    PROMPT_SURFACE_CONTRACT_VERSION,
    PromptSurfaceContract,
    PromptSurfaceContractError,
    PromptSurfaceExecutorPort,
    PromptSurfaceLimitError,
    PromptSurfaceResult,
    PromptSurfaceSchemaError,
    PromptSurfaceTimeoutError,
    SurfaceObserver,
    SurfaceRunner,
    observe_surface_result,
    validate_prompt_surface_schema,
)


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


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


class PromptSurfaceExecutor(PromptSurfaceExecutorPort):
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


def contract_for_hook(timeout_seconds: float) -> PromptSurfaceContract:
    return HOOK_PROMPT_SURFACE_CONTRACT.with_limits(
        timeout_seconds=timeout_seconds)


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
