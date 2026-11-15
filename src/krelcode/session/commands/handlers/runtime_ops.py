"""执行环境、上下文、Skill 和模型类内置命令 handlers。"""

from __future__ import annotations

import html
import shlex

from ....agent.context import build_workspace_context_bundle
from ....agent.prompts import build_system_prompt
from ....agent.tokens import estimate_history_tokens
from ..context import CommandContext
from ..sources import sync_mcp_prompt_commands, sync_skill_commands
from ..types import CommandResult


def _ok(*output: str) -> CommandResult:
    return CommandResult(handled=True, output=tuple(output))


async def handle_tools(ctx: CommandContext) -> CommandResult:
    output = []
    for name in ctx.loop.registry.names():
        tool = ctx.loop.registry.get(name)
        output.append(f"  {name}: {tool.description}")
    return _ok(*output)


async def handle_mcp(ctx: CommandContext) -> CommandResult:
    if ctx.mcp_supervisor is None:
        return _ok("  （MCP 未启用）")
    output: list[str] = []
    if ctx.request.arguments.strip() == "reload":
        restarted = await ctx.mcp_reload() if ctx.mcp_reload else []
        note = f"，重启: {', '.join(restarted)}" if restarted else ""
        output.append(f"  [mcp] 配置已同步{note}")
        sync_mcp_prompt_commands(ctx.command_registry, ctx.mcp_supervisor)
    status = ctx.mcp_supervisor.status()
    for item in status:
        state = "在线" if item["alive"] else "已断开"
        output.append(
            f"  {item['name']}（{item['server']}，{item['transport']}）"
            f"{state}，{item['tools']} 个工具，{item.get('prompts', 0)} 个 Prompt")
    if not status:
        output.append("  （没有配置 MCP 服务器；在 krelcode.toml 的 "
                      "[mcp_servers.*] 里声明）")
    return _ok(*output)


async def handle_agents(ctx: CommandContext) -> CommandResult:
    if ctx.subagents is None:
        return _ok("  （子代理未启用）")
    lines = ctx.subagents.status_lines()
    return _ok("\n".join(f"  {line}" for line in lines)
               if lines else "  （暂无子代理）")


def _skills_disabled(ctx: CommandContext) -> bool:
    return ctx.skills is None or not getattr(ctx.skills, "enabled", False)


def _disabled_message() -> str:
    return ("  （Skills 未启用：移除 --no-skills 或在配置中设置 "
            "skills.enabled=true）")


async def handle_skills(ctx: CommandContext) -> CommandResult:
    if _skills_disabled(ctx):
        return _ok(_disabled_message())
    output: list[str] = []
    parts = ctx.request.arguments.split(maxsplit=1)
    sub = parts[0].strip() if parts else ""
    if sub == "reload":
        report = (ctx.skills_reload() if ctx.skills_reload else ctx.skills.reload())
        sync_skill_commands(ctx.command_registry, ctx.skills)
        output.append(f"  [skills] 已重载 {report.count} 个 Skill")
        for issue in report.issues:
            output.append(f"  [skills] 跳过无效 Skill: {issue.path}: {issue.message}")
    elif sub == "show":
        if len(parts) < 2 or not parts[1].strip():
            output.append("  用法: /skills show <name>")
        else:
            output.append(_format_skill_details(ctx.skills, parts[1].strip()))
    else:
        query = sub
        items = ctx.skills.list(query=query)
        if not items:
            output.append("  （没有匹配的 Skill）" if query else "  （未发现 Skill）")
        for skill in items:
            flags = []
            if not skill.user_invocable:
                flags.append("仅模型")
            if skill.disable_model_invocation:
                flags.append("仅用户")
            flag_text = f" [{', '.join(flags)}]" if flags else ""
            hint = f" · {skill.argument_hint}" if skill.argument_hint else ""
            output.append(f"  {skill.name}: {skill.description}{hint}{flag_text}")
        if items:
            output.append("  使用 /skills show <name> 查看详情，"
                          "/skill <name> [参数] 调用")
        for issue in ctx.skills.issues:
            output.append(
                f"  [skills] 跳过无效 Skill: {issue.path}: {issue.message}")
    return _ok(*output)


async def handle_skill(ctx: CommandContext) -> CommandResult:
    if _skills_disabled(ctx):
        return _ok(_disabled_message())
    parts = ctx.request.arguments.split(maxsplit=1)
    if not parts or not parts[0].strip():
        return _ok("  用法: /skill <name> [参数]")
    name = parts[0].strip()
    arguments = parts[1] if len(parts) > 1 else ""
    try:
        prompt = ctx.skills.user_prompt(name, arguments)
        skill = ctx.skills.get(name)
    except Exception as exc:
        return _ok(f"  [skill] {exc}")
    return CommandResult(
        handled=True,
        output=(f"  [skill] 已加载并执行: {name}",),
        run_prompt=prompt,
        tool_allowlist=tuple(skill.allowed_tools) if skill else None,
    )


async def handle_skill_command(ctx: CommandContext) -> CommandResult:
    """执行由 Skill 自动注册的 `/skill-name [args]`。"""
    if _skills_disabled(ctx):
        return _ok(_disabled_message())
    name = ctx.request.name
    try:
        prompt = ctx.skills.user_prompt(name, ctx.request.arguments)
        skill = ctx.skills.get(name)
    except Exception as exc:
        return _ok(f"  [skill] {exc}")
    return CommandResult(
        handled=True,
        output=(f"  [skill] 已加载并执行: {name}",),
        run_prompt=prompt,
        tool_allowlist=tuple(skill.allowed_tools) if skill else None,
    )


def _render_mcp_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
        elif block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            parts.append(
                f"[{block.get('type', 'unknown')} 内容块，暂不支持渲染]")
    return "\n".join(parts)


def _parse_prompt_arguments(names: tuple[str, ...], raw: str) -> dict[str, str]:
    raw = raw.strip()
    if not names or not raw:
        return {}
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"参数解析失败: {exc}") from exc
    if tokens and all("=" in token for token in tokens):
        values: dict[str, str] = {}
        for token in tokens:
            key, value = token.split("=", 1)
            if key not in names:
                raise ValueError(f"未知参数: {key}")
            values[key] = value
        return values
    if len(names) == 1:
        return {names[0]: raw}
    if len(tokens) == len(names):
        return dict(zip(names, tokens))
    usage = " ".join(f"{name}=<value>" for name in names)
    raise ValueError(f"参数数量不匹配，用法: {usage}")


async def handle_mcp_prompt_command(ctx: CommandContext) -> CommandResult:
    spec = ctx.command_registry.resolve(ctx.request.name)
    if spec is None or ctx.mcp_supervisor is None:
        return _ok(f"  （MCP Prompt 不可用: /{ctx.request.name}）")
    server = str(spec.metadata.get("server", ""))
    prompt_name = str(spec.metadata.get("prompt_name", ""))
    names = tuple(spec.metadata.get("argument_names") or ())
    try:
        arguments = _parse_prompt_arguments(names, ctx.request.arguments)
    except ValueError as exc:
        return _ok(f"  [mcp] {exc}")
    try:
        result = await ctx.mcp_supervisor.get_prompt(
            server, prompt_name, arguments)
    except Exception as exc:
        return _ok(f"  [mcp] Prompt 获取失败: {type(exc).__name__}: {exc}")
    lines: list[str] = []
    if result.get("description"):
        lines.append(f"# {result['description']}")
    messages = result.get("messages")
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "?")
        text = _render_mcp_content(message.get("content"))
        lines.append(f"[{role}] {text}")
    body = "\n".join(lines) if lines else "（空模板）"
    wrapped = (
        f'<mcp_prompt server="{html.escape(server, quote=True)}" '
        f'name="{html.escape(prompt_name, quote=True)}">\n'
        f"{body}\n</mcp_prompt>"
    )
    return CommandResult(
        handled=True,
        output=(f"  [mcp] 已加载并执行: {server}/{prompt_name}",),
        run_prompt=wrapped,
    )


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


async def handle_model(ctx: CommandContext) -> CommandResult:
    if ctx.model_switch is None:
        return _ok("  （模型切换不可用）")
    target = ctx.request.arguments or None
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


async def handle_cost(ctx: CommandContext) -> CommandResult:
    return _ok(f"  [cost] {ctx.usage.summary()}" if ctx.usage
               else "  （用量统计不可用）")


async def handle_context(ctx: CommandContext) -> CommandResult:
    if ctx.request.arguments.strip() == "refresh":
        options = getattr(ctx.loop, "context_options", None)
        bundle = await build_workspace_context_bundle(
            ctx.workspace_root, options=options)
        if ctx.loop.history and ctx.loop.history[0].role == "system":
            base_prompt = build_system_prompt(bundle.text)
            ctx.loop.skill_base_prompt = base_prompt
            ctx.loop.history[0].content = (
                ctx.skills.apply_to_prompt(base_prompt)
                if ctx.skills else base_prompt)
            ctx.loop.workspace_context = bundle
            details = bundle.summary_lines()
            return _ok(
                "  [context] 工作区上下文已刷新",
                *[f"  {line}" for line in details],
            )
        return _ok("  [context] 历史中无 system 消息，未刷新")
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
    return _ok(*lines)


async def handle_compact(ctx: CommandContext) -> CommandResult:
    if ctx.loop.context_manager is None:
        return _ok("  （上下文压缩未启用：--no-compress）")
    before = estimate_history_tokens(ctx.loop.history)
    done = await ctx.loop.compress_context(force=True)
    after = estimate_history_tokens(ctx.loop.history)
    if done:
        return _ok(f"  [compact] 已压缩: {before} → {after} tokens")
    return _ok(f"  [compact] 历史太短，无需压缩（当前约 {before} tokens）")


def _format_skill_details(skills, name: str) -> str:
    """格式化 Skill 元数据、资源和完整说明。"""
    try:
        skill = skills.get(name)
        if skill is None:
            raise ValueError(f"未知 Skill: {name}")
        lines = [
            f"  [skill] {skill.name}",
            f"  描述: {skill.description}",
            f"  来源: {skill.source}",
            f"  位置: {skill.location}",
        ]
        if skill.argument_hint:
            lines.append(f"  参数: {skill.argument_hint}")
        if skill.allowed_tools:
            lines.append(f"  建议工具: {', '.join(skill.allowed_tools)}")
        if skill.version:
            lines.append(f"  版本: {skill.version}")
        if skill.license:
            lines.append(f"  许可证: {skill.license}")
        resources = skills.list_resources(name)
        if resources:
            lines.append("  资源:")
            for resource in resources[:50]:
                lines.append(
                    f"    {resource.kind:4s} {resource.size:>8d}  {resource.path}")
            if len(resources) > 50:
                lines.append(f"    … 另有 {len(resources) - 50} 个资源")
        lines.append("\n" + skills.load(name, for_model=False))
        return "\n".join(lines)
    except Exception as exc:
        return f"  [skill] {exc}"
