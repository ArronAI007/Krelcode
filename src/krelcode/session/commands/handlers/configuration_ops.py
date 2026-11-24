"""Undo, LSP, web, output style, model, and permission commands."""

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


async def handle_undo(ctx: CommandContext) -> CommandResult:
    if ctx.checkpoints is None:
        return _ok("  （检查点未启用：--no-checkpoint）")
    if ctx.request.arguments.strip() == "list":
        lines = ctx.checkpoints.list_groups()
        return _ok("\n".join(f"  {line}" for line in lines)
                   if lines else "  （无可回滚的检查点）")
    result = ctx.checkpoints.undo_last()
    if result is None:
        return _ok("  （无可回滚的检查点）")
    details = []
    if result.restored:
        details.append(f"还原 {len(result.restored)} 个: {', '.join(result.restored)}")
    if result.deleted:
        details.append(f"删除新建 {len(result.deleted)} 个: {', '.join(result.deleted)}")
    label = f"（{result.label}）" if result.label else ""
    detail = "；".join(details) if details else "无文件变化"
    return _ok(f"  [undo] 已回滚上一轮{label}: {detail}")


async def handle_lsp(ctx: CommandContext) -> CommandResult:
    manager = ctx.loop.services.extensions.lsp
    if manager is None:
        return _ok("  （LSP 不可用）")
    return _ok(*manager.status_lines())


async def handle_web(ctx: CommandContext) -> CommandResult:
    runtime = ctx.loop.services.extensions.web
    options = runtime.options if runtime is not None else None
    browser = ctx.loop.services.extensions.browser
    browser_options = browser.options if browser is not None else None
    if options is None or not options.enabled:
        browser_line = (
            "  Browser: disabled"
            if (
                browser is None
                or browser_options is None
                or not browser_options.enabled
            )
            else f"  Browser: enabled · backend={browser.backend_name}"
        )
        return _ok(
            "  WebFetch/WebSearch 已禁用（--no-web 或 [web].enabled=false）",
            browser_line,
        )
    key_env = options.search_api_key_env
    configured = bool(os.environ.get(key_env)) if key_env else False
    if (
        browser is None
        or browser_options is None
        or not browser_options.enabled
    ):
        browser_line = "  Browser: disabled"
    else:
        capabilities = ", ".join(sorted(browser.capabilities)) or "none"
        browser_line = (
            f"  Browser: enabled · backend={browser.backend_name}"
            f" · max_sessions={browser_options.max_sessions}"
            f" · capabilities={capabilities}"
        )
    return _ok(
        f"  WebFetch: enabled · timeout {options.timeout:g}s"
        f" · max {options.max_bytes} bytes · private={'allow' if options.allow_private else 'deny'}",
        f"  允许端口: {', '.join(str(port) for port in options.allowed_ports)}",
        f"  WebSearch: {'enabled' if options.search_enabled else 'disabled'}"
        f" · provider={options.search_provider}"
        f" · key_env={key_env or '（无）'}"
        f" · configured={'yes' if configured else 'no'}",
        browser_line,
    )


async def handle_output_style(ctx: CommandContext) -> CommandResult:
    manager = ctx.loop.services.extensions.output_styles
    if manager is None:
        return _ok("  （Output Styles 不可用）")
    registry = ctx.loop.services.prompt.registry
    if registry is None:
        return _ok("  （Prompt Registry 未启用）")
    parts = ctx.request.arguments.split(maxsplit=1)
    operation = parts[0].strip().lower() if parts else ""
    argument = parts[1].strip() if len(parts) > 1 else ""
    if not operation:
        return _ok(*manager.status_lines())
    if operation == "list":
        return _ok(*manager.status_lines())
    if operation == "reload":
        manager.reload()
        manager.apply_to_registry(registry)
        refresh_registry_snapshot(ctx.loop)
        return _ok("  Output Styles 已重载", *manager.status_lines())
    if operation in {"show", "info"}:
        if not argument:
            return _ok("  用法: /output-style show <name>")
        style = manager.get(argument)
        if style is None:
            return _ok(f"  未找到 Output Style: {argument}")
        body = style.content.rstrip() or "（无额外人格 Prompt；使用 Krelcode 默认行为）"
        return _ok(
            f"  {style.name} [{style.scope}] {style.description}".rstrip(),
            f"  来源: {style.path or style.source}",
            f"  保留编码指令: {'yes' if style.keep_coding_instructions else 'no'}",
            body)
    if operation == "default":
        manager.select("default")
        manager.apply_to_registry(registry)
        refresh_registry_snapshot(ctx.loop)
        return _ok("  已恢复默认输出样式")
    if operation in {"off", "none"}:
        manager.select("off")
        manager.apply_to_registry(registry)
        refresh_registry_snapshot(ctx.loop)
        return _ok("  已恢复默认输出样式")
    style = manager.select(operation)
    if style is None:
        return _ok(
            f"  未找到 Output Style: {operation}",
            "  可用样式: " + (", ".join(item.name for item in manager.styles)
                              if manager.styles else "（无）"))
    manager.apply_to_registry(registry)
    refresh_registry_snapshot(ctx.loop)
    return _ok(
        f"  已切换 Output Style: {style.name} [{style.scope}]",
        f"  来源: {style.path or style.source}")


async def handle_model(ctx: CommandContext) -> CommandResult:
    if ctx.model_switch is None:
        return _ok("  （模型切换不可用）")
    target = ctx.request.arguments.strip() or None
    return _ok(f"  [model] {ctx.model_switch(target)}")


async def handle_allow(ctx: CommandContext) -> CommandResult:
    rules = getattr(ctx.loop.policy, "rules", None)
    if rules is None:
        return _ok("  （安全策略未启用（--no-sandbox），无审批白名单）")
    parts = ctx.request.arguments.split()
    if not parts:
        programs = rules.as_list()
        commands = rules.as_commands()
        entries = [f"程序:{item}" for item in programs]
        entries.extend(f"命令:{item}" for item in commands)
        return _ok(
            "  已允许: " + (", ".join(entries) if entries else "（空）"),
            f"  规则文件: {rules.path or '（仅内存）'}",
        )
    if len(parts) == 2 and parts[0] == "add":
        added = rules.add_program(parts[1])
        return _ok(f"  [allow] {'已添加' if added else '已存在，无需重复添加'}: "
                   f"{parts[1]}")
    if len(parts) == 2 and parts[0] == "remove":
        removed = rules.remove_program(parts[1])
        return _ok(f"  [allow] {'已移除' if removed else '规则不存在'}: {parts[1]}")
    return _ok("  用法: /allow | /allow add <程序名> | /allow remove <程序名>")


async def handle_permissions(ctx: CommandContext) -> CommandResult:
    """运行时权限模式与 session/workspace 规则管理。"""
    manager = ctx.permission_manager
    rules = getattr(ctx.loop.policy, "rules", None)
    if manager is None and rules is None:
        return _ok("  （权限管理不可用）")

    try:
        parts = shlex.split(ctx.request.arguments)
    except ValueError as exc:
        return _ok(f"  [permissions] 参数解析失败: {exc}")

    if not parts:
        lines = ["  [permissions] 当前权限模式与规则"]
        if manager is not None:
            lines.extend(manager.mode_lines())
        if rules is None:
            lines.append("  规则仓库: 不可用（--no-sandbox）")
        else:
            lines.append(f"  规则文件: {rules.path or '（仅内存）'}")
            rule_lines = [rules.format_rule(rule) for rule in rules.as_rules()]
            lines.extend(f"  {line}" for line in rule_lines)
            legacy_programs = rules.as_list()
            legacy_commands = rules.as_commands()
            if legacy_programs:
                lines.append(
                    "  旧版程序 allow: " + ", ".join(legacy_programs))
            if legacy_commands:
                lines.append(
                    "  旧版精确命令 allow: " + ", ".join(legacy_commands))
            if not rule_lines and not legacy_programs and not legacy_commands:
                lines.append("  规则: （空，沿用内置风险分级）")
            lines.append("  优先级: deny > ask > allow；受限模式不能通过规则越权")
        lines.append(
            "  用法: /permissions mode <default|plan|read-only|auto-approve>")
        lines.append(
            "        /permissions add <allow|ask|deny> <session|workspace> "
            "<path|command|command_program|tool> <pattern>")
        lines.append("        /permissions remove <rule-id>")
        return _ok(*lines)

    operation = parts[0].lower()
    if operation == "mode" and len(parts) == 2:
        if manager is None:
            return _ok("  （权限模式管理不可用）")
        try:
            mode = parse_permission_mode(parts[1])
        except ValueError:
            return _ok("  [permissions] mode 必须是 default / plan / read-only / auto-approve")
        previous = manager.mode
        manager.set_mode(mode)
        if ctx.plan_manager is not None:
            ctx.plan_manager.on_mode_change(previous, manager.mode)
        return _ok(
            f"  [permissions] 模式: {previous.value} → {manager.mode.value}",
            f"  {manager.status_line()}",
        )

    if operation in {"allow", "ask", "deny"} and len(parts) == 4:
        parts = ["add", operation, *parts[1:]]
        operation = "add"
    if operation == "add" and len(parts) == 5:
        if rules is None:
            return _ok("  （安全策略未启用，无法管理规则）")
        decision, scope, kind, pattern = parts[1:]
        try:
            rule, added = rules.add_rule(
                RuleDecision(decision), RuleScope(scope), kind, pattern)
        except ValueError as exc:
            return _ok(f"  [permissions] 添加失败: {exc}")
        return _ok(
            f"  [permissions] {'已添加' if added else '规则已存在'}: "
            f"{rules.format_rule(rule)}",
            f"  作用域: {rule.scope.value}"
            + ("（仅本次会话）" if rule.scope is RuleScope.SESSION else "（已持久化）"),
        )

    if operation == "remove" and len(parts) == 2:
        if rules is None:
            return _ok("  （安全策略未启用，无法管理规则）")
        removed = rules.remove_rule(parts[1])
        return _ok(
            f"  [permissions] {'已移除' if removed else '规则不存在'}: {parts[1]}")

    return _ok(
        "  用法: /permissions | /permissions mode <mode> | "
        "/permissions add <decision> <scope> <kind> <pattern> | "
        "/permissions remove <rule-id>"
    )


async def handle_cost(ctx: CommandContext) -> CommandResult:
    return _ok(f"  [cost] {ctx.usage.summary()}" if ctx.usage
               else "  （用量统计不可用）")
