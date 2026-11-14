"""CONFIG.md 与实现的一致性守护：每个 CLI 旗标都必须被文档收录。"""

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


def test_default_max_steps_matches_agent_default():
    from krelcode.agent.loop import DEFAULT_MAX_STEPS
    from krelcode.app import build_parser

    assert DEFAULT_MAX_STEPS >= 50
    assert build_parser().parse_args([]).max_steps == DEFAULT_MAX_STEPS


def test_subcommands_documented():
    text = (ROOT / "CONFIG.md").read_text(encoding="utf-8")
    for cmd in ("doctor", "init", "review"):
        assert f"`{cmd}`" in text


def test_repl_commands_documented():
    text = (ROOT / "CONFIG.md").read_text(encoding="utf-8")
    for command in ("/help", "/history", "/tools", "/session", "/sessions",
                    "/resume", "/mcp", "/agents", "/skills", "/skill",
                    "/undo", "/model", "/allow", "/cost", "/context",
                    "/compact", "/export", "/reset", "/stop", "/exit"):
        assert f"`{command}" in text, f"CONFIG.md 缺少 REPL 命令: {command}"


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
