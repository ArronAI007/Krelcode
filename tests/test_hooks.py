"""Step 19 测试：Hook 系统（解析/触发映射/变量 quote/超时/失败安全/e2e）。"""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import EventBus, RunFinished, ToolCallCompleted
from krelcode.core.messages import Message, ToolCall
from krelcode.hooks import HookRunner, HookSpec, parse_hooks
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.tools.registry import ToolRegistry


# -- 配置解析 ------------------------------------------------------------------------------

def test_parse_hooks_valid_and_invalid():
    config = {"hooks": [
        {"on": "file_edited", "command": "fmt {path}"},
        {"on": "run_finished", "command": "notify", "timeout": 5},
        {"on": "bogus_event", "command": "x"},          # 未知触发器 → 跳过
        {"on": "tool_failed"},                          # 缺 command → 跳过
        {"on": "tool_completed", "command": "  "},       # 空 command → 跳过
        "not-a-dict",
    ]}
    specs = parse_hooks(config)
    assert [(s.on, s.command, s.timeout) for s in specs] == [
        ("file_edited", "fmt {path}", 60.0),
        ("run_finished", "notify", 5.0),
    ]
    assert parse_hooks({}) == []
    assert parse_hooks({"hooks": "not-a-list"}) == []


# -- 触发与执行 --------------------------------------------------------------------------------

def make_runner(tmp_path, specs, logs=None):
    # 注意：不能写 `logs or []`——空列表为 falsy 会绑到新列表上
    sink = logs if logs is not None else []
    return HookRunner(tmp_path, specs, log=sink.append)


def emit_completed(bus, name, args, content="ok", is_error=False):
    return bus.emit(ToolCallCompleted(ToolCall("c1", name, args), content, is_error))


def test_file_edited_hook_runs_with_path_var(tmp_path):
    logs = []
    runner = make_runner(tmp_path, [
        HookSpec(on="file_edited", command="echo {path} > hook.log"),
    ], logs)
    bus = EventBus()
    runner.attach(bus)
    asyncio.run(emit_completed(bus, "write_file", {"path": "a.txt"}))
    assert (tmp_path / "hook.log").read_text().strip() == "a.txt"
    assert runner.executions[0]["rc"] == 0
    assert any("[hook:file_edited]" in l for l in logs)


def test_path_with_spaces_and_injection_is_quoted(tmp_path):
    runner = make_runner(tmp_path, [
        HookSpec(on="file_edited", command="echo {path} > hook.log"),
    ])
    bus = EventBus()
    runner.attach(bus)
    evil = "a b.txt; touch PWNED"     # LLM 参数里的恶意内容必须被 quote
    asyncio.run(emit_completed(bus, "edit_file", {"path": evil}))
    assert (tmp_path / "hook.log").read_text().strip() == evil
    assert not (tmp_path / "PWNED").exists()


def test_tool_failed_only_on_error(tmp_path):
    runner = make_runner(tmp_path, [
        HookSpec(on="tool_failed", command="echo {tool} >> failed.log"),
        HookSpec(on="tool_completed", command="echo {tool}:{is_error} >> all.log"),
    ])
    bus = EventBus()
    runner.attach(bus)

    async def go():
        await emit_completed(bus, "run_command", {"command": "x"}, content="ok", is_error=False)
        await emit_completed(bus, "run_command", {"command": "x"}, content="boom", is_error=True)

    asyncio.run(go())
    assert not (tmp_path / "failed.log").exists() or \
        (tmp_path / "failed.log").read_text() == "run_command\n"   # 只有失败那次
    assert (tmp_path / "all.log").read_text().splitlines() == \
        ["run_command:false", "run_command:true"]


def test_write_tool_error_does_not_trigger_file_edited(tmp_path):
    runner = make_runner(tmp_path, [
        HookSpec(on="file_edited", command="echo x >> edited.log"),
    ])
    bus = EventBus()
    runner.attach(bus)
    asyncio.run(emit_completed(bus, "write_file", {"path": "a.txt"},
                               content="越出工作区", is_error=True))
    assert not (tmp_path / "edited.log").exists()


def test_run_finished_reply_var(tmp_path):
    runner = make_runner(tmp_path, [
        HookSpec(on="run_finished", command="echo {reply} > done.log"),
    ])
    bus = EventBus()
    runner.attach(bus)
    asyncio.run(bus.emit(RunFinished("任务完成了")))
    assert (tmp_path / "done.log").read_text().strip() == "任务完成了"


def test_hook_failure_does_not_raise(tmp_path):
    logs = []
    runner = make_runner(tmp_path, [
        HookSpec(on="run_finished", command="exit 3"),
    ], logs)
    bus = EventBus()
    runner.attach(bus)
    asyncio.run(bus.emit(RunFinished("x")))     # 不应抛异常
    assert runner.executions[0]["rc"] == 3


def test_hook_timeout_terminated(tmp_path):
    logs = []
    runner = make_runner(tmp_path, [
        HookSpec(on="run_finished", command="sleep 30", timeout=0.5),
    ], logs)
    bus = EventBus()
    runner.attach(bus)
    start = time.monotonic()
    asyncio.run(bus.emit(RunFinished("x")))
    assert time.monotonic() - start < 5
    assert "超时" in runner.executions[0]["output"]


# -- e2e：真实 AgentLoop 一轮写文件触发 formatter 钩子 ------------------------------------------

def test_e2e_agent_loop_triggers_hook(tmp_path):
    logs = []
    bus = EventBus()
    runner = HookRunner(tmp_path, [
        HookSpec(on="file_edited", command="echo {path} >> edited.log"),
        HookSpec(on="run_finished", command="echo done >> edited.log"),
    ], log=logs.append)
    runner.attach(bus)

    scripted = [
        Message.assistant(tool_calls=[ToolCall(
            "w1", "write_file", {"path": "gen.txt", "content": "hello"})]),
        "写好了",
    ]
    loop = AgentLoop(
        llm=MockProvider(scripted=scripted),
        registry=ToolRegistry([WriteFileTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
    )
    asyncio.run(loop.run("生成 gen.txt"))

    assert (tmp_path / "gen.txt").exists()
    assert (tmp_path / "edited.log").read_text().splitlines() == ["gen.txt", "done"]
