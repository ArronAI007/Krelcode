"""Direct slash-command tool execution outside a model turn."""

from __future__ import annotations

import asyncio
from typing import Dict, Iterator, Optional, Protocol

from krelcode.contracts.application import ApplicationRequest
from krelcode.contracts.tool import ToolContext, ToolResult, TurnAbortSignal
from krelcode.core.messages import ToolCall
from krelcode.tools.registry import ToolRegistry
from .intent import TurnIntent
from .tool_pool import TurnToolPool
from .workflows import TaskWorkflowSelection


class _StreamResultPort(Protocol):
    def to_tool_result(self) -> ToolResult: ...


class DirectToolHost(Protocol):
    _turn_prepared: bool
    _turn_tool_context: ToolContext
    _turn_intent: TurnIntent
    _turn_workflow: TaskWorkflowSelection | None
    _active_tool_pool: TurnToolPool | None
    _turn_abort_signal: TurnAbortSignal
    _current_turn_id: str
    _direct_counter: Iterator[int]
    application_request: Optional[ApplicationRequest]
    registry: ToolRegistry

    def _begin_turn(self, turn_id: str) -> None: ...

    async def _run_stream_tool(self, call: ToolCall) -> _StreamResultPort: ...


async def execute_direct_tool(
    loop: DirectToolHost,
    name: str,
    arguments: Dict[str, object],
    *,
    turn_id: Optional[str] = None,
) -> ToolResult:
    if loop._turn_prepared:
        raise RuntimeError("Agent 正在执行查询，不能并发启动直接工具")
    saved_context = loop._turn_tool_context
    saved_intent = loop._turn_intent
    saved_workflow = loop._turn_workflow
    saved_pool = loop._active_tool_pool
    saved_abort = loop._turn_abort_signal
    saved_turn_id = loop._current_turn_id
    saved_application_request = loop.application_request
    direct_turn = turn_id or f"direct-{next(loop._direct_counter)}"
    call = ToolCall(
        id=f"{direct_turn}-call",
        name=name,
        arguments=dict(arguments),
    )
    loop._begin_turn(direct_turn)
    loop._turn_intent = TurnIntent()
    loop._turn_workflow = None
    loop._active_tool_pool = TurnToolPool(
        loop.registry,
        intent=loop._turn_intent,
        runtime=loop._turn_tool_context.runtime,
    )
    loop._active_tool_pool.authorize_direct(name)
    try:
        return (await loop._run_stream_tool(call)).to_tool_result()
    except asyncio.CancelledError:
        loop._turn_abort_signal.cancel("直接工具被用户中止")
        raise
    finally:
        loop._turn_tool_context = saved_context
        loop._turn_intent = saved_intent
        loop._turn_workflow = saved_workflow
        loop._active_tool_pool = saved_pool
        loop._turn_abort_signal = saved_abort
        loop._current_turn_id = saved_turn_id
        loop.application_request = saved_application_request


__all__ = ["execute_direct_tool"]
