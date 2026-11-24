"""REPL 会话运行器：查询、事件消费、审批与下一条队列的唯一所有者。

本模块位于 session 层，不导入 ``ui.*``。终端前台只需：
1. 提交 ``SessionPrompt``；
2. 用 ``wait_tasks()`` 加入自己的前台等待集合；
3. 在任务完成时调用 ``process_completed()``；
4. 注入事件渲染、审批展示、状态栏刷新和提示输出回调。

这样 stdin、编辑器与视觉样式留在 UI，而查询推进和审批生命周期不再散落在
``run_repl()`` 的局部变量与闭包里。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from ..agent.query import QueryEvent, QuerySession
from ..core.events import AgentEvent, PlanUpdated
from ..core.redact import SecretRedactor
from .controller import SessionController
from .envelope import SessionEnvelope
from .event_router import EnvelopeSink, SessionEventRouter
from .interactions import InputResolution, UserInputBroker, UserInputRequest
from .prompt_queue import PromptQueue
from .state import SessionEventReducer, SessionViewState


class ApprovalBrokerPort(Protocol):
    @property
    def pending(self) -> object | None: ...

    async def next_request(self) -> object | None: ...

    def resolve(self, raw: str) -> Optional[str]: ...

    def cancel_all(self) -> None: ...


EventSink = Callable[[AgentEvent], None]
StatusSink = Callable[[SessionViewState], None]
ApprovalSink = Callable[[Any], None]
UserInputSink = Callable[[Any], None]
NoticeSink = Callable[[str], None]
VoidCallback = Callable[[], None]


@dataclass(frozen=True)
class SessionPrompt:
    """session 层的一次用户请求，不包含输入控件或终端状态。"""

    text: str
    images: tuple[Any, ...] = ()
    tool_allowlist: Optional[tuple[str, ...]] = None
    reminder_ids: tuple[str, ...] = ()
    label: str = ""

    @classmethod
    def create(
        cls,
        text: str,
        *,
        images: Sequence[Any] = (),
        tool_allowlist: Optional[Sequence[str]] = None,
        reminder_ids: Sequence[str] = (),
        label: str = "",
    ) -> "SessionPrompt":
        return cls(
            text=text,
            images=tuple(images),
            tool_allowlist=(
                tuple(tool_allowlist) if tool_allowlist is not None else None),
            reminder_ids=tuple(reminder_ids),
            label=label or " ".join(text.split()),
        )


class ReplSessionRunner:
    """拥有当前 query、事件 consumer、审批 watcher 和 queued prompts。"""

    def __init__(
        self,
        controller: SessionController,
        state: SessionViewState,
        *,
        on_event: EventSink,
        on_status: Optional[StatusSink] = None,
        on_approval: Optional[ApprovalSink] = None,
        on_user_input: Optional[UserInputSink] = None,
        on_notice: Optional[NoticeSink] = None,
        on_stream_end: Optional[VoidCallback] = None,
        approval_broker: Optional[ApprovalBrokerPort] = None,
        input_broker: Optional[UserInputBroker] = None,
        reducer: Optional[SessionEventReducer] = None,
        session_id: str = "",
        redactor: Optional[SecretRedactor] = None,
        event_router: Optional[SessionEventRouter] = None,
        envelope_sink: Optional[EnvelopeSink] = None,
    ) -> None:
        self.controller = controller
        self.state = state
        self.approval_broker = approval_broker
        self.input_broker = input_broker
        self._on_event = on_event
        self._on_status = on_status or (lambda _state: None)
        self._on_approval = on_approval or (lambda _request: None)
        self._on_user_input = on_user_input or (lambda _request: None)
        self._on_notice = on_notice or (lambda _message: None)
        self._on_stream_end = on_stream_end or (lambda: None)
        self._reducer = reducer or SessionEventReducer()
        active_session_path = getattr(controller, "session_path", None)
        self.session_id = session_id or (
            Path(str(active_session_path)).stem if active_session_path else "")
        self.event_router = event_router or SessionEventRouter(
            state,
            session_id=self.session_id,
            reducer=self._reducer,
            redactor=redactor,
        )
        self.event_router.subscribe_event(self._handle_query_event)
        if envelope_sink is not None:
            self.event_router.subscribe_envelope(envelope_sink)
        self.last_envelope: Optional[SessionEnvelope] = None

        self.queue: PromptQueue[SessionPrompt] = PromptQueue()
        self.active_query: Optional[QuerySession] = None
        self.active_prompt: Optional[SessionPrompt] = None
        self.consumer_task: Optional[asyncio.Task[None]] = None
        self.approval_task: Optional[asyncio.Task[Any]] = None
        self.input_task: Optional[asyncio.Task[Optional[UserInputRequest]]] = None
        self.pending_approval: Optional[Any] = None
        self.pending_input: Optional[Any] = None

    @property
    def is_running(self) -> bool:
        return self.active_query is not None

    @property
    def pending(self) -> Optional[Any]:
        return self.pending_approval

    @property
    def has_pending_input(self) -> bool:
        """broker watcher 或 reducer 任一先看到请求，都可开始消费回答。"""
        return (
            self.pending_input is not None
            or self.state.awaiting_user_input
        )

    def wait_tasks(self) -> set[asyncio.Task[Any]]:
        """返回前台循环应等待的任务集合。"""
        tasks: set[asyncio.Task[Any]] = set()
        if self.active_query is not None:
            tasks.add(self.active_query.task)
        if self.consumer_task is not None:
            tasks.add(self.consumer_task)
        if self.approval_task is not None:
            tasks.add(self.approval_task)
        if self.input_task is not None:
            tasks.add(self.input_task)
        return tasks

    def arm_approval_watcher(self) -> Optional[asyncio.Task[Any]]:
        """没有待展示审批时，常驻等待 broker 的下一个请求。"""
        if (self.approval_broker is None
                or self.pending_approval is not None):
            return None
        if self.approval_task is not None:
            return self.approval_task
        self.approval_task = asyncio.create_task(
            self.approval_broker.next_request())
        return self.approval_task

    def arm_input_watcher(
        self,
    ) -> Optional[asyncio.Task[Optional[UserInputRequest]]]:
        """没有待展示输入时，常驻等待 broker 的下一个请求。"""
        if self.input_broker is None or self.pending_input is not None:
            return None
        if self.input_task is not None:
            return self.input_task
        self.input_task = asyncio.create_task(
            self.input_broker.next_request())
        return self.input_task

    async def process_completed(
        self,
        done: set[asyncio.Task[Any]],
    ) -> bool:
        """处理前台 ``asyncio.wait`` 的结果；返回是否发生了查询收尾。"""
        handled = False
        if self.input_task is not None and self.input_task in done:
            watcher = self.input_task
            self.input_task = None
            input_request: Optional[UserInputRequest] = None
            try:
                input_request = watcher.result()
            except asyncio.CancelledError:
                input_request = None
            except Exception as exc:
                self._on_notice(
                    f"用户输入监听失败: {type(exc).__name__}: {exc}")
            if input_request is not None:
                self.pending_input = input_request
                self._on_user_input(input_request)
                handled = True

        if self.approval_task is not None and self.approval_task in done:
            watcher = self.approval_task
            self.approval_task = None
            approval_request: Optional[Any] = None
            try:
                approval_request = watcher.result()
            except asyncio.CancelledError:
                approval_request = None
            except Exception as exc:  # broker/transport 故障必须可见
                self._on_notice(
                    f"审批监听失败: {type(exc).__name__}: {exc}")
            if approval_request is not None:
                self.pending_approval = approval_request
                self._on_approval(approval_request)
                handled = True

        if (self.pending_approval is not None
                and self.approval_broker is not None
                and self.approval_broker.pending is None):
            self.pending_approval = None
            self._on_notice("审批请求已取消或过期")
            self.arm_approval_watcher()
            handled = True

        if (self.pending_input is not None
                and self.input_broker is not None
                and self.input_broker.pending is None
                and self.pending_input.future.done()):
            self.pending_input = None
            self._on_notice("用户输入请求已取消或过期")
            self.arm_input_watcher()
            handled = True

        if self.active_query is not None:
            query_done = self.active_query.task in done
            consumer_done = (
                self.consumer_task is not None and self.consumer_task in done)
            if query_done or consumer_done:
                await self.finish_current()
                handled = True
        return handled

    def start(self, prompt: SessionPrompt) -> QuerySession:
        """启动一轮查询并创建有序事件消费者。"""
        if self.active_query is not None:
            raise RuntimeError("已有任务正在运行")
        query = self.controller.start_query(
            prompt.text,
            images=list(prompt.images) or None,
            tool_allowlist=(
                list(prompt.tool_allowlist)
                if prompt.tool_allowlist is not None else None),
            reminder_ids=(
                list(prompt.reminder_ids) if prompt.reminder_ids else None),
        )
        self.state.begin_turn(turn_id=query.turn_id)
        self.active_query = query
        self.active_prompt = prompt
        self.consumer_task = asyncio.create_task(self._consume(query))
        self._on_status(self.state)
        self.arm_approval_watcher()
        self.arm_input_watcher()
        return query

    def _handle_query_event(self, query_event: QueryEvent) -> None:
        event = query_event.event
        if isinstance(event, PlanUpdated):
            self.controller.record_plan(
                event.items,
                event.statuses,
                active_index=event.active_index,
                explanation=event.explanation,
            )
            self.state.plan_approval_status = (
                self.controller.plan_manager.status.value)
        self._on_event(event)

    async def _consume(self, query: QuerySession) -> None:
        async for item in query.events:
            self.last_envelope = self.event_router.route(item)
            self._on_status(self.state)

    def queue_prompt(self, prompt: SessionPrompt) -> int:
        position = self.queue.append(prompt)
        self._on_status(self.state)
        return position

    def start_queued(self) -> bool:
        """启动队列中的下一条；调用方负责仅在本轮收尾后触发。"""
        if self.active_query is not None or not self.queue:
            return False
        prompt = self.queue.popleft()
        if self.queue:
            self._on_notice(f"启动下一条消息；队列还剩 {len(self.queue)} 条")
        self.start(prompt)
        return True

    def resolve_approval(self, raw: str) -> bool:
        """提交当前审批答案；过期请求按 fail-closed 处理。"""
        if self.approval_broker is None or self.pending_approval is None:
            return False
        answer = self.approval_broker.resolve(raw)
        self.pending_approval = None
        if answer is None:
            self._on_notice("审批请求已取消或过期")
        else:
            self._on_notice(
                f"审批结果: {'已允许' if answer in ('yes', 'always') else '已拒绝'}")
        self.arm_approval_watcher()
        self._on_status(self.state)
        return True

    def resolve_user_input(self, raw: str) -> bool:
        """提交当前用户输入；无效选项保留请求并返回 False。"""
        if self.input_broker is None:
            return False
        request_id = (
            self.pending_input.request_id
            if self.pending_input is not None
            else self.state.input_request_id
        )
        if not request_id:
            return False
        resolution: InputResolution = self.input_broker.resolve(
            raw, request_id=request_id)
        if not resolution.accepted:
            self._on_notice(resolution.reason or "输入无效，请重试")
            self._on_status(self.state)
            return False
        self.pending_input = None
        self._on_notice("已提交用户输入")
        self.arm_input_watcher()
        self._on_status(self.state)
        return True

    def reject_pending_approval(self) -> None:
        """拒绝 broker 中当前与排队的全部审批，并停掉旧 watcher。"""
        if self.approval_broker is not None:
            self.approval_broker.cancel_all()
        self.pending_approval = None
        if self.approval_task is not None and not self.approval_task.done():
            self.approval_task.cancel()
        self.approval_task = None

    def reject_pending_input(self) -> None:
        """取消 broker 中当前与排队的全部用户输入请求。"""
        if self.input_broker is not None:
            self.input_broker.cancel_all()
        self.pending_input = None
        if self.input_task is not None and not self.input_task.done():
            self.input_task.cancel()
        self.input_task = None

    async def finish_current(
        self,
        *,
        interrupt: bool = False,
        run_queued: bool = True,
        show_stopped_note: bool = True,
    ) -> None:
        """统一收尾：中止（可选）、drain 事件、清理审批、启动下一条。"""
        current = self.active_query
        if current is None:
            return

        approval_watcher = self.approval_task
        input_watcher = self.input_task
        if interrupt and not current.task.done():
            self.state.request_stop()
            self.reject_pending_approval()
            self.reject_pending_input()
            await self.controller.stop()
        elif interrupt:
            self.reject_pending_approval()
            self.reject_pending_input()

        if interrupt and approval_watcher is not None:
            if not approval_watcher.done():
                approval_watcher.cancel()
            await asyncio.gather(approval_watcher, return_exceptions=True)
            if self.approval_task is approval_watcher:
                self.approval_task = None
        if interrupt and input_watcher is not None:
            if not input_watcher.done():
                input_watcher.cancel()
            await asyncio.gather(input_watcher, return_exceptions=True)
            if self.input_task is input_watcher:
                self.input_task = None

        consumer = self.consumer_task
        consumer_error: Optional[BaseException] = None
        if consumer is not None:
            result = await asyncio.gather(consumer, return_exceptions=True)
            if result and isinstance(result[0], BaseException):
                consumer_error = result[0]

        cancelled = current.task.cancelled()
        error: Optional[BaseException] = consumer_error
        if not cancelled and error is None:
            try:
                current.task.result()
            except BaseException as exc:  # noqa: BLE001 - 前台展示全部终态
                error = exc

        if cancelled or error is not None:
            self._on_stream_end()
        if cancelled and show_stopped_note:
            self._on_notice("⎿ 已中止（历史已修复，可继续对话）")
        elif error is not None:
            self._on_notice(f"错误：{type(error).__name__}: {error}")

        self.state.finish_turn()
        self.active_query = None
        self.active_prompt = None
        self.consumer_task = None
        self.pending_approval = None
        self.pending_input = None
        self._on_status(self.state)
        self.drain_steering_notices()
        if run_queued:
            self.start_queued()
        self.arm_approval_watcher()
        self.arm_input_watcher()

    def drain_steering_notices(self) -> None:
        """任务结束后丢弃未送达插话（可见地），避免污染下一轮。"""
        for leftover in self.controller.drain_steering():
            text = getattr(leftover, "text", leftover)
            self._on_notice(f"⎿ 任务已结束，未送达的插话已丢弃: {str(text)[:60]}")

    async def close(self) -> None:
        """释放 query 与审批 watcher；可重复调用。"""
        if self.active_query is not None:
            await self.finish_current(
                interrupt=True,
                run_queued=False,
                show_stopped_note=False,
            )
        if self.approval_task is not None:
            if not self.approval_task.done():
                self.approval_task.cancel()
            await asyncio.gather(self.approval_task, return_exceptions=True)
            self.approval_task = None
        self.reject_pending_approval()
        if self.input_task is not None:
            if not self.input_task.done():
                self.input_task.cancel()
            await asyncio.gather(self.input_task, return_exceptions=True)
            self.input_task = None
        self.reject_pending_input()
