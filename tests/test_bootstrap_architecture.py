from __future__ import annotations

import argparse
import asyncio

import krelcode.app as app
from krelcode.bootstrap import main as bootstrap_main
from krelcode.bootstrap.main import AsyncMain
from krelcode.ui import conversation_host as host_module
from krelcode.ui.repl import run_repl


def test_app_facade_preserves_public_composition_symbols():
    assert callable(app.build_loop)
    assert callable(app.async_main)
    assert callable(app.build_parser)
    assert callable(app.default_tools)


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
