"""AgentLoop：整个系统的心脏。

只依赖四个端口：LLMProvider / ToolRegistry / ApprovalPolicy / EventBus。
语义见 DESIGN.md「主循环语义」。Step 5 版本：
- 同批只读 tool_calls 并行、写操作串行，结果按原序回填
- 卡死检测：同一签名（工具名+参数）连续失败达阈值 → 中止本轮
- 最大步数守卫
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from itertools import count
from typing import Callable, Dict, List, Optional

from ..core.redact import SecretRedactor
from ..core.events import (
    AgentEvent,
    ContextCompressed,
    ErrorOccurred,
    EventBus,
    LLMResponseReceived,
    LLMStreamDelta,
    LLMThinkingDelta,
    PermissionDenied,
    RunCancelled,
    RunFinished,
    RunStarted,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    UserSteered,
)
from ..core.textutil import truncate_middle
from ..core.messages import (
    INVALID_ARGUMENTS_KEY, Message, SteeringInput, ToolCall, sanitize_history,
)
from ..llm.base import (
    LLMProvider,
    StreamFinished,
    TextDelta,
    ThinkingDelta,
    ToolCallReady,
)
from .compression import ContextManager
from .intent import TurnIntent, infer_turn_intent
from .streaming_tools import StreamingToolExecutor
from .tokens import estimate_history_tokens
from .tool_pool import TurnToolPool
from ..safety.policy import ApprovalPolicy
from ..tools.base import RuntimeSnapshot, ToolContext, ToolResult, TurnAbortSignal
from ..tools.registry import ToolRegistry

DEFAULT_MAX_STEPS = 64


def max_steps_reached_text(max_steps: int) -> str:
    """步数守卫中止说明：同时给出原因和可操作的调整方式。"""
    return (
        f"[krelcode] 工具循环已达到单轮 {max_steps} 个模型步骤的安全上限，"
        f"本轮已中止。可用 --max-steps {max_steps * 2} 提高上限后重试。"
    )


INTERRUPTED_NOTE = "[已被用户中止]"
STEERING_PREFIX = "[用户插话] "


@dataclass(frozen=True)
class LLMTurnResult:
    """一次模型响应及其已完成的工具结果（结果尚未写入历史）。"""

    message: Message
    tool_results: List[ToolResult]


class AgentLoop:
    def __init__(
        self,
        llm: LLMProvider,
        registry: ToolRegistry,
        tool_context: ToolContext,
        policy: Optional[ApprovalPolicy] = None,
        bus: Optional[EventBus] = None,
        system_prompt: str = "",
        max_steps: int = DEFAULT_MAX_STEPS,
        repeat_failure_limit: int = 3,
        context_manager: Optional[ContextManager] = None,
        tool_output_inline_limit: int = 12_000,
        token_budget: Optional[int] = None,
        redactor: Optional[SecretRedactor] = None,
        runtime_snapshot_provider: Optional[
            Callable[[RuntimeSnapshot], RuntimeSnapshot]
        ] = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.tool_context = tool_context
        self.policy = policy
        self.bus = bus or EventBus()
        self.max_steps = max_steps
        self.repeat_failure_limit = repeat_failure_limit
        self.context_manager = context_manager
        self.tool_output_inline_limit = tool_output_inline_limit
        self.token_budget = token_budget
        self.redactor = redactor
        self.runtime_snapshot_provider = runtime_snapshot_provider
        self._token_total = 0
        self._failure_counts: Dict[str, int] = {}
        self._turn_intent = TurnIntent()
        self._active_tool_pool: Optional[TurnToolPool] = None
        self._turn_tool_context = tool_context
        self._turn_abort_signal = TurnAbortSignal()
        self._turn_counter = count(1)
        self._turn_prepared = False
        # 运行中用户插话队列（REPL steering）：每个 step 开始前注入历史
        self.steering_queue: asyncio.Queue = asyncio.Queue()
        self.history: List[Message] = (
            [Message.system(system_prompt)] if system_prompt else []
        )

    async def _emit(self, event: AgentEvent) -> None:
        await self.bus.emit(event)

    def reset(self) -> None:
        """清空历史，只保留系统提示词。"""
        self.history = self.history[:1] if self.history and self.history[0].role == "system" else []

    # -- 对外主入口 -----------------------------------------------------------

    async def run_turn(
        self,
        user_input: str,
        images: Optional[list] = None,
        *,
        turn_id: Optional[str] = None,
    ) -> str:
        """QueryEngine 的窄入口；允许 runner 替换 ``run`` 时仍传入 turn id。"""
        self._begin_turn(turn_id)
        self._turn_prepared = True
        try:
            return await self.run(user_input, images=images)
        finally:
            self._turn_prepared = False

    async def run(
        self,
        user_input: str,
        images: Optional[list] = None,
        *,
        turn_id: Optional[str] = None,
    ) -> str:
        """处理一轮用户输入：内部可能经历多次 LLM 调用与工具执行。

        images: 可选 ImageRef 列表（@图片附件），挂在 user 消息上由
        provider adapter 转成各自的多模态 wire 格式。
        被 cancel 时（REPL /stop）：消毒历史（弹掉不配对的尾部消息），
        若结尾是未答完的 user 消息则补一条中止说明（保角色交替），再重抛。
        """
        if not self._turn_prepared:
            self._begin_turn(turn_id)
        self.history.append(Message.user(user_input, images=images or None))
        self._failure_counts = {}
        self._token_total = 0
        self._turn_intent = infer_turn_intent(
            self.tool_context.workspace_root, user_input)
        # 一轮只构建一次：后续每个模型 step 和每次工具执行共用同一份能力快照。
        self._active_tool_pool = TurnToolPool(
            self.registry, intent=self._turn_intent)
        await self._emit(RunStarted(user_input))

        try:
            for step in range(1, self.max_steps + 1):
                await self._inject_steering()
                if self.token_budget is not None and self._token_total > self.token_budget:
                    text = (f"[krelcode] token 预算耗尽（已用 {self._token_total} > "
                            f"预算 {self.token_budget}），中止本轮。")
                    self.history.append(Message.assistant(text))
                    await self._emit(RunFinished(text))
                    return text
                await self._emit(StepStarted(step))
                llm_turn = await self._llm_turn()
                assistant = llm_turn.message
                if not assistant.tool_calls:
                    final_text = assistant.content or ""
                    await self._emit(RunFinished(final_text))
                    return final_text
                abort_reason = await self._apply_tool_results(
                    assistant.tool_calls, llm_turn.tool_results)
                if abort_reason:
                    text = f"[krelcode] {abort_reason}，判定为循环，已中止本轮。"
                    self.history.append(Message.assistant(text))
                    await self._emit(RunFinished(text))
                    return text
        except asyncio.CancelledError:
            self._turn_abort_signal.cancel()
            self._repair_after_cancel()
            await self._emit(RunCancelled())
            raise

        text = max_steps_reached_text(self.max_steps)
        self.history.append(Message.assistant(text))
        await self._emit(RunFinished(text))
        return text

    def _begin_turn(self, turn_id: Optional[str]) -> None:
        resolved_turn_id = turn_id or f"turn-{next(self._turn_counter)}"
        self._turn_abort_signal = TurnAbortSignal()
        snapshot = self.tool_context.runtime
        if self.runtime_snapshot_provider is not None:
            snapshot = self.runtime_snapshot_provider(snapshot)
        snapshot = replace(snapshot, turn_id=resolved_turn_id)
        self._turn_tool_context = replace(
            self.tool_context,
            runtime=snapshot,
            abort=self._turn_abort_signal,
        )

    def abort_current_turn(self, reason: str = "用户中止") -> None:
        """向正在执行的工具广播协作式取消；Task.cancel 仍由 Runtime 负责。"""
        self._turn_abort_signal.cancel(reason)

    async def _inject_steering(self) -> None:
        """把插话队列里的消息注入历史（每个 step 开始前调用）。

        尾部若是 user 消息则合并（防连续 user——Anthropic 要求角色交替）；
        尾部是 tool/assistant 时作为新 user 消息追加（合法的交替点）。
        """
        while not self.steering_queue.empty():
            try:
                item = self.steering_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, SteeringInput):
                text = item.text.strip()
                images = list(item.images)
            else:
                text = str(item).strip()
                images = []
            if not text:
                continue
            if self.history and self.history[-1].role == "user":
                last = self.history[-1]
                last.content = (
                    (last.content or "") + "\n" + STEERING_PREFIX + text)
                if images:
                    last.images = list(last.images or []) + images
            else:
                self.history.append(
                    Message.user(STEERING_PREFIX + text, images=images or None))
            await self._emit(UserSteered(text))

    def _repair_after_cancel(self) -> None:
        sanitize_history(self.history)
        if self.history and self.history[-1].role == "user":
            self.history.append(Message.assistant(INTERRUPTED_NOTE))

    # -- 内部：一次 LLM 调用 ---------------------------------------------------

    async def _llm_turn(self) -> LLMTurnResult:
        if self.context_manager is not None:
            before = estimate_history_tokens(self.history)
            if await self.context_manager.maybe_compress(self.history):
                await self._emit(
                    ContextCompressed(before, estimate_history_tokens(self.history))
                )
        # 能力协商：provider 声明不支持 tools 时不 advertise，避免 400。
        # 无 capabilities 属性的测试替身保持旧行为（照常传 tools）。
        capabilities = getattr(self.llm, "capabilities", None)
        if capabilities is not None and not capabilities.supports_tools:
            tools = None
        else:
            specs = self._current_tool_pool().tool_specs()
            tools = specs or None
        messages = self._messages_for_turn()
        finished: Optional[StreamFinished] = None
        executor = StreamingToolExecutor(
            self._current_tool_pool(), self._run_stream_tool)
        try:
            async for event in self.llm.stream(messages, tools):
                if isinstance(event, TextDelta):
                    await self._emit(LLMStreamDelta(event.text))
                elif isinstance(event, ThinkingDelta):
                    await self._emit(LLMThinkingDelta(event.text))
                elif isinstance(event, ToolCallReady):
                    # 参数已完整：无需等后续文本/其他工具参数，立刻进入调度。
                    executor.register(event.index, event.tool_call)
                elif isinstance(event, StreamFinished):
                    finished = event
            if finished is None:  # provider 违反契约
                raise RuntimeError("LLMProvider 未产出 StreamFinished")

            # 旧 Provider 可能只在最终消息里给出 tool_calls；统一补齐。
            final_calls = list(finished.message.tool_calls or [])
            executor.register_final(final_calls)

            # 必须先提交 assistant(tool_use) 再追加 tool_result，否则 wire
            # 历史会出现无来源的结果。工具可在此前已执行完并推送 UI 事件。
            self.history.append(finished.message)
            usage = dict(finished.usage)
            self._token_total += usage.get("total_tokens") or (
                usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
            await self._emit(LLMResponseReceived(finished.message, usage))

            results = (
                await executor.wait_results(len(final_calls))
                if final_calls else []
            )
            return LLMTurnResult(message=finished.message, tool_results=results)
        except BaseException:
            await executor.abort()
            raise

    def _current_tool_pool(self) -> TurnToolPool:
        if self._active_tool_pool is None:
            self._active_tool_pool = TurnToolPool(
                self.registry, intent=self._turn_intent)
        return self._active_tool_pool

    def _messages_for_turn(self) -> List[Message]:
        """临时注入本轮工具目录与意图，不污染持久历史。"""
        hints = [
            self._current_tool_pool().system_hint(),
            self._turn_intent.system_hint(self.tool_context.workspace_root),
        ]
        hint = "\n\n".join(part for part in hints if part)
        if not hint:
            return self.history
        if self.history and self.history[0].role == "system":
            base = self.history[0].content or ""
            system = Message.system(base + "\n\n" + hint if base else hint)
            return [system, *self.history[1:]]
        return [Message.system(hint), *self.history]

    # -- 内部：执行一批工具调用 -------------------------------------------------

    async def _apply_tool_results(
        self,
        tool_calls: List[ToolCall],
        results: List[ToolResult],
    ) -> Optional[str]:
        """按只读并发/写操作串行执行一批工具调用，结果按原序回填历史。

        返回 None 表示继续；返回字符串表示触发卡死检测，应中止本轮。
        """
        for tool_call, result in zip(tool_calls, results):
            self.history.append(
                Message.tool_result(tool_call.id, result.content)
            )
            signature = _call_signature(tool_call)
            if result.is_error:
                self._failure_counts[signature] = self._failure_counts.get(signature, 0) + 1
                if self._failure_counts[signature] >= self.repeat_failure_limit:
                    return (
                        f"同一调用连续失败 {self._failure_counts[signature]} 次: "
                        f"{signature}"
                    )
            else:
                self._failure_counts.pop(signature, None)
        return None

    async def _run_stream_tool(self, tool_call: ToolCall) -> ToolResult:
        """流式调度器的 runner：执行一个调用并立即发布完成事件。"""
        await self._emit(ToolCallStarted(tool_call))
        result = await self._execute_one(tool_call)
        await self._publish_tool_result(tool_call, result)
        return result

    async def _publish_tool_result(
        self, tool_call: ToolCall, result: ToolResult,
    ) -> None:
        """工具一完成就脱敏、持久化并推送 UI 事件。"""
        content = self.redactor.redact(result.content) if self.redactor else result.content
        inline_content = self._persist_large_output(tool_call, content)
        result.content = inline_content
        await self._emit(
            ToolCallCompleted(tool_call, inline_content, result.is_error)
        )

    def _persist_large_output(self, tool_call: ToolCall, content: str) -> str:
        """超长工具输出全文落盘，历史里只留截断版 + 文件引用。"""
        if len(content) <= self.tool_output_inline_limit:
            return content
        try:
            out_dir = self.tool_context.workspace_root / ".krelcode" / "tool_outputs"
            out_dir.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in tool_call.name
            )
            call_id = tool_call.id or f"call{len(self.history)}"
            path = out_dir / f"{safe_name}_{call_id}.txt"
            path.write_text(content, encoding="utf-8")
        except OSError as e:
            # 落盘失败不阻断：退回纯截断
            return truncate_middle(content, self.tool_output_inline_limit) + (
                f"\n[注意: 输出落盘失败({e})，已就地截断]"
            )
        return (
            truncate_middle(content, self.tool_output_inline_limit)
            + f"\n[输出过长（{len(content)} 字符），全文已存盘: {path}]"
        )

    async def _execute_one(self, tool_call: ToolCall) -> ToolResult:
        # 1) 单轮工具池是执行边界：未暴露给模型的工具绝不能进入审批/执行。
        pool = self._current_tool_pool()
        tool = pool.get(tool_call.name)
        if tool is None:
            excluded = pool.excluded_reason(tool_call.name)
            if excluded is not None:
                return ToolResult(
                    content=(
                        f"工具在本轮不可用: {tool_call.name}（{excluded}；"
                        f"本轮可用工具: {', '.join(pool.names()) or '无'}）"
                    ),
                    is_error=True,
                )
            return ToolResult(
                content=(
                    f"未知工具: {tool_call.name}（本轮可用工具: "
                    f"{', '.join(pool.names()) or '无'}）"
                ),
                is_error=True,
            )

        execution_ctx = replace(
            self._turn_tool_context,
            tool_descriptor=pool.descriptor(tool_call.name),
        )

        # 2) 审批
        if self.policy is not None:
            try:
                decision = await self.policy.check(tool_call, execution_ctx)
            except Exception as e:  # 审批器自身出错 → 保守拒绝
                await self._emit(ErrorOccurred(str(e), "policy.check"))
                await self._emit(PermissionDenied(tool_call, str(e)))
                return ToolResult(content=f"审批检查失败，已拒绝执行: {e}", is_error=True)
            if not decision.allowed:
                await self._emit(PermissionDenied(tool_call, decision.reason))
                return ToolResult(
                    content=f"操作被安全策略拒绝: {decision.reason}", is_error=True
                )

        # 3) 参数校验（Step 2 只做最低限度：非法 JSON 透传标记）
        if tool_call.has_invalid_arguments:
            return ToolResult(
                content=f"工具参数不是合法 JSON: {tool_call.arguments[INVALID_ARGUMENTS_KEY]!r}",
                is_error=True,
            )
        args = dict(tool_call.arguments)

        # 4) 执行（异常 → is_error 结果回填，错误恢复原则）
        try:
            return await tool.execute(args, execution_ctx)
        except Exception as e:
            await self._emit(ErrorOccurred(f"{type(e).__name__}: {e}", tool_call.name))
            return ToolResult(content=f"工具执行出错: {type(e).__name__}: {e}", is_error=True)


def _call_signature(tool_call: ToolCall) -> str:
    """工具名 + 稳定序列化的参数，作为卡死检测的签名。"""
    try:
        args = json.dumps(tool_call.arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = repr(tool_call.arguments)
    if len(args) > 200:
        args = args[:200] + "…"
    return f"{tool_call.name}({args})"
