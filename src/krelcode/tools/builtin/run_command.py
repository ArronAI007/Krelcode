"""run_command：在工作区内执行 shell 命令。

安全语义（Step 4 版）：
- cwd 限定在工作区内（默认工作区根）
- 独立进程组：超时用 SIGKILL 整组击杀，不留孤儿进程
- stdout+stderr 合并回传，带 exit code；输出头尾截断
- 危险命令过滤/审批属于 Step 6（ApprovalPolicy 已在循环层就位，
  本工具只负责执行语义）
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import signal
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Dict

from ..base import ToolContext, ToolResult
from ..fsutils import truncate_middle
from ..paths import PathOutsideWorkspaceError, resolve_in_workspace

MAX_OUTPUT_CHARS = 30_000
DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0

# 敏感环境变量：子进程执行时从环境中剥离，避免 coding agent 通过
# `run_command("env")` 等顺手读到 LLM API key 或其它 secret。
# 采用黑名单（按名字匹配），而非白名单：保留 PATH/HOME 等正常变量，
# 只移除明显是密钥/令牌的那部分。
SENSITIVE_ENV_PATTERNS: Sequence[str] = (
    "*_API_KEY", "*_API_TOKEN", "*_TOKEN", "*_SECRET", "*_SECRETS",
    "*_PASSWORD", "*_PASSWD", "*_CREDENTIAL", "*_CREDENTIALS",
    "*_PRIVATE_KEY", "*_ACCESS_KEY", "*_SESSION_TOKEN",
)

# 不以常见密钥后缀结尾、但仍属敏感值的显式名单。
SENSITIVE_ENV_NAMES: frozenset[str] = frozenset({
    "AWS_ACCESS_KEY_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
})


def scrub_environment(
    env: Mapping[str, str],
    extra_names: Iterable[str] = (),
    patterns: Sequence[str] = SENSITIVE_ENV_PATTERNS,
    names: frozenset[str] = SENSITIVE_ENV_NAMES,
) -> Dict[str, str]:
    """返回移除敏感变量后的环境副本（供子进程使用，不修改原环境）。"""
    scrubbed: Dict[str, str] = {}
    for key, value in env.items():
        if key in names or key in extra_names:
            continue
        if any(fnmatch.fnmatch(key, pattern) for pattern in patterns):
            continue
        scrubbed[key] = value
    return scrubbed


class RunCommandTool:
    name = "run_command"
    description = (
        "在工作区内执行 shell 命令并返回合并的 stdout+stderr 与 exit code。"
        "默认超时 60 秒（可用 timeout 参数调整，上限 600 秒），超时会终止整个进程组。"
        "cwd 可指定工作区内的子目录。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout": {"type": "number", "description": f"超时秒数（默认 {int(DEFAULT_TIMEOUT)}，上限 {int(MAX_TIMEOUT)}）"},
            "cwd": {"type": "string", "description": "工作目录（相对工作区根；缺省为工作区根）"},
        },
        "required": ["command"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult(content="command 必须是非空字符串", is_error=True)

        try:
            cwd = resolve_in_workspace(ctx, str(args.get("cwd") or "."))
        except (PathOutsideWorkspaceError, ValueError) as e:
            return ToolResult(content=str(e), is_error=True)
        if not cwd.is_dir():
            return ToolResult(content=f"cwd 不是工作区内的目录: {cwd}", is_error=True)

        timeout = _clamp_timeout(args.get("timeout"))

        # 子进程不继承 LLM API key 等敏感环境变量（防 agent 用 env/子进程读 key）
        env = scrub_environment(os.environ)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(cwd),
                start_new_session=True,  # 独立进程组，超时可整组击杀
                env=env,
            )
        except OSError as e:
            return ToolResult(content=f"无法启动命令: {e}", is_error=True)

        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            _terminate_process_tree(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            return ToolResult(
                content=f"命令超时（>{timeout:g}s），已终止进程组: {command}",
                is_error=True,
                data={"exit_code": None, "timed_out": True},
            )

        output = raw.decode("utf-8", "replace")
        output = truncate_middle(output, MAX_OUTPUT_CHARS)
        exit_code = proc.returncode if proc.returncode is not None else -1
        content = f"exit_code: {exit_code}\n{output if output.strip() else '（无输出）'}"
        return ToolResult(
            content=content,
            is_error=exit_code != 0,
            data={"exit_code": exit_code, "timed_out": False},
        )


def _clamp_timeout(value: Any) -> float:
    try:
        t = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return max(1.0, min(t, MAX_TIMEOUT))


def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """跨平台终止：POSIX 用进程组整组击杀；Windows（无 killpg/SIGKILL）
    退化为杀主进程（start_new_session 在 Windows 上被 subprocess 忽略，无害）。"""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError, AttributeError):
            pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
