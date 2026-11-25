from __future__ import annotations

import argparse
import asyncio
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from krelcode.core.events import (
    ContextDropped,
    EventBus,
    RunFinished,
    RunStarted,
    ToolCallStarted,
)
from krelcode.core.messages import ToolCall
from krelcode.core.redact import SecretRedactor
from krelcode.observability.experiments import (
    ExperimentError,
    apply_profile,
    profile_from_mapping,
)
from krelcode.observability.journal import ObservabilitySink
from krelcode.observability.models import RunManifest
from krelcode.observability.projection import EventProjector
from krelcode.observability.settings import ObservabilityOptions
from krelcode.webui.store import DashboardStore, safe_run_id


def _manifest(run_id: str = "run-test") -> RunManifest:
    return RunManifest(
        run_id=run_id,
        started_at=1_700_000_000.0,
        workspace_root="/tmp/workspace",
        code_version="0.81.0",
        provider="deepseek",
        model="deepseek-chat",
        run_mode="print",
        permission_mode="default",
        config_hash="config-hash",
        tool_catalog_hash="tool-hash",
    )


def test_projection_redacts_bounds_and_exposes_tool_arguments() -> None:
    secret = "sk-super-secret-value"
    projector = EventProjector(
        "run-1",
        SecretRedactor([secret]),
        max_string_chars=128,
    )
    started = projector.project(
        RunStarted("analyze " + "x" * 500 + " " + secret), 1)
    assert "sk-super-secret-value" not in json.dumps(started.to_dict())
    assert started.payload["input"].endswith("…")

    called = projector.project(
        ToolCallStarted(ToolCall("call-1", "read_file", {
            "path": ".env",
            "api_key": secret,
            "content": "y" * 300,
        })), 2)
    payload = json.dumps(called.to_dict())
    assert "***" in payload
    assert secret not in payload
    assert called.span_id == "call-1"


def test_sink_writes_private_redacted_journal(tmp_path: Path) -> None:
    async def scenario() -> Path:
        redactor = SecretRedactor(["sk-live-secret"])
        sink = ObservabilitySink(
            tmp_path,
            _manifest(),
            redactor,
            queue_size=16,
        )
        bus = EventBus()
        await sink.start()
        sink.attach(bus)
        await bus.emit(RunStarted(
            "token sk-live-secret", turn_id="turn-1", session_id="session-1"))
        await bus.emit(ContextDropped(("old-tool-result",), "budget"))
        await bus.emit(RunFinished("done", turn_id="turn-1"))
        await sink.flush()
        await sink.close()
        return sink.events_path

    events_path = asyncio.run(scenario())
    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert "sk-live-secret" not in events_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(events_path.stat().st_mode) == 0o600
    first = json.loads(lines[0])
    assert first["sequence"] == 1
    assert first["session_id"] == "session-1"
    assert first["turn_id"] == "turn-1"
    manifest = json.loads(
        (events_path.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_id"] == "session-1"


def test_sink_disk_failure_never_escapes_into_event_bus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from krelcode.observability import journal

    def fail_append(path: Path, record: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(journal, "append_jsonl", fail_append)

    async def scenario() -> tuple[int, str]:
        sink = ObservabilitySink(tmp_path, _manifest("run-fail"),
                                 SecretRedactor(), queue_size=4)
        bus = EventBus()
        await sink.start()
        sink.attach(bus)
        await bus.emit(RunStarted("hello"))
        await sink.flush()
        await sink.close()
        return sink.write_failures, sink.last_error

    failures, error = asyncio.run(scenario())
    assert failures == 1
    assert "disk full" in error


def test_experiment_profile_is_whitelisted_and_applied() -> None:
    profile = profile_from_mapping({
        "schema_version": 1,
        "id": "ablation-1",
        "name": "No context",
        "knobs": {
            "context.enabled": False,
            "context.compression": False,
            "context.directory_rules": True,
            "memory.mode": "off",
            "tools.disabled": ["write_file", "edit_file"],
        },
    })
    args = argparse.Namespace(
        no_context=False,
        no_compress=False,
        memory_mode=None,
        no_mcp=False,
    )
    config: dict[str, Any] = {"tools": {"disabled": ["grep"]}}
    applied = apply_profile(profile, args, config)
    assert args.no_context is True
    assert args.no_compress is True
    assert args.memory_mode == "off"
    assert config["tools"]["disabled"] == ["write_file", "edit_file"]
    assert applied.warnings

    with pytest.raises(ExperimentError, match="不允许的实验开关"):
        profile_from_mapping({
            "schema_version": 1,
            "knobs": {"sandbox.network": True},
        })


def test_store_aggregates_events_and_rejects_path_traversal(tmp_path: Path) -> None:
    async def write() -> None:
        sink = ObservabilitySink(tmp_path, _manifest("run-store"),
                                 SecretRedactor(), queue_size=16)
        bus = EventBus()
        await sink.start()
        sink.attach(bus)
        await bus.emit(RunStarted("work", turn_id="turn-1", session_id="s1"))
        await bus.emit(RunFinished("ok", turn_id="turn-1"))
        await sink.flush()
        await sink.close()

    asyncio.run(write())
    store = DashboardStore(tmp_path)
    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0]["run_id"] == "run-store"
    metrics = store.metrics("run-store")
    assert metrics["status"] == "completed"
    assert metrics["event_count"] == 2
    assert len(store.read_events("run-store", after=1)) == 1
    with pytest.raises(ValueError):
        safe_run_id("../escape")


def test_web_auth_origin_and_profile_write_contract(tmp_path: Path) -> None:
    from email.message import Message as HeaderMessage
    from types import SimpleNamespace

    from krelcode.observability.experiments import load_profile, save_profile
    from krelcode.webui.server import DashboardHandler

    token = "test-token"
    handler = object.__new__(DashboardHandler)
    handler.server = SimpleNamespace(token=token)
    handler.headers = HeaderMessage()
    assert handler._query_token_valid({"token": [token]})
    assert not handler._query_token_valid({"token": ["wrong"]})
    handler.headers["Authorization"] = f"Bearer {token}"
    assert handler._authorized({})
    handler.headers.replace_header("Authorization", "Bearer wrong")
    assert not handler._authorized({})

    handler.headers["Host"] = "127.0.0.1:8765"
    handler.headers["Origin"] = "http://127.0.0.1:8765"
    assert handler._same_origin()
    handler.headers.replace_header("Origin", "http://evil.example")
    assert not handler._same_origin()

    profile = profile_from_mapping({
        "schema_version": 1,
        "id": "web-ablation",
        "name": "Web profile",
        "knobs": {"context.enabled": False},
    })
    path = DashboardStore(tmp_path).profile_path
    save_profile(path, profile)
    assert load_profile(path).id == "web-ablation"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_observability_options_are_bounded() -> None:
    options = ObservabilityOptions.from_args_and_config(
        argparse.Namespace(observability=None),
        {"observability": {
            "queue_size": 1,
            "max_string_chars": 999_999_999,
            "max_collection_items": 0,
        }},
    )
    assert options.enabled is True
    assert options.queue_size == 64
    assert options.max_string_chars == 100_000
    assert options.max_collection_items == 1
