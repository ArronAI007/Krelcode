"""会话运行策略：headless 单次任务与交互 REPL 的应用层编排。

UI 通过 ``repl_runner`` 注入，因此本模块不导入终端实现。依赖装配仍由
``app.py`` 完成。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List

from ..agent.runtime import AgentRuntime
from ..core.events import PermissionDenied, ToolCallCompleted
from .bootstrap import setup_session
from .input import InputEnvelope, InputKind


class _RunSummary:
    """JSON 输出模式的事件收集器（旁路订阅）。"""

    def __init__(self) -> None:
        self.tools_used: List[str] = []
        self.denied = 0

    def on_event(self, event) -> None:
        if isinstance(event, ToolCallCompleted):
            self.tools_used.append(event.tool_call.name)
        elif isinstance(event, PermissionDenied):
            self.denied += 1


async def run_session(
    args,
    runtime: AgentRuntime,
    *,
    supervisor,
    checkpoints,
    usage,
    model_switch,
    skills_reload,
    redactor,
    input_processor=None,
    approval_broker=None,
    user_input_broker=None,
    hooks=None,
    log=print,
    repl_runner=None,
    input_stream=None,
    output_stream=None,
) -> int:
    """按 CLI 参数运行 headless turn 或交互 REPL。"""
    loop = runtime.loop
    session_path = setup_session(args, loop, log=log, redactor=redactor)
    runtime.bind_session(Path(session_path).stem if session_path else None)
    sessions_dir = runtime.workspace_root / ".krelcode" / "sessions"

    if hooks is not None:
        if loop.hook_port is None:
            loop.hook_port = hooks
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
                log=log,
            )

        if args.message:
            message = args.message
            images = None
            if input_processor is not None:
                action = await input_processor.process(
                    InputEnvelope(args.message, source="headless"))
                if action.kind is InputKind.PROMPT:
                    message = action.text
                    images = list(action.images) or None
                    for note in action.notes:
                        log(f"[input] {note}")
            if args.output_format == "json":
                summary = _RunSummary()
                loop.bus.subscribe(summary.on_event)
                try:
                    reply = redactor.redact(await runtime.run_once(
                        message, images=images))
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
                    message, images=images)))
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
            skills=loop.skill_manager,
            skills_reload=skills_reload,
            plugins=getattr(loop, "plugin_manager", None),
            plugins_reload=getattr(loop, "plugin_reload", None),
            input_processor=input_processor,
            approval_broker=approval_broker,
            input_broker=user_input_broker,
        )

    try:
        return await _run_body()
    finally:
        if hooks is not None:
            await loop.run_lifecycle_hook("session_end", {
                "session": str(session_path) if session_path else "",
                "workspace": str(runtime.workspace_root),
            })
