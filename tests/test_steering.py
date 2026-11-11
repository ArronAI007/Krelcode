"""Step 26 测试：AgentLoop 级 steering 注入与取消修复（不经 REPL）。"""

import asyncio
from pathlib import Path
from typing import Any, Dict

import pytest

from krelcode.agent.loop import INTERRUPTED_NOTE, STEERING_PREFIX, AgentLoop
from krelcode.core.events import EventBus, UserSteered
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.base import ProviderCapabilities, StreamFinished
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


class EchoTool:
    name = "echo"
    description = "echo"
    parameters: Dict[str, Any] = {"type": "object", "properties": {"m": {"type": "string"}}}

    async def execute(self, args, ctx) -> ToolResult:
        await asyncio.sleep(float(args.get("s", 0)))
        return ToolResult(content=f"echo: {args.get('m')}")


class ToolThenDoneProvider:
    """第 1 次调用（可选延迟）返回 echo tool_call；之后返回 final。"""
    name = "tool-then-done"
    model = "m"
    capabilities = ProviderCapabilities()

    def __init__(self, delay=0.0, final="done"):
        self.delay = delay
        self.final = final

    async def stream(self, messages, tools=None):
        n_assistant = sum(1 for m in messages if m.role == "assistant")
        if self.delay and n_assistant == 0:
            await asyncio.sleep(self.delay)
        if n_assistant == 0:
            msg = Message.assistant(
                tool_calls=[ToolCall("c1", "echo", {"m": "x", "s": self.delay})])
        else:
            msg = Message.assistant(self.final)
        yield StreamFinished(message=msg, finish_reason="stop", usage={})


class SlowProvider:
    """每次调用都睡很久（用于取消测试）。可选先发 tool_call。"""
    name = "slow"
    model = "m"
    capabilities = ProviderCapabilities()

    def __init__(self, emit_tool_call=False):
        self.emit_tool_call = emit_tool_call

    async def stream(self, messages, tools=None):
        n_assistant = sum(1 for m in messages if m.role == "assistant")
        if self.emit_tool_call and n_assistant == 0:
            yield StreamFinished(
                message=Message.assistant(
                    tool_calls=[ToolCall("c1", "echo", {"m": "x", "s": 5})]),
                finish_reason="stop", usage={})
            return
        await asyncio.sleep(30)
        yield StreamFinished(message=Message.assistant("不该到达"),
                             finish_reason="stop", usage={})


def make_loop(provider, tmp_path, bus=None):
    return AgentLoop(
        llm=provider,
        registry=ToolRegistry([EchoTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=bus or EventBus(),
        system_prompt="SYS",
    )


# -- steering 注入 ----------------------------------------------------------------------------------

def test_prefilled_steering_merges_into_pending_user_message(tmp_path):
    """队列在 run 前已有消息：step1 注入时尾部是 user → 合并（防连续 user）。"""
    loop = make_loop(ToolThenDoneProvider(), tmp_path)
    loop.steering_queue.put_nowait("补充要求")

    async def go():
        return await loop.run("主任务")

    assert asyncio.run(go()) == "done"
    assert loop.history[1].role == "user"
    assert loop.history[1].content == f"主任务\n{STEERING_PREFIX}补充要求"


def test_midrun_steering_appended_after_tool_result(tmp_path):
    """运行中注入：step2 开始时尾部是 tool 结果 → 作为独立 user 消息追加（合法交替位）。"""
    loop = make_loop(ToolThenDoneProvider(delay=0.2), tmp_path)

    async def go():
        task = asyncio.create_task(loop.run("主任务"))
        await asyncio.sleep(0.05)               # 第一次 LLM 调用进行中
        loop.steering_queue.put_nowait("顺便检查拼写")
        return await task

    assert asyncio.run(go()) == "done"
    roles = [m.role for m in loop.history]
    # system, user, assistant(tc), tool, user(插话), assistant(final)
    assert roles == ["system", "user", "assistant", "tool", "user", "assistant"]
    assert loop.history[4].content == f"{STEERING_PREFIX}顺便检查拼写"


def test_steering_emits_event_and_skips_blank(tmp_path):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    loop = make_loop(ToolThenDoneProvider(), tmp_path, bus=bus)
    loop.steering_queue.put_nowait("  ")        # 空白插话忽略
    loop.steering_queue.put_nowait("有效插话")

    async def go():
        return await loop.run("任务")

    asyncio.run(go())
    steered = [e for e in events if isinstance(e, UserSteered)]
    assert len(steered) == 1 and steered[0].text == "有效插话"


# -- 取消修复 ----------------------------------------------------------------------------------------

def test_cancel_during_llm_stream_repairs_history(tmp_path):
    loop = make_loop(SlowProvider(), tmp_path)

    async def go():
        task = asyncio.create_task(loop.run("慢任务"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    # 尾部是未答完的 user → 补中止说明（保角色交替，下轮可直接续聊）
    assert loop.history[-1].role == "assistant"
    assert loop.history[-1].content == INTERRUPTED_NOTE
    assert loop.history[-2] == Message.user("慢任务")


def test_cancel_during_tool_execution_pops_dangling(tmp_path):
    loop = make_loop(SlowProvider(emit_tool_call=True), tmp_path)

    async def go():
        task = asyncio.create_task(loop.run("慢工具任务"))
        await asyncio.sleep(0.15)               # 工具（echo 睡 5s）执行中
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    # 悬挂的 assistant(tool_calls) 被弹掉 → 尾部 user → 补中止说明
    roles = [m.role for m in loop.history]
    assert roles == ["system", "user", "assistant"]
    assert loop.history[-1].content == INTERRUPTED_NOTE
    assert all(not (m.role == "assistant" and m.tool_calls) for m in loop.history)


def test_after_cancel_next_run_works(tmp_path):
    """取消修复后的历史可以直接续跑（provider 视角协议合法）。"""
    provider = SlowProvider()
    loop = make_loop(provider, tmp_path)

    async def go():
        task = asyncio.create_task(loop.run("慢任务"))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # 换一个会立即完成的 provider 续聊
        loop.llm = ToolThenDoneProvider(final="续聊成功")
        return await loop.run("继续")

    assert asyncio.run(go()) == "续聊成功"
    roles = [m.role for m in loop.history]
    # system, user(慢任务), assistant(中止), user(继续), assistant(续聊成功)
    # 关键不变量：user/assistant 严格交替（provider 视角协议合法），无悬挂消息
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert not any(m.role == "tool" for m in loop.history)
