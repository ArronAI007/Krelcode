"""CONFIG.md 与实现的一致性守护：每个 CLI 旗标都必须被文档收录。"""

import asyncio
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_all_cli_flags_documented():
    from krelcode.app import build_parser
    text = (ROOT / "CONFIG.md").read_text(encoding="utf-8")
    missing = []
    for action in build_parser()._actions:
        for opt in action.option_strings:
            if opt in ("-h", "--help"):
                continue
            if opt not in text:
                missing.append(opt)
    assert not missing, f"CONFIG.md 缺少旗标文档: {missing}"


def test_tui_mouse_flags_are_explicit_and_default_to_config_resolution():
    from krelcode.app import build_parser

    parser = build_parser()
    assert parser.parse_args([]).tui_mouse is None
    assert parser.parse_args(["--tui-mouse"]).tui_mouse is True
    assert parser.parse_args(["--no-tui-mouse"]).tui_mouse is False


def test_tui_mouse_default_is_copy_friendly(tmp_path: Path):
    from krelcode.app import build_parser
    from krelcode.bootstrap.application_stages.context import (
        build_application_context,
    )

    config_path = tmp_path / "empty.toml"
    config_path.write_text("", encoding="utf-8")
    args = build_parser().parse_args([
        "--mock",
        "--workspace", str(tmp_path),
        "--config", str(config_path),
        "--no-session",
    ])
    context = asyncio.run(build_application_context(args))
    assert context.args.tui_mouse is False


def test_default_max_steps_matches_agent_default():
    from krelcode.agent.loop import DEFAULT_MAX_STEPS
    from krelcode.app import build_parser

    assert DEFAULT_MAX_STEPS >= 50
    assert build_parser().parse_args([]).max_steps == DEFAULT_MAX_STEPS


def test_subcommands_documented():
    text = (ROOT / "CONFIG.md").read_text(encoding="utf-8")
    for cmd in ("doctor", "init", "review", "security-review", "benchmark"):
        assert f"`{cmd}`" in text


def test_repl_commands_documented():
    text = (ROOT / "CONFIG.md").read_text(encoding="utf-8")
    for command in ("/help", "/version", "/history", "/tools", "/session", "/sessions",
                    "/resume", "/mcp", "/agents", "/skills", "/skill",
                    "/undo", "/model", "/allow", "/permissions", "/cost",
                    "/context", "/context plan", "/context why",
                    "/context sources", "/context cache", "/context ledger",
                    "/context refresh",
                    "/compact", "/export", "/reset", "/stop", "/exit",
                    "/thinking", "/tool-output", "/diff", "/plugins", "/fork",
                    "/rewind", "/rename", "/delete", "/memory",
                    "/memory list", "/memory show", "/memory add",
                    "/memory search", "/memory pin",
                    "/memory forget", "/memory review", "/memory migrate",
                    "/security-review"):
        assert f"`{command}" in text, f"CONFIG.md 缺少 REPL 命令: {command}"


def test_context_memory_guide_covers_config_storage_commands_and_events():
    text = (ROOT / "docs" / "context-and-memory.md").read_text(encoding="utf-8")
    for required in (
        "[context]", "[memory]", "/context plan", "/context why",
        "/context sources", "/context cache", "/context ledger",
        "/context refresh",
        "/memory list", "/memory show", "/memory add", "/memory search",
        "/memory pin", "/memory forget", "/memory review", "/memory migrate",
        "/compact", "entries.jsonl", "candidates.jsonl", "backup",
        "context_assembled", "context_dropped", "memory_loaded",
        "memory_candidate", "memory_written", "compact_checkpoint",
    ):
        assert required in text, f"context/memory 文档缺少: {required}"


def test_env_vars_documented():
    text = (ROOT / "CONFIG.md").read_text(encoding="utf-8")
    for var in ("KRELCODE_PROVIDER", "KRELCODE_MODEL", "OPENAI_API_KEY",
                "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BASE_URL", "NO_COLOR"):
        assert var in text, f"CONFIG.md 缺少环境变量: {var}"


def test_hook_triggers_match_implementation():
    from krelcode.hooks import SUPPORTED_TRIGGERS
    text = (ROOT / "CONFIG.md").read_text(encoding="utf-8")
    for trigger in SUPPORTED_TRIGGERS:
        assert trigger in text
