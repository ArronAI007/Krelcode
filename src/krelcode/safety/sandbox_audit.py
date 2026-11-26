"""Static command and path audit for the execution Sandbox.

This module is provider- and process-neutral. It classifies a concrete shell
command before execution, including user-level installation scopes and nested
shell wrappers. OS-level confinement remains in ``safety.sandbox``.
"""

from __future__ import annotations

import glob as globlib
import ntpath
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from .commands import (
    FIND_ACTION_OPTIONS,
    REDIRECTS,
    SEPARATORS,
    _tokenize,
    _tokenize_cmd,
)
from ..core.secret_guard import classify_secret_path

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
INSTALL_NETWORK_GIT_SUBCOMMANDS = {"clone", "submodule"}
PACKAGE_NETWORK_SUBCOMMANDS = {
    "install", "add", "update", "upgrade", "download", "sync",
}
PACKAGE_MANAGERS = {"pip", "pip3", "uv", "poetry", "npm", "yarn", "pnpm", "gem"}
USER_INSTALL_PROGRAMS = {"install", "pipx", "brew", "npx"}
USER_INSTALL_NAME_MARKERS = {
    "install", "installer", "setup", "bootstrap", "postinstall",
}
DEFAULT_USER_INSTALL_DIRS = (
    ".local/bin",
    ".local/share",
    ".local/lib",
    ".local/state",
)

_SHELL_COMMAND_FLAGS = {"-c", "/c", "--command", "-lc", "-ec", "-xc"}
_PATH_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=(.*)$")
_SAFE_DEVICE_PATHS = {"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"}
_PRIVILEGE_PROGRAMS = {"sudo", "su", "doas", "pkexec"}
_DYNAMIC_PATH_VARIABLE = re.compile(
    r"\$(?:\{)?(?:HOME|PWD|OLDPWD)(?:\})?", re.IGNORECASE)
_SHELL_VARIABLE = re.compile(r"\$(?:\{)?[A-Za-z_][A-Za-z0-9_]*")
_CMD_VARIABLE = re.compile(r"%[A-Za-z_][A-Za-z0-9_]*%")
_CMD_DELAYED_VARIABLE = re.compile(r"![A-Za-z_][A-Za-z0-9_]*!")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_WINDOWS_OPTION = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]*$")


def _validated_user_install_roots(
    user_home: Path,
    configured: Iterable[Path] = (),
) -> tuple[Path, ...]:
    """Return only user-owned installation roots below the real home."""
    home = Path(user_home).expanduser().resolve()
    candidates = [
        *(home / relative for relative in DEFAULT_USER_INSTALL_DIRS),
        *[
            Path(path).expanduser()
            if Path(path).expanduser().is_absolute()
            else home / Path(path)
            for path in configured
        ],
    ]
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if resolved == home or not _inside(resolved, home):
            continue
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def detect_user_install_command(
    command: str,
    user_home: Path,
    user_install_roots: Sequence[Path],
    *,
    shell_dialect: str = "posix",
) -> bool:
    """Detect commands that intentionally target a supported user install root."""
    if not user_install_roots:
        return False
    windows_paths = shell_dialect == "cmd"
    try:
        tokens = (
            _tokenize_cmd(command)
            if windows_paths
            else _tokenize(command)
        )
    except ValueError:
        lowered = command.lower()
        return any(marker in lowered for marker in USER_INSTALL_NAME_MARKERS)
    return _tokens_request_user_install(
        tokens,
        Path(user_home),
        [Path(root) for root in user_install_roots],
        windows_paths=windows_paths,
    )


def _tokens_request_user_install(
    tokens: Sequence[str],
    user_home: Path,
    user_install_roots: Sequence[Path],
    *,
    windows_paths: bool,
) -> bool:
    for token in tokens:
        if token in SEPARATORS or token in REDIRECTS:
            continue
        expanded = _expand_home_path_token(token, user_home)
        if expanded is not None and any(
            _inside(Path(expanded).resolve(), root)
            for root in user_install_roots
        ):
            return True

    for args in _split_segments(tokens):
        if not args:
            continue
        program = _program_name(args[0])
        program_args = [arg.lower() for arg in args[1:]]
        if program in USER_INSTALL_PROGRAMS:
            return True
        if program in PACKAGE_MANAGERS and any(
                arg in PACKAGE_NETWORK_SUBCOMMANDS for arg in program_args):
            return True
        if program in {"python", "python3"}:
            if _argument_subcommand(
                    program_args, "pip") in PACKAGE_NETWORK_SUBCOMMANDS:
                return True
        if (
            program == "git"
            and _git_subcommand(args[1:]) in INSTALL_NETWORK_GIT_SUBCOMMANDS
        ):
            return True
        if any(
            marker in Path(item).name
            for item in args
            for marker in USER_INSTALL_NAME_MARKERS
        ):
            return True
        for nested in _embedded_commands(args):
            if _tokens_request_user_install(
                    nested, user_home, user_install_roots,
                    windows_paths=windows_paths):
                return True
    return False


def _argument_subcommand(args: Sequence[str], target: str) -> str:
    try:
        index = args.index(target)
    except ValueError:
        return ""
    return args[index + 1] if index + 1 < len(args) else ""


def _is_install_network_command(
    command: str,
    *,
    shell_dialect: str,
) -> bool:
    windows_paths = shell_dialect == "cmd"
    try:
        tokens = _tokenize_cmd(command) if windows_paths else _tokenize(command)
    except ValueError:
        return False
    for args in _split_segments(tokens):
        if not args:
            continue
        program = _program_name(args[0])
        lowered_args = [arg.lower() for arg in args[1:]]
        if any(
            marker in Path(item).name
            for item in args
            for marker in USER_INSTALL_NAME_MARKERS
        ):
            return True
        if program in NETWORK_PROGRAMS or program in USER_INSTALL_PROGRAMS:
            return True
        if program in PACKAGE_MANAGERS and any(
                arg in PACKAGE_NETWORK_SUBCOMMANDS for arg in lowered_args):
            return True
        if program in {"python", "python3"}:
            subcommand = _argument_subcommand(lowered_args, "pip")
            if subcommand in PACKAGE_NETWORK_SUBCOMMANDS:
                return True
        if (
            program == "git"
            and _git_subcommand(args[1:]) in INSTALL_NETWORK_GIT_SUBCOMMANDS
        ):
            return True
        for nested in _embedded_commands(args):
            if _is_install_network_command(
                    _join_tokens(nested, shell_dialect=shell_dialect),
                    shell_dialect=shell_dialect):
                return True
    return False


def _expand_home_path_token(
    token: str,
    user_home: Optional[Path],
) -> Optional[str]:
    """Expand only leading HOME forms; other dynamic variables remain audited."""
    if user_home is None:
        return token
    home = str(Path(user_home).expanduser().resolve())
    normalized = token
    for prefix in ("${HOME}", "$HOME", "%USERPROFILE%"):
        if normalized == prefix:
            return home
        if normalized.lower().startswith(prefix.lower() + "/"):
            return home + normalized[len(prefix):]
        if normalized.lower().startswith(prefix.lower() + "\\"):
            return home + normalized[len(prefix):]
    if normalized == "~":
        return home
    if normalized.startswith("~/"):
        return home + normalized[1:]
    return token


def audit_command(
    command: str,
    cwd: Path,
    workspace_root: Path,
    *,
    network_allowed: bool = False,
    shell_dialect: str = "posix",
    user_home: Optional[Path] = None,
    extra_write_roots: Sequence[Path] = (),
) -> Optional[str]:
    """Static command audit; returns a hard-deny reason or ``None``."""
    if "\x00" in command:
        return "命令包含 NUL 字符"
    windows_paths = shell_dialect == "cmd"
    if windows_paths:
        preflight = _audit_cmd_preflight(command)
        if preflight:
            return preflight
    try:
        tokens = _tokenize_cmd(command) if windows_paths else _tokenize(command)
    except ValueError as error:
        return f"命令无法解析: {error}"
    privilege = _privilege_escalation(tokens)
    if privilege:
        return f"禁止提权命令: {privilege}"
    roots = [
        Path(workspace_root).resolve(),
        *[Path(root).resolve() for root in extra_write_roots],
    ]
    violation = audit_argv_paths(
        tokens, cwd, roots, skip_program=False,
        network_allowed=network_allowed, shell_context=True,
        windows_paths=windows_paths, user_home=user_home)
    if violation:
        return violation

    if user_home is None and ("$HOME" in command or "${HOME}" in command):
        return "禁止通过 HOME 展开访问用户主目录"
    if "`" in command or "$(" in command:
        return "禁止命令替换/反引号（可绕过路径审计；请拆分为明确命令）"

    for nested in _embedded_commands(tokens):
        nested_command = (
            nested[0] if len(nested) == 1
            else _join_tokens(nested, shell_dialect=shell_dialect)
        )
        nested_violation = audit_command(
            nested_command,
            cwd,
            workspace_root,
            network_allowed=network_allowed,
            shell_dialect=shell_dialect,
            user_home=user_home,
            extra_write_roots=extra_write_roots,
        )
        if nested_violation:
            return f"嵌套命令违规: {nested_violation}"
    return None


def _privilege_escalation(tokens: Sequence[str]) -> str:
    for args in _split_segments(tokens):
        if args and _program_name(args[0]) in _PRIVILEGE_PROGRAMS:
            return _program_name(args[0])
        for nested in _embedded_commands(args):
            privilege = _privilege_escalation(nested)
            if privilege:
                return privilege
    return ""


def _audit_cmd_preflight(command: str) -> Optional[str]:
    if _CMD_VARIABLE.search(command):
        return "禁止使用 cmd 环境变量展开构造路径（可能绕过路径审计）"
    if _CMD_DELAYED_VARIABLE.search(command):
        return "禁止使用 cmd 延迟展开变量（可能绕过路径审计）"
    if _powershell_encoded_command(command):
        return "禁止执行编码后的 PowerShell 命令（无法静态审计）"
    return None


def _powershell_encoded_command(command: str) -> bool:
    lowered = command.lower()
    if "powershell" not in lowered and "pwsh" not in lowered:
        return False
    return bool(re.search(
        r"(?:^|\s)-(?:e|ec|enc|enco|encod|encode|encoded|encodedcommand)"
        r"(?:\s|:|$)",
        lowered,
    ))


def _join_tokens(tokens: Sequence[str], *, shell_dialect: str) -> str:
    if shell_dialect == "cmd":
        return subprocess.list2cmdline(list(tokens))
    return shlex.join(tokens)


def audit_argv_paths(
    tokens: Sequence[str],
    cwd: Path,
    allowed_roots: Sequence[Path],
    *,
    skip_program: bool,
    network_allowed: bool = False,
    shell_context: bool = False,
    windows_paths: bool = False,
    user_home: Optional[Path] = None,
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
        if skip_program and index == 0:
            continue
        candidates = [raw]
        assigned = _PATH_ASSIGNMENT.match(raw)
        if assigned:
            candidates = [assigned.group(1)]
        for token in candidates:
            if shell_context:
                expanded_token = _expand_home_path_token(token, user_home)
                if expanded_token is not None:
                    token = expanded_token
                if _DYNAMIC_PATH_VARIABLE.search(token):
                    return "禁止使用 HOME/PWD/OLDPWD 变量构造访问路径"
                if windows_paths and re.search(
                        r"%USERPROFILE%", token, re.IGNORECASE):
                    return "禁止使用 USERPROFILE 变量构造访问路径"
            if _is_url(token) or token in _SAFE_DEVICE_PATHS:
                continue
            if windows_paths and _WINDOWS_OPTION.match(token):
                continue
            if token.startswith("-") and "/" not in token and "=" not in token:
                continue
            if token.startswith("-") and "=" in token:
                token = token.split("=", 1)[1]
            if shell_context:
                if _SHELL_VARIABLE.search(token) and "/" in token:
                    return "禁止在路径参数中使用 shell 变量展开（可能绕过路径审计）"
                if (
                    windows_paths
                    and _CMD_VARIABLE.search(token)
                    and user_home is None
                ):
                    return "禁止使用 cmd 环境变量展开构造路径（可能绕过路径审计）"
                if windows_paths and _CMD_DELAYED_VARIABLE.search(token):
                    return "禁止使用 cmd 延迟展开变量（可能绕过路径审计）"
            if (
                windows_paths
                and _WINDOWS_ABSOLUTE_PATH.match(token)
                and os.name != "nt"
            ):
                return f"路径越出 Sandbox 允许范围: {token}"
            glob_violation = _audit_expanded_globs(
                token, cwd, roots, windows_paths=windows_paths)
            if glob_violation:
                return glob_violation
            sensitive_token = _looks_like_sensitive_path_token(
                token, cwd, roots, windows_paths=windows_paths)
            if not sensitive_token and not _looks_like_path(
                    token, cwd, windows_paths=windows_paths):
                continue
            resolved = _resolve_token(
                token, cwd, windows_paths=windows_paths)
            if resolved is None:
                continue
            if _contains_sensitive_path(resolved, roots):
                return f"禁止访问敏感路径: {resolved}"
            if any(_inside(resolved, root) for root in roots):
                if _is_hardlinked_file(resolved):
                    return f"禁止通过硬链接访问工作区文件: {resolved}"
                continue
            if index == 0 and (
                _is_system_executable(resolved)
                or not _has_path_separator(token)
            ):
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
        original_program = _program_name(args[0])
        if original_program == "env":
            nested_env = _unwrap_env(args[1:])
            if nested_env:
                nested_violation = _network_violation(nested_env)
                if nested_violation:
                    return nested_violation
        program_index = _effective_program_index(args)
        program = _program_name(args[program_index])
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
                arg.lower() in PACKAGE_NETWORK_SUBCOMMANDS
                for arg in program_args):
            return f"网络已禁用，拒绝执行 {program} 网络操作"
        for nested_tokens in _embedded_commands(args):
            violation = _network_violation(nested_tokens)
            if violation:
                return violation
    return None


def _effective_program_index(args: Sequence[str]) -> int:
    """Skip env/command wrappers and environment assignments."""
    index = 0
    while index < len(args):
        program = _program_name(args[index])
        if program not in {"env", "command", "exec"}:
            return index
        index += 1
        while index < len(args):
            token = args[index]
            if (
                _PATH_ASSIGNMENT.match(token)
                or (token.startswith("-") and token != "-")
            ):
                index += 1
                continue
            break
    return max(0, len(args) - 1)


def _git_subcommand(args: Sequence[str]) -> str:
    """Skip git global options and return the actual subcommand."""
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
    return (
        token.startswith("-")
        and not token.startswith("--")
        and "c" in token[1:]
    )


def _resolve_token(
    token: str,
    cwd: Path,
    *,
    windows_paths: bool = False,
) -> Optional[Path]:
    if "\x00" in token:
        return None
    if token.startswith("~"):
        return Path(token).expanduser().resolve()
    if windows_paths and not _WINDOWS_ABSOLUTE_PATH.match(token):
        token = token.replace("\\", "/")
    path = Path(token)
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.resolve()
    except (OSError, ValueError):
        return None


def _looks_like_path(
    token: str,
    cwd: Path,
    *,
    windows_paths: bool = False,
) -> bool:
    if token in {".", ".."} or token.startswith(
            ("/", "~", "./", "../", ".\\", "..\\")):
        return True
    if windows_paths and _WINDOWS_ABSOLUTE_PATH.match(token):
        return True
    if "/" not in token and "\\" not in token:
        try:
            return (cwd / token).exists()
        except OSError:
            return False
    resolved = _resolve_token(token, cwd, windows_paths=windows_paths)
    if resolved is None:
        return False
    return resolved.exists() or not _inside(resolved, cwd)


def _looks_like_sensitive_path_token(
    token: str,
    cwd: Path,
    allowed_roots: Sequence[Path],
    *,
    windows_paths: bool = False,
) -> bool:
    resolved = _resolve_token(token, cwd, windows_paths=windows_paths)
    return resolved is not None and _contains_sensitive_path(
        resolved, allowed_roots)


def _contains_sensitive_path(
    path: Path,
    allowed_roots: Sequence[Path],
) -> bool:
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
    token: str,
    cwd: Path,
    allowed_roots: Sequence[Path],
    *,
    windows_paths: bool = False,
) -> Optional[str]:
    if not any(char in token for char in "*?["):
        return None
    if windows_paths and not _WINDOWS_ABSOLUTE_PATH.match(token):
        token = token.replace("\\", "/")
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
    program: str,
    args: Sequence[str],
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
        index += 1
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


def _option_value(args: Sequence[str], option: str) -> Optional[str]:
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
        program = _program_name(args[0])
        program_args = args[1:]
        if program in {"sh", "bash", "zsh", "dash", "ksh"}:
            for index, arg in enumerate(program_args):
                if _is_shell_command_flag(arg) and index + 1 < len(program_args):
                    embedded.append([program_args[index + 1]])
                    break
        elif program == "cmd":
            for index, arg in enumerate(program_args):
                if arg.lower() in {"/c", "/k"} and index + 1 < len(program_args):
                    try:
                        embedded.append(_tokenize_cmd(program_args[index + 1]))
                    except ValueError:
                        embedded.append([program_args[index + 1]])
                    break
        elif program in {"powershell", "pwsh"}:
            for index, arg in enumerate(program_args):
                nested = _powershell_command_argument(program_args, index)
                if nested is None:
                    continue
                try:
                    embedded.append(_tokenize_cmd(
                        nested, semicolon_as_separator=True))
                except ValueError:
                    embedded.append([nested])
                break
        elif program == "eval" and program_args:
            embedded.append([program_args[0]])
        elif program == "env":
            nested_env_tokens = _unwrap_env(program_args)
            if nested_env_tokens:
                embedded.append(nested_env_tokens)
        elif program in {
                "command", "exec", "nohup", "nice", "time", "timeout", "stdbuf"}:
            wrapped_tokens = _unwrap_runtime_wrapper(program, program_args)
            if wrapped_tokens:
                embedded.append(wrapped_tokens)
        elif program == "find":
            for index, arg in enumerate(program_args):
                if arg in FIND_ACTION_OPTIONS and arg not in {
                        "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}:
                    if program_args[index + 1:]:
                        embedded.append(program_args[index + 1:])
                    break
        elif program == "xargs":
            xargs_tokens = _unwrap_xargs(program_args)
            if xargs_tokens:
                embedded.append(xargs_tokens)
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
    return any(
        _inside(path, Path(root).resolve())
        for root in SYSTEM_READ_ROOTS
        if Path(root).exists()
    )


def _has_path_separator(token: str) -> bool:
    return "/" in token or "\\" in token or token.startswith("~")


def _program_name(token: str) -> str:
    """Normalize POSIX/Windows executable names and strip a Windows suffix."""
    base = ntpath.basename(str(token).replace("/", "\\")).lower()
    for suffix in (".exe", ".com", ".cmd", ".bat"):
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return base


def _powershell_command_argument(
    args: Sequence[str],
    index: int,
) -> Optional[str]:
    raw = args[index]
    lowered = raw.lower().strip('"')
    for prefix in ("-command", "-c"):
        if lowered.startswith(prefix + ":"):
            return raw.split(":", 1)[1].strip('"') or None
        exact = lowered == prefix
        abbreviated = (
            prefix == "-command"
            and len(lowered) >= 3
            and prefix.startswith(lowered)
        )
        if (exact or abbreviated) and index + 1 < len(args):
            return args[index + 1]
    return None


def _is_url(token: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", token))


def _inside(path: Path, root: Path) -> bool:
    try:
        path = path.resolve()
        root = root.resolve()
    except OSError:
        return False
    return path == root or root in path.parents


__all__ = [
    "DEFAULT_USER_INSTALL_DIRS",
    "SYSTEM_READ_ROOTS",
    "audit_argv_paths",
    "audit_command",
    "detect_user_install_command",
]
