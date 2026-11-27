"""Core agent, policy, audit, and MCP runtime construction."""

from __future__ import annotations

import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, cast

from ... import __version__
from ...agent.turn.runtime import AgentRuntime
from ...application.acceptance import DefaultAcceptanceEvaluator
from ...config import load_config_layers
from ...contracts.runtime import RunMode
from ...contracts.application import RiskLevel
from ...contracts.change_admission import (
    ChangeApproval,
    ChangeCandidate,
    ChangeType,
    EvaluationCheck,
    EvaluationReport,
    RollbackManifest,
)
from ...core.errors import ConfigError
from ...core.redact import SecretRedactor
from ...enterprise.audit import AuditError, ComplianceAuditor
from ...enterprise.auth import AuthError
from ...enterprise.policy import EnterprisePolicyError, PolicyViolation
from ...observability import ObservabilitySink, RunManifest
from ...observability.models import stable_hash
from ...observability.settings import ObservabilityOptions
from ...mcp.supervisor import MCPSupervisor
from ...plugins import PluginManager
from ...runtime.execution import LocalTaskRuntime
from ...session.memory import MemoryStore
from ...skills import SkillManager
from ...agent.turn.runtime_services import MCPServicePort, RuntimeServiceBinderPort
from ...safety.sandbox import SandboxError
from ...governance.change_pipeline import DefaultAdmissionGate
from ...governance.control_plane import DefaultControlPlane
from ...governance.control_plane.helpers import governance_for_change
from ...webui.service import WebUIOptions, WebUIService
from ...session.elicitation_bridge import MCPElicitationBridge
from ...agent.prompt.prompt_surface_contract import PromptSurfaceExecutor
from ..composition import (
    RuntimeServiceBinder,
    build_loop,
    build_mcp_roots,
    build_mcp_sampler,
    build_runtime_release_manifest,
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
    webui: Optional[WebUIService]
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
        before = {"servers": supervisor.status()}
        candidate = ChangeCandidate(
            change_type=ChangeType.MCP_SERVER,
            target="mcp:reload",
            before=before,
            after={"action": "reload_layered_config"},
            risk=RiskLevel.HIGH,
            affected_artifacts=(
                {"kind": "mcp_catalog"},
                {"kind": "tool_catalog"},
            ),
        )
        admission = DefaultAdmissionGate().admit(
            candidate,
            evaluation=EvaluationReport.from_checks(
                candidate.candidate_id,
                [EvaluationCheck(
                    name="mcp_reload_requested",
                    passed=True,
                    reason="explicit /mcp reload",
                    score=1.0,
                )],
            ),
            approval=ChangeApproval(
                approved=True,
                actor="session-user",
                reason="用户显式执行 /mcp reload",
                scope="mcp-catalog",
            ),
            rollback=RollbackManifest(
                candidate_id=candidate.candidate_id,
                snapshot=before,
                instructions="恢复 before 中的 MCP 配置并重启服务器",
            ),
            governance_decision=governance_for_change(
                candidate, tool_context=loop.tool_context),
        )
        if not admission.admitted:
            context.note(f"[mcp] 热更新未准入: {admission.reason}")
            return ()
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
        acceptance_evaluator=DefaultAcceptanceEvaluator(),
        control_plane=DefaultControlPlane(),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
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
        runtime_release = build_runtime_release_manifest(
            code_version=__version__,
            run_mode=context.run_mode.value,
            loop=loop,
            supervisor=supervisor,
            plugin_manager=cast(PluginManager, loop.plugin_manager),
            skill_manager=cast(SkillManager, loop.skill_manager),
            memory_store=cast(MemoryStore, loop.memory_store),
            enterprise_policy=context.enterprise_policy,
            tool_names=tool_names,
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
            runtime_release=runtime_release,
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
    webui_options = WebUIOptions.from_args_and_config(
        context.args,
        context.config,
        interactive=(
            context.run_mode is RunMode.REPL and sys.stdin.isatty()
        ),
    )
    webui = (
        WebUIService(
            loop.tool_context.workspace_root,
            webui_options,
            note=context.note,
        )
        if webui_options.enabled else None
    )
    return CoreServices(
        loop=loop,
        subagents=subagents,
        runtime=runtime,
        supervisor=supervisor,
        redactor=redactor,
        auditor=auditor,
        observability=observability,
        webui=webui,
        config=composer,
        binder=binder,
    )


__all__ = ["CoreServices", "build_core_services"]
