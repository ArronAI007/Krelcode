"""执行环境、上下文、Skill 和模型类内置命令 handlers。"""

from __future__ import annotations

import html
import json
import os
import shlex

from ....agent.context import build_workspace_context_bundle
from ....agent.context_inspection import ContextInspectionService
from ....agent.fragments import (
    GROUP_WORKSPACE,
    refresh_registry_snapshot,
    skills_fragment,
    workspace_fragments,
)
from ....agent.prompts import build_system_prompt
from ....agent.tokens import estimate_history_tokens
from ....core.messages import Message
from ....safety.runtime import parse_permission_mode
from ....safety.rules import RuleDecision, RuleScope
from ..context import CommandContext
from ..sources import (
    sync_mcp_prompt_commands,
    sync_plugin_commands,
    sync_skill_commands,
)
from ..types import CommandResult


def _ok(*output: str) -> CommandResult:
    return CommandResult(handled=True, output=tuple(output))


async def handle_tools(ctx: CommandContext) -> CommandResult:
    pool = ctx.loop.current_tool_pool()
    output = ["  Tool catalogue（本轮权限/运行模式/MCP/功能开关过滤后）:"]
    for entry in pool.catalog_entries():
        tier = entry.descriptor.tier.value
        if entry.descriptor.tier.value == "core":
            state = "core"
        elif pool.is_loaded(entry.name):
            state = f"loaded:{tier}"
        else:
            state = tier
        category = "read-only" if entry.descriptor.read_only else "mutating"
        output.append(
            f"  [{state}] {entry.name}: {entry.tool.description} "
            f"({category}, source={entry.descriptor.source.value})")
    excluded = pool.excluded_items()
    if excluded:
        output.append("  Excluded:")
        output.extend(f"  [excluded] {name}: {reason}" for name, reason in excluded)
    if "tool_search" in pool.names():
        output.append(
            "  Use [tool_search(query=..., load=true)] to load discoverable "
            "or deferred schemas.")
    if not pool.catalog_entries() and not excluded:
        output.append("  （没有工具通过当前运行时过滤）")
    return _ok(*output)


async def handle_mcp(ctx: CommandContext) -> CommandResult:
    if ctx.mcp_supervisor is None:
        return _ok("  （MCP 未启用）")
    output: list[str] = []
    arguments = ctx.request.arguments.strip()
    if arguments == "reload":
        restarted = await ctx.mcp_reload() if ctx.mcp_reload else []
        note = f"，重启: {', '.join(restarted)}" if restarted else ""
        output.append(f"  [mcp] 配置已同步{note}")
        sync_mcp_prompt_commands(ctx.command_registry, ctx.mcp_supervisor)
    elif arguments.startswith("auth "):
        name = arguments.partition(" ")[2].strip()
        if not name:
            return _ok("  用法: /mcp auth <server>")
        try:
            await ctx.mcp_supervisor.authorize(name)
        except Exception as exc:
            return _ok(
                f"  [mcp:oauth] {name} 授权失败: "
                f"{type(exc).__name__}: {exc}")
        sync_mcp_prompt_commands(ctx.command_registry, ctx.mcp_supervisor)
        output.append(f"  [mcp:oauth] {name} 授权成功，服务器已刷新")
    elif arguments and arguments not in {"status", "list"}:
        return _ok("  用法: /mcp [reload|auth <server>]")
    status = ctx.mcp_supervisor.status()
    for item in status:
        state = "在线" if item["alive"] else "已断开"
        oauth = " · OAuth" if item.get("oauth") else ""
        output.append(
            f"  {item['name']}（{item['server']}，{item['transport']}）"
            f"{state}，{item['tools']} 个工具，{item.get('prompts', 0)} 个 Prompt，"
            f"{item.get('resources', 0)} 个 Resource，"
            f"{item.get('resource_templates', 0)} 个 Template{oauth}")
    if not status:
        output.append("  （没有配置 MCP 服务器；在 krelcode.toml 的 "
                      "[mcp_servers.*] 里声明）")
    return _ok(*output)


async def handle_agents(ctx: CommandContext) -> CommandResult:
    manager = ctx.subagents
    if manager is None:
        return _ok("  （子代理未启用）")
    raw = ctx.request.arguments.strip()
    try:
        parts = shlex.split(raw) if raw else []
    except ValueError as exc:
        return _ok(f"  [agents] 参数解析失败: {exc}")
    operation = parts[0].lower() if parts else "list"

    if operation == "list":
        lines = manager.status_lines()
        if not lines:
            return _ok("  （暂无子代理）",
                       "  用法: /agents feed <id> [after] | pause/resume/kill <id>",
                       "  /agents definitions | sidechains | sidechain <id>")
        return _ok("\n".join(f"  {line}" for line in lines))

    if operation == "definitions":
        lines = manager.definition_lines()
        return _ok(*[f"  {line}" for line in lines]) if lines else _ok(
            "  （未发现 Agent Definition）",
            "  创建 ~/.krelcode/agents/<name>.md 或 .krelcode/agents/<name>.md")

    if operation == "feed":
        if len(parts) < 2:
            return _ok("  用法: /agents feed <id> [after-sequence] [--limit N]")
        after = 0
        limit = 200
        try:
            index = 2
            if index < len(parts) and not parts[index].startswith("--"):
                after = max(0, int(parts[index]))
                index += 1
            if index < len(parts) and parts[index] == "--limit":
                limit = max(1, min(int(parts[index + 1]), 1000))
            elif index < len(parts):
                raise ValueError("多余参数")
            page = manager.feed(parts[1], after_sequence=after, limit=limit)
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            return _ok(f"  [agents] {exc}")
        return _ok("\n".join(
            f"  {line}" for line in page.render().splitlines()))

    if operation in {"pause", "resume", "kill"}:
        if len(parts) != 2:
            return _ok(f"  用法: /agents {operation} <id>")
        try:
            handle = await getattr(manager, operation)(parts[1])
        except (KeyError, ValueError) as exc:
            return _ok(f"  [agents] {exc}")
        return _ok(f"  [agents] {handle.id} 状态: {handle.status}")

    if operation == "sidechains":
        limit = 50
        if len(parts) > 1:
            try:
                limit = max(1, min(int(parts[1]), 500))
            except ValueError:
                return _ok("  用法: /agents sidechains [limit]")
        lines = manager.sidechain_lines(limit=limit)
        return _ok(*[f"  {line}" for line in lines]) if lines else _ok(
            "  （暂无 sidechain）")

    if operation == "sidechain":
        if len(parts) != 2:
            return _ok("  用法: /agents sidechain <agent-id|run/path>")
        try:
            path, records = manager.read_sidechain(parts[1])
        except (OSError, ValueError) as exc:
            return _ok(f"  [agents] {exc}")
        output = [f"  sidechain: {path}", f"  records: {len(records)}"]
        for record in records[-200:]:
            compact = json.dumps(record, ensure_ascii=False, sort_keys=True)
            if len(compact) > 500:
                compact = compact[:500] + "…"
            record_type = str(record.get("type") or "record")
            output.append(f"  [{record_type}] {compact}")
        return _ok(*output)

    return _ok(
        "  用法: /agents list | definitions | sidechains | sidechain <id>",
        "  /agents feed <id> [after-sequence] [--limit N]",
        "  /agents pause|resume|kill <id>")


async def handle_jobs(ctx: CommandContext) -> CommandResult:
    manager = ctx.jobs
    if manager is None:
        return _ok("  （后台任务管理器未启用）")
    raw = ctx.request.arguments.strip()
    try:
        parts = shlex.split(raw) if raw else []
    except ValueError as exc:
        return _ok(f"  [jobs] 参数解析失败: {exc}")
    operation = parts[0].lower() if parts else "list"

    if operation == "list":
        jobs = manager.list_jobs()
        if not jobs:
            return _ok("  （暂无后台任务）")
        lines = [f"  后台任务（{len(jobs)}）:"]
        for job in jobs:
            exit_text = "-" if job.exit_code is None else str(job.exit_code)
            command = " ".join(job.command.split())
            if len(command) > 80:
                command = command[:79] + "…"
            lines.append(
                f"  {job.job_id}  {job.status.value:<9} exit={exit_text:<4} "
                f"pid={job.pid} seq={job.latest_sequence}  {command}")
            lines.append(f"    log: {job.log_path}")
        return _ok(*lines)

    if operation == "output":
        if len(parts) < 2:
            return _ok("  用法: /jobs output <id> [after-sequence] [--wait 秒]")
        job_id = parts[1]
        after = 0
        wait_seconds = 0.0
        index = 2
        try:
            if index < len(parts) and not parts[index].startswith("--"):
                after = int(parts[index])
                index += 1
            if index < len(parts):
                token = parts[index]
                if token == "--wait" and index + 1 < len(parts):
                    wait_seconds = float(parts[index + 1])
                    index += 2
                elif token.startswith("--wait="):
                    wait_seconds = float(token.split("=", 1)[1])
                    index += 1
                if index != len(parts):
                    raise ValueError("多余参数")
            page = await manager.output(
                job_id, after_sequence=after, wait_seconds=wait_seconds)
        except (ValueError, TypeError) as exc:
            return _ok(f"  [jobs] 参数错误: {exc}")
        except Exception as exc:
            return _ok(f"  [jobs] {exc}")
        return _ok("\n".join(f"  {line}" for line in page.render().splitlines()))

    if operation == "kill":
        if len(parts) != 2:
            return _ok("  用法: /jobs kill <id>")
        try:
            job = await manager.kill(parts[1])
        except Exception as exc:
            return _ok(f"  [jobs] {exc}")
        exit_text = "-" if job.exit_code is None else str(job.exit_code)
        return _ok(
            f"  [jobs] {job.job_id} {job.status.value}（exit {exit_text}）")

    return _ok(
        "  用法: /jobs | /jobs list | /jobs output <id> [after] [--wait 秒] "
        "| /jobs kill <id>")


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


async def _refresh_plugin_runtime(ctx: CommandContext) -> None:
    """Refresh every extension surface after a package lifecycle change."""
    callback = ctx.plugins_reload
    if callback is not None:
        result = callback()
        if hasattr(result, "__await__"):
            await result
    elif ctx.plugins is not None:
        result = ctx.plugins.reload()
        if hasattr(result, "__await__"):
            await result
    sync_plugin_commands(ctx.command_registry, ctx.plugins)


def _plugin_lifecycle_args(raw: str) -> tuple[list[str], str]:
    try:
        tokens = shlex.split(raw) if raw.strip() else []
    except ValueError as exc:
        raise ValueError(f"参数解析失败: {exc}") from exc
    scope = "user"
    filtered: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--scope":
            if index + 1 >= len(tokens):
                raise ValueError("--scope 需要 user 或 project")
            scope = tokens[index + 1].strip().lower()
            index += 2
            continue
        if token.startswith("--scope="):
            scope = token.split("=", 1)[1].strip().lower()
            index += 1
            continue
        filtered.append(token)
        index += 1
    if scope not in {"user", "project"}:
        raise ValueError("scope 必须是 user 或 project")
    return filtered, scope


async def handle_plugins(ctx: CommandContext) -> CommandResult:
    manager = ctx.plugins
    if manager is None:
        return _ok("  （插件系统未启用）")
    if not getattr(manager, "enabled", False):
        return _ok("  （插件已禁用：移除 --no-plugins 或设置 plugins.enabled=true）")
    raw = ctx.request.arguments.strip()
    try:
        parts = shlex.split(raw) if raw else []
    except ValueError as exc:
        return _ok(f"  [plugins] 参数解析失败: {exc}")
    operation = parts[0].lower() if parts else "list"
    output: list[str] = []

    if operation in {"install", "update"}:
        try:
            filtered, scope = _plugin_lifecycle_args(raw)
        except ValueError as exc:
            return _ok(f"  [plugins] {exc}")
        if len(filtered) != 2:
            return _ok(
                f"  用法: /plugins {operation} <path> "
                "[--scope user|project]")
        source = filtered[1]
        try:
            action = (manager.update(source, scope=scope)
                      if operation == "update"
                      else manager.install(source, scope=scope))
            result = await action
            await _refresh_plugin_runtime(ctx)
        except Exception as exc:
            return _ok(
                f"  [plugins] {operation} 失败: {type(exc).__name__}: {exc}")
        output.append(f"  [plugins] {result.message} · scope={result.scope}")
    elif operation in {"enable", "disable"}:
        if len(parts) != 2:
            return _ok(f"  用法: /plugins {operation} <name>")
        try:
            result = await manager.set_enabled(parts[1], operation == "enable")
            await _refresh_plugin_runtime(ctx)
        except Exception as exc:
            return _ok(
                f"  [plugins] {operation} 失败: {type(exc).__name__}: {exc}")
        output.append(f"  [plugins] {result.message} · scope={result.scope}")
    elif operation == "remove":
        if len(parts) != 2:
            return _ok("  用法: /plugins remove <name>")
        try:
            result = await manager.remove(parts[1])
            await _refresh_plugin_runtime(ctx)
        except Exception as exc:
            return _ok(
                f"  [plugins] remove 失败: {type(exc).__name__}: {exc}")
        output.append(
            f"  [plugins] 已移除 {result.name}（移入 {result.path}）")
    elif operation == "reload":
        try:
            await _refresh_plugin_runtime(ctx)
        except Exception as exc:
            return _ok(
                f"  [plugins] reload 失败: {type(exc).__name__}: {exc}")
        output.append("  [plugins] 已重新扫描插件目录并刷新扩展")
    elif operation == "show":
        if len(parts) != 2 or not parts[1].strip():
            return _ok("  用法: /plugins show <name>")
        name = parts[1].strip()
        record = next(
            (item for item in manager.records() if item.name == name), None)
        if record is None:
            return _ok(f"  [plugins] 未找到插件: {name}")
        output.extend((
            f"  [plugin] {record.name}@{record.version} [{record.status}]",
            f"  位置: {record.path}",
            f"  scope: {record.scope or 'custom'} · manifest v{record.manifest_version}",
            f"  入口: {record.entrypoint or '（无）'}",
            f"  组件: tool={record.tools_count} skill={record.skills_count} "
            f"agent={record.agents_count} command={record.commands_count} "
            f"hook={record.hooks_count} mcp={record.mcp_servers_count}",
        ))
        if record.description:
            output.append(f"  描述: {record.description}")
        if record.error:
            output.append(f"  错误: {record.error}")
    elif operation == "list":
        output.extend(manager.status_lines())
    else:
        return _ok(
            "  用法: /plugins | /plugins show <name> | /plugins reload",
            "  /plugins install <path> [--scope user|project]",
            "  /plugins update <path> [--scope user|project]",
            "  /plugins enable|disable <name> | /plugins remove <name>")
    for issue in manager.issues:
        line = f"  ! {issue.path}: {issue.message}"
        if line not in output:
            output.append(line)
    return _ok(*output)


async def handle_extensions(ctx: CommandContext) -> CommandResult:
    """Show the unified extension catalogue with live provenance refresh."""
    callback = getattr(ctx.loop, "extension_refresh", None)
    if callback is not None:
        result = callback()
        if hasattr(result, "__await__"):
            await result
    registry = getattr(ctx.loop, "extension_registry", None)
    if registry is None:
        return _ok("  （ExtensionRegistry 不可用）")
    raw = ctx.request.arguments.strip()
    try:
        parts = shlex.split(raw) if raw else []
    except ValueError as exc:
        return _ok(f"  [extensions] 参数解析失败: {exc}")
    if len(parts) > 2:
        return _ok(
            "  用法: /extensions [plugin|tool|skill|agent|command|hook|mcp] "
            "[source]")
    kind = parts[0].lower() if parts else None
    source = parts[1] if len(parts) == 2 else ""
    return _ok(*registry.status_lines(kind=kind, source=source))


async def handle_plugin_command(ctx: CommandContext) -> CommandResult:
    """Execute a namespaced Plugin v2 Markdown prompt command."""
    manager = ctx.plugins
    if manager is None or not getattr(manager, "enabled", False):
        return _ok("  （插件命令不可用）")
    command = manager.get_command(ctx.request.name)
    if command is None:
        return _ok(f"  （Plugin Command 不可用: /{ctx.request.name}）")
    try:
        prompt = command.render(ctx.request.arguments)
    except Exception as exc:
        return _ok(
            f"  [plugin] Command 渲染失败: {type(exc).__name__}: {exc}")
    return CommandResult(
        handled=True,
        output=(f"  [plugin] 已加载并执行: {command.plugin_name}/{command.local_name}",),
        run_prompt=prompt,
        tool_allowlist=command.allowed_tools or None,
    )


async def handle_custom_command(ctx: CommandContext) -> CommandResult:
    """Execute a user/project Markdown command."""
    manager = getattr(ctx.loop, "custom_command_manager", None)
    command = manager.get(ctx.request.name) if manager is not None else None
    if command is None:
        return _ok(f"  （Custom Command 不可用: /{ctx.request.name}）")
    try:
        prompt = command.render(ctx.request.arguments)
    except Exception as exc:
        return _ok(
            f"  [custom] Command 渲染失败: {type(exc).__name__}: {exc}")
    return CommandResult(
        handled=True,
        output=(f"  [custom:{command.source}] 已加载并执行: {command.name}",),
        run_prompt=prompt,
        tool_allowlist=command.allowed_tools or None,
    )


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


async def handle_lsp(ctx: CommandContext) -> CommandResult:
    manager = getattr(ctx.loop, "lsp_manager", None)
    if manager is None:
        return _ok("  （LSP 不可用）")
    return _ok(*manager.status_lines())


async def handle_web(ctx: CommandContext) -> CommandResult:
    runtime = getattr(ctx.loop, "web_runtime", None)
    options = getattr(runtime, "options", None)
    if options is None or not options.enabled:
        return _ok("  WebFetch/WebSearch 已禁用（--no-web 或 [web].enabled=false）")
    key_env = getattr(options, "search_api_key_env", "")
    configured = bool(os.environ.get(key_env)) if key_env else False
    return _ok(
        f"  WebFetch: enabled · timeout {options.timeout:g}s"
        f" · max {options.max_bytes} bytes · private={'allow' if options.allow_private else 'deny'}",
        f"  允许端口: {', '.join(str(port) for port in options.allowed_ports)}",
        f"  WebSearch: {'enabled' if options.search_enabled else 'disabled'}"
        f" · provider={options.search_provider}"
        f" · key_env={key_env or '（无）'}"
        f" · configured={'yes' if configured else 'no'}",
    )


async def handle_output_style(ctx: CommandContext) -> CommandResult:
    manager = getattr(ctx.loop, "output_style_manager", None)
    if manager is None:
        return _ok("  （Output Styles 不可用）")
    parts = ctx.request.arguments.split(maxsplit=1)
    operation = parts[0].strip().lower() if parts else ""
    argument = parts[1].strip() if len(parts) > 1 else ""
    if not operation:
        return _ok(*manager.status_lines())
    if operation == "list":
        return _ok(*manager.status_lines())
    if operation == "reload":
        manager.reload()
        manager.apply_to_registry(ctx.loop.prompt_registry)
        refresh_registry_snapshot(ctx.loop)
        return _ok("  Output Styles 已重载", *manager.status_lines())
    if operation in {"show", "info"}:
        if not argument:
            return _ok("  用法: /output-style show <name>")
        style = manager.get(argument)
        if style is None:
            return _ok(f"  未找到 Output Style: {argument}")
        return _ok(
            f"  {style.name} [{style.scope}] {style.description}".rstrip(),
            f"  来源: {style.path or style.source}",
            style.content.rstrip())
    if operation in {"off", "none", "default"}:
        manager.select("off")
        manager.apply_to_registry(ctx.loop.prompt_registry)
        refresh_registry_snapshot(ctx.loop)
        return _ok("  已恢复默认输出样式")
    style = manager.select(operation)
    if style is None:
        return _ok(
            f"  未找到 Output Style: {operation}",
            "  可用样式: " + (", ".join(item.name for item in manager.styles)
                              if manager.styles else "（无）"))
    manager.apply_to_registry(ctx.loop.prompt_registry)
    refresh_registry_snapshot(ctx.loop)
    return _ok(
        f"  已切换 Output Style: {style.name} [{style.scope}]",
        f"  来源: {style.path or style.source}")


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

    if operation == "refresh" and len(parts) == 1:
        options = getattr(ctx.loop, "context_options", None)
        rule_loader = getattr(ctx.loop, "rule_loader", None)
        bundle = await build_workspace_context_bundle(
            ctx.workspace_root, options=options, rule_loader=rule_loader)
        ctx.loop.workspace_context = bundle
        manager = ctx.skills or getattr(ctx.loop, "skill_manager", None)
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
        base_prompt = build_system_prompt(bundle.text)
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

    if operation == "rules" and len(parts) == 1:
        loader = getattr(ctx.loop, "rule_loader", None)
        base = getattr(getattr(ctx.loop, "workspace_context", None),
                       "rule_bundle", None)
        if base is None and loader is not None:
            base = loader.load_base()
        directory_source = getattr(ctx.loop, "directory_rule_source", None)
        directory = getattr(directory_source, "last_bundle", None)
        bundles = [bundle for bundle in (base, directory) if bundle is not None]
        if not bundles:
            return _ok("  [rules] 规则加载未启用")
        source_count = sum(len(bundle.sources) for bundle in bundles)
        import_count = sum(bundle.import_count for bundle in bundles)
        issue_count = sum(len(bundle.issues) for bundle in bundles)
        lines = [
            f"  [rules] source {source_count} 个 · import {import_count} 个"
            f" · issue {issue_count} 个",
        ]
        for bundle in bundles:
            for source in bundle.sources:
                imported = (
                    f" · import←{source.imported_by.name}"
                    if source.imported_by else ""
                )
                lines.append(
                    f"  {source.scope.value:9} {source.display_path}"
                    f" · {source.chars} chars{imported}")
            for issue in bundle.issues:
                lines.append(
                    f"  [warn] {issue.code} · {issue.path} · {issue.message}")
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
        if registry is not None:
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

    if operation != "stats" or parts:
        return _ok(
            "  用法: /context | /context fragments [source] | "
            "/context sources | /context rules | "
            "/context plan | /context why <id> | "
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
            f" · stable blocks {snapshot.stable_system_block_count}"
            f" · dynamic blocks {snapshot.dynamic_system_block_count}"
            f" · stable {snapshot.stable_tokens}"
            f" · volatile {snapshot.volatile_tokens}"
            f" · boundary {snapshot.cache_boundary_chars} chars"
            f" · segments {snapshot.cache_segment_count}",
            f"  stable hash: {snapshot.stable_hash or '(none)'}",
            f"  dynamic hash: {snapshot.dynamic_hash or '(none)'}",
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
