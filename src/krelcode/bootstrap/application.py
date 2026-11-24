"""Public application object and process-independent bootstrap facade."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Callable, Optional, cast

from ..agent.runtime import AgentRuntime
from ..core.redact import SecretRedactor
from ..session.checkpoint import CheckpointManager
from ..session.interactions import UserInputBroker
from ..session.runner import run_session
from ..ui.approval import ApprovalBroker
from ..ui.repl import run_repl
from .application_builder import BuiltRuntime, build_application
from .composition import ExtensionRuntime, RuntimeGraph


async def create_application(
    args: argparse.Namespace,
) -> "BootstrappedApplication | int":
    """Build the runtime graph and wrap it in the public application object."""
    built = await build_application(args)
    if isinstance(built, int):
        return built
    return BootstrappedApplication(
        args=args, graph=built.graph, note=built.note)


@dataclass
class BootstrappedApplication:
    """A runnable application backed by an explicit :class:`RuntimeGraph`."""

    args: argparse.Namespace
    graph: RuntimeGraph
    note: Callable[[str], None]
    _closed: bool = False

    @property
    def runtime(self) -> AgentRuntime:
        return cast(AgentRuntime, self.graph.core.runtime)

    @property
    def supervisor(self) -> Any:
        return self.graph.core.supervisor

    @property
    def checkpoints(self) -> Optional[CheckpointManager]:
        return cast(Optional[CheckpointManager], self.graph.core.checkpoints)

    @property
    def usage(self) -> Any:
        return self.graph.core.usage

    @property
    def model_switch(self) -> Any:
        return self.graph.core.model_switch

    @property
    def reload_skills(self) -> Any:
        return self.graph.extensions.reload_skills

    @property
    def redactor(self) -> SecretRedactor:
        return cast(SecretRedactor, self.graph.core.redactor)

    @property
    def input_processor(self) -> Any:
        return self.graph.core.input_processor

    @property
    def approval_broker(self) -> Optional[ApprovalBroker]:
        return cast(Optional[ApprovalBroker], self.graph.core.approval_broker)

    @property
    def input_broker(self) -> Optional[UserInputBroker]:
        return cast(Optional[UserInputBroker], self.graph.core.input_broker)

    @property
    def hooks(self) -> Any:
        return self.graph.core.hooks

    @property
    def telemetry_runtime(self) -> Any:
        return self.graph.core.telemetry_runtime

    @property
    def auditor(self) -> Any:
        return self.graph.core.auditor

    @property
    def loop(self) -> Any:
        return self.graph.core.loop

    @property
    def extensions(self) -> ExtensionRuntime:
        return self.graph.extensions

    async def run(self) -> int:
        core = self.graph.core
        return await run_session(
            self.args,
            core.runtime,
            supervisor=core.supervisor,
            checkpoints=core.checkpoints,
            usage=core.usage,
            model_switch=core.model_switch,
            skills_reload=self.graph.extensions.reload_skills,
            redactor=core.redactor,
            input_processor=core.input_processor,
            log=self.note,
            repl_runner=run_repl,
            approval_broker=core.approval_broker,
            user_input_broker=core.input_broker,
            hooks=core.hooks,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        core = self.graph.core
        await core.telemetry_runtime.close()
        if core.auditor is not None:
            core.auditor.detach()
        if core.approval_broker is not None:
            core.approval_broker.close()
        if core.input_broker is not None:
            core.input_broker.close()
        if getattr(core.loop, "lsp_manager", None) is not None:
            await core.loop.lsp_manager.close()
        await core.runtime.close()
