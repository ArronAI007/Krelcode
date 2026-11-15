"""交互会话控制层：命令结果、任务生命周期与 UI 解耦。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.controller import SessionController
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.time_tool import GetTimeTool
from krelcode.tools.registry import ToolRegistry

ROOT = Path(__file__).parent.parent


def make_loop(tmp_path):
    return AgentLoop(
        llm=MockProvider(),
        registry=ToolRegistry([GetTimeTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_non_command_is_unhandled(tmp_path):
    controller = SessionController(make_loop(tmp_path))
    result = asyncio.run(controller.execute_command("解释这个文件"))
    assert result.handled is False
    assert result.output == ()


def test_command_returns_structured_output_without_printing(tmp_path, capsys):
    controller = SessionController(make_loop(tmp_path), session_path="/tmp/s.jsonl")
    result = asyncio.run(controller.execute_command("/session"))
    captured = capsys.readouterr()
    assert result.handled is True
    assert result.output == ("  当前会话文件: /tmp/s.jsonl",)
    assert captured.out == ""


def test_task_lifecycle_and_steering_stay_in_controller(tmp_path):
    loop = make_loop(tmp_path)
    started = asyncio.Event()

    async def slow_run(message, images=None):
        started.set()
        await asyncio.sleep(30)
        return message

    loop.run = slow_run

    async def scenario():
        controller = SessionController(loop)
        task = controller.start_run("任务")
        await started.wait()
        assert controller.active_task is task
        assert controller.is_running is True
        assert controller.steer("补充要求") == "补充要求"
        assert controller.drain_steering() == ["补充要求"]
        assert await controller.stop() is True
        assert controller.is_running is False
        assert await controller.stop() is False

    asyncio.run(scenario())


def test_controller_import_does_not_pull_terminal_ui():
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys\n"
         "import krelcode.session.controller\n"
         "assert 'krelcode.ui' not in sys.modules\n"
         "assert 'krelcode.ui.repl' not in sys.modules\n"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
