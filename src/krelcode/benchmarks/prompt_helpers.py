"""Content-light checks and bounded report helpers for Prompt benchmarks."""

from __future__ import annotations

import hashlib
import time
from typing import Mapping

from ..agent.context.context_cache_ledger import ContextCacheLedger
from ..agent.prompt.fragments import LAYER_L7_EVIDENCE
from ..agent.prompt.prompt_plan import PromptFragmentSnapshot, PromptPlanSnapshot
from ..core.messages import Message
from ..llm.base import PreparedLLMRequest
from .prompt_models import PromptBenchmarkCheck, PromptBenchmarkMetric


_RULE_ORDER = (
    "managed:rules",
    "user:rules",
    "project:rules",
    "directory:rules",
)


def _message_payload(message: Message) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "name": call.name,
                "arguments": dict(call.arguments),
            }
            for call in (message.tool_calls or ())
        ],
        "tool_call_id": message.tool_call_id or "",
        "tool_name": message.tool_name or "",
        "tool_is_error": message.tool_is_error,
    }


def _check(
    metric: PromptBenchmarkMetric,
    passed: bool,
    detail: str,
) -> PromptBenchmarkCheck:
    return PromptBenchmarkCheck(metric=metric, passed=bool(passed), detail=detail)


def _memory_fragments(
    plan: PromptPlanSnapshot,
) -> tuple[PromptFragmentSnapshot, ...]:
    return tuple(
        fragment for fragment in plan.fragments
        if fragment.layer == LAYER_L7_EVIDENCE
        or fragment.source in {"memory", "memory_retrieval", "inherited_memory"}
        or fragment.id.startswith("memory:")
    )


def _memory_is_evidence(plan: PromptPlanSnapshot) -> bool:
    fragments = _memory_fragments(plan)
    return bool(fragments) and all(
        fragment.provenance.get("source_role") == "evidence"
        and fragment.provenance.get("instruction") is False
        and fragment.provenance.get("requires_validation") is True
        and not fragment.stable
        for fragment in fragments
    )


def _rule_order(plan: PromptPlanSnapshot) -> tuple[str, ...]:
    positions = {
        fragment.id: index
        for index, fragment in enumerate(plan.fragments)
        if fragment.id in _RULE_ORDER
    }
    return tuple(sorted(positions, key=lambda item: positions[item]))


def _rule_order_is_stable(
    first: PromptPlanSnapshot,
    second: PromptPlanSnapshot,
) -> bool:
    return _rule_order(first) == _RULE_ORDER and _rule_order(second) == _RULE_ORDER


def _stable_prefix_is_isolated(
    first: PromptPlanSnapshot,
    second: PromptPlanSnapshot,
) -> bool:
    stable_ids = {
        fragment.id
        for fragment in first.fragments
        if fragment.stable
    } | {
        fragment.id
        for fragment in second.fragments
        if fragment.stable
    }
    polluted = stable_ids.intersection({
        "memory:scoped",
        "subagent:inherited-memory",
        "attachment:environment_snapshot",
        "attachment:environment_delta",
        "attachment:date",
        "attachment:mode_reminder",
        "attachment:mode_permission_contract",
    })
    return (
        bool(first.stable_hash)
        and first.stable_hash == second.stable_hash
        and not polluted
    )


def _provider_hash_is_exact(
    plan: PromptPlanSnapshot,
    ledger: ContextCacheLedger,
    prepared: PreparedLLMRequest | None,
) -> bool:
    if prepared is None:
        return False
    actual_body_hash = hashlib.sha256(prepared.body).hexdigest()
    return (
        ledger.wire_fidelity == "exact"
        and bool(ledger.provider_request_digest)
        and prepared.payload_digest == actual_body_hash
        and ledger.provider_request_digest == prepared.payload_digest
        and ledger.stable_hash == plan.stable_hash
        and prepared.stable_cache_hash == plan.stable_hash
    )


def _scope_filtered(plan: PromptPlanSnapshot) -> bool:
    return any(
        omission.reason == "file-explanation-scope"
        and omission.id in {
            "workspace:tree",
            "workspace:git",
            "attachment:environment_snapshot",
            "attachment:environment_delta",
        }
        for omission in plan.omitted
    )


def _safe_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """Keep report facts JSON-safe and never stringify opaque prompt objects."""
    result: dict[str, object] = {}
    for key, item in value.items():
        result[str(key)[:120]] = _safe_report_value(item)
    return result


def _safe_report_value(value: object, *, depth: int = 0) -> object:
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 4:
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, Mapping):
        return {
            str(key)[:100]: _safe_report_value(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [
            _safe_report_value(item, depth=depth + 1)
            for item in value[:100]
        ]
    # Opaque objects may carry Prompt, Memory or tool text.  Keep only their
    # type instead of calling str(), which could silently leak content.
    return {"type": type(value).__name__}


def _safe_error(error: BaseException) -> str:
    message = " ".join(str(error).split())
    return f"{type(error).__name__}: {message[:300]}"


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))
