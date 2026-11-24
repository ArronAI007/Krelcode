"""Read-only Prompt Surface catalogue and live benchmark diagnostics."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from ....agent.prompt_surface import (
    CORE_SURFACE_KINDS,
    PromptSurfaceKind,
    PromptSurfaceRegistry,
)
from ....agent.prompt_surface_catalog import refresh_prompt_surfaces
from ....tools.prompt_lint import lint_tool_registry
from ..context import CommandContext
from ..types import CommandResult


MAX_LISTED_SURFACES = 100
OBSERVATION_STATES = frozenset({
    "observed", "unobserved", "stale", "unavailable",
})


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


def _observation_line(registry: PromptSurfaceRegistry, observation) -> str:
    surface = registry.get(observation.surface_id)
    current = bool(
        surface is not None
        and observation.is_current(surface.content_hash))
    return (
        f"  {observation.surface_id} · renders={observation.render_count}"
        f" · current={str(current).lower()}"
        f" · turn={observation.last_turn_id or '(none)'}"
        f" · step={observation.last_step}"
        f" · tokens={observation.last_tokens}"
        f" · contract={observation.contract_status or 'unobserved'}"
        f" · elapsed_ms={observation.elapsed_ms}"
        f" · rendered_hash={observation.last_content_hash[:12] or '(none)'}")


def _matches_text(surface, selector: str) -> bool:
    if not selector:
        return True
    return any(
        selector.lower() in value.lower()
        for value in (
            surface.id,
            surface.kind.value,
            surface.source,
            surface.owner,
        )
    )


def _kind_filter(selector: str) -> PromptSurfaceKind | None:
    try:
        return PromptSurfaceKind(selector) if selector else None
    except ValueError:
        return None


def _benchmark_lines(
    operation: str,
    ctx: CommandContext,
    *,
    report_path: str = "",
) -> list[str]:
    from ....agent.prompt_benchmark_catalog import PROMPT_BENCHMARK_CASES
    from ....agent.prompt_benchmark_report import (
        DEFAULT_BEHAVIOR_REPORT_PATH,
        load_behavior_report_summary,
    )
    from ....agent.prompt_parity import PROMPT_PARITY_CONTRACT

    if operation == "list":
        lines = [
            f"  [prompts benchmark] {len(PROMPT_BENCHMARK_CASES)} live behavior cases"
            " · real provider required · content-light reports",
        ]
        for case in PROMPT_BENCHMARK_CASES:
            dimensions = PROMPT_PARITY_CONTRACT.requirement_for_case(case.id)
            suffix = f" · {', '.join(dimensions)}" if dimensions else ""
            lines.append(f"  {case.id} · {case.description}{suffix}")
        lines.append("  run: krelcode benchmark --live --trials 3 --save-report")
        return lines

    configured = report_path or DEFAULT_BEHAVIOR_REPORT_PATH
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = ctx.workspace_root / path
    try:
        summary = load_behavior_report_summary(path)
    except ValueError as exc:
        return [
            f"  [prompts benchmark] no readable report · {exc}",
            "  run: krelcode benchmark --live --trials 3 --save-report",
        ]
    errors = (
        ", ".join(f"{key}={value}" for key, value in summary.error_counts.items())
        or "none")
    return [
        f"  [prompts benchmark] {'PASS' if summary.passed else 'FAIL'}"
        f" · {summary.provider}/{summary.model}",
        f"  generated={summary.generated_at} · cases={summary.cases}"
        f" · stable={summary.stable_cases}"
        f" · trials={summary.passed_trials}/{summary.trials}"
        f" · pass_at_k={summary.pass_at_k}",
        f"  tools={summary.tool_calls} · tokens={summary.total_tokens}"
        f" · cost_usd={summary.cost_usd} · duration_ms={summary.duration_ms}",
        f"  errors={errors} · path={summary.path} · content-light=true",
        "  run: krelcode benchmark --live --trials 3 --save-report",
    ]


async def handle_prompts(ctx: CommandContext) -> CommandResult:
    try:
        parts = shlex.split(ctx.request.arguments) if ctx.request.arguments else []
    except ValueError as exc:
        return _ok(f"  [prompts] 参数解析失败: {exc}")
    operation = parts[0].lower() if parts else "summary"

    if operation == "lint" and len(parts) <= 2:
        tool_names = {parts[1]} if len(parts) == 2 else None
        tool_registry = getattr(ctx.loop, "registry", None)
        if tool_registry is None:
            return _ok("  [prompts lint] 当前会话没有 ToolRegistry")
        if tool_names is not None and tool_registry.get(parts[1]) is None:
            return _ok(f"  [prompts lint] 未找到工具: {parts[1]}")
        report = lint_tool_registry(tool_registry, tool_names=tool_names)
        scope = f" tool={parts[1]}" if len(parts) == 2 else ""
        return _ok(*report.lines(), f"  scope={scope or ' all'}")

    registry = _registry(ctx)

    if operation == "summary" and len(parts) <= 1:
        lines = registry.summary_lines()
        lines.extend((
            "  usage: /prompts list [kind|source|state] | /prompts show <id>"
            " | /prompts health | /prompts observations [kind|source]"
            " | /prompts active [kind|source] | /prompts lint [tool]"
            " | /prompts benchmark [status [path]|list]"
            " | /prompts coverage | /prompts hash",
            "  Prompt bodies are never included; values are metadata and hashes only.",
        ))
        return _ok(*lines)

    if operation == "health" and len(parts) <= 1:
        health = registry.health()
        lines = [
            f"  [prompts health] surfaces={health['surfaces']}"
            f" · observed={health['observed']}"
            f" · unobserved={health['unobserved']}"
            f" · stale={health['stale']}"
            f" · unavailable={health['unavailable']}"
            f" · renders={health['render_count']}"
            f" · contract_failures={health['contract_failures']}",
        ]
        by_kind = health.get("by_kind")
        if isinstance(by_kind, dict):
            for kind, values in by_kind.items():
                if not isinstance(values, dict):
                    continue
                lines.append(
                    f"  {kind}: surfaces={values.get('surfaces', 0)}"
                    f" · observed={values.get('observed', 0)}"
                    f" · stale={values.get('stale', 0)}"
                    f" · unavailable={values.get('unavailable', 0)}"
                    f" · contract_failures={values.get('contract_failures', 0)}")
        lines.append("  content-light=true")
        return _ok(*lines)

    if operation == "benchmark":
        if len(parts) == 1:
            return _ok(*_benchmark_lines("status", ctx))
        if len(parts) == 2 and parts[1].lower() == "list":
            return _ok(*_benchmark_lines("list", ctx))
        if len(parts) <= 3 and parts[1].lower() == "status":
            path = parts[2] if len(parts) == 3 else ""
            return _ok(*_benchmark_lines("status", ctx, report_path=path))
        return _ok(
            "  用法: /prompts benchmark [status [path]|list]"
            "；真实调用请运行 krelcode benchmark --live --trials 3 --save-report")

    if operation == "list" and len(parts) <= 2:
        selector = parts[1] if len(parts) == 2 else ""
        kind = _kind_filter(selector)
        if kind is not None:
            surfaces = registry.list(kind=kind)
        elif selector.lower() in OBSERVATION_STATES:
            state = selector.lower()
            surfaces = []
            for surface in registry.list():
                observation = registry.observation(surface.id)
                if state == "observed" and observation is not None:
                    surfaces.append(surface)
                elif state == "unobserved" and observation is None:
                    surfaces.append(surface)
                elif state == "unavailable" and not surface.available:
                    surfaces.append(surface)
                elif (
                    state == "stale"
                    and observation is not None
                    and not observation.is_current(surface.content_hash)
                ):
                    surfaces.append(surface)
            surfaces = tuple(surfaces)
        else:
            surfaces = registry.list(query=selector)
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

    if operation == "observations" and len(parts) <= 2:
        selector = parts[1] if len(parts) == 2 else ""
        kind = _kind_filter(selector)
        observations = []
        for observation in registry.observations():
            surface = registry.get(observation.surface_id)
            if surface is None:
                continue
            if kind is not None and surface.kind is not kind:
                continue
            if kind is None and not _matches_text(surface, selector):
                continue
            observations.append(observation)
        lines = [
            f"  [prompts observations] {len(observations)} rendered surfaces"
            + (f" · filter={selector}" if selector else ""),
        ]
        lines.extend(_observation_line(registry, item) for item in observations)
        if not observations:
            lines.append("  （当前进程还没有匹配的真实渲染观测）")
        return _ok(*lines)

    if operation == "active" and len(parts) <= 2:
        selector = parts[1] if len(parts) == 2 else ""
        kind = _kind_filter(selector)
        context_plan = getattr(ctx.loop, "last_context_plan", None)
        prompt_plan = getattr(context_plan, "prompt_plan", None)
        turn_id = str(getattr(prompt_plan, "turn_id", "") or "")
        if not turn_id:
            observations = registry.observations()
            turn_id = observations[-1].last_turn_id if observations else ""
        selected = []
        for observation in registry.observations_for_turn(turn_id):
            surface = registry.get(observation.surface_id)
            if surface is None:
                continue
            if kind is not None and surface.kind is not kind:
                continue
            if kind is None and not _matches_text(surface, selector):
                continue
            selected.append(observation)
        lines = [
            f"  [prompts active] turn={turn_id or '(none)'}"
            f" · surfaces={len(selected)}"
            + (f" · filter={selector}" if selector else ""),
        ]
        if prompt_plan is not None:
            lines.append(
                f"  fragments={len(prompt_plan.fragments)}"
                f" · stable_tokens={prompt_plan.stable_tokens}"
                f" · volatile_tokens={prompt_plan.volatile_tokens}"
                f" · tools={prompt_plan.tool_count}"
                f" · conflicts={prompt_plan.unresolved_conflict_count}"
                f" · cache_boundary={prompt_plan.cache_boundary_chars}")
        lines.extend(_observation_line(registry, item) for item in selected)
        if not selected:
            lines.append(
                "  （该 turn 尚无真实渲染观测；先执行一轮 Agent 请求）")
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
            lines.append(_observation_line(registry, observation))
        return _ok(*lines)

    if operation == "coverage" and len(parts) == 1:
        counts = registry.kind_counts()
        missing = [
            kind.value for kind in CORE_SURFACE_KINDS if counts.get(kind.value, 0) == 0
        ]
        health = registry.health()
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
            f"  observed surfaces: {health['observed']}/{health['surfaces']}"
            f" · stale: {health['stale']}"
            f" · contract failures: {health['contract_failures']}",
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
        "  用法: /prompts | /prompts list [kind|source|state] | "
        "/prompts show <id> | /prompts health | "
        "/prompts observations [kind|source] | "
        "/prompts active [kind|source] | /prompts lint [tool] | "
        "/prompts benchmark [status [path]|list] | "
        "/prompts coverage | /prompts hash")


__all__ = ["handle_prompts"]
