"""命令 handler 与 dispatcher 边界测试。"""

import asyncio

from krelcode.agent.loop import AgentLoop
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands import (
    CommandKind,
    CommandRegistry,
    CommandSource,
    CommandSpec,
    builtin_command_registry,
)
from krelcode.session.commands.handlers import BUILTIN_HANDLERS
from krelcode.session.controller import SessionController
from krelcode.session.memory import MemoryStore
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def make_controller(tmp_path, registry=None):
    loop = AgentLoop(
        llm=MockProvider(scripted=[]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    return SessionController(loop, command_registry=registry)


def test_every_business_command_has_a_handler():
    business = {
        spec.name for spec in builtin_command_registry().list(include_disabled=True)
        if spec.kind is CommandKind.COMMAND
    }
    assert set(BUILTIN_HANDLERS) == business


def test_controller_dispatches_without_monolithic_command_branches(tmp_path):
    controller = make_controller(tmp_path)
    result = asyncio.run(controller.execute_command("/cost"))
    assert result.handled is True
    assert result.output == ("  （用量统计不可用）",)


def test_controller_uses_registry_alias_before_handler_lookup(tmp_path):
    registry = builtin_command_registry()
    help_spec = registry.resolve("/help")
    registry.replace_source(CommandSource.BUILTIN, [
        CommandSpec(
            help_spec.name,
            help_spec.description,
            aliases=("h",),
            help=help_spec.help,
        ),
        *[spec for spec in registry.specs if spec.name != "help"],
    ])
    controller = make_controller(tmp_path, registry)

    result = asyncio.run(controller.execute_command("/h"))

    assert result.handled is True
    assert "/review-plan" not in result.output[0]
    assert "/help" in result.output[0]


def test_controller_leaves_ui_and_control_commands_for_frontend(tmp_path):
    controller = make_controller(tmp_path)
    assert asyncio.run(controller.execute_command("/thinking")).handled is False
    assert asyncio.run(controller.execute_command("/stop")).handled is False


def test_unknown_command_goes_through_dispatcher_error_path(tmp_path):
    controller = make_controller(tmp_path)
    result = asyncio.run(controller.execute_command("/does-not-exist"))
    assert result.handled is True
    assert "未知命令: /does-not-exist" in result.output[0]


def test_memory_command_manages_scoped_records(tmp_path):
    controller = make_controller(tmp_path)
    controller.loop.memory_store = MemoryStore(
        tmp_path, user_path=tmp_path / "global" / "entries.jsonl")

    added = asyncio.run(controller.execute_command(
        "/memory add user 全局偏好使用中文"))
    assert added.handled
    assert "已写入用户记忆 user-" in added.output[0]

    listing = asyncio.run(controller.execute_command("/memory list"))
    assert any("user/note" in line and "全局偏好使用中文" in line
               for line in listing.output)

    scopes = asyncio.run(controller.execute_command("/memory scopes"))
    rendered = "\n".join(scopes.output)
    assert "用户 (user) · 1 条" in rendered
    assert "项目 (project) · 0 条" in rendered
    assert "会话 (session) · 0 条" in rendered

    entry_id = controller.loop.memory_store.list()[0].id
    removed = asyncio.run(controller.execute_command(
        f"/memory remove {entry_id}"))
    assert "已删除" in removed.output[0]
    assert controller.loop.memory_store.list() == ()
