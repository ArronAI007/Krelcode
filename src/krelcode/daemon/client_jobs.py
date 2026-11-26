"""Daemon-backed background Shell job manager adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.events import AgentEvent, EventBus, ShellJobFinished, ShellJobOutput, ShellJobStarted
from ..core.redact import SecretRedactor
from ..jobs.shell import (
    ShellJobError,
    ShellJobInfo,
    ShellJobNotFound,
    ShellJobOutputPage,
    ShellJobStatus,
)
from .client_transport import DaemonClient, DaemonUnavailable
from .paths import DaemonPaths
from .protocol import RPCError
from .storage import JobRecordStore


class DaemonJobManager:
    """Drop-in ShellJobManager adapter whose lifecycle is daemon-owned."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        config_path: Optional[Path] = None,
        client: Optional[DaemonClient] = None,
        redactor: Optional[SecretRedactor] = None,
        idle_timeout: Optional[float] = None,
        bus: Optional[EventBus] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.client = client or DaemonClient(
            self.workspace_root, config_path=config_path, redactor=redactor)
        self._redactor = redactor
        self.client.redactor = redactor
        self.idle_timeout = idle_timeout
        self.bus = bus
        self._cache: Dict[str, ShellJobInfo] = {}
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._seen_status: Dict[str, str] = {}
        self._seen_sequence: Dict[str, int] = {}
        self._closed = False

    @property
    def redactor(self) -> Optional[SecretRedactor]:
        return self._redactor

    @redactor.setter
    def redactor(self, value: Optional[SecretRedactor]) -> None:
        self._redactor = value
        self.client.redactor = value

    def list_jobs(self) -> List[ShellJobInfo]:
        try:
            rows = self.client._request_sync("job.list", {})
        except (DaemonUnavailable, RPCError):
            store = JobRecordStore(DaemonPaths(self.workspace_root), redactor=self.redactor)
            rows = [_job_info_dict(item.info()) for item in store.list()]
        jobs = [_job_from_dict(item) for item in rows if isinstance(item, dict)]
        self._cache = {job.job_id: job for job in jobs}
        return jobs

    def get(self, job_id: str) -> ShellJobInfo:
        target = str(job_id or "").strip()
        if target in self._cache:
            return self._cache[target]
        for job in self.list_jobs():
            if job.job_id == target:
                return job
        raise ShellJobNotFound(f"找不到后台任务: {job_id}")

    async def start(
        self,
        command: str,
        *,
        cwd: Path,
        sandbox: Any = None,
        permission_profile: str | None = None,
    ) -> ShellJobInfo:
        await self.client.ensure_started(idle_timeout=self.idle_timeout)
        params: Dict[str, Any] = {
            "command": str(command or ""),
            "cwd": str(Path(cwd).resolve()),
        }
        if sandbox is not None:
            params["sandbox_backend"] = getattr(sandbox, "backend", "")
            params["sandbox_network"] = bool(getattr(sandbox, "network", False))
        if permission_profile is not None:
            params["sandbox_permission"] = permission_profile
        data = await self.client.request("job.start", params, timeout=30.0)
        job = _job_from_dict(data)
        self._cache[job.job_id] = job
        if self.bus is not None:
            self._seen_status[job.job_id] = job.status.value
            self._seen_sequence[job.job_id] = 0
            await self._safe_emit(ShellJobStarted(
                job_id=job.job_id,
                command=job.command,
                pid=job.pid,
                cwd=job.cwd,
            ))
            self._ensure_polling()
        return job

    async def output(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        max_chars: int = 30_000,
        wait_seconds: float = 0.0,
    ) -> ShellJobOutputPage:
        try:
            data = await self.client.request(
                "job.output",
                {
                    "job_id": job_id,
                    "after_sequence": int(after_sequence),
                    "max_chars": int(max_chars),
                    "wait_seconds": float(wait_seconds),
                },
                timeout=max(5.0, min(float(wait_seconds) + 5.0, 40.0)),
            )
        except (DaemonUnavailable, RPCError) as exc:
            store = JobRecordStore(DaemonPaths(self.workspace_root), redactor=self.redactor)
            page = store.output(
                job_id,
                after_sequence=after_sequence,
                max_chars=max_chars,
            )
            if page is not None:
                return ShellJobOutputPage(
                    job=page.job,
                    text=page.text,
                    next_sequence=page.next_sequence,
                    truncated=page.truncated,
                )
            raise ShellJobError(str(exc)) from exc
        job = _job_from_dict(data.get("job") or {})
        self._cache[job.job_id] = job
        return ShellJobOutputPage(
            job=job,
            text=str(data.get("text") or ""),
            next_sequence=int(data.get("next_sequence") or 0),
            truncated=bool(data.get("truncated")),
        )

    async def kill(self, job_id: str) -> ShellJobInfo:
        try:
            data = await self.client.request(
                "job.kill", {"job_id": job_id}, timeout=15.0)
        except (DaemonUnavailable, RPCError) as exc:
            raise ShellJobError(str(exc)) from exc
        job = _job_from_dict(data)
        self._cache[job.job_id] = job
        return job

    async def shutdown(self) -> None:
        # Deliberately does not stop the daemon: AgentRuntime.close() only ends
        # this client session.  Background jobs survive until explicit kill,
        # daemon shutdown, or configured idle timeout.
        self._closed = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
            self._poll_task = None

    def _ensure_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(
                self._poll_events(), name="krelcode-daemon-job-events")

    async def _poll_events(self) -> None:
        while not self._closed:
            try:
                rows = await self.client.request("job.list", {}, timeout=5.0)
                for item in rows:
                    job_id = str(item.get("job_id") or "")
                    if not job_id:
                        continue
                    job = _job_from_dict(item)
                    previous_sequence = self._seen_sequence.get(job_id, 0)
                    previous_status = self._seen_status.get(job_id, "")
                    if not previous_status:
                        await self._safe_emit(ShellJobStarted(
                            job_id=job.job_id,
                            command=job.command,
                            pid=job.pid,
                            cwd=job.cwd,
                        ))
                    if job.latest_sequence > previous_sequence:
                        page = await self.output(
                            job_id, after_sequence=previous_sequence)
                        self._seen_sequence[job_id] = page.next_sequence
                        if page.text:
                            await self._safe_emit(ShellJobOutput(
                                job_id=job_id,
                                sequence=page.next_sequence,
                                text=page.text,
                            ))
                    if (job.status.value != previous_status
                            and job.status is not ShellJobStatus.RUNNING
                            and previous_status):
                        try:
                            tail = await self.output(
                                job_id, after_sequence=0, max_chars=600)
                            preview = tail.text.strip()
                        except Exception:
                            preview = ""
                        await self._safe_emit(ShellJobFinished(
                            job_id=job.job_id,
                            command=job.command,
                            status=job.status.value,
                            exit_code=job.exit_code,
                            preview=preview,
                        ))
                    self._seen_status[job_id] = job.status.value
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.5)

    async def _safe_emit(self, event: AgentEvent) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.emit(event)
        except Exception:
            pass


def _job_from_dict(raw: Dict[str, Any]) -> ShellJobInfo:
    if not isinstance(raw, dict) or not raw.get("job_id"):
        raise ShellJobError("daemon 返回了无效 job 信息")
    return ShellJobInfo(
        job_id=str(raw.get("job_id") or ""),
        command=str(raw.get("command") or ""),
        cwd=str(raw.get("cwd") or ""),
        pid=int(raw.get("pid") or 0),
        status=ShellJobStatus(str(raw.get("status") or "lost")),
        started_at=float(raw.get("started_at") or 0.0),
        ended_at=(float(raw["ended_at"]) if raw.get("ended_at") is not None else None),
        exit_code=(int(raw["exit_code"]) if raw.get("exit_code") is not None else None),
        latest_sequence=int(raw.get("latest_sequence") or 0),
        log_path=str(raw.get("log_path") or ""),
    )


def _job_info_dict(info: ShellJobInfo) -> Dict[str, Any]:
    return {
        "job_id": info.job_id,
        "command": info.command,
        "cwd": info.cwd,
        "pid": info.pid,
        "status": info.status.value,
        "started_at": info.started_at,
        "ended_at": info.ended_at,
        "exit_code": info.exit_code,
        "latest_sequence": info.latest_sequence,
        "log_path": info.log_path,
    }



__all__ = ["DaemonJobManager"]
