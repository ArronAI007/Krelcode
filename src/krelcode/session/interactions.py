"""会话级用户输入 rendezvous。

与审批 broker 一样，本模块不读取 stdin、不导入 UI。REPL 订阅输入请求并在
前台显示，工具任务则在 Future 上等待答案。broker 关闭、任务取消或输入流
结束时统一返回 ``None``，保证 headless/子代理路径 fail-closed 且不会挂死。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from ..core.interactions import UserInputExchangePort


@dataclass(frozen=True)
class InputResolution:
    """一次输入提交的结果；拒绝无效答案时保留当前请求。"""

    accepted: bool
    value: Optional[str] = None
    reason: str = ""


@dataclass
class UserInputRequest:
    request_id: str
    prompt: str
    options: Tuple[str, ...]
    allow_free_text: bool
    future: asyncio.Future[Optional[str]]


class UserInputExchange(UserInputExchangePort):
    def __init__(
        self,
        request: UserInputRequest,
        broker: "UserInputBroker",
    ) -> None:
        self.request = request
        self._broker = broker

    @property
    def request_id(self) -> str:
        return self.request.request_id

    async def wait(self) -> Optional[str]:
        try:
            return await self.request.future
        except asyncio.CancelledError:
            self.cancel()
            raise

    def cancel(self) -> None:
        self._broker.cancel_request(self.request_id)


class UserInputBroker:
    """同时只展示一个输入请求，其余请求按提交顺序排队。"""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Optional[UserInputRequest]] = asyncio.Queue()
        self._requests: Dict[str, UserInputRequest] = {}
        self._pending: Optional[UserInputRequest] = None
        self._closed = False
        self._counter = 0

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> Optional[UserInputRequest]:
        return self._pending

    def submit(
        self,
        prompt: str,
        options: Tuple[str, ...] = (),
        *,
        allow_free_text: bool = True,
        request_id: str = "",
    ) -> UserInputExchange:
        """同步登记请求，让调用方可在 await 前安全发布事件。"""
        normalized_options = tuple(
            str(option).strip() for option in options if str(option).strip())
        if self._closed:
            future = asyncio.get_running_loop().create_future()
            future.set_result(None)
            request = UserInputRequest(
                request_id=request_id or self._next_id(),
                prompt=str(prompt),
                options=normalized_options,
                allow_free_text=bool(allow_free_text),
                future=future,
            )
            return UserInputExchange(request, self)

        resolved_id = request_id.strip() or self._next_id()
        if resolved_id in self._requests:
            raise ValueError(f"重复的用户输入 request_id: {resolved_id}")
        future = asyncio.get_running_loop().create_future()
        request = UserInputRequest(
            request_id=resolved_id,
            prompt=str(prompt),
            options=normalized_options,
            allow_free_text=bool(allow_free_text),
            future=future,
        )
        self._requests[resolved_id] = request
        self._queue.put_nowait(request)
        return UserInputExchange(request, self)

    async def request(
        self,
        prompt: str,
        options: Tuple[str, ...] = (),
        *,
        allow_free_text: bool = True,
        request_id: str = "",
    ) -> Optional[str]:
        """便捷入口；AgentLoop 使用 ``submit`` 以便先发布事件再等待。"""
        exchange = self.submit(
            prompt,
            options,
            allow_free_text=allow_free_text,
            request_id=request_id,
        )
        return await exchange.wait()

    async def next_request(self) -> Optional[UserInputRequest]:
        """等待下一个待展示请求；关闭时返回 ``None``。"""
        while True:
            request = await self._queue.get()
            if request is None:
                return None
            if self._closed or request.future.done():
                self._requests.pop(request.request_id, None)
                continue
            if self._pending is not None and not self._pending.future.done():
                raise RuntimeError("已有待处理的用户输入请求")
            self._pending = request
            return request

    def resolve(
        self,
        raw: str,
        *,
        request_id: str = "",
    ) -> InputResolution:
        """提交答案；无效选项不会关闭请求，便于前台提示后重试。"""
        request = (
            self._requests.get(request_id) if request_id
            else self._pending
        )
        if request is None:
            return InputResolution(False, reason="没有待处理的用户输入请求")
        if request.future.done():
            return InputResolution(False, reason="用户输入请求已结束")

        accepted, value, reason = _normalize_answer(request, raw)
        if not accepted:
            return InputResolution(False, reason=reason)
        request.future.set_result(value)
        self._requests.pop(request.request_id, None)
        if self._pending is request:
            self._pending = None
        return InputResolution(True, value=value)

    def resolve_pending(self, raw: str) -> InputResolution:
        return self.resolve(raw)

    def cancel_request(self, request_id: str) -> bool:
        request = self._requests.pop(request_id, None)
        if request is None:
            return False
        if not request.future.done():
            request.future.set_result(None)
        if self._pending is request:
            self._pending = None
        return True

    def cancel_pending(self) -> bool:
        if self._pending is None:
            return False
        return self.cancel_request(self._pending.request_id)

    def cancel_all(self) -> None:
        """取消当前及排队请求；所有等待方都得到 ``None``。"""
        requests = list(self._requests.values())
        self._requests.clear()
        self._pending = None
        for request in requests:
            if not request.future.done():
                request.future.set_result(None)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cancel_all()
        self._queue.put_nowait(None)

    def _next_id(self) -> str:
        self._counter += 1
        return f"input-{self._counter}"


def _normalize_answer(
    request: UserInputRequest,
    raw: str,
) -> tuple[bool, Optional[str], str]:
    text = (raw or "").strip()
    if not text:
        return False, None, "输入不能为空"

    options = request.options
    if options:
        if text in options:
            return True, text, ""
        try:
            index = int(text)
        except ValueError:
            index = 0
        if index:
            if 1 <= index <= len(options):
                return True, options[index - 1], ""
            return False, None, f"选项编号必须在 1-{len(options)} 之间"
        if not request.allow_free_text:
            return False, None, "请输入选项编号或完整选项"
    return True, text, ""
