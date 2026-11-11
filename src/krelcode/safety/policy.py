"""ApprovalPolicy 端口与实现。

- AllowAllPolicy：全放行（--no-sandbox / 测试用）
- SandboxPolicy（Step 6）：
  · 只读工具直接放行；文件写工具已被工作区路径约束兜底，放行
  · run_command 走 safety.commands 风险分级：DENY 拒绝 / ALLOW 放行 / ASK 审批
  · ASK：先查持久化 allow 规则 → auto_approve → 交互式 approver → 都没有则拒绝
  · 未知工具保守 ASK
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol, Union

from ..core.events import ApprovalRequested, EventBus
from ..core.messages import ToolCall
from ..tools.base import ToolContext
from .commands import RISK_ALLOW, RISK_DENY, classify_command
from .rules import AllowRules


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


ALLOW = Decision(True)


def deny(reason: str) -> Decision:
    return Decision(False, reason)


class ApprovalPolicy(Protocol):
    async def check(self, tool_call: ToolCall, ctx: ToolContext) -> Decision:
        ...


class AllowAllPolicy:
    """Step 2 占位策略：全部放行。真实沙箱见 Step 6。"""

    async def check(self, tool_call: ToolCall, ctx: ToolContext) -> Decision:
        return ALLOW


# 工具风险分类（与 tools/builtin 对应；文件写工具的路径约束在 tools/paths.py 兜底）
READONLY_TOOLS = {"get_time", "read_file", "grep", "glob"}
WORKSPACE_WRITE_TOOLS = {"write_file", "edit_file"}
SHELL_TOOLS = {"run_command"}

# approver：async (tool_call, reason) -> "yes" | "no" | "always"
Approver = Callable[[ToolCall, str], Union[str, Awaitable[str]]]


class SandboxPolicy:
    """规则分级 + 持久化 allow 规则 + 交互审批（fail-closed）。"""

    def __init__(
        self,
        approver: Optional[Approver] = None,
        rules_path: Optional[Path] = None,
        bus: Optional[EventBus] = None,
        auto_approve: bool = False,
    ) -> None:
        self.approver = approver
        self.rules = AllowRules(rules_path)
        self.bus = bus
        self.auto_approve = auto_approve

    async def check(self, tool_call: ToolCall, ctx: ToolContext) -> Decision:
        name = tool_call.name
        if name in READONLY_TOOLS or name in WORKSPACE_WRITE_TOOLS:
            return ALLOW
        if name in SHELL_TOOLS:
            return await self._check_command(tool_call)
        # 未知/未来新工具：保守 ASK
        return await self._ask(tool_call, f"未分级工具 {name}，保守请求审批")

    async def _check_command(self, tool_call: ToolCall) -> Decision:
        command = str(tool_call.arguments.get("command", ""))
        risk, reason = classify_command(command)
        if risk == RISK_DENY:
            return deny(reason)
        if risk == RISK_ALLOW:
            return ALLOW
        return await self._ask(tool_call, reason, command=command)

    async def _ask(self, tool_call: ToolCall, reason: str, command: Optional[str] = None) -> Decision:
        # 1) 持久化规则命中
        if command is not None and self.rules.matches(command):
            return Decision(True, f"命中已存 allow 规则: {reason}")
        # 2) 全自动批准模式
        if self.auto_approve:
            return Decision(True, f"--auto-approve 自动批准: {reason}")
        # 3) 交互审批
        if self.bus is not None:
            await self.bus.emit(ApprovalRequested(tool_call, reason))
        if self.approver is None:
            return deny(f"非交互模式且无审批器，已拒绝（{reason}）")
        answer = await self.approver(tool_call, reason)
        if inspect.isawaitable(answer):
            answer = await answer
        if answer == "always":
            if command is not None:
                self.rules.add(command)
            return Decision(True, f"用户批准（always）: {reason}")
        if answer == "yes":
            return Decision(True, f"用户批准: {reason}")
        return deny(f"用户拒绝（{reason}）")
