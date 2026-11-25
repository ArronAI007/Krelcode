"""Bounded, redacted JSONL journal and EventBus isolation adapter."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from krelcode.core.events import AgentEvent, EventBus
from krelcode.core.redact import SecretRedactor

from .models import EventEnvelope, RunManifest
from .projection import EventProjector


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write one private JSON file atomically."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ) + "\n"
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
        os.chmod(path, 0o600)
    except Exception:
        # fdopen owns the descriptor once entered; this branch only documents
        # that every write path must preserve append-only ordering.
        raise


class _JournalWriter:
    def __init__(
        self,
        run_dir: Path,
        manifest: RunManifest,
        *,
        queue_size: int,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.events_path = self.run_dir / "events.jsonl"
        self.manifest_path = self.run_dir / "manifest.json"
        self.manifest = manifest
        self.queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(
            maxsize=max(1, int(queue_size)))
        self._worker: Optional[asyncio.Task[None]] = None
        self._closed = False
        self.write_failures = 0
        self.last_error = ""

    async def start(self) -> None:
        if self._worker is not None:
            return
        await asyncio.to_thread(atomic_write_json, self.manifest_path,
                                self.manifest.to_dict())
        self._worker = asyncio.create_task(
            self._run(), name=f"observability-journal-{self.manifest.run_id}")

    async def submit(self, envelope: EventEnvelope) -> bool:
        if self._closed:
            return False
        if envelope.session_id and not self.manifest.session_id:
            self.manifest = replace(
                self.manifest, session_id=envelope.session_id)
            await asyncio.to_thread(
                atomic_write_json, self.manifest_path, self.manifest.to_dict())
        try:
            self.queue.put_nowait(envelope)
            return True
        except asyncio.QueueFull:
            return False

    async def _run(self) -> None:
        while True:
            envelope = await self.queue.get()
            try:
                if envelope.session_id and not self.manifest.session_id:
                    self.manifest = replace(
                        self.manifest, session_id=envelope.session_id)
                    await asyncio.to_thread(
                        atomic_write_json, self.manifest_path,
                        self.manifest.to_dict())
                await asyncio.to_thread(
                    append_jsonl, self.events_path, envelope.to_dict())
            except Exception as exc:
                self.write_failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self.queue.task_done()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._worker is not None:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=5.0)
            except asyncio.TimeoutError:
                self.last_error = "journal drain timeout"
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)


class ObservabilitySink:
    """Non-blocking EventBus subscriber.

    The synchronous handler only projects/redacts/enqueues.  Disk I/O and SSE
    delivery happen outside the Agent critical path.  A full queue, bad event,
    UI disconnect or journal failure is converted into a counter and never
    propagates into ``EventBus.emit``.
    """

    def __init__(
        self,
        workspace_root: Path,
        manifest: RunManifest,
        redactor: SecretRedactor,
        *,
        queue_size: int = 4_096,
        max_string_chars: int = 4_000,
        max_collection_items: int = 100,
        include_text_previews: bool = True,
        on_drop: Optional[Callable[[str], None]] = None,
    ) -> None:
        safe_run_id = "".join(
            char for char in manifest.run_id
            if char.isalnum() or char in {"-", "_"}
        )
        if not safe_run_id:
            raise ValueError("run_id 必须包含安全字符")
        root = Path(workspace_root).expanduser().resolve()
        self.run_dir = root / ".krelcode" / "observability" / "runs" / safe_run_id
        self.manifest = manifest
        self.projector = EventProjector(
            manifest.run_id,
            redactor,
            max_string_chars=max_string_chars,
            max_collection_items=max_collection_items,
            include_text_previews=include_text_previews,
        )
        self._writer = _JournalWriter(
            self.run_dir, manifest, queue_size=queue_size)
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._sequence = 0
        self.dropped_events = 0
        self.projection_failures = 0
        self.last_error = ""
        self._on_drop = on_drop
        self._started = False
        self._closed = False

    @property
    def run_id(self) -> str:
        return self.manifest.run_id

    @property
    def events_path(self) -> Path:
        return self._writer.events_path

    @property
    def manifest_path(self) -> Path:
        return self._writer.manifest_path

    @property
    def write_failures(self) -> int:
        return self._writer.write_failures

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            await self._writer.start()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

    def attach(self, bus: EventBus) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = bus.subscribe(self.on_event)

    def on_event(self, event: AgentEvent) -> None:
        if self._closed:
            return
        try:
            self._sequence += 1
            envelope = self.projector.project(event, self._sequence)
        except Exception as exc:
            self.projection_failures += 1
            self.last_error = f"projection: {type(exc).__name__}: {exc}"
            self._note_drop("projection_error")
            return
        if not self._started:
            try:
                self._writer.queue.put_nowait(envelope)
            except asyncio.QueueFull:
                self._record_drop("queue_full")
            return
        try:
            self._writer.queue.put_nowait(envelope)
        except asyncio.QueueFull:
            self._record_drop("queue_full")

    async def submit(self, event: AgentEvent) -> bool:
        """Explicit async submission useful for non-EventBus hosts and tests."""
        if self._closed:
            return False
        try:
            self._sequence += 1
            envelope = self.projector.project(event, self._sequence)
            accepted = await self._writer.submit(envelope)
        except Exception as exc:
            self.projection_failures += 1
            self.last_error = f"projection: {type(exc).__name__}: {exc}"
            self._note_drop("projection_error")
            return False
        if not accepted:
            self._record_drop("queue_full")
        return accepted

    async def flush(self) -> None:
        if self._started:
            await self._writer.queue.join()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._started:
            await self._writer.close()
        self.last_error = self._writer.last_error or self.last_error

    def _record_drop(self, reason: str) -> None:
        self.dropped_events += 1
        self._note_drop(reason)

    def _note_drop(self, reason: str) -> None:
        if self._on_drop is None:
            return
        try:
            self._on_drop(reason)
        except Exception:
            pass

    @property
    def dropped(self) -> int:
        return self.dropped_events

    @property
    def is_private_file_mode(self) -> bool:
        try:
            return stat.S_IMODE(self.events_path.stat().st_mode) == 0o600
        except OSError:
            return False


__all__ = [
    "ObservabilitySink",
    "append_jsonl",
    "atomic_write_json",
]
