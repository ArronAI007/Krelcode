"""后台 Shell job 工具：只通过 ToolContext.shell_jobs 访问 Job Manager。"""

from __future__ import annotations

from typing import Any, Dict

from ..base import ToolContext, ToolResult
from ..paths import PathOutsideWorkspaceError, resolve_in_workspace


def _manager(ctx: ToolContext):
    manager = getattr(ctx, "shell_jobs", None)
    if manager is None:
        return None, ToolResult(
            content="后台任务管理器未启用",
            is_error=True,
        )
    return manager, None


class JobStartTool:
    name = "job_start"
    description = (
        "启动一个独立的后台 Shell 任务并立即返回 job_id。任务不会随当前 turn "
        "结束而停止；用 job_output 轮询实时输出，用 job_kill 终止。适合 dev server、"
        "watcher、长时间测试等。不要重复启动同一个后台命令。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要后台执行的 shell 命令"},
            "cwd": {
                "type": "string",
                "description": "工作目录（相对工作区根；缺省为工作区根）",
            },
        },
        "required": ["command"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        manager, error = _manager(ctx)
        if error is not None:
            return error
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return ToolResult(content="command 必须是非空字符串", is_error=True)
        try:
            cwd = resolve_in_workspace(ctx, str(args.get("cwd") or "."))
        except (PathOutsideWorkspaceError, ValueError) as exc:
            return ToolResult(content=str(exc), is_error=True)
        try:
            job = await manager.start(command, cwd=cwd, sandbox=ctx.sandbox)
        except Exception as exc:
            return ToolResult(
                content=f"后台任务启动失败: {exc}",
                is_error=True,
                data={"sandbox_denied": "Sandbox 拒绝命令" in str(exc)},
            )
        content = (
            f"job_id: {job.job_id}\n"
            f"status: {job.status.value}\n"
            f"pid: {job.pid}\n"
            f"cwd: {job.cwd}\n"
            f"log: {job.log_path}\n"
            "任务已在后台运行；用 job_output 读取增量，用 job_kill 终止。"
        )
        return ToolResult(
            content=content,
            data={
                "job_id": job.job_id,
                "pid": job.pid,
                "status": job.status.value,
                "cwd": job.cwd,
                "log_path": job.log_path,
            },
        )


class JobOutputTool:
    name = "job_output"
    description = (
        "读取后台任务的增量输出。after_sequence 使用上次返回的 sequence 游标，"
        "避免重复读取；wait_seconds 可等待新输出或任务结束（最多 30 秒）。"
    )
    read_only = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "job_start 返回的 job id"},
            "after_sequence": {
                "type": "integer",
                "description": "只返回 sequence 大于该值的输出；首次为 0",
                "minimum": 0,
            },
            "wait_seconds": {
                "type": "number",
                "description": "等待新输出的秒数（0-30，默认 0）",
                "minimum": 0,
                "maximum": 30,
            },
        },
        "required": ["job_id"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        manager, error = _manager(ctx)
        if error is not None:
            return error
        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            return ToolResult(content="job_id 不能为空", is_error=True)
        try:
            page = await manager.output(
                job_id,
                after_sequence=int(args.get("after_sequence") or 0),
                wait_seconds=float(args.get("wait_seconds") or 0),
            )
        except Exception as exc:
            return ToolResult(content=f"读取后台任务失败: {exc}", is_error=True)
        return ToolResult(
            content=page.render(),
            data={
                "job_id": page.job.job_id,
                "status": page.job.status.value,
                "exit_code": page.job.exit_code,
                "next_sequence": page.next_sequence,
                "truncated": page.truncated,
                "running": page.job.running,
            },
        )


class JobListTool:
    name = "job_list"
    description = "列出当前进程管理的后台 Shell 任务、状态、退出码与最新 sequence。"
    read_only = True
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        manager, error = _manager(ctx)
        if error is not None:
            return error
        jobs = manager.list_jobs()
        if not jobs:
            return ToolResult(content="当前没有后台任务", data={"jobs": []})
        lines = [f"后台任务（{len(jobs)}）:"]
        for job in jobs:
            exit_text = "-" if job.exit_code is None else str(job.exit_code)
            lines.append(
                f"{job.job_id}  {job.status.value:<9} exit={exit_text:<4} "
                f"pid={job.pid} seq={job.latest_sequence}  {job.command}")
        return ToolResult(
            content="\n".join(lines),
            data={"jobs": [
                {
                    "job_id": job.job_id,
                    "status": job.status.value,
                    "exit_code": job.exit_code,
                    "pid": job.pid,
                    "latest_sequence": job.latest_sequence,
                    "command": job.command,
                }
                for job in jobs
            ]},
        )


class JobKillTool:
    name = "job_kill"
    description = "终止一个由 job_start 启动的后台任务及其整个进程组。"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "要终止的 job id"},
        },
        "required": ["job_id"],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        manager, error = _manager(ctx)
        if error is not None:
            return error
        job_id = str(args.get("job_id") or "").strip()
        if not job_id:
            return ToolResult(content="job_id 不能为空", is_error=True)
        try:
            job = await manager.kill(job_id)
        except Exception as exc:
            return ToolResult(content=f"终止后台任务失败: {exc}", is_error=True)
        return ToolResult(
            content=(
                f"job_id: {job.job_id}\nstatus: {job.status.value}\n"
                f"exit_code: {job.exit_code if job.exit_code is not None else '-'}"
            ),
            data={
                "job_id": job.job_id,
                "status": job.status.value,
                "exit_code": job.exit_code,
            },
        )


__all__ = [
    "JobKillTool",
    "JobListTool",
    "JobOutputTool",
    "JobStartTool",
]
