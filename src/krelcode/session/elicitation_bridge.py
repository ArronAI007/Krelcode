"""MCP elicitation 与会话用户输入 broker 的窄桥接层。

MCP 层只理解协议请求；终端只理解 UserInputBroker。本桥接把一次
``elicitation/create`` 映射为普通前台输入请求，并在答案返回前完成本地
Schema 校验。没有前台 broker 时不会创建本对象，MCPClient 会 fail-closed。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Optional

from ..core.events import UserInputRequested, UserInputResolved
from ..core.redact import SecretRedactor
from ..mcp.elicitation import (
    ElicitationRequest,
    ElicitationResult,
    ElicitationValidationError,
    parse_elicitation_answer,
    validate_answer_text,
)
from .interactions import UserInputBroker


EventEmitter = Callable[[Any], Awaitable[None]]


class MCPElicitationBridge:
    def __init__(
        self,
        broker: UserInputBroker,
        emit: EventEmitter,
        *,
        redactor: Optional[SecretRedactor] = None,
    ) -> None:
        self._broker = broker
        self._emit = emit
        self._redactor = redactor
        self._counter = 0

    async def __call__(self, request: ElicitationRequest) -> ElicitationResult:
        self._counter += 1
        request_id = f"mcp:elicitation:{self._counter}"
        prompt = request.prompt_text(redactor=self._redactor)
        if self._redactor is not None:
            prompt = self._redactor.redact(prompt)
        options = (
            ("accept", "decline", "cancel")
            if not request.fields else ("decline", "cancel")
        )
        exchange = self._broker.submit(
            prompt,
            options,
            allow_free_text=True,
            request_id=request_id,
            validator=lambda raw: validate_answer_text(request, raw),
        )
        await self._emit(UserInputRequested(
            request_id=exchange.request_id,
            prompt=prompt,
            options=options,
            allow_free_text=True,
        ))
        answered = False
        try:
            raw = await exchange.wait()
            if raw is None:
                return ElicitationResult(action="cancel")
            try:
                result = parse_elicitation_answer(request, raw)
            except ElicitationValidationError:
                # 正常路径由 broker validator 拦住；这里处理 broker 取消与
                # 实现替换之间的竞态，绝不把非法内容发送给服务器。
                return ElicitationResult(action="cancel")
            answered = result.action == "accept"
            return result
        except asyncio.CancelledError:
            exchange.cancel()
            raise
        finally:
            await self._emit(UserInputResolved(
                request_id=exchange.request_id,
                answered=answered,
            ))


__all__ = ["MCPElicitationBridge"]
