"""Public application object and process-independent bootstrap facade."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Callable, Optional

from ..agent.turn.loop import AgentLoop
from ..agent.turn.runtime import AgentRuntime
from ..agent.usage import UsageTracker
from ..core.hooks import HookPort
from ..core.redact import SecretRedactor
from ..session.commands.ports import (
    MCPSupervisorPort,
    ModelSwitchPort,
    SkillReloadPort,
)
from ..session.input import InputProcessor
from ..session.runner import run_session
from ..session.runtime_ports import (
    ApprovalBrokerPort,
    CheckpointPort,
    SessionRuntimePorts,
    UserInputBrokerPort,
)
from ..ui.repl import run_repl
from .application_builder import BuiltRuntime, build_application
from .composition import ExtensionRuntime, RuntimeGraph
from .composition.graph import AuditorPort, TelemetryRuntimePort


async def create_application(
    args: argparse.Namespace,
) -> "BootstrappedApplication | int":
    """Build the runtime graph and wrap it in the public application object."""
    built = await build_application(args)
    if isinstance(built, int):
        return built
    return BootstrappedApplication(
        args=built.args, graph=built.graph, note=built.note)


@dataclass
class BootstrappedApplication:
    """A runnable application backed by an explicit :class:`RuntimeGraph`."""

    args: argparse.Namespace
    graph: RuntimeGraph
    note: Callable[[str], None]
    _closed: bool = False

    @property
    def runtime(self) -> AgentRuntime:
        return self.graph.core.runtime

    @property
    def supervisor(self) -> Optional[MCPSupervisorPort]:
        return self.graph.core.supervisor

    @property
    def checkpoints(self) -> Optional[CheckpointPort]:
        return self.graph.core.checkpoints

    @property
    def usage(self) -> UsageTracker:
        return self.graph.core.usage

    @property
    def model_switch(self) -> Optional[ModelSwitchPort]:
        return self.graph.core.model_switch

    @property
    def reload_skills(self) -> SkillReloadPort:
        return self.graph.extensions.reload_skills

    @property
    def redactor(self) -> SecretRedactor:
        return self.graph.core.redactor

    @property
    def input_processor(self) -> Optional[InputProcessor]:
        return self.graph.core.input_processor

    @property
    def approval_broker(self) -> Optional[ApprovalBrokerPort]:
        return self.graph.core.approval_broker

    @property
    def input_broker(self) -> Optional[UserInputBrokerPort]:
        return self.graph.core.input_broker

    @property
    def hooks(self) -> Optional[HookPort]:
        return self.graph.core.hooks

    @property
    def telemetry_runtime(self) -> TelemetryRuntimePort:
        return self.graph.core.telemetry_runtime

    @property
    def auditor(self) -> Optional[AuditorPort]:
        return self.graph.core.auditor

    @property
    def loop(self) -> AgentLoop:
        return self.graph.core.loop

    @property
    def extensions(self) -> ExtensionRuntime:
        return self.graph.extensions

    async def run(self) -> int:
        core = self.graph.core
        if core.webui is not None:
            core.webui.start()
        return await run_session(
            self.args,
            core.runtime,
            SessionRuntimePorts(
                supervisor=core.supervisor,
                checkpoints=core.checkpoints,
                hooks=core.hooks,
                approval=core.approval_broker,
                user_input=core.input_broker,
            ),
            usage=core.usage,
            model_switch=core.model_switch,
            skills_reload=self.graph.extensions.reload_skills,
            redactor=core.redactor,
            input_processor=core.input_processor,
            log=self.note,
            repl_runner=run_repl,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        core = self.graph.core
        if core.webui is not None:
            core.webui.close()
        await core.telemetry_runtime.close()
        if core.observability is not None:
            await core.observability.close()
        if core.auditor is not None:
            core.auditor.detach()
        if core.approval_broker is not None:
            core.approval_broker.close()
        if core.input_broker is not None:
            core.input_broker.close()
        await core.runtime.close()
