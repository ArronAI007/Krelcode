"""Context inspection, rule, fragment, and compact commands."""

from __future__ import annotations

import json
import os
import shlex

from ....agent.context.context_inspection import ContextInspectionService
from ....agent.prompt.fragments import (
    refresh_registry_snapshot,
    rule_fragment,
    skills_fragment,
)
from ....agent.prompt.prompt_authoring import PROMPT_AUTHORING_FRAGMENT_ID
from ....core.tokens import estimate_history_tokens
from ....mcp.security import render_mcp_prompt_evidence
from ....safety.runtime import parse_permission_mode
from ....safety.rules import RuleDecision, RuleScope
from ..context import CommandContext
from ..sources import (
    sync_mcp_prompt_commands,
    sync_plugin_commands,
    sync_skill_commands,
)
from ..template import (
    COMMAND_TEMPLATE_VERSION,
    CommandArgumentMode,
    CommandRenderResult,
    CommandTemplate,
    split_reminder_ids,
)
from ..types import CommandResult, CommandSource

from .handler_utils import _ok


async def handle_context(ctx: CommandContext) -> CommandResult:
    raw = ctx.request.arguments.strip()
    try:
        parts = shlex.split(raw) if raw else []
    except ValueError as exc:
        return _ok(f"  [context] 参数解析失败: {exc}")
    operation = parts[0].lower() if parts else "stats"
    state = ctx.loop.runtime_state
    services = ctx.loop.services
    registry = services.prompt.registry

    if operation == "plan" and len(parts) == 1:
        inspection = ContextInspectionService(ctx.loop)
        return _ok(*inspection.plan_lines())

    if operation == "why" and len(parts) == 2:
        inspection = ContextInspectionService(ctx.loop)
        return _ok(*inspection.why_lines(parts[1]))

    if operation == "ledger" and len(parts) == 1:
        inspection = ContextInspectionService(ctx.loop)
        return _ok(*inspection.ledger_lines())

    if operation == "reminders" and len(parts) == 1:
        inspection = ContextInspectionService(ctx.loop)
        return _ok(*inspection.reminder_lines())

    if operation == "refresh" and len(parts) == 1:
        if ctx.skills is not None:
            services.extensions.skills = ctx.skills
        refreshed_workspace = await ctx.loop.refresh_workspace_context()
        lines = ["  [context] 工作区规则与环境附件已刷新"]
        lines.extend(f"  {line}" for line in refreshed_workspace.summary_lines())
        diff = state.context.last_attachment_diff
        if diff is not None:
            lines.extend(f"  {line}" for line in diff.summary_lines())
        prompt_snapshot = state.prompt.prompt_snapshot
        if prompt_snapshot is not None:
            lines.extend(f"  {line}" for line in prompt_snapshot.summary_lines())
        return _ok(*lines)

    if operation == "attachments" and len(parts) == 1:
        store = state.context.context_attachments
        if store is None:
            return _ok("  （Context Attachment Store 未启用）")
        attachment_snapshot = store.snapshot()
        lines = [
            f"  [attachments] {len(attachment_snapshot.items)} 个"
            f" · generation {attachment_snapshot.generation}"
            f" · {attachment_snapshot.total_tokens} tokens",
        ]
        for item in attachment_snapshot.items:
            lines.append(
                f"  {item.id} · {item.kind.value} · revision {item.revision}"
                f" · {item.layer} · {item.tokens} tokens · dynamic")
        diff = state.context.last_attachment_diff
        if diff is not None and diff.changed:
            lines.extend(f"  {line}" for line in diff.summary_lines())
        if not attachment_snapshot.items:
            lines.append("  （当前没有动态附件）")
        return _ok(*lines)

    if operation == "rules" and len(parts) == 1:
        loader = services.context.rule_loader
        workspace = state.context.workspace_context
        base = getattr(workspace, "rule_bundle", None)
        layers = list(getattr(workspace, "rule_layers", ()) or ())
        if not layers and loader is not None:
            stack = loader.load_stack(include_directory=False)
            layers = list(stack.base_layers)
            base = stack.base_bundle
        if base is None and loader is not None:
            base = loader.load_base()
        directory_source = services.context.directory_rule_source
        directory = getattr(directory_source, "last_bundle", None)
        if directory is not None and directory.content:
            from ....agent.rules import RuleLayer, RuleScope

            layers.append(RuleLayer(RuleScope.DIRECTORY, directory))
        bundles = [bundle for bundle in (base, directory) if bundle is not None]
        if not bundles:
            return _ok("  [rules] 规则加载未启用")
        source_count = sum(len(bundle.sources) for bundle in bundles)
        import_count = sum(bundle.import_count for bundle in bundles)
        issue_count = sum(len(bundle.issues) for bundle in bundles)
        lines = [
            f"  [rules] source {source_count} 个 · import {import_count} 个"
            f" · issue {issue_count} 个",
            "  precedence: managed → user → project → directory"
            "（后者更具体；managed 约束始终保留）",
        ]
        for layer in layers:
            fragment = rule_fragment(layer)
            if fragment is None:
                continue
            metadata = fragment.metadata
            raw_sources = metadata.get("rule_entry_sources", ())
            source_names = (
                ", ".join(str(item) for item in raw_sources)
                if isinstance(raw_sources, (list, tuple, set))
                else str(raw_sources or "")
            )
            raw_issues = metadata.get("rule_issues", ())
            issue_total = len(raw_issues) if isinstance(raw_issues, (list, tuple, set)) else 0
            lines.append(
                f"  [{metadata.get('rule_precedence', '?'):>2}] "
                f"{fragment.id} · sources="
                f"{source_names or '(none)'}"
                f" · imports={metadata.get('rule_import_count', 0)}"
                f" · issues={issue_total}")
            for source in layer.bundle.sources:
                imported = (
                    f" · import←{source.imported_by.name}"
                    if source.imported_by else ""
                )
                lines.append(
                    f"       {source.scope.value:9} {source.display_path}"
                    f" · {source.chars} chars{imported}")
            for issue in layer.bundle.issues:
                lines.append(
                    f"       [warn] {issue.code} · {issue.path} · {issue.message}")
        if not layers:
            for bundle in bundles:
                for source in bundle.sources:
                    lines.append(
                        f"  {source.scope.value:9} {source.display_path}"
                        f" · {source.chars} chars")
        return _ok(*lines)

    if operation == "sources":
        assembler = services.context.assembler
        if assembler is None:
            return _ok("  （ContextAssembler 未启用）")
        names = tuple(getattr(assembler, "source_names", ()) or ())
        lines = [
            f"  [sources] 已注册 {len(names)} 个 ContextSource: "
            f"{', '.join(names) if names else '(none)'}",
        ]
        lines.extend(ContextInspectionService(ctx.loop).sources_lines())
        return _ok(*lines)

    if operation == "fragments":
        if registry is None:
            return _ok("  （Prompt Fragment Registry 未启用）")
        if len(parts) > 2:
            return _ok("  用法: /context fragments [source]")
        source = parts[1] if len(parts) == 2 else ""
        fragments = registry.list(source=source)
        enabled_count = sum(
            1 for fragment in fragments if registry.is_enabled(fragment)
        )
        label = f"source={source}" if source else "all sources"
        lines = [
            f"  [fragments] {enabled_count}/{len(fragments)} enabled"
            f" · {label} · generation {registry.generation}",
        ]
        for fragment in fragments:
            fragment_state = "on " if registry.is_enabled(fragment) else "off"
            cache_state = "cache" if fragment.cacheable else "dynamic"
            toggle = "" if fragment.toggleable else " · locked"
            lines.append(
                f"  [{fragment_state}] {fragment.id} · {fragment.source}"
                f" · {fragment.group} · {fragment.tokens} tokens"
                f" · {cache_state}{toggle} · {fragment.title}")
        if not fragments:
            lines.append("  （没有匹配的 fragment）")
        return _ok(*lines)

    if operation in {"enable", "disable", "toggle"} and len(parts) == 2:
        if registry is None:
            return _ok("  （Prompt Fragment Registry 未启用）")
        target = parts[1]
        try:
            if operation == "toggle":
                kind, count, enabled = registry.toggle(target)
            else:
                enabled = operation == "enable"
                kind, count = registry.set_enabled(target, enabled)
        except (KeyError, ValueError) as exc:
            return _ok(f"  [fragments] {exc}")
        fragment_snapshot = refresh_registry_snapshot(ctx.loop)
        fragment_state = "enabled" if enabled else "disabled"
        lines = [f"  [fragments] {fragment_state} {kind}: {target} ({count})"]
        if fragment_snapshot is not None:
            lines.extend(f"  {line}" for line in fragment_snapshot.summary_lines())
        return _ok(*lines)

    if operation == "reset" and len(parts) == 1:
        if registry is None:
            return _ok("  （Prompt Fragment Registry 未启用）")
        registry.reset_overrides()
        reset_snapshot = refresh_registry_snapshot(ctx.loop)
        lines = ["  [fragments] 已清除全部手动启停设置"]
        if reset_snapshot is not None:
            lines.extend(f"  {line}" for line in reset_snapshot.summary_lines())
        return _ok(*lines)

    if operation == "cache" and len(parts) == 1:
        lines = ContextInspectionService(ctx.loop).cache_lines()
        context_plan = state.context.last_context_plan
        plan = state.prompt.last_prompt_plan
        if plan is not None and context_plan is None:
            cache_meta = plan.cache
            store = state.context.context_attachments
            attachment_generation = (
                store.snapshot().generation
                if store is not None else 0
            )
            lines[0:0] = [
                f"  [cache] prompt stable prefix {cache_meta.offset} chars"
                f" · {cache_meta.stable_tokens} tokens"
                f" · generation {plan.generation}"
                f" · attachment generation {attachment_generation}",
                f"  stable fragments: {', '.join(cache_meta.stable_fragments) or '(none)'}",
                f"  volatile fragments: {', '.join(cache_meta.volatile_fragments) or '(none)'}",
            ]
        elif registry is not None:
            registry_snapshot = registry.snapshot()
            cache_meta = registry_snapshot.cache
            lines[0:0] = [
                f"  [cache] prompt stable prefix {cache_meta.offset} chars"
                f" · {cache_meta.stable_tokens} tokens"
                f" · generation {registry_snapshot.generation}",
                f"  stable fragments: {', '.join(cache_meta.stable_fragments) or '(none)'}",
                f"  volatile fragments: {', '.join(cache_meta.volatile_fragments) or '(none)'}",
            ]
        elif not lines:
            lines = ["  （Prompt Fragment Registry 与请求缓存均未启用）"]
        return _ok(*lines)

    if operation == "authoring" and len(parts) == 1:
        if registry is None:
            return _ok("  （Prompt Fragment Registry 未启用）")
        fragment = registry.get(PROMPT_AUTHORING_FRAGMENT_ID)
        configured = bool(state.prompt.prompt_authoring_enabled)
        if fragment is None:
            return _ok(
                "  [authoring] disabled · PromptAuthoringContract 未装配",
                "  启用: [prompt.authoring] enabled = true",
                "  默认关闭；启用后仅在涉及 Prompt 设计/评审时使用 L6 契约",
            )
        enabled = bool(registry.is_enabled(fragment))
        metadata = fragment.metadata
        return _ok(
            f"  [authoring] {'enabled' if enabled else 'disabled'}"
            f" · contract v{metadata.get('prompt_authoring_contract', '?')}"
            f" · hash {str(metadata.get('prompt_authoring_hash', ''))[:12]}",
            f"  configured={str(configured).lower()}"
            f" · fragment={fragment.id} · {fragment.layer}"
            f" · {fragment.stability} · {fragment.trust}"
            f" · {fragment.tokens} tokens",
            "  capabilities: objective-analysis · instruction-evidence-separation"
            " · stable-dynamic-placement · conflict-detection"
            " · tool-permission-design · machine-verifiable-validation"
            " · candidate-prompt-schema · isolated-candidate-evaluation",
        )

    if operation == "conflicts" and len(parts) == 1:
        return _ok(*ContextInspectionService(ctx.loop).conflict_lines())

    if operation != "stats" or parts:
        return _ok(
            "  用法: /context | /context fragments [source] | "
            "/context attachments | "
            "/context sources | /context rules | /context conflicts | "
            "/context plan | /context why <id> | /context ledger | "
            "/context reminders | /context authoring | "
            "/context enable|disable|toggle <id|source> | "
            "/context reset | /context cache | /context refresh")

    context_snapshot = state.context.last_context_snapshot
    if context_snapshot is not None:
        budget = (
            f"{context_snapshot.budget_tokens} tokens"
            if context_snapshot.budget_tokens else "未启用压缩")
        window = (
            f"{context_snapshot.model_context_window} tokens"
            if context_snapshot.model_context_window else "未知")
        reserve = (
            f"{context_snapshot.output_reserve_tokens} tokens"
            if context_snapshot.model_context_window else "未知")
        ratio = (
            f"{context_snapshot.usage_ratio:.1%}"
            if context_snapshot.usage_ratio is not None else "未知")
        sources = " · ".join(
            f"{name} {tokens}"
            for name, tokens in sorted(
                context_snapshot.source_tokens.items(),
                key=lambda item: (-item[1], item[0]))[:8]
        ) or "（空）"
        lines = [
            f"  [context] 最近组装请求 · {context_snapshot.turn_id or '(unknown turn)'}"
            f" · Step {context_snapshot.step}",
            f"  输入估算: {context_snapshot.estimated_input_tokens} tokens"
            f" · 压缩预算: {budget} · 使用率: {ratio}",
            f"  请求: {context_snapshot.provider or '(unknown)'}"
            f" · wire {context_snapshot.wire_fidelity}"
            f" · body {context_snapshot.request_body_bytes} bytes"
            f" / {context_snapshot.request_body_tokens} tokens"
            f" · {'fits' if context_snapshot.input_fits else 'over budget'}",
            f"  模型窗口: {window} · 输出/安全保留: {reserve}"
            f" · provider 输出上限: {context_snapshot.output_limit_tokens or '未知'}",
            f"  分项: system {context_snapshot.system_tokens}"
            f" · workspace {context_snapshot.workspace_tokens}"
            f" · memory {context_snapshot.memory_tokens}"
            f" · history {context_snapshot.history_tokens}"
            f" · runtime {context_snapshot.runtime_tokens}"
            f" · tool schema {context_snapshot.tool_schema_tokens}"
            f" · overhead {context_snapshot.overhead_tokens}",
            f"  缓存: {context_snapshot.cache_mode}"
            f" · requested {context_snapshot.cache_requested_mode or 'unknown'}"
            f" · {context_snapshot.cache_negotiation_reason or 'unavailable'}"
            f" · stable blocks {context_snapshot.stable_system_block_count}"
            f" · dynamic blocks {context_snapshot.dynamic_system_block_count}"
            f" · stable {context_snapshot.stable_tokens}"
            f" · volatile {context_snapshot.volatile_tokens}"
            f" · boundary {context_snapshot.cache_boundary_chars} chars"
            f" · segments {context_snapshot.cache_segment_count}",
            f"  cache capabilities: "
            f"{', '.join(context_snapshot.cache_supported_modes) or '(unknown)'}"
            f" · max breakpoints {context_snapshot.cache_max_breakpoints}"
            f" · fallback {'yes' if context_snapshot.cache_fallback else 'no'}",
            f"  breakpoint planner: {context_snapshot.cache_breakpoint_strategy or '未启用'}"
            f" · selected {context_snapshot.cache_breakpoint_count}"
            f"/{context_snapshot.cache_max_breakpoints}"
            f" · skipped {context_snapshot.cache_breakpoint_skipped_count}"
            f" · minimum "
            f"{context_snapshot.cache_breakpoint_plan.get('minimum_cacheable_tokens', 0)}"
            f" tokens",
            f"  stable hash: {context_snapshot.stable_hash or '(none)'}",
            f"  dynamic hash: {context_snapshot.dynamic_hash or '(none)'}",
            f"  evidence channel: {context_snapshot.evidence_block_count} blocks"
            f" · hash {context_snapshot.evidence_hash or '(none)'}",
            f"  cache_control boundary: {context_snapshot.cache_control_boundary} chars",
            f"  full hash: {context_snapshot.full_hash or '(none)'}",
            f"  工具 {context_snapshot.tool_count} 个 · fragment {context_snapshot.fragment_count} 个"
            f" · source {context_snapshot.source_count} 个",
            f"  来源: {sources}",
        ]
        tool_messages = sum(
            1 for message in ctx.loop.history if message.role == "tool")
        lines.append(
            f"  当前历史 {len(ctx.loop.history)} 条消息"
            f"（其中工具结果 {tool_messages} 条）")
        working_store = services.context.working_context
        if working_store is not None:
            working = working_store.snapshot()
            lines.append(
                f"  Working Context: {working.count} 条 · {working.token_count} tokens"
                f" · {'已裁剪' if working.truncated else '完整'}")
        micro = state.context.last_microcompact_report
        if micro is not None:
            lines.append(
                f"  Tool Microcompact: {micro.compacted_count} 条"
                f" · {micro.before_tokens} → {micro.after_tokens} tokens"
                f" · summary {micro.summarized_count}"
                f" · reference {micro.referenced_count}")
        if context_snapshot.dropped_items:
            lines.append("  已裁剪: " + ", ".join(context_snapshot.dropped_items))
        workspace_bundle = state.context.workspace_context
        if workspace_bundle is not None and workspace_bundle.omitted:
            lines.append("  工作区未注入: " + ", ".join(workspace_bundle.omitted))
        inspection = ContextInspectionService(ctx.loop)
        memory_ids, memory_scopes, memory_tokens = inspection.memory_metadata()
        scopes_text = " · ".join(
            f"{scope} {count}" for scope, count in sorted(memory_scopes.items()))
        lines.append(
            f"  Memory 命中: {len(memory_ids)} 条"
            f" · {memory_tokens or context_snapshot.memory_tokens} tokens"
            f" · {scopes_text or '(none)'}")
        lines.append(f"  cache hash changed: {inspection.cache_change_text()}")
        return _ok(*lines)

    total = estimate_history_tokens(ctx.loop.history)
    budget_text = (
        str(ctx.loop.context_manager.max_tokens)
        if ctx.loop.context_manager else "未启用压缩"
    )
    tool_messages = sum(1 for message in ctx.loop.history if message.role == "tool")
    lines = [
        f"  [context] 历史 {len(ctx.loop.history)} 条消息"
        f"（含 {tool_messages} 条工具结果） · 估算 {total} tokens"
        f" · 预算 {budget_text}",
    ]
    workspace_bundle = state.context.workspace_context
    if workspace_bundle is not None:
        lines.extend(f"  {line}" for line in workspace_bundle.summary_lines())
    runtime_context = state.turn.runtime_context
    if runtime_context:
        lines.append(f"  运行时 Hook 上下文: {len(runtime_context)} 段")
    working_store = services.context.working_context
    if working_store is not None:
        working = working_store.snapshot()
        lines.append(
            f"  Working Context: {working.count} 条"
            f" · {working.token_count} tokens"
            f" · {'已裁剪' if working.truncated else '完整'}")
    micro = state.context.last_microcompact_report
    if micro is not None:
        lines.append(
            f"  Tool Microcompact: {micro.compacted_count} 条"
            f" · saved {micro.saved_tokens} tokens")
    context_manager = services.context.manager
    summary = getattr(context_manager, "last_summary", None)
    if summary is not None:
        lines.append(
            f"  Structured Compact: v{summary.version}"
            f" · generation {summary.generation}"
            f" · source {summary.source}"
            f" · {summary.token_count} tokens"
            f" · {len(summary.files)} files"
            f" · {len(summary.tests)} tests"
            f" · digest {summary.digest}")
    checkpoint = state.context.last_compact_checkpoint
    if checkpoint is not None:
        lines.append(
            f"  Compact Checkpoint: {checkpoint.id}"
            f" · {checkpoint.before_tokens} → {checkpoint.after_tokens} tokens")
    if registry is not None:
        lines.extend(f"  {line}" for line in registry.snapshot().summary_lines())
    return _ok(*lines)


async def handle_compact(ctx: CommandContext) -> CommandResult:
    state = ctx.loop.runtime_state
    if ctx.loop.context_manager is None:
        return _ok("  （上下文压缩未启用：--no-compress）")
    raw = ctx.request.arguments.strip()
    try:
        parts = shlex.split(raw) if raw else []
    except ValueError as exc:
        return _ok(f"  [compact] 参数解析失败: {exc}")
    preview_mode = bool(parts and parts[0].lower() == "preview")
    auto_mode = bool(parts and "--auto" in parts)
    if preview_mode:
        focus_parts = [part for part in parts[1:] if part != "--auto"]
    else:
        focus_parts = [part for part in parts if part != "--auto"]
    focus = " ".join(focus_parts).strip()
    manager = ctx.loop.context_manager
    if preview_mode:
        preview = manager.preview_compact(
            ctx.loop.history, force=not auto_mode,
            current_turn_id=state.turn.current_turn_id)
        return _ok(*[
            f"  {line}" for line in preview.render().splitlines()
        ])

    before = estimate_history_tokens(ctx.loop.history)
    done = await ctx.loop.compress_context(force=True, focus=focus)
    after = estimate_history_tokens(ctx.loop.history)
    last_preview = getattr(manager, "last_compact_preview", None)
    details = (
        last_preview.render(include_messages=False).splitlines()
        if last_preview is not None else []
    )
    if done:
        lines = [f"  [compact] 已压缩: {before} → {after} tokens"
                 + (f" · focus={focus[:80]}" if focus else "")]
        checkpoint = state.context.last_compact_checkpoint
        if checkpoint is not None:
            lines.append(f"  checkpoint: {checkpoint.id}")
    else:
        lines = [f"  [compact] 未压缩（当前约 {before} tokens）"]
    lines.extend(f"  {line}" for line in details)
    return _ok(*lines)
