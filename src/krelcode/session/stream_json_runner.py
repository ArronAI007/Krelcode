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
from pathlib import Path
from typing import Any, Optional, TextIO

from ..agent.runtime import AgentRuntime
from ..agent.usage import UsageTotals, UsageTracker
from ..core.events import ShellJobFinished, ShellJobOutput, ShellJobStarted
from ..core.redact import SecretRedactor
from .input import InputEnvelope, InputKind, InputProcessor
from .stream_json import (
    StreamControlMessage,
    StreamJsonEncoder,
    StreamJsonMetadata,
    StreamJsonProtocolError,
    StreamUserMessage,
    dumps_message,
    parse_input_line,
)


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
    log=print,
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

    def on_job_event(event) -> None:
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
                    reason = "control_end"
                    break
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
    input_queue: asyncio.Queue,
    input_processor: Optional[InputProcessor],
    usage: UsageTracker,
    writer: _JsonlWriter,
    encoder: StreamJsonEncoder,
    log,
) -> _TurnOutcome:
    """执行一个 turn；同时监听 interrupt/end，并保留后续 user 消息顺序。"""
    started = time.monotonic()
    try:
        prepared = await _prepare_prompt(
            input_processor, message.content, log=log)
        images = list(message.images) or (
            list(prepared.images) if prepared is not None else None)
        prompt = prepared.text if prepared is not None else message.content
        query = runtime.start_query(prompt, images=images or None)
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
        runtime, query, input_queue, outcome))

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
    query,
    input_queue: asyncio.Queue,
    outcome: _TurnOutcome,
) -> None:
    """在 turn 运行期间消费控制消息，user 消息延后但保持顺序。"""
    while not query.task.done():
        item = await input_queue.get()
        if isinstance(item, StreamUserMessage) or item is _INPUT_DONE:
            outcome.deferred.append(item)
            if item is _INPUT_DONE:
                return
            continue
        if isinstance(item, StreamJsonProtocolError):
            outcome.deferred.append(item)
            await runtime.stop()
            return
        if isinstance(item, StreamControlMessage):
            if item.subtype == "interrupt":
                await runtime.stop()
                return
            if item.subtype == "end":
                # 优雅关闭：当前 turn 及 end 之前已到达的 user 消息都完成，
                # 外层读到这个 end 后退出。立即中止应先发送 interrupt。
                outcome.deferred.append(item)
                return


async def _read_inputs(stream: TextIO, queue: asyncio.Queue) -> None:
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


async def _next_item(queue: asyncio.Queue, pending: deque[Any]) -> Any:
    if pending:
        return pending.popleft()
    return await queue.get()


async def _prepare_prompt(
    processor: Optional[InputProcessor],
    raw_prompt: str,
    *,
    log,
):
    if processor is None:
        return None
    action = await processor.process(
        InputEnvelope(raw_prompt, source="stream-json"))
    if action.kind is not InputKind.PROMPT:
        return None
    for note in action.notes:
        log(f"[input] {note}")
    return action


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
