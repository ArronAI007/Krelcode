"""端到端集成：write → run（失败）→ edit 修复 → run（通过）→ 总结。

覆盖 coding agent 最核心的工作流，全链路（AgentLoop + MockProvider + 真实工具）。
"""

import asyncio
import sys
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
from krelcode.tools.builtin.run_command import RunCommandTool
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.tools.registry import ToolRegistry

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
# sys.executable: 不依赖测试环境 PATH；-B: 禁写字节码缓存——
# 否则「编辑后同秒内重跑且文件大小不变」会命中 stale __pycache__（真实陷阱）
CHECK = f"'{sys.executable}' -B -c 'from func import add; assert add(2, 3) == 5; print(\"PASS\")'"


def test_fix_bug_until_check_passes(tmp_path):
    scripted = [
        # 1) 写出带 bug 的实现
        Message.assistant(tool_calls=[ToolCall(
            "w1", "write_file", {"path": "func.py", "content": BUGGY})]),
        # 2) 运行检查 → 会失败（AssertionError）
        Message.assistant(tool_calls=[ToolCall(
            "c1", "run_command", {"command": CHECK})]),
        # 3) 根据错误输出修复
        Message.assistant(tool_calls=[ToolCall(
            "e1", "edit_file",
            {"path": "func.py", "old_string": "return a - b", "new_string": "return a + b"})]),
        # 4) 重跑检查 → PASS
        Message.assistant(tool_calls=[ToolCall(
            "c2", "run_command", {"command": CHECK})]),
        # 5) 总结
        "已修复 add()：检查命令输出 PASS。",
    ]
    loop = AgentLoop(
        llm=MockProvider(scripted=scripted),
        registry=ToolRegistry([WriteFileTool(), EditFileTool(), RunCommandTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=EventBus(),
        system_prompt=build_system_prompt(),
    )
    events = []
    loop.bus.subscribe(events.append)

    reply = asyncio.run(loop.run("写 add(a,b) 并保证 assert add(2,3)==5 通过"))

    # 文件系统最终状态正确
    assert (tmp_path / "func.py").read_text(encoding="utf-8") == FIXED
    assert reply == "已修复 add()：检查命令输出 PASS。"

    # 工具调用序列与成败：第一次 run 失败，修复后第二次成功
    completed = [e for e in events if isinstance(e, ToolCallCompleted)]
    assert [(e.tool_call.name, e.is_error) for e in completed] == [
        ("write_file", False),
        ("run_command", True),   # AssertionError → exit 1 → is_error 回填
        ("edit_file", False),
        ("run_command", False),  # PASS
    ]

    # 历史索引：0 system,1 user,2 asst(w),3 tool(w),4 asst(run),5 tool(run失败),
    #          6 asst(edit),7 tool(edit),8 asst(run),9 tool(run成功),10 asst(总结)
    first_run_result = loop.history[5]
    assert first_run_result.role == "tool"
    assert "exit_code: 1" in first_run_result.content
    assert "AssertionError" in first_run_result.content
    # 成功的第二次运行输出 PASS
    assert loop.history[9].role == "tool" and "PASS" in loop.history[9].content
