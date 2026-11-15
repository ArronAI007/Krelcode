"""交互会话控制层。

负责会话级业务动作：斜杠命令、历史恢复、任务生命周期和 steering。
本模块不得导入终端渲染、PromptWriter 或任何 ``ui.*`` 模块；纯展示命令
（/thinking、/tool-output）由 UI 层处理。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from ..agent.loop import AgentLoop
from ..agent.runtime import AgentRuntime
from ..tools.base import CommandDescriptor, ToolServices
from .commands import (
    CommandContext,
    CommandKind,
    CommandRegistry,
    CommandRequest,
    CommandResult,
    builtin_command_registry,
    normalize_token,
    render_help,
    sync_mcp_prompt_commands,
    sync_skill_commands,
)
from .commands.handlers import BUILTIN_HANDLERS, execute_prompt_command

HELP_TEXT = render_help(builtin_command_registry())


class SessionController:
    """拥有会话命令与会话任务状态，不依赖任何 UI 实现。"""

    def __init__(
        self,
        runtime: AgentRuntime | AgentLoop,
        *,
        session_path: Optional[str] = None,
        sessions_dir: Optional[Path] = None,
        mcp_supervisor=None,
        mcp_reload=None,
        subagents=None,
        checkpoints=None,
        usage=None,
        model_switch=None,
        skills=None,
        skills_reload=None,
        workspace_root: Optional[Path] = None,
        command_registry: Optional[CommandRegistry] = None,
    ) -> None:
        self.runtime = (runtime if isinstance(runtime, AgentRuntime)
                        else AgentRuntime(runtime))
        self.loop = self.runtime.loop
        self.session_path = session_path
        self.sessions_dir = sessions_dir
        self.mcp_supervisor = (mcp_supervisor
                               if mcp_supervisor is not None
                               else self.runtime.mcp_supervisor)
        self.mcp_reload = (mcp_reload
                           if mcp_reload is not None
                           else self.runtime.mcp_reload)
        self.subagents = (subagents
                          if subagents is not None
                          else self.runtime.subagents)
        self.checkpoints = checkpoints
        self.usage = usage
        self.model_switch = model_switch
        self.skills = skills
        self.skills_reload = skills_reload
        self.workspace_root = workspace_root or self.runtime.workspace_root
        self.command_registry = command_registry or builtin_command_registry()
        sync_skill_commands(self.command_registry, self.skills)
        sync_mcp_prompt_commands(self.command_registry, self.mcp_supervisor)
        self.refresh_tool_services()

    @property
    def active_task(self) -> Optional[asyncio.Task]:
        return self.runtime.active_task

    @property
    def is_running(self) -> bool:
        return self.runtime.is_running

    def start_query(
        self,
        message: str,
        *,
        images=None,
        tool_allowlist: Optional[list[str]] = None,
    ):
        """启动一轮查询并返回可消费事件的 QuerySession。"""
        return self.runtime.start_query(
            message,
            images=images,
            tool_allowlist=tool_allowlist,
        )

    def start_run(self, message: str, *, images=None) -> asyncio.Task:
        """兼容入口：启动一轮 Agent 执行并返回 task。"""
        return self.runtime.start_turn(message, images=images)

    def steer(self, message: str, *, images=None) -> str:
        """把运行中插话放入 runtime steering 队列，返回可见摘要。"""
        return self.runtime.steer(message, images=images)

    def drain_steering(self) -> list[Any]:
        """任务结束后取出未送达插话，避免污染下一轮。"""
        return self.runtime.drain_steering()

    async def stop(self) -> bool:
        """取消当前任务；没有活动任务时返回 False。"""
        return await self.runtime.stop()

    async def execute_command(self, user_input: str) -> CommandResult:
        """解析并分发内置业务命令；UI/Control 命令由前台处理。"""
        if not user_input.startswith("/"):
            return CommandResult.unhandled()
        token, _, arguments = user_input.partition(" ")
        spec = self.command_registry.resolve(token)
        if spec is not None and spec.kind not in (
                CommandKind.COMMAND, CommandKind.PROMPT):
            return CommandResult.unhandled()
        name = spec.name if spec is not None else normalize_token(token)
        request = CommandRequest(
            name=name,
            arguments=arguments.strip(),
            raw_text=user_input,
        )
        context = self._command_context(request)
        if spec is not None and spec.kind is CommandKind.PROMPT:
            return await execute_prompt_command(context)
        handler = BUILTIN_HANDLERS.get(name)
        if handler is None:
            return CommandResult(
                handled=True,
                output=(f"  （未知命令: /{name}，/help 查看可用命令）",),
            )
        result = await handler(context)
        self.refresh_tool_services()
        return result

    def refresh_tool_services(self) -> None:
        """把命令/MCP 目录刷新为 ToolUseContext 中的只读窄快照。"""
        commands = tuple(
            CommandDescriptor(
                name=spec.invocation,
                description=spec.description,
                kind=spec.kind.value,
                source=spec.source.value,
            )
            for spec in self.command_registry.specs
        )
        mcp_servers = ()
        if self.mcp_supervisor is not None:
            mcp_servers = tuple(
                item["name"] for item in self.mcp_supervisor.status())
        self.loop.tool_context.services = ToolServices(
            commands=commands,
            mcp_servers=mcp_servers,
        )

    def _command_context(self, request: CommandRequest) -> CommandContext:
        return CommandContext(
            request=request,
            loop=self.loop,
            command_registry=self.command_registry,
            workspace_root=self.workspace_root,
            session_path=self.session_path,
            sessions_dir=self.sessions_dir,
            mcp_supervisor=self.mcp_supervisor,
            mcp_reload=self.mcp_reload,
            subagents=self.subagents,
            checkpoints=self.checkpoints,
            usage=self.usage,
            model_switch=self.model_switch,
            skills=self.skills,
            skills_reload=self.skills_reload,
        )
