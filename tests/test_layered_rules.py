"""Step 87: layered rules, on-demand directory rules and safe @import."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from krelcode.agent.context import ContextOptions, build_workspace_context_bundle
from krelcode.agent.context_assembler import ContextAssembler
from krelcode.agent.context_source import (
    ContextRequest,
    DirectoryRulesContextSource,
)
from krelcode.agent.fragments import (
    STABILITY_SESSION,
    PromptFragmentRegistry,
    agent_definition_fragment,
    base_fragment,
)
from krelcode.agent.intent import TurnIntent
from krelcode.agent.prompt_assembler import (
    PromptAssembler,
    PromptAssemblyRequest,
)
from krelcode.agent.rules import (
    RuleLimits,
    RuleLoader,
    RuleScope,
    extract_rule_targets,
    load_rules,
)
from krelcode.agent.turn.runtime_state import AgentRuntimeState
from krelcode.core.messages import Message, ToolCall
from krelcode.session.commands.builtin import builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers.runtime_ops import handle_context
from krelcode.session.commands.types import CommandRequest


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".krelcode").mkdir(parents=True)
    return home


def test_layered_rules_render_user_project_directory_and_nested_imports(tmp_path):
    home = _home(tmp_path)
    ws = tmp_path / "repo"
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir()
    (home / ".krelcode" / "AGENTS.md").write_text(
        "USER_RULE\n@user-extra.md\n", encoding="utf-8")
    (home / ".krelcode" / "user-extra.md").write_text(
        "USER_IMPORT\n", encoding="utf-8")
    (ws / "AGENTS.md").write_text(
        "PROJECT_RULE\n@docs/project.md\n", encoding="utf-8")
    (ws / "docs" / "project.md").write_text(
        "PROJECT_IMPORT\n", encoding="utf-8")
    (ws / "src" / "AGENTS.md").write_text(
        "DIRECTORY_RULE\n@local.md\n", encoding="utf-8")
    (ws / "src" / "local.md").write_text(
        "DIRECTORY_IMPORT\n", encoding="utf-8")

    bundle = RuleLoader(ws, home=home).load([ws / "src" / "app.py"])

    assert (
        bundle.content.index("USER_RULE")
        < bundle.content.index("PROJECT_RULE")
        < bundle.content.index("DIRECTORY_RULE")
    )
    for text in ("USER_IMPORT", "PROJECT_IMPORT", "DIRECTORY_IMPORT"):
        assert text in bundle.content
    assert [source.scope for source in bundle.sources] == [
        RuleScope.USER, RuleScope.USER,
        RuleScope.PROJECT, RuleScope.PROJECT,
        RuleScope.DIRECTORY, RuleScope.DIRECTORY,
    ]
    assert bundle.import_count == 3


def test_directory_rules_are_on_demand_not_recursive(tmp_path):
    home = _home(tmp_path)
    ws = tmp_path / "repo"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "src" / "AGENTS.md").write_text("SRC_RULE", encoding="utf-8")
    (ws / "tests" / "AGENTS.md").write_text("TESTS_RULE", encoding="utf-8")

    src_bundle = load_rules(ws, home=home, target_paths=[ws / "src" / "a.py"])
    root_bundle = load_rules(ws, home=home, target_paths=[ws / "README.md"])

    assert "SRC_RULE" in src_bundle.content
    assert "TESTS_RULE" not in src_bundle.content
    assert "SRC_RULE" not in root_bundle.content
    assert "TESTS_RULE" not in root_bundle.content


def test_import_cycle_is_bounded_and_fenced_example_is_not_expanded(tmp_path):
    home = _home(tmp_path)
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "AGENTS.md").write_text(
        "ROOT\n@a.md\n```md\n@missing.md\n```\n", encoding="utf-8")
    (ws / "a.md").write_text("IMPORT_A\n@b.md\n", encoding="utf-8")
    (ws / "b.md").write_text("IMPORT_B\n@a.md\n", encoding="utf-8")

    bundle = RuleLoader(ws, home=home).load_base()

    assert bundle.content.count("ROOT") == 1
    assert bundle.content.count("IMPORT_A") == 1
    assert bundle.content.count("IMPORT_B") == 1
    assert "@missing.md" in bundle.content
    assert any(issue.code == "duplicate-import" for issue in bundle.issues)


def test_import_rejects_outside_scope_sensitive_files_and_symlink_escape(tmp_path):
    home = _home(tmp_path)
    ws = tmp_path / "repo"
    ws.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE_SECRET", encoding="utf-8")
    (ws / ".env").write_text("DEEPSEEK_API_KEY=sk-leaked", encoding="utf-8")
    (ws / "credentials.json").write_text('{"token":"leaked"}', encoding="utf-8")
    (home / ".krelcode" / "config.toml").write_text(
        'api_key = "sk-global-config-leak"', encoding="utf-8")
    (home / ".krelcode" / "AGENTS.md").write_text(
        "@config.toml\nGLOBAL", encoding="utf-8")
    (ws / "AGENTS.md").write_text(
        "@../outside.md\n@.env\n@credentials.json\n", encoding="utf-8")
    try:
        (ws / "linked.md").symlink_to(outside)
        (ws / "AGENTS.md").write_text(
            (ws / "AGENTS.md").read_text(encoding="utf-8") + "@linked.md\n",
            encoding="utf-8")
    except OSError:
        pass

    bundle = RuleLoader(ws, home=home).load_base()

    assert "OUTSIDE_SECRET" not in bundle.content
    assert "sk-leaked" not in bundle.content
    assert "sk-global-config-leak" not in bundle.content
    assert '"token":"leaked"' not in bundle.content
    codes = {issue.code for issue in bundle.issues}
    assert "import-outside-scope" in codes
    assert "import-sensitive" in codes


def test_import_depth_file_count_and_character_budget_are_enforced(tmp_path):
    home = _home(tmp_path)
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "AGENTS.md").write_text(
        "@one.md\n@two.md\n", encoding="utf-8")
    (ws / "one.md").write_text("@nested.md\nONE", encoding="utf-8")
    (ws / "nested.md").write_text("NESTED", encoding="utf-8")
    (ws / "two.md").write_text("TWO", encoding="utf-8")

    bundle = RuleLoader(
        ws,
        home=home,
        limits=RuleLimits(
            max_chars=320, import_max_depth=1, import_max_files=2),
    ).load_base()

    assert len(bundle.content) <= 320
    assert any(issue.code == "import-depth" for issue in bundle.issues)


def test_character_budget_truncates_without_breaking_hard_limit(tmp_path):
    home = _home(tmp_path)
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("R" * 10_000, encoding="utf-8")

    bundle = RuleLoader(
        ws, home=home, limits=RuleLimits(max_chars=512)).load_base()

    assert len(bundle.content) <= 512
    assert bundle.truncated
    assert "字符预算截断" in bundle.content


def test_directory_context_source_uses_current_turn_paths_only(tmp_path):
    home = _home(tmp_path)
    ws = tmp_path / "repo"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "src" / "AGENTS.md").write_text("SRC_RULE", encoding="utf-8")
    (ws / "tests" / "AGENTS.md").write_text("TESTS_RULE", encoding="utf-8")
    source = DirectoryRulesContextSource(RuleLoader(ws, home=home))

    history = (
        Message.user("修复 src/main.py"),
        Message.assistant("done"),
        Message.user("继续处理 src/worker.py"),
    )
    request = ContextRequest(
        turn_id="t1",
        step=2,
        history=history,
        workspace_root=ws,
    )
    layers = source.collect(request)

    assert len(layers) == 1
    assert "SRC_RULE" in layers[0].content
    assert "TESTS_RULE" not in layers[0].content
    assert layers[0].cacheable is True
    assert source.last_bundle.entry_count == 1


def test_directory_rules_join_l3_cacheable_prefix_before_l4(tmp_path):
    home = _home(tmp_path)
    ws = tmp_path / "repo"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "AGENTS.md").write_text(
        "DIRECTORY_RULE", encoding="utf-8")
    registry = PromptFragmentRegistry([
        base_fragment(),
        agent_definition_fragment("reviewer", "DEFINITION_RULE"),
    ])
    assembler = PromptAssembler(
        ContextAssembler([
            DirectoryRulesContextSource(RuleLoader(ws, home=home)),
        ]),
        registry_provider=lambda: registry,
    )
    snapshot = registry.snapshot()
    plan = asyncio.run(assembler.assemble(PromptAssemblyRequest(
        context_request=ContextRequest(
            turn_id="directory-cache",
            step=1,
            history=(Message.user("fix src/main.py"),),
            workspace_root=ws,
            prompt_snapshot=snapshot,
        ),
        prompt_snapshot=snapshot,
    )))

    stable_ids = plan.cache.stable_fragments
    assert "directory:rules" in stable_ids
    assert "agent_definition:reviewer" in stable_ids
    assert stable_ids.index("directory:rules") < stable_ids.index(
        "agent_definition:reviewer")
    directory = next(
        fragment for fragment in plan.fragments
        if fragment.id == "directory:rules")
    assert directory.cacheable is True
    assert directory.stability == STABILITY_SESSION
    assert plan.system_text.index("DIRECTORY_RULE") < plan.system_text.index(
        "DEFINITION_RULE")


def test_extract_rule_targets_reads_intent_attachments_and_tool_arguments(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "src").mkdir()
    target = ws / "src" / "app.py"
    target.write_text("x", encoding="utf-8")
    history = (
        Message.user(
            f'修复 <attached_file path="{target}">x</attached_file> 和 src/other.py'),
    )
    intent = TurnIntent(target_files=(target,))
    calls = Message.assistant(tool_calls=[ToolCall(
        id="1", name="write_file",
        arguments={"file_path": str(ws / "src" / "new.py")},
    )])

    targets = extract_rule_targets(ws, intent=intent, history=history + (calls,))

    assert target in targets
    assert ws / "src" / "other.py" in targets
    assert ws / "src" / "new.py" in targets


def test_build_workspace_context_keeps_rule_bundle_diagnostics(tmp_path):
    home = _home(tmp_path)
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("@missing.md\nROOT", encoding="utf-8")

    bundle = asyncio.run(build_workspace_context_bundle(
        ws,
        home=home,
        options=ContextOptions(include_git=False, include_tree=False),
    ))

    assert bundle.rule_bundle is not None
    assert bundle.rule_bundle.content == bundle.sections[0].content
    assert bundle.rule_bundle.issues[0].code == "import-missing"
    assert "规则问题: 1" in "\n".join(bundle.summary_lines())


def test_context_rules_command_reports_scopes_imports_and_issues(tmp_path):
    home = _home(tmp_path)
    ws = tmp_path / "repo"
    (ws / "src").mkdir(parents=True)
    (ws / "AGENTS.md").write_text("@missing.md\nROOT", encoding="utf-8")
    (ws / "src" / "AGENTS.md").write_text("DIR", encoding="utf-8")
    loader = RuleLoader(ws, home=home)
    source = DirectoryRulesContextSource(loader)
    source.collect(ContextRequest(
        turn_id="t1",
        step=1,
        history=(Message.user("修复 src/a.py"),),
        workspace_root=ws,
    ))

    state = AgentRuntimeState()
    state.context.workspace_context = SimpleNamespace(
        rule_bundle=loader.load_base())
    loop = SimpleNamespace(
        runtime_state=state,
        services=SimpleNamespace(
            context=SimpleNamespace(
                rule_loader=loader,
                directory_rule_source=source,
            ),
            prompt=SimpleNamespace(registry=None),
        ),
    )
    ctx = CommandContext(
        request=CommandRequest(
            name="context", arguments="rules", raw_text="/context rules"),
        loop=loop,
        command_registry=builtin_command_registry(),
        workspace_root=ws,
    )

    result = asyncio.run(handle_context(ctx))
    output = "\n".join(result.output)

    assert "[rules]" in output
    assert "project" in output and "directory" in output
    assert "import-missing" in output
    assert "ROOT" not in output


def test_build_loop_injects_directory_rule_before_first_model_call(tmp_path):
    from krelcode.app import build_loop, build_parser
    from krelcode.llm.providers.mock import MockProvider

    ws = tmp_path / "repo"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "AGENTS.md").write_text(
        "SRC_ONLY_RULE\n@details.md\n", encoding="utf-8")
    (ws / "src" / "details.md").write_text(
        "SRC_IMPORTED_RULE", encoding="utf-8")
    args = build_parser().parse_args([
        "--mock", "--workspace", str(ws), "--no-compress",
        "--no-skills", "--no-plugins", "--no-mcp", "--no-session",
        "--no-checkpoint", "--no-sandbox",
    ])
    config = {"context": {
        "include_git": False,
        "include_tree": False,
        "include_directory_rules": True,
    }}

    loop, manager = asyncio.run(build_loop(args, config))
    provider = MockProvider(scripted=["ok"])
    loop.llm = provider
    try:
        assert asyncio.run(loop.run("修复 src/main.py")) == "ok"
        system_prompt = provider.calls[0][0].content
        assert "SRC_ONLY_RULE" in system_prompt
        assert "SRC_IMPORTED_RULE" in system_prompt
        rule_layers = [
            layer for layer in loop.last_context_assembly.layers
            if layer.id == "directory:rules"
        ]
        assert len(rule_layers) == 1
        assert rule_layers[0].source == "directory:rules"
    finally:
        asyncio.run(manager.shutdown())


def test_context_options_parse_rule_layering_and_import_limits():
    options = ContextOptions.from_config({
        "include_directory_rules": False,
        "rules_imports": False,
        "rules_import_max_depth": 99,
        "rules_import_max_files": 99,
        "rules_max_chars": 4096,
    })

    assert options.include_directory_rules is False
    assert options.rules_imports is False
    assert options.rules_import_max_depth == 12
    assert options.rules_import_max_files == 99
    assert options.rule_limits().max_chars == 4096


def test_nonexistent_nested_target_uses_nearest_existing_ancestor_rules(tmp_path):
    home = _home(tmp_path)
    ws = tmp_path / "repo"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "AGENTS.md").write_text("SRC_RULE", encoding="utf-8")

    bundle = load_rules(
        ws,
        home=home,
        target_paths=[ws / "src" / "new-package" / "new_file.py"],
    )

    assert "SRC_RULE" in bundle.content
