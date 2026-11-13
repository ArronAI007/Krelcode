"""运行时 Sandbox：静态审计、环境净化、OS 后端生成与工具接线。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from krelcode.app import build_execution_sandbox, build_loop, build_parser
from krelcode.mcp.stdio_transport import build_stdio_environment
from krelcode.safety import sandbox as sandbox_module
from krelcode.safety.environment import scrub_environment
from krelcode.safety.sandbox import ExecutionSandbox, SandboxError
from krelcode.skills import SkillManager
from krelcode.skills.tools import RunSkillScriptTool
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.check_syntax import CheckSyntaxTool
from krelcode.tools.builtin.find_refs import FindRefsTool
from krelcode.tools.builtin.glob import GlobTool
from krelcode.tools.builtin.grep import GrepTool
from krelcode.tools.builtin.repo_map import RepoMapTool
from krelcode.tools.builtin.run_command import RunCommandTool
from krelcode.tools.paths import safe_file_in_workspace


def run(coro):
    return asyncio.run(coro)


def static_sandbox(root: Path, *, network: bool = False) -> ExecutionSandbox:
    return ExecutionSandbox(root, backend="static", network=network)


# -- 静态命令审计 -------------------------------------------------------------------

def test_static_audit_blocks_escape_sensitive_shell_and_network(tmp_path):
    sandbox = static_sandbox(tmp_path)
    commands = [
        "cat ../outside.txt",
        "cat /etc/passwd",
        "cat .env",
        "cat krelcode.toml",
        "cat .git/config",
        "cat $HOME/.ssh/id_rsa",
        "cat ${PWD}/../outside.txt",
        "cat $PWD/../outside.txt",
        "cd $OLDPWD && cat outside.txt",
        "cat $DIR/../outside.txt",
        "echo $(cat ../outside.txt)",
        "sh -c 'cat ../outside.txt'",
        "sh -lc 'cat ../outside.txt'",
        "eval 'cat ../outside.txt'",
        "curl https://example.com",
        "git fetch origin",
        "env curl https://example.com",
        "git -C . fetch origin",
        "pip install requests",
        "pip --disable-pip-version-check install requests",
    ]
    for command in commands:
        assert sandbox.audit_command(command, tmp_path), command


def test_static_audit_allows_workspace_commands_and_slash_arguments(tmp_path):
    (tmp_path / "README.md").write_text("demo", encoding="utf-8")
    sandbox = static_sandbox(tmp_path)
    assert sandbox.audit_command("cat README.md", tmp_path) is None
    assert sandbox.audit_command("git status --short", tmp_path) is None
    assert sandbox.audit_command("git checkout feature/foo", tmp_path) is None
    assert sandbox.audit_command(r"grep -E 'foo/bar' README.md", tmp_path) is None


def test_network_commands_require_explicit_sandbox_network(tmp_path):
    allowed = static_sandbox(tmp_path, network=True)
    assert allowed.audit_command("curl https://example.com", tmp_path) is None
    assert allowed.audit_command("git fetch origin", tmp_path) is None
    assert allowed.audit_command("pip install requests", tmp_path) is None


def test_exec_audit_and_cwd_must_stay_in_allowed_roots(tmp_path):
    workspace = tmp_path / "workspace"
    skill = tmp_path / "skill"
    outside = tmp_path / "outside"
    for path in (workspace, skill, outside):
        path.mkdir()
    (skill / "run.py").write_text("print('ok')", encoding="utf-8")
    sandbox = static_sandbox(workspace)

    assert sandbox.audit_exec(
        [sys.executable, str(skill / "run.py")], skill,
        allowed_read_roots=[skill],
    ) is None
    assert sandbox.audit_exec(
        [sys.executable, str(skill / "run.py")], outside,
        allowed_read_roots=[skill],
    )
    with pytest.raises(SandboxError):
        sandbox.prepare_exec(
            [sys.executable, str(skill / "run.py")], outside,
            extra_read_roots=[skill],
        )


# -- 环境隔离与进程接线 -------------------------------------------------------------

def test_sandbox_environment_scrubs_secrets_and_redirects_runtime_dirs(
        tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy-user:secret@example")
    monkeypatch.setenv("http_proxy", "http://proxy-user:secret@example")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.dylib")
    monkeypatch.setenv("KRELCODE_BENIGN", "visible")

    prepared = static_sandbox(tmp_path).prepare_shell("env", tmp_path)
    env = prepared.env
    runtime = (tmp_path / ".krelcode" / "sandbox").resolve()
    assert env["KRELCODE_BENIGN"] == "visible"
    assert Path(env["HOME"]).resolve() == runtime / "home"
    assert Path(env["TMPDIR"]).resolve() == runtime / "tmp"
    for name in (
        "DEEPSEEK_API_KEY", "GITHUB_TOKEN", "HTTPS_PROXY", "http_proxy",
        "SSH_AUTH_SOCK", "LD_PRELOAD",
    ):
        assert name not in env


def test_scrub_environment_is_case_insensitive_for_sensitive_patterns():
    out = scrub_environment({
        "openai_api_key": "secret",
        "https_proxy": "proxy-secret",
        "NORMAL": "keep",
    })
    assert out == {"NORMAL": "keep"}


def test_run_command_uses_sandbox_env_and_hard_denies_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("KRELCODE_TEST_API_KEY", "must-not-leak")
    ctx = ToolContext(
        workspace_root=tmp_path,
        sandbox=static_sandbox(tmp_path),
    )
    env_result = run(RunCommandTool().execute({"command": "env"}, ctx))
    assert not env_result.is_error
    assert env_result.data["sandbox_backend"] == "static"
    assert str((tmp_path / ".krelcode" / "sandbox" / "home").resolve()) in env_result.content
    assert "must-not-leak" not in env_result.content

    denied = run(RunCommandTool().execute({"command": "cat ../outside"}, ctx))
    assert denied.is_error and denied.data.get("sandbox_denied")
    assert "Sandbox 拒绝命令" in denied.content


# -- OS 后端探测与 argv 生成 --------------------------------------------------------

def test_auto_backend_falls_back_when_seatbelt_probe_fails(monkeypatch):
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(sandbox_module, "_probe_seatbelt", lambda executable: False)
    sandbox_module._detect_backend.cache_clear()
    try:
        assert sandbox_module._detect_backend("auto", "darwin") == "static"
        with pytest.raises(SandboxError, match="seatbelt 不可用"):
            sandbox_module._detect_backend("seatbelt", "darwin")
    finally:
        sandbox_module._detect_backend.cache_clear()


def test_auto_backend_uses_probed_seatbelt(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_module.shutil, "which", lambda name: "/usr/bin/sandbox-exec")
    monkeypatch.setattr(sandbox_module, "_probe_seatbelt", lambda executable: True)
    sandbox_module._detect_backend.cache_clear()
    try:
        sandbox = ExecutionSandbox(tmp_path, backend="auto")
        assert sandbox.backend == "seatbelt"
        prepared = sandbox.prepare_shell("printf ok", tmp_path)
        assert prepared.argv[:2] == ["/usr/bin/sandbox-exec", "-p"]
        assert "sandbox-exec" in prepared.argv[0]
    finally:
        sandbox_module._detect_backend.cache_clear()


def test_seatbelt_profile_is_workspace_scoped_and_network_denied(tmp_path):
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    sandbox = static_sandbox(tmp_path)
    profile = sandbox._seatbelt_profile([skill.resolve()])
    assert "(deny default)" in profile
    assert f'(subpath "{tmp_path.resolve()}")' in profile
    assert f'(subpath "{skill.resolve()}")' in profile
    assert "network-outbound" not in profile

    network_profile = sandbox._seatbelt_profile([skill.resolve()])
    assert "(allow network-outbound)" not in network_profile
    assert ExecutionSandbox(
        tmp_path, backend="static", network=True)._seatbelt_profile([]).count(
            "(allow network-outbound)") == 1


def test_bubblewrap_argv_binds_workspace_and_controls_network(tmp_path):
    skill = tmp_path / "skill"
    skill.mkdir()
    sandbox = static_sandbox(tmp_path)
    argv = sandbox._bubblewrap_argv(["sh", "-c", "true"], tmp_path, [skill.resolve()])
    assert "--unshare-all" in argv and "--die-with-parent" in argv
    assert "--share-net" not in argv
    assert ["--bind", str(tmp_path), str(tmp_path)] == argv[
        argv.index("--bind"):argv.index("--bind") + 3]
    proc_events = [i for i, token in enumerate(argv) if token == "/proc"]
    assert proc_events == [argv.index("--proc") + 1]

    networked = ExecutionSandbox(tmp_path, backend="static", network=True)
    assert "--share-net" in networked._bubblewrap_argv(["true"], tmp_path, [])


# -- 搜索工具 symlink 防逃逸 --------------------------------------------------------

def _make_external_file(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "leak.py"
    target.write_text("def escape_symbol():\n    return 'ESCAPE_MARKER'\n", encoding="utf-8")
    return target


def test_safe_file_in_workspace_rejects_escaping_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = _make_external_file(tmp_path)
    link = workspace / "leak.py"
    try:
        link.symlink_to(target)
    except OSError as e:
        pytest.skip(f"symlink 不可用: {e}")
    assert not safe_file_in_workspace(link, workspace)


def test_search_tools_do_not_follow_external_file_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = _make_external_file(tmp_path)
    try:
        (workspace / "leak.py").symlink_to(target)
    except OSError as e:
        pytest.skip(f"symlink 不可用: {e}")
    (workspace / "safe.py").write_text("def safe_symbol():\n    pass\n", encoding="utf-8")
    ctx = ToolContext(workspace_root=workspace, sandbox=static_sandbox(workspace))

    grep = run(GrepTool().execute({"pattern": "ESCAPE_MARKER"}, ctx))
    assert "leak.py" not in grep.content

    glob = run(GlobTool().execute({"pattern": "**/*.py"}, ctx))
    assert "leak.py" not in glob.content and "safe.py" in glob.content

    repo_map = run(RepoMapTool().execute({}, ctx))
    assert "escape_symbol" not in repo_map.content and "safe_symbol" in repo_map.content

    refs = run(FindRefsTool().execute({"symbol": "escape_symbol"}, ctx))
    assert "leak.py" not in refs.content and refs.data["count"] == 0


def test_check_syntax_does_not_follow_external_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def broken(:\n", encoding="utf-8")
    try:
        (workspace / "broken.py").symlink_to(outside)
    except OSError as e:
        pytest.skip(f"symlink 不可用: {e}")
    (workspace / "safe.py").write_text("def ok():\n    return 1\n", encoding="utf-8")
    ctx = ToolContext(workspace_root=workspace, sandbox=static_sandbox(workspace))
    result = run(CheckSyntaxTool().execute({}, ctx))
    assert not result.is_error
    assert result.data["files"] == 1


def test_walkers_do_not_descend_external_directory_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside_dir"
    workspace.mkdir()
    outside.mkdir()
    (outside / "leak.py").write_text("ESCAPE_DIR_MARKER = 1\n", encoding="utf-8")
    try:
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as e:
        pytest.skip(f"symlink 不可用: {e}")
    ctx = ToolContext(workspace_root=workspace, sandbox=static_sandbox(workspace))
    result = run(GrepTool().execute({"pattern": "ESCAPE_DIR_MARKER"}, ctx))
    assert result.data["files_scanned"] == 0


# -- Skill / MCP / 组装根 -----------------------------------------------------------

def _write_skill(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: sandbox test\n---\nBODY\n",
        encoding="utf-8",
    )
    return directory


def test_skill_script_uses_sandbox_and_blocks_escaping_argv(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-leak")
    directory = _write_skill(tmp_path / ".agents" / "skills", "sandboxed")
    (directory / "show.py").write_text(
        "import json, os\nprint(json.dumps({'home': os.environ.get('HOME'), "
        "'key': os.environ.get('DEEPSEEK_API_KEY')}))\n",
        encoding="utf-8",
    )
    manager = SkillManager(tmp_path, home=tmp_path / "home")
    tool = RunSkillScriptTool(manager)
    ctx = ToolContext(workspace_root=tmp_path, sandbox=static_sandbox(tmp_path))

    denied = run(tool.execute(
        {"name": "sandboxed", "script": "show.py", "args": ["../../../../outside"]}, ctx))
    assert denied.is_error and denied.data.get("sandbox_denied")

    result = run(tool.execute({"name": "sandboxed", "script": "show.py"}, ctx))
    assert not result.is_error
    assert str((tmp_path / ".krelcode" / "sandbox" / "home").resolve()) in result.content
    assert "must-not-leak" not in result.content
    assert result.data["sandbox_backend"] == "static"


def test_mcp_stdio_environment_scrubs_host_secrets_but_allows_explicit_overlay(
        monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "host-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "host-token")
    monkeypatch.setenv("KRELCODE_BENIGN_MCP", "visible")
    env = build_stdio_environment({"MCP_EXPLICIT_TOKEN": "configured"})
    assert "DEEPSEEK_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["KRELCODE_BENIGN_MCP"] == "visible"
    assert env["MCP_EXPLICIT_TOKEN"] == "configured"


def test_build_loop_injects_configured_sandbox_and_no_sandbox_disables_it(tmp_path):
    parser = build_parser()
    base = ["--mock", "--workspace", str(tmp_path), "--no-context", "--no-session"]
    args = parser.parse_args(base + ["--sandbox-backend", "static"])
    loop, manager = run(build_loop(args, {}))
    try:
        assert loop.tool_context.sandbox is not None
        assert loop.tool_context.sandbox.backend == "static"
        assert loop.tool_context.sandbox.network is False
    finally:
        run(manager.shutdown())

    disabled_args = parser.parse_args(base + ["--no-sandbox"])
    disabled_loop, disabled_manager = run(build_loop(disabled_args, {}))
    try:
        assert disabled_loop.tool_context.sandbox is None
    finally:
        run(disabled_manager.shutdown())


def test_build_loop_reads_sandbox_toml_and_cli_overrides(tmp_path):
    parser = build_parser()
    base = ["--mock", "--workspace", str(tmp_path), "--no-context", "--no-session"]
    config = {"sandbox": {"backend": "static", "network": True}}
    loop, manager = run(build_loop(parser.parse_args(base), config))
    try:
        assert loop.tool_context.sandbox.backend == "static"
        assert loop.tool_context.sandbox.network is True
    finally:
        run(manager.shutdown())

    sandbox = build_execution_sandbox(parser.parse_args(base), config, tmp_path)
    assert sandbox is not None and sandbox.network is True
    no_sandbox = build_execution_sandbox(
        parser.parse_args(base + ["--no-sandbox"]), config, tmp_path)
    assert no_sandbox is None


def test_rebind_uses_child_worktree_root(tmp_path):
    parent = static_sandbox(tmp_path / "main")
    (tmp_path / "main").mkdir()
    child_root = tmp_path / "child"
    child_root.mkdir()
    child = parent.rebind(child_root)
    assert child.backend == "static"
    assert child.workspace_root == child_root.resolve()
    assert child.audit_command("cat ../secret", child_root)
