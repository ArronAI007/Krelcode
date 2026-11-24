"""Build a fully wired application object without owning process lifecycle."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ..agent.prompt_surface_catalog import refresh_prompt_surfaces
from ..agent.runtime import AgentRuntime
from ..agent.usage import UsageTracker
from ..config import load_config_layers, load_managed_config
from ..core.errors import ConfigError
from ..core.messages import Message
from ..core.redact import SecretRedactor
from ..enterprise.audit import AuditError, ComplianceAuditor
from ..enterprise.auth import AuthError, OIDCClient, load_sso_config
from ..enterprise.policy import (
    EnterprisePolicyError,
    PolicyViolation,
    load_enterprise_policy,
)
from ..extensions import replace_runtime_extensions
from ..hooks import HookRunner
from ..llm.base import collect
from ..mcp.supervisor import MCPSupervisor
from ..safety.sandbox import SandboxError
from ..session.autocommit import AutoCommitter
from ..session.checkpoint import CheckpointManager
from ..session.commands import (
    sync_custom_commands,
    sync_mcp_prompt_commands,
    sync_plugin_commands,
    sync_skill_commands,
)
from ..session.elicitation_bridge import MCPElicitationBridge
from ..session.input import InputProcessor
from ..session.interactions import UserInputBroker
from ..session.restore import refresh_skill_system_prompt
from ..session.runner import run_session
from ..telemetry import build_telemetry_runtime
from ..ui.approval import ApprovalBroker
from ..ui.repl import run_repl
from ..contracts.tool import RunMode
from .composition import (
    CoreRuntime,
    ExtensionRuntime,
    RuntimeGraph,
    build_loop,
    build_mcp_roots,
    build_mcp_sampler,
    build_model_switcher,
    compose_hook_specs,
    compose_mcp_config,
    resolve_run_mode,
)


async def create_application(
    args: argparse.Namespace,
) -> "BootstrappedApplication | int":
    input_format = getattr(args, "input_format", "text")
    if input_format == "stream-json":
        if args.message is not None:
            print("--input-format stream-json 不能与 -m/--message 同时使用",
                  file=sys.stderr)
            return 2
        if args.output_format != "stream-json":
            print("--input-format stream-json 需要 --output-format stream-json",
                  file=sys.stderr)
            return 2
    if (
        args.output_format == "stream-json"
        and args.message is None
        and input_format != "stream-json"
    ):
        print("--output-format stream-json 需要 -m 或 --input-format stream-json",
              file=sys.stderr)
        return 2

    quiet = (
        args.message is not None
        and args.output_format in {"json", "stream-json"}
    ) or input_format == "stream-json"
    note = (lambda msg: print(msg, file=sys.stderr)) if quiet else print
    config_path = Path(args.config).expanduser() if args.config else None
    try:
        config_bundle = load_config_layers(
            workspace=Path(args.workspace),
            explicit_path=config_path,
        )
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    if config_path is not None and not config_bundle.sources:
        print(f"配置错误: 指定的配置文件不存在: {config_path}", file=sys.stderr)
        return 2
    config = config_bundle.data
    current_config = config
    try:
        # ``--config`` is intentionally an exclusive user configuration.  It
        # must not become an enterprise-policy bypass, so managed policy is
        # loaded independently in that case.
        if config_path is not None:
            managed_config = load_managed_config(
                workspace=Path(args.workspace))
            enterprise_policy = load_enterprise_policy(managed_config)
        else:
            enterprise_policy = load_enterprise_policy(
                config, config_bundle=config_bundle)
        enterprise_policy.validate_cli(args)
        sso_client = None
        principal = None
        sso_config = load_sso_config(enterprise_policy)
        if sso_config.enabled:
            sso_client = OIDCClient(sso_config)
            status = sso_client.status()
            if enterprise_policy.require_sso:
                principal = await sso_client.require_authenticated()
            elif status.authenticated:
                principal = status.principal
    except (ConfigError, EnterprisePolicyError, PolicyViolation, AuthError) as exc:
        print(f"企业策略/SSO 错误: {exc}", file=sys.stderr)
        return 2
    run_mode = resolve_run_mode(args)
    editor_stream = getattr(args, "input_format", "text") == "stream-json"
    approval_broker = (
        ApprovalBroker()
        if (run_mode is RunMode.REPL or editor_stream)
        and not getattr(args, "no_sandbox", False)
        else None
    )
    input_broker = (
        UserInputBroker()
        if run_mode is RunMode.REPL or editor_stream
        else None
    )
    try:
        loop, subagents = await build_loop(
            args,
            config,
            log=note,
            approver=approval_broker.request if approval_broker is not None else None,
            user_input_port=input_broker,
            enterprise_policy=enterprise_policy,
            principal=principal,
        )
    except SandboxError as e:
        print(f"Sandbox 配置错误: {e}", file=sys.stderr)
        return 2
    except (EnterprisePolicyError, PolicyViolation, AuthError) as e:
        print(f"企业策略/SSO 错误: {e}", file=sys.stderr)
        return 2
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2
    custom_commands = loop.custom_command_manager
    binder = loop.runtime_service_binder
    redactor = binder.redactor
    binder.add_secret(getattr(loop.llm, "api_key", None))
    if sso_client is not None:
        binder.add_secrets(sso_client.redaction_secrets())
    auditor = None
    if enterprise_policy.audit and bool(enterprise_policy.audit.get("enabled", False)):
        try:
            auditor = ComplianceAuditor.from_config(
                enterprise_policy.audit,
                workspace=loop.tool_context.workspace_root,
                organization=enterprise_policy.organization,
                principal=(principal.subject if principal is not None else ""),
                policy_version=enterprise_policy.policy_version,
                redactor=redactor,
            )
            auditor.attach(loop.bus)
            loop.auditor = auditor
        except AuditError as exc:
            print(f"企业审计错误: {exc}", file=sys.stderr)
            return 2
    elicitor = (
        MCPElicitationBridge(input_broker, loop.bus.emit, redactor=redactor)
        if input_broker is not None else None
    )
    supervisor = MCPSupervisor(
        loop.registry, log=note,
        sampler=build_mcp_sampler(loop.llm),
        roots=build_mcp_roots(loop.tool_context.workspace_root),
        elicitor=elicitor,
        redactor=redactor,
        prompt_surface_registry=loop.prompt_surface_registry)
    loop.mcp_supervisor = supervisor
    def merged_mcp_config(source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        active = current_config if source is None else source
        return compose_mcp_config(
            active, dict(loop.plugin_manager.mcp_servers))

    if not args.no_mcp:
        await supervisor.sync(merged_mcp_config())
        loop.sync_context_attachments(include_mcp=True)

    async def reload_mcp():
        """REPL /mcp reload：重读分层配置 → diff 同步 → 刷新 Prompt。"""
        nonlocal current_config
        new_bundle = load_config_layers(
            workspace=Path(args.workspace),
            explicit_path=config_path,
        )
        current_config = new_bundle.data
        await supervisor.sync(merged_mcp_config(new_bundle.data))
        restarted = await supervisor.check_and_restart()
        await supervisor.refresh_prompts()
        loop.sync_context_attachments(include_mcp=True)
        refresh = getattr(loop, "extension_refresh", None)
        if refresh is not None:
            refresh()
        return restarted

    runtime = AgentRuntime(
        loop,
        subagents=subagents,
        mcp_supervisor=supervisor,
        mcp_reload=reload_mcp,
    )
    checkpoints = None
    if not args.no_checkpoint:
        checkpoints = CheckpointManager(loop.tool_context.workspace_root)
        checkpoints.attach(loop.bus)
    usage = UsageTracker(model=getattr(loop.llm, "model", None))
    usage.attach(loop.bus)
    model_switch = build_model_switcher(
        args, config, loop, usage, subagents=subagents, redactor=redactor,
        enterprise_policy=enterprise_policy)

    def merged_hook_specs(source: Optional[Dict[str, Any]] = None) -> list[Any]:
        active = current_config if source is None else source
        return compose_hook_specs(
            active, loop.plugin_manager.hook_definitions)

    hooks = None

    def ensure_hooks(specs: list[Any]) -> None:
        nonlocal hooks
        if hooks is None:
            hooks = HookRunner(
                loop.tool_context.workspace_root, specs, log=note,
                redactor=redactor,
                prompt_runner=run_prompt_hook,
                prompt_surface_registry=loop.prompt_surface_registry)
            hooks.attach(loop.bus)
        else:
            hooks.replace_specs(specs)
        loop.hook_port = hooks
        subagents.hook_port = hooks
        if specs:
            note(f"[hooks] 已加载 {len(specs)} 个钩子: "
                 + ", ".join(sorted({h.on for h in specs})))

    async def run_prompt_hook(
        prompt: str, _payload: Dict[str, Any],
    ) -> str:
        finished = await collect(loop.llm, [Message.user(prompt)])
        return finished.message.content or ""

    ensure_hooks(merged_hook_specs())

    async def collect_hook_context(prompt: str) -> tuple[str, ...]:
        if hooks is None:
            return ()
        return await hooks.collect_input_context(prompt)

    input_processor = InputProcessor(
        loop.tool_context.workspace_root,
        hook_context_provider=collect_hook_context,
    )
    loop.command_registry = input_processor.command_registry
    sync_custom_commands(input_processor.command_registry, custom_commands)
    sync_plugin_commands(input_processor.command_registry, loop.plugin_manager)
    sync_skill_commands(input_processor.command_registry, loop.skill_manager)
    sync_mcp_prompt_commands(input_processor.command_registry, supervisor)

    def refresh_extensions():
        result = replace_runtime_extensions(
            loop.extension_registry,
            plugins=loop.plugin_manager,
            skills=loop.skill_manager,
            agents=loop.agent_definitions,
            tools=loop.registry,
            commands=input_processor.command_registry,
            hooks=hooks,
            mcp_servers=merged_mcp_config(),
            mcp_supervisor=supervisor,
        )
        refresh_prompt_surfaces(
            loop, hooks=hooks, mcp_supervisor=supervisor)
        return result

    loop.extension_refresh = refresh_extensions
    async def refresh_mcp_prompt_commands() -> None:
        sync_mcp_prompt_commands(input_processor.command_registry, supervisor)
        refresh_extensions()

    supervisor.on_prompts_changed = refresh_mcp_prompt_commands
    refresh_extensions()

    def reload_skills():
        report = loop.skill_manager.reload()
        refresh_skill_system_prompt(loop)
        sync_skill_commands(input_processor.command_registry, loop.skill_manager)
        refresh_extensions()
        return report

    def reload_custom_commands():
        custom_commands.reload()
        sync_custom_commands(input_processor.command_registry, custom_commands)
        refresh_extensions()
        return custom_commands

    loop.custom_commands_reload = reload_custom_commands

    base_plugin_reload = loop.plugin_reload

    async def reload_plugins():
        records = await base_plugin_reload()
        ensure_hooks(merged_hook_specs())
        if not args.no_mcp:
            await supervisor.sync(merged_mcp_config())
            await supervisor.refresh_prompts()
        sync_plugin_commands(input_processor.command_registry, loop.plugin_manager)
        sync_mcp_prompt_commands(input_processor.command_registry, supervisor)
        refresh_extensions()
        return records

    loop.plugin_reload = reload_plugins
    if args.auto_commit:
        committer = AutoCommitter(loop.tool_context.workspace_root)
        if await committer.is_git_repo():
            await committer.ensure_local_exclude()
            loop.bus.subscribe(committer.on_event)
            note("[git] 自动提交已开启（每轮有变更时 commit；.krelcode/ 已本地排除）")
        else:
            note("[git] 工作区不是 git 仓库，--auto-commit 未生效")
    telemetry_runtime = build_telemetry_runtime(
        config.get("telemetry"),
        provider=str(getattr(loop.llm, "name", "") or ""),
        model=str(getattr(loop.llm, "model", "") or ""),
        run_mode=resolve_run_mode(args).value,
        policy_allowed=enterprise_policy.allow_telemetry,
    )
    loop.telemetry = telemetry_runtime
    telemetry_runtime.attach(loop.bus)

    extensions = ExtensionRuntime(
        plugin_manager=loop.plugin_manager,
        skill_manager=loop.skill_manager,
        agent_definitions=loop.agent_definitions,
        extension_registry=loop.extension_registry,
        custom_commands=custom_commands,
        refresh=refresh_extensions,
        reload_skills=reload_skills,
        reload_plugins=reload_plugins,
        reload_custom_commands=reload_custom_commands,
    )
    graph = RuntimeGraph(
        core=CoreRuntime(
            runtime=runtime,
            supervisor=supervisor,
            checkpoints=checkpoints,
            usage=usage,
            model_switch=model_switch,
            redactor=redactor,
            input_processor=input_processor,
            approval_broker=approval_broker,
            input_broker=input_broker,
            hooks=hooks,
            telemetry_runtime=telemetry_runtime,
            auditor=auditor,
            loop=loop,
        ),
        extensions=extensions,
    )
    return BootstrappedApplication(args=args, graph=graph, note=note)


@dataclass
class BootstrappedApplication:
    """A runnable application backed by an explicit :class:`RuntimeGraph`."""

    args: argparse.Namespace
    graph: RuntimeGraph
    note: Any
    _closed: bool = False

    @property
    def runtime(self) -> AgentRuntime:
        return self.graph.core.runtime

    @property
    def supervisor(self) -> Any:
        return self.graph.core.supervisor

    @property
    def checkpoints(self) -> Optional[CheckpointManager]:
        return self.graph.core.checkpoints

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
        return self.graph.core.redactor

    @property
    def input_processor(self) -> Any:
        return self.graph.core.input_processor

    @property
    def approval_broker(self) -> Optional[ApprovalBroker]:
        return self.graph.core.approval_broker

    @property
    def input_broker(self) -> Optional[UserInputBroker]:
        return self.graph.core.input_broker

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
