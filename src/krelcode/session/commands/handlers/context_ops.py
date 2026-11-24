"""Context inspection, rule, fragment, and compact commands."""

from __future__ import annotations

import json
import os
import shlex

from ....agent.context import build_workspace_context_bundle
from ....agent.context_inspection import ContextInspectionService
from ....agent.cyber_risk import prepend_cyber_risk_prompt
from ....agent.fragments import (
    GROUP_WORKSPACE,
    refresh_registry_snapshot,
    rule_fragment,
    skills_fragment,
    workspace_fragments,
)
from ....agent.prompt_authoring import PROMPT_AUTHORING_FRAGMENT_ID
from ....agent.prompts import build_system_prompt
from ....agent.tokens import estimate_history_tokens
from ....core.messages import Message
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
    registry = getattr(ctx.loop, "prompt_registry", None)

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
        manager = ctx.skills or getattr(ctx.loop, "skill_manager", None)
        if manager is not None and getattr(ctx.loop, "skill_manager", None) is None:
            ctx.loop.skill_manager = manager
        refresh = getattr(ctx.loop, "refresh_workspace_context", None)
        if callable(refresh):
            bundle = await refresh()
            lines = ["  [context] 工作区规则与环境附件已刷新"]
            lines.extend(f"  {line}" for line in bundle.summary_lines())
            diff = getattr(ctx.loop, "last_attachment_diff", None)
            if diff is not None:
                lines.extend(f"  {line}" for line in diff.summary_lines())
            snapshot = getattr(ctx.loop, "prompt_snapshot", None)
            if snapshot is not None:
                lines.extend(f"  {line}" for line in snapshot.summary_lines())
            return _ok(*lines)

        options = getattr(ctx.loop, "context_options", None)
        rule_loader = getattr(ctx.loop, "rule_loader", None)
        bundle = await build_workspace_context_bundle(
            ctx.workspace_root, options=options, rule_loader=rule_loader)
        ctx.loop.workspace_context = bundle
        if registry is not None:
            registry.replace_group(
                GROUP_WORKSPACE, workspace_fragments(bundle))
            if manager is not None:
                skill_fragment = skills_fragment(manager.prompt_index())
                if skill_fragment is None:
                    registry.remove("skills:index")
                else:
                    registry.upsert(skill_fragment)
            snapshot = refresh_registry_snapshot(ctx.loop)
            lines = ["  [context] 工作区上下文已刷新"]
            lines.extend(f"  {line}" for line in bundle.summary_lines())
            if snapshot is not None:
                lines.extend(f"  {line}" for line in snapshot.summary_lines())
            return _ok(*lines)

        # 兼容没有 Registry 的嵌入式 AgentLoop。
        base_prompt = prepend_cyber_risk_prompt(build_system_prompt(
            bundle.text,
            profile=getattr(ctx.loop, "prompt_profile_config", "auto"),
            provider=getattr(ctx.loop, "llm", ""),
            model=getattr(getattr(ctx.loop, "llm", None), "model", ""),
        ))
        ctx.loop.skill_base_prompt = base_prompt
        prompt = (
            manager.apply_to_prompt(base_prompt) if manager is not None
            else base_prompt)
        if ctx.loop.history and ctx.loop.history[0].role == "system":
            ctx.loop.history[0] = Message.system(prompt)
        else:
            ctx.loop.history.insert(0, Message.system(prompt))
        return _ok(
            "  [context] 工作区上下文已刷新",
            *[f"  {line}" for line in bundle.summary_lines()],
        )

    if operation == "attachments" and len(parts) == 1:
        store = getattr(ctx.loop, "context_attachments", None)
        if store is None:
            return _ok("  （Context Attachment Store 未启用）")
        snapshot = store.snapshot()
        lines = [
            f"  [attachments] {len(snapshot.items)} 个"
            f" · generation {snapshot.generation}"
            f" · {snapshot.total_tokens} tokens",
        ]
        for item in snapshot.items:
            lines.append(
                f"  {item.id} · {item.kind.value} · revision {item.revision}"
                f" · {item.layer} · {item.tokens} tokens · dynamic")
        diff = getattr(ctx.loop, "last_attachment_diff", None)
        if diff is not None and diff.changed:
            lines.extend(f"  {line}" for line in diff.summary_lines())
        if not snapshot.items:
            lines.append("  （当前没有动态附件）")
        return _ok(*lines)

    if operation == "rules" and len(parts) == 1:
        loader = getattr(ctx.loop, "rule_loader", None)
        workspace = getattr(ctx.loop, "workspace_context", None)
        base = getattr(workspace, "rule_bundle", None)
        layers = list(getattr(workspace, "rule_layers", ()) or ())
        if not layers and loader is not None:
            stack = loader.load_stack(include_directory=False)
            layers = list(stack.base_layers)
            base = stack.base_bundle
        if base is None and loader is not None:
            base = loader.load_base()
        directory_source = getattr(ctx.loop, "directory_rule_source", None)
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
            lines.append(
                f"  [{metadata.get('rule_precedence', '?'):>2}] "
                f"{fragment.id} · sources="
                f"{', '.join(metadata.get('rule_entry_sources', ())) or '(none)'}"
                f" · imports={metadata.get('rule_import_count', 0)}"
                f" · issues={len(metadata.get('rule_issues', ()))}")
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
        assembler = getattr(ctx.loop, "context_assembler", None)
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
        enabled_count = sum(1 for item in fragments if registry.is_enabled(item))
        label = f"source={source}" if source else "all sources"
        lines = [
            f"  [fragments] {enabled_count}/{len(fragments)} enabled"
            f" · {label} · generation {registry.generation}",
        ]
        for item in fragments:
            state = "on " if registry.is_enabled(item) else "off"
            cache = "cache" if item.cacheable else "dynamic"
            toggle = "" if item.toggleable else " · locked"
            lines.append(
                f"  [{state}] {item.id} · {item.source} · {item.group}"
                f" · {item.tokens} tokens · {cache}{toggle} · {item.title}")
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
        snapshot = refresh_registry_snapshot(ctx.loop)
        state = "enabled" if enabled else "disabled"
        lines = [f"  [fragments] {state} {kind}: {target} ({count})"]
        if snapshot is not None:
            lines.extend(f"  {line}" for line in snapshot.summary_lines())
        return _ok(*lines)

    if operation == "reset" and len(parts) == 1:
        if registry is None:
            return _ok("  （Prompt Fragment Registry 未启用）")
        registry.reset_overrides()
        snapshot = refresh_registry_snapshot(ctx.loop)
        lines = ["  [fragments] 已清除全部手动启停设置"]
        if snapshot is not None:
            lines.extend(f"  {line}" for line in snapshot.summary_lines())
        return _ok(*lines)

    if operation == "cache" and len(parts) == 1:
        lines = ContextInspectionService(ctx.loop).cache_lines()
        context_plan = getattr(ctx.loop, "last_context_plan", None)
        plan = getattr(ctx.loop, "last_prompt_plan", None)
        if plan is not None and context_plan is None:
            cache = plan.cache
            store = getattr(ctx.loop, "context_attachments", None)
            attachment_generation = (
                store.snapshot().generation
                if store is not None else 0
            )
            lines[0:0] = [
                f"  [cache] prompt stable prefix {cache.offset} chars"
                f" · {cache.stable_tokens} tokens"
                f" · generation {plan.generation}"
                f" · attachment generation {attachment_generation}",
                f"  stable fragments: {', '.join(cache.stable_fragments) or '(none)'}",
                f"  volatile fragments: {', '.join(cache.volatile_fragments) or '(none)'}",
            ]
        elif registry is not None:
            snapshot = registry.snapshot()
            cache = snapshot.cache
            lines[0:0] = [
                f"  [cache] prompt stable prefix {cache.offset} chars"
                f" · {cache.stable_tokens} tokens"
                f" · generation {snapshot.generation}",
                f"  stable fragments: {', '.join(cache.stable_fragments) or '(none)'}",
                f"  volatile fragments: {', '.join(cache.volatile_fragments) or '(none)'}",
            ]
        elif not lines:
            lines = ["  （Prompt Fragment Registry 与请求缓存均未启用）"]
        return _ok(*lines)

    if operation == "authoring" and len(parts) == 1:
        fragment = registry.get(PROMPT_AUTHORING_FRAGMENT_ID) if registry else None
        configured = bool(getattr(ctx.loop, "prompt_authoring_enabled", False))
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

    snapshot = getattr(ctx.loop, "last_context_snapshot", None)
    if snapshot is not None:
        budget = (
            f"{snapshot.budget_tokens} tokens"
            if snapshot.budget_tokens else "未启用压缩")
        window = (
            f"{snapshot.model_context_window} tokens"
            if snapshot.model_context_window else "未知")
        reserve = (
            f"{snapshot.output_reserve_tokens} tokens"
            if snapshot.model_context_window else "未知")
        ratio = (
            f"{snapshot.usage_ratio:.1%}"
            if snapshot.usage_ratio is not None else "未知")
        sources = " · ".join(
            f"{name} {tokens}"
            for name, tokens in sorted(
                snapshot.source_tokens.items(),
                key=lambda item: (-item[1], item[0]))[:8]
        ) or "（空）"
        lines = [
            f"  [context] 最近组装请求 · {snapshot.turn_id or '(unknown turn)'}"
            f" · Step {snapshot.step}",
            f"  输入估算: {snapshot.estimated_input_tokens} tokens"
            f" · 压缩预算: {budget} · 使用率: {ratio}",
            f"  请求: {snapshot.provider or '(unknown)'}"
            f" · wire {snapshot.wire_fidelity}"
            f" · body {snapshot.request_body_bytes} bytes"
            f" / {snapshot.request_body_tokens} tokens"
            f" · {'fits' if snapshot.input_fits else 'over budget'}",
            f"  模型窗口: {window} · 输出/安全保留: {reserve}"
            f" · provider 输出上限: {snapshot.output_limit_tokens or '未知'}",
            f"  分项: system {snapshot.system_tokens}"
            f" · workspace {snapshot.workspace_tokens}"
            f" · memory {snapshot.memory_tokens}"
            f" · history {snapshot.history_tokens}"
            f" · runtime {snapshot.runtime_tokens}"
            f" · tool schema {snapshot.tool_schema_tokens}"
            f" · overhead {snapshot.overhead_tokens}",
            f"  缓存: {snapshot.cache_mode}"
            f" · requested {snapshot.cache_requested_mode or 'unknown'}"
            f" · {snapshot.cache_negotiation_reason or 'unavailable'}"
            f" · stable blocks {snapshot.stable_system_block_count}"
            f" · dynamic blocks {snapshot.dynamic_system_block_count}"
            f" · stable {snapshot.stable_tokens}"
            f" · volatile {snapshot.volatile_tokens}"
            f" · boundary {snapshot.cache_boundary_chars} chars"
            f" · segments {snapshot.cache_segment_count}",
            f"  cache capabilities: "
            f"{', '.join(snapshot.cache_supported_modes) or '(unknown)'}"
            f" · max breakpoints {snapshot.cache_max_breakpoints}"
            f" · fallback {'yes' if snapshot.cache_fallback else 'no'}",
            f"  breakpoint planner: {snapshot.cache_breakpoint_strategy or '未启用'}"
            f" · selected {snapshot.cache_breakpoint_count}"
            f"/{snapshot.cache_max_breakpoints}"
            f" · skipped {snapshot.cache_breakpoint_skipped_count}"
            f" · minimum "
            f"{snapshot.cache_breakpoint_plan.get('minimum_cacheable_tokens', 0)}"
            f" tokens",
            f"  stable hash: {snapshot.stable_hash or '(none)'}",
            f"  dynamic hash: {snapshot.dynamic_hash or '(none)'}",
            f"  evidence channel: {snapshot.evidence_block_count} blocks"
            f" · hash {snapshot.evidence_hash or '(none)'}",
            f"  cache_control boundary: {snapshot.cache_control_boundary} chars",
            f"  full hash: {snapshot.full_hash or '(none)'}",
            f"  工具 {snapshot.tool_count} 个 · fragment {snapshot.fragment_count} 个"
            f" · source {snapshot.source_count} 个",
            f"  来源: {sources}",
        ]
        tool_messages = sum(
            1 for message in ctx.loop.history if message.role == "tool")
        lines.append(
            f"  当前历史 {len(ctx.loop.history)} 条消息"
            f"（其中工具结果 {tool_messages} 条）")
        working_store = getattr(ctx.loop, "working_context_store", None)
        if working_store is not None:
            working = working_store.snapshot()
            lines.append(
                f"  Working Context: {working.count} 条 · {working.token_count} tokens"
                f" · {'已裁剪' if working.truncated else '完整'}")
        micro = getattr(ctx.loop, "last_microcompact_report", None)
        if micro is not None:
            lines.append(
                f"  Tool Microcompact: {micro.compacted_count} 条"
                f" · {micro.before_tokens} → {micro.after_tokens} tokens"
                f" · summary {micro.summarized_count}"
                f" · reference {micro.referenced_count}")
        if snapshot.dropped_items:
            lines.append("  已裁剪: " + ", ".join(snapshot.dropped_items))
        bundle = getattr(ctx.loop, "workspace_context", None)
        if bundle is not None and bundle.omitted:
            lines.append("  工作区未注入: " + ", ".join(bundle.omitted))
        inspection = ContextInspectionService(ctx.loop)
        memory_ids, memory_scopes, memory_tokens = inspection.memory_metadata()
        scopes_text = " · ".join(
            f"{scope} {count}" for scope, count in sorted(memory_scopes.items()))
        lines.append(
            f"  Memory 命中: {len(memory_ids)} 条"
            f" · {memory_tokens or snapshot.memory_tokens} tokens"
            f" · {scopes_text or '(none)'}")
        lines.append(f"  cache hash changed: {inspection.cache_change_text()}")
        return _ok(*lines)

    total = estimate_history_tokens(ctx.loop.history)
    budget = (ctx.loop.context_manager.max_tokens
              if ctx.loop.context_manager else "未启用压缩")
    tool_messages = sum(1 for message in ctx.loop.history if message.role == "tool")
    lines = [
        f"  [context] 历史 {len(ctx.loop.history)} 条消息"
        f"（含 {tool_messages} 条工具结果） · 估算 {total} tokens"
        f" · 预算 {budget}",
    ]
    bundle = getattr(ctx.loop, "workspace_context", None)
    if bundle is not None:
        lines.extend(f"  {line}" for line in bundle.summary_lines())
    runtime_context = getattr(ctx.loop, "runtime_context", ())
    if runtime_context:
        lines.append(f"  运行时 Hook 上下文: {len(runtime_context)} 段")
    working_store = getattr(ctx.loop, "working_context_store", None)
    if working_store is not None:
        working = working_store.snapshot()
        lines.append(
            f"  Working Context: {working.count} 条"
            f" · {working.token_count} tokens"
            f" · {'已裁剪' if working.truncated else '完整'}")
    micro = getattr(ctx.loop, "last_microcompact_report", None)
    if micro is not None:
        lines.append(
            f"  Tool Microcompact: {micro.compacted_count} 条"
            f" · saved {micro.saved_tokens} tokens")
    manager = getattr(ctx.loop, "context_manager", None)
    summary = getattr(manager, "last_summary", None)
    if summary is not None:
        lines.append(
            f"  Structured Compact: v{summary.version}"
            f" · generation {summary.generation}"
            f" · source {summary.source}"
            f" · {summary.token_count} tokens"
            f" · {len(summary.files)} files"
            f" · {len(summary.tests)} tests"
            f" · digest {summary.digest}")
    checkpoint = getattr(ctx.loop, "last_compact_checkpoint", None)
    if checkpoint is not None:
        lines.append(
            f"  Compact Checkpoint: {checkpoint.id}"
            f" · {checkpoint.before_tokens} → {checkpoint.after_tokens} tokens")
    if registry is not None:
        lines.extend(f"  {line}" for line in registry.snapshot().summary_lines())
    return _ok(*lines)


async def handle_compact(ctx: CommandContext) -> CommandResult:
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
            current_turn_id=getattr(ctx.loop, "_current_turn_id", ""))
        return _ok(*[
            f"  {line}" for line in preview.render().splitlines()
        ])

    before = estimate_history_tokens(ctx.loop.history)
    done = await ctx.loop.compress_context(force=True, focus=focus)
    after = estimate_history_tokens(ctx.loop.history)
    preview = getattr(manager, "last_compact_preview", None)
    details = (
        preview.render(include_messages=False).splitlines()
        if preview is not None else []
    )
    if done:
        lines = [f"  [compact] 已压缩: {before} → {after} tokens"
                 + (f" · focus={focus[:80]}" if focus else "")]
        checkpoint = getattr(ctx.loop, "last_compact_checkpoint", None)
        if checkpoint is not None:
            lines.append(f"  checkpoint: {checkpoint.id}")
    else:
        lines = [f"  [compact] 未压缩（当前约 {before} tokens）"]
    lines.extend(f"  {line}" for line in details)
    return _ok(*lines)


