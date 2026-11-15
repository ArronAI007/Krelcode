"""统一输入处理：分类、附件展开与来源保留。"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from krelcode.core.messages import ImageRef
from krelcode.session.commands import CommandRegistry, CommandSpec
from krelcode.session.input import InputEnvelope, InputKind, InputProcessor


def test_empty_prompt_is_normalized(tmp_path):
    processor = InputProcessor(tmp_path)
    action = asyncio.run(processor.process(InputEnvelope("  \n")))
    assert action.kind is InputKind.EMPTY


def test_prompt_attachments_are_expanded_before_queue(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")

    def fake_expand(text, root):
        return text.replace("@a.txt", "ATTACHED"), ["injected"], [ImageRef("image/png", "AA==")]

    processor = InputProcessor(tmp_path, expander=fake_expand)
    action = asyncio.run(processor.process(
        InputEnvelope("解释 @a.txt", source="ide")))
    assert action.kind is InputKind.PROMPT
    assert action.text == "解释 ATTACHED"
    assert action.notes == ("injected",)
    assert action.has_attachments is True
    assert action.source == "ide"


def test_commands_are_classified_without_attachment_expansion(tmp_path):
    def explode(*_args):
        raise AssertionError("命令不应展开 @ 文件")

    processor = InputProcessor(tmp_path, expander=explode)

    cases = [
        ("/exit", InputKind.CONTROL, "/exit"),
        ("/stop", InputKind.CONTROL, "/stop"),
        ("/thinking", InputKind.UI_COMMAND, "/thinking"),
        ("/tool-output 1", InputKind.UI_COMMAND, "/tool-output"),
        ("/resume 2", InputKind.COMMAND, "/resume"),
        ("/skill explain @a.py", InputKind.COMMAND, "/skill"),
    ]
    for text, kind, command in cases:
        action = asyncio.run(processor.process(InputEnvelope(text)))
        assert action.kind is kind
        assert action.command == command


def test_command_arguments_are_preserved(tmp_path):
    processor = InputProcessor(tmp_path)
    action = asyncio.run(processor.process(
        InputEnvelope("/resume repo with spaces.jsonl")))
    assert action.arguments == "repo with spaces.jsonl"

    tool_output = asyncio.run(processor.process(InputEnvelope("/tool-output 1")))
    assert tool_output.kind is InputKind.UI_COMMAND
    assert tool_output.arguments == "1"


def test_hook_context_is_added_only_to_prompts(tmp_path):
    processor = InputProcessor(tmp_path)
    prompt = asyncio.run(processor.process(InputEnvelope("解释代码")))
    updated = asyncio.run(processor.with_hook_context(prompt, ("规则 A",)))
    assert "<hook_context>\n规则 A\n</hook_context>" in updated.text
    assert updated.hook_context == ("规则 A",)

    command = asyncio.run(processor.process(InputEnvelope("/history")))
    unchanged = asyncio.run(processor.with_hook_context(command, ("规则 A",)))
    assert unchanged is command


def test_real_text_and_image_attachments_are_expanded_by_pipeline(tmp_path):
    (tmp_path / "notes.txt").write_text("正文", encoding="utf-8")
    (tmp_path / "screen.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    processor = InputProcessor(tmp_path)

    action = asyncio.run(processor.process(
        InputEnvelope("总结 @notes.txt 和 @screen.png", source="api")))

    assert action.kind is InputKind.PROMPT
    assert "<attached_file" in action.text
    assert "正文" in action.text
    assert "[图片 screen.png 已附件]" in action.text
    assert action.source == "api"
    assert action.images[0].media_type == "image/png"
    assert base64.b64decode(action.images[0].data_b64).startswith(b"\x89PNG")


def test_hook_provider_failure_preserves_prompt(tmp_path):
    async def broken_hook(_prompt):
        raise RuntimeError("hook crashed")

    processor = InputProcessor(tmp_path, hook_context_provider=broken_hook)
    action = asyncio.run(processor.process(InputEnvelope("继续实现")))

    assert action.kind is InputKind.PROMPT
    assert action.text == "继续实现"
    assert action.hook_context == ()


def test_hook_provider_does_not_run_for_commands(tmp_path):
    calls = []

    async def hook(prompt):
        calls.append(prompt)
        return ("context",)

    processor = InputProcessor(tmp_path, hook_context_provider=hook)
    action = asyncio.run(processor.process(InputEnvelope("/history")))

    assert action.kind is InputKind.COMMAND
    assert calls == []


def test_registry_alias_is_canonicalized_before_dispatch(tmp_path):
    registry = CommandRegistry([
        CommandSpec("help", "帮助", aliases=("h",)),
    ])
    processor = InputProcessor(tmp_path, command_registry=registry)

    action = asyncio.run(processor.process(InputEnvelope("/h topic")))

    assert action.kind is InputKind.COMMAND
    assert action.command == "/help"
    assert action.text == "/help topic"
    assert action.arguments == "topic"
    assert action.raw_text == "/h topic"
