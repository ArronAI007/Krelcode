"""Build one content-light manifest for the final model call."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional, Protocol

from ...contracts.context_manifest import (
    ContextManifest,
    ContextOmissionManifest,
    ContextSourceManifest,
)
from ...core.context_usage import ContextUsageSnapshot
from ...core.events import AgentEvent, ContextManifestCreated
from ...contracts.tool import ToolContext
from .context_plan import ContextPlanSnapshot, ContextSourceSnapshot
from .context_policy import ContextPolicy


class ContextManifestHostPort(Protocol):
    _current_turn_id: str
    _turn_tool_context: ToolContext
    last_context_manifest: ContextManifest | None = None

    async def _emit(self, event: AgentEvent) -> None: ...


def build_context_manifest(
    *,
    plan: ContextPlanSnapshot,
    usage: ContextUsageSnapshot,
    policy: ContextPolicy,
    session_id: str = "",
    task_id: str = "",
    model_call_id: str = "",
) -> ContextManifest:
    records: dict[str, ContextSourceManifest] = {}
    for fragment in plan.prompt_plan.fragments:
        provenance = fragment.provenance
        records[fragment.id] = ContextSourceManifest(
            layer_id=fragment.id,
            source_type=_source_type(
                fragment.source, provenance.get("source_type")),
            source_id=str(
                provenance.get("source_id") or fragment.source),
            scope=fragment.scope or "session",
            version=str(
                provenance.get("version")
                or provenance.get("prompt_release_id")
                or provenance.get("tool_playbook_version")
                or ""),
            trust_level=fragment.trust,
            permission_basis=str(
                provenance.get("permission_basis") or "context-policy"),
            selected_reason=str(
                provenance.get("selected_reason")
                or "selected-by-context-policy"),
            token_count=fragment.tokens,
            transform=str(provenance.get("transform") or "rendered"),
            content_hash=fragment.content_hash,
            category=fragment.layer,
        )
    for context_record in plan.source_records:
        existing = records.get(context_record.layer_id)
        if existing is None:
            records[context_record.layer_id] = _from_context_record(
                context_record)
        else:
            records[context_record.layer_id] = replace(
                existing,
                source_type=context_record.source_type or existing.source_type,
                source_id=context_record.source_id or existing.source_id,
                scope=context_record.scope or existing.scope,
                version=context_record.version or existing.version,
                trust_level=context_record.trust or existing.trust_level,
                permission_basis=(
                    context_record.permission_basis
                    or existing.permission_basis),
                selected_reason=(
                    context_record.selected_reason
                    or existing.selected_reason),
                token_count=context_record.tokens or existing.token_count,
                transform=context_record.transform or existing.transform,
                content_hash=(
                    context_record.content_hash or existing.content_hash),
                category=context_record.category or existing.category,
            )
    if usage.tool_schema_tokens > 0:
        records["tool-schema"] = ContextSourceManifest(
            layer_id="tool-schema",
            source_type="tool_schema",
            source_id="tool-catalog",
            scope="session",
            version=plan.prompt_plan.tool_catalog_hash,
            trust_level="platform",
            permission_basis="runtime-tool-allowlist",
            selected_reason="tools-advertised-for-current-turn",
            token_count=usage.tool_schema_tokens,
            transform="provider-schema",
            content_hash=plan.prompt_plan.tool_catalog_hash,
            category="tool_schema",
        )
    if usage.history_tokens > 0 and not any(
        item.source_type in {"conversation", "history"}
        for item in records.values()
    ):
        records["conversation-history"] = ContextSourceManifest(
            layer_id="conversation-history",
            source_type="conversation",
            source_id="history",
            scope="session",
            version=plan.history_digest,
            trust_level="mixed",
            permission_basis="session-membership",
            selected_reason="active-conversation-window",
            token_count=usage.history_tokens,
            transform="history-window",
            content_hash=plan.history_digest,
            category="history",
        )
    omitted = _omissions(plan)
    return ContextManifest(
        model_call_id=model_call_id or (
            f"{plan.turn_id}:model:{plan.step}"),
        turn_id=plan.turn_id,
        step=plan.step,
        context_policy=policy.identity(),
        sources=tuple(sorted(
            records.values(), key=lambda item: item.layer_id)),
        omitted=omitted,
        session_id=session_id,
        task_id=task_id,
        history_digest=plan.history_digest,
        request_digest=usage.request_digest,
        total_tokens=usage.estimated_input_tokens,
    )


async def emit_context_manifest(
    host: ContextManifestHostPort,
    *,
    plan: Optional[ContextPlanSnapshot],
    usage: ContextUsageSnapshot,
    policy: ContextPolicy,
) -> Optional[ContextManifest]:
    if plan is None:
        return None
    manifest = build_context_manifest(
        plan=plan,
        usage=usage,
        policy=policy,
        session_id=host._turn_tool_context.runtime.session_id or "",
        task_id="",
    )
    host.last_context_manifest = manifest
    await host._emit(ContextManifestCreated(manifest.as_dict()))
    return manifest


def _from_context_record(
    record: ContextSourceSnapshot,
) -> ContextSourceManifest:
    return ContextSourceManifest(
        layer_id=record.layer_id,
        source_type=record.source_type,
        source_id=record.source_id,
        scope=record.scope,
        version=record.version,
        trust_level=record.trust,
        permission_basis=record.permission_basis,
        selected_reason=record.selected_reason,
        token_count=record.tokens,
        transform=record.transform,
        content_hash=record.content_hash,
        category=record.category,
    )


def _omissions(plan: ContextPlanSnapshot) -> tuple[ContextOmissionManifest, ...]:
    records: dict[tuple[str, str], ContextOmissionManifest] = {}
    for item in plan.prompt_plan.omitted:
        records[(item.id, item.reason)] = ContextOmissionManifest(
            source_id=item.id,
            reason=item.reason,
            source_type=item.source,
            scope=item.scope,
            token_count=item.tokens,
        )
    for decision in plan.budget_decisions:
        if bool(decision.get("selected")):
            continue
        source_id = str(decision.get("layer_id") or "")
        reason = str(decision.get("reason") or "budget")
        records[(source_id, reason)] = ContextOmissionManifest(
            source_id=source_id,
            reason=reason,
            source_type=str(decision.get("category") or ""),
            scope="session",
            token_count=_int(decision.get("tokens")),
        )
    return tuple(records.values())


def _source_type(source: str, value: object) -> str:
    explicit = str(value or "").strip()
    if explicit:
        return explicit
    normalized = str(source or "").strip().lower()
    for marker in (
        "knowledge", "memory", "skill", "mcp", "prompt",
        "subagent", "workspace", "history",
    ):
        if marker in normalized:
            return marker
    return normalized or "unknown"


def _int(value: object) -> int:
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "ContextManifestHostPort",
    "build_context_manifest",
    "emit_context_manifest",
]
