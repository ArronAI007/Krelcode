"""端到端集成：AgentLoop + MockProvider + 真实文件工具（tmp 工作区，无网络）。

脚本一个典型 coding 流程：write_file → read_file 检查 → edit_file 修改，
验证文件系统真实效果、工具结果回填、事件序列。
"""

import asyncio
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompts import build_system_prompt
from krelcode.core.events import EventBus, ToolCallCompleted
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.edit_file import EditFileTool
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.tools.registry import ToolRegistry


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


def build_agent(workspace: Path, scripted):
    provider = MockProvider(scripted=scripted)
    registry = ToolRegistry([ReadFileTool(), WriteFileTool(), EditFileTool()])
    bus = EventBus()
    loop = AgentLoop(
        llm=provider,
        registry=registry,
        tool_context=ToolContext(workspace_root=workspace),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt=build_system_prompt(),
    )
    return loop, provider, bus


def test_write_read_edit_cycle(workspace):
    code_v1 = "def add(a, b):\n    return a - b\n"
    scripted = [
        # 1) 写一个有 bug 的文件
        Message.assistant(tool_calls=[ToolCall(
            "w1", "write_file", {"path": "mathx.py", "content": code_v1})]),
        # 2) 读回来检查
        Message.assistant(tool_calls=[ToolCall(
            "r1", "read_file", {"path": "mathx.py"})]),
        # 3) 修复 bug
        Message.assistant(tool_calls=[ToolCall(
            "e1", "edit_file",
            {"path": "mathx.py", "old_string": "return a - b", "new_string": "return a + b"})]),
        # 4) 总结
        "已修复：add() 现在返回 a + b。",
    ]
    loop, provider, bus = build_agent(workspace, scripted)
    events = []
    bus.subscribe(events.append)

    reply = asyncio.run(loop.run("写一个 add 函数并保证正确"))

    # 文件系统真实效果
    assert (workspace / "mathx.py").read_text(encoding="utf-8") == \
        "def add(a, b):\n    return a + b\n"
    assert reply == "已修复：add() 现在返回 a + b。"

    # 三次工具调用都成功
    completed = [e for e in events if isinstance(e, ToolCallCompleted)]
    assert [e.tool_call.name for e in completed] == ["write_file", "read_file", "edit_file"]
    assert not any(e.is_error for e in completed)

    # 历史结构：system,user, (assistant+tool)*3, assistant(final)
    roles = [m.role for m in loop.history]
    assert roles == ["system", "user",
                     "assistant", "tool", "assistant", "tool", "assistant", "tool",
                     "assistant"]
    # read_file 的结果里能看到带行号的原始（buggy）代码
    assert "return a - b" in loop.history[5].content

    # 每一步 LLM 都拿到了统一 ToolSpec（3 个文件工具）
    assert {t["name"] for t in provider.tools_seen[0]} == {"read_file", "write_file", "edit_file"}


def test_tool_error_reaches_llm_for_self_correction(workspace):
    scripted = [
        # 1) 读不存在的文件 → is_error 回填
        Message.assistant(tool_calls=[ToolCall(
            "r1", "read_file", {"path": "ghost.py"})]),
        # 2) LLM "自我修正"：改用 write_file 创建
        Message.assistant(tool_calls=[ToolCall(
            "w1", "write_file", {"path": "ghost.py", "content": "# created\n"})]),
        "文件原本不存在，我已创建 ghost.py。",
    ]
    loop, _, _ = build_agent(workspace, scripted)
    reply = asyncio.run(loop.run("看看 ghost.py"))

    assert "不存在" in loop.history[3].content          # 错误回填给 LLM
    assert (workspace / "ghost.py").exists()            # 修正动作生效
    assert reply == "文件原本不存在，我已创建 ghost.py。"
