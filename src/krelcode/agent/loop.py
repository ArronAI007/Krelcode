"""AgentLoop：整个系统的心脏。

只依赖四个端口：LLMProvider / ToolRegistry / ApprovalPolicy / EventBus。
语义见 DESIGN.md「主循环语义」。Step 5 版本：
- 同一批 tool_calls 并行执行（asyncio.gather），结果按原序回填
- 卡死检测：同一签名（工具名+参数）连续失败达阈值 → 中止本轮
- 最大步数守卫
"""

from __future__ import annotations

import asyncio
import json
from typing import Dict, List, Optional

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
    RunFinished,
    RunStarted,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    UserSteered,
)
from ..core.textutil import truncate_middle
from ..core.messages import (
    INVALID_ARGUMENTS_KEY, Message, ToolCall, sanitize_history,
)
from ..llm.base import LLMProvider, StreamFinished, TextDelta, ThinkingDelta
from .compression import ContextManager
from .intent import TurnIntent, infer_turn_intent
from .tokens import estimate_history_tokens
from ..safety.policy import ApprovalPolicy
from ..tools.base import ToolContext, ToolResult
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
        self._token_total = 0
        self._failure_counts: Dict[str, int] = {}
        self._turn_intent = TurnIntent()
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

    async def run(self, user_input: str, images: Optional[list] = None) -> str:
        """处理一轮用户输入：内部可能经历多次 LLM 调用与工具执行。

        images: 可选 ImageRef 列表（@图片附件），挂在 user 消息上由
        provider adapter 转成各自的多模态 wire 格式。
        被 cancel 时（REPL /stop）：消毒历史（弹掉不配对的尾部消息），
        若结尾是未答完的 user 消息则补一条中止说明（保角色交替），再重抛。
        """
        self.history.append(Message.user(user_input, images=images or None))
        self._failure_counts = {}
        self._token_total = 0
        self._turn_intent = infer_turn_intent(
            self.tool_context.workspace_root, user_input)
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
                assistant = await self._llm_turn()
                if not assistant.tool_calls:
                    final_text = assistant.content or ""
                    await self._emit(RunFinished(final_text))
                    return final_text
                abort_reason = await self._execute_tool_calls(assistant.tool_calls)
                if abort_reason:
                    text = f"[krelcode] {abort_reason}，判定为循环，已中止本轮。"
                    self.history.append(Message.assistant(text))
                    await self._emit(RunFinished(text))
                    return text
        except asyncio.CancelledError:
            self._repair_after_cancel()
            raise

        text = max_steps_reached_text(self.max_steps)
        self.history.append(Message.assistant(text))
        await self._emit(RunFinished(text))
        return text

    async def _inject_steering(self) -> None:
        """把插话队列里的消息注入历史（每个 step 开始前调用）。

        尾部若是 user 消息则合并（防连续 user——Anthropic 要求角色交替）；
        尾部是 tool/assistant 时作为新 user 消息追加（合法的交替点）。
        """
        while not self.steering_queue.empty():
            try:
                text = self.steering_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            text = str(text).strip()
            if not text:
                continue
            if self.history and self.history[-1].role == "user":
                self.history[-1].content = (
                    (self.history[-1].content or "") + "\n" + STEERING_PREFIX + text)
            else:
                self.history.append(Message.user(STEERING_PREFIX + text))
            await self._emit(UserSteered(text))

    def _repair_after_cancel(self) -> None:
        sanitize_history(self.history)
        if self.history and self.history[-1].role == "user":
            self.history.append(Message.assistant(INTERRUPTED_NOTE))

    # -- 内部：一次 LLM 调用 ---------------------------------------------------

    async def _llm_turn(self) -> Message:
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
            specs = [
                spec for spec in self.registry.tool_specs()
                if self._turn_intent.allows_tool(spec["name"])
            ]
            tools = specs or None
        messages = self._messages_for_turn()
        finished: Optional[StreamFinished] = None
        async for event in self.llm.stream(messages, tools):
            if isinstance(event, TextDelta):
                await self._emit(LLMStreamDelta(event.text))
            elif isinstance(event, ThinkingDelta):
                await self._emit(LLMThinkingDelta(event.text))
            elif isinstance(event, StreamFinished):
                finished = event
        if finished is None:  # provider 违反契约
            raise RuntimeError("LLMProvider 未产出 StreamFinished")
        self.history.append(finished.message)
        usage = dict(finished.usage)
        self._token_total += usage.get("total_tokens") or (
            usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
        await self._emit(LLMResponseReceived(finished.message, usage))
        return finished.message

    def _messages_for_turn(self) -> List[Message]:
        """把本轮意图作为临时 system context 注入，不污染持久历史。"""
        hint = self._turn_intent.system_hint(self.tool_context.workspace_root)
        if not hint:
            return self.history
        if self.history and self.history[0].role == "system":
            base = self.history[0].content or ""
            system = Message.system(base + "\n\n" + hint if base else hint)
            return [system, *self.history[1:]]
        return [Message.system(hint), *self.history]

    # -- 内部：执行一批工具调用 -------------------------------------------------

    async def _execute_tool_calls(self, tool_calls: List[ToolCall]) -> Optional[str]:
        """并行执行一批工具调用，结果按原序回填历史。

        返回 None 表示继续；返回字符串表示触发卡死检测，应中止本轮。
        """
        for tool_call in tool_calls:
            await self._emit(ToolCallStarted(tool_call))
        results = await asyncio.gather(
            *(self._execute_one(tc) for tc in tool_calls)
        )
        for tool_call, result in zip(tool_calls, results):
            content = self.redactor.redact(result.content) if self.redactor else result.content
            inline_content = self._persist_large_output(tool_call, content)
            await self._emit(
                ToolCallCompleted(tool_call, inline_content, result.is_error)
            )
            self.history.append(
                Message.tool_result(tool_call.id, inline_content)
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
        # 1) 审批
        if self.policy is not None:
            try:
                decision = await self.policy.check(tool_call, self.tool_context)
            except Exception as e:  # 审批器自身出错 → 保守拒绝
                await self._emit(ErrorOccurred(str(e), "policy.check"))
                await self._emit(PermissionDenied(tool_call, str(e)))
                return ToolResult(content=f"审批检查失败，已拒绝执行: {e}", is_error=True)
            if not decision.allowed:
                await self._emit(PermissionDenied(tool_call, decision.reason))
                return ToolResult(
                    content=f"操作被安全策略拒绝: {decision.reason}", is_error=True
                )

        # 2) 查找工具
        tool = self.registry.get(tool_call.name)
        if tool is None:
            return ToolResult(
                content=f"未知工具: {tool_call.name}（可用工具: {', '.join(self.registry.names()) or '无'}）",
                is_error=True,
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
            return await tool.execute(args, self.tool_context)
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
