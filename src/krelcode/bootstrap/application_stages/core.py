"""Core agent, policy, audit, and MCP runtime construction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, cast

from ...agent.runtime import AgentRuntime
from ...config import load_config_layers
from ...core.errors import ConfigError
from ...core.redact import SecretRedactor
from ...enterprise.audit import AuditError, ComplianceAuditor
from ...enterprise.auth import AuthError
from ...enterprise.policy import EnterprisePolicyError, PolicyViolation
from ...mcp.supervisor import MCPSupervisor
from ...agent.turn.runtime_services import MCPServicePort, RuntimeServiceBinderPort
from ...safety.sandbox import SandboxError
from ...session.elicitation_bridge import MCPElicitationBridge
from ...agent.prompt_surface_contract import PromptSurfaceExecutor
from ..composition import (
    RuntimeServiceBinder,
    build_loop,
    build_mcp_roots,
    build_mcp_sampler,
)
from ..composition.model_switcher import SubagentManagerPort
from ...agent.loop import AgentLoop
from .context import (
    ApplicationBuildContext,
    BuildStageError,
    RuntimeConfigComposer,
)
from .interactions import InteractionServices


@dataclass(frozen=True)
class CoreServices:
    """Runtime objects that exist for the whole application lifetime."""

    loop: AgentLoop
    subagents: SubagentManagerPort
    runtime: AgentRuntime
    supervisor: MCPSupervisor
    redactor: SecretRedactor
    auditor: Optional[ComplianceAuditor]
    config: RuntimeConfigComposer
    binder: RuntimeServiceBinder


async def build_core_services(
    context: ApplicationBuildContext,
    interactions: InteractionServices,
) -> CoreServices:
    """Build the agent loop, enterprise audit boundary, MCP and AgentRuntime."""
    approval = interactions.approval_broker
    try:
        loop, subagents = await build_loop(
            context.args,
            context.config,
            log=context.note,
            approver=approval.request if approval is not None else None,
            user_input_port=interactions.input_broker,
            enterprise_policy=context.enterprise_policy,
            principal=context.principal,
        )
    except SandboxError as exc:
        raise BuildStageError(f"Sandbox 配置错误: {exc}") from exc
    except (EnterprisePolicyError, PolicyViolation, AuthError) as exc:
        raise BuildStageError(f"企业策略/SSO 错误: {exc}") from exc
    except ConfigError as exc:
        raise BuildStageError(f"配置错误: {exc}") from exc

    binder_port = loop.runtime_service_binder
    if binder_port is None:
        raise BuildStageError("运行时服务绑定器未初始化")
    binder = cast(RuntimeServiceBinder, binder_port)
    redactor = binder.redactor
    binder.add_secret(getattr(loop.llm, "api_key", None))
    if context.sso_client is not None:
        binder.add_secrets(context.sso_client.redaction_secrets())

    auditor: Optional[ComplianceAuditor] = None
    if context.enterprise_policy.audit and bool(
        context.enterprise_policy.audit.get("enabled", False)
    ):
        try:
            auditor = ComplianceAuditor.from_config(
                context.enterprise_policy.audit,
                workspace=loop.tool_context.workspace_root,
                organization=context.enterprise_policy.organization,
                principal=(
                    context.principal.subject
                    if context.principal is not None else ""
                ),
                policy_version=context.enterprise_policy.policy_version,
                redactor=redactor,
            )
            auditor.attach(loop.bus)
            loop.auditor = auditor
        except AuditError as exc:
            raise BuildStageError(f"企业审计错误: {exc}") from exc

    elicitor = (
        MCPElicitationBridge(
            interactions.input_broker,
            loop.bus.emit,
            redactor=redactor,
        )
        if interactions.input_broker is not None else None
    )
    supervisor = MCPSupervisor(
        loop.registry,
        log=context.note,
        sampler=build_mcp_sampler(loop.llm),
        roots=build_mcp_roots(loop.tool_context.workspace_root),
        elicitor=elicitor,
        redactor=redactor,
        prompt_surface_registry=loop.prompt_surface_registry,
        prompt_surface_executor=PromptSurfaceExecutor(),
    )
    loop.mcp_supervisor = cast(MCPServicePort, supervisor)
    composer = RuntimeConfigComposer(loop, context.config)
    if not context.args.no_mcp:
        await supervisor.sync(composer.merged_mcp_config())
        loop.sync_context_attachments(include_mcp=True)

    async def reload_mcp() -> Sequence[str]:
        """Reload layered MCP config, synchronize servers, then refresh prompts."""
        new_bundle = load_config_layers(
            workspace=Path(context.args.workspace),
            explicit_path=context.config_path,
        )
        composer.update(new_bundle.data)
        await supervisor.sync(composer.merged_mcp_config(new_bundle.data))
        restarted = await supervisor.check_and_restart()
        await supervisor.refresh_prompts()
        loop.sync_context_attachments(include_mcp=True)
        refresh = loop.extension_refresh
        if refresh is not None:
            refresh()
        return restarted

    runtime = AgentRuntime(
        loop,
        subagents=subagents,
        mcp_supervisor=supervisor,
        mcp_reload=reload_mcp,
    )
    return CoreServices(
        loop=loop,
        subagents=subagents,
        runtime=runtime,
        supervisor=supervisor,
        redactor=redactor,
        auditor=auditor,
        config=composer,
        binder=binder,
    )


__all__ = ["CoreServices", "build_core_services"]
