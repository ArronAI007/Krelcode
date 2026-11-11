"""Step 5 主循环加固测试：并行执行、结果保序、卡死检测。"""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import EventBus, ToolCallCompleted
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry

CTX = ToolContext(workspace_root=Path("."))


class SleepTool:
    """记录执行时间窗，用于验证并行。"""
    name = "sleepy"
    description = "sleep for the given seconds"
    parameters: Dict[str, Any] = {"type": "object", "properties": {"s": {"type": "number"}}, "required": ["s"]}

    def __init__(self):
        self.windows: List[tuple] = []

    async def execute(self, args, ctx):
        start = time.monotonic()
        await asyncio.sleep(float(args.get("s", 0)))
        end = time.monotonic()
        self.windows.append((start, end))
        return ToolResult(content=f"slept {args.get('s')}")


class FlakyTool:
    """按预设失败表执行：fail_plan[i]=True 表示第 i 次调用失败。"""
    name = "flaky"
    description = "sometimes fails"
    parameters: Dict[str, Any] = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": []}

    def __init__(self, fail_plan: List[bool]):
        self.fail_plan = list(fail_plan)
        self.calls = 0

    async def execute(self, args, ctx):
        i = self.calls
        self.calls += 1
        fails = self.fail_plan[i] if i < len(self.fail_plan) else False
        if fails:
            return ToolResult(content=f"第 {i+1} 次失败", is_error=True)
        return ToolResult(content=f"第 {i+1} 次成功")


def build(scripted, tools, **kw):
    provider = MockProvider(scripted=scripted)
    loop = AgentLoop(
        llm=provider, registry=ToolRegistry(tools), tool_context=CTX,
        policy=AllowAllPolicy(), bus=EventBus(), system_prompt="SYS", **kw,
    )
    return loop, provider


def tc(call_id, name, args):
    return Message.assistant(tool_calls=[ToolCall(call_id, name, args)])


def multi_tc(*pairs):
    return Message.assistant(tool_calls=[ToolCall(i, n, a) for i, n, a in pairs])


# -- 并行执行 ----------------------------------------------------------------------

def test_batch_tool_calls_run_in_parallel():
    tool = SleepTool()
    # 一条 assistant 消息带 3 个 sleep(0.2)：串行需 ≥0.6s，并行约 0.2s
    script = [
        multi_tc(("a", "sleepy", {"s": 0.2}), ("b", "sleepy", {"s": 0.2}), ("c", "sleepy", {"s": 0.2})),
        "全部完成",
    ]
    loop, _ = build(script, [tool])
    start = time.monotonic()
    reply = asyncio.run(loop.run("并行睡觉"))
    elapsed = time.monotonic() - start

    assert reply == "全部完成"
    assert elapsed < 0.55, f"应并行执行，实际耗时 {elapsed:.2f}s"
    # 三个时间窗互相重叠（任意两个的起点都早于对方的终点）
    for i in range(3):
        for j in range(i + 1, 3):
            s1, e1 = tool.windows[i]
            s2, e2 = tool.windows[j]
            assert s1 < e2 and s2 < e1, "时间窗未重叠，说明没有并行"


def test_parallel_results_backfilled_in_original_order():
    tool = SleepTool()
    # a 睡 0.3s 后完成，b 立即完成：回填顺序仍须按 tool_call 原序
    script = [
        multi_tc(("id-a", "sleepy", {"s": 0.3}), ("id-b", "sleepy", {"s": 0})),
        "ok",
    ]
    loop, _ = build(script, [tool])
    asyncio.run(loop.run("x"))
    tool_msgs = [m for m in loop.history if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["id-a", "id-b"]
    assert "slept 0.3" in tool_msgs[0].content


# -- 卡死检测 ------------------------------------------------------------------------

def test_repeat_failure_aborts_run():
    # 同一签名连续失败 3 次（默认阈值）→ 中止，不再消耗第 4 段脚本
    script = [
        tc("c1", "flaky", {"x": 1}),
        tc("c2", "flaky", {"x": 1}),
        tc("c3", "flaky", {"x": 1}),
        "这段不应被消费",
    ]
    tool = FlakyTool(fail_plan=[True, True, True])
    loop, provider = build(script, [tool], max_steps=10)
    reply = asyncio.run(loop.run("x"))

    assert "判定为循环" in reply and "连续失败 3 次" in reply
    assert len(provider.calls) == 3          # 第 4 次 LLM 调用没有发生
    assert tool.calls == 3
    assert loop.history[-1].role == "assistant"


def test_custom_repeat_limit():
    script = [tc("c1", "flaky", {}), tc("c2", "flaky", {}), "不应消费"]
    tool = FlakyTool(fail_plan=[True, True])
    loop, provider = build(script, [tool], repeat_failure_limit=2)
    reply = asyncio.run(loop.run("x"))
    assert "连续失败 2 次" in reply
    assert len(provider.calls) == 2


def test_success_resets_failure_counter():
    # 失败(1) → 成功(重置) → 失败(1) → 成功 → 最终文本：不应触发中止
    script = [
        tc("c1", "flaky", {"x": 1}),
        tc("c2", "flaky", {"x": 1}),
        tc("c3", "flaky", {"x": 1}),
        tc("c4", "flaky", {"x": 1}),
        "任务完成",
    ]
    tool = FlakyTool(fail_plan=[True, False, True, False])
    loop, _ = build(script, [tool], repeat_failure_limit=2)
    reply = asyncio.run(loop.run("x"))
    assert reply == "任务完成"


def test_different_args_do_not_share_counter():
    # 相同工具不同参数 = 不同签名，各自计数，不触发中止
    script = [
        tc("c1", "flaky", {"x": 1}),
        tc("c2", "flaky", {"x": 2}),
        tc("c3", "flaky", {"x": 1}),
        tc("c4", "flaky", {"x": 2}),
        "完成",
    ]
    tool = FlakyTool(fail_plan=[True, True, True, True])
    loop, _ = build(script, [tool], repeat_failure_limit=3)
    reply = asyncio.run(loop.run("x"))
    assert reply == "完成"  # 每个签名只连败 2 次，未到阈值 3
