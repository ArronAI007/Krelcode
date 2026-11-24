"""AgentLoop：整个系统的心脏。

只依赖四个端口：LLMProvider / ToolRegistry / ApprovalPolicy / EventBus。
语义见 DESIGN.md「主循环语义」。Step 5 版本：
- 同批只读 tool_calls 并行、写操作串行，结果按原序回填
- 卡死检测：同一签名（工具名+参数）连续失败达阈值 → 中止本轮
- 最大步数守卫
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from itertools import count
from typing import (
    Any, Awaitable, Callable, Dict, Iterable, Iterator, List, Optional,
    Protocol, Sequence, cast,
)

from krelcode.core.hooks import HookPort, HookResult
from krelcode.core.interactions import UserInputPort
from krelcode.core.redact import SecretRedactor
from krelcode.core.context_usage import ContextUsageSnapshot
from krelcode.core.events import (
    AgentEvent,
    ErrorOccurred,
    EventBus,
    HookTriggered,
    QueryPhaseChanged,
    RunCancelled,
    RunFinished,
    RunStarted,
    StepStarted,
    UserSteered,
)
from krelcode.core.messages import ImageRef, Message, SteeringInput, ToolCall, ToolResultBlock
from krelcode.llm.base import LLMProvider, PreparedLLMRequest, ToolSpec
from krelcode.agent.context.attachments import ContextAttachment, ContextAttachmentDiff, ContextAttachmentKind
from krelcode.agent.context.compression import ContextManager
from krelcode.agent.context.context_assembler import ContextAssembler
from krelcode.agent.context.workspace import WorkspaceContext
from krelcode.agent.context.context_source import (
    RetrievedMemoryContextSource,
    default_context_sources,
)
from krelcode.agent.turn.dependencies import AgentDependencies
from krelcode.agent.context.context_cache_ledger import ContextCacheLedger
from krelcode.agent.turn.context_turn import ContextTurnCoordinator
from krelcode.agent.context.context_source import ContextAssembly
from krelcode.agent.turn.intent import TurnIntent, infer_turn_intent
from krelcode.agent.turn.model_turn import (
    LLMTurnResult,
    ModelTurnExecutor,
    finish_reason_failure,
)
from krelcode.agent.prompt.prompt_assembler import PromptAssembler
from krelcode.agent.prompt.prompt_surface import PromptSurfaceRegistry, main_prompt_surface
from krelcode.agent.turn.reminders import (
    ReminderCatalog,
    ReminderSelection,
    default_reminder_catalog,
)
from krelcode.agent.turn.tool_pool import TurnToolPool
from krelcode.agent.context.tool_microcompact import MicrocompactReport, ToolMicrocompactor
from krelcode.agent.turn.tool_turn import ToolBatchExecutor
from krelcode.agent.turn.runtime_ports import (
    CompactCheckpointStorePort,
    ContextManagerPort,
    ContextTurnHostPort,
    ModelTurnHostPort,
    ToolTurnHostPort,
)
from krelcode.agent.turn.runtime_state import AgentRuntimeState, RuntimeStateHost
from krelcode.agent.turn.runtime_composition import build_agent_services
from krelcode.agent.context.working_context import WorkingContextSnapshot, WorkingContextStore
from krelcode.agent.context.working_context_source import WorkingContextSource
from krelcode.agent.turn.workflows import TaskWorkflowSelection, select_task_workflow
from krelcode.safety.policy import ApprovalPolicy
from krelcode.contracts.tool import RuntimeSnapshot, ToolContext, ToolResult, TurnAbortSignal
from krelcode.tools.registry import ToolRegistry

DEFAULT_MAX_STEPS = 64


def max_steps_reached_text(max_steps: int) -> str:
    """步数守卫中止说明：同时给出原因和可操作的调整方式。"""
    return (
        f"[krelcode] 工具循环已达到单轮 {max_steps} 个模型步骤的安全上限，"
        f"本轮已中止。可用 --max-steps {max_steps * 2} 提高上限后重试。"
    )


INTERRUPTED_NOTE = "[已被用户中止]"
STEERING_PREFIX = "[用户插话] "


class AgentLoop(RuntimeStateHost):
    """Turn orchestration facade.

    Model streaming, context assembly and tool batch execution live in
    dedicated executors.  This class owns only turn sequencing, lifecycle
    guards and the backwards-compatible public surface.
    """

    _DEPENDENCY_ATTRS = frozenset({
        "llm", "registry", "tool_context", "policy", "bus", "max_steps",
        "repeat_failure_limit", "context_manager", "tool_output_inline_limit",
        "token_budget", "redactor", "working_context_store",
        "tool_microcompactor", "context_assembler", "prompt_assembler",
        "reminder_catalog", "compact_checkpoint_store",
        "runtime_snapshot_provider", "hook_port", "user_input_port",
        "step_gate",
    })

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
        context_assembler: Optional[ContextAssembler] = None,
        prompt_assembler: Optional[PromptAssembler] = None,
        redactor: Optional[SecretRedactor] = None,
        runtime_snapshot_provider: Optional[
            Callable[[RuntimeSnapshot], RuntimeSnapshot]
        ] = None,
        hook_port: Optional[HookPort] = None,
        user_input_port: Optional[UserInputPort] = None,
        step_gate: Optional[Callable[[], Awaitable[None]]] = None,
        working_context_store: Optional[WorkingContextStore] = None,
        tool_microcompactor: Optional[ToolMicrocompactor] = None,
        compact_checkpoint_store: Optional[CompactCheckpointStorePort] = None,
        today_provider: Optional[Callable[[], str]] = None,
        reminder_catalog: Optional[ReminderCatalog] = None,
    ) -> None:
        self._runtime_state = AgentRuntimeState()
        self.services = build_agent_services(
            llm=llm,
            registry=registry,
            tool_context=tool_context,
            policy=policy,
            bus=bus,
            redactor=redactor,
            hook_port=hook_port,
            user_input_port=user_input_port,
            system_prompt=system_prompt,
            context_manager=context_manager,
            context_assembler=context_assembler,
            prompt_assembler=prompt_assembler,
            working_context_store=working_context_store,
            tool_microcompactor=tool_microcompactor,
            compact_checkpoint_store=compact_checkpoint_store,
            reminder_catalog=reminder_catalog,
            registry_provider=lambda: self.services.prompt.registry,
        )

        # Non-service executor dependencies remain explicit attributes.
        self.max_steps = max_steps
        self.repeat_failure_limit = repeat_failure_limit
        self.tool_output_inline_limit = tool_output_inline_limit
        self.token_budget = token_budget
        self.runtime_snapshot_provider = runtime_snapshot_provider
        self.step_gate = step_gate

        # Mutable state owned by this loop.
        self.context_engine = str(
            getattr(self.services.context.assembler, "engine", "v3") or "v3")
        self.last_reminder_selection: Optional[ReminderSelection] = None
        self._turn_reminder_selection: Optional[ReminderSelection] = None
        self._turn_requested_reminder_ids: tuple[str, ...] = ()
        self.runtime_context: List[str] = []
        self._token_total = 0
        self._failure_counts: Dict[str, int] = {}
        self._turn_intent = TurnIntent()
        self._turn_workflow: Optional[TaskWorkflowSelection] = None
        self._active_tool_pool: Optional[TurnToolPool] = None
        self._turn_tool_context = tool_context
        self._turn_abort_signal = TurnAbortSignal()
        self._turn_counter = count(1)
        self._direct_counter = count(1)
        self._current_turn_id = ""
        self._input_counter: Iterator[int] = count(1)
        self._turn_prepared = False
        self.pending_context_refresh = False
        self.workspace_context: Optional[WorkspaceContext] = None
        self.steering_queue: asyncio.Queue[SteeringInput | str] = asyncio.Queue()
        self.history: List[Message] = (
            [Message.system(system_prompt)] if system_prompt else []
        )

        self.dependencies = AgentDependencies(
            llm=self.services.core.llm,
            registry=self.services.core.registry,
            tool_context=self.services.core.tool_context,
            bus=self.services.core.bus,
            context_manager=cast(
                ContextManagerPort, self.services.context.manager),
            context_assembler=self.services.context.assembler,
            prompt_assembler=self.services.prompt.assembler,
            working_context_store=self.services.context.working_context,
            tool_microcompactor=self.services.context.tool_microcompactor,
            compact_checkpoint_store=self.services.context.compact_checkpoint,
            reminder_catalog=self.services.context.reminder_catalog,
            policy=self.services.core.policy,
            redactor=self.services.core.redactor,
            runtime_snapshot_provider=self.runtime_snapshot_provider,
            hook_port=self.services.core.hook_port,
            user_input_port=self.services.core.user_input_port,
            step_gate=self.step_gate,
            today_provider=today_provider,
            tool_output_inline_limit=self.tool_output_inline_limit,
            repeat_failure_limit=self.repeat_failure_limit,
            max_steps=self.max_steps,
            token_budget=self.token_budget,
        )
        self.context_coordinator = ContextTurnCoordinator(
            cast(ContextTurnHostPort, self), self.dependencies)
        self.tool_executor = ToolBatchExecutor(
            cast(ToolTurnHostPort, self), self.dependencies)
        self.model_executor = ModelTurnExecutor(
            cast(ModelTurnHostPort, self),
            self.dependencies,
            self.context_coordinator,
            self.tool_executor,
        )

        if self.services.context.manager is not None:
            self.services.context.manager.rebuild_state(self.history)
        self.context_coordinator.sync_mode_permission_contract()

    async def _emit(self, event: AgentEvent) -> None:
        await self.services.core.bus.emit(event)

    def reset(self) -> None:
        """Clear history while preserving the system prompt."""
        self.history = (
            self.history[:1]
            if self.history and self.history[0].role == "system" else []
        )
        self._turn_intent = TurnIntent()
        self._turn_workflow = None
        self._turn_reminder_selection = None
        self._turn_requested_reminder_ids = ()
        self.last_reminder_selection = None
        self.context_coordinator.reset()
        self.services.context.working_context.clear()

    def rebuild_working_context(self) -> WorkingContextSnapshot:
        """Rebuild session-scoped working facts after resume/fork/rewind."""
        snapshot = self.services.context.working_context.rebuild_from_history(
            self.history)
        if self.services.context.manager is not None:
            self.services.context.manager.rebuild_state(self.history)
        return snapshot

    async def refresh_workspace_context(self) -> WorkspaceContext:
        return await self.context_coordinator.refresh_workspace_context()

    def sync_context_attachments(
        self,
        *,
        include_environment: bool = True,
        include_catalogs: bool = True,
        include_mcp: bool = True,
        include_contract: bool = True,
        include_reminders: bool = True,
    ) -> ContextAttachmentDiff:
        return self.context_coordinator.sync_context_attachments(
            include_environment=include_environment,
            include_catalogs=include_catalogs,
            include_mcp=include_mcp,
            include_contract=include_contract,
            include_reminders=include_reminders,
        )

    def _sync_mode_permission_contract(self) -> None:
        self.context_coordinator.sync_mode_permission_contract()

    def _sync_catalog_attachment(
        self,
        kind: ContextAttachmentKind,
        manager: object | None,
        builder: Callable[
            [object, Optional[ContextAttachment]], Optional[ContextAttachment]
        ],
    ) -> None:
        self.context_coordinator._sync_catalog_attachment(kind, manager, builder)

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
        images: Optional[list[ImageRef]] = None,
        *,
        turn_id: Optional[str] = None,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[Iterable[str]] = None,
    ) -> str:
        """QueryEngine 的窄入口；允许 runner 替换 ``run`` 时仍传入 turn id。"""
        self._begin_turn(
            turn_id,
            tool_allowlist=tool_allowlist,
            reminder_ids=reminder_ids,
        )
        self._turn_prepared = True
        try:
            return await self.run(user_input, images=images)
        finally:
            self._turn_prepared = False

    async def execute_direct_tool(
        self,
        name: str,
        arguments: Dict[str, object],
        *,
        turn_id: Optional[str] = None,
    ) -> ToolResult:
        """Execute one tool outside a model turn while preserving all guards.

        REPL shortcuts such as ``!command`` use this narrow entry.  It applies
        the same runtime snapshot, dynamic tool pool, schema validation, Hooks,
        approval policy and Sandbox context as normal tool calls, but never
        appends either the invocation or its result to model history.
        """
        if self._turn_prepared:
            raise RuntimeError("Agent 正在执行查询，不能并发启动直接工具")
        saved_context = self._turn_tool_context
        saved_intent = self._turn_intent
        saved_workflow = self._turn_workflow
        saved_pool = self._active_tool_pool
        saved_abort = self._turn_abort_signal
        saved_turn_id = self._current_turn_id
        direct_turn = turn_id or f"direct-{next(self._direct_counter)}"
        call = ToolCall(
            id=f"{direct_turn}-call",
            name=name,
            arguments=dict(arguments),
        )
        self._begin_turn(direct_turn)
        self._turn_intent = TurnIntent()
        self._turn_workflow = None
        self._active_tool_pool = TurnToolPool(
            self.services.core.registry,
            intent=self._turn_intent,
            runtime=self._turn_tool_context.runtime,
        )
        self._active_tool_pool.authorize_direct(name)
        try:
            return await self._run_stream_tool(call)
        except asyncio.CancelledError:
            self._turn_abort_signal.cancel("直接工具被用户中止")
            raise
        finally:
            self._turn_tool_context = saved_context
            self._turn_intent = saved_intent
            self._turn_workflow = saved_workflow
            self._active_tool_pool = saved_pool
            self._turn_abort_signal = saved_abort
            self._current_turn_id = saved_turn_id

    async def run(
        self,
        user_input: str,
        images: Optional[list[ImageRef]] = None,
        *,
        turn_id: Optional[str] = None,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[Iterable[str]] = None,
    ) -> str:
        """处理一轮用户输入：内部可能经历多次 LLM 调用与工具执行。

        images: 可选 ImageRef 列表（@图片附件），挂在 user 消息上由
        provider adapter 转成各自的多模态 wire 格式。
        被 cancel 时（REPL /stop）：消毒历史（弹掉不配对的尾部消息），
        若结尾是未答完的 user 消息则补一条中止说明（保角色交替），再重抛。
        """
        if not self._turn_prepared:
            self._begin_turn(
                turn_id,
                tool_allowlist=tool_allowlist,
                reminder_ids=reminder_ids,
            )
        if self.pending_context_refresh:
            await self.refresh_workspace_context()
        # Keep the last completed request visible until this turn's first
        # request is assembled.  ``_llm_turn`` rotates it into
        # ``previous_context_snapshot`` so /context cache can compare stable
        # prefixes across turn boundaries as well as across tool steps.
        self.history.append(Message.user(user_input, images=images or None))
        self._failure_counts = {}
        self._token_total = 0
        self._turn_intent = infer_turn_intent(
            self.services.core.tool_context.workspace_root, user_input)
        self._turn_workflow = select_task_workflow(
            user_input, intent=self._turn_intent)
        self.services.context.working_context.begin_turn(
            self._current_turn_id, user_input)
        # 一轮只构建一次：后续每个模型 step 和每次工具执行共用同一份能力快照。
        self._active_tool_pool = TurnToolPool(
            self.services.core.registry,
            intent=self._turn_intent,
            runtime=self._turn_tool_context.runtime,
        )
        self._select_turn_reminders()
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
                    self.services.context.working_context.record_error(
                        text, source="budget")
                    self.history.append(Message.assistant(text))
                    await self._emit(RunFinished(
                        text, turn_id=self._current_turn_id))
                    return text
                await self._emit(StepStarted(step))
                await self._emit(QueryPhaseChanged(
                    "model", f"模型生成中（Step {step}）"))
                llm_turn = await self._llm_turn(step)
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
                    self.services.context.working_context.record_error(
                        text, source="loop_guard")
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
        self.services.context.working_context.record_error(
            text, source="max_steps")
        self.history.append(Message.assistant(text))
        await self._emit(RunFinished(
            text, turn_id=self._current_turn_id))
        return text

    def _begin_turn(
        self,
        turn_id: Optional[str],
        *,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[Iterable[str]] = None,
    ) -> None:
        resolved_turn_id = turn_id or f"turn-{next(self._turn_counter)}"
        self._current_turn_id = resolved_turn_id
        self._turn_workflow = None
        self._turn_reminder_selection = None
        self._turn_requested_reminder_ids = tuple(dict.fromkeys(
            str(item).strip()
            for item in (reminder_ids or ())
            if str(item).strip()
        ))
        self._turn_abort_signal = TurnAbortSignal()
        snapshot = self.services.core.tool_context.runtime
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
            self.services.core.tool_context,
            runtime=snapshot,
            abort=self._turn_abort_signal,
        )

    def _select_turn_reminders(self) -> ReminderSelection:
        return self.context_coordinator.select_turn_reminders()

    def _current_context_usage_ratio(self) -> float:
        return self.context_coordinator.current_context_usage_ratio()

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

    async def microcompact_tools(
        self,
        *,
        trigger: str = "manual",
    ) -> MicrocompactReport:
        return await self.context_coordinator.microcompact_tools(
            trigger=trigger)

    async def compress_context(
        self,
        force: bool = False,
        *,
        focus: str = "",
        request_tokens: Optional[int] = None,
        output_reserve_tokens: int = 0,
        model_context_window: Optional[int] = None,
        pre_microcompacted: bool = False,
    ) -> bool:
        return await self.context_coordinator.compress_context(
            force=force,
            focus=focus,
            request_tokens=request_tokens,
            output_reserve_tokens=output_reserve_tokens,
            model_context_window=model_context_window,
            pre_microcompacted=pre_microcompacted,
        )

    def _repair_after_cancel(self) -> None:
        self.context_coordinator.repair_after_cancel()

    # -- 内部：一次 LLM 调用 ---------------------------------------------------

    async def _build_llm_request(
        self,
        *,
        step: int,
        prompt_specs: Sequence[object],
        specs: Sequence[ToolSpec],
        tools: Optional[Sequence[ToolSpec]],
    ) -> tuple[List[Message], ContextAssembly, Optional[PreparedLLMRequest]]:
        return await self.context_coordinator.build_llm_request(
            step=step,
            prompt_specs=prompt_specs,
            specs=specs,
            tools=tools,
        )

    def _finalize_context_cache_ledger(
        self, context_snapshot: ContextUsageSnapshot,
    ) -> Optional[ContextCacheLedger]:
        return self.context_coordinator.finalize_context_cache_ledger(
            context_snapshot)

    def _request_snapshot(
        self,
        *,
        step: int,
        specs: Sequence[ToolSpec],
        messages: List[Message],
        context_assembly: ContextAssembly,
        prepared: Optional[PreparedLLMRequest],
    ) -> ContextUsageSnapshot:
        return self.context_coordinator.request_snapshot(
            step=step,
            specs=specs,
            messages=messages,
            context_assembly=context_assembly,
            prepared=prepared,
        )

    async def _llm_turn(self, step: int) -> LLMTurnResult:
        return await self.model_executor.run(step)

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
        self.services.context.working_context.record_error(
            failure, source="provider", metadata={"finish_reason": finish_reason or ""})
        await self._emit(ErrorOccurred(
            failure, f"finish_reason={finish_reason or 'unknown'}"))
        await self._emit(RunFinished(
            final_text, turn_id=self._current_turn_id))
        return final_text

    def _current_tool_pool(self) -> TurnToolPool:
        if self._active_tool_pool is None:
            self._active_tool_pool = TurnToolPool(
                self.services.core.registry,
                intent=self._turn_intent,
                runtime=self._turn_tool_context.runtime,
            )
        return self._active_tool_pool

    def current_tool_pool(self) -> TurnToolPool:
        """Public, read-mostly view used by /tools and diagnostics."""
        return self._current_tool_pool()

    def current_task_workflow(self) -> Optional[TaskWorkflowSelection]:
        """Return the workflow selected for the current turn, if any."""
        return self._turn_workflow

    # -- 内部：执行一批工具调用 -------------------------------------------------

    async def _apply_tool_results(
        self,
        tool_calls: List[ToolCall],
        results: List[ToolResult],
    ) -> Optional[str]:
        return await self.tool_executor.apply_results(tool_calls, results)

    async def _run_stream_tool(self, tool_call: ToolCall) -> ToolResult:
        return await self.tool_executor.run_stream_tool(tool_call)

    async def _publish_tool_result(
        self, tool_call: ToolCall, result: ToolResult,
    ) -> None:
        await self.tool_executor.publish_tool_result(tool_call, result)

    def _redact_tool_block(self, block: ToolResultBlock) -> ToolResultBlock:
        return self.tool_executor._redact_tool_block(block)

    def _persist_large_output(self, tool_call: ToolCall, content: str) -> str:
        return self.tool_executor.persist_large_output(tool_call, content)

    async def _execute_one(self, tool_call: ToolCall) -> ToolResult:
        return await self.tool_executor.execute_one(tool_call)

    async def run_lifecycle_hook(
        self, trigger: str, variables: dict[str, str],
    ) -> HookResult:
        """执行生命周期 Hook，并统一注入运行时上下文与审计事件。"""
        if self.services.core.hook_port is None:
            return HookResult()
        resolved = dict(variables)
        resolved.setdefault("workspace", str(self.services.core.tool_context.workspace_root))
        try:
            result = await self.services.core.hook_port.run_lifecycle(trigger, resolved)
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
        if self.services.core.hook_port is None:
            return HookResult()
        try:
            result = await self.services.core.hook_port.before_tool(tool_call)
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
        if self.services.core.hook_port is None:
            return HookResult()
        try:
            hook_result = await self.services.core.hook_port.after_tool(
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

    def set_llm(self, llm: LLMProvider) -> None:
        """Replace the provider through an explicit runtime-owned mutation."""
        self.llm = llm
