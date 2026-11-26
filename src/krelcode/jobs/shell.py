"""后台 Shell Job Manager：独立进程组、实时输出、查询与终止。

职责边界：
- ShellJobManager 不依赖 Tool、REPL 或 AgentLoop；
- 每条命令经 ToolContext 的 ExecutionSandbox 审计/包装后启动；
- 输出由独立 reader 持续消费：事件总线实时通知、有界内存保留 tail、
  `.krelcode/jobs/<id>/output.log` 保存脱敏后的完整日志（有磁盘上限）；
- job 生命周期刻意不绑定 turn abort：用户/模型显式 kill 或 runtime shutdown
  才终止，避免一轮对话结束误杀 dev server/test watcher。
"""

from __future__ import annotations

import asyncio
import codecs
import os
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Deque, Dict, List, Optional

from ..core.events import (
    EventBus,
    AgentEvent,
    ShellJobFinished,
    ShellJobOutput,
    ShellJobStarted,
)
from ..core.redact import SecretRedactor
from ..safety.environment import scrub_environment
from ..safety.sandbox import ExecutionSandbox, SandboxError
from ..core.process import (
    process_isolation_kwargs,
    shell_argv,
    terminate_process_tree,
)

DEFAULT_MAX_ACTIVE = 16
DEFAULT_MAX_OUTPUT_CHARS = 200_000
DEFAULT_MAX_LOG_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_JOBS = 100
JOB_DIRNAME = Path(".krelcode") / "jobs"


class ShellJobError(RuntimeError):
    """后台 Shell job 无法创建。"""


class ShellJobNotFound(ShellJobError):
    """请求的 job id 不存在。"""


class ShellJobStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"
    # Durable daemon records can outlive the pipe-owning process.  ``lost`` is
    # deliberately not considered terminal success/failure: the OS process may
    # still exist, but Krelcode cannot safely attach to or kill it by PID alone.
    LOST = "lost"


@dataclass(frozen=True)
class ShellJobInfo:
    job_id: str
    command: str
    cwd: str
    pid: int
    status: ShellJobStatus
    started_at: float
    ended_at: Optional[float] = None
    exit_code: Optional[int] = None
    latest_sequence: int = 0
    log_path: str = ""

    @property
    def running(self) -> bool:
        return self.status is ShellJobStatus.RUNNING


@dataclass(frozen=True)
class ShellJobOutputPage:
    job: ShellJobInfo
    text: str
    next_sequence: int
    truncated: bool = False

    def render(self, *, max_chars: int = 30_000) -> str:
        header = (
            f"job_id: {self.job.job_id}\n"
            f"status: {self.job.status.value}\n"
            f"exit_code: {self.job.exit_code if self.job.exit_code is not None else '-'}\n"
            f"sequence: {self.next_sequence}"
            + ("\ntruncated: true" if self.truncated else "")
        )
        body = self.text
        if len(body) > max_chars:
            body = body[-max_chars:]
            header += "\ntruncated: true"
        if not body:
            body = "（暂无新输出）"
        return f"{header}\n--- output ---\n{body}"


@dataclass(frozen=True)
class _OutputChunk:
    sequence: int
    text: str


@dataclass
class _JobRuntime:
    info_id: str
    command: str
    cwd: Path
    pid: int
    started_at: float
    proc: asyncio.subprocess.Process
    log_path: Path
    log_handle: Optional[BinaryIO] = None
    status: ShellJobStatus = ShellJobStatus.RUNNING
    ended_at: Optional[float] = None
    exit_code: Optional[int] = None
    kill_requested: bool = False
    latest_sequence: int = 0
    dropped_sequence: int = 0
    buffer_chars: int = 0
    chunks: Deque[_OutputChunk] = field(default_factory=deque)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    reader_task: Optional[asyncio.Task[None]] = None
    wait_task: Optional[asyncio.Task[None]] = None
    log_bytes: int = 0
    log_capped: bool = False


class ShellJobManager:
    def __init__(
        self,
        bus: EventBus,
        *,
        workspace_root: Optional[Path] = None,
        redactor: Optional[SecretRedactor] = None,
        max_active: int = DEFAULT_MAX_ACTIVE,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
        max_jobs: int = DEFAULT_MAX_JOBS,
    ) -> None:
        self.bus = bus
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None
        self.redactor = redactor
        self.max_active = max(1, int(max_active))
        self.max_output_chars = max(1_000, int(max_output_chars))
        self.max_log_bytes = max(0, int(max_log_bytes))
        self.max_jobs = max(self.max_active, int(max_jobs))
        self._jobs: Dict[str, _JobRuntime] = {}
        self._sequence = 0
        self._closed = False
        self._start_lock = asyncio.Lock()

    def list_jobs(self) -> List[ShellJobInfo]:
        return [self._snapshot(job) for job in self._jobs.values()]

    def get(self, job_id: str) -> ShellJobInfo:
        return self._snapshot(self._get_runtime(job_id))

    async def start(
        self,
        command: str,
        *,
        cwd: Path,
        sandbox: Optional[ExecutionSandbox] = None,
        permission_profile: str | None = None,
    ) -> ShellJobInfo:
        if self._closed:
            raise ShellJobError("Shell job manager 已关闭")
        text = str(command or "").strip()
        if not text:
            raise ShellJobError("command 不能为空")
        resolved_cwd = self._validate_cwd(Path(cwd))
        if sandbox is not None:
            violation = (
                sandbox.audit_command(text, resolved_cwd)
                if permission_profile is None
                else sandbox.audit_command(
                    text,
                    resolved_cwd,
                    permission_profile=permission_profile,
                )
            )
            if violation:
                raise ShellJobError(f"Sandbox 拒绝命令: {violation}")
            try:
                prepared = (
                    sandbox.prepare_shell(text, resolved_cwd)
                    if permission_profile is None
                    else sandbox.prepare_shell(
                        text,
                        resolved_cwd,
                        permission_profile=permission_profile,
                    )
                )
            except SandboxError as exc:
                raise ShellJobError(f"Sandbox 准备失败: {exc}") from exc
            argv, env = prepared.argv, prepared.env
            process_kwargs = prepared.process_kwargs
        else:
            argv = shell_argv(text)
            env = scrub_environment(os.environ)
            process_kwargs = process_isolation_kwargs()

        # create_subprocess_exec 会 await；锁住容量检查与占位，避免并发 start
        # 同时越过 max_active。关闭操作也使用同一把锁，防止 shutdown 与
        # “准备中”的 start 竞态留下未回收进程。
        async with self._start_lock:
            if self._closed:
                raise ShellJobError("Shell job manager 已关闭")
            active = sum(
                job.status is ShellJobStatus.RUNNING
                for job in self._jobs.values())
            if active >= self.max_active:
                raise ShellJobError(
                    f"后台任务已达到并发上限 {self.max_active}；先 /jobs kill <id>")
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(resolved_cwd),
                    env=env,
                    **process_kwargs,
                )
            except OSError as exc:
                raise ShellJobError(f"无法启动命令: {exc}") from exc

            self._sequence += 1
            job_id = f"job-{self._sequence}"
            stamp = time.time()
            job_dir = (
                (self.workspace_root or resolved_cwd)
                / JOB_DIRNAME
                / f"{job_id}-{os.getpid()}-{int(stamp * 1000)}"
            )
            log_path = job_dir / "output.log"
            log_handle = None
            try:
                job_dir.mkdir(parents=True, exist_ok=True)
                if os.name == "posix":
                    os.chmod(job_dir, 0o700)
                fd = os.open(
                    log_path,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                log_handle = os.fdopen(fd, "ab", buffering=0)
            except OSError:
                log_handle = None

            job = _JobRuntime(
                info_id=job_id,
                command=self._redact(text),
                cwd=resolved_cwd,
                pid=proc.pid,
                started_at=stamp,
                proc=proc,
                log_path=log_path,
                log_handle=log_handle,
            )
            self._jobs[job_id] = job
            job.reader_task = asyncio.create_task(
                self._consume_output(job), name=f"shell-job-read-{job_id}")
            job.wait_task = asyncio.create_task(
                self._wait_job(job), name=f"shell-job-wait-{job_id}")

        await self._safe_emit(ShellJobStarted(
            job_id=job_id,
            command=job.command,
            pid=proc.pid,
            cwd=str(resolved_cwd),
        ))
        self._prune_history()
        return self._snapshot(job)

    async def output(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        max_chars: int = 30_000,
        wait_seconds: float = 0.0,
    ) -> ShellJobOutputPage:
        job = self._get_runtime(job_id)
        cursor = max(0, int(after_sequence))
        wait_for = max(0.0, min(float(wait_seconds), 30.0))
        if wait_for > 0 and cursor >= job.latest_sequence and job.status is ShellJobStatus.RUNNING:
            async with job.condition:
                if cursor >= job.latest_sequence and job.status is ShellJobStatus.RUNNING:
                    try:
                        await asyncio.wait_for(job.condition.wait(), timeout=wait_for)
                    except asyncio.TimeoutError:
                        pass
        return self._output_page(job, cursor, max_chars)

    async def kill(self, job_id: str) -> ShellJobInfo:
        job = self._get_runtime(job_id)
        if job.status is not ShellJobStatus.RUNNING:
            return self._snapshot(job)
        job.kill_requested = True
        terminate_process_tree(job.proc, force=False)
        waiter = job.wait_task
        if waiter is not None:
            try:
                await asyncio.wait_for(asyncio.shield(waiter), timeout=3.0)
            except asyncio.TimeoutError:
                terminate_process_tree(job.proc, force=True)
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
        return self._snapshot(job)

    async def shutdown(self) -> None:
        async with self._start_lock:
            if self._closed:
                return
            self._closed = True
        running = [
            job_id for job_id, job in self._jobs.items()
            if job.status is ShellJobStatus.RUNNING
        ]
        for job_id in running:
            await self.kill(job_id)
        waiters = [
            job.wait_task for job in self._jobs.values()
            if job.wait_task is not None and not job.wait_task.done()
        ]
        if waiters:
            await asyncio.gather(*waiters, return_exceptions=True)
        for job in self._jobs.values():
            self._close_log(job)

    async def _consume_output(self, job: _JobRuntime) -> None:
        assert job.proc.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        try:
            while True:
                chunk = await job.proc.stdout.read(4096)
                if not chunk:
                    break
                pending += decoder.decode(chunk)
                overlap = self._stream_secret_overlap(pending)
                if overlap:
                    emit, pending = pending[:-overlap], pending[-overlap:]
                else:
                    emit, pending = pending, ""
                if emit:
                    await self._append_output(job, emit)
            pending += decoder.decode(b"", final=True)
            if pending:
                await self._append_output(job, pending)
        except asyncio.CancelledError:
            raise
        except Exception:
            # reader 故障不能阻塞进程 reaper；已产生内容仍可通过 finish 收口。
            pass

    def _stream_secret_overlap(self, text: str) -> int:
        """返回必须暂存的最长“secret 前缀”尾巴，防止跨块泄漏。"""
        if self.redactor is None:
            return 0
        max_overlap = 0
        for secret in getattr(self.redactor, "secrets", ()):
            limit = min(len(secret) - 1, len(text))
            for size in range(limit, max_overlap, -1):
                if text.endswith(secret[:size]):
                    max_overlap = size
                    break
        return max_overlap

    async def _append_output(self, job: _JobRuntime, text: str) -> None:
        safe = self._redact(text)
        if not safe:
            return
        job.latest_sequence += 1
        sequence = job.latest_sequence
        if len(safe) > self.max_output_chars:
            # 单条超长输出只保留尾部。sequence 仍可用；任何更早的 cursor
            # 都会看到 truncated=true。
            safe = safe[-self.max_output_chars:]
            job.chunks.clear()
            job.buffer_chars = len(safe)
            job.chunks.append(_OutputChunk(sequence=sequence, text=safe))
            job.dropped_sequence = sequence
        else:
            job.chunks.append(_OutputChunk(sequence=sequence, text=safe))
            job.buffer_chars += len(safe)
            while job.buffer_chars > self.max_output_chars and job.chunks:
                removed = job.chunks.popleft()
                job.buffer_chars -= len(removed.text)
                job.dropped_sequence = max(
                    job.dropped_sequence, removed.sequence)
        self._write_log(job, safe)
        async with job.condition:
            job.condition.notify_all()
        await self._safe_emit(ShellJobOutput(
            job_id=job.info_id,
            sequence=sequence,
            text=safe,
        ))

    async def _wait_job(self, job: _JobRuntime) -> None:
        finished_normally = False
        try:
            exit_code = await job.proc.wait()
            reader = job.reader_task
            if reader is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(reader), timeout=2.0)
                except asyncio.TimeoutError:
                    # 主进程已退出但后代仍持有 pipe：结束残留进程组，避免
                    # reaper 被一个孤儿子进程永久卡住。
                    terminate_process_tree(job.proc, force=False)
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(reader), timeout=1.0)
                    except asyncio.TimeoutError:
                        terminate_process_tree(job.proc, force=True)
                        reader.cancel()
                        await asyncio.gather(reader, return_exceptions=True)
            job.exit_code = exit_code
            if job.kill_requested:
                job.status = ShellJobStatus.KILLED
            elif exit_code == 0:
                job.status = ShellJobStatus.COMPLETED
            else:
                job.status = ShellJobStatus.FAILED
            job.ended_at = time.time()
            finished_normally = True
        except asyncio.CancelledError:
            if job.kill_requested or job.proc.returncode is not None:
                job.status = ShellJobStatus.KILLED
                job.exit_code = job.proc.returncode
                job.ended_at = job.ended_at or time.time()
            raise
        finally:
            self._close_log(job)
            async with job.condition:
                job.condition.notify_all()
        if finished_normally:
            preview = self._output_page(
                job, max(0, job.latest_sequence - 8), 600).text.strip()
            await self._safe_emit(ShellJobFinished(
                job_id=job.info_id,
                command=job.command,
                status=job.status.value,
                exit_code=job.exit_code,
                preview=preview,
            ))
            self._prune_history()

    def _output_page(
        self,
        job: _JobRuntime,
        after_sequence: int,
        max_chars: int,
    ) -> ShellJobOutputPage:
        limit = max(1, min(int(max_chars), 100_000))
        selected = [
            chunk for chunk in job.chunks
            if chunk.sequence > after_sequence
        ]
        text = "".join(chunk.text for chunk in selected)
        truncated = after_sequence < job.dropped_sequence
        if len(text) > limit:
            text = text[-limit:]
            truncated = True
        next_sequence = (
            selected[-1].sequence if selected else job.latest_sequence)
        return ShellJobOutputPage(
            job=self._snapshot(job),
            text=text,
            next_sequence=next_sequence,
            truncated=truncated,
        )

    def _snapshot(self, job: _JobRuntime) -> ShellJobInfo:
        return ShellJobInfo(
            job_id=job.info_id,
            command=job.command,
            cwd=str(job.cwd),
            pid=job.pid,
            status=job.status,
            started_at=job.started_at,
            ended_at=job.ended_at,
            exit_code=job.exit_code,
            latest_sequence=job.latest_sequence,
            log_path=str(job.log_path),
        )

    def _get_runtime(self, job_id: str) -> _JobRuntime:
        job = self._jobs.get(str(job_id or "").strip())
        if job is None:
            raise ShellJobNotFound(f"找不到后台任务: {job_id}")
        return job

    def _validate_cwd(self, cwd: Path) -> Path:
        root = self.workspace_root or cwd.resolve()
        try:
            resolved = cwd.expanduser().resolve()
        except (OSError, ValueError) as exc:
            raise ShellJobError(f"非法 cwd: {cwd}") from exc
        if resolved != root and root not in resolved.parents:
            raise ShellJobError(f"cwd 越出工作区: {resolved}")
        if not resolved.is_dir():
            raise ShellJobError(f"cwd 不是目录: {resolved}")
        return resolved

    def _write_log(self, job: _JobRuntime, text: str) -> None:
        handle = job.log_handle
        if handle is None or job.log_capped:
            return
        data = text.encode("utf-8", "replace")
        if self.max_log_bytes and job.log_bytes + len(data) > self.max_log_bytes:
            marker = (
                b"\n[krelcode] job log reached size limit; "
                b"output streaming stopped in log\n")
            remaining = max(0, self.max_log_bytes - job.log_bytes)
            try:
                if remaining:
                    handle.write(marker[:remaining])
                    job.log_bytes += min(len(marker), remaining)
            except OSError:
                self._close_log(job)
            job.log_capped = True
            return
        try:
            handle.write(data)
            job.log_bytes += len(data)
        except OSError:
            self._close_log(job)

    @staticmethod
    def _close_log(job: _JobRuntime) -> None:
        handle = job.log_handle
        job.log_handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _redact(self, text: str) -> str:
        return self.redactor.redact(text) if self.redactor is not None else text

    async def _safe_emit(self, event: AgentEvent) -> None:
        try:
            await self.bus.emit(event)
        except Exception:
            # 审计/UI 订阅者故障不能破坏后台进程 reaper 或输出消费。
            pass

    def _prune_history(self) -> None:
        if len(self._jobs) <= self.max_jobs:
            return
        terminal = [
            job_id for job_id, job in self._jobs.items()
            if job.status is not ShellJobStatus.RUNNING
            and (job.wait_task is None or job.wait_task.done())
        ]
        for job_id in terminal[:max(0, len(self._jobs) - self.max_jobs)]:
            job = self._jobs.pop(job_id)
            try:
                shutil.rmtree(job.log_path.parent, ignore_errors=True)
            except OSError:
                pass


__all__ = [
    "ShellJobError",
    "ShellJobInfo",
    "ShellJobManager",
    "ShellJobNotFound",
    "ShellJobOutputPage",
    "ShellJobStatus",
]
