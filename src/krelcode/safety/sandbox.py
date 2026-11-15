"""工具执行沙箱：静态命令审计 + 可选 OS 级进程隔离。

分层：
- static：所有平台可用，拦截明显的越界路径、敏感文件、shell 包装、命令替换
  和已知网络命令；它无法抵御任意解释器代码中的动态绕过，不能等同于内核沙箱；
- seatbelt：macOS sandbox-exec，工作区可写，系统运行时只读，用户 Home 不可读，
  网络默认关闭；外部 Skill 根目录以只读方式挂入；
- bubblewrap：Linux bwrap 提供同类的文件/网络隔离。

组装根默认 auto：优先探测 OS 后端，探测失败时退回 static。无论后端如何，
run_command / run_skill_script 都先执行同样的硬性审计；审批不能绕过拒绝规则。
"""

from __future__ import annotations

import functools
import glob as globlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .commands import (
    FIND_ACTION_OPTIONS, REDIRECTS, SEPARATORS, _tokenize,
)
from .environment import scrub_environment
from ..tools.secret_guard import classify_secret_path

SYSTEM_READ_ROOTS = (
    "/bin", "/sbin", "/usr", "/System", "/Library", "/opt",
    "/private/etc", "/dev", "/proc", "/lib", "/lib64", "/etc", "/sys",
)

NETWORK_PROGRAMS = {
    "curl", "wget", "fetch", "nc", "netcat", "telnet", "ssh", "scp", "sftp",
    "ftp", "ping", "dig", "nslookup", "host", "traceroute",
}
NETWORK_GIT_SUBCOMMANDS = {
    "clone", "fetch", "pull", "push", "ls-remote", "submodule",
}
PACKAGE_NETWORK_SUBCOMMANDS = {
    "install", "add", "update", "upgrade", "download", "sync",
}
PACKAGE_MANAGERS = {"pip", "pip3", "uv", "poetry", "npm", "yarn", "pnpm", "gem"}

_SHELL_COMMAND_FLAGS = {"-c", "/c", "--command", "-lc", "-ec", "-xc"}
_PATH_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(.*)$")
_SAFE_DEVICE_PATHS = {"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"}
_DYNAMIC_PATH_VARIABLE = re.compile(
    r"\$(?:\{)?(?:HOME|PWD|OLDPWD)(?:\})?", re.IGNORECASE)
_SHELL_VARIABLE = re.compile(r"\$(?:\{)?[A-Za-z_][A-Za-z0-9_]*")


class SandboxError(ValueError):
    """Sandbox 配置或准备失败。"""


@dataclass(frozen=True)
class SandboxedCommand:
    argv: List[str]
    env: dict[str, str]
    backend: str


class ExecutionSandbox:
    """为 shell/exec 子进程准备受限 argv、环境与网络策略。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        backend: str = "auto",
        network: bool = False,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.network = bool(network)
        requested = (backend or "auto").strip().lower()
        if requested not in {"auto", "static", "seatbelt", "bubblewrap"}:
            raise SandboxError(f"未知 sandbox backend: {backend!r}")
        self.requested_backend = requested
        self.backend = _detect_backend(requested)

    def rebind(self, workspace_root: Path) -> "ExecutionSandbox":
        """子代理 worktree 使用新根目录，保留后端与网络策略。"""
        return ExecutionSandbox(
            workspace_root, backend=self.requested_backend, network=self.network)

    def audit_command(self, command: str, cwd: Path) -> Optional[str]:
        """返回硬性违规原因；None 表示可交给隔离后端执行。"""
        return audit_command(
            command, Path(cwd), self.workspace_root,
            network_allowed=self.network,
        )

    def audit_exec(
        self, argv: Sequence[str], cwd: Path, *,
        allowed_read_roots: Iterable[Path] = (),
    ) -> Optional[str]:
        roots = [self.workspace_root] + [
            Path(p).resolve() for p in allowed_read_roots]
        if not any(_inside(Path(cwd), root) for root in roots):
            return f"cwd 越出 Sandbox 允许范围: {Path(cwd).resolve()}"
        return audit_argv_paths(
            argv, Path(cwd), roots, skip_program=True, network_allowed=self.network)

    def prepare_shell(self, command: str, cwd: Path) -> SandboxedCommand:
        return self._prepare(
            ["/bin/sh", "-c", command], Path(cwd), extra_read_roots=())

    def prepare_exec(
        self, argv: Sequence[str], cwd: Path, *,
        extra_read_roots: Iterable[Path] = (),
    ) -> SandboxedCommand:
        return self._prepare(
            list(argv), Path(cwd),
            extra_read_roots=[Path(p).resolve() for p in extra_read_roots])

    def _prepare(
        self, argv: List[str], cwd: Path,
        *, extra_read_roots: Sequence[Path],
    ) -> SandboxedCommand:
        cwd = cwd.resolve()
        allowed_roots = [self.workspace_root, *extra_read_roots]
        if not any(_inside(cwd, root) for root in allowed_roots):
            raise SandboxError(f"cwd 越出 Sandbox 允许范围: {cwd}")
        env = self._sandbox_env(cwd)
        if self.backend == "seatbelt":
            profile = self._seatbelt_profile(extra_read_roots)
            wrapped = [shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec",
                       "-p", profile, "--", *argv]
            return SandboxedCommand(wrapped, env, self.backend)
        if self.backend == "bubblewrap":
            wrapped = self._bubblewrap_argv(argv, cwd, extra_read_roots)
            return SandboxedCommand(wrapped, env, self.backend)
        return SandboxedCommand(argv, env, self.backend)

    def _sandbox_env(self, cwd: Path) -> dict[str, str]:
        base = scrub_environment(os.environ)
        runtime = self.workspace_root / ".krelcode" / "sandbox"
        home = runtime / "home"
        tmp = runtime / "tmp"
        cache = runtime / "cache"
        try:
            for path in (home, tmp, cache):
                path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SandboxError(f"无法创建 Sandbox 运行目录: {e}") from e
        env = dict(base)
        env.update({
            "HOME": str(home),
            "PWD": str(cwd),
            "TMPDIR": str(tmp),
            "TMP": str(tmp),
            "TEMP": str(tmp),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(cache),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "PIP_CACHE_DIR": str(cache / "pip"),
            "UV_CACHE_DIR": str(cache / "uv"),
            "npm_config_cache": str(cache / "npm"),
            "GOCACHE": str(cache / "go-build"),
            "CARGO_HOME": str(cache / "cargo"),
            "GRADLE_USER_HOME": str(cache / "gradle"),
        })
        return env

    def _seatbelt_profile(self, extra_read_roots: Sequence[Path]) -> str:
        runtime = self.workspace_root / ".krelcode" / "sandbox"
        read_roots = list(dict.fromkeys([
            *[root for root in SYSTEM_READ_ROOTS if Path(root).exists()],
            str(Path(sys.executable).resolve().parent),
            str(Path(sys.prefix).resolve()),
            str(Path(sys.base_prefix).resolve()),
            str(self.workspace_root),
            *[str(path) for path in extra_read_roots],
        ]))
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow signal)",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow file-read-metadata)",
        ]
        lines.append("(allow file-read* " + " ".join(
            f'(subpath "{_seatbelt_quote(p)}")' for p in read_roots) + ")")
        lines.append("(allow file-write* " + " ".join(
            f'(subpath "{_seatbelt_quote(p)}")'
            for p in (str(self.workspace_root), str(runtime))) + ")")
        lines.append('(allow file-write* (literal "/dev/null"))')
        if self.network:
            lines.append("(allow network-outbound)")
        return "\n".join(lines)

    def _bubblewrap_argv(
        self, argv: Sequence[str], cwd: Path, extra_read_roots: Sequence[Path],
    ) -> List[str]:
        bwrap = shutil.which("bwrap") or "bwrap"
        out = [
            bwrap, "--die-with-parent", "--new-session", "--unshare-all",
            "--cap-drop", "ALL",
        ]
        if self.network:
            out.append("--share-net")
        out += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        bind_roots: List[Path] = []
        for raw in SYSTEM_READ_ROOTS:
            root = Path(raw)
            if str(root) in {"/dev", "/proc"}:
                continue  # 已由 --dev/--proc 创建，避免重复 mount
            if root.exists() and not any(_inside(root, seen) for seen in bind_roots):
                bind_roots.append(root)
                out += ["--ro-bind", str(root), str(root)]
        prefix = Path(sys.prefix).resolve()
        if not any(_inside(prefix, seen) for seen in bind_roots):
            bind_roots.append(prefix)
            out += ["--ro-bind", str(prefix), str(prefix)]
        out += ["--bind", str(self.workspace_root), str(self.workspace_root)]
        for root in extra_read_roots:
            if _inside(root, self.workspace_root) or any(
                    _inside(root, seen) for seen in bind_roots):
                continue
            out += ["--ro-bind", str(root), str(root)]
        out += ["--chdir", str(cwd), "--", *argv]
        return out


@functools.lru_cache(maxsize=16)
def _detect_backend(requested: str, platform: Optional[str] = None) -> str:
    platform = platform or sys.platform
    if requested == "static":
        return "static"
    if requested in {"auto", "seatbelt"}:
        executable = shutil.which("sandbox-exec")
        if platform == "darwin" and executable and _probe_seatbelt(executable):
            return "seatbelt"
        if requested == "seatbelt":
            raise SandboxError("seatbelt 不可用：仅支持已允许 sandbox-exec 的 macOS")
    if requested in {"auto", "bubblewrap"}:
        executable = shutil.which("bwrap")
        if platform.startswith("linux") and executable and _probe_bubblewrap(executable):
            return "bubblewrap"
        if requested == "bubblewrap":
            raise SandboxError("bubblewrap 不可用：需要 Linux、bwrap 和可用的用户命名空间")
    return "static"


def _probe_seatbelt(executable: str) -> bool:
    try:
        completed = subprocess.run(
            [executable, "-p", "(version 1)\n(allow default)\n", "--", "/bin/true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=scrub_environment(os.environ),
            timeout=3,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _probe_bubblewrap(executable: str) -> bool:
    try:
        completed = subprocess.run(
            [executable, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
             "--", "/bin/true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=scrub_environment(os.environ),
            timeout=3,
            check=False,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def audit_command(
    command: str, cwd: Path, workspace_root: Path,
    *, network_allowed: bool = False,
) -> Optional[str]:
    """静态命令审计；用于拦截明显越界、敏感路径和 shell 包装绕过。"""
    if "\x00" in command:
        return "命令包含 NUL 字符"
    try:
        tokens = _tokenize(command)
    except ValueError as e:
        return f"命令无法解析: {e}"
    roots = [Path(workspace_root).resolve()]
    violation = audit_argv_paths(
        tokens, cwd, roots, skip_program=False, network_allowed=network_allowed,
        shell_context=True)
    if violation:
        return violation

    if "$HOME" in command or "${HOME}" in command:
        return "禁止通过 HOME 展开访问用户主目录"
    if "`" in command or "$(" in command:
        return "禁止命令替换/反引号（可绕过路径审计；请拆分为明确命令）"

    # 递归审计 shell -c/eval、env、find -exec、xargs、rg --pre 等包装命令。
    for nested in _embedded_commands(tokens):
        nested_command = (
            nested[0] if len(nested) == 1
            else shlex.join(nested)
        )
        nested_violation = audit_command(
            nested_command, cwd, workspace_root, network_allowed=network_allowed)
        if nested_violation:
            return f"嵌套命令违规: {nested_violation}"
    return None


def audit_argv_paths(
    tokens: Sequence[str], cwd: Path, allowed_roots: Sequence[Path], *,
    skip_program: bool, network_allowed: bool = False,
    shell_context: bool = False,
) -> Optional[str]:
    roots = [Path(root).resolve() for root in allowed_roots]
    cwd = Path(cwd).resolve()
    if not network_allowed:
        network = _network_violation(tokens)
        if network:
            return network
    for index, raw in enumerate(tokens):
        if not raw or raw in SEPARATORS or raw in REDIRECTS:
            continue
        candidates = [raw]
        assigned = _PATH_ASSIGNMENT.match(raw)
        if assigned:
            candidates = [assigned.group(1)]
        for token in candidates:
            if _is_url(token) or token in _SAFE_DEVICE_PATHS:
                continue
            if token.startswith("-") and "/" not in token and "=" not in token:
                continue
            if token.startswith("-") and "=" in token:
                token = token.split("=", 1)[1]
            if shell_context:
                if _DYNAMIC_PATH_VARIABLE.search(token):
                    return "禁止使用 HOME/PWD/OLDPWD 变量构造访问路径"
                if _SHELL_VARIABLE.search(token) and "/" in token:
                    return "禁止在路径参数中使用 shell 变量展开（可能绕过路径审计）"
            glob_violation = _audit_expanded_globs(token, cwd, roots)
            if glob_violation:
                return glob_violation
            sensitive_token = _looks_like_sensitive_path_token(
                token, cwd, roots)
            if not sensitive_token and not _looks_like_path(token, cwd):
                continue
            resolved = _resolve_token(token, cwd)
            if resolved is None:
                continue
            if _contains_sensitive_path(resolved, roots):
                return f"禁止访问敏感路径: {resolved}"
            if any(_inside(resolved, root) for root in roots):
                if _is_hardlinked_file(resolved):
                    return f"禁止通过硬链接访问工作区文件: {resolved}"
                continue
            if index == 0 and (_is_system_executable(resolved)
                               or not _has_path_separator(token)):
                continue
            return f"路径越出 Sandbox 允许范围: {resolved}"
    return None


def _network_violation(tokens: Sequence[str]) -> Optional[str]:
    segment: List[str] = []
    segments: List[List[str]] = []
    for token in tokens:
        if token in SEPARATORS or token in REDIRECTS:
            if segment:
                segments.append(segment)
                segment = []
        else:
            segment.append(token)
    if segment:
        segments.append(segment)
    for args in segments:
        if not args:
            continue
        original_program = Path(args[0]).name.lower()
        if original_program == "env":
            nested_env = _unwrap_env(args[1:])
            if nested_env:
                nested = _network_violation(nested_env)
                if nested:
                    return nested
        program_index = _effective_program_index(args)
        program = Path(args[program_index]).name.lower()
        program_args = args[program_index + 1:]
        subcommand = program_args[0].lower() if program_args else ""
        if program in NETWORK_PROGRAMS:
            return f"网络已禁用，拒绝执行 {program}"
        if program == "openssl" and subcommand == "s_client":
            return "网络已禁用，拒绝执行 openssl s_client"
        if program == "git":
            git_subcommand = _git_subcommand(program_args)
            if git_subcommand in NETWORK_GIT_SUBCOMMANDS:
                return f"网络已禁用，拒绝执行 git {git_subcommand}"
        if program in PACKAGE_MANAGERS and any(
                arg.lower() in PACKAGE_NETWORK_SUBCOMMANDS for arg in program_args):
            return f"网络已禁用，拒绝执行 {program} 网络操作"
        for nested in _embedded_commands(args):
            violation = _network_violation(nested)
            if violation:
                return violation
    return None


def _effective_program_index(args: Sequence[str]) -> int:
    """跳过 env/command 包装和环境赋值，识别实际网络程序。"""
    index = 0
    while index < len(args):
        program = Path(args[index]).name.lower()
        if program not in {"env", "command", "exec"}:
            return index
        index += 1
        while index < len(args):
            token = args[index]
            if _PATH_ASSIGNMENT.match(token) or (token.startswith("-") and token != "-"):
                index += 1
                continue
            break
    return max(0, len(args) - 1)


def _git_subcommand(args: Sequence[str]) -> str:
    """跳过 `git -C path`/`git -c k=v` 等全局选项，返回实际子命令。"""
    options_with_value = {
        "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
        "--config-env", "--super-prefix",
    }
    index = 0
    while index < len(args):
        token = args[index]
        if token in options_with_value:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token.lower()
    return ""


def _is_shell_command_flag(token: str) -> bool:
    if token in _SHELL_COMMAND_FLAGS:
        return True
    return token.startswith("-") and not token.startswith("--") and "c" in token[1:]


def _resolve_token(token: str, cwd: Path) -> Optional[Path]:
    if "\x00" in token:
        return None
    if token.startswith("~"):
        return Path(token).expanduser().resolve()
    path = Path(token)
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.resolve()
    except (OSError, ValueError):
        return None


def _looks_like_path(token: str, cwd: Path) -> bool:
    if token in {".", ".."} or token.startswith(("/", "~", "./", "../")):
        return True
    if "/" not in token:
        # 基名也可能指向工作区内的硬链接文件，存在时必须进入后续身份检查。
        try:
            return (cwd / token).exists()
        except OSError:
            return False
    resolved = _resolve_token(token, cwd)
    if resolved is None:
        return False
    # 实际存在，或规范化后逃出当前目录，才按文件路径处理；避免把
    # `feature/foo` 一类普通参数/正则误判为路径。
    return resolved.exists() or not _inside(resolved, cwd)


def _looks_like_sensitive_path_token(
    token: str, cwd: Path, allowed_roots: Sequence[Path],
) -> bool:
    """即使目标尚不存在，也使用统一凭据分类器拦截敏感路径。"""
    resolved = _resolve_token(token, cwd)
    return resolved is not None and _contains_sensitive_path(
        resolved, allowed_roots)


def _contains_sensitive_path(path: Path, allowed_roots: Sequence[Path]) -> bool:
    return any(
        classify_secret_path(path, root) is not None
        for root in allowed_roots
    )


def _is_hardlinked_file(path: Path) -> bool:
    try:
        value = path.stat()
        return stat.S_ISREG(value.st_mode) and value.st_nlink != 1
    except OSError:
        return False


def _audit_expanded_globs(
    token: str, cwd: Path, allowed_roots: Sequence[Path],
) -> Optional[str]:
    """审计 shell 在执行时才会展开的 glob，防止 `cat *` 绕过敏感路径规则。"""
    if not any(char in token for char in "*?["):
        return None
    pattern = Path(token).expanduser()
    if not pattern.is_absolute():
        pattern = cwd / pattern
    try:
        matches = globlib.glob(str(pattern), recursive=True, include_hidden=True)
    except (OSError, ValueError, re.error):
        return f"无法安全展开 glob: {token}"
    for raw_match in matches:
        try:
            resolved = Path(raw_match).resolve(strict=True)
        except (OSError, ValueError):
            continue
        if _contains_sensitive_path(resolved, allowed_roots):
            return f"glob 命中敏感路径: {resolved}"
        if not any(_inside(resolved, root) for root in allowed_roots):
            return f"glob 展开越出 Sandbox 允许范围: {resolved}"
        if _is_hardlinked_file(resolved):
            return f"glob 命中硬链接文件: {resolved}"
    return None


def _split_segments(tokens: Sequence[str]) -> List[List[str]]:
    segments: List[List[str]] = []
    current: List[str] = []
    for token in tokens:
        if token in SEPARATORS or token in REDIRECTS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _unwrap_env(args: Sequence[str]) -> Optional[List[str]]:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg in {"-S", "--split-string"}:
            if index + 1 >= len(args):
                return None
            try:
                return _tokenize(args[index + 1])
            except ValueError:
                return [args[index + 1]]
        if arg.startswith("--split-string="):
            try:
                return _tokenize(arg.split("=", 1)[1])
            except ValueError:
                return [arg.split("=", 1)[1]]
        if arg in {"-u", "--unset", "-C", "--chdir"}:
            index += 2
            continue
        if arg.startswith(("--unset=", "--chdir=")):
            index += 1
            continue
        if arg.startswith("-") and arg != "-":
            index += 1
            continue
        break
    while index < len(args) and _PATH_ASSIGNMENT.match(args[index]):
        index += 1
    return list(args[index:]) if index < len(args) else None


def _unwrap_runtime_wrapper(
    program: str, args: Sequence[str],
) -> Optional[List[str]]:
    index = 0
    if program == "command":
        while index < len(args) and args[index] in {"-p", "--"}:
            index += 1
        return list(args[index:]) if index < len(args) else None
    if program == "exec":
        while index < len(args) and args[index] == "--":
            index += 1
        return list(args[index:]) if index < len(args) else None
    if program == "nohup":
        while index < len(args) and args[index] == "--":
            index += 1
        return list(args[index:]) if index < len(args) else None
    if program == "nice":
        while index < len(args):
            arg = args[index]
            if arg in {"-n", "--adjustment"}:
                index += 2
                continue
            if arg.startswith(("--adjustment=", "-")) and arg != "-":
                index += 1
                continue
            break
        return list(args[index:]) if index < len(args) else None
    if program == "time":
        while index < len(args) and args[index] in {
                "-p", "-v", "--verbose", "--"}:
            index += 1
        return list(args[index:]) if index < len(args) else None
    if program == "timeout":
        while index < len(args) and args[index].startswith("-"):
            if args[index] in {"-s", "--signal", "-k", "--kill-after"}:
                index += 2
            else:
                index += 1
        if index >= len(args):
            return None
        index += 1  # duration
        return list(args[index:]) if index < len(args) else None
    if program == "stdbuf":
        while index < len(args) and args[index].startswith("-"):
            if args[index] in {"-i", "-o", "-e"}:
                index += 2
            else:
                index += 1
        return list(args[index:]) if index < len(args) else None
    return list(args)


def _unwrap_xargs(args: Sequence[str]) -> Optional[List[str]]:
    value_options = {
        "-I", "-n", "-P", "-L", "-s", "-E", "-d", "--delimiter",
        "--max-args", "--max-procs", "--max-lines", "--max-chars",
        "--eof", "--replace", "--arg-file",
    }
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg in value_options:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        break
    return list(args[index:]) if index < len(args) else None


def _option_value(
    args: Sequence[str], option: str,
) -> Optional[str]:
    for index, arg in enumerate(args):
        if arg == option and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(f"{option}="):
            return arg.split("=", 1)[1]
    return None


def _embedded_commands(tokens: Sequence[str]) -> List[List[str]]:
    embedded: List[List[str]] = []
    for args in _split_segments(tokens):
        if not args:
            continue
        program = Path(args[0]).name.lower()
        program_args = args[1:]
        if program in {"sh", "bash", "zsh", "dash", "ksh"}:
            for index, arg in enumerate(program_args):
                if _is_shell_command_flag(arg) and index + 1 < len(program_args):
                    embedded.append([program_args[index + 1]])
                    break
        elif program == "eval" and program_args:
            embedded.append([program_args[0]])
        elif program == "env":
            nested = _unwrap_env(program_args)
            if nested:
                embedded.append(nested)
        elif program in {
                "command", "exec", "nohup", "nice", "time", "timeout", "stdbuf"}:
            nested = _unwrap_runtime_wrapper(program, program_args)
            if nested:
                embedded.append(nested)
        elif program == "find":
            for index, arg in enumerate(program_args):
                if arg in FIND_ACTION_OPTIONS and arg not in {
                        "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}:
                    if program_args[index + 1:]:
                        embedded.append(program_args[index + 1:])
                    break
        elif program == "xargs":
            nested = _unwrap_xargs(program_args)
            if nested:
                embedded.append(nested)
        elif program == "sort":
            value = _option_value(program_args, "--compress-program")
            if value:
                try:
                    embedded.append(_tokenize(value))
                except ValueError:
                    embedded.append([value])
        elif program == "rg":
            value = _option_value(program_args, "--pre")
            if value:
                try:
                    embedded.append(_tokenize(value))
                except ValueError:
                    embedded.append([value])
    return embedded


def _is_system_executable(path: Path) -> bool:
    return any(_inside(path, Path(root).resolve())
               for root in SYSTEM_READ_ROOTS if Path(root).exists())


def _has_path_separator(token: str) -> bool:
    return "/" in token or token.startswith("~")


def _is_url(token: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", token))


def _inside(path: Path, root: Path) -> bool:
    try:
        path = path.resolve()
        root = root.resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def _seatbelt_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
