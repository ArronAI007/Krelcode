"""EventBus telemetry collector with explicit consent and fail-open delivery."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .. import __version__
from ..core.events import (
    AgentEvent,
    ApprovalRequested,
    ContextCompressed,
    ErrorOccurred,
    EventBus,
    LLMResponseReceived,
    PermissionDenied,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from .config import (
    TelemetryConfig,
    TelemetryConfigError,
    TelemetryPreferenceStore,
    TelemetryPreferences,
    validate_endpoint,
)
from .privacy import (
    TELEMETRY_SCHEMA_VERSION,
    char_bucket,
    duration_bucket,
    error_category,
    runtime_environment,
    safe_model_name,
    safe_provider_name,
    token_bucket,
    tool_category,
)
from .store import TelemetryStore, TelemetryStoreError
from .transport import (
    HttpTelemetryTransport,
    TelemetryTransport,
    TelemetryTransportError,
)


class TelemetryRuntime:
    """Collect anonymous metrics without reading prompts, code or tool output."""

    def __init__(
        self,
        config: TelemetryConfig,
        *,
        provider: str = "",
        model: str = "",
        run_mode: str = "repl",
        policy_allowed: bool = True,
        store: Optional[TelemetryStore] = None,
        preferences_store: Optional[TelemetryPreferenceStore] = None,
        transport: Optional[TelemetryTransport] = None,
        initial_error: str = "",
    ) -> None:
        config_error = initial_error
        try:
            config.validate()
        except TelemetryConfigError as exc:
            config_error = config_error or f"{type(exc).__name__}: {exc}"
            config = replace(config, mode="off", endpoint="")
        self.config = config
        self.provider = safe_provider_name(provider)
        self.model = safe_model_name(model)
        self.run_mode = safe_model_name(run_mode) or "unknown"
        self.policy_allowed = bool(policy_allowed)
        self.store = store or TelemetryStore(
            config.queue_path,
            install_id_path=config.install_id_path,
            max_events=config.max_events,
            max_bytes=config.max_bytes,
            retention_days=config.retention_days,
        )
        self.preferences_store = preferences_store or TelemetryPreferenceStore(
            config.preferences_path)
        self.transport = transport
        self._bus: Optional[EventBus] = None
        self._unsubscribe = None
        self._task: Optional[asyncio.Task[None]] = None
        self._background: set[asyncio.Task[None]] = set()
        self._flush_lock = asyncio.Lock()
        self._sampled_in = self._sample_decision()
        self._session_id = uuid.uuid4().hex
        self._run_started: Optional[float] = None
        self._tool_started: dict[str, tuple[str, float]] = {}
        self._tool_calls = 0
        self._llm_calls = 0
        self._usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        self._os_family, self._arch, self._python_minor = runtime_environment()
        self.last_error = config_error

    def _sample_decision(self) -> bool:
        if self.config.sample_rate <= 0:
            return False
        if self.config.sample_rate >= 1:
            return True
        return secrets.randbelow(1_000_000) / 1_000_000 < self.config.sample_rate

    @property
    def enabled(self) -> bool:
        return self.policy_allowed and self.config.enabled and self._sampled_in

    @property
    def remote(self) -> bool:
        return self.enabled and self.config.remote

    def _envelope(
        self,
        event_type: str,
        data: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "ts": int(time.time()),
            "install_id": self.store.install_id,
            "session_id": self._session_id,
            "event": event_type,
            "krelcode_version": safe_model_name(__version__) or "unknown",
            "os_family": self._os_family,
            "arch": self._arch,
            "python_minor": self._python_minor,
            "provider": self.provider,
            "model": self.model,
            "run_mode": self.run_mode,
            "data": dict(data or {}),
        }

    def _spawn(self, awaitable) -> asyncio.Task[None]:
        task = asyncio.create_task(awaitable)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def set_identity(self, provider: str, model: str) -> None:
        """Update content-free provider identity after an in-process model switch."""
        self.provider = safe_provider_name(provider)
        self.model = safe_model_name(model)

    async def _record(
        self,
        event_type: str,
        data: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            await asyncio.to_thread(
                self.store.append, self._envelope(event_type, data))
        except (OSError, ValueError, TelemetryStoreError) as exc:
            # Telemetry is never allowed to interrupt the user's Agent task.
            self.last_error = type(exc).__name__

    def attach(self, bus: EventBus) -> None:
        self._bus = bus
        if not self.enabled or self._unsubscribe is not None:
            return
        self._unsubscribe = bus.subscribe(self.on_event)
        if self.remote and self._task is None:
            self._task = asyncio.create_task(self._flush_loop())
        self._spawn(self._record(
            "telemetry_started", {"mode": self.config.mode}))

    def detach(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    async def on_event(self, event: AgentEvent) -> None:
        if not self.enabled:
            return
        if isinstance(event, RunStarted):
            self._run_started = time.monotonic()
            self._tool_calls = 0
            self._llm_calls = 0
            self._usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }
            await self._record("run_started")
            return
        if isinstance(event, ToolCallStarted):
            self._tool_started[event.tool_call.id] = (
                event.tool_call.name, time.monotonic())
            return
        if isinstance(event, ToolCallCompleted):
            started = self._tool_started.pop(event.tool_call.id, None)
            elapsed_ms = 0 if started is None else int(
                (time.monotonic() - started[1]) * 1000)
            self._tool_calls += 1
            await self._record("tool_call", {
                "tool_category": tool_category(event.tool_call.name),
                "success": not bool(event.is_error),
                "duration_bucket": duration_bucket(elapsed_ms),
            })
            return
        if isinstance(event, LLMResponseReceived):
            self._llm_calls += 1
            usage = event.usage or {}
            for key in self._usage:
                try:
                    self._usage[key] += int(usage.get(key, 0) or 0)
                except (TypeError, ValueError):
                    pass
            await self._record("llm_response", {
                "input_tokens_bucket": token_bucket(usage.get("input_tokens")),
                "output_tokens_bucket": token_bucket(usage.get("output_tokens")),
                "total_tokens_bucket": token_bucket(usage.get("total_tokens")),
                "cache_read_tokens_bucket": token_bucket(
                    usage.get("cache_read_tokens")),
                "cache_write_tokens_bucket": token_bucket(
                    usage.get("cache_write_tokens")),
            })
            return
        if isinstance(event, RunFinished):
            elapsed = 0 if self._run_started is None else int(
                (time.monotonic() - self._run_started) * 1000)
            await self._record("run_finished", {
                "outcome": "success" if event.final_text else "empty",
                "duration_bucket": duration_bucket(elapsed),
                "tool_calls": self._tool_calls,
                "llm_calls": self._llm_calls,
                "input_tokens_bucket": token_bucket(
                    self._usage["input_tokens"]),
                "output_tokens_bucket": token_bucket(
                    self._usage["output_tokens"]),
                "total_tokens_bucket": token_bucket(
                    self._usage["total_tokens"]),
                "answer_chars_bucket": char_bucket(len(event.final_text or "")),
            })
            self._run_started = None
            return
        if isinstance(event, RunCancelled):
            await self._record("run_cancelled")
            self._run_started = None
            return
        if isinstance(event, ContextCompressed):
            await self._record("context_compacted", {
                "before_tokens_bucket": token_bucket(event.before_tokens),
                "after_tokens_bucket": token_bucket(event.after_tokens),
                "strategy": safe_model_name(event.strategy) or "unknown",
            })
            return
        if isinstance(event, ApprovalRequested):
            await self._record("approval_requested", {
                "tool_category": tool_category(event.tool_call.name)})
            return
        if isinstance(event, PermissionDenied):
            reason = event.reason or ""
            if "企业策略" in reason or "managed" in reason.lower():
                category = "managed"
            elif "用户拒绝" in reason:
                category = "user_denied"
            elif "sandbox" in reason.lower() or "沙箱" in reason:
                category = "sandbox"
            else:
                category = "policy"
            await self._record("permission_denied", {"category": category})
            return
        if isinstance(event, ErrorOccurred):
            await self._record("error", {
                "error_category": error_category(event.error)})

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.config.flush_interval)
                if self.remote:
                    await self.flush()
            except asyncio.CancelledError:
                break
            except Exception:
                continue

    def _transport(self) -> TelemetryTransport:
        if self.transport is None:
            self.transport = HttpTelemetryTransport(
                self.config.endpoint,
                timeout=self.config.request_timeout,
                user_agent=f"krelcode-telemetry/{__version__}",
            )
        return self.transport

    async def flush(self, *, max_batches: int = 20) -> int:
        if not self.remote:
            return 0
        sent = 0
        async with self._flush_lock:
            for _ in range(max(1, int(max_batches))):
                batch = await asyncio.to_thread(
                    self.store.take_batch, self.config.batch_size)
                if not batch:
                    break
                try:
                    await self._transport().send(batch)
                except (TelemetryTransportError, OSError, ValueError) as exc:
                    self.last_error = type(exc).__name__
                    break
                ids = tuple(str(item.get("event_id", "")) for item in batch)
                await asyncio.to_thread(self.store.acknowledge, ids)
                sent += len(batch)
                if len(batch) < self.config.batch_size:
                    break
        return sent

    def enable(
        self,
        mode: str = "local",
        *,
        endpoint: str = "",
        sample_rate: Optional[float] = None,
    ) -> TelemetryPreferences:
        if not self.policy_allowed:
            raise TelemetryConfigError("企业策略禁止遥测")
        normalized_mode = str(mode or "local").strip().lower()
        chosen_endpoint = endpoint or self.config.endpoint
        if normalized_mode == "remote":
            chosen_endpoint = validate_endpoint(chosen_endpoint)
        preferences = self.preferences_store.save(
            mode=normalized_mode,
            endpoint=chosen_endpoint,
            sample_rate=(
                self.config.sample_rate if sample_rate is None else sample_rate),
        )
        self.config = replace(
            self.config,
            mode=preferences.mode,
            endpoint=preferences.endpoint,
            sample_rate=preferences.sample_rate,
        )
        self._sampled_in = self._sample_decision()
        if self._bus is not None:
            if self.enabled:
                self.attach(self._bus)
            else:
                self.detach()
        return preferences

    def disable(self) -> TelemetryPreferences:
        preferences = self.preferences_store.save(mode="off")
        self.config = replace(self.config, mode="off", endpoint="")
        self.detach()
        return preferences

    def clear(self) -> bool:
        return self.store.clear()

    def export(self, destination: Path) -> Path:
        return self.store.export(destination)

    def summary(self) -> dict[str, Any]:
        return self.store.summary()

    def status(self) -> dict[str, Any]:
        store_status = self.store.status()
        return {
            "schema_version": 1,
            "mode": self.config.mode,
            "provider": self.provider,
            "model": self.model,
            "run_mode": self.run_mode,
            "effective_enabled": self.enabled,
            "policy_allowed": self.policy_allowed,
            "sampled_in": self._sampled_in,
            "sample_rate": self.config.sample_rate,
            "endpoint_configured": bool(self.config.endpoint),
            "flush_interval_seconds": self.config.flush_interval,
            "retention_days": self.config.retention_days,
            "queue": store_status.as_dict(),
            "last_error": self.last_error,
            "content_collection": False,
        }

    async def close(self) -> None:
        self.detach()
        if self._background:
            pending = tuple(self._background)
            await asyncio.gather(*pending, return_exceptions=True)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.remote:
            with contextlib.suppress(Exception):
                await self.flush()


def build_telemetry_runtime(
    config: Optional[Mapping[str, Any]],
    *,
    provider: str = "",
    model: str = "",
    run_mode: str = "repl",
    policy_allowed: bool = True,
    home: Optional[Path] = None,
    transport: Optional[TelemetryTransport] = None,
) -> TelemetryRuntime:
    config_error = ""
    try:
        telemetry_config = TelemetryConfig.from_mapping(config, home=home)
    except TelemetryConfigError as exc:
        config_error = f"{type(exc).__name__}: {exc}"
        home_path = Path(home or Path.home()).expanduser()
        root = home_path / ".krelcode" / "telemetry"
        telemetry_config = TelemetryConfig(
            queue_path=root / "queue.jsonl",
            install_id_path=root / "install_id",
            preferences_path=root / "consent.json",
        )
    return TelemetryRuntime(
        telemetry_config,
        provider=provider,
        model=model,
        run_mode=run_mode,
        policy_allowed=policy_allowed,
        transport=transport,
        initial_error=config_error,
    )


__all__ = ["TelemetryRuntime", "build_telemetry_runtime"]
