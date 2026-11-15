"""交互审批桥：把安全策略与终端输入循环解耦。

``ApprovalBroker`` 不直接读 stdin。策略调用 ``request`` 后等待 Future；REPL
通过 ``next_request`` 在前台展示审批项，再用 ``resolve`` 提交 y/n/a。这样
字符级编辑器（cbreak 模式）与审批输入不会同时争抢同一个文件描述符。

输入关闭、查询取消或 broker 被关闭时一律按 "no" 处理（fail-closed）。
``make_console_approver`` 仅为非 REPL 嵌入场景保留。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from ..core.messages import ToolCall

YES, NO, ALWAYS = "yes", "no", "always"


def parse_approval_input(raw: str) -> str:
    """归一化用户输入；空输入/无法识别一律按 no（保守）。"""
    token = (raw or "").strip().lower()
    if token in {"y", "yes"}:
        return YES
    if token in {"a", "always"}:
        return ALWAYS
    return NO


@dataclass
class ApprovalRequest:
    """一个等待 REPL 前台处理的审批请求。"""

    tool_call: ToolCall
    reason: str
    future: asyncio.Future[str]


class ApprovalBroker:
    """异步任务间审批 rendezvous。

    同时只允许一个前台审批项。REPL 在没有 pending 请求时等待
    ``next_request()``；安全策略则可在任意工具任务中 ``await request(...)``。
    """

    def __init__(self) -> None:
        self._requests: asyncio.Queue[Optional[ApprovalRequest]] = asyncio.Queue()
        self._pending: Optional[ApprovalRequest] = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> Optional[ApprovalRequest]:
        return self._pending

    async def request(self, tool_call: ToolCall, reason: str) -> str:
        """SandboxPolicy approver 入口；返回归一化的 yes/no/always。"""
        if self._closed:
            return NO
        future = asyncio.get_running_loop().create_future()
        approval = ApprovalRequest(tool_call, reason, future)
        await self._requests.put(approval)
        try:
            return await future
        finally:
            if self._pending is approval:
                self._pending = None

    async def next_request(self) -> Optional[ApprovalRequest]:
        """等待下一个审批项；broker 关闭时返回 None。"""
        request = await self._requests.get()
        if request is None:
            return None
        if self._closed:
            if not request.future.done():
                request.future.set_result(NO)
            return None
        if self._pending is not None and not self._pending.future.done():
            raise RuntimeError("已有待处理的审批请求")
        self._pending = request
        return request

    def resolve(self, raw: str) -> Optional[str]:
        """提交当前审批答案；没有 pending 项时返回 None。"""
        request = self._pending
        if request is None or request.future.done():
            return None
        answer = parse_approval_input(raw)
        request.future.set_result(answer)
        self._pending = None
        return answer

    def cancel_pending(self) -> bool:
        """将当前审批按拒绝处理；用于中断、退出和超时清理。"""
        request = self._pending
        if request is None or request.future.done():
            self._pending = None
            return False
        request.future.set_result(NO)
        self._pending = None
        return True

    def cancel_all(self) -> None:
        """拒绝当前及已排队但尚未展示的全部审批请求。"""
        self.cancel_pending()
        while True:
            try:
                request = self._requests.get_nowait()
            except asyncio.QueueEmpty:
                break
            if request is not None and not request.future.done():
                request.future.set_result(NO)

    def close(self) -> None:
        """关闭 broker；未完成审批 fail-closed。可重复调用。"""
        if self._closed:
            return
        self._closed = True
        self.cancel_all()
        self._requests.put_nowait(None)


def make_console_approver():
    """返回 async approver(tool_call, reason) -> yes/no/always。"""

    async def approve(tool_call: ToolCall, reason: str) -> str:
        command = tool_call.arguments.get("command")
        print(f"  [审批] {tool_call.name}: {command if command is not None else tool_call.arguments}")
        print(f"  原因: {reason}")
        try:
            raw = await asyncio.to_thread(input, "  允许执行? [y]是 / [n]否 / [a]始终允许: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return NO
        return parse_approval_input(raw)

    return approve
