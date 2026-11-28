"""User-input waiting bridge used by tool execution."""

from __future__ import annotations

from typing import Optional

from krelcode.contracts.loop import WaitCondition, WaitKind
from krelcode.core.events import UserInputRequested, UserInputResolved
from .dependencies import AgentDependencies
from .observation import user_input_observation
from .runtime_ports import ToolTurnHostPort


async def request_tool_user_input(
    host: ToolTurnHostPort,
    dependencies: AgentDependencies,
    prompt: str,
    options: tuple[str, ...],
    allow_free_text: bool,
) -> Optional[str]:
    port = dependencies.user_input_port
    if port is None:
        return None
    redactor = dependencies.redactor
    safe_prompt = redactor.redact(prompt) if redactor else prompt
    safe_options = tuple(
        redactor.redact(option) if redactor else option
        for option in options
    )
    turn_id = host._turn_tool_context.runtime.turn_id or "turn"
    request_id = f"{turn_id}:input:{next(host._input_counter)}"
    exchange = port.submit(
        safe_prompt,
        safe_options,
        allow_free_text=allow_free_text,
        request_id=request_id,
    )
    await host._emit(UserInputRequested(
        request_id=exchange.request_id,
        prompt=safe_prompt,
        options=safe_options,
        allow_free_text=allow_free_text,
    ))
    await host._enter_waiting(WaitCondition(
        kind=WaitKind.USER_INPUT,
        request_id=exchange.request_id,
        reason=safe_prompt,
    ))
    try:
        answer = await exchange.wait()
    finally:
        await host._resume_waiting(exchange.request_id)
    await host._emit(UserInputResolved(
        request_id=exchange.request_id,
        answered=answer is not None,
    ))
    await host._record_observation(user_input_observation(
        turn_id=turn_id,
        request_id=exchange.request_id,
        answered=answer is not None,
        answer_chars=len(answer or ""),
    ))
    return answer


__all__ = ["request_tool_user_input"]
