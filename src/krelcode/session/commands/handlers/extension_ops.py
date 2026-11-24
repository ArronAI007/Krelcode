"""Skill, plugin, extension, custom-command, and MCP prompt commands."""

from __future__ import annotations

import json
import os
import shlex
from typing import Optional, cast

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
from ....session.commands.custom import CustomCommandManager
from ....agent.tokens import estimate_history_tokens
from ....contracts.json import JsonObject
from ....core.messages import Message
from ....mcp.security import render_mcp_prompt_evidence
from ....safety.runtime import parse_permission_mode
from ....safety.rules import RuleDecision, RuleScope
from ..context import CommandContext
from ..ports import SkillManagerPort
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


def _skills_disabled(ctx: CommandContext) -> bool:
    return ctx.skills is None or not getattr(ctx.skills, "enabled", False)


def _disabled_message() -> str:
    return ("  （Skills 未启用：移除 --no-skills 或在配置中设置 "
            "skills.enabled=true）")


async def handle_skills(ctx: CommandContext) -> CommandResult:
    if _skills_disabled(ctx):
        return _ok(_disabled_message())
    assert ctx.skills is not None
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
            lifecycle_task = (
                manager.update(source, scope=scope)
                if operation == "update"
                else manager.install(source, scope=scope)
            )
            result = await lifecycle_task
            await _refresh_plugin_runtime(ctx)
        except Exception as exc:
            return _ok(
                f"  [plugins] {operation} 失败: {type(exc).__name__}: {exc}")
        output.append(f"  [plugins] {result.message} · scope={result.scope}")
    elif operation == "registry":
        if len(parts) < 2:
            return _ok(
                "  用法: /plugins registry list",
                "  /plugins registry add <name> <path>",
                "  /plugins registry remove <name>",
                "  /plugins registry install <name> <plugin>[@约束] "
                "[--scope user|project]")
        registry_action = parts[1].lower()
        if registry_action == "list":
            if len(parts) != 2:
                return _ok("  用法: /plugins registry list")
            output.extend(manager.registry_lines())
        elif registry_action == "add":
            if len(parts) != 4:
                return _ok("  用法: /plugins registry add <name> <path>")
            try:
                config = manager.add_registry(parts[2], parts[3])
            except Exception as exc:
                return _ok(
                    f"  [plugins] registry add 失败: {type(exc).__name__}: {exc}")
            output.append(
                f"  [plugins] registry 已加入可信配置: {config.name} · {config.path}")
        elif registry_action == "remove":
            if len(parts) != 3:
                return _ok("  用法: /plugins registry remove <name>")
            try:
                config = manager.remove_registry(parts[2])
                await _refresh_plugin_runtime(ctx)
            except Exception as exc:
                return _ok(
                    f"  [plugins] registry remove 失败: {type(exc).__name__}: {exc}")
            output.append(f"  [plugins] registry 已移除: {config.name}")
        elif registry_action == "install":
            try:
                filtered, scope = _plugin_lifecycle_args(raw)
            except ValueError as exc:
                return _ok(f"  [plugins] {exc}")
            # filtered: registry, install, <registry>, <plugin>[@constraint]
            if len(filtered) != 4:
                return _ok(
                    "  用法: /plugins registry install <registry> "
                    "<plugin>[@约束] [--scope user|project]")
            registry_name = filtered[2]
            package_spec = filtered[3]
            if "@" in package_spec:
                plugin_name, constraint = package_spec.split("@", 1)
            else:
                plugin_name, constraint = package_spec, "*"
            try:
                results = await manager.install_from_registry(
                    registry_name,
                    plugin_name,
                    constraint=constraint or "*",
                    scope=scope,
                )
                await _refresh_plugin_runtime(ctx)
            except Exception as exc:
                return _ok(
                    f"  [plugins] registry install 失败: "
                    f"{type(exc).__name__}: {exc}")
            for result in results:
                output.append(
                    f"  [plugins] {result.message} · scope={result.scope} · "
                    f"registry={registry_name}")
        else:
            return _ok(
                "  用法: /plugins registry list|add|remove|install ...")
    elif operation == "trust":
        if len(parts) < 2:
            return _ok(
                "  用法: /plugins trust list",
                "  /plugins trust add <registry> <key-id> <base64-public-key> "
                "[--label <名称>]",
                "  /plugins trust revoke <registry> <key-id>")
        trust_action = parts[1].lower()
        if trust_action == "list":
            output.extend(manager.trust_store.status_lines())
        elif trust_action == "add":
            label = ""
            values = list(parts[2:])
            if "--label" in values:
                index = values.index("--label")
                if index + 1 >= len(values):
                    return _ok("  [plugins] --label 需要值")
                label = values[index + 1]
                del values[index:index + 2]
            if len(values) != 3:
                return _ok(
                    "  用法: /plugins trust add <registry> <key-id> "
                    "<base64-public-key> [--label <名称>]")
            try:
                key = manager.add_trusted_key(
                    values[0], values[1], values[2], label=label)
                await _refresh_plugin_runtime(ctx)
            except Exception as exc:
                return _ok(
                    f"  [plugins] trust add 失败: {type(exc).__name__}: {exc}")
            output.append(
                f"  [plugins] trusted key: {key.registry}/{key.key_id} · "
                f"sha256:{key.fingerprint}")
        elif trust_action == "revoke":
            if len(parts) != 4:
                return _ok("  用法: /plugins trust revoke <registry> <key-id>")
            try:
                key = manager.revoke_trusted_key(parts[2], parts[3])
                await _refresh_plugin_runtime(ctx)
            except Exception as exc:
                return _ok(
                    f"  [plugins] trust revoke 失败: {type(exc).__name__}: {exc}")
            output.append(f"  [plugins] trusted key 已撤销: {key.registry}/{key.key_id}")
        else:
            return _ok("  用法: /plugins trust list|add|revoke ...")
    elif operation == "verify":
        if len(parts) != 2:
            return _ok("  用法: /plugins verify <name>")
        try:
            manifest, check = manager.verify(parts[1])
        except Exception as exc:
            return _ok(
                f"  [plugins] verify 失败: {type(exc).__name__}: {exc}")
        output.extend((
            f"  [plugin] {manifest.name}@{manifest.version} · "
            f"signature={check.status}",
            f"  key: {check.registry + '/' if check.registry else ''}"
            f"{check.key_id or '（无）'}",
            f"  signer: {check.signer or '（未知/未签名）'}",
            f"  integrity: {check.integrity_digest or '（无）'}",
            f"  message: {check.message or check.status}",
        ))
    elif operation == "deps":
        if len(parts) > 2:
            return _ok("  用法: /plugins deps [name]")
        output.extend(manager.dependency_lines(parts[1] if len(parts) == 2 else None))
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
            f"  信任: {record.trust_status or 'not_checked'} · "
            f"registry={record.registry or '（无）'} · "
            f"key={record.key_id or '（无）'}",
            f"  依赖: {', '.join(record.dependencies) or '（无）'}",
            f"  被依赖: {', '.join(record.dependents) or '（无）'}",
        ))
        if record.description:
            output.append(f"  描述: {record.description}")
        if record.error:
            output.append(f"  错误: {record.error}")
        if record.integrity_digest:
            output.append(f"  integrity: {record.integrity_digest}")
    elif operation == "list":
        output.extend(manager.status_lines())
    else:
        return _ok(
            "  用法: /plugins | /plugins show <name> | /plugins reload",
            "  /plugins install <path> [--scope user|project]",
            "  /plugins update <path> [--scope user|project]",
            "  /plugins enable|disable <name> | /plugins remove <name>",
            "  /plugins verify <name> | /plugins deps [name]",
            "  /plugins registry list|add|remove|install ...",
            "  /plugins trust list|add|revoke ...")
    for issue in manager.issues:
        line = f"  ! {issue.path}: {issue.message}"
        if line not in output:
            output.append(line)
    return _ok(*output)


async def handle_extensions(ctx: CommandContext) -> CommandResult:
    """Show the unified extension catalogue with live provenance refresh."""
    callback = ctx.loop.services.extensions.extension_refresh
    if callback is not None:
        result = callback()
        if hasattr(result, "__await__"):
            await result
    registry = ctx.loop.services.extensions.extension_registry
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
        registry = getattr(ctx, "command_registry", None)
        spec = registry.resolve(ctx.request.name) if registry is not None else None
        template = (
            CommandTemplate.from_spec(spec)
            if spec is not None else CommandTemplate.for_source(
                CommandSource.PLUGIN,
                scope=command.scope,
                allowed_tools=tuple(command.allowed_tools),
                reminder_ids=tuple(command.reminder_ids),
            )
        )
        rendered = template.render(
            name=command.name,
            body=command.prompt,
            arguments=ctx.request.arguments,
            attributes={"plugin": command.plugin_name, "name": command.local_name},
        )
    except Exception as exc:
        return _ok(
            f"  [plugin] Command 渲染失败: {type(exc).__name__}: {exc}")
    return _result_from_render(
        rendered,
        output=(
            f"  [plugin] 已加载并执行: "
            f"{command.plugin_name}/{command.local_name}"),
    )


async def handle_custom_command(ctx: CommandContext) -> CommandResult:
    """Execute a user/project Markdown command."""
    manager = cast(Optional[CustomCommandManager], ctx.loop.services.extensions.custom_commands)
    command = manager.get(ctx.request.name) if manager is not None else None
    if command is None:
        return _ok(f"  （Custom Command 不可用: /{ctx.request.name}）")
    try:
        registry = getattr(ctx, "command_registry", None)
        spec = registry.resolve(ctx.request.name) if registry is not None else None
        template = (
            CommandTemplate.from_spec(spec)
            if spec is not None else command.template()
        )
        rendered = template.render(
            name=command.name,
            body=command.prompt,
            arguments=ctx.request.arguments,
        )
    except Exception as exc:
        return _ok(
            f"  [custom] Command 渲染失败: {type(exc).__name__}: {exc}")
    return _result_from_render(
        rendered,
        output=f"  [custom:{command.source}] 已加载并执行: {command.name}",
    )


async def handle_skill(ctx: CommandContext) -> CommandResult:
    if _skills_disabled(ctx):
        return _ok(_disabled_message())
    assert ctx.skills is not None
    parts = ctx.request.arguments.split(maxsplit=1)
    if not parts or not parts[0].strip():
        return _ok("  用法: /skill <name> [参数]")
    name = parts[0].strip()
    arguments = parts[1] if len(parts) > 1 else ""
    try:
        prompt = ctx.skills.user_prompt(name, arguments)
        skill = ctx.skills.get(name)
        template = _template_for_command(ctx, name)
        if skill is not None and not template.reminder_ids:
            template = CommandTemplate.for_source(
                CommandSource.SKILL,
                scope=skill.source,
                allowed_tools=tuple(skill.allowed_tools),
                reminder_ids=split_reminder_ids(
                    skill.metadata.get(
                        "reminder-ids", skill.metadata.get("reminders"))),
            )
        rendered = template.bind(
            argument_mode=CommandArgumentMode.PASSTHROUGH
        ).render(name=name, body=prompt)
    except Exception as exc:
        return _ok(f"  [skill] {exc}")
    return _result_from_render(
        rendered, output=f"  [skill] 已加载并执行: {name}")


async def handle_skill_command(ctx: CommandContext) -> CommandResult:
    """执行由 Skill 自动注册的 `/skill-name [args]`。"""
    if _skills_disabled(ctx):
        return _ok(_disabled_message())
    assert ctx.skills is not None
    name = ctx.request.name
    try:
        prompt = ctx.skills.user_prompt(name, ctx.request.arguments)
        skill = ctx.skills.get(name)
        template = _template_for_command(ctx, name)
        if skill is not None and not template.reminder_ids:
            template = CommandTemplate.for_source(
                CommandSource.SKILL,
                scope=skill.source,
                allowed_tools=tuple(skill.allowed_tools),
                reminder_ids=split_reminder_ids(
                    skill.metadata.get(
                        "reminder-ids", skill.metadata.get("reminders"))),
            )
        rendered = template.bind(
            argument_mode=CommandArgumentMode.PASSTHROUGH
        ).render(name=name, body=prompt)
    except Exception as exc:
        return _ok(f"  [skill] {exc}")
    return _result_from_render(
        rendered, output=f"  [skill] 已加载并执行: {name}")


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
            server, prompt_name, cast(JsonObject, arguments))
    except Exception as exc:
        return _ok(f"  [mcp] Prompt 获取失败: {type(exc).__name__}: {exc}")
    wrapped = render_mcp_prompt_evidence(
        server=server,
        name=prompt_name,
        result=result,
        redactor=getattr(ctx.mcp_supervisor, "_redactor", None),
    )
    try:
        template = _template_for_command(ctx, ctx.request.name)
        if not template.reminder_ids:
            template = CommandTemplate.for_source(CommandSource.MCP)
        rendered = template.bind(
            argument_mode=CommandArgumentMode.PASSTHROUGH
        ).render(name=ctx.request.name, body=wrapped)
    except Exception as exc:
        return _ok(
            f"  [mcp] Command 模板渲染失败: {type(exc).__name__}: {exc}")
    return _result_from_render(
        rendered,
        output=f"  [mcp] 已加载并执行: {server}/{prompt_name}",
    )




def _format_skill_details(skills: SkillManagerPort, name: str) -> str:
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
