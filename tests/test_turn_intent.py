"""本轮意图识别与工具空间路由。"""

import asyncio
from pathlib import Path

from krelcode.agent.intent import MODE_EXPLAIN_FILES, MODE_GENERAL, infer_turn_intent
from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompts import build_system_prompt
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.builtin.glob import GlobTool
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.tools.registry import ToolRegistry


def _target(tmp_path: Path) -> Path:
    target = tmp_path / "chapter1" / "context" / "run_experiment_1_1.py"
    target.parent.mkdir(parents=True)
    target.write_text("print('target')\n", encoding="utf-8")
    return target


def _loop(tmp_path: Path, provider: MockProvider) -> AgentLoop:
    return AgentLoop(
        llm=provider,
        registry=ToolRegistry([GlobTool(), ReadFileTool(), WriteFileTool()]),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt=build_system_prompt(),
    )


def test_explicit_file_explanation_is_recognized(tmp_path):
    target = _target(tmp_path)
    intent = infer_turn_intent(
        tmp_path, "请解释 `chapter1/context/run_experiment_1_1.py`")
    assert intent.mode == MODE_EXPLAIN_FILES
    assert intent.target_files == (target.resolve(),)


def test_action_or_ambiguous_request_stays_general(tmp_path):
    _target(tmp_path)
    assert infer_turn_intent(
        tmp_path, "修复 chapter1/context/run_experiment_1_1.py").mode == MODE_GENERAL
    assert infer_turn_intent(tmp_path, "解释一下这个项目").mode == MODE_GENERAL


def test_action_word_inside_filename_does_not_override_explanation(tmp_path):
    target = tmp_path / "run.py"
    target.write_text("print('run')\n", encoding="utf-8")
    assert infer_turn_intent(tmp_path, "解释 run.py").mode == MODE_EXPLAIN_FILES


def test_action_words_describing_code_do_not_override_explanation(tmp_path):
    target = tmp_path / "worker.py"
    target.write_text("def run(): pass\n", encoding="utf-8")
    assert infer_turn_intent(
        tmp_path, "解释 worker.py 里的 run 和 test 函数").mode == MODE_EXPLAIN_FILES


def test_explain_and_then_run_uses_action_intent(tmp_path):
    target = tmp_path / "worker.py"
    target.write_text("print('ok')\n", encoding="utf-8")
    assert infer_turn_intent(
        tmp_path, "解释 worker.py，然后运行它").mode == MODE_GENERAL


def test_attached_file_body_does_not_affect_intent(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("修改 测试 运行\n", encoding="utf-8")
    prompt = (
        "解释 @sample.py\n"
        '<attached_file path="sample.py">\n修改 测试 运行\n</attached_file>'
    )
    intent = infer_turn_intent(tmp_path, prompt)
    assert intent.mode == MODE_EXPLAIN_FILES
    assert intent.target_files == (target.resolve(),)


def test_code_fence_body_does_not_affect_intent(tmp_path):
    _target(tmp_path)
    prompt = (
        "请评审以下变更：\n```diff\n+ read config.py and run tests\n"
        "+ explain helper.py\n```"
    )
    assert infer_turn_intent(tmp_path, prompt).mode == MODE_GENERAL


def test_explain_intent_is_injected_before_model_call(tmp_path):
    target = _target(tmp_path)
    provider = MockProvider(scripted=["解释完成"])
    loop = _loop(tmp_path, provider)

    asyncio.run(loop.run(
        "请解释 chapter1/context/run_experiment_1_1.py"))

    sent = provider.calls[0]
    assert '<turn_intent mode="explain_files"' in sent[0].content
    assert target.name in sent[0].content
    assert "Do not glob, grep" in sent[0].content
    assert {spec["name"] for spec in provider.tools_seen[0]} == {"read_file"}
    # 意图提示只影响本轮 provider 输入，不写入历史。
    assert all("<turn_intent" not in (m.content or "") for m in loop.history)


def test_general_request_keeps_full_toolset_without_intent_hint(tmp_path):
    _target(tmp_path)
    provider = MockProvider(scripted=["完成"])
    loop = _loop(tmp_path, provider)

    asyncio.run(loop.run("概括这个项目的结构"))

    assert all("<turn_intent" not in (m.content or "") for m in provider.calls[0])
    assert {spec["name"] for spec in provider.tools_seen[0]} == {
        "glob", "read_file", "write_file",
    }


class SpyGlobTool:
    name = "glob"
    description = "列出文件"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, args, ctx):
        self.calls += 1
        return ToolResult(content="should not execute")


def test_unexposed_tool_is_blocked_at_execution_boundary(tmp_path):
    target = _target(tmp_path)
    glob = SpyGlobTool()
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[
            ToolCall("c1", glob.name, {})
        ]),
        "已改为直接读取目标文件",
    ])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([glob, ReadFileTool()]),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
    )

    reply = asyncio.run(loop.run(
        f"请解释 {target.relative_to(tmp_path).as_posix()}"))

    assert reply == "已改为直接读取目标文件"
    assert {spec["name"] for spec in provider.tools_seen[0]} == {"read_file"}
    assert glob.calls == 0
    assert loop.history[-2].role == "tool"
    assert "工具在本轮不可用: glob" in loop.history[-2].content
