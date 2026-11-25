"""会话运行策略：headless 单次任务与交互 REPL 的应用层编排。

UI 通过 ``repl_runner`` 注入，因此本模块不导入终端实现。依赖装配仍由
``app.py`` 完成。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Awaitable, cast, Callable, List, Optional, TextIO

from ..agent.turn.runtime import AgentRuntime
from ..agent.usage import UsageTracker
from ..core.events import AgentEvent, PermissionDenied, ToolCallCompleted
from ..core.redact import SecretRedactor
from .bootstrap import setup_session
from .commands.ports import (
    ModelSwitchPort,
    SkillManagerPort,
    SkillReloadPort,
    SubagentManagerCommandPort,
)
from .controller import SessionController
from .input import InputEnvelope, InputKind, InputProcessor
from .runtime_ports import SessionRuntimePorts


class _RunSummary:
    """JSON 输出模式的事件收集器（旁路订阅）。"""

    def __init__(self) -> None:
        self.tools_used: List[str] = []
        self.denied = 0

    def on_event(self, event: AgentEvent) -> None:
        if isinstance(event, ToolCallCompleted):
            self.tools_used.append(event.tool_call.name)
        elif isinstance(event, PermissionDenied):
            self.denied += 1


async def run_session(
    args: argparse.Namespace,
    runtime: AgentRuntime,
    ports: SessionRuntimePorts,
    *,
    usage: UsageTracker,
    model_switch: Optional[ModelSwitchPort],
    skills_reload: Optional[SkillReloadPort],
    redactor: SecretRedactor,
    input_processor: Optional[InputProcessor] = None,
    log: Callable[[str], None] = print,
    repl_runner: Optional[Callable[..., Awaitable[int]]] = None,
    input_stream: Optional[TextIO] = None,
    output_stream: Optional[TextIO] = None,
) -> int:
    """按 CLI 参数运行 headless turn 或交互 REPL。"""
    loop = runtime.loop
    supervisor = ports.supervisor
    checkpoints = ports.checkpoints
    approval_broker = ports.approval
    user_input_broker = ports.user_input
    hooks = ports.hooks
    session_path = setup_session(args, loop, log=log, redactor=redactor)
    runtime.bind_session(Path(session_path).stem if session_path else None)
    sessions_dir = runtime.workspace_root / ".krelcode" / "sessions"

    if hooks is not None:
        if loop.services.core.hook_port is None:
            loop.set_hook_port(hooks)
        await loop.run_lifecycle_hook("session_start", {
            "session": str(session_path) if session_path else "",
            "workspace": str(runtime.workspace_root),
        })

    async def _run_body() -> int:
        if getattr(args, "output_format", "text") == "stream-json":
            from .stream_json_runner import run_stream_json_session

            return await run_stream_json_session(
                runtime,
                session_path=Path(session_path) if session_path else None,
                input_processor=input_processor,
                usage=usage,
                redactor=redactor,
                initial_prompt=args.message,
                input_stream=input_stream or sys.stdin,
                output_stream=output_stream or sys.stdout,
                approval_broker=approval_broker,
                user_input_broker=user_input_broker,
                log=log,
            )

        if args.message:
            message = args.message
            images = None
            command_result = None
            if input_processor is not None:
                action = await input_processor.process(
                    InputEnvelope(args.message, source="headless"))
                if action.kind is InputKind.PROMPT:
                    message = action.text
                    images = list(action.images) or None
                    for note in action.notes:
                        log(f"[input] {note}")
                elif action.kind is InputKind.COMMAND:
                    controller = SessionController(
                        runtime,
                        mcp_supervisor=supervisor,
                        mcp_reload=runtime.mcp_reload,
                        subagents=cast(
                            Optional[SubagentManagerCommandPort],
                            runtime.subagents,
                        ),
                        checkpoints=checkpoints,
                        usage=usage,
                        model_switch=model_switch,
                        skills=cast(
                            Optional[SkillManagerPort],
                            loop.services.extensions.skills,
                        ),
                        skills_reload=skills_reload,
                        workspace_root=runtime.workspace_root,
                        command_registry=input_processor.command_registry,
                    )
                    result = await controller.execute_command(action.text)
                    command_result = result
                    output = [redactor.redact(line) for line in result.output]
                    if args.output_format == "json":
                        print(json.dumps({
                            "ok": True,
                            "command": action.command,
                            "output": output,
                            "run_prompt": result.run_prompt is not None,
                            "command_source": (
                                result.command_source.value
                                if result.command_source is not None else None),
                            "command_trust": result.command_trust or None,
                            "reminder_ids": list(result.reminder_ids),
                            "template_hash": result.template_hash or None,
                            "session": str(session_path) if session_path else None,
                        }, ensure_ascii=False))
                    else:
                        for line in output:
                            print(line)
                    if result.run_prompt is None:
                        return 0
                    message = result.run_prompt
            if args.output_format == "json":
                summary = _RunSummary()
                loop.bus.subscribe(summary.on_event)
                try:
                    reply = redactor.redact(await runtime.run_once(
                        message,
                        images=images,
                        tool_allowlist=(
                            list(command_result.tool_allowlist)
                            if command_result is not None
                            and command_result.tool_allowlist is not None
                            else None),
                        reminder_ids=(
                            list(command_result.reminder_ids)
                            if command_result is not None
                            and command_result.reminder_ids else None),
                    ))
                    payload = {
                        "ok": True,
                        "reply": reply,
                        "usage": {
                            "calls": usage.session.calls,
                            "input": usage.session.input,
                            "output": usage.session.output,
                            "total": usage.session.total,
                        },
                        "tools_used": summary.tools_used,
                        "denied": summary.denied,
                        "cost_usd": usage.cost_usd,
                        "session": str(session_path) if session_path else None,
                    }
                    code = 0
                except Exception as exc:
                    payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    code = 1
                print(json.dumps(payload, ensure_ascii=False))
                return code
            try:
                print(redactor.redact(await runtime.run_once(
                    message,
                    images=images,
                    tool_allowlist=(
                        list(command_result.tool_allowlist)
                        if command_result is not None
                        and command_result.tool_allowlist is not None
                        else None),
                    reminder_ids=(
                        list(command_result.reminder_ids)
                        if command_result is not None
                        and command_result.reminder_ids else None),
                )))
            except Exception as exc:
                print(f"错误：{type(exc).__name__}: {exc}")
                return 1
            if usage.session.calls:
                print(f"[usage] {usage.session.text()}{usage.cost_note()}")
            return 0

        if repl_runner is None:
            raise RuntimeError("交互模式缺少 REPL runner")
        return await repl_runner(
            loop,
            tool_names=runtime.tool_names,
            runtime=runtime,
            session_path=str(session_path) if session_path else None,
            sessions_dir=sessions_dir,
            mcp_supervisor=supervisor,
            mcp_reload=runtime.mcp_reload,
            subagents=runtime.subagents,
            checkpoints=checkpoints,
            usage=usage,
            model_switch=model_switch,
            redactor=redactor,
            skills=cast(
                Optional[SkillManagerPort],
                loop.services.extensions.skills,
            ),
            skills_reload=skills_reload,
            plugins=loop.services.extensions.plugins,
            plugins_reload=loop.plugin_reload,
            input_processor=input_processor,
            approval_broker=approval_broker,
            input_broker=user_input_broker,
            tui_mouse=bool(getattr(args, "tui_mouse", False)),
        )

    try:
        return await _run_body()
    finally:
        if hooks is not None:
            await loop.run_lifecycle_hook("session_end", {
                "session": str(session_path) if session_path else "",
                "workspace": str(runtime.workspace_root),
            })
