"""流式工具执行调度器。

Provider 在参数完整时发出 ``ToolCallReady``，本调度器无需等待整条 LLM 流
结束即可启动工具。执行顺序遵循模型给出的工具索引：

- 连续只读工具可以并发；
- mutating/未知工具是独占 barrier，等待之前只读任务后串行执行；
- barrier 之后的工具必须等 barrier 完成；
- 工具结果按原始 index 保存，最终回填历史时由 AgentLoop 统一排序。

调度器只负责调度，不知道审批、Sandbox、事件渲染等细节；这些仍由注入的
runner 完成，保持查询推进层与工具运行时解耦。
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Dict, List, Optional

from krelcode.core.messages import ToolCall
from krelcode.contracts.tool_execution import ToolExecutionResult
from krelcode.agent.turn.tool_pool import TurnToolPool

ToolRunner = Callable[[ToolCall], Awaitable[ToolExecutionResult]]


class StreamingToolExecutor:
    def __init__(self, pool: TurnToolPool, runner: ToolRunner) -> None:
        self._pool = pool
        self._runner = runner
        self._ready: Dict[int, ToolCall] = {}
        self._tasks: Dict[int, asyncio.Task[ToolExecutionResult]] = {}
        self._next_index = 0
        self._reader_tasks: List[asyncio.Task[ToolExecutionResult]] = []
        self._barrier_task: Optional[
            asyncio.Task[ToolExecutionResult]] = None
        self._closed = False

    @property
    def scheduled_count(self) -> int:
        return len(self._tasks)

    def register(self, index: int, tool_call: ToolCall) -> None:
        """登记一个已完整形成的工具调用；index 必须是连续工具序号。"""
        if self._closed:
            raise RuntimeError("流式工具调度器已关闭")
        if index < 0:
            raise ValueError(f"工具索引不能为负数: {index}")
        if index < self._next_index:
            existing = self._ready.get(index)
            if existing != tool_call:
                raise ValueError(
                    f"工具索引 {index} 的最终参数与增量参数不一致")
            return
        previous = self._ready.get(index)
        if previous is not None:
            if previous != tool_call:
                raise ValueError(
                    f"工具索引 {index} 收到不一致的重复 Ready 事件")
            return
        self._ready[index] = tool_call
        self._drain()

    def register_final(self, tool_calls: List[ToolCall]) -> None:
        """用 StreamFinished 的权威列表补齐旧 Provider 未发的增量调用。"""
        for index, tool_call in enumerate(tool_calls):
            self.register(index, tool_call)
        unexpected = sorted(set(self._ready) - set(range(len(tool_calls))))
        if unexpected:
            raise ValueError(
                f"Provider 增量工具列表与最终消息不一致，多余索引: {unexpected}")

    def _drain(self) -> None:
        while self._next_index in self._ready:
            index = self._next_index
            tool_call = self._ready[index]
            descriptor = self._pool.descriptor(tool_call.name)
            read_only = descriptor is not None and descriptor.read_only

            waits: List[asyncio.Task[ToolExecutionResult]] = []
            if read_only:
                if self._barrier_task is not None:
                    waits.append(self._barrier_task)
            else:
                waits.extend(self._reader_tasks)
                if self._barrier_task is not None:
                    waits.append(self._barrier_task)

            task = asyncio.create_task(
                self._run_after(
                    tuple(dict.fromkeys(waits)),
                    index,
                    tool_call,
                )
            )
            self._tasks[index] = task
            if read_only:
                self._reader_tasks.append(task)
            else:
                self._reader_tasks.clear()
                self._barrier_task = task
            self._next_index += 1

    async def _run_after(
        self,
        waits: tuple[asyncio.Task[ToolExecutionResult], ...],
        index: int,
        tool_call: ToolCall,
    ) -> ToolExecutionResult:
        if waits:
            await asyncio.gather(*waits)
        if self._closed:
            raise asyncio.CancelledError
        return await self._runner(tool_call)

    async def wait_results(self, count: int) -> List[ToolExecutionResult]:
        """等待所有已调度工具，按 0..count-1 返回结果。"""
        if len(self._tasks) != count:
            raise RuntimeError(
                f"工具执行数量不一致: 已调度 {len(self._tasks)}，期望 {count}")
        tasks = [self._tasks[i] for i in range(count)]
        try:
            results = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        self._closed = True
        return list(results)

    async def abort(self) -> None:
        """流失败/取消：停止尚未完成的调度任务。"""
        if self._closed:
            return
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
