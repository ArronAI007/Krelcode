"""First-stage runtime composition: policy, context, extensions and tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple, cast

from ._deps import *  # noqa: F401,F403
from ...agent.context.workspace import WorkspaceContext
from ...agent.prompt.prompt_release import ModelProfileCalibration
from ...agent.prompts import PromptProfileResolution
from ...daemon.options import DaemonOptions, resolve_daemon_options
from ...plugins.manager import PluginManager
from ...skills import SkillManager
from .extensions import (
    build_custom_command_manager,
    build_plugin_manager,
    build_skill_manager,
    configured_skill_paths,
)
from .model_switcher import load_prompt_calibrations
from .providers import build_llm
from .security import (
    _configured_features,
    _disabled_tools,
    build_execution_sandbox,
    build_policy,
    resolve_permission_mode,
    resolve_run_mode,
)


@dataclass(frozen=True)
class RuntimeFoundation:
    """Runtime-ready inputs shared by the agent loop and subagent plane."""

    workspace_root: Path
    enterprise_policy: EnterprisePolicy
    bus: EventBus
    llm: LLMProvider
    redactor: SecretRedactor
    context_options: ContextOptions
    rule_loader: RuleLoader
    workspace_context: Optional[WorkspaceContext]
    prompt_authoring_enabled: bool
    prompt_profile_setting: str
    prompt_release_setting: str
    prompt_calibrations: Tuple[ModelProfileCalibration, ...]
    prompt_profile_resolution: PromptProfileResolution
    memory_store: MemoryStore
    base_skill_paths: List[object]
    custom_commands: CustomCommandManager
    plugin_manager: PluginManager
    plugin_tools: List[Tool]
    skill_manager: SkillManager
    skill_tools: List[Tool]
    context_manager: Optional[ContextManager]
    context_budget: Optional[int]
    sandbox: Optional[ExecutionSandbox]
    tool_context: ToolContext
    daemon_options: DaemonOptions
    daemon_owned: bool
    shell_jobs: object
    approval_policy: ApprovalPolicy
    lsp_manager: LSPManager
    web_runtime: WebRuntime
    browser_runtime: BrowserRuntime
    notebook_options: NotebookOptions
    migration_options: MigrationOptions


async def build_runtime_foundation(
    args: argparse.Namespace,
    config: Dict[str, Any],
    *,
    log: Callable[[str], None],
    approver: Optional[Approver],
    principal: Optional[PrincipalPort],
    enterprise_policy: Optional[EnterprisePolicy],
) -> RuntimeFoundation:
    """Build policy, model, context, extension and execution foundations."""
    workspace_root = Path(args.workspace).resolve()
    if enterprise_policy is None:
        policy = load_enterprise_policy(
            load_managed_config(workspace=workspace_root))
    else:
        policy = enterprise_policy
    policy.validate_cli(args)
    if policy.require_sso and principal is None:
        raise PolicyViolation("企业策略要求 SSO，但当前没有有效身份")
    bus = EventBus()
    llm = build_llm(args, config, log=log)
    policy.validate_provider(str(getattr(llm, "name", "") or ""))
    redactor = SecretRedactor()
    redactor.add(getattr(llm, "api_key", None))
    context_options = ContextOptions.from_config(config.get("context"))
    rule_loader = RuleLoader(
        workspace_root,
        limits=context_options.rule_limits(),
        imports_enabled=context_options.rules_imports,
        redactor=redactor,
    )
    workspace_context: Optional[WorkspaceContext] = None
    if not args.no_context:
        workspace_context = await build_workspace_context_bundle(
            workspace_root, options=context_options, rule_loader=rule_loader)
        _log_rule_issues(workspace_context, log)
    prompt_authoring_enabled = prompt_authoring_enabled_from_config(config)
    prompt_profile_setting = prompt_profile_from_config(config)
    prompt_release_setting = prompt_release_id_from_config(config)
    prompt_calibrations = load_prompt_calibrations(config, workspace_root)
    prompt_profile_resolution = resolve_prompt_profile(
        llm,
        getattr(llm, "model", ""),
        configured=prompt_profile_setting,
        release_id=prompt_release_setting,
        calibrations=prompt_calibrations,
    )
    memory_store = MemoryStore(workspace_root, redactor=redactor)
    base_skill_paths = configured_skill_paths(args, config)
    custom_commands = build_custom_command_manager(config, workspace_root)
    plugin_manager = await _build_plugin_plane(
        args, config, workspace_root, policy=policy, log=log)
    plugin_tools = cast(List[Tool], list(plugin_manager.tools))
    skill_manager = build_skill_manager(
        args, config, plugin_paths=list(plugin_manager.skill_paths))
    skill_tools = build_skill_tools(skill_manager) if skill_manager.enabled else []
    _log_extension_issues(plugin_manager, skill_manager, plugin_tools, log)
    context_manager: Optional[ContextManager] = None
    context_budget: Optional[int] = None
    if not args.no_compress:
        context_budget = (
            args.context_tokens
            or default_budget_for(getattr(llm, "model", None))
            or DEFAULT_CONTEXT_BUDGET
        )
        context_manager = ContextManager(
            llm, max_tokens=context_budget, redactor=redactor)
    sandbox = build_execution_sandbox(args, config, workspace_root)
    _log_sandbox(sandbox, log)
    tool_context = ToolContext(
        workspace_root=workspace_root,
        sandbox=sandbox,
        sandbox_enabled=sandbox is not None,
        runtime=RuntimeSnapshot(
            run_mode=resolve_run_mode(args),
            permission_mode=resolve_permission_mode(args, config, policy),
            features=_configured_features(config),
            disabled_tools=_disabled_tools(config),
            managed_tool_allowlist=(
                frozenset(policy.allowed_tools) if policy.allowed_tools else None),
            managed_tool_denylist=(
                frozenset(policy.disabled_tools) if policy.disabled_tools else None),
        ),
        enterprise_policy=policy,
        principal=principal,
    )
    daemon_options = resolve_daemon_options(args, config)
    daemon_owned = daemon_options.enabled and not bool(
        getattr(args, "daemon_worker", False))
    shell_jobs: object
    if daemon_owned:
        from ...daemon.client import DaemonJobManager

        shell_jobs = DaemonJobManager(
            workspace_root,
            config_path=daemon_options.config_path,
            idle_timeout=daemon_options.idle_timeout,
            bus=bus,
            redactor=redactor,
        )
    else:
        shell_jobs = ShellJobManager(
            bus, workspace_root=workspace_root, redactor=redactor)
    approval_policy = build_policy(
        args,
        workspace_root,
        bus,
        approver=approver,
        config=config,
        enterprise_policy=policy,
    )
    lsp_manager = LSPManager(
        workspace_root, options=LSPOptions.from_config(config.get("lsp")))
    web_runtime, browser_runtime, notebook_options, migration_options = (
        _build_feature_runtimes(args, config))
    return RuntimeFoundation(
        workspace_root=workspace_root,
        enterprise_policy=policy,
        bus=bus,
        llm=llm,
        redactor=redactor,
        context_options=context_options,
        rule_loader=rule_loader,
        workspace_context=workspace_context,
        prompt_authoring_enabled=prompt_authoring_enabled,
        prompt_profile_setting=prompt_profile_setting,
        prompt_release_setting=prompt_release_setting,
        prompt_calibrations=prompt_calibrations,
        prompt_profile_resolution=prompt_profile_resolution,
        memory_store=memory_store,
        base_skill_paths=base_skill_paths,
        custom_commands=custom_commands,
        plugin_manager=plugin_manager,
        plugin_tools=plugin_tools,
        skill_manager=skill_manager,
        skill_tools=skill_tools,
        context_manager=context_manager,
        context_budget=context_budget,
        sandbox=sandbox,
        tool_context=tool_context,
        daemon_options=daemon_options,
        daemon_owned=daemon_owned,
        shell_jobs=shell_jobs,
        approval_policy=approval_policy,
        lsp_manager=lsp_manager,
        web_runtime=web_runtime,
        browser_runtime=browser_runtime,
        notebook_options=notebook_options,
        migration_options=migration_options,
    )


async def _build_plugin_plane(
    args: argparse.Namespace,
    config: Dict[str, Any],
    workspace_root: Path,
    *,
    policy: EnterprisePolicy,
    log: Callable[[str], None],
) -> PluginManager:
    try:
        plugin_manager = build_plugin_manager(
            args, config, workspace_root, enterprise_policy=policy)
        await plugin_manager.load()
        return plugin_manager
    except (PluginTrustError, PluginSignatureError) as exc:
        empty_store = PluginTrustStore(
            workspace_root / ".krelcode" / ".plugin-trust-disabled.json",
            load=False,
        )
        log(f"[plugins] 信任库无效，插件系统已 fail-closed 禁用: {exc}")
        return PluginManager(
            workspace_root, enabled=False, trust_store=empty_store)


def _log_rule_issues(
    workspace_context: WorkspaceContext,
    log: Callable[[str], None],
) -> None:
    rule_bundle = workspace_context.rule_bundle
    if rule_bundle is None:
        return
    if rule_bundle.content:
        log(
            f"[rules] 已加载 {rule_bundle.entry_count} 个入口"
            f" / {rule_bundle.import_count} 个导入"
            f"（{rule_bundle.chars} chars）")
    for issue in rule_bundle.issues:
        log(f"[rules] 跳过 {issue.code}: {issue.path}: {issue.message}")


def _log_extension_issues(
    plugin_manager: PluginManager,
    skill_manager: SkillManager,
    plugin_tools: List[Tool],
    log: Callable[[str], None],
) -> None:
    if skill_manager.enabled:
        if skill_manager.list():
            log("[skills] 已加载 "
                + ", ".join(skill.name for skill in skill_manager.list())
                + f"（{len(skill_manager.list())} 个）")
        for skill_issue in skill_manager.issues:
            log(f"[skills] 跳过无效 skill: {skill_issue.path}: {skill_issue.message}")
    if plugin_manager.enabled:
        if plugin_manager.records():
            log("[plugins] 已发现 "
                + ", ".join(
                    f"{record.name}@{record.version}[{record.status}]"
                    for record in plugin_manager.records())
                + f"（{len(plugin_tools)} 个工具）")
        for plugin_issue in plugin_manager.issues:
            log(f"[plugins] 跳过问题: {plugin_issue.path}: {plugin_issue.message}")


def _log_sandbox(
    sandbox: Optional[ExecutionSandbox],
    log: Callable[[str], None],
) -> None:
    if sandbox is None:
        return
    log(
        f"[sandbox] backend={sandbox.backend} "
        f"network={'on' if sandbox.network else 'off'}")
    if sandbox.backend == "static":
        log("[sandbox] 警告：OS 隔离后端不可用，当前为静态护栏模式"
            "（无法抵御解释器中的动态绕过）")


def _build_feature_runtimes(
    args: argparse.Namespace,
    config: Dict[str, Any],
) -> tuple[WebRuntime, BrowserRuntime, NotebookOptions, MigrationOptions]:
    web_options = WebOptions.from_config(
        config.get("web"), config.get("web_search"))
    if getattr(args, "no_web", False):
        web_options = replace(
            web_options, enabled=False, search_enabled=False)
    web_runtime = WebRuntime.from_options(web_options)
    browser_options = BrowserOptions.from_config(config.get("browser"))
    if getattr(args, "no_web", False) or getattr(args, "no_browser", False):
        browser_options = replace(browser_options, enabled=False)
    browser_runtime = BrowserRuntime.from_options(browser_options)
    notebook_options = NotebookOptions.from_config(config.get("notebook"))
    if getattr(args, "no_notebook", False):
        notebook_options = replace(notebook_options, enabled=False)
    migration_options = MigrationOptions.from_config(config.get("database"))
    if getattr(args, "no_database", False):
        migration_options = replace(migration_options, enabled=False)
    return web_runtime, browser_runtime, notebook_options, migration_options


__all__ = ["RuntimeFoundation", "build_runtime_foundation"]
