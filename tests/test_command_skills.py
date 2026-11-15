"""68C：Skill 动态 slash command 注册与执行。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from krelcode.agent.loop import AgentLoop
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands import (
    CommandKind,
    CommandSource,
    builtin_command_registry,
    skill_command_specs,
)
from krelcode.session.controller import SessionController
from krelcode.skills import SkillManager
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.repl import match_slash_commands, run_repl


def write_skill(root: Path, name: str, body: str, *, meta: str = "") -> None:
    directory = root / name
    directory.mkdir(parents=True)
    frontmatter = meta or (
        f"name: {name}\ndescription: {name} description\n"
        "argument-hint: <path>\n"
    )
    (directory / "SKILL.md").write_text(
        f"---\n{frontmatter}---\n{body}", encoding="utf-8")


def make_loop(tmp_path):
    return AgentLoop(
        llm=MockProvider(scripted=["done"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_skill_command_specs_only_include_user_invocable_skills():
    skills = SimpleNamespace(
        enabled=True,
        list=lambda **kwargs: [
            SimpleNamespace(
                name="explain", description="Explain code",
                argument_hint="<path>\noptional",
            ),
        ],
    )
    specs = skill_command_specs(skills)
    assert len(specs) == 1
    assert specs[0].name == "explain"
    assert specs[0].kind is CommandKind.PROMPT
    assert specs[0].source is CommandSource.SKILL
    assert specs[0].argument_hint == "<path> optional"


def test_controller_registers_and_executes_skill_command(tmp_path):
    write_skill(
        tmp_path / ".agents" / "skills", "explain",
        "EXPLAIN BODY: $ARGUMENTS\n")
    manager = SkillManager(tmp_path, home=tmp_path / "home")
    registry = builtin_command_registry()
    controller = SessionController(
        make_loop(tmp_path), skills=manager, command_registry=registry)

    spec = registry.resolve("/explain")
    assert spec is not None
    assert spec.kind is CommandKind.PROMPT
    assert spec.source is CommandSource.SKILL
    assert ("/explain", "explain description") in match_slash_commands(
        "/exp", registry=registry)

    result = asyncio.run(controller.execute_command("/explain src/app.py"))
    assert result.handled is True
    assert "已加载并执行: explain" in result.output[0]
    assert "EXPLAIN BODY: src/app.py" in result.run_prompt


def test_skill_reload_refreshes_command_catalog(tmp_path):
    root = tmp_path / ".agents" / "skills"
    write_skill(root, "first", "FIRST\n")
    manager = SkillManager(tmp_path, home=tmp_path / "home")
    registry = builtin_command_registry()
    controller = SessionController(
        make_loop(tmp_path), skills=manager, command_registry=registry)
    assert registry.resolve("/first") is not None

    write_skill(root, "second", "SECOND\n")
    asyncio.run(controller.execute_command("/skills reload"))

    assert registry.resolve("/first") is not None
    assert registry.resolve("/second").source is CommandSource.SKILL


def test_builtin_name_wins_over_conflicting_skill(tmp_path):
    write_skill(
        tmp_path / ".agents" / "skills", "help", "SHOULD NOT WIN\n")
    manager = SkillManager(tmp_path, home=tmp_path / "home")
    registry = builtin_command_registry()
    SessionController(
        make_loop(tmp_path), skills=manager, command_registry=registry)

    spec = registry.resolve("/help")
    assert spec.kind is CommandKind.COMMAND
    assert spec.source is CommandSource.BUILTIN
    assert any(conflict.token == "help" for conflict in registry.conflicts)


def test_repl_dynamic_skill_command_enters_agent_history(tmp_path):
    write_skill(
        tmp_path / ".agents" / "skills", "explain",
        "DYNAMIC EXPLAIN: $ARGUMENTS\n")
    manager = SkillManager(tmp_path, home=tmp_path / "home")
    loop = make_loop(tmp_path)
    lines = ["/explain src/main.py", "/exit"]

    async def scripted_input(_prompt):
        if not lines:
            raise EOFError
        await asyncio.sleep(0.01)
        return lines.pop(0)

    rc = asyncio.run(run_repl(
        loop, input_fn=scripted_input, skills=manager))

    assert rc == 0
    assert "DYNAMIC EXPLAIN: src/main.py" in loop.history[1].content
