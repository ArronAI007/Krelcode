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
from typing import Awaitable, Callable, Dict, Iterable, List, Optional

from ..core.hooks import HookPort, HookResult
from ..core.interactions import UserInputPort
from ..core.redact import SecretRedactor
from ..core.events import (
    AgentEvent,
    ContextCompressed,
    ErrorOccurred,
    EventBus,
    HookTriggered,
    LLMResponseReceived,
    LLMStreamDelta,
    LLMThinkingDelta,
    PermissionDenied,
    PlanUpdated,
    QueryPhaseChanged,
    RunCancelled,
    RunFinished,
    RunStarted,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    ToolOutputDelta,
    ToolProgress,
    UserInputRequested,
    UserInputResolved,
    UserSteered,
)
from ..core.textutil import truncate_middle
from ..core.messages import (
    INVALID_ARGUMENTS_KEY,
    ImageBlock,
    Message,
    ResourceBlock,
    SteeringInput,
    StructuredBlock,
    TextBlock,
    ToolCall,
    sanitize_history,
)
from ..llm.base import (
    FINISH_LENGTH,
    FINISH_TOOLS,
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
from ..tools.schema import validate_tool_arguments

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
    finish_reason: Optional[str] = None


def finish_reason_failure(
    finish_reason: Optional[str], *, has_tool_calls: bool,
) -> Optional[str]:
    """把异常/不兼容 finish_reason 转成可见终态错误；None 表示可继续。"""
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
        hook_port: Optional[HookPort] = None,
        user_input_port: Optional[UserInputPort] = None,
        step_gate: Optional[Callable[[], Awaitable[None]]] = None,
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
        self.hook_port = hook_port
        self.user_input_port = user_input_port
        # 通用协作式运行边界（子代理 pause 等）；主 Agent 默认为 None。
        self.step_gate = step_gate
        self.runtime_context: List[str] = []
        self._token_total = 0
        self._failure_counts: Dict[str, int] = {}
        self._turn_intent = TurnIntent()
        self._active_tool_pool: Optional[TurnToolPool] = None
        self._turn_tool_context = tool_context
        self._turn_abort_signal = TurnAbortSignal()
        self._turn_counter = count(1)
        self._current_turn_id = ""
        self._input_counter = count(1)
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

    def add_runtime_context(self, contents: Iterable[str], source: str = "hook") -> int:
        """注入不写入历史快照的运行时上下文（SessionStart/Compact Hook）。"""
        added = 0
        current_chars = sum(len(item) for item in self.runtime_context)
        for content in contents:
            text = str(content).strip()
            if not text or current_chars >= 16_000:
                continue
            remaining = 16_000 - current_chars
            clipped = text[:remaining]
            self.runtime_context.append(clipped)
            current_chars += len(clipped)
            added += 1
        return added

    # -- 对外主入口 -----------------------------------------------------------

    async def run_turn(
        self,
        user_input: str,
        images: Optional[list] = None,
        *,
        turn_id: Optional[str] = None,
        tool_allowlist: Optional[list[str]] = None,
    ) -> str:
        """QueryEngine 的窄入口；允许 runner 替换 ``run`` 时仍传入 turn id。"""
        self._begin_turn(turn_id, tool_allowlist=tool_allowlist)
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
        tool_allowlist: Optional[list[str]] = None,
    ) -> str:
        """处理一轮用户输入：内部可能经历多次 LLM 调用与工具执行。

        images: 可选 ImageRef 列表（@图片附件），挂在 user 消息上由
        provider adapter 转成各自的多模态 wire 格式。
        被 cancel 时（REPL /stop）：消毒历史（弹掉不配对的尾部消息），
        若结尾是未答完的 user 消息则补一条中止说明（保角色交替），再重抛。
        """
        if not self._turn_prepared:
            self._begin_turn(turn_id, tool_allowlist=tool_allowlist)
        self.history.append(Message.user(user_input, images=images or None))
        self._failure_counts = {}
        self._token_total = 0
        self._turn_intent = infer_turn_intent(
            self.tool_context.workspace_root, user_input)
        # 一轮只构建一次：后续每个模型 step 和每次工具执行共用同一份能力快照。
        self._active_tool_pool = TurnToolPool(
            self.registry,
            intent=self._turn_intent,
            runtime=self._turn_tool_context.runtime,
        )
        await self._emit(RunStarted(
            user_input,
            turn_id=self._current_turn_id,
            session_id=self._turn_tool_context.runtime.session_id or "",
        ))
        await self._emit(QueryPhaseChanged(
            "awaiting_model", "等待模型响应"))
        await self.run_lifecycle_hook(
            "user_prompt_submit",
            {"prompt": user_input[:12_000]},
        )

        try:
            for step in range(1, self.max_steps + 1):
                if self.step_gate is not None:
                    await self.step_gate()
                await self._inject_steering()
                if self.token_budget is not None and self._token_total > self.token_budget:
                    text = (f"[krelcode] token 预算耗尽（已用 {self._token_total} > "
                            f"预算 {self.token_budget}），中止本轮。")
                    self.history.append(Message.assistant(text))
                    await self._emit(RunFinished(
                        text, turn_id=self._current_turn_id))
                    return text
                await self._emit(StepStarted(step))
                await self._emit(QueryPhaseChanged(
                    "model", f"模型生成中（Step {step}）"))
                llm_turn = await self._llm_turn()
                assistant = llm_turn.message
                finish_failure = finish_reason_failure(
                    llm_turn.finish_reason,
                    has_tool_calls=bool(assistant.tool_calls),
                )
                if finish_failure is not None:
                    if assistant.tool_calls:
                        await self._apply_tool_results(
                            assistant.tool_calls, llm_turn.tool_results)
                    return await self._finish_protocol_failure(
                        assistant, finish_failure, llm_turn.finish_reason)
                if not assistant.tool_calls:
                    final_text = assistant.content or ""
                    await self._emit(RunFinished(
                        final_text, turn_id=self._current_turn_id))
                    return final_text
                abort_reason = await self._apply_tool_results(
                    assistant.tool_calls, llm_turn.tool_results)
                if abort_reason:
                    text = f"[krelcode] {abort_reason}，判定为循环，已中止本轮。"
                    self.history.append(Message.assistant(text))
                    await self._emit(RunFinished(
                        text, turn_id=self._current_turn_id))
                    return text
        except asyncio.CancelledError:
            self._turn_abort_signal.cancel()
            self._repair_after_cancel()
            await self._emit(RunCancelled(
                turn_id=self._current_turn_id))
            raise

        text = max_steps_reached_text(self.max_steps)
        self.history.append(Message.assistant(text))
        await self._emit(RunFinished(
            text, turn_id=self._current_turn_id))
        return text

    def _begin_turn(
        self,
        turn_id: Optional[str],
        *,
        tool_allowlist: Optional[list[str]] = None,
    ) -> None:
        resolved_turn_id = turn_id or f"turn-{next(self._turn_counter)}"
        self._current_turn_id = resolved_turn_id
        self._turn_abort_signal = TurnAbortSignal()
        snapshot = self.tool_context.runtime
        if self.runtime_snapshot_provider is not None:
            snapshot = self.runtime_snapshot_provider(snapshot)
        snapshot = replace(
            snapshot,
            turn_id=resolved_turn_id,
            tool_allowlist=(
                frozenset(tool_allowlist)
                if tool_allowlist is not None else snapshot.tool_allowlist
            ),
        )
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

    async def compress_context(self, force: bool = False) -> bool:
        """统一压缩入口：预算检查、Pre/PostCompact Hook 与事件。"""
        if self.context_manager is None:
            return False
        before = estimate_history_tokens(self.history)
        needs_compression = force or self.context_manager.over_budget(self.history)
        if needs_compression:
            await self.run_lifecycle_hook(
                "pre_compact", {"before_tokens": str(before)})
        changed = await self.context_manager.maybe_compress(
            self.history, force=force)
        if not changed:
            return False
        after = estimate_history_tokens(self.history)
        await self.run_lifecycle_hook(
            "post_compact",
            {"before_tokens": str(before), "after_tokens": str(after)},
        )
        await self._emit(ContextCompressed(before, after))
        return True

    def _repair_after_cancel(self) -> None:
        sanitize_history(self.history)
        if self.history and self.history[-1].role == "user":
            self.history.append(Message.assistant(INTERRUPTED_NOTE))

    # -- 内部：一次 LLM 调用 ---------------------------------------------------

    async def _llm_turn(self) -> LLMTurnResult:
        await self.compress_context()
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
            return LLMTurnResult(
                message=finished.message,
                tool_results=results,
                finish_reason=finished.finish_reason,
            )
        except BaseException:
            await executor.abort()
            raise

    async def _finish_protocol_failure(
        self,
        assistant: Message,
        failure: str,
        finish_reason: Optional[str],
    ) -> str:
        partial = (assistant.content or "").strip()
        assistant.content = (
            f"{partial}\n\n[krelcode] {failure}" if partial
            else f"[krelcode] {failure}"
        )
        final_text = assistant.content
        await self._emit(ErrorOccurred(
            failure, f"finish_reason={finish_reason or 'unknown'}"))
        await self._emit(RunFinished(
            final_text, turn_id=self._current_turn_id))
        return final_text

    def _current_tool_pool(self) -> TurnToolPool:
        if self._active_tool_pool is None:
            self._active_tool_pool = TurnToolPool(
                self.registry,
                intent=self._turn_intent,
                runtime=self._turn_tool_context.runtime,
            )
        return self._active_tool_pool

    def _messages_for_turn(self) -> List[Message]:
        """临时注入本轮工具目录与意图，不污染持久历史。"""
        hints = [
            self._current_tool_pool().system_hint(),
            self._turn_intent.system_hint(self.tool_context.workspace_root),
        ]
        hints.extend(
            f"<runtime_context>\n{context}\n</runtime_context>"
            for context in self.runtime_context
        )
        hint = "\n\n".join(part for part in hints if part)
        if not hint:
            return self.history
        if self.history and self.history[0].role == "system":
            first = self.history[0]
            base = first.content or ""
            system = Message(
                role="system",
                content=base + "\n\n" + hint if base else hint,
                provider_meta=first.provider_meta,
            )
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
                Message.tool_result(
                    tool_call.id,
                    result.content,
                    blocks=list(result.blocks) or None,
                )
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
        if self.redactor is not None:
            result.blocks = tuple(
                self._redact_tool_block(block) for block in result.blocks)
        await self._emit(
            ToolCallCompleted(
                tool_call,
                inline_content,
                result.is_error,
                result.blocks,
                dict(result.data),
            )
        )

    def _redact_tool_block(self, block):
        redact = self.redactor.redact
        if isinstance(block, TextBlock):
            return TextBlock(redact(block.text))
        if isinstance(block, ImageBlock):
            return ImageBlock(block.media_type, block.data_b64, redact(block.alt))
        if isinstance(block, ResourceBlock):
            return ResourceBlock(
                uri=redact(block.uri),
                name=redact(block.name) if block.name else None,
                mime_type=block.mime_type,
                text=redact(block.text) if block.text else None,
                blob=block.blob,
            )
        if isinstance(block, StructuredBlock):
            return StructuredBlock(_redact_json(block.data, redact))
        return block

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

        async def emit_progress(
            message: str, data: Dict[str, object],
        ) -> None:
            safe_message = (
                self.redactor.redact(message) if self.redactor else message)
            safe_data = (
                _redact_json(data, self.redactor.redact)
                if self.redactor is not None else data)
            await self._emit(ToolProgress(
                tool_call.id, tool_call.name, safe_message, safe_data))

        async def emit_output(text: str, stream: str) -> None:
            if not text:
                return
            safe_text = (
                self.redactor.redact(text) if self.redactor else text)
            if safe_text:
                await self._emit(ToolOutputDelta(
                    tool_call.id, tool_call.name, safe_text, stream))

        async def request_user_input(
            prompt: str,
            options: tuple[str, ...],
            allow_free_text: bool,
        ) -> Optional[str]:
            if self.user_input_port is None:
                return None
            safe_prompt = (
                self.redactor.redact(prompt) if self.redactor else prompt)
            safe_options = tuple(
                self.redactor.redact(option) if self.redactor else option
                for option in options
            )
            turn_id = self._turn_tool_context.runtime.turn_id or "turn"
            request_id = f"{turn_id}:input:{next(self._input_counter)}"
            exchange = self.user_input_port.submit(
                safe_prompt,
                safe_options,
                allow_free_text=allow_free_text,
                request_id=request_id,
            )
            await self._emit(UserInputRequested(
                request_id=exchange.request_id,
                prompt=safe_prompt,
                options=safe_options,
                allow_free_text=allow_free_text,
            ))
            answer = await exchange.wait()
            await self._emit(UserInputResolved(
                request_id=exchange.request_id,
                answered=answer is not None,
            ))
            return answer

        async def emit_plan(
            items: tuple[str, ...],
            statuses: tuple[str, ...],
            active_index: int,
            explanation: str,
        ) -> None:
            redact = self.redactor.redact if self.redactor else (lambda text: text)
            await self._emit(PlanUpdated(
                items=tuple(redact(item) for item in items),
                active_index=active_index,
                statuses=tuple(redact(status) for status in statuses),
                explanation=redact(explanation),
                turn_id=self._current_turn_id,
            ))

        execution_ctx = replace(
            self._turn_tool_context,
            tool_descriptor=pool.descriptor(tool_call.name),
            progress_emitter=emit_progress,
            output_emitter=emit_output,
            input_requester=request_user_input,
            plan_emitter=emit_plan,
        )

        # 2) 参数格式与 JSON Schema：坏参数不进入审批或工具实现。
        if tool_call.has_invalid_arguments:
            return ToolResult(
                content=(
                    "工具参数不是合法 JSON: "
                    f"{tool_call.arguments[INVALID_ARGUMENTS_KEY]!r}"
                ),
                is_error=True,
            )
        validation_errors = validate_tool_arguments(
            tool.parameters, tool_call.arguments)
        if validation_errors:
            rendered = [error.render() for error in validation_errors[:20]]
            if len(validation_errors) > 20:
                rendered.append(f"…另有 {len(validation_errors) - 20} 个错误")
            return ToolResult(
                content=(
                    f"工具参数校验失败: {tool_call.name}\n- "
                    + "\n- ".join(rendered)
                ),
                is_error=True,
            )
        args = dict(tool_call.arguments)
        pre_hook = await self._before_tool_hook(tool_call)
        if pre_hook.blocked:
            return ToolResult(
                content=f"PreToolUse Hook 阻止执行: {pre_hook.reason}",
                is_error=True,
                data={"hook_blocked": True, "hook_trigger": "pre_tool_use"},
            )

        # 3) 审批（策略看到的是已通过 schema 校验的参数）
        if self.policy is not None:
            await execution_ctx.emit_progress("安全策略检查中")
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

        # 4) 执行（异常 → is_error 结果回填，错误恢复原则）
        await execution_ctx.emit_progress("开始执行")
        try:
            result = await tool.execute(args, execution_ctx)
        except Exception as e:
            await self._emit(ErrorOccurred(f"{type(e).__name__}: {e}", tool_call.name))
            result = ToolResult(
                content=f"工具执行出错: {type(e).__name__}: {e}", is_error=True)

        post_hook = await self._after_tool_hook(tool_call, result)
        contexts = (*pre_hook.context, *post_hook.context)
        if contexts:
            blocks = "\n\n".join(
                f"<hook_context>\n{item}\n</hook_context>" for item in contexts)
            result.content = (result.content + "\n\n" + blocks).strip()
        return result

    async def run_lifecycle_hook(
        self, trigger: str, variables: dict[str, str],
    ) -> HookResult:
        """执行生命周期 Hook，并统一注入运行时上下文与审计事件。"""
        if self.hook_port is None:
            return HookResult()
        resolved = dict(variables)
        resolved.setdefault("workspace", str(self.tool_context.workspace_root))
        try:
            result = await self.hook_port.run_lifecycle(trigger, resolved)
        except Exception as exc:
            await self._emit(ErrorOccurred(
                f"{type(exc).__name__}: {exc}", f"hook.{trigger}"))
            return HookResult()
        if result.context:
            self.add_runtime_context(result.context, source=trigger)
        if result.matched:
            await self._emit(HookTriggered(
                trigger, True, result.blocked, "", result.reason,
                result.system_message))
        return result

    async def _before_tool_hook(self, tool_call: ToolCall) -> HookResult:
        if self.hook_port is None:
            return HookResult()
        try:
            result = await self.hook_port.before_tool(tool_call)
        except Exception as exc:
            await self._emit(ErrorOccurred(
                f"{type(exc).__name__}: {exc}", "hook.pre_tool_use"))
            return HookResult()
        if result.matched:
            await self._emit(HookTriggered(
                "pre_tool_use", True, result.blocked, tool_call.name,
                result.reason, result.system_message))
        return result

    async def _after_tool_hook(
        self, tool_call: ToolCall, result: ToolResult,
    ) -> HookResult:
        if self.hook_port is None:
            return HookResult()
        try:
            hook_result = await self.hook_port.after_tool(
                tool_call, result.content, result.is_error)
        except Exception as exc:
            await self._emit(ErrorOccurred(
                f"{type(exc).__name__}: {exc}", "hook.post_tool_use"))
            return HookResult()
        if hook_result.matched:
            await self._emit(HookTriggered(
                "post_tool_use", True, hook_result.blocked, tool_call.name,
                hook_result.reason, hook_result.system_message))
        return hook_result


def _call_signature(tool_call: ToolCall) -> str:
    """工具名 + 稳定序列化的参数，作为卡死检测的签名。"""
    try:
        args = json.dumps(tool_call.arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = repr(tool_call.arguments)
    if len(args) > 200:
        args = args[:200] + "…"
    return f"{tool_call.name}({args})"


def _redact_json(value, redact):
    """递归脱敏结构化工具结果中的字符串值。"""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_json(item, redact) for item in value]
    if isinstance(value, dict):
        return {
            str(redact(str(key))): _redact_json(item, redact)
            for key, item in value.items()
        }
    return value
