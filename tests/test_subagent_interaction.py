"""Step 81：子代理 Agent Feed、控制、Definition 与 sidechain。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from krelcode.agent.query import QueryEvent
from krelcode.core.events import (
    EventBus,
    SubagentFeedUpdated,
    SubagentFinished,
    SubagentStateChanged,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.core.redact import SecretRedactor
from krelcode.llm.base import ProviderCapabilities, StreamFinished, TextDelta
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands.handlers.runtime_ops import handle_agents
from krelcode.session.recorder import SessionRecorder
from krelcode.session.stream_json import StreamJsonEncoder, StreamJsonMetadata
from krelcode.subagent.definition import AgentDefinitionManager
from krelcode.subagent.feed import AgentFeed
from krelcode.subagent.manager import SubagentManager
from krelcode.subagent.sidechain import SidechainStore, read_sidechain
from krelcode.subagent.tools import (
    GetAgentFeedTool,
    ListAgentDefinitionsTool,
    SpawnAgentTool,
)
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.builtin.tool_search import ToolSearchTool


class CaptureProvider:
    name = "capture"
    model = "capture"
    capabilities = ProviderCapabilities()

    def __init__(self, text: str = "done"):
        self.text = text
        self.last_system = ""
        self.last_tools = []

    async def stream(self, messages, tools=None):
        self.last_system = messages[0].content or ""
        self.last_tools = list(tools or [])
        yield TextDelta(self.text)
        yield StreamFinished(
            message=Message.assistant(self.text), finish_reason="stop", usage={})


class GateTool:
    name = "slow"
    description = "wait until released"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, args, ctx) -> ToolResult:
        self.started.set()
        await self.release.wait()
        return ToolResult(content="released")


class ToolThenFinalProvider:
    """第一次调用工具，之后返回最终文本。"""

    name = "tool-then-final"
    model = "tool-then-final"
    capabilities = ProviderCapabilities()

    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            message = Message.assistant(
                tool_calls=[ToolCall("slow-1", "slow", {})])
        else:
            message = Message.assistant(f"final-{self.calls}")
        yield StreamFinished(message=message, finish_reason="stop", usage={})


def make_manager(tmp_path: Path, provider, tools) -> SubagentManager:
    return SubagentManager(
        llm=provider,
        tool_factory=lambda: list(tools),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=EventBus(),
        max_steps=10,
    )


def test_agent_definition_global_project_override_and_bad_definition(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    global_dir = home / ".krelcode" / "agents"
    project_dir = workspace / ".krelcode" / "agents"
    global_dir.mkdir(parents=True)
    project_dir.mkdir(parents=True)
    (global_dir / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: global reviewer\n"
        "allowed-tools: [slow]\n---\nGLOBAL PROMPT\n", encoding="utf-8")
    (project_dir / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: project reviewer\n"
        "allowed-tools: slow\nisolated: false\n---\nPROJECT PROMPT\n",
        encoding="utf-8")
    (project_dir / "broken.md").write_text("no frontmatter", encoding="utf-8")

    manager = AgentDefinitionManager(workspace, home=home)
    definition = manager.get("reviewer")
    assert definition.source == "project"
    assert definition.description == "project reviewer"
    assert definition.allowed_tools == ("slow",)
    assert "PROJECT PROMPT" in definition.system_prompt
    assert any(issue.path.name == "broken.md" for issue in manager.issues)


def test_spawn_definition_applies_prompt_and_tool_allowlist(tmp_path):
    home = tmp_path / "home"
    agents = home / ".krelcode" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: review code\n"
        "allowed-tools: [slow]\n---\nReview carefully.\n", encoding="utf-8")
    provider = CaptureProvider()
    definitions = AgentDefinitionManager(tmp_path, home=home)
    class OtherToolImpl:
        name = "other"
        description = "other"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, args, ctx):
            return ToolResult(content="other")

    manager = SubagentManager(
        llm=provider,
        tool_factory=lambda: [ToolSearchTool(), GateTool(), OtherToolImpl()],
        tool_context=ToolContext(workspace_root=tmp_path),
        bus=EventBus(),
        definitions=definitions,
    )
    handle = asyncio.run(_spawn_and_wait(manager, definition="reviewer"))
    assert handle.definition == "reviewer"
    assert "Review carefully." in provider.last_system
    assert [item["name"] for item in provider.last_tools] == [
        "slow", "tool_search"]
    assert handle.sidechain_path is not None and handle.sidechain_path.is_file()

    (agents / "bad.md").write_text(
        "---\nname: bad\ndescription: bad tools\n"
        "allowed-tools: [not_registered]\n---\nPrompt.\n", encoding="utf-8")
    definitions.reload()
    with pytest.raises(ValueError, match="未知工具"):
        asyncio.run(manager.spawn("bad", definition="bad"))
    asyncio.run(manager.shutdown())


async def _spawn_and_wait(manager, **kwargs):
    await manager.spawn("review this", **kwargs)
    return await manager.wait("a1", timeout=5)


def test_pause_resume_preserves_task_and_queues_messages(tmp_path):
    async def go():
        provider = ToolThenFinalProvider()
        tool = GateTool()
        manager = make_manager(tmp_path, provider, [tool])
        handle = await manager.spawn("first")
        await asyncio.wait_for(tool.started.wait(), timeout=2)
        await manager.pause("a1")
        assert handle.status == "paused"
        tool.release.set()
        await asyncio.sleep(0.1)
        assert provider.calls == 1
        assert not handle.task_obj.done()

        queued, action = await manager.send_input("a1", "follow up")
        assert action == "queued" and queued.queue == ["follow up"]
        await manager.resume("a1")
        result = await manager.wait("a1", timeout=5)
        assert result.status == "completed"
        assert result.turns == 2
        assert provider.calls >= 3
        assert "follow up" in [m.content for m in result.loop.history if m.role == "user"]
        await manager.shutdown()
    asyncio.run(go())


def test_kill_cancels_task_and_emits_one_terminal_event(tmp_path):
    async def go():
        provider = ToolThenFinalProvider()
        tool = GateTool()
        events = []

        async def capture(event):
            events.append(event)

        manager = make_manager(tmp_path, provider, [tool])
        manager.bus.subscribe(capture)
        handle = await manager.spawn("long")
        await asyncio.wait_for(tool.started.wait(), timeout=2)
        killed = await manager.kill("a1")
        assert killed.status == "killed"
        assert handle.task_obj.done()
        assert [e.status for e in events if isinstance(e, SubagentFinished)] == ["killed"]
        assert any(isinstance(e, SubagentStateChanged) and e.status == "killed"
                   for e in events)
        await manager.shutdown()
    asyncio.run(go())


def test_feed_cursor_bounded_redaction_and_sidechain_persist(tmp_path):
    async def go():
        secret = "super-secret-api-key"
        provider = CaptureProvider(text=f"result {secret}")
        manager = make_manager(tmp_path, provider, [])
        manager.redactor = SecretRedactor([secret])
        manager.sidechains.redactor = manager.redactor
        await manager.spawn("secret task")
        handle = await manager.wait("a1", timeout=5)
        page = manager.feed("a1", after_sequence=0, limit=500)
        assert page.next_sequence == page.latest_sequence > 0
        assert all(secret not in entry.text for entry in page.entries)
        assert all(secret not in json.dumps(entry.data, ensure_ascii=False)
                   for entry in page.entries)
        assert not manager.feed("a1", after_sequence=page.latest_sequence).entries

        records = read_sidechain(handle.sidechain_path)
        assert any(record.get("type") == "sidechain_start" for record in records)
        assert any(record.get("type") == "feed" for record in records)
        assert any(record.get("type") == "history" for record in records)
        assert any(record.get("type") == "state"
                   and record.get("status") == "completed" for record in records)
        assert secret not in handle.sidechain_path.read_text(encoding="utf-8")
        assert SidechainStore(tmp_path).list()[0].agent_id == "a1"
        await manager.shutdown()
    asyncio.run(go())

    feed = AgentFeed("a1", max_entries=2)
    for index in range(3):
        feed.add("text", str(index), ts=float(index))
    page = feed.page()
    assert page.latest_sequence == 3 and page.truncated
    assert [entry.sequence for entry in page.entries] == [2, 3]
    feed.add("text", "4", ts=4.0)
    limited = feed.page(after_sequence=2, limit=1)
    assert [entry.sequence for entry in limited.entries] == [3]
    assert limited.next_sequence == 3

    split = AgentFeed("a2", redactor=SecretRedactor(["split-secret"]))
    assert split.add("assistant_delta", "split-", ts=1.0) is None
    completed = split.add("assistant_delta", "secret", ts=2.0)
    assert completed is not None and completed.text == "***"
    assert "split-secret" not in "\n".join(
        entry.text for entry in split.page().entries)


def test_feed_and_definition_tools_are_parent_callable(tmp_path):
    async def go():
        home = tmp_path / "home"
        path = home / ".krelcode" / "agents"
        path.mkdir(parents=True)
        (path / "worker.md").write_text(
            "---\nname: worker\ndescription: worker definition\n---\nWork.\n",
            encoding="utf-8")
        provider = CaptureProvider()
        manager = SubagentManager(
            llm=provider,
            tool_factory=list,
            tool_context=ToolContext(workspace_root=tmp_path),
            bus=EventBus(),
            definitions=AgentDefinitionManager(tmp_path, home=home),
        )
        spawn = await SpawnAgentTool(manager).execute(
            {"task": "work", "definition": "worker"}, manager.tool_context)
        assert not spawn.is_error and spawn.data["definition"] == "worker"
        await manager.wait("a1", timeout=5)
        feed = await GetAgentFeedTool(manager).execute(
            {"agent_id": "a1", "after_sequence": 0}, manager.tool_context)
        assert not feed.is_error and "assistant_delta" in feed.content
        definitions = await ListAgentDefinitionsTool(manager).execute(
            {}, manager.tool_context)
        assert "worker" in definitions.content
        await manager.shutdown()
    asyncio.run(go())


def test_agents_command_controls_feed_and_sidechain(tmp_path):
    manager = make_manager(tmp_path, CaptureProvider(), [])

    async def go():
        await manager.spawn("hello")
        await manager.wait("a1", timeout=5)
        ctx = SimpleNamespace(
            request=SimpleNamespace(arguments="feed a1 0"),
            subagents=manager,
        )
        result = await handle_agents(ctx)
        assert any("assistant_delta" in line for line in result.output)
        ctx.request.arguments = "sidechains"
        result = await handle_agents(ctx)
        assert any("a1" in line for line in result.output)
        ctx.request.arguments = "sidechain a1"
        result = await handle_agents(ctx)
        assert any("sidechain_start" in line for line in result.output)
        await manager.shutdown()
    asyncio.run(go())


def test_subagent_feed_and_state_events_encode_and_record(tmp_path):
    metadata = StreamJsonMetadata(
        session_id="s1", cwd=str(tmp_path), provider="mock", model="mock")
    encoder = StreamJsonEncoder(metadata)
    feed = SubagentFeedUpdated("a1", 3, "assistant_delta", "hello", {"x": 1})
    state = SubagentStateChanged("a1", "paused", "user")
    encoded_feed = encoder.encode(QueryEvent("turn-1", 0, feed))
    encoded_state = encoder.encode(QueryEvent("turn-1", 1, state))
    assert encoded_feed["type"] == "subagent"
    assert encoded_feed["subtype"] == "feed"
    assert encoded_feed["agent_sequence"] == 3
    assert encoded_state["subtype"] == "state"

    recorder = SessionRecorder(tmp_path / "session.jsonl", lambda: [])
    asyncio.run(recorder.on_event(feed))
    asyncio.run(recorder.on_event(state))
    records = [json.loads(line) for line in recorder.path.read_text().splitlines()]
    assert [record["type"] for record in records] == [
        "subagent_feed", "subagent_state"]
