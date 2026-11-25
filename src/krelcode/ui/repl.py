"""REPL compatibility facade and process adapter.

The concrete terminal primitives live in :mod:`krelcode.ui.repl_primitives`,
command-facing helpers in :mod:`krelcode.ui.repl_surface`, and command routing
in :mod:`krelcode.ui.command_dispatch`.  This module keeps historical imports
stable while delegating the actual interactive session to ConversationHost.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..agent.turn.runtime import AgentRuntime
from ..agent.turn.loop import AgentLoop
from ..core.redact import SecretRedactor
from ..session.commands.ports import (
    MCPReloadPort,
    MCPSupervisorPort,
    ModelSwitchPort,
    PluginManagerPort,
    PluginReloadPort,
    SkillManagerPort,
    SkillReloadPort,
    SubagentManagerCommandPort,
    UsageTrackerPort,
)
from ..session.input import InputProcessor
from ..session.interactions import UserInputBroker
from ..session.manager import resolve_resume_target
from ..session.runtime_ports import CheckpointPort
from .approval import ApprovalBroker
from .command_dispatch import _handle_command
from .editor import EditBuffer, HistorySearch, InputHistory, sanitize_pasted_text
from .repl_primitives import (
    IDLE_PROMPT,
    MENU_ROWS,
    RUNNING_PROMPT,
    PromptWriter,
    ReplControl,
    TerminalLineEditor,
    TerminalSubmission,
    _display_width,
    _input_rule,
    _pad_display,
    _stdout_write,
    _truncate_display,
)
from .repl_surface import (
    QUEUE_USAGE,
    RUNNING_HINT,
    RUNNING_SLASH_COMMANDS,
    SLASH_COMMANDS,
    _format_job_notification,
    _format_prompt_queue,
    _queue_label,
    format_plan_lines,
    handle_plan_command,
    manage_prompt_queue,
    match_slash_commands,
    print_banner,
)


async def run_repl(
    loop: AgentLoop,
    tool_names: Optional[list[str]] = None,
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
    input_fn: Optional[Callable[[str], str | Awaitable[str]]] = None,
    redactor: Optional[SecretRedactor] = None,
    runtime: Optional[AgentRuntime] = None,
    input_processor: Optional[InputProcessor] = None,
    approval_broker: Optional[ApprovalBroker] = None,
    input_broker: Optional[UserInputBroker] = None,
    history_path: Optional[Path] = None,
    persist_history: Optional[bool] = None,
    tui_mouse: bool = False,
) -> int:
    """Compatibility entrypoint for the extracted conversation host."""
    from .conversation_host import ConversationHost

    host = ConversationHost(
        loop,
        tool_names=tool_names,
        session_path=session_path,
        sessions_dir=sessions_dir,
        mcp_supervisor=mcp_supervisor,
        mcp_reload=mcp_reload,
        subagents=subagents,
        checkpoints=checkpoints,
        usage=usage,
        model_switch=model_switch,
        skills=skills,
        skills_reload=skills_reload,
        plugins=plugins,
        plugins_reload=plugins_reload,
        input_fn=input_fn,
        redactor=redactor,
        runtime=runtime,
        input_processor=input_processor,
        approval_broker=approval_broker,
        input_broker=input_broker,
        history_path=history_path,
        persist_history=persist_history,
        tui_mouse=tui_mouse,
    )
    return await host.run()


def _resolve_resume_target(target: str, sessions_dir: Optional[Path]) -> Optional[Path]:
    """兼容旧测试/嵌入方的导入路径。"""
    return resolve_resume_target(target, sessions_dir)
