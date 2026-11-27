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
from krelcode.safety.sandbox import (
    ExecutionSandbox,
    SandboxError,
    SandboxPermissionProfile,
    SandboxedCommand,
)
from krelcode.contracts.governance import (
    AuditCorrelation,
    GovernanceDecision,
    PolicyVersion,
    SandboxProfile,
    SandboxResourceLimits,
)
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
        "env -S 'curl https://example.com'",
        r"find . -exec curl https://example.com {} \;",
        r"find . -exec sh -c 'cat ../outside.txt' {} \;",
        "sort --compress-program=curl input.txt",
        "rg --pre 'curl https://example.com' TODO .",
        "command curl https://example.com",
        "exec curl https://example.com",
        "nohup curl https://example.com",
        "nice -n 5 curl https://example.com",
        "time curl https://example.com",
        "timeout 10 curl https://example.com",
        "stdbuf -oL curl https://example.com",
        "echo https://example.com | xargs curl",
        "cat token.json",
        "cat .config/gcloud/credentials.db",
        "cat .ENV",
        "git -C . fetch origin",
    ]
    for command in commands:
        assert sandbox.audit_command(command, tmp_path), command


def test_static_audit_allows_workspace_commands_and_slash_arguments(tmp_path):
    (tmp_path / "README.md").write_text("demo", encoding="utf-8")
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "settings.json").write_text("{}", encoding="utf-8")
    sandbox = static_sandbox(tmp_path)
    assert sandbox.audit_command("cat README.md", tmp_path) is None
    assert sandbox.audit_command("cat .config/settings.json", tmp_path) is None
    assert sandbox.audit_command("git status --short", tmp_path) is None
    assert sandbox.audit_command("git checkout feature/foo", tmp_path) is None
    assert sandbox.audit_command(r"grep -E 'foo/bar' README.md", tmp_path) is None


def test_static_audit_rejects_workspace_hardlink_to_external_inode(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    os.link(outside, workspace / "ordinary.txt")
    sandbox = static_sandbox(workspace)

    violation = sandbox.audit_command("cat ordinary.txt", workspace)
    assert violation and "硬链接" in violation
    glob_violation = sandbox.audit_command("cat *", workspace)
    assert glob_violation and "硬链接" in glob_violation


def test_static_audit_checks_shell_glob_expansion_before_execution(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "safe.txt").write_text("safe", encoding="utf-8")
    (workspace / ".env").write_text("API_KEY=secret", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "linked.txt").symlink_to(outside)
    sandbox = static_sandbox(workspace)

    assert sandbox.audit_command("cat *.txt", workspace)
    assert sandbox.audit_command("cat *", workspace)
    (workspace / ".env").unlink()
    (workspace / "linked.txt").unlink()
    assert sandbox.audit_command("cat *.txt", workspace) is None


def test_network_commands_require_explicit_sandbox_network(tmp_path):
    allowed = static_sandbox(tmp_path, network=True)
    assert allowed.audit_command("curl https://example.com", tmp_path) is None
    assert allowed.audit_command("git fetch origin", tmp_path) is None
    assert allowed.audit_command("pip install requests", tmp_path) is None


def test_sandbox_resource_limits_and_egress_allowlist(tmp_path):
    sandbox = ExecutionSandbox(
        tmp_path,
        backend="static",
        network=True,
        allowed_domains=("example.com",),
        resource_limits=SandboxResourceLimits(
            cpu_seconds=2,
            memory_mb=128,
            storage_mb=1,
            process_limit=12,
        ),
    )
    assert sandbox.audit_command(
        "curl https://example.com/docs", tmp_path) is None
    assert sandbox.audit_command(
        "curl https://evil.invalid/file", tmp_path)
    kwargs = sandbox.runtime_policy.process_kwargs(sandbox.process_platform)
    assert "preexec_fn" in kwargs
    capabilities = sandbox.capabilities
    assert capabilities.cpu_limit is True
    assert capabilities.memory_limit is True
    assert capabilities.storage_limit is True
    assert capabilities.process_limit is True


def test_sandbox_storage_quota_rejects_oversized_runtime_dir(tmp_path):
    sandbox = ExecutionSandbox(
        tmp_path,
        backend="static",
        resource_limits=SandboxResourceLimits(storage_mb=1),
    )
    runtime = tmp_path / ".krelcode" / "sandbox"
    runtime.mkdir(parents=True)
    (runtime / "large.bin").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(SandboxError, match="存储已超过配额"):
        sandbox.prepare_shell("true", tmp_path)


def test_container_backend_argv_applies_resource_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "krelcode.safety.sandbox_runtime.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    sandbox = ExecutionSandbox(
        tmp_path,
        backend="static",
        network=False,
        resource_limits=SandboxResourceLimits(
            cpu_seconds=3,
            memory_mb=256,
            storage_mb=2,
            process_limit=20,
        ),
    )
    argv = sandbox.runtime_policy.container_argv(
        ["/bin/true"],
        tmp_path,
        workspace_root=tmp_path,
        permission_profile="workspace",
        network_allowed=False,
        platform=sandbox.process_platform,
    )
    text = " ".join(argv)
    assert "--network none" in text
    assert "--memory 256m" in text
    assert "--pids-limit 20" in text
    assert "--ulimit cpu=3" in text


def test_user_install_profile_allows_only_user_owned_command_locations(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    target = workspace / "hermes"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    sandbox = ExecutionSandbox(
        workspace, backend="static", user_home=home)
    command = f"ln -sf {target} {home}/.local/bin/hermes"
    home_command = f'ln -sf {target} "$HOME/.local/bin/hermes"'

    assert sandbox.audit_command(command, workspace) is None
    assert sandbox.audit_command(home_command, workspace) is None
    prepared = sandbox.prepare_shell(home_command, workspace)
    assert (
        prepared.permission_profile
        is SandboxPermissionProfile.USER_INSTALL
    )
    assert Path(prepared.env["HOME"]) != home.resolve()
    assert (
        Path(prepared.env["HOME"]) / ".local" / "bin"
    ).resolve() == (home / ".local" / "bin").resolve()

    result = run(RunCommandTool().execute(
        {"command": command, "sandbox_permission": "user-install"},
        ToolContext(
            workspace_root=workspace,
            sandbox=sandbox,
        ),
    ))
    assert not result.is_error
    installed = home / ".local" / "bin" / "hermes"
    assert installed.is_symlink()
    assert installed.resolve() == target.resolve()
    assert result.data["sandbox_permission_profile"] == "user-install"


def test_user_install_profile_rejects_system_paths_sudo_and_credentials(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    sandbox = ExecutionSandbox(
        workspace, backend="static", user_home=home)
    denied = [
        f"ln -sf {workspace}/hermes /usr/local/bin/hermes",
        f"ln -sf {workspace}/hermes /etc/hermes",
        f"ln -sf {workspace}/hermes /System/hermes",
        "sudo ln -sf hermes ~/.local/bin/hermes",
        "cat $HOME/.ssh/id_rsa",
        "cat $HOME/.local/share/token.json",
    ]
    for command in denied:
        assert sandbox.audit_command(
            command,
            workspace,
            permission_profile="user-install",
        ), command

    ordinary = f"ln -sf {workspace}/hermes {home}/.local/bin/hermes"
    assert sandbox.audit_command(
        ordinary,
        workspace,
        permission_profile="workspace",
    )


def test_install_network_is_enabled_only_for_install_commands(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    sandbox = ExecutionSandbox(
        workspace,
        backend="static",
        user_home=home,
        network=False,
        install_network=True,
    )
    assert sandbox.audit_command("pip install requests", workspace) is None
    assert sandbox.audit_command("uv tool install hermes-agent", workspace) is None
    assert sandbox.audit_command("git clone https://example.com/repo", workspace) is None
    assert sandbox.audit_command("curl https://example.com", workspace)
    assert sandbox.audit_command("git fetch origin", workspace)

    no_install_network = ExecutionSandbox(
        workspace,
        backend="static",
        user_home=home,
        network=False,
        install_network=False,
    )
    assert no_install_network.audit_command("pip install requests", workspace)


def test_installer_url_and_script_infer_application_home(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    sandbox = ExecutionSandbox(
        workspace,
        backend="static",
        user_home=home,
        network=False,
        install_network=True,
    )
    download = (
        "curl -fsSL https://raw.githubusercontent.com/"
        "NousResearch/hermes-agent/main/install.sh "
        "-o .hermes-install.sh"
    )
    assert sandbox.resolve_permission_profile(download).value == "user-install"
    assert sandbox.audit_command(download, workspace) is None
    assert (home / ".hermes").resolve() in sandbox._install_roots_for_command(
        download)

    installer = workspace / "hermes-install.sh"
    installer.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "mkdir -p \"$HOME/.hermes\" \"$HOME/.local/bin\"\n"
        "printf '0.19.0\\n' > \"$HOME/.hermes/version\"\n"
        "ln -sf /bin/true \"$HOME/.local/bin/hermes\"\n",
        encoding="utf-8",
    )
    result = run(RunCommandTool().execute(
        {
            "command": "sh hermes-install.sh",
            "sandbox_permission": "user-install",
        },
        ToolContext(workspace_root=workspace, sandbox=sandbox),
    ))
    assert not result.is_error
    assert (home / ".hermes" / "version").read_text(
        encoding="utf-8").strip() == "0.19.0"
    assert (home / ".local" / "bin" / "hermes").is_symlink()


def test_user_install_can_be_disabled_by_policy(tmp_path):
    sandbox = ExecutionSandbox(
        tmp_path,
        backend="static",
        allow_user_install=False,
    )
    violation = sandbox.audit_command(
        "uv tool install hermes-agent",
        tmp_path,
        permission_profile="user-install",
    )
    assert violation and "禁用用户级安装权限" in violation


def test_run_command_auto_escalates_install_to_user_profile(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class InstallSandbox:
        backend = "static"
        network = False
        allow_user_install = True
        install_network = True

        def audit_command(
            self,
            command: str,
            cwd: Path,
            *,
            permission_profile: str | None = None,
        ) -> str | None:
            del command, cwd
            if permission_profile == "user-install":
                return None
            return "网络已禁用，拒绝执行 curl"

        def prepare_shell(
            self,
            command: str,
            cwd: Path,
            *,
            permission_profile: str | None = None,
        ) -> SandboxedCommand:
            del command, cwd
            assert permission_profile == "user-install"
            return SandboxedCommand(
                argv=[sys.executable, "-c", "print('installed')"],
                env=dict(os.environ),
                backend="static",
                permission_profile=SandboxPermissionProfile.USER_INSTALL,
            )

    result = run(RunCommandTool().execute(
        {
            "command": (
                "curl -fsSL https://example.invalid/bootstrap "
                "-o install.sh"
            ),
        },
        ToolContext(
            workspace_root=workspace,
            sandbox=InstallSandbox(),  # type: ignore[arg-type]
            governance_decision=GovernanceDecision(
                allowed=True,
                policy_version=PolicyVersion(source="test"),
                sandbox_profile=SandboxProfile(
                    backend="static",
                    enabled=True,
                    network=False,
                    permission_profile="auto",
                ),
                audit=AuditCorrelation(correlation_id="trace-install"),
            ),
        ),
    ))

    assert not result.is_error
    assert result.data["sandbox_auto_user_install"] is True
    assert result.data["sandbox_permission_profile"] == "user-install"
    assert "sandbox_permission=\"user-install\"" in result.content


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


def test_seatbelt_and_bubblewrap_include_user_install_roots(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sandbox = ExecutionSandbox(
        tmp_path / "workspace",
        backend="static",
        user_home=home,
    )
    user_bin = (home / ".local" / "bin").resolve()
    profile = sandbox._seatbelt_profile(
        [],
        permission_profile=SandboxPermissionProfile.USER_INSTALL,
        network_allowed=True,
    )
    assert f'(subpath "{user_bin}")' in profile
    assert "(allow network-outbound)" in profile

    argv = sandbox._bubblewrap_argv(
        ["true"],
        tmp_path / "workspace",
        [],
        permission_profile=SandboxPermissionProfile.USER_INSTALL,
        network_allowed=True,
    )
    bind_index = argv.index(str(user_bin))
    assert argv[bind_index - 1:bind_index + 2] == [
        "--bind", str(user_bin), str(user_bin)]
    assert "--share-net" in argv


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


def test_build_execution_sandbox_reads_user_install_config(
        tmp_path, monkeypatch):
    parser = build_parser()
    args = parser.parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context", "--no-session"])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config = {
        "sandbox": {
            "backend": "static",
            "user_install": True,
            "install_network": False,
            "user_install_paths": [".hermes"],
        },
    }
    sandbox = build_execution_sandbox(args, config, tmp_path)
    assert sandbox is not None
    assert sandbox.install_network is False
    assert (home / ".hermes").resolve() in sandbox.user_install_roots


def test_rebind_uses_child_worktree_root(tmp_path):
    parent = static_sandbox(tmp_path / "main")
    (tmp_path / "main").mkdir()
    child_root = tmp_path / "child"
    child_root.mkdir()
    child = parent.rebind(child_root)
    assert child.backend == "static"
    assert child.workspace_root == child_root.resolve()
    assert child.audit_command("cat ../secret", child_root)
