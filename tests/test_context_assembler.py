"""Step 85: ContextSource collection and deterministic context assembly."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from krelcode.agent.context import ContextSection, WorkspaceContext
from krelcode.agent.context_assembler import ContextAssembler
from krelcode.agent.context_source import (
    CATEGORY_MEMORY,
    CATEGORY_RUNTIME,
    CATEGORY_SYSTEM,
    CATEGORY_WORKSPACE,
    ContextLayer,
    ContextRequest,
    ContextTokenContribution,
    PromptSnapshotContextSource,
    ToolDirectoryContextSource,
    default_context_sources,
)
from krelcode.agent.fragments import (
    PromptFragmentRegistry,
    base_fragment,
    workspace_fragments,
)
from krelcode.agent.intent import TurnIntent
from krelcode.agent.loop import AgentLoop
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands.builtin import builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers.runtime_ops import handle_context
from krelcode.session.commands.types import CommandRequest
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry
from krelcode.agent.tool_pool import TurnToolPool
from krelcode.core.messages import Message


class EchoTool:
    name = "echo"
    description = "Echo a short message."
    parameters = {"type": "object", "properties": {}}
    read_only = True

    async def execute(self, args, ctx):
        return ToolResult(content="ok")


class StaticSource:
    name = "static"

    def __init__(self, layers):
        self.layers = tuple(layers)

    def collect(self, request):
        return self.layers


class AsyncSource:
    name = "async"

    async def collect(self, request):
        return (ContextLayer(
            id="async:late",
            source="async",
            category=CATEGORY_RUNTIME,
            content="LATE",
            priority=200,
        ), ContextLayer(
            id="async:early",
            source="async",
            category=CATEGORY_SYSTEM,
            content="EARLY",
            priority=10,
        ))


class FailingSource:
    name = "broken"

    def collect(self, request):
        raise RuntimeError("secret-looking source failure")


class TenantPolicySource:
    name = "tenant_policy"

    def collect(self, request):
        content = "<tenant_policy>local-only</tenant_policy>"
        return (ContextLayer(
            id="tenant:policy",
            source=self.name,
            category=CATEGORY_RUNTIME,
            content=content,
            priority=50,
            token_contributions=(ContextTokenContribution(
                "tenant:policy", CATEGORY_RUNTIME, 8),),
        ),)


def _request(tmp_path: Path, **kwargs) -> ContextRequest:
    values = {
        "turn_id": "turn-85",
        "step": 1,
        "history": (),
        "workspace_root": tmp_path,
    }
    values.update(kwargs)
    return ContextRequest(**values)


def _workspace_bundle(tmp_path: Path) -> WorkspaceContext:
    section = ContextSection(
        "rules", "RULES: keep changes focused",
        priority=10, source="AGENTS.md")
    return WorkspaceContext(
        root=tmp_path,
        sections=(section,),
        text=f"<workspace_context>\n{section.content}\n</workspace_context>",
        max_tokens=1_000,
        total_tokens=section.tokens,
    )


def test_assembler_orders_async_sources_and_isolates_failures(tmp_path):
    assembler = ContextAssembler([
        StaticSource([ContextLayer(
            id="static:dynamic",
            source="static",
            category=CATEGORY_RUNTIME,
            content="DYNAMIC",
            priority=100,
        )]),
        AsyncSource(),
        FailingSource(),
    ])

    assembly = asyncio.run(assembler.assemble(_request(tmp_path)))

    assert assembly.text == "EARLY\n\nDYNAMIC\n\nLATE"
    assert [layer.id for layer in assembly.layers] == [
        "async:early", "static:dynamic", "async:late"]
    assert assembly.issues[0].source == "broken"
    assert assembly.issues[0].error == "RuntimeError"
    assert "secret-looking" not in assembly.issues[0].error
    assert assembly.source_count == 2
    assert assembly.full_hash == hashlib.sha256(
        assembly.text.encode("utf-8")).hexdigest()


def test_prompt_cache_boundary_survives_dynamic_tool_context(tmp_path):
    registry = PromptFragmentRegistry([
        base_fragment(), *workspace_fragments(_workspace_bundle(tmp_path))])
    prompt_snapshot = registry.snapshot()
    tool_pool = TurnToolPool(
        ToolRegistry([EchoTool()]), intent=TurnIntent(), runtime=None)
    assembler = ContextAssembler([
        PromptSnapshotContextSource(),
        ToolDirectoryContextSource(),
    ])

    assembly = asyncio.run(assembler.assemble(_request(
        tmp_path,
        system_prompt=prompt_snapshot.text,
        system_provider_meta=prompt_snapshot.provider_meta(),
        prompt_snapshot=prompt_snapshot,
        tool_pool=tool_pool,
    )))

    assert assembly.text.startswith(prompt_snapshot.text)
    assert "<available_tools>" in assembly.text
    assert assembly.stable_prefix_chars == prompt_snapshot.cache.offset
    assert assembly.stable_prefix == prompt_snapshot.stable_prefix
    assert assembly.provider_meta == prompt_snapshot.provider_meta()
    assert assembly.source_count == 2
    assert assembly.fragment_count == len(prompt_snapshot.fragments)
    assert assembly.full_hash != prompt_snapshot.cache.full_hash


def test_agent_loop_uses_registered_source_and_exposes_source_diagnostics(tmp_path):
    provider = MockProvider(scripted=["done"])
    assembler = ContextAssembler([
        *default_context_sources(),
        TenantPolicySource(),
    ])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM",
        context_assembler=assembler,
    )

    assert asyncio.run(loop.run("hello", turn_id="turn-85")) == "done"

    first_messages = provider.calls[0]
    system_text = first_messages[0].content
    assert system_text.startswith("SYSTEM")
    assert "EARLY" not in system_text
    assert "tenant_policy>local-only" in system_text
    assert loop.last_context_assembly is not None
    assert loop.last_context_assembly.full_hash == hashlib.sha256(
        system_text.encode("utf-8")).hexdigest()
    assert loop.last_context_snapshot is not None
    assert loop.last_context_snapshot.source_count == 2
    assert "tenant:policy" in loop.last_context_snapshot.source_tokens

    ctx = CommandContext(
        request=CommandRequest(
            name="context", arguments="sources", raw_text="/context sources"),
        loop=loop,
        command_registry=builtin_command_registry(),
        workspace_root=tmp_path,
    )
    output = "\n".join(asyncio.run(handle_context(ctx)).output)
    assert "已注册 7 个 ContextSource" in output
    assert "tenant_policy" in output
    assert "tenant:policy" in output
    assert "最近装配:" in output


def test_assembler_uses_category_priority_and_source_order(tmp_path):
    layers = [
        ContextLayer(
            id="runtime:first",
            source="ordered",
            category=CATEGORY_RUNTIME,
            content="RUNTIME",
            priority=1,
        ),
        ContextLayer(
            id="memory:first",
            source="ordered",
            category=CATEGORY_MEMORY,
            content="MEMORY",
            priority=1,
        ),
        ContextLayer(
            id="workspace:late",
            source="ordered",
            category=CATEGORY_WORKSPACE,
            content="WORKSPACE-LATE",
            priority=99,
        ),
        ContextLayer(
            id="system:late",
            source="ordered",
            category=CATEGORY_SYSTEM,
            content="SYSTEM-LATE",
            priority=200,
        ),
        ContextLayer(
            id="workspace:first",
            source="ordered",
            category=CATEGORY_WORKSPACE,
            content="WORKSPACE-FIRST",
            priority=1,
        ),
    ]

    assembly = asyncio.run(ContextAssembler([
        StaticSource(layers),
    ]).assemble(_request(tmp_path)))

    assert [layer.id for layer in assembly.layers] == [
        "system:late",
        "workspace:first",
        "workspace:late",
        "memory:first",
        "runtime:first",
    ]


def test_invalid_source_outputs_are_isolated_without_losing_valid_layers(tmp_path):
    valid = ContextLayer(
        id="valid:policy",
        source="mixed",
        category=CATEGORY_RUNTIME,
        content="VALID",
        priority=10,
    )
    duplicate = ContextLayer(
        id="valid:policy",
        source="mixed",
        category=CATEGORY_RUNTIME,
        content="DUPLICATE",
        priority=20,
    )
    invalid = ContextLayer(
        id="invalid:category",
        source="mixed",
        category="unknown",
        content="INVALID",
        priority=30,
    )
    assembler = ContextAssembler([
        StaticSource([
            "not-a-layer",
            valid,
            duplicate,
            invalid,
            ContextLayer(
                id="invalid:metadata",
                source="mixed",
                category=CATEGORY_RUNTIME,
                content="INVALID",
                metadata={"fragment_count": "many"},
            ),
        ]),
        FailingSource(),
    ])

    assembly = asyncio.run(assembler.assemble(_request(tmp_path)))

    assert assembly.text == "VALID"
    assert assembly.source_count == 1
    assert [issue.error for issue in assembly.issues] == [
        "invalid-layer",
        "duplicate-id",
        "invalid-category",
        "invalid-metadata",
        "RuntimeError",
    ]


def test_messages_for_replaces_system_without_mutating_history(tmp_path):
    assembler = ContextAssembler([StaticSource([ContextLayer(
        id="runtime:one",
        source="dynamic",
        category=CATEGORY_RUNTIME,
        content="DYNAMIC",
    )])])
    previous_system = Message.system("OLD", provider_meta={"old": True})
    user = Message.user("hello")
    history = [previous_system, user]

    assembly = asyncio.run(assembler.assemble(_request(
        tmp_path,
        system_provider_meta={"custom": "kept"},
    )))
    messages = assembly.messages_for(history)

    assert history == [previous_system, user]
    assert messages[0].role == "system"
    assert messages[0].content == "DYNAMIC"
    assert messages[0].provider_meta == {"custom": "kept"}
    assert messages[1] is user
