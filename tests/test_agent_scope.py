"""显式文件解释请求的读取范围守卫。"""

import asyncio
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompts import build_system_prompt
from krelcode.agent.scope import infer_read_scope
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.glob import GlobTool
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.registry import ToolRegistry


def _workspace(tmp_path: Path):
    context = tmp_path / "chapter1" / "context"
    context.mkdir(parents=True)
    target = context / "run_experiment_1_1.py"
    target.write_text("print('target')\n", encoding="utf-8")
    (context / "README.md").write_text("other file\n", encoding="utf-8")
    return target


def test_infer_scope_for_explicit_explanation(tmp_path):
    target = _workspace(tmp_path)
    scope = infer_read_scope(
        tmp_path, "请解释 `chapter1/context/run_experiment_1_1.py`")
    assert scope is not None
    assert scope.allowed_files == (target.resolve(),)


def test_action_request_does_not_enable_read_scope(tmp_path):
    _workspace(tmp_path)
    assert infer_read_scope(tmp_path, "修复 chapter1/context/run_experiment_1_1.py") is None
    assert infer_read_scope(tmp_path, "解释一下这个项目") is None


def test_action_word_inside_filename_does_not_disable_scope(tmp_path):
    target = tmp_path / "run.py"
    target.write_text("print('run')\n", encoding="utf-8")
    scope = infer_read_scope(tmp_path, "解释 run.py")
    assert scope is not None
    assert scope.allowed_files == (target.resolve(),)


def test_explanation_scope_blocks_glob_and_sibling_reads(tmp_path):
    target = _workspace(tmp_path)
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[ToolCall(
            "c1", "glob", {"pattern": "chapter1/context/*"})]),
        Message.assistant(tool_calls=[ToolCall(
            "c2", "read_file", {"path": "chapter1/context/README.md"})]),
        Message.assistant(tool_calls=[ToolCall(
            "c3", "read_file", {"path": "chapter1/context/run_experiment_1_1.py"})]),
        "解释完成",
    ])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([GlobTool(), ReadFileTool()]),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt=build_system_prompt(),
    )

    reply = asyncio.run(loop.run(
        "请解释 chapter1/context/run_experiment_1_1.py"))

    assert reply == "解释完成"
    tool_messages = [m for m in loop.history if m.role == "tool"]
    assert "已阻止范围外工具调用 glob" in tool_messages[0].content
    assert "已阻止读取范围外文件" in tool_messages[1].content
    assert "print('target')" in tool_messages[2].content
    assert target.exists()
