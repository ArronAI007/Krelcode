"""SubagentManager：子代理生命周期管理。

每个子代理 = 一个独立 AgentLoop（自己的历史/事件总线/工具注册表），
共享父级的 provider、ToolContext（工作区）与审批策略。
后台执行用 asyncio.Task；父代理经 spawn/wait/send/list 四个工具驱动。

Step 23 增强（对齐 Codex multi_agent 语义）：
- **消息队列**：子代理运行中收到 send_input → 入队，当前任务完成后自动续跑，
  同一 Task 内排空队列（wait 的语义 = 队列排空）
- **interrupt**：取消当前 Task → 历史消毒（见 _sanitize_history）→ 立即
  开新 Task 处理打断消息；队列中未处理的消息保留
- **wait_many**：一次等多个子代理（asyncio.wait，超时不取消任务）

防递归：子代理的工具集由 tool_factory 提供（app 里 = default_tools()，
不含 spawn 系工具），委派深度硬限制为 1 层。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from ..agent.loop import AgentLoop
from ..agent.prompts import build_system_prompt
from ..core.events import EventBus, SubagentFinished, SubagentSpawned
from ..llm.base import LLMProvider
from ..safety.policy import AllowAllPolicy
from ..tools.base import Tool, ToolContext
from ..tools.registry import ToolRegistry

SUBAGENT_SYSTEM_PROMPT = build_system_prompt(
    "You are a Krelcode SUBAGENT working on a delegated subtask.\n"
    "- Focus strictly on the task you were given; do not ask the user questions.\n"
    "- Use tools to do the work and verify results when possible.\n"
    "- Your final text message is the report handed back to the parent agent: "
    "make it dense and self-contained (what was done, key results, file paths, caveats)."
)

PREVIEW_CHARS = 160

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

INTERRUPT_MARKER = "\n[被打断，用户新指示] "


@dataclass
class AgentHandle:
    id: str
    task: str
    loop: AgentLoop
    status: str = STATUS_RUNNING
    result: Optional[str] = None
    error: Optional[str] = None
    turns: int = 0
    queue: List[str] = field(default_factory=list)
    task_obj: Optional[asyncio.Task] = field(default=None, repr=False)

    def preview(self) -> str:
        text = self.result or self.error or ""
        if len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS] + "…"
        return text.replace("\n", " ")


def sanitize_history(history: List) -> None:
    """打断后的历史消毒（原地）。

    取消可能发生在任意点：历史尾部可能残留
    - assistant(tool_calls) 而对应 tool 结果缺失 → provider 会拒绝请求
    - 部分 tool 结果 → 与 assistant.tool_calls 不配对
    规则：从尾部弹掉所有 tool 消息与带 tool_calls 的 assistant 消息，
    直到最后一条是「纯文本 assistant」或「user」。
    """
    while history:
        last = history[-1]
        if last.role == "tool":
            history.pop()
        elif last.role == "assistant" and last.tool_calls:
            history.pop()
        else:
            break


class SubagentManager:
    def __init__(
        self,
        llm: LLMProvider,
        tool_factory: Callable[[], List[Tool]],
        tool_context: ToolContext,
        policy=None,
        bus: Optional[EventBus] = None,
        max_steps: int = 30,
    ) -> None:
        self.llm = llm
        self.tool_factory = tool_factory
        self.tool_context = tool_context
        self.policy = policy or AllowAllPolicy()
        self.bus = bus or EventBus()
        self.max_steps = max_steps
        self._handles: Dict[str, AgentHandle] = {}
        self._counter = 0

    # -- 生命周期 ---------------------------------------------------------------------

    async def spawn(self, task: str) -> AgentHandle:
        """创建一个后台子代理并立即开始执行任务。"""
        self._counter += 1
        agent_id = f"a{self._counter}"
        child_bus = EventBus()
        loop = AgentLoop(
            llm=self.llm,
            registry=ToolRegistry(self.tool_factory()),
            tool_context=self.tool_context,
            policy=self.policy,
            bus=child_bus,
            system_prompt=SUBAGENT_SYSTEM_PROMPT,
            max_steps=self.max_steps,
        )
        handle = AgentHandle(id=agent_id, task=task, loop=loop)
        self._handles[agent_id] = handle
        handle.task_obj = asyncio.create_task(self._run(handle, task))
        await self.bus.emit(SubagentSpawned(agent_id, task))
        return handle

    async def _run(self, handle: AgentHandle, message: str) -> None:
        """执行一条消息；完成后若队列非空则继续排空（同一 Task）。"""
        while True:
            handle.status = STATUS_RUNNING
            handle.result = None
            handle.error = None
            try:
                result = await handle.loop.run(message)
                handle.result = result
                handle.status = STATUS_COMPLETED
                handle.turns += 1
            except asyncio.CancelledError:
                handle.status = STATUS_CANCELLED
                handle.error = "任务被打断/取消"
                await self.bus.emit(
                    SubagentFinished(handle.id, handle.status, handle.preview()))
                raise
            except Exception as e:
                handle.status = STATUS_FAILED
                handle.error = f"{type(e).__name__}: {e}"
                handle.turns += 1
            await self.bus.emit(
                SubagentFinished(handle.id, handle.status, handle.preview()))
            if handle.queue:
                message = handle.queue.pop(0)
                continue
            return

    def get(self, agent_id: str) -> Optional[AgentHandle]:
        return self._handles.get(agent_id)

    def is_running(self, handle: AgentHandle) -> bool:
        return handle.task_obj is not None and not handle.task_obj.done()

    # -- 交互：send / interrupt -----------------------------------------------------------

    async def send_input(
        self, agent_id: str, message: str, interrupt: bool = False
    ) -> Tuple[AgentHandle, str]:
        """给子代理发消息。返回 (handle, action)。

        action ∈ started（空闲，立即开跑）/ queued（运行中，排队）/
        interrupted（打断当前任务，立即处理本消息）。
        """
        handle = self._handles.get(agent_id)
        if handle is None:
            raise KeyError(f"未知子代理: {agent_id}")

        if not self.is_running(handle):
            handle.task = message
            handle.task_obj = asyncio.create_task(self._run(handle, message))
            return handle, "started"

        if not interrupt:
            handle.queue.append(message)
            return handle, "queued"

        # interrupt：取消当前 Task → 消毒历史 → 立即处理新消息
        handle.task_obj.cancel()
        await asyncio.gather(handle.task_obj, return_exceptions=True)
        sanitize_history(handle.loop.history)
        # 历史尾部若是未答完的 user 消息，把打断指示合并进去
        # （避免连续两条 user 消息——Anthropic 要求角色交替）；
        # 合并后先弹出，_run→loop.run() 会把它作为新 user 消息重新追加
        if handle.loop.history and handle.loop.history[-1].role == "user":
            first = ((handle.loop.history[-1].content or "")
                     + INTERRUPT_MARKER + message)
            handle.loop.history.pop()
        else:
            first = message
        handle.task = message
        handle.task_obj = asyncio.create_task(self._run(handle, first))
        return handle, "interrupted"

    # -- 等待 -----------------------------------------------------------------------------

    async def wait(self, agent_id: str, timeout: Optional[float] = None) -> AgentHandle:
        """等待单个子代理队列排空；超时返回当前状态（不取消任务）。"""
        handle = self._handles.get(agent_id)
        if handle is None:
            raise KeyError(f"未知子代理: {agent_id}")
        await self._wait_handles([handle], timeout)
        return handle

    async def wait_many(
        self, agent_ids: List[str], timeout: Optional[float] = None
    ) -> List[AgentHandle]:
        """一次等待多个子代理；任一未知 id 抛 KeyError。"""
        handles = []
        for agent_id in agent_ids:
            handle = self._handles.get(agent_id)
            if handle is None:
                raise KeyError(f"未知子代理: {agent_id}")
            handles.append(handle)
        await self._wait_handles(handles, timeout)
        return handles

    @staticmethod
    async def _wait_handles(handles: List[AgentHandle], timeout: Optional[float]) -> None:
        tasks = [h.task_obj for h in handles if h.task_obj is not None and not h.task_obj.done()]
        if not tasks:
            return
        # asyncio.wait 超时不取消任务（与 shield 语义一致且支持多任务）
        await asyncio.wait(tasks, timeout=timeout)

    async def shutdown(self) -> None:
        """取消所有运行中的子代理（进程退出前调用）。"""
        running = [h for h in self._handles.values() if self.is_running(h)]
        for handle in running:
            handle.task_obj.cancel()
        if running:
            await asyncio.gather(
                *(h.task_obj for h in running), return_exceptions=True)

    # -- 观测 -------------------------------------------------------------------------

    def status_lines(self) -> List[str]:
        lines = []
        for handle in self._handles.values():
            task_preview = handle.task
            if len(task_preview) > 60:
                task_preview = task_preview[:60] + "…"
            queued = f" · 排队 {len(handle.queue)}" if handle.queue else ""
            line = (f"{handle.id} [{handle.status}] {handle.turns} 轮{queued} · "
                    f"任务: {task_preview.replace(chr(10), ' ')}")
            if handle.status in (STATUS_COMPLETED, STATUS_FAILED):
                line += f" · 结果: {handle.preview()}"
            lines.append(line)
        return lines

    def build_tools(self) -> List[Tool]:
        from .tools import build_subagent_tools
        return build_subagent_tools(self)
