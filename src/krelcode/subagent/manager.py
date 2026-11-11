"""SubagentManager：子代理生命周期管理。

每个子代理 = 一个独立 AgentLoop（自己的历史/事件总线/工具注册表），
共享父级的 provider、ToolContext（工作区）与审批策略。
后台执行用 asyncio.Task；父代理经 spawn/wait/send/list 四个工具驱动。

防递归：子代理的工具集由 tool_factory 提供（app 里 = default_tools()，
不含 spawn 系工具），委派深度硬限制为 1 层。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

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


@dataclass
class AgentHandle:
    id: str
    task: str
    loop: AgentLoop
    status: str = STATUS_RUNNING
    result: Optional[str] = None
    error: Optional[str] = None
    turns: int = 0
    task_obj: Optional[asyncio.Task] = field(default=None, repr=False)

    def preview(self) -> str:
        text = self.result or self.error or ""
        if len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS] + "…"
        return text.replace("\n", " ")


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
            handle.error = "任务被取消"
            raise
        except Exception as e:
            handle.status = STATUS_FAILED
            handle.error = f"{type(e).__name__}: {e}"
            handle.turns += 1
        finally:
            await self.bus.emit(
                SubagentFinished(handle.id, handle.status, handle.preview()))

    def get(self, agent_id: str) -> Optional[AgentHandle]:
        return self._handles.get(agent_id)

    def is_running(self, handle: AgentHandle) -> bool:
        return handle.task_obj is not None and not handle.task_obj.done()

    def send_input(self, agent_id: str, message: str) -> AgentHandle:
        """给已空闲的子代理追加一轮任务（上下文延续）。运行中则拒绝。"""
        handle = self._handles.get(agent_id)
        if handle is None:
            raise KeyError(f"未知子代理: {agent_id}")
        if self.is_running(handle):
            raise RuntimeError(f"子代理 {agent_id} 仍在运行，请先 wait_agent")
        handle.task = message
        handle.task_obj = asyncio.create_task(self._run(handle, message))
        return handle

    async def wait(self, agent_id: str, timeout: Optional[float] = None) -> AgentHandle:
        """等待子代理到达终态；超时返回当前状态（不取消任务）。"""
        handle = self._handles.get(agent_id)
        if handle is None:
            raise KeyError(f"未知子代理: {agent_id}")
        task = handle.task_obj
        if task is None or task.done():
            return handle
        try:
            # shield：wait 超时不能把子任务本身取消掉
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            pass  # 取消由 shutdown 统一处理
        return handle

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
            line = (f"{handle.id} [{handle.status}] {handle.turns} 轮 · "
                    f"任务: {task_preview.replace(chr(10), ' ')}")
            if handle.status in (STATUS_COMPLETED, STATUS_FAILED):
                line += f" · 结果: {handle.preview()}"
            lines.append(line)
        return lines

    def build_tools(self) -> List[Tool]:
        from .tools import build_subagent_tools
        return build_subagent_tools(self)
