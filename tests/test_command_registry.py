"""统一 slash command registry：元数据、解析、过滤与来源替换。"""

import pytest

from krelcode.session.commands import (
    CommandKind,
    CommandRegistry,
    CommandSource,
    CommandSpec,
    builtin_command_registry,
    render_help,
)


def test_builtin_catalog_has_expected_commands_and_run_subset():
    registry = builtin_command_registry()
    assert len(registry.specs) == 39
    assert [spec.name for spec in registry.list(running=True)] == [
        "thinking", "plan", "tool-output", "queue", "agents", "jobs", "permissions",
        "stop", "exit",
    ]
    assert registry.resolve("/thinking").kind is CommandKind.UI_COMMAND
    assert registry.resolve("/stop").kind is CommandKind.CONTROL
    assert registry.resolve("/cost").kind is CommandKind.COMMAND
    assert registry.resolve("/memory").kind is CommandKind.COMMAND
    assert registry.resolve("/lsp").kind is CommandKind.COMMAND
    assert registry.resolve("/web").kind is CommandKind.COMMAND
    assert registry.resolve("/output-style").kind is CommandKind.COMMAND
    assert registry.resolve("/statusline").kind is CommandKind.COMMAND
    assert registry.resolve("/prompts").kind is CommandKind.COMMAND


def test_resolution_accepts_slash_alias_and_case_insensitive_name():
    registry = CommandRegistry([
        CommandSpec("help", "help", aliases=("h", "?")),
    ])
    assert registry.resolve("/help").name == "help"
    assert registry.resolve("h").name == "help"
    assert registry.resolve("/?").name == "help"
    assert registry.resolve("/HELP").name == "help"
    assert registry.resolve("/missing") is None


def test_filters_hidden_disabled_running_and_prefix():
    registry = CommandRegistry([
        CommandSpec("visible", "v"),
        CommandSpec("hidden", "h", is_hidden=True),
        CommandSpec("disabled", "d", is_enabled=False),
        CommandSpec("run", "r", available_during_run=True),
    ])

    assert [spec.name for spec in registry.list()] == ["visible", "run"]
    assert [spec.name for spec in registry.list(include_hidden=True)] == [
        "visible", "hidden", "run",
    ]
    assert [spec.name for spec in registry.list(include_disabled=True)] == [
        "visible", "disabled", "run",
    ]
    assert [spec.name for spec in registry.list(running=True)] == ["run"]
    assert [spec.name for spec in registry.list(query="/vi")] == ["visible"]


def test_sources_replace_atomically_and_builtin_wins_conflicts():
    registry = builtin_command_registry()
    registry.replace_source(CommandSource.SKILL, [
        CommandSpec("demo", "skill demo", source=CommandSource.SKILL),
        CommandSpec("help", "collision", source=CommandSource.SKILL),
    ])
    assert registry.resolve("/demo").source is CommandSource.SKILL
    assert registry.resolve("/help").source is CommandSource.BUILTIN
    assert registry.conflicts[-1].token == "help"
    assert registry.conflicts[-1].rejected.source is CommandSource.SKILL

    registry.replace_source(CommandSource.SKILL, [
        CommandSpec("next", "next skill", source=CommandSource.SKILL),
    ])
    assert registry.resolve("/demo") is None
    assert registry.resolve("/next").source is CommandSource.SKILL


def test_replace_source_rejects_mismatched_source_and_bad_names():
    registry = CommandRegistry()
    with pytest.raises(ValueError, match="不能注册"):
        registry.replace_source(CommandSource.MCP, [
            CommandSpec("bad", "bad", source=CommandSource.SKILL),
        ])
    with pytest.raises(ValueError, match="非法命令名"):
        CommandRegistry([CommandSpec("has space", "bad")])


def test_help_is_generated_from_live_registry():
    registry = builtin_command_registry()
    registry.replace_source(CommandSource.SKILL, [
        CommandSpec(
            "review-plan", "评审计划",
            source=CommandSource.SKILL,
            argument_hint="[路径]",
        ),
    ])
    text = render_help(registry)
    assert "/review-plan [路径]" in text
    assert "/help" in text
    assert "Ctrl-D" in text
