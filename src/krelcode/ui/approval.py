"""控制台审批器：SandboxPolicy 的 approver 实现。

ASK 时向终端展示命令与风险原因，等待 y/n/a。
输入 EOF（管道/非终端）或中断 → 按 "no" 处理（fail-closed）。
"""

from __future__ import annotations

import asyncio

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
