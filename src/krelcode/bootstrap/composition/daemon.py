"""Daemon worker and compatibility parser composition."""

from __future__ import annotations

from ._deps import *  # noqa: F401,F403
from .mcp import build_mcp_roots, build_mcp_sampler, compose_mcp_config
from ..parser import build_parser as _build_parser
from .runtime import build_loop


def build_parser() -> argparse.ArgumentParser:
    """Backward-compatible import for tests and embedders.

    CLI ownership now lives in :mod:`krelcode.entrypoints.main`.
    """
    return _build_parser()


async def build_daemon_agent_runtime(request):
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
    redactor = getattr(loop, "redactor", None) or SecretRedactor()
    supervisor = None
    if not args.no_mcp:
        supervisor = MCPSupervisor(
            loop.registry,
            log=request.log,
            sampler=build_mcp_sampler(loop.llm),
            roots=build_mcp_roots(loop.tool_context.workspace_root),
            redactor=redactor,
            prompt_surface_registry=loop.prompt_surface_registry,
        )
        await supervisor.sync(compose_mcp_config(
            request.config, dict(loop.plugin_manager.mcp_servers)))
        loop.mcp_supervisor = supervisor
        loop.sync_context_attachments(include_mcp=True)
    return DaemonAgentRuntime(
        loop=loop,
        agents=agents,
        mcp_supervisor=supervisor,
    )

