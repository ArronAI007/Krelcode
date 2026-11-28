"""Read-only workspace store behind the Web UI API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from krelcode.contracts.application import (
    ApplicationRequest,
    ApplicationRequestFactory,
)
from krelcode.contracts.agent import AgentContract
from krelcode.contracts.runtime import RuntimeReleaseManifest
from krelcode.observability.models import (
    EventEnvelope,
    RunManifest,
)

_SAFE_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def safe_run_id(value: str) -> str:
    text = str(value or "").strip()
    if not text or any(char not in _SAFE_ID_CHARS for char in text):
        raise ValueError("非法 run id")
    return text


class DashboardStore:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.runs_root = (
            self.workspace_root / ".krelcode" / "observability" / "runs")
        self.profile_path = (
            self.workspace_root / ".krelcode" / "observability"
            / "active-experiment.json")

    def list_runs(self, *, limit: int = 200) -> list[dict[str, object]]:
        if not self.runs_root.is_dir():
            return []
        manifests: list[RunManifest] = []
        for path in self.runs_root.glob("*/manifest.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, Mapping):
                    continue
                manifests.append(RunManifest.from_dict(raw))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        manifests.sort(key=lambda item: item.started_at, reverse=True)
        return [
            self._run_summary(manifest)
            for manifest in manifests[:max(1, min(int(limit), 1_000))]
        ]

    def get_run(self, run_id: str) -> dict[str, object]:
        run_id = safe_run_id(run_id)
        manifest = self._manifest(run_id)
        metrics = self.metrics(run_id)
        contract = self.agent_contract(run_id)
        return {
            "manifest": manifest.to_dict(),
            "metrics": metrics,
            "application": self.application(run_id).to_dict(),
            "agent_contract": (
                contract.to_dict() if contract is not None else {}),
        }

    def agent_contract(self, run_id: str) -> Optional[AgentContract]:
        """Read the frozen task contract from a run's start event."""
        run_id = safe_run_id(run_id)
        for event in self.events_after(run_id, 0, limit=1_000):
            if event.name != "run_started":
                continue
            raw = event.payload.get("agent_contract")
            if not isinstance(raw, Mapping) or not raw:
                return None
            try:
                return AgentContract.from_dict(raw)
            except (TypeError, ValueError):
                return None
        return None

    def application(self, run_id: str) -> ApplicationRequest:
        """Read a run's application contract or build a legacy WebUI view."""
        run_id = safe_run_id(run_id)
        manifest = self._manifest(run_id)
        events = self.events_after(run_id, 0, limit=1_000)
        for event in events:
            if event.name != "run_started":
                continue
            raw = event.payload.get("application")
            if isinstance(raw, Mapping) and raw:
                try:
                    return ApplicationRequest.from_mapping(raw)
                except (TypeError, ValueError):
                    break
            input_text = str(event.payload.get("input") or "")
            return ApplicationRequestFactory.from_input_source(
                "webui",
                goal=input_text or "[legacy run without recorded goal]",
                metadata={
                    "run_id": run_id,
                    "legacy": True,
                    "run_mode": manifest.run_mode,
                },
            )
        return ApplicationRequestFactory.from_input_source(
            "webui",
            goal="[run has no application event]",
            metadata={"run_id": run_id, "legacy": True},
        )

    def runtime_release(self, run_id: str) -> RuntimeReleaseManifest:
        release = self._manifest(safe_run_id(run_id)).runtime_release
        if not isinstance(release, RuntimeReleaseManifest):
            raise ValueError(f"run 未记录 RuntimeReleaseManifest: {run_id}")
        return release

    def diff_runtime_releases(
        self,
        left_run_id: str,
        right_run_id: str,
    ) -> dict[str, object]:
        return self.runtime_release(left_run_id).diff(
            self.runtime_release(right_run_id))

    def read_events(
        self,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        path = self._events_path(safe_run_id(run_id))
        if not path.is_file():
            return []
        output: list[dict[str, Any]] = []
        maximum = max(1, min(int(limit), 50_000))
        floor = max(0, int(after))
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if len(output) >= maximum:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                        if not isinstance(raw, Mapping):
                            continue
                        envelope = EventEnvelope.from_dict(raw)
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
                    if envelope.sequence > floor:
                        output.append(envelope.to_dict())
        except OSError:
            return output
        return output

    def events_after(
        self,
        run_id: str,
        after: int,
        *,
        limit: int = 5_000,
    ) -> list[EventEnvelope]:
        return [
            EventEnvelope.from_dict(raw)
            for raw in self.read_events(run_id, after=after, limit=limit)
        ]

    def metrics(self, run_id: str) -> dict[str, object]:
        manifest = self._manifest(run_id)
        events = self.events_after(run_id, 0, limit=50_000)
        counts: dict[str, int] = {}
        input_tokens = 0
        output_tokens = 0
        tool_calls = 0
        errors = 0
        status = "running"
        last_timestamp = manifest.started_at
        for event in events:
            counts[event.kind] = counts.get(event.kind, 0) + 1
            last_timestamp = max(last_timestamp, event.timestamp)
            if event.name == "llm_response_received":
                usage = event.payload.get("usage")
                if isinstance(usage, Mapping):
                    input_tokens += _int(usage.get("input_tokens"))
                    output_tokens += _int(usage.get("output_tokens"))
            elif event.name == "tool_call_completed":
                tool_calls += 1
                if bool(event.payload.get("is_error")):
                    errors += 1
            elif event.name == "run_finished":
                status = "completed"
            elif event.name == "run_cancelled":
                status = "cancelled"
            elif event.name == "error_occurred":
                status = "failed"
                errors += 1
        sequence = events[-1].sequence if events else 0
        gaps = 0
        expected = 1
        for event in events:
            if event.sequence != expected:
                gaps += 1
            expected = event.sequence + 1
        return {
            "run_id": manifest.run_id,
            "status": status,
            "started_at": manifest.started_at,
            "last_event_at": last_timestamp,
            "duration_ms": max(0.0, (last_timestamp - manifest.started_at) * 1000),
            "event_count": len(events),
            "last_sequence": sequence,
            "sequence_gaps": gaps,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "tool_calls": tool_calls,
            "errors": errors,
            "event_kinds": counts,
        }

    def _run_summary(self, manifest: RunManifest) -> dict[str, object]:
        return {
            **manifest.to_dict(),
            "has_events": self._events_path(manifest.run_id).is_file(),
        }

    def _manifest(self, run_id: str) -> RunManifest:
        path = self.runs_root / run_id / "manifest.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError(f"run 不存在: {run_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"run manifest 无法读取: {run_id}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ValueError("run manifest 顶层必须是 object")
        return RunManifest.from_dict(raw)

    def _events_path(self, run_id: str) -> Path:
        return self.runs_root / run_id / "events.jsonl"


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def summarize_event_gaps(events: Iterable[EventEnvelope]) -> int:
    gaps = 0
    expected = 1
    for event in events:
        if event.sequence != expected:
            gaps += 1
        expected = event.sequence + 1
    return gaps


__all__ = ["DashboardStore", "safe_run_id", "summarize_event_gaps"]
