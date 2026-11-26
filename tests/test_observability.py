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
    application = store.application("run-store")
    assert application.trigger.value == "webui"
    assert application.metadata["run_id"] == "run-store"
    assert store.get_run("run-store")["application"] == application.to_dict()
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


def test_web_server_connection_errors_never_corrupt_cli_terminal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from krelcode.webui.server import DashboardHTTPServer

    server = object.__new__(DashboardHTTPServer)
    server.handle_error(object(), ("127.0.0.1", 51079))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


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


def test_webui_options_follow_interactive_cli_auto_mode() -> None:
    from krelcode.webui.service import WebUIOptions

    auto = {"webui": {"enabled": "auto"}}
    disabled = WebUIOptions.from_args_and_config(
        argparse.Namespace(webui=None), auto, interactive=False)
    enabled = WebUIOptions.from_args_and_config(
        argparse.Namespace(webui=None), auto, interactive=True)
    forced = WebUIOptions.from_args_and_config(
        argparse.Namespace(webui=True), auto, interactive=False)
    off = WebUIOptions.from_args_and_config(
        argparse.Namespace(webui=False), {"webui": {"enabled": True}},
        interactive=True)
    assert not disabled.enabled
    assert enabled.enabled
    assert forced.enabled
    assert not off.enabled


def test_webui_service_is_background_and_closes_with_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from krelcode.webui import service as service_module

    stopped = threading.Event()

    class FakeServer:
        def __init__(self, address, store, token) -> None:
            self.server_address = ("127.0.0.1", 9876)

        def serve_forever(self, *, poll_interval: float) -> None:
            stopped.wait(2)

        def shutdown(self) -> None:
            stopped.set()

        def server_close(self) -> None:
            return

    monkeypatch.setattr(service_module, "DashboardHTTPServer", FakeServer)
    notes: list[str] = []
    options = service_module.WebUIOptions(
        enabled=True, host="127.0.0.1", port=9876, port_scan=1)
    service = service_module.WebUIService(
        tmp_path, options, note=notes.append, token="test-token")
    assert service.start()
    assert service.running
    assert "token=test-token" in service.url
    service.close()
    assert not service.running
    assert notes == [
        "[webui] 本地控制台: http://127.0.0.1:9876/?token=test-token"]


def test_bootstrapped_application_owns_webui_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from krelcode.bootstrap import application as application_module
    from krelcode.bootstrap.application import BootstrappedApplication
    from krelcode.bootstrap.composition import (
        CoreRuntime,
        ExtensionRuntime,
        RuntimeGraph,
    )

    events: list[str] = []

    class FakeRuntime:
        async def close(self) -> None:
            events.append("runtime.close")

    class FakeTelemetry:
        async def close(self) -> None:
            events.append("telemetry.close")

    class FakeWebUI:
        def start(self) -> bool:
            events.append("webui.start")
            return True

        def close(self) -> None:
            events.append("webui.close")

    async def fake_run_session(*args: object, **kwargs: object) -> int:
        events.append("session.run")
        return 0

    monkeypatch.setattr(application_module, "run_session", fake_run_session)
    graph = RuntimeGraph(
        core=CoreRuntime(
            runtime=FakeRuntime(),
            supervisor=None,
            checkpoints=None,
            usage=object(),
            model_switch=None,
            redactor=object(),
            input_processor=None,
            approval_broker=None,
            input_broker=None,
            hooks=None,
            telemetry_runtime=FakeTelemetry(),
            auditor=None,
            loop=object(),
            webui=FakeWebUI(),
        ),
        extensions=ExtensionRuntime(
            plugin_manager=object(),
            skill_manager=object(),
            agent_definitions=object(),
            extension_registry=object(),
            custom_commands=object(),
            refresh=lambda: (),
            reload_skills=lambda: None,
            reload_plugins=lambda: None,
            reload_custom_commands=lambda: None,
        ),
    )
    app = BootstrappedApplication(
        args=argparse.Namespace(), graph=graph, note=lambda message: None)
    assert asyncio.run(app.run()) == 0
    asyncio.run(app.close())
    assert events == ["webui.start", "session.run", "webui.close", "telemetry.close", "runtime.close"]
