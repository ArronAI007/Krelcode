"""富 ToolResult：text/image/resource/structured、事件、持久化与 wire 降级。"""

import asyncio
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import EventBus, ToolCallCompleted
from krelcode.core.messages import (
    ImageBlock,
    Message,
    ResourceBlock,
    StructuredBlock,
    TextBlock,
    ToolCall,
)
from krelcode.core.redact import MASK, SecretRedactor
from krelcode.llm.providers.anthropic import to_anthropic_request_messages
from krelcode.llm.providers.gemini import to_gemini_contents
from krelcode.llm.providers.mock import MockProvider
from krelcode.llm.providers.openai import to_openai_messages
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.codec import history_from_dicts, history_to_dicts
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


BLOCKS = (
    TextBlock("secret-value-123"),
    ImageBlock("image/png", "aGVsbG8=", alt="chart"),
    ResourceBlock(
        uri="file:///tmp/report.md",
        name="report",
        mime_type="text/markdown",
        text="details",
    ),
    StructuredBlock({"status": "ok", "token": "secret-value-123"}),
)


class RichTool:
    name = "rich"
    description = "rich result"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    async def execute(self, args, ctx):
        return ToolResult.from_blocks(
            BLOCKS, data={"ui": {"kind": "card"}})


def run_rich_tool(tmp_path, redactor=None):
    call = ToolCall("r1", "rich", {})
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[call]),
        "done",
    ])
    bus = EventBus()
    completed = []
    bus.subscribe(lambda event: completed.append(event)
                  if isinstance(event, ToolCallCompleted) else None)
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([RichTool()]),
        tool_context=ToolContext(
            workspace_root=Path(tmp_path), sandbox_enabled=False),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
        redactor=redactor,
    )
    assert asyncio.run(loop.run("rich")) == "done"
    return loop, completed[0]


def test_rich_blocks_flow_to_tool_history_and_completion_event(tmp_path):
    loop, event = run_rich_tool(tmp_path)
    tool_message = loop.history[3]

    assert [type(block) for block in tool_message.tool_blocks] == [
        TextBlock, ImageBlock, ResourceBlock, StructuredBlock]
    assert event.blocks == tuple(tool_message.tool_blocks)
    assert event.data == {"ui": {"kind": "card"}}
    restored = history_from_dicts(history_to_dicts(loop.history))
    assert restored[3].tool_blocks == tool_message.tool_blocks


def test_rich_blocks_and_text_are_redacted_before_history(tmp_path):
    redactor = SecretRedactor(["secret-value-123"])
    loop, event = run_rich_tool(tmp_path, redactor=redactor)
    tool_message = loop.history[3]

    assert "secret-value-123" not in tool_message.content
    assert MASK in tool_message.content
    assert tool_message.tool_blocks[0] == TextBlock(MASK)
    assert tool_message.tool_blocks[3].data["token"] == MASK
    assert event.blocks[0] == TextBlock(MASK)
    assert "secret-value-123" not in str(history_to_dicts(loop.history))


def test_provider_wire_formats_for_rich_tool_result():
    assistant = Message.assistant(
        tool_calls=[ToolCall("r1", "rich", {})])
    tool_message = Message.tool_result(
        "r1", "fallback", blocks=list(BLOCKS))

    openai_wire = to_openai_messages([assistant, tool_message])
    openai_parts = openai_wire[1]["content"]
    assert openai_parts[0] == {"type": "text", "text": "secret-value-123"}
    assert openai_parts[1]["text"] == "chart"
    assert "details" in openai_parts[2]["text"]
    assert '"status": "ok"' in openai_parts[3]["text"]

    _, anthropic_wire = to_anthropic_request_messages([assistant, tool_message])
    anthropic_content = anthropic_wire[1]["content"][0]["content"]
    assert anthropic_content[1]["type"] == "image"
    assert anthropic_content[1]["source"]["media_type"] == "image/png"

    _, gemini_contents = to_gemini_contents([assistant, tool_message])
    response = gemini_contents[1]["parts"][0]["functionResponse"]["response"]
    assert response["content"] == "fallback"
    assert response["blocks"][1]["type"] == "image"
    assert response["blocks"][3]["data"]["status"] == "ok"
