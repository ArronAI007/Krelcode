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
from itertools import count
from typing import Dict, Optional, Set

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
    request_id: str = ""


class ApprovalBroker:
    """异步任务间审批 rendezvous。

    同时只允许一个前台审批项。REPL 在没有 pending 请求时等待
    ``next_request()``；安全策略则可在任意工具任务中 ``await request(...)``。
    """

    def __init__(self) -> None:
        self._requests: asyncio.Queue[Optional[ApprovalRequest]] = asyncio.Queue()
        self._by_id: Dict[str, ApprovalRequest] = {}
        self._early: Dict[str, str] = {}
        self._expected: Set[str] = set()
        self._cancelled: Set[str] = set()
        self._counter = count(1)
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
        request_id = str(tool_call.id or "").strip()
        if not request_id:
            request_id = f"approval-{next(self._counter)}"
        # The policy event is emitted before the approver call. If the turn is
        # interrupted in that window, do not let the late approver register a
        # request that can no longer be answered by the editor.
        if request_id in self._cancelled:
            self._cancelled.discard(request_id)
            return NO
        self.expect(request_id)
        future = asyncio.get_running_loop().create_future()
        approval = ApprovalRequest(tool_call, reason, future, request_id)
        self._by_id[request_id] = approval
        early = self._early.pop(request_id, None)
        if early is not None:
            future.set_result(early)
        else:
            await self._requests.put(approval)
        try:
            return await future
        finally:
            if self._pending is approval:
                self._pending = None
            self._by_id.pop(request_id, None)
            self._expected.discard(request_id)

    def expect(self, request_id: str) -> None:
        """登记已向前台发布的审批 ID，限制无来源的提前响应。"""
        key = (request_id or "").strip()
        if not key:
            return
        if len(self._expected) >= 256:
            self._expected.pop()
        self._expected.add(key)

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
        return self._resolve_request(request, raw)

    def resolve_for(self, request_id: str, raw: str) -> Optional[str]:
        """按一次性 request_id 提交；支持响应先于 approver 注册的竞态。"""
        key = (request_id or "").strip()
        if not key:
            return None
        answer = parse_approval_input(raw)
        request = self._by_id.get(key)
        if request is None:
            if key not in self._expected:
                return None
            # stdout 事件可能先于 SandboxPolicy 调用 approver 到达编辑器。
            # 只缓存合法且唯一的首个答案，等 request() 注册后立即消费。
            if key not in self._early and len(self._early) < 64:
                self._early[key] = answer
                self._expected.discard(key)
            return answer
        if request.future.done():
            return None
        return self._resolve_request(request, answer, normalized=True)

    def _resolve_request(
        self,
        request: ApprovalRequest,
        raw: str,
        *,
        normalized: bool = False,
    ) -> Optional[str]:
        answer = raw if normalized else parse_approval_input(raw)
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
        pending = list(self._by_id.values())
        late_ids = self._expected.union(self._early).difference(self._by_id)
        self._by_id.clear()
        self._early.clear()
        # Keep the IDs that were already announced but whose approver has not
        # registered yet; a late request gets fail-closed NO exactly once.
        self._cancelled.update(late_ids)
        while len(self._cancelled) > 256:
            self._cancelled.pop()
        self._expected.clear()
        for request in pending:
            if not request.future.done():
                request.future.set_result(NO)
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
            raw = await asyncio.to_thread(input, "  允许执行? [y]是 / [n]否 / [a]始终允许此命令: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return NO
        return parse_approval_input(raw)

    return approve
