"""Core agent, policy, audit, and MCP runtime construction."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, cast

from ... import __version__
from ...agent.turn.runtime import AgentRuntime
from ...config import load_config_layers
from ...core.errors import ConfigError
from ...core.redact import SecretRedactor
from ...enterprise.audit import AuditError, ComplianceAuditor
from ...enterprise.auth import AuthError
from ...enterprise.policy import EnterprisePolicyError, PolicyViolation
from ...observability import ObservabilitySink, RunManifest
from ...observability.models import stable_hash
from ...observability.settings import ObservabilityOptions
from ...mcp.supervisor import MCPSupervisor
from ...agent.turn.runtime_services import MCPServicePort, RuntimeServiceBinderPort
from ...safety.sandbox import SandboxError
from ...session.elicitation_bridge import MCPElicitationBridge
from ...agent.prompt.prompt_surface_contract import PromptSurfaceExecutor
from ..composition import (
    RuntimeServiceBinder,
    build_loop,
    build_mcp_roots,
    build_mcp_sampler,
)
from ..composition.model_switcher import SubagentManagerPort
from ...agent.turn.loop import AgentLoop
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
    observability: Optional[ObservabilitySink]
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
    observability: Optional[ObservabilitySink] = None
    options = ObservabilityOptions.from_args_and_config(
        context.args, context.config)
    if options.enabled:
        tool_names = tuple(sorted(loop.registry.names()))
        experiment = context.experiment
        provider_name = str(
            getattr(loop.llm, "name", type(loop.llm).__name__))
        model_name = str(getattr(loop.llm, "model", "") or "")
        run_id = (
            f"{int(time.time())}-{uuid.uuid4().hex[:12]}"
        )
        manifest = RunManifest(
            run_id=run_id,
            started_at=time.time(),
            workspace_root=str(loop.tool_context.workspace_root.resolve()),
            code_version=__version__,
            provider=provider_name,
            model=model_name,
            run_mode=context.run_mode.value,
            permission_mode=loop.tool_context.runtime.permission_mode.value,
            config_hash=stable_hash(context.config),
            tool_catalog_hash=stable_hash(tool_names),
            tool_names=tool_names,
            experiment_id=experiment.profile.id if experiment else "",
            experiment_name=experiment.profile.name if experiment else "",
            experiment_hash=(
                experiment.profile_hash if experiment is not None else ""),
            experiment_knobs=(
                dict(experiment.profile.knobs)
                if experiment is not None else {}),
            metadata={
                "experiment_warnings": list(experiment.warnings)
                if experiment is not None else [],
            },
        )
        observability = ObservabilitySink(
            loop.tool_context.workspace_root,
            manifest,
            redactor,
            queue_size=options.queue_size,
            max_string_chars=options.max_string_chars,
            max_collection_items=options.max_collection_items,
            include_text_previews=options.include_text_previews,
            on_drop=context.note,
        )
        await observability.start()
        observability.attach(loop.bus)
    return CoreServices(
        loop=loop,
        subagents=subagents,
        runtime=runtime,
        supervisor=supervisor,
        redactor=redactor,
        auditor=auditor,
        observability=observability,
        config=composer,
        binder=binder,
    )


__all__ = ["CoreServices", "build_core_services"]
