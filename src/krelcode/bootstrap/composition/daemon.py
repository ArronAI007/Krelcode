"""Daemon worker and compatibility parser composition."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, cast

from krelcode.core.redact import SecretRedactor

from ...agent.prompt.prompt_surface_contract import PromptSurfaceExecutor
from ...agent.turn.runtime_services import MCPServicePort
from ...runtime.execution import (
    LocalRuntimeResourceRegistry,
    LocalTaskRuntime,
    RuntimeResourceAdapter,
)
from ..parser import build_parser as _build_parser
from .mcp import build_mcp_roots, build_mcp_sampler, compose_mcp_config
from .runtime import build_loop

if TYPE_CHECKING:
    from ...daemon.server import DaemonAgentRuntime, DaemonAgentRuntimeRequest


def build_parser() -> argparse.ArgumentParser:
    """Backward-compatible import for tests and embedders.

    CLI ownership now lives in :mod:`krelcode.entrypoints.main`.
    """
    return _build_parser()


async def build_daemon_agent_runtime(
    request: "DaemonAgentRuntimeRequest",
) -> "DaemonAgentRuntime":
    """Composition-root factory for daemon-owned subagent runtimes."""
    from ...daemon.server import DaemonAgentRuntime
    from ...mcp.supervisor import MCPSupervisor

    argv = ["--workspace", str(request.workspace_root)]
    if request.config_path is not None:
        argv += ["--config", str(request.config_path)]
    if request.no_sandbox:
        argv.append("--no-sandbox")
    if request.sandbox_backend:
        argv += ["--sandbox-backend", str(request.sandbox_backend)]
    if request.sandbox_network is True:
        argv.append("--sandbox-network")
    if request.auto_approve:
        argv.append("--auto-approve")
    if request.permission_mode:
        argv += ["--permission-mode", str(request.permission_mode)]
    args = build_parser().parse_args(argv)
    # The daemon owns the concrete local manager; never recursively construct
    # an RPC client that points back at the same daemon process.
    args.daemon_worker = True
    loop, agents = await build_loop(args, request.config, log=request.log)
    redactor = loop.services.core.redactor or SecretRedactor()
    supervisor = None
    if not args.no_mcp:
        supervisor = MCPSupervisor(
            loop.services.core.registry,
            log=request.log,
            sampler=build_mcp_sampler(loop.services.core.llm),
            roots=build_mcp_roots(loop.services.core.tool_context.workspace_root),
            redactor=redactor,
            prompt_surface_registry=loop.services.prompt.surface_registry,
            prompt_surface_executor=PromptSurfaceExecutor(),
        )
        plugins = loop.services.extensions.plugins
        if plugins is None:
            raise RuntimeError("插件服务未初始化")
        await supervisor.sync(
            compose_mcp_config(request.config, dict(plugins.mcp_servers))
        )
        loop.mcp_supervisor = cast(MCPServicePort, supervisor)
        loop.sync_context_attachments(include_mcp=True)
    resources = LocalRuntimeResourceRegistry()
    resources.register(RuntimeResourceAdapter(
        "subagents",
        agents.shutdown,
    ))
    if supervisor is not None:
        resources.register(RuntimeResourceAdapter(
            "mcp",
            supervisor.close_all,
        ))
    lsp = loop.services.extensions.lsp
    if lsp is not None:
        resources.register(RuntimeResourceAdapter(
            "lsp",
            lsp.close,
        ))
    return DaemonAgentRuntime(
        task_runtime=LocalTaskRuntime(
            state_path=(
                request.workspace_root
                / ".krelcode" / "runtime" / "daemon-tasks.json"
            ),
            redactor=redactor,
        ),
        resources=resources,
        bus=loop.services.core.bus,
        agents=agents,
        tool_context=loop.services.core.tool_context,
        redactor=redactor,
        mcp_supervisor=supervisor,
    )
