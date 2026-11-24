from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import krelcode.app as app
from krelcode.bootstrap import main as bootstrap_main
from krelcode.bootstrap.main import AsyncMain
from krelcode.session.runtime_ports import SessionRuntimePorts
from krelcode.ui import conversation_host as host_module
from krelcode.ui.repl import run_repl


def test_app_facade_preserves_public_composition_symbols():
    assert callable(app.build_loop)
    assert callable(app.async_main)
    assert callable(app.build_parser)
    assert callable(app.default_tools)


def test_bootstrap_has_explicit_build_phases_and_thin_host():
    from krelcode.bootstrap.application import create_application
    from krelcode.bootstrap.application_builder import (
        BuiltRuntime,
        build_application,
    )
    from krelcode.bootstrap.composition.foundation import (
        RuntimeFoundation,
        build_runtime_foundation,
    )
    from krelcode.bootstrap.composition.model_switcher import (
        build_model_switcher,
    )

    assert callable(create_application)
    assert callable(build_application)
    assert callable(build_runtime_foundation)
    assert callable(build_model_switcher)
    assert BuiltRuntime.__dataclass_fields__
    assert RuntimeFoundation.__dataclass_fields__
    application_source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "krelcode"
        / "bootstrap"
        / "application.py"
    )
    assert len(application_source.read_text(encoding="utf-8").splitlines()) < 200


def test_async_main_owns_only_build_run_close(monkeypatch):
    events = []

    class FakeApplication:
        async def run(self):
            events.append("run")
            return 17

        async def close(self):
            events.append("close")

    async def create_application(args):
        events.append("build")
        return FakeApplication()

    monkeypatch.setattr(bootstrap_main, "create_application", create_application)

    assert asyncio.run(AsyncMain().run(argparse.Namespace())) == 17
    assert events == ["build", "run", "close"]


def test_async_main_closes_when_run_fails(monkeypatch):
    events = []

    class FailingApplication:
        async def run(self):
            events.append("run")
            raise RuntimeError("boom")

        async def close(self):
            events.append("close")

    async def create_application(args):
        return FailingApplication()

    monkeypatch.setattr(bootstrap_main, "create_application", create_application)

    try:
        asyncio.run(AsyncMain().run(argparse.Namespace()))
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:  # pragma: no cover - defensive
        raise AssertionError("expected runtime failure")
    assert events == ["run", "close"]


def test_run_repl_is_a_thin_conversation_host_adapter(monkeypatch):
    captured = {}

    class FakeHost:
        def __init__(self, loop, **kwargs):
            captured["loop"] = loop
            captured.update(kwargs)

        async def run(self):
            return 9

    monkeypatch.setattr(host_module, "ConversationHost", FakeHost)
    loop = object()

    assert asyncio.run(run_repl(loop, input_fn=lambda _prompt: "")) == 9
    assert captured["loop"] is loop
    assert captured["input_fn"] is not None


def test_bootstrapped_application_uses_runtime_graph_for_run_and_close(monkeypatch):
    from krelcode.bootstrap import application as application_module
    from krelcode.bootstrap.composition import (
        CoreRuntime,
        ExtensionRuntime,
        RuntimeGraph,
    )
    from krelcode.bootstrap.application import BootstrappedApplication

    events = []

    class FakeRuntime:
        async def close(self):
            events.append("runtime.close")

    class FakeTelemetry:
        async def close(self):
            events.append("telemetry.close")

    class FakeAuditor:
        def detach(self):
            events.append("auditor.detach")

    class FakeBroker:
        def close(self):
            events.append("broker.close")

    graph = RuntimeGraph(
        core=CoreRuntime(
            runtime=FakeRuntime(),
            supervisor=object(),
            checkpoints=object(),
            usage=object(),
            model_switch=object(),
            redactor=object(),
            input_processor=object(),
            approval_broker=FakeBroker(),
            input_broker=FakeBroker(),
            hooks=object(),
            telemetry_runtime=FakeTelemetry(),
            auditor=FakeAuditor(),
            loop=object(),
        ),
        extensions=ExtensionRuntime(
            plugin_manager=object(),
            skill_manager=object(),
            agent_definitions=object(),
            extension_registry=object(),
            custom_commands=object(),
            refresh=lambda: None,
            reload_skills=lambda: None,
            reload_plugins=lambda: None,
            reload_custom_commands=lambda: None,
        ),
    )
    application = BootstrappedApplication(
        args=argparse.Namespace(name="test"),
        graph=graph,
        note=lambda _message: None,
    )

    async def fake_run_session(args, runtime, ports, **kwargs):
        assert isinstance(ports, SessionRuntimePorts)
        events.append(("run", args.name, runtime, kwargs["skills_reload"]))
        return 23

    monkeypatch.setattr(application_module, "run_session", fake_run_session)

    async def drive():
        assert await application.run() == 23
        await application.close()
        await application.close()

    asyncio.run(drive())
    assert events[0] == (
        "run", "test", graph.core.runtime, graph.extensions.reload_skills)
    assert events[1:] == [
        "telemetry.close",
        "auditor.detach",
        "broker.close",
        "broker.close",
        "runtime.close",
    ]
    assert application.runtime is graph.core.runtime
    assert application.extensions is graph.extensions


def test_application_builder_runs_declarative_stages_in_order(monkeypatch):
    from krelcode.bootstrap import application_builder as builder_module

    events = []
    created = {}
    graph = object()

    class FakeContext:
        note = staticmethod(lambda _message: None)

    class FakeInteractions:
        pass

    class FakeCore:
        pass

    class FakeContextServices:
        pass

    class FakeExtensions:
        pass

    class FakeSession:
        pass

    async def build_context(args):
        events.append(("context", args))
        created["context"] = FakeContext()
        return created["context"]

    def build_interactions(context):
        events.append(("interactions", context))
        created["interactions"] = FakeInteractions()
        return created["interactions"]

    async def build_core(context, interactions):
        events.append(("core", context, interactions))
        created["core"] = FakeCore()
        return created["core"]

    def build_context_services(context, core):
        events.append(("context_services", context, core))
        created["context_services"] = FakeContextServices()
        return created["context_services"]

    def build_extensions(context, core, context_services):
        events.append(("extensions", context, core, context_services))
        created["extensions"] = FakeExtensions()
        return created["extensions"]

    async def build_session(context, core):
        events.append(("session", context, core))
        created["session"] = FakeSession()
        return created["session"]

    def build_graph(**services):
        events.append(("graph", services))
        return graph

    monkeypatch.setattr(
        builder_module, "build_application_context", build_context)
    monkeypatch.setattr(
        builder_module, "build_interaction_services", build_interactions)
    monkeypatch.setattr(builder_module, "build_core_services", build_core)
    monkeypatch.setattr(
        builder_module, "build_context_services", build_context_services)
    monkeypatch.setattr(
        builder_module, "build_extension_services", build_extensions)
    monkeypatch.setattr(
        builder_module, "build_session_services", build_session)
    monkeypatch.setattr(builder_module, "build_runtime_graph", build_graph)

    args = argparse.Namespace(name="ordered")
    built = asyncio.run(builder_module.build_application(args))

    assert built.graph is graph
    assert [event[0] for event in events] == [
        "context",
        "interactions",
        "core",
        "context_services",
        "extensions",
        "session",
        "graph",
    ]
    graph_services = events[-1][1]
    assert graph_services == {
        "core": created["core"],
        "context": created["context_services"],
        "extensions": created["extensions"],
        "session": created["session"],
        "interactions": created["interactions"],
    }


def test_application_builder_turns_stage_failure_into_exit_code(
    monkeypatch, capsys,
):
    from krelcode.bootstrap import application_builder as builder_module

    async def fail(_args):
        raise builder_module.BuildStageError("invalid bootstrap input")

    monkeypatch.setattr(builder_module, "build_application_context", fail)

    assert asyncio.run(
        builder_module.build_application(argparse.Namespace())) == 2
    assert "invalid bootstrap input" in capsys.readouterr().err


def test_runtime_graph_contracts_have_no_any_fields():
    from krelcode.bootstrap.composition import CoreRuntime, ExtensionRuntime

    for contract in (CoreRuntime, ExtensionRuntime):
        assert all(
            "Any" not in annotation
            for annotation in contract.__annotations__.values()
        )


def test_application_builder_is_a_thin_stage_orchestrator():
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "krelcode"
        / "bootstrap"
        / "application_builder.py"
    )
    lines = source.read_text(encoding="utf-8").splitlines()
    assert len(lines) < 100
