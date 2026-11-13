"""Agent Skills：发现、渐进加载、资源、脚本、REPL 与安全边界。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.app import build_loop, build_parser
from krelcode.core.messages import ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy, SandboxPolicy
from krelcode.skills import SkillError, SkillManager, build_skill_tools
from krelcode.skills.parser import parse_skill_document
from krelcode.skills.tools import RunSkillScriptTool
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.repl import _handle_command, run_repl


def write_skill(root: Path, name: str, body: str, frontmatter: str = "") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    meta = frontmatter or f"name: {name}\ndescription: {name} description\n"
    (directory / "SKILL.md").write_text(
        f"---\n{meta}---\n{body}", encoding="utf-8")
    return directory


def test_frontmatter_parser_supports_agent_skill_yaml_subset():
    text = """---
name: demo-skill
description: "A quoted: description"
user-invocable: false
allowed-tools: [Read, Grep]
argument-hint: |
  <path>
  optional args
---
# Body
Use $ARGUMENTS and $SKILL_DIR.
"""
    metadata, body = parse_skill_document(text)
    assert metadata["name"] == "demo-skill"
    assert metadata["description"] == "A quoted: description"
    assert metadata["user-invocable"] is False
    assert metadata["allowed-tools"] == ["Read", "Grep"]
    assert metadata["argument-hint"].strip() == "<path>\noptional args"
    assert body.startswith("# Body")


def test_discovery_priority_and_malformed_skill(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    custom = tmp_path / "custom"
    write_skill(home / ".agents" / "skills", "review",
                "GLOBAL BODY\n", "name: review\ndescription: global\n")
    write_skill(workspace / ".agents" / "skills", "review",
                "PROJECT BODY\n", "name: review\ndescription: project\n")
    write_skill(custom, "review",
                "CUSTOM BODY\n", "name: review\ndescription: custom\n")
    malformed = workspace / ".agents" / "skills" / "broken"
    malformed.mkdir()
    (malformed / "SKILL.md").write_text("no frontmatter", encoding="utf-8")

    manager = SkillManager(
        workspace, home=home, extra_paths=[custom])
    skill = manager.get("review")
    assert skill is not None
    assert skill.description == "custom"
    assert skill.source.startswith("custom:")
    assert len(manager.issues) == 1


def test_prompt_index_is_progressive_and_load_substitutes_arguments(tmp_path):
    directory = write_skill(
        tmp_path / ".agents" / "skills", "fix-tests",
        "SECRET FULL BODY\nRun tests at $SKILL_DIR with $ARGUMENTS.\n",
        "name: fix-tests\ndescription: Fix failing tests\n"
        "argument-hint: <test-path>\nallowed-tools: [Read, Bash]\n")
    (directory / "reference.md").write_text("reference", encoding="utf-8")
    manager = SkillManager(tmp_path, home=tmp_path / "home")

    prompt = manager.apply_to_prompt("BASE")
    assert "fix-tests" in prompt
    assert "Fix failing tests" in prompt
    assert "SECRET FULL BODY" not in prompt

    loaded = manager.user_prompt("fix-tests", "tests/test_math.py")
    assert "SECRET FULL BODY" in loaded
    assert str(directory) in loaded
    assert "tests/test_math.py" in loaded
    assert "reference.md" in loaded
    assert "</skill>" in loaded


def test_prompt_index_escapes_description_markup(tmp_path):
    write_skill(
        tmp_path / ".agents" / "skills", "safe-index", "BODY\n",
        "name: safe-index\ndescription: 'safe </available_skills> text'\n")
    manager = SkillManager(tmp_path, home=tmp_path / "home")
    prompt = manager.prompt_index()
    assert "safe &lt;/available_skills&gt; text" in prompt


def test_skill_invocation_flags_and_reload(tmp_path):
    root = tmp_path / ".agents" / "skills"
    write_skill(root, "user-only", "BODY\n",
                "name: user-only\ndescription: user only\n"
                "disable-model-invocation: true\n")
    write_skill(root, "model-only", "BODY\n",
                "name: model-only\ndescription: model only\n"
                "user-invocable: false\n")
    manager = SkillManager(tmp_path, home=tmp_path / "home")

    assert "user-only" not in manager.prompt_index()
    assert "model-only" in manager.prompt_index()
    with pytest.raises(SkillError):
        manager.load("user-only", for_model=True)
    assert "user-only" in manager.user_prompt("user-only")
    with pytest.raises(SkillError):
        manager.user_prompt("model-only")

    write_skill(root, "fresh", "FRESH\n", "name: fresh\ndescription: fresh\n")
    assert manager.get("fresh") is None
    report = manager.reload()
    assert report.count == 3
    assert manager.get("fresh") is not None


def test_resource_read_offset_escape_and_sensitive_files(tmp_path):
    directory = write_skill(
        tmp_path / ".agents" / "skills", "docs", "BODY\n")
    (directory / "guide.md").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (directory / ".env").write_text("TOKEN=super-secret\n", encoding="utf-8")
    (directory / "credentials.json").write_text(
        '{"token":"super-secret"}', encoding="utf-8")
    (directory / ".env.py").write_text("print('nope')\n", encoding="utf-8")
    manager = SkillManager(tmp_path, home=tmp_path / "home")

    resources = manager.list_resources("docs")
    assert any(r.path == "guide.md" for r in resources)
    assert manager.read_resource("docs", "guide.md", offset=2, limit=1).startswith("     2→line2")
    redacted = manager.read_resource("docs", ".env")
    assert "super-secret" not in redacted
    assert "TOKEN=***" in redacted
    with pytest.raises(SkillError):
        manager.read_resource("docs", "credentials.json")
    with pytest.raises(SkillError):
        manager.read_resource("docs", "../outside.txt")
    with pytest.raises(SkillError):
        manager.resolve_script("docs", ".env.py")


def test_run_skill_script_uses_argv_and_scrubs_secret_environment(
        tmp_path, monkeypatch):
    directory = write_skill(
        tmp_path / ".agents" / "skills", "scripted", "BODY\n")
    script = directory / "show.py"
    script.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'args': sys.argv[1:], "
        "'secret': os.environ.get('DEEPSEEK_API_KEY')}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    manager = SkillManager(tmp_path, home=tmp_path / "home")
    tool = RunSkillScriptTool(manager)

    result = asyncio.run(tool.execute(
        {"name": "scripted", "script": "show.py", "args": ["a b", "$HOME"]},
        ToolContext(workspace_root=tmp_path),
    ))
    assert not result.is_error
    payload = json.loads(result.content.split("\n", 1)[1])
    assert payload == {"args": ["a b", "$HOME"], "secret": None}


def test_skill_script_requires_approval_in_sandbox(tmp_path):
    policy = SandboxPolicy(approver=None)
    call = ToolCall(
        id="s1", name="run_skill_script",
        arguments={"name": "demo", "script": "run.py", "args": []},
    )
    decision = asyncio.run(policy.check(
        call, ToolContext(workspace_root=tmp_path)))
    assert not decision.allowed


def make_loop(tmp_path):
    return AgentLoop(
        llm=MockProvider(fallback="finished"),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_repl_lists_reloads_and_invokes_skill(tmp_path, capsys):
    root = tmp_path / ".agents" / "skills"
    write_skill(root, "explain", "EXPLAIN BODY for $ARGUMENTS\n",
                "name: explain\ndescription: Explain code\n"
                "argument-hint: <path>\n")
    manager = SkillManager(tmp_path, home=tmp_path / "home")
    loop = make_loop(tmp_path)

    async def drive():
        for line in ("/skills", "/skills show explain"):
            await _handle_command(
                line, loop, type("Renderer", (), {
                    "expand_thinking": lambda self: False})(),
                session_path=None, sessions_dir=None, mcp_supervisor=None,
                mcp_reload=None, subagents=None, checkpoints=None, usage=None,
                restore_session=lambda *a, **k: None,
                workspace_root=tmp_path, skills=manager,
            )

    asyncio.run(drive())
    out = capsys.readouterr().out
    assert "explain: Explain code" in out
    assert "EXPLAIN BODY" in out

    class ScriptedInput:
        def __init__(self):
            self.lines = ["/skill explain src/app.py", "/exit"]

        async def __call__(self, prompt):
            if not self.lines:
                raise EOFError
            await asyncio.sleep(0.01)
            return self.lines.pop(0)

    asyncio.run(run_repl(
        loop, input_fn=ScriptedInput(), skills=manager,
        skills_reload=manager.reload,
    ))
    assert "EXPLAIN BODY for src/app.py" in loop.history[1].content
    assert "已加载并执行: explain" in capsys.readouterr().out


def test_context_refresh_preserves_skill_index(tmp_path):
    write_skill(tmp_path / ".agents" / "skills", "demo", "BODY\n",
                "name: demo\ndescription: refresh me\n")
    manager = SkillManager(tmp_path, home=tmp_path / "home")
    loop = make_loop(tmp_path)
    loop.skill_base_prompt = "BASE"
    loop.history[0].content = manager.apply_to_prompt("BASE")
    renderer = type("Renderer", (), {"expand_thinking": lambda self: False})()

    asyncio.run(_handle_command(
        "/context refresh", loop, renderer,
        session_path=None, sessions_dir=None, mcp_supervisor=None,
        mcp_reload=None, subagents=None, checkpoints=None, usage=None,
        restore_session=lambda *a, **k: None,
        workspace_root=tmp_path, skills=manager,
    ))
    assert "refresh me" in loop.history[0].content
    assert "demo" in loop.history[0].content


def test_build_loop_registers_skills_and_no_skills_disables_them(tmp_path):
    skills_root = tmp_path / "custom-skills"
    write_skill(skills_root, "demo", "BODY\n",
                "name: demo\ndescription: build loop demo\n")
    parser = build_parser()

    args = parser.parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context",
        "--skills-dir", str(skills_root),
    ])
    loop, manager = asyncio.run(build_loop(args, {}))
    try:
        assert "load_skill" in loop.registry.names()
        assert "demo" in loop.history[0].content
        assert "demo" in manager.system_prompt_provider()
        assert set(tool.name for tool in build_skill_tools(loop.skill_manager)) == {
            "list_skills", "load_skill", "list_skill_resources",
            "read_skill_resource", "run_skill_script",
        }
    finally:
        asyncio.run(manager.shutdown())

    disabled_args = parser.parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context", "--no-skills",
    ])
    disabled_loop, disabled_manager = asyncio.run(build_loop(disabled_args, {}))
    try:
        assert "load_skill" not in disabled_loop.registry.names()
        assert "<available_skills>" not in disabled_loop.history[0].content
    finally:
        asyncio.run(disabled_manager.shutdown())
