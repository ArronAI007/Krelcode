"""Headless stream-json 会话运行器。

支持单次 ``-m`` 与持久 stdin 多轮输入。输入/输出只使用 session 层协议，
不依赖具体 Provider、终端 renderer 或 CLI 表单。

持久模式允许在当前 turn 运行时继续读取控制消息。``control/interrupt`` 会
通过 ``AgentRuntime.stop()`` 中止当前查询，但不会关闭整个 stream 会话；
后续 user 消息仍可继续执行。
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any, cast, Callable, Iterator, Optional, Protocol, TextIO

from ..agent.runtime import AgentRuntime
from ..agent.usage import UsageTotals, UsageTracker
from ..core.events import (
    AgentEvent,
    ShellJobFinished,
    ShellJobOutput,
    ShellJobStarted,
)
from ..core.redact import SecretRedactor
from ..core.session_events import SessionEventPayload, as_session_event_payload
from ..agent.query import QueryEvent, QuerySession
from .controller import SessionController
from .input import InputAction, InputEnvelope, InputKind, InputProcessor
from .interactions import UserInputBroker
from .commands.types import CommandResult
from .commands.ports import MCPSupervisorPort, SubagentManagerCommandPort
from .stream_json import (
    StreamControlMessage,
    StreamJsonEncoder,
    StreamJsonMetadata,
    StreamJsonProtocolError,
    StreamUserMessage,
    dumps_message,
    parse_input_line,
)


class _ApprovalBrokerPort(Protocol):
    """Session-layer interaction port; the UI broker is injected by the composition root."""

    def expect(self, request_id: str) -> None: ...
    def resolve_for(self, request_id: str, raw: str) -> Optional[str]: ...
    def cancel_all(self) -> None: ...


_INPUT_DONE = object()


class _JsonlWriter:
    def __init__(self, stream: TextIO, redactor: SecretRedactor) -> None:
        self.stream = stream
        self.redactor = redactor

    def emit(self, message: dict[str, Any]) -> None:
        # 结构化对象先脱敏再序列化，避免 secret 中的引号/换行在 JSON 转义后
        # 绕过纯文本 replace。
        safe = _redact_value(message, self.redactor)
        self.stream.write(dumps_message(safe) + "\n")
        self.stream.flush()


@dataclass
class _TurnOutcome:
    deferred: deque[Any] = field(default_factory=deque)
    exit_code: int = 0


async def run_stream_json_session(
    runtime: AgentRuntime,
    *,
    session_path: Optional[Path],
    input_processor: Optional[InputProcessor],
    usage: UsageTracker,
    redactor: SecretRedactor,
    initial_prompt: Optional[str] = None,
    input_stream: Optional[TextIO] = None,
    output_stream: Optional[TextIO] = None,
    approval_broker: Optional[_ApprovalBrokerPort] = None,
    user_input_broker: Optional[UserInputBroker] = None,
    log: Callable[[str], None] = print,
) -> int:
    """运行 stream-json 输入输出；返回进程退出码。"""
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    loop = runtime.loop
    encoder = StreamJsonEncoder(StreamJsonMetadata(
        session_id=Path(session_path).stem if session_path else None,
        cwd=str(runtime.workspace_root),
        provider=str(getattr(loop.llm, "name", "unknown")),
        model=str(getattr(loop.llm, "model", "unknown")),
        tools=tuple(runtime.tool_names),
        mcp_servers=_mcp_server_names(runtime),
        permission_mode=loop.tool_context.runtime.permission_mode.value,
        run_mode="stream-json",
    ))
    writer = _JsonlWriter(output_stream, redactor)
    writer.emit(encoder.init_message())
    controller = SessionController(
        runtime,
        mcp_supervisor=cast(
            Optional[MCPSupervisorPort], runtime.mcp_supervisor),
        mcp_reload=runtime.mcp_reload,
        subagents=cast(
            Optional[SubagentManagerCommandPort], runtime.subagents),
        usage=usage,
        skills=getattr(loop, "skill_manager", None),
        workspace_root=runtime.workspace_root,
        command_registry=(
            input_processor.command_registry
            if input_processor is not None else None),
    )
    command_counter = count(1)

    def on_job_event(event: AgentEvent) -> None:
        if isinstance(event, (ShellJobStarted, ShellJobOutput, ShellJobFinished)):
            # 后台 job 独立于 turn；全局订阅保证 turn 结束后仍能实时输出。
            writer.emit(encoder.job_event_message(event))

    unsubscribe_jobs = loop.bus.subscribe(on_job_event)
    input_queue: asyncio.Queue[Any] = asyncio.Queue()
    if initial_prompt is not None:
        input_queue.put_nowait(StreamUserMessage(initial_prompt))
        input_queue.put_nowait(_INPUT_DONE)
    reader_task = asyncio.create_task(_read_inputs(input_stream, input_queue))

    pending: deque[Any] = deque()
    exit_code = 0
    reason = "eof"
    try:
        while True:
            item = await _next_item(input_queue, pending)
            if item is _INPUT_DONE:
                break
            if isinstance(item, StreamControlMessage):
                if item.subtype == "end":
                    _cancel_interactions(approval_broker, user_input_broker)
                    reason = "control_end"
                    break
                if item.subtype in {
                        "permission_response", "user_input_response"}:
                    _emit_idle_interaction_response(writer, encoder, item)
                    continue
                _emit_idle_interrupt(writer, encoder)
                continue
            if isinstance(item, StreamJsonProtocolError):
                _emit_input_error(writer, encoder, item)
                exit_code = 2
                break
            if not isinstance(item, StreamUserMessage):
                continue

            outcome = await _run_turn(
                runtime,
                item,
                input_queue=input_queue,
                input_processor=input_processor,
                usage=usage,
                writer=writer,
                encoder=encoder,
                controller=controller,
                command_counter=command_counter,
                approval_broker=approval_broker,
                user_input_broker=user_input_broker,
                log=log,
            )
            pending.extend(outcome.deferred)
            if outcome.exit_code:
                exit_code = outcome.exit_code
    except asyncio.CancelledError:
        # 进程级取消：尽量给协议消费者留下终态，再退出。
        exit_code = 130
        raise
    except (BrokenPipeError, ConnectionResetError):
        exit_code = 1
    finally:
        _cancel_interactions(approval_broker, user_input_broker)
        unsubscribe_jobs()
        reader_task.cancel()
        await asyncio.gather(reader_task, return_exceptions=True)
        try:
            writer.emit(encoder.session_end_message(reason=reason))
        except (BrokenPipeError, ConnectionResetError):
            pass
    return exit_code


async def _run_turn(
    runtime: AgentRuntime,
    message: StreamUserMessage,
    *,
    input_queue: asyncio.Queue[Any],
    input_processor: Optional[InputProcessor],
    usage: UsageTracker,
    writer: _JsonlWriter,
    encoder: StreamJsonEncoder,
    controller: SessionController,
    command_counter: Iterator[int],
    approval_broker: Optional[_ApprovalBrokerPort],
    user_input_broker: Optional[UserInputBroker],
    log: Callable[[str], None],
) -> _TurnOutcome:
    """执行一个 turn；同时监听 interrupt/end，并保留后续 user 消息顺序。"""
    started = time.monotonic()
    command_turn_id = ""
    try:
        action = await _prepare_input(input_processor, message.content, log=log)
        if action is not None and action.kind is InputKind.COMMAND:
            command_turn_id = f"command-{next(command_counter)}"
            result, event_sequence = await _run_command_action(
                controller,
                action,
                message,
                command_turn_id=command_turn_id,
                writer=writer,
                encoder=encoder,
            )
            if result.run_prompt is None:
                writer.emit(encoder.result_message(
                    subtype="success",
                    result="\n".join(result.output),
                    is_error=False,
                    turn_id=command_turn_id,
                    sequence=event_sequence,
                    extra={
                        **_result_extra(
                            usage, duration_ms=_elapsed_ms(started)),
                        **_request_extra(message),
                    },
                ))
                return _TurnOutcome()
            prepared = None
            prompt = result.run_prompt
            images = None
            tool_allowlist = (
                list(result.tool_allowlist)
                if result.tool_allowlist is not None else None)
            reminder_ids = (
                list(result.reminder_ids) if result.reminder_ids else None)
        else:
            prepared = (
                action if action is not None
                and action.kind is InputKind.PROMPT else None)
            images = list(message.images) or (
                list(prepared.images) if prepared is not None else None)
            prompt = prepared.text if prepared is not None else message.content
            tool_allowlist = None
            reminder_ids = None
        query = runtime.start_query(
            prompt,
            images=images or None,
            tool_allowlist=tool_allowlist,
            reminder_ids=reminder_ids,
        )
    except Exception as exc:
        writer.emit(encoder.result_message(
            subtype="error",
            result=f"{type(exc).__name__}: {exc}",
            is_error=True,
            extra={
                **_result_extra(
                    usage, duration_ms=_elapsed_ms(started)),
                **_request_extra(message),
            },
        ))
        return _TurnOutcome(exit_code=1)

    outcome = _TurnOutcome()
    terminal_seen = False
    last_sequence = -1
    extra_fields = _request_extra(message) or None
    monitor = asyncio.create_task(_monitor_during_turn(
        runtime,
        query,
        input_queue,
        outcome,
        writer=writer,
        encoder=encoder,
        approval_broker=approval_broker,
        user_input_broker=user_input_broker,
    ))

    try:
        async for item in query.events:
            last_sequence = item.sequence
            if isinstance(item.event, (
                    ShellJobStarted, ShellJobOutput, ShellJobFinished)):
                # 已由全局 job 订阅写出；跳过可避免 turn 内重复。
                continue
            encoded = encoder.encode(
                item,
                result_extra=_result_extra(
                    usage, duration_ms=_elapsed_ms(started)),
                extra_fields=extra_fields,
            )
            if (
                approval_broker is not None
                and encoded.get("type") == "permission"
                and encoded.get("subtype") == "requested"
            ):
                approval_broker.expect(str(encoded.get("permission_id", "")))
            writer.emit(encoded)
            terminal_seen = encoded.get("type") == "result"
        await query.wait()
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and getattr(current, "cancelling", lambda: 0)():
            raise
        # runtime.stop() 取消的是 query task，而不是 stream runner。典型情况下
        # RunCancelled 已经把 result 写入；兜底保证协议始终有且仅有一个终态。
        if not terminal_seen:
            writer.emit(encoder.result_message(
                subtype="cancelled",
                result="查询已取消",
                is_error=True,
                turn_id=query.turn_id,
                sequence=last_sequence + 1,
                extra={
                    **_result_extra(
                        usage, duration_ms=_elapsed_ms(started)),
                    **(extra_fields or {}),
                },
            ))
    except Exception as exc:
        outcome.exit_code = 1
        if not terminal_seen:
            writer.emit(encoder.result_message(
                subtype="error",
                result=f"{type(exc).__name__}: {exc}",
                is_error=True,
                turn_id=query.turn_id,
                sequence=last_sequence + 1,
                extra={
                    **_result_extra(
                        usage, duration_ms=_elapsed_ms(started)),
                    **(extra_fields or {}),
                },
            ))
    finally:
        _cancel_interactions(approval_broker, user_input_broker)
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        # monitor 可能在取消边界刚取走一条消息；把它排到尚未读取的消息之前。
        while not input_queue.empty():
            try:
                outcome.deferred.append(input_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
    return outcome


async def _monitor_during_turn(
    runtime: AgentRuntime,
    query: QuerySession,
    input_queue: asyncio.Queue[Any],
    outcome: _TurnOutcome,
    *,
    writer: _JsonlWriter,
    encoder: StreamJsonEncoder,
    approval_broker: Optional[_ApprovalBrokerPort],
    user_input_broker: Optional[UserInputBroker],
) -> None:
    """在 turn 运行期间消费控制消息，user 消息延后但保持顺序。"""
    while not query.task.done():
        item = await input_queue.get()
        if isinstance(item, StreamUserMessage) or item is _INPUT_DONE:
            if item is _INPUT_DONE:
                _cancel_interactions(approval_broker, user_input_broker)
            outcome.deferred.append(item)
            if item is _INPUT_DONE:
                return
            continue
        if isinstance(item, StreamJsonProtocolError):
            _cancel_interactions(approval_broker, user_input_broker)
            outcome.deferred.append(item)
            await runtime.stop()
            return
        if isinstance(item, StreamControlMessage):
            if item.subtype == "permission_response":
                accepted = _resolve_permission(
                    approval_broker, item.request_id, item.decision)
                _emit_interaction_ack(
                    writer, encoder, item, accepted=accepted,
                    detail=(
                        "" if accepted else
                        "没有匹配的待审批请求或审批已结束"))
                continue
            if item.subtype == "user_input_response":
                accepted = _resolve_user_input(user_input_broker, item)
                _emit_interaction_ack(
                    writer, encoder, item, accepted=accepted,
                    detail=(
                        "" if accepted else
                        "没有匹配的待输入请求或输入已结束"))
                continue
            if item.subtype == "interrupt":
                _cancel_interactions(approval_broker, user_input_broker)
                await runtime.stop()
                return
            if item.subtype == "end":
                # 优雅关闭：当前 turn 及 end 之前已到达的 user 消息都完成，
                # 外层读到这个 end 后退出。立即中止应先发送 interrupt。
                _cancel_interactions(approval_broker, user_input_broker)
                outcome.deferred.append(item)
                return


def _resolve_permission(
    broker: Optional[_ApprovalBrokerPort], request_id: str, decision: str,
) -> bool:
    if broker is None:
        return False
    mapped = {"allow": "yes", "deny": "no", "always": "always"}[decision]
    return broker.resolve_for(request_id, mapped) is not None


def _resolve_user_input(
    broker: Optional[UserInputBroker], item: StreamControlMessage,
) -> bool:
    if broker is None:
        return False
    if item.action == "cancel":
        return broker.cancel_request(item.request_id)
    result = broker.resolve(item.content, request_id=item.request_id)
    return result.accepted


def _cancel_interactions(
    approval_broker: Optional[_ApprovalBrokerPort],
    user_input_broker: Optional[UserInputBroker],
) -> None:
    if approval_broker is not None:
        approval_broker.cancel_all()
    if user_input_broker is not None:
        user_input_broker.cancel_all()


def _emit_interaction_ack(
    writer: _JsonlWriter,
    encoder: StreamJsonEncoder,
    item: StreamControlMessage,
    *,
    accepted: bool,
    detail: str = "",
) -> None:
    writer.emit({
        "type": "control",
        "subtype": f"{item.subtype}_ack",
        "request_id": item.request_id,
        "accepted": accepted,
        "detail": detail,
        "session_id": encoder.metadata.session_id,
    })


def _emit_idle_interaction_response(
    writer: _JsonlWriter,
    encoder: StreamJsonEncoder,
    item: StreamControlMessage,
) -> None:
    writer.emit({
        "type": "error",
        "subtype": "control",
        "error": "当前没有运行中的查询可处理交互响应",
        "interaction_subtype": item.subtype,
        "request_id": item.request_id,
        "session_id": encoder.metadata.session_id,
    })


async def _read_inputs(stream: TextIO, queue: asyncio.Queue[Any]) -> None:
    """逐行读取 stdin，不阻塞 Provider/MCP 等后台协程。"""
    try:
        while True:
            line = await asyncio.to_thread(stream.readline)
            if line == "":
                await queue.put(_INPUT_DONE)
                return
            if not line.strip():
                continue
            try:
                await queue.put(parse_input_line(line))
            except StreamJsonProtocolError as exc:
                await queue.put(exc)
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await queue.put(StreamJsonProtocolError(
            f"读取 stdin 失败: {type(exc).__name__}: {exc}"))


async def _next_item(queue: asyncio.Queue[Any], pending: deque[Any]) -> Any:
    if pending:
        return pending.popleft()
    return await queue.get()


async def _prepare_input(
    processor: Optional[InputProcessor],
    raw_prompt: str,
    *,
    log: Callable[[str], None],
) -> Optional[InputAction]:
    if processor is None:
        return None
    action = await processor.process(
        InputEnvelope(raw_prompt, source="stream-json"))
    for note in action.notes:
        log(f"[input] {note}")
    return action


async def _run_command_action(
    controller: SessionController,
    action: InputAction,
    message: StreamUserMessage,
    *,
    command_turn_id: str,
    writer: _JsonlWriter,
    encoder: StreamJsonEncoder,
) -> tuple[CommandResult, int]:
    captured: list[SessionEventPayload] = []

    def capture(event: AgentEvent) -> None:
        captured.append(as_session_event_payload(event))

    unsubscribe = controller.loop.bus.subscribe(capture)
    try:
        result = await controller.execute_command(action.text)
    finally:
        unsubscribe()

    sequence = 0
    extra = _request_extra(message) or None
    for event in captured:
        writer.emit(encoder.encode(
            QueryEvent(
                turn_id=command_turn_id,
                sequence=sequence,
                event=event,
            ),
            extra_fields=extra,
        ))
        sequence += 1
    writer.emit({
        "type": "command",
        "subtype": "result",
        "command": action.command,
        "handled": result.handled,
        "output": list(result.output),
        "run_prompt": result.run_prompt is not None,
        "command_source": (
            result.command_source.value
            if result.command_source is not None else None),
        "command_trust": result.command_trust or None,
        "reminder_ids": list(result.reminder_ids),
        "template_hash": result.template_hash or None,
        "turn_id": command_turn_id,
        "sequence": sequence,
        "session_id": encoder.metadata.session_id,
        **(extra or {}),
    })
    return result, sequence + 1


def _emit_idle_interrupt(
    writer: _JsonlWriter, encoder: StreamJsonEncoder,
) -> None:
    writer.emit({
        "type": "error",
        "subtype": "control",
        "error": "当前没有运行中的查询可中断",
        "session_id": encoder.metadata.session_id,
    })


def _emit_input_error(
    writer: _JsonlWriter,
    encoder: StreamJsonEncoder,
    error: StreamJsonProtocolError,
) -> None:
    writer.emit(encoder.result_message(
        subtype="error",
        result=f"stream-json 输入错误: {error}",
        is_error=True,
        extra={"code": "input_error"},
    ))


def _request_extra(message: StreamUserMessage) -> dict[str, str]:
    return {"request_id": message.request_id} if message.request_id else {}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _result_extra(usage: UsageTracker, *, duration_ms: int) -> dict[str, Any]:
    return {
        "usage": _usage_payload(usage.turn),
        "session_usage": _usage_payload(usage.session),
        "cost_usd": usage.turn_cost_usd,
        "session_cost_usd": usage.cost_usd,
        "num_turns": usage.turn.calls,
        "duration_ms": duration_ms,
    }


def _usage_payload(totals: UsageTotals) -> dict[str, int]:
    return {
        "calls": totals.calls,
        "input": totals.input,
        "output": totals.output,
        "total": totals.total,
        "cache_read": totals.cache_read,
        "cache_creation": totals.cache_creation,
    }


def _mcp_server_names(runtime: AgentRuntime) -> tuple[str, ...]:
    supervisor = runtime.mcp_supervisor
    if supervisor is None:
        return ()
    return tuple(str(item["name"]) for item in supervisor.status())


def _redact_value(value: Any, redactor: SecretRedactor) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, list):
        return [_redact_value(item, redactor) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, redactor) for item in value]
    if isinstance(value, dict):
        return {
            redactor.redact(str(key)): _redact_value(item, redactor)
            for key, item in value.items()
        }
    return value
