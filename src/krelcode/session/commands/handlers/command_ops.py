"""Command catalogue, tool pool, MCP, subagent, statusline, and job commands."""

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

from .handler_utils import _ok, _result_from_render, _template_for_command


async def handle_commands(ctx: CommandContext) -> CommandResult:
    """Show command provenance and content-light template contracts."""
    raw = ctx.request.arguments.strip()
    try:
        parts = shlex.split(raw) if raw else []
    except ValueError as exc:
        return _ok(f"  [commands] 参数解析失败: {exc}")
    if parts and (parts[0].lower() != "show" or len(parts) > 2):
        return _ok("  用法: /commands | /commands show <name>")
    if parts:
        spec = ctx.command_registry.resolve(parts[1])
        if spec is None:
            return _ok(f"  [commands] 未找到命令: {parts[1]}")
        template = CommandTemplate.from_spec(spec)
        return _ok(
            f"  [command] {spec.usage} · {spec.kind.value}"
            f" · source={spec.source.value}",
            f"  trust={template.trust}"
            f" · envelope={template.envelope.value}"
            f" · arguments={template.argument_mode.value}",
            f"  tools={', '.join(template.allowed_tools) or '(all)'}"
            f" · reminders={', '.join(template.reminder_ids) or '(none)'}",
            f"  template v{template.version}"
            f" · hash {template.template_hash}"
            f" · content-light=true",
            f"  origin: {spec.metadata.get('path', '(builtin)')}",
        )

    specs = ctx.command_registry.list(include_hidden=True)
    templates = [
        (spec, CommandTemplate.from_spec(spec))
        for spec in specs
    ]
    by_source: dict[str, int] = {}
    for spec, _template in templates:
        by_source[spec.source.value] = by_source.get(spec.source.value, 0) + 1
    lines = [
        f"  [commands] {len(templates)} 个命令"
        f" · template v{COMMAND_TEMPLATE_VERSION}"
        f" · sources "
        + " · ".join(
            f"{source} {count}"
            for source, count in sorted(by_source.items())),
    ]
    for spec, template in templates:
        lines.append(
            f"  /{spec.name} · {spec.source.value}/{template.trust}"
            f" · args={template.argument_mode.value}"
            f" · tools={len(template.allowed_tools)}"
            f" · reminders={len(template.reminder_ids)}"
            f" · {template.template_hash[:12]}")
    lines.append("  使用 /commands show <name> 查看单个契约；不显示命令正文。")
    return _ok(*lines)


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
    elif arguments == "conformance" or arguments.startswith("conformance "):
        try:
            parts = shlex.split(arguments)
        except ValueError as exc:
            return _ok(f"  [mcp:conformance] 参数解析失败: {exc}")
        if len(parts) > 2:
            return _ok("  用法: /mcp conformance [server]")
        target = parts[1] if len(parts) == 2 else ""
        try:
            lines = ctx.mcp_supervisor.conformance_lines(target)
        except KeyError as exc:
            return _ok(f"  [mcp:conformance] {exc}")
        if not lines:
            return _ok("  （没有在线 MCP 服务器可供一致性检查）")
        return _ok(*lines)
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
        return _ok(
            "  用法: /mcp [status|list|reload|conformance [server]|"
            "auth <server>]")
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


async def handle_statusline(ctx: CommandContext) -> CommandResult:
    manager = ctx.subagents
    if manager is None:
        return _ok("  （子代理未启用，无法启动 statusline-setup）")
    requirements = ctx.request.arguments.strip()
    requested = (
        requirements
        or "Use a concise default layout with model, permission mode, git branch, "
             "and context usage when those placeholders are supported."
    )
    task = (
        "Configure Krelcode's declarative [status_line].template for the user.\n"
        f"User requirements: {requested}\n"
        "Inspect the workspace configuration and supported status-line placeholders "
        "before editing. Prefer the project krelcode.toml when it is writable. If the "
        "user's global config is outside the workspace boundary, do not bypass it: "
        "return the exact TOML snippet and target path. Never execute shell content "
        "inside the template."
    )
    try:
        handle = await manager.spawn(task, definition="statusline-setup")
    except (KeyError, ValueError) as exc:
        return _ok(f"  [statusline] 启动失败: {exc}")
    return _ok(
        f"  [statusline] 已启动 {handle.id}（definition=statusline-setup）",
        "  使用 /agents feed <id> 查看进度，或 /agents list 查看状态。",
    )


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


