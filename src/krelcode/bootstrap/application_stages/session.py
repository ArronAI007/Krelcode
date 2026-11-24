"""Session-scoped checkpoints, usage, model switching, and telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ...agent.usage import UsageTracker
from ...session.autocommit import AutoCommitter
from ...session.checkpoint import CheckpointManager
from ...session.commands.ports import ModelSwitchPort
from ...telemetry import TelemetryRuntime, build_telemetry_runtime
from ..composition import build_model_switcher
from .context import ApplicationBuildContext
from .core import CoreServices


@dataclass(frozen=True)
class SessionServices:
    """Services owned by one process-level conversation session."""

    checkpoints: Optional[CheckpointManager]
    usage: UsageTracker
    model_switch: Optional[ModelSwitchPort]
    telemetry_runtime: TelemetryRuntime


async def build_session_services(
    context: ApplicationBuildContext,
    core: CoreServices,
) -> SessionServices:
    """Attach session resources after all runtime and extension ports exist."""
    loop = core.loop
    checkpoints: Optional[CheckpointManager] = None
    if not context.args.no_checkpoint:
        checkpoints = CheckpointManager(loop.tool_context.workspace_root)
        checkpoints.attach(loop.bus)

    usage = UsageTracker(model=getattr(loop.llm, "model", None))
    usage.attach(loop.bus)
    model_switch = build_model_switcher(
        context.args,
        context.config,
        loop,
        usage,
        subagents=core.subagents,
        redactor=core.redactor,
        enterprise_policy=context.enterprise_policy,
    )

    if context.args.auto_commit:
        committer = AutoCommitter(loop.tool_context.workspace_root)
        if await committer.is_git_repo():
            await committer.ensure_local_exclude()
            loop.bus.subscribe(committer.on_event)
            context.note(
                "[git] 自动提交已开启（每轮有变更时 commit；"
                ".krelcode/ 已本地排除）")
        else:
            context.note("[git] 工作区不是 git 仓库，--auto-commit 未生效")

    telemetry_runtime = build_telemetry_runtime(
        context.config.get("telemetry"),
        provider=str(getattr(loop.llm, "name", "") or ""),
        model=str(getattr(loop.llm, "model", "") or ""),
        run_mode=context.run_mode.value,
        policy_allowed=context.enterprise_policy.allow_telemetry,
    )
    loop.update_runtime_state(telemetry=telemetry_runtime)
    telemetry_runtime.attach(loop.bus)

    return SessionServices(
        checkpoints=checkpoints,
        usage=usage,
        model_switch=model_switch,
        telemetry_runtime=telemetry_runtime,
    )


__all__ = ["SessionServices", "build_session_services"]
