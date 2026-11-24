"""Model-turn execution for the agent runtime.

The model executor resolves the current tool catalogue, asks the context
coordinator for one exact request, streams provider events, starts tools as
soon as their arguments are complete, and returns a completed turn result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from ..core.events import (
    LLMResponseReceived,
    LLMStreamDelta,
    LLMThinkingDelta,
)
from ..core.messages import Message
from ..contracts.tool import ToolResult
from ..llm.base import (
    FINISH_LENGTH,
    FINISH_TOOLS,
    StreamFinished,
    TextDelta,
    ThinkingDelta,
    ToolCallReady,
    supports_keyword_argument,
)
from .context_turn import ContextTurnCoordinator
from .dependencies import AgentDependencies
from .tool_turn import ToolBatchExecutor


@dataclass(frozen=True)
class LLMTurnResult:
    """One model response plus completed tool results, not yet in history."""

    message: Message
    tool_results: List[ToolResult]
    finish_reason: Optional[str] = None


def finish_reason_failure(
    finish_reason: Optional[str], *, has_tool_calls: bool,
) -> Optional[str]:
    """Convert an abnormal finish reason into a visible terminal error."""
    if finish_reason == FINISH_LENGTH:
        return (
            "模型响应达到输出长度上限，回答或工具参数可能不完整，本轮已中止。"
            "请缩小请求范围或提高模型 max_tokens 后重试。")
    if finish_reason == FINISH_TOOLS and not has_tool_calls:
        return "Provider 返回 finish_reason=tool_calls 但没有工具调用，协议不一致。"
    if finish_reason not in (None, "stop", FINISH_TOOLS, FINISH_LENGTH):
        return (
            f"Provider 以非正常原因结束响应: {finish_reason}。"
            "本轮结果可能被安全策略、内容过滤或上游服务截断。")
    return None


class ModelTurnExecutor:
    """Stream one provider response and bridge it to streamed tool execution."""

    def __init__(
        self,
        host: Any,
        dependencies: AgentDependencies,
        context: ContextTurnCoordinator,
        tools: ToolBatchExecutor,
    ) -> None:
        self.host = host
        self.dependencies = dependencies
        self.context = context
        self.tools = tools

    async def run(self, step: int) -> LLMTurnResult:
        host = self.host
        deps = self.dependencies
        capabilities = getattr(deps.llm, "capabilities", None)
        tool_pool = host._current_tool_pool()
        if capabilities is not None and not capabilities.supports_tools:
            tool_pool.clear_advertised()
            prompt_specs = []
            specs = []
        else:
            prompt_specs = tool_pool.prompt_specs()
            specs = tool_pool.tool_specs()
        tools = specs or None

        request = await self.context.prepare_model_request(
            step=step,
            prompt_specs=prompt_specs,
            specs=specs,
            tools=tools,
        )
        finished: Optional[StreamFinished] = None
        executor = self.tools.streaming_executor(tool_pool)
        if request.prepared is not None:
            stream = deps.llm.stream_prepared(request.prepared)
        else:
            capabilities = getattr(deps.llm, "capabilities", None)
            if (
                host.last_provider_messages is not None
                and getattr(
                    capabilities, "supports_evidence_channel", False)
                and supports_keyword_argument(deps.llm.stream, "evidence")
            ):
                stream = deps.llm.stream(
                    request.messages,
                    tools,
                    evidence=host.last_provider_messages.evidence_channel,
                )
            else:
                stream = deps.llm.stream(request.messages, tools)
        try:
            async for event in stream:
                if isinstance(event, TextDelta):
                    await host._emit(LLMStreamDelta(event.text))
                elif isinstance(event, ThinkingDelta):
                    await host._emit(LLMThinkingDelta(event.text))
                elif isinstance(event, ToolCallReady):
                    executor.register(event.index, event.tool_call)
                elif isinstance(event, StreamFinished):
                    finished = event
            if finished is None:
                raise RuntimeError("LLMProvider 未产出 StreamFinished")

            final_calls = list(finished.message.tool_calls or [])
            executor.register_final(final_calls)

            host.history.append(finished.message)
            usage = dict(finished.usage)
            host._token_total += usage.get("total_tokens") or (
                usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
            await host._emit(LLMResponseReceived(finished.message, usage))

            results = (
                await executor.wait_results(len(final_calls))
                if final_calls else []
            )
            return LLMTurnResult(
                message=finished.message,
                tool_results=results,
                finish_reason=finished.finish_reason,
            )
        except BaseException:
            await executor.abort()
            raise


__all__ = [
    "LLMTurnResult",
    "ModelTurnExecutor",
    "finish_reason_failure",
]
