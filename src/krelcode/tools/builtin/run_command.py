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
import os
import signal
from typing import Any, Dict

from ..base import ToolContext, ToolResult
from ..fsutils import truncate_middle
from ..paths import PathOutsideWorkspaceError, resolve_in_workspace
from ...safety.environment import (
    SENSITIVE_ENV_NAMES, SENSITIVE_ENV_PATTERNS, scrub_environment,
)
from ...safety.sandbox import SandboxError

MAX_OUTPUT_CHARS = 30_000
DEFAULT_TIMEOUT = 60.0
MAX_TIMEOUT = 600.0

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

        sandbox = getattr(ctx, "sandbox", None)
        if sandbox is not None:
            violation = sandbox.audit_command(command, cwd)
            if violation:
                return ToolResult(
                    content=f"Sandbox 拒绝命令: {violation}", is_error=True,
                    data={"sandbox_denied": True},
                )
            try:
                prepared = sandbox.prepare_shell(command, cwd)
            except SandboxError as e:
                return ToolResult(
                    content=f"Sandbox 准备失败: {e}", is_error=True,
                    data={"sandbox_denied": True},
                )
            argv = prepared.argv
            env = prepared.env
        else:
            argv = ["/bin/sh", "-c", command]
            env = scrub_environment(os.environ)

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
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
            data={
                "exit_code": exit_code, "timed_out": False,
                "sandbox_backend": getattr(sandbox, "backend", "none"),
            },
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
