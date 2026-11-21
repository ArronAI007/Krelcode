"""Read-only Prompt Surface catalogue commands."""

from __future__ import annotations

import json
import shlex

from ....agent.prompt_surface import (
    CORE_SURFACE_KINDS,
    PromptSurfaceKind,
    PromptSurfaceRegistry,
)
from ....agent.prompt_surface_catalog import refresh_prompt_surfaces
from ..context import CommandContext
from ..types import CommandResult


MAX_LISTED_SURFACES = 100


def _ok(*output: str) -> CommandResult:
    return CommandResult(handled=True, output=tuple(output))


def _registry(ctx: CommandContext) -> PromptSurfaceRegistry:
    return refresh_prompt_surfaces(
        ctx.loop, mcp_supervisor=ctx.mcp_supervisor)


def _surface_line(registry: PromptSurfaceRegistry, surface) -> str:
    observation = registry.observation(surface.id)
    state = "unobserved"
    if observation is not None:
        state = f"observed×{observation.render_count}"
        if not observation.is_current(surface.content_hash):
            state += "/stale"
    available = "" if surface.available else " · unavailable"
    contract = (
        f" · contract={surface.contract.version}"
        f"/{surface.contract.timeout_seconds:g}s"
        if surface.contract is not None else "")
    return (
        f"  {surface.id} · {surface.kind.value}"
        f" · {surface.role.value}/{surface.authority}"
        f" · trust={surface.trust} · {surface.token_estimate}t"
        f" · {surface.content_hash[:12]} · {state}{contract}{available}")


async def handle_prompts(ctx: CommandContext) -> CommandResult:
    try:
        parts = shlex.split(ctx.request.arguments) if ctx.request.arguments else []
    except ValueError as exc:
        return _ok(f"  [prompts] 参数解析失败: {exc}")
    registry = _registry(ctx)
    operation = parts[0].lower() if parts else "summary"

    if operation == "summary" and len(parts) <= 1:
        lines = registry.summary_lines()
        lines.extend((
            "  usage: /prompts list [kind|source] | /prompts show <id>"
            " | /prompts coverage | /prompts hash",
            "  Prompt bodies are never included; values are metadata and hashes only.",
        ))
        return _ok(*lines)

    if operation == "list" and len(parts) <= 2:
        selector = parts[1] if len(parts) == 2 else ""
        try:
            kind = PromptSurfaceKind(selector)
        except ValueError:
            kind = None
        surfaces = (
            registry.list(kind=kind) if kind is not None
            else registry.list(query=selector)
        )
        lines = [
            f"  [prompts] {len(surfaces)} surfaces"
            + (f" · filter={selector}" if selector else ""),
        ]
        lines.extend(
            _surface_line(registry, surface)
            for surface in surfaces[:MAX_LISTED_SURFACES])
        if len(surfaces) > MAX_LISTED_SURFACES:
            lines.append(
                f"  …另有 {len(surfaces) - MAX_LISTED_SURFACES} 个；请增加过滤条件")
        if not surfaces:
            lines.append("  （没有匹配的 Prompt Surface）")
        return _ok(*lines)

    if operation == "show" and len(parts) == 2:
        surface = registry.get(parts[1])
        if surface is None:
            return _ok(f"  [prompts] 未找到: {parts[1]}")
        observation = registry.observation(surface.id)
        metadata = json.dumps(
            dict(surface.metadata), ensure_ascii=False, sort_keys=True)
        contract = (
            json.dumps(
                surface.contract.as_record(), ensure_ascii=False, sort_keys=True)
            if surface.contract is not None else "(none)")
        lines = [
            f"  [prompts] {surface.id}",
            f"  kind={surface.kind.value} · collection={registry.collection_for(surface.id)}",
            f"  source={surface.source} · owner={surface.owner}"
            f" · version={surface.version or '(none)'}",
            f"  role={surface.role.value}/{surface.authority}"
            f" · trust={surface.trust} · stability={surface.stability}"
            f" · scope={surface.scope}",
            f"  hash={surface.content_hash} · tokens={surface.token_estimate}"
            f" · available={str(surface.available).lower()}",
            f"  capabilities={', '.join(surface.capabilities) or '(none)'}",
            f"  description={surface.description or '(none)'}",
            f"  metadata={metadata}",
            f"  contract={contract}",
        ]
        if observation is None:
            lines.append("  observation=unobserved")
        else:
            current = observation.is_current(surface.content_hash)
            lines.append(
                f"  observation=renders:{observation.render_count}"
                f" · current:{str(current).lower()}"
                f" · turn:{observation.last_turn_id or '(none)'}"
                f" · step:{observation.last_step}"
                f" · tokens:{observation.last_tokens}"
                f" · contract:{observation.contract_status or 'unobserved'}"
                f" · schema:{str(observation.schema_valid).lower()}"
                f" · timed_out:{str(observation.timed_out).lower()}"
                f" · truncated:{str(observation.truncated).lower()}"
                f" · redacted:{str(observation.redaction_applied).lower()}"
                f" · elapsed_ms:{observation.elapsed_ms}"
                f" · rendered_hash:{observation.last_content_hash[:12] or '(none)'}")
        return _ok(*lines)

    if operation == "coverage" and len(parts) == 1:
        counts = registry.kind_counts()
        missing = [
            kind.value for kind in CORE_SURFACE_KINDS if counts.get(kind.value, 0) == 0
        ]
        observed = sum(
            1 for surface in registry.list()
            if registry.observation(surface.id) is not None)
        lines = [
            f"  [prompts] core coverage: {'complete' if not missing else 'missing ' + ', '.join(missing)}",
            "  kinds: " + (" · ".join(
                f"{name}={count}" for name, count in counts.items()) or "(none)"),
            "  roles: " + (" · ".join(
                f"{name}={count}" for name, count in registry.role_counts().items())
                or "(none)"),
            "  trust: " + (" · ".join(
                f"{name}={count}" for name, count in registry.trust_counts().items())
                or "(none)"),
            f"  observed surfaces: {observed}/{len(registry.list())}",
            "  optional external sources: custom_command, skill, plugin_command,"
            " hook, mcp_prompt, output_style",
        ]
        return _ok(*lines)

    if operation == "hash" and len(parts) == 1:
        snapshot = registry.snapshot()
        return _ok(
            f"  [prompts] catalogue hash {snapshot.content_hash}",
            f"  generation={snapshot.generation} · surfaces={len(snapshot.surfaces)}"
            f" · observations={len(snapshot.observations)}"
            " · content-light=true")

    return _ok(
        "  用法: /prompts | /prompts list [kind|source] | "
        "/prompts show <id> | /prompts coverage | /prompts hash")


__all__ = ["handle_prompts"]
