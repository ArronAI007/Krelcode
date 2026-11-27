"""Slash-command integration for knowledge and skill releases."""

from __future__ import annotations

import asyncio
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.knowledge import LocalKnowledgeBase
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.controller import SessionController
from krelcode.skills import SkillManager
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def make_loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        llm=MockProvider(scripted=["unused"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_knowledge_command_lists_and_searches(tmp_path: Path) -> None:
    directory = tmp_path / ".krelcode" / "knowledge"
    directory.mkdir(parents=True)
    (directory / "runbook.md").write_text(
        "# Release runbook\n\nRollback requires a snapshot.",
        encoding="utf-8",
    )
    loop = make_loop(tmp_path)
    loop.services.context.knowledge = LocalKnowledgeBase(
        tmp_path, home=tmp_path / "home")
    controller = SessionController(loop, workspace_root=tmp_path)

    listing = asyncio.run(controller.execute_command("/knowledge list"))
    search = asyncio.run(
        controller.execute_command("/knowledge search rollback"))

    assert any("documents 1" in line for line in listing.output)
    assert any("Release runbook" in line for line in search.output)


def test_skills_release_command_records_version(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review\nversion: 1.0.0\ndescription: Review changes\n---\n"
        "Use checklist.\n",
        encoding="utf-8",
    )
    loop = make_loop(tmp_path)
    skills = SkillManager(
        tmp_path, extra_paths=[root], home=tmp_path / "home")
    controller = SessionController(
        loop,
        skills=skills,
        workspace_root=tmp_path,
        command_registry=None,
    )

    released = asyncio.run(
        controller.execute_command("/skills release review"))
    history = asyncio.run(controller.execute_command("/skills releases review"))

    assert any("review@1.0.0" in line for line in released.output)
    assert any("review@1.0.0" in line for line in history.output)
