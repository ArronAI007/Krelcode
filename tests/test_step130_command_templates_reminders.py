"""Step 130: standard command templates and bounded turn reminders."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from krelcode.agent.context_assembler import ContextAssembler
from krelcode.agent.fragments import (
    LAYER_L12_REMINDER,
    STABILITY_TURN,
    TRUST_MANAGED,
    base_fragment,
    PromptFragmentRegistry,
)
from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompt_assembler import PromptAssembler, PromptAssemblyRequest
from krelcode.agent.context_source import ContextRequest
from krelcode.agent.reminders import (
    COMMAND_TOOL_SCOPE_REMINDER_ID,
    ReminderCatalog,
    ReminderContext,
    ReminderSpec,
    TRIGGER_ALWAYS,
    TRIGGER_TASK_TYPE,
)
from krelcode.agent.turn.runtime_state import AgentRuntimeState
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands import (
    CommandArgumentMode,
    CommandRequest,
    CommandSource,
    CommandTemplate,
    builtin_command_registry,
    sync_custom_commands,
)
from krelcode.session.commands.custom import CustomCommandManager
from krelcode.session.commands.handlers.runtime_ops import (
    handle_commands,
    handle_context,
    handle_custom_command,
)
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


class NoopTool:
    name = "read_file"
    description = "read a file"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    async def execute(self, args, ctx):
        return ToolResult(content="ok")


def _custom_registry(tmp_path: Path):
    command_dir = tmp_path / ".krelcode" / "commands"
    command_dir.mkdir(parents=True)
    (command_dir / "review.md").write_text(
        "---\n"
        "name: review\n"
        "description: project review\n"
        "allowed-tools: [read_file]\n"
        "reminder-ids: [test:declared]\n"
        "---\n"
        "Review $ARGUMENTS only.\n",
        encoding="utf-8",
    )
    manager = CustomCommandManager(tmp_path, home=tmp_path / "home")
    registry = builtin_command_registry()
    sync_custom_commands(registry, manager)
    return manager, registry


def test_command_template_binds_provenance_trust_arguments_tools_and_reminders(
    tmp_path,
):
    manager, registry = _custom_registry(tmp_path)
    spec = registry.resolve("/review")
    assert spec is not None
    template = CommandTemplate.from_spec(spec)

    assert template.source is CommandSource.CUSTOM
    assert template.trust == "project"
    assert template.argument_mode is CommandArgumentMode.RAW
    assert template.allowed_tools == ("read_file",)
    assert template.reminder_ids == (
        "command:scoped-instructions",
        COMMAND_TOOL_SCOPE_REMINDER_ID,
        "test:declared",
    )
    assert len(template.template_hash) == 64
    assert "Review" not in json.dumps(template.as_record())

    context = SimpleNamespace(
        request=CommandRequest("review", "src/app.py", "/review src/app.py"),
        loop=SimpleNamespace(
            services=SimpleNamespace(
                extensions=SimpleNamespace(custom_commands=manager),
            ),
        ),
        command_registry=registry,
    )
    result = asyncio.run(handle_custom_command(context))
    assert result.run_prompt is not None
    assert 'trust="project" instruction="false"' in result.run_prompt
    assert "Review src/app.py only." in result.run_prompt
    assert result.tool_allowlist == ("read_file",)
    assert result.command_source is CommandSource.CUSTOM
    assert result.command_trust == "project"
    assert "test:declared" in result.reminder_ids
    assert result.template_hash == template.template_hash


def test_command_template_rejects_oversized_arguments():
    template = CommandTemplate.for_source("custom", scope="user").bind(
        argument_mode=CommandArgumentMode.RAW)
    with pytest.raises(ValueError, match="参数过长"):
        template.render(name="x", body="$ARGUMENTS", arguments="x" * 12_001)


def test_reminder_catalog_selects_manual_and_triggered_with_stable_order():
    catalog = ReminderCatalog((
        ReminderSpec(
            id="test:manual",
            source="test",
            content="MANUAL",
            priority=80,
            trigger="manual",
        ),
        ReminderSpec(
            id="test:auto",
            source="test",
            content="AUTO",
            priority=70,
            trigger=TRIGGER_TASK_TYPE,
            trigger_values=("bug_fix",),
        ),
        ReminderSpec(
            id="test:ignored",
            source="test",
            content="IGNORED",
            priority=100,
            trigger=TRIGGER_TASK_TYPE,
            trigger_values=("feature_implementation",),
        ),
    ))
    selection = catalog.select(ReminderContext(
        requested_reminder_ids=("test:manual", "test:missing"),
        task_type="bug_fix",
    ))

    assert selection.ids == ("test:manual", "test:auto")
    assert selection.selected[0].requested is True
    assert selection.selected[1].reason == "task-type=bug_fix"
    assert [item.id for item in selection.excluded] == ["test:missing"]
    assert selection.excluded[0].reason == "unknown-requested-id"
    assert "MANUAL" not in json.dumps(selection.as_record())


def test_reminder_catalog_deduplicates_content_and_enforces_budget():
    catalog = ReminderCatalog((
        ReminderSpec(
            id="test:a", source="test", content="same reminder",
            priority=90, trigger=TRIGGER_ALWAYS),
        ReminderSpec(
            id="test:b", source="test", content="same reminder",
            priority=80, trigger=TRIGGER_ALWAYS),
        ReminderSpec(
            id="test:c", source="test", content="another very long reminder",
            priority=70, trigger=TRIGGER_ALWAYS),
    ))
    all_candidates = catalog.select(ReminderContext(), budget_tokens=500)
    first_tokens = all_candidates.selected[0].tokens
    selection = catalog.select(
        ReminderContext(),
        budget_tokens=first_tokens,
    )

    assert all_candidates.ids == ("test:a", "test:c")
    assert all_candidates.duplicate_count == 1
    assert any(item.reason == "duplicate-content"
               for item in all_candidates.excluded)
    assert selection.ids == ("test:a",)
    assert any(item.id == "test:c" and item.reason == "turn-budget"
               for item in selection.excluded)


def test_prompt_assembler_uses_catalog_ids_in_l12_not_cache(tmp_path):
    registry = PromptFragmentRegistry([base_fragment()])
    assembler = PromptAssembler(
        ContextAssembler([]), registry_provider=lambda: registry)
    catalog = ReminderCatalog((ReminderSpec(
        id="test:stable-id",
        source="unit",
        content="Verify before completion.",
        priority=61,
        trigger="manual",
    ),))
    selected = catalog.select(ReminderContext(
        requested_reminder_ids=("test:stable-id",)))
    request = ContextRequest(
        turn_id="turn-reminder",
        step=1,
        history=(Message.user("do work"),),
        workspace_root=tmp_path,
    )
    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=request,
        prompt_snapshot=registry.snapshot(),
        per_turn_reminders=selected.selected,
    )))

    fragment = next(
        item for item in plan.fragments
        if item.id == "reminder:test:stable-id")
    assert fragment.layer == LAYER_L12_REMINDER
    assert fragment.stability == STABILITY_TURN
    assert fragment.cacheable is False
    assert fragment.trust == TRUST_MANAGED
    assert fragment.metadata["reminder_source"] == "unit"
    assert fragment.metadata["selection_reason"] == "requested"
    assert fragment.id not in plan.cache.stable_fragments


class CountingReminderCatalog(ReminderCatalog):
    def __init__(self, specs):
        super().__init__(specs)
        self.select_calls = 0

    def select(self, context, *, budget_tokens=500):
        self.select_calls += 1
        return super().select(context, budget_tokens=budget_tokens)


def test_agent_loop_selects_once_and_reuses_reminder_across_tool_steps(tmp_path):
    catalog = CountingReminderCatalog((ReminderSpec(
        id="test:turn-reminder",
        source="unit",
        content="Use tool evidence for completion.",
        priority=90,
        trigger="manual",
    ),))
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[
            ToolCall("call-1", "read_file", {"path": "x.txt"}),
        ]),
        "done",
    ])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([NoopTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path,
            sandbox_enabled=False,
        ),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM",
        reminder_catalog=catalog,
    )

    assert asyncio.run(loop.run(
        "修复 x.txt 的问题",
        reminder_ids=["test:turn-reminder"],
    )) == "done"

    assert catalog.select_calls == 1
    assert len(provider.calls) == 2
    for call in provider.calls:
        assert "Use tool evidence for completion." in call[0].content
    assert loop.last_reminder_selection is not None
    assert "test:turn-reminder" in loop.last_reminder_selection.ids
    fragment = next(
        item for item in loop.last_prompt_plan.fragments
        if item.id == "reminder:test:turn-reminder")
    assert fragment.id not in loop.last_prompt_plan.cache.stable_fragments


def test_reminder_and_command_diagnostics_are_content_light(tmp_path):
    manager, command_registry = _custom_registry(tmp_path)
    catalog = ReminderCatalog((ReminderSpec(
        id="test:diagnostic",
        source="unit",
        content="TOP SECRET REMINDER BODY",
        priority=100,
        trigger="manual",
    ),))
    selection = catalog.select(ReminderContext(
        requested_reminder_ids=("test:diagnostic",)))
    state = AgentRuntimeState()
    state.turn.last_reminder_selection = selection
    loop = SimpleNamespace(
        runtime_state=state,
        services=SimpleNamespace(
            context=SimpleNamespace(
                reminder_catalog=catalog,
                manager=None,
                assembler=None,
                working_context=None,
            ),
            prompt=SimpleNamespace(registry=None),
        ),
        command_registry=command_registry,
    )
    reminder_output = "\n".join(asyncio.run(handle_context(SimpleNamespace(
        request=CommandRequest("context", "reminders", "/context reminders"),
        loop=loop,
    ))).output)
    assert "test:diagnostic" in reminder_output
    assert "content-light=true" in reminder_output
    assert "TOP SECRET REMINDER BODY" not in reminder_output

    command_output = "\n".join(asyncio.run(handle_commands(SimpleNamespace(
        request=CommandRequest("commands", "show review", "/commands show review"),
        loop=loop,
        command_registry=loop.command_registry,
    ))).output)
    assert "/review" in command_output
    assert "trust=project" in command_output
    assert "content-light=true" in command_output
    assert "Review $ARGUMENTS only." not in command_output
