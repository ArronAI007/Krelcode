"""Hook 端口：Agent 内核只依赖这里的窄结果类型。

具体配置解析与子进程执行位于顶层 ``krelcode.hooks``，避免 agent 层反向
依赖应用组装逻辑。Hook 是增强/策略扩展点，不应让内核知道其实现方式。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Tuple, Union

from .messages import ToolCall


@dataclass(frozen=True)
class HookResult:
    """一次或一组 Hook 的可消费结果。"""

    matched: bool = False
    blocked: bool = False
    reason: str = ""
    context: Tuple[str, ...] = ()
    system_message: str = ""

    @classmethod
    def merge(cls, results: "list[HookResult] | tuple[HookResult, ...]") -> "HookResult":
        matched = any(result.matched for result in results)
        blocked_result = next((result for result in results if result.blocked), None)
        contexts = tuple(
            context
            for result in results
            for context in result.context
            if context
        )
        messages = [result.system_message for result in results if result.system_message]
        return cls(
            matched=matched,
            blocked=blocked_result is not None,
            reason=blocked_result.reason if blocked_result is not None else "",
            context=contexts,
            system_message="\n".join(messages),
        )


class HookPort(Protocol):
    """执行前/后 Hook 端口；失败由实现负责 fail-open。"""

    async def before_tool(self, tool_call: ToolCall) -> HookResult:
        ...

    async def after_tool(
        self,
        tool_call: ToolCall,
        content: str,
        is_error: bool,
    ) -> HookResult:
        ...

    async def run_lifecycle(
        self,
        trigger: str,
        variables: dict[str, str],
    ) -> HookResult:
        ...
