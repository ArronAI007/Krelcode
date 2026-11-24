"""交互会话控制层。

负责会话级业务动作：斜杠命令、历史恢复、任务生命周期和 steering。
本模块不得导入终端渲染、PromptWriter 或任何 ``ui.*`` 模块；纯展示命令
（/thinking、/tool-output）由 UI 层处理。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, cast, Optional, Sequence

from ..agent.loop import AgentLoop
from ..agent.runtime import AgentRuntime
from ..agent.turn.query import QuerySession
from ..contracts.runtime import PermissionMode
from ..safety.runtime import PlanApprovalCoordinator, RuntimePermissionManager
from ..contracts.tool import CommandDescriptor, ToolResult, ToolServices
from ..core.messages import ImageRef, SteeringInput
from .compact_checkpoint import CompactCheckpointStore
from .lifecycle import SessionLifecycle
from .rewind import RewindCoordinator
from .runtime_ports import CheckpointPort
from .commands import (
    CommandContext,
    CommandKind,
    CommandRegistry,
    CommandRequest,
    CommandResult,
    CommandSource,
    CommandSpec,
    CommandTemplate,
    builtin_command_registry,
    normalize_token,
    render_help,
    sync_custom_commands,
    sync_mcp_prompt_commands,
    sync_plugin_commands,
    sync_skill_commands,
)
from .commands.handlers import BUILTIN_HANDLERS, execute_prompt_command
from .commands.ports import (
    CheckpointManagerPort,
    MCPReloadPort,
    MCPSupervisorPort,
    ModelSwitchPort,
    PluginManagerPort,
    PluginReloadPort,
    RewindPort,
    ShellJobCommandPort,
    SkillManagerPort,
    SkillReloadPort,
    SubagentManagerCommandPort,
    UsageTrackerPort,
)

HELP_TEXT = render_help(builtin_command_registry())


class SessionController:
    """拥有会话命令与会话任务状态，不依赖任何 UI 实现。"""

    def __init__(
        self,
        runtime: AgentRuntime | AgentLoop,
        *,
        session_path: Optional[str] = None,
        sessions_dir: Optional[Path] = None,
        mcp_supervisor: Optional[MCPSupervisorPort] = None,
        mcp_reload: Optional[MCPReloadPort] = None,
        subagents: Optional[SubagentManagerCommandPort] = None,
        checkpoints: Optional[CheckpointPort] = None,
        usage: Optional[UsageTrackerPort] = None,
        model_switch: Optional[ModelSwitchPort] = None,
        skills: Optional[SkillManagerPort] = None,
        skills_reload: Optional[SkillReloadPort] = None,
        plugins: Optional[PluginManagerPort] = None,
        plugins_reload: Optional[PluginReloadPort] = None,
        workspace_root: Optional[Path] = None,
        command_registry: Optional[CommandRegistry] = None,
        permission_manager: Optional[RuntimePermissionManager] = None,
        plan_manager: Optional[PlanApprovalCoordinator] = None,
        rewind: Optional[RewindPort] = None,
        jobs: Optional[ShellJobCommandPort] = None,
    ) -> None:
        self.runtime = (runtime if isinstance(runtime, AgentRuntime)
                        else AgentRuntime(runtime))
        self.loop = self.runtime.loop
        self.session_path = session_path
        self.sessions_dir = sessions_dir
        self.session_lifecycle = SessionLifecycle(
            self.loop,
            sessions_dir,
            session_path,
            runtime=self.runtime,
            redactor=self.loop.services.core.redactor,
        )
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
        self.jobs = jobs if jobs is not None else self.runtime.shell_jobs
        self.skills = skills
        self.skills_reload = skills_reload
        self.plugins = (
            plugins if plugins is not None
            else cast(
                Optional[PluginManagerPort],
                self.loop.services.extensions.plugins,
            )
        )
        self.plugins_reload = (
            plugins_reload
            if plugins_reload is not None
            else self.loop.plugin_reload
        )
        self.workspace_root = workspace_root or self.runtime.workspace_root
        self.permission_manager = (
            permission_manager or RuntimePermissionManager(self.loop))
        self.plan_manager = (
            plan_manager or PlanApprovalCoordinator(self.permission_manager))
        self.rewind = (
            rewind
            or RewindCoordinator(
                runtime=self.runtime,
                loop=self.loop,
                session_lifecycle=self.session_lifecycle,
                plan_manager=self.plan_manager,
                checkpoints=self.checkpoints,
                compact_checkpoints=cast(
                    Optional[CompactCheckpointStore],
                    self.loop.services.context.compact_checkpoint,
                ),
                context_checkpoints=self.loop.services.context.checkpoint,
                context_coordinator=(
                    self.loop.services.context.checkpoint_coordinator),
            )
        )
        self.command_registry = command_registry or builtin_command_registry()
        sync_custom_commands(
            self.command_registry,
            self.loop.services.extensions.custom_commands,
        )
        sync_skill_commands(self.command_registry, self.skills)
        sync_plugin_commands(self.command_registry, self.plugins)
        sync_mcp_prompt_commands(self.command_registry, self.mcp_supervisor)
        self.refresh_tool_services()

    @property
    def active_task(self) -> Optional[asyncio.Task[str]]:
        return self.runtime.active_task

    @property
    def is_running(self) -> bool:
        return self.runtime.is_running

    def start_query(
        self,
        message: str,
        *,
        images: Optional[list[ImageRef]] = None,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[list[str]] = None,
    ) -> QuerySession:
        """启动一轮查询并返回可消费事件的 QuerySession。"""
        if self.permission_manager.mode.value == "plan":
            message = self.plan_manager.wrap_request(message)
        return self.runtime.start_query(
            message,
            images=images,
            tool_allowlist=tool_allowlist,
            reminder_ids=reminder_ids,
        )

    def start_run(
        self,
        message: str,
        *,
        images: Optional[list[ImageRef]] = None,
    ) -> asyncio.Task[str]:
        """兼容入口：启动一轮 Agent 执行并返回 task。"""
        return self.runtime.start_turn(message, images=images)

    def steer(
        self,
        message: str,
        *,
        images: Optional[list[ImageRef]] = None,
    ) -> str:
        """把运行中插话放入 runtime steering 队列，返回可见摘要。"""
        return self.runtime.steer(message, images=images)

    def drain_steering(self) -> list[SteeringInput | str]:
        """任务结束后取出未送达插话，避免污染下一轮。"""
        return self.runtime.drain_steering()

    async def execute_shell_shortcut(self, command: str) -> ToolResult:
        """Run a REPL ``!`` command through the guarded direct-tool path."""
        if self.is_running:
            raise RuntimeError("当前任务运行中，不能并发执行 Shell 快捷命令")
        text = str(command).strip()
        if not text:
            raise ValueError("Shell 快捷命令不能为空")
        return await self.loop.execute_direct_tool(
            "run_command", {"command": text})

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
            result = await execute_prompt_command(context)
        else:
            handler = BUILTIN_HANDLERS.get(name)
            if handler is None:
                return CommandResult(
                    handled=True,
                    output=(f"  （未知命令: /{name}，/help 查看可用命令）",),
                )
            result = await handler(context)
        if self.session_lifecycle.session_path is not None:
            self.session_path = str(self.session_lifecycle.session_path)
        self.refresh_tool_services()
        return self._enrich_command_result(result, spec)

    @staticmethod
    def _enrich_command_result(
        result: CommandResult,
        spec: Optional[CommandSpec],
    ) -> CommandResult:
        """Attach a uniform content-light contract to every prompt result."""
        if result.run_prompt is None:
            return result
        template = (
            CommandTemplate.from_spec(spec)
            if spec is not None
            else CommandTemplate.for_source(CommandSource.BUILTIN)
        )
        reminder_ids = tuple(dict.fromkeys((
            *result.reminder_ids,
            *template.reminder_ids,
        )))
        return replace(
            result,
            tool_allowlist=(
                result.tool_allowlist
                if result.tool_allowlist is not None
                else (template.allowed_tools or None)
            ),
            command_source=result.command_source or template.source,
            command_trust=result.command_trust or template.trust,
            reminder_ids=reminder_ids,
            template_hash=result.template_hash or template.template_hash,
        )

    def set_permission_mode(
        self,
        mode: PermissionMode | str,
    ) -> tuple[str, str]:
        """切换模式并让 Plan 协调器同步处理待确认状态。"""
        previous = self.permission_manager.mode
        self.permission_manager.set_mode(mode)
        current = self.permission_manager.mode
        self.plan_manager.on_mode_change(previous, current)
        return previous.value, current.value

    def cycle_permission_mode(self, *, reverse: bool = False) -> tuple[str, str]:
        previous = self.permission_manager.mode
        current = self.permission_manager.cycle_mode(reverse=reverse)
        self.plan_manager.on_mode_change(previous, current)
        return previous.value, current.value

    def record_plan(
        self,
        items: Sequence[str],
        statuses: Sequence[str],
        active_index: int = -1,
        explanation: str = "",
    ) -> None:
        self.plan_manager.record(
            items, statuses, active_index=active_index,
            explanation=explanation,
        )

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
        mcp_servers: tuple[str, ...] = ()
        if self.mcp_supervisor is not None:
            mcp_servers = tuple(
                str(item["name"]) for item in self.mcp_supervisor.status())
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
            mcp_supervisor=cast(
                Optional[MCPSupervisorPort], self.mcp_supervisor),
            mcp_reload=self.mcp_reload,
            subagents=cast(
                Optional[SubagentManagerCommandPort], self.subagents),
            checkpoints=self.checkpoints,
            usage=self.usage,
            model_switch=self.model_switch,
            skills=self.skills,
            skills_reload=self.skills_reload,
            plugins=self.plugins,
            plugins_reload=self.plugins_reload,
            session_lifecycle=self.session_lifecycle,
            permission_manager=self.permission_manager,
            plan_manager=self.plan_manager,
            rewind=self.rewind,
            jobs=cast(Optional[ShellJobCommandPort], self.jobs),
        )
