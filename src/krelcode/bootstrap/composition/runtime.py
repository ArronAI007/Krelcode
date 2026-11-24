"""Agent runtime and model hot-switch composition."""

from __future__ import annotations

from typing import Callable, Protocol, Sequence, Tuple, cast

from ._deps import *  # noqa: F401,F403
from ...agent.prompt.fragments import PromptFragment
from ...agent.prompt.prompt_release import ModelProfileCalibration
from ...contracts.service_ports import PromptFragmentRegistryPort, PromptReleasePipelinePort
from ...core.hooks import HookPort
from ...core.interactions import UserInputPort
from ...extensions import ExtensionRecord
from ...plugins import PluginRecord
from ...subagent.sidechain import SidechainStore
from .extensions import (
    build_custom_command_manager,
    build_plugin_manager,
    build_skill_manager,
    compose_hook_specs,
    configured_skill_paths,
)
from .mcp import build_mcp_roots, build_mcp_sampler, compose_mcp_config
from .memory import resolve_auto_memory_mode, resolve_auto_memory_thresholds
from .providers import build_llm
from .services import (
    PersistenceServices,
    RuntimeServiceBinder,
    RuntimeServices,
)
from .security import (
    _configured_features,
    _disabled_tools,
    build_execution_sandbox,
    build_policy,
    resolve_permission_mode,
    resolve_run_mode,
)
from .subagents import build_subagent_llm_resolver
from .tools import default_tools

class _SubagentManagerPort(Protocol):
    """Common runtime surface shared by local and daemon-backed managers."""

    llm: LLMProvider
    prompt_profile: str
    prompt_release_id: str
    prompt_calibrations: Sequence[ModelProfileCalibration]
    get_parent_history: Optional[Callable[[], List[Message]]]
    cache_ledger_provider: Optional[Callable[[], object | None]]
    hook_port: Optional[HookPort]
    sidechains: object

    def build_tools(self) -> List[Tool]: ...

    async def shutdown(self) -> None: ...


def _default_log(message: str) -> None:
    print(message)



async def build_loop(
    args: argparse.Namespace,
    config: Dict[str, Any],
    extra_tools: Optional[List[Tool]] = None,
    log: Callable[[str], None] = _default_log,
    approver: Optional[Approver] = None,
    user_input_port: Optional[UserInputPort] = None,
    enterprise_policy: Optional[EnterprisePolicy] = None,
    principal: Optional[PrincipalPort] = None,
) -> Tuple[AgentLoop, _SubagentManagerPort]:
    """返回 (AgentLoop, SubagentManager)。

    子代理共享 provider/工作区/审批策略，但工具集由 default_tools() 单独构建
    （不含 spawn 系工具 → 防递归），历史与总线各自独立。
    """
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
    workspace_context = None
    if not args.no_context:
        workspace_context = await build_workspace_context_bundle(
            workspace_root, options=context_options, rule_loader=rule_loader)
        if workspace_context.rule_bundle is not None:
            rule_bundle = workspace_context.rule_bundle
            if rule_bundle.content:
                log(
                    f"[rules] 已加载 {rule_bundle.entry_count} 个入口"
                    f" / {rule_bundle.import_count} 个导入"
                    f"（{rule_bundle.chars} chars）")
            for issue in rule_bundle.issues:
                log(
                    f"[rules] 跳过 {issue.code}: {issue.path}: "
                    f"{issue.message}")
    prompt_authoring_enabled = prompt_authoring_enabled_from_config(config)
    prompt_profile_setting = prompt_profile_from_config(config)
    prompt_release_setting = prompt_release_id_from_config(config)
    prompt_calibrations = _load_prompt_calibrations(config, workspace_root)
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
    try:
        plugin_manager = build_plugin_manager(
            args, config, workspace_root, enterprise_policy=policy)
        await plugin_manager.load()
    except (PluginTrustError, PluginSignatureError) as exc:
        # Trust metadata is security-sensitive: a malformed store disables the
        # plugin plane for this process instead of silently loading untrusted code.
        empty_store = PluginTrustStore(
            workspace_root / ".krelcode" / ".plugin-trust-disabled.json",
            load=False,
        )
        plugin_manager = PluginManager(
            workspace_root, enabled=False, trust_store=empty_store)
        log(f"[plugins] 信任库无效，插件系统已 fail-closed 禁用: {exc}")
    plugin_tools = list(plugin_manager.tools)
    skill_manager = build_skill_manager(
        args, config, plugin_paths=list(plugin_manager.skill_paths))
    skill_tools = build_skill_tools(skill_manager) if skill_manager.enabled else []
    if skill_manager.enabled:
        if skill_manager.list():
            log("[skills] 已加载 "
                + ", ".join(s.name for s in skill_manager.list())
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
    context_manager = None
    context_budget = None
    if not args.no_compress:
        # 预算优先级：显式 --context-tokens > 模型目录推导（窗口×75%）> 兜底默认
        context_budget = (args.context_tokens
                          or default_budget_for(getattr(llm, "model", None))
                          or DEFAULT_CONTEXT_BUDGET)
        context_manager = ContextManager(
            llm, max_tokens=context_budget, redactor=redactor)
    sandbox = build_execution_sandbox(args, config, workspace_root)
    if sandbox is not None:
        log(f"[sandbox] backend={sandbox.backend} "
            f"network={'on' if sandbox.network else 'off'}")
        if sandbox.backend == "static":
            log("[sandbox] 警告：OS 隔离后端不可用，当前为静态护栏模式"
                "（无法抵御解释器中的动态绕过）")
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
                frozenset(policy.allowed_tools)
                if policy.allowed_tools else None),
            managed_tool_denylist=(
                frozenset(policy.disabled_tools)
                if policy.disabled_tools else None),
        ),
        enterprise_policy=policy,
        principal=principal,
    )
    from ...daemon.options import resolve_daemon_options

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
        args, workspace_root, bus, approver=approver, config=config,
        enterprise_policy=policy)
    lsp_manager = LSPManager(
        workspace_root, options=LSPOptions.from_config(config.get("lsp")))
    web_options = WebOptions.from_config(
        config.get("web"), config.get("web_search"))
    if getattr(args, "no_web", False):
        web_options = WebOptions(
            enabled=False, search_enabled=False,
            max_bytes=web_options.max_bytes, timeout=web_options.timeout,
            max_redirects=web_options.max_redirects,
            max_chars=web_options.max_chars,
            allowed_ports=web_options.allowed_ports,
            allow_private=web_options.allow_private,
            search_provider=web_options.search_provider,
            search_api_key_env=web_options.search_api_key_env,
            search_endpoint=web_options.search_endpoint,
            search_max_results=web_options.search_max_results,
        )
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
    builtin_tools = default_tools(
        memory_store,
        lsp_manager=lsp_manager,
        web_runtime=web_runtime,
        browser_runtime=browser_runtime,
        notebook_options=notebook_options,
        migration_options=migration_options,
    )
    if prompt_authoring_enabled:
        builtin_tools.append(PromptLabTool())

    def child_tool_factory() -> List[Tool]:
        tools = (
            default_tools(
                memory_store,
                lsp_manager=lsp_manager,
                web_runtime=web_runtime,
                browser_runtime=browser_runtime,
                notebook_options=notebook_options,
                migration_options=migration_options,
            )
            + skill_tools
            + list(extra_tools or [])
            + list(plugin_manager.tools)
        )
        if prompt_authoring_enabled:
            tools.append(PromptLabTool())
        # MCP is registered asynchronously after loop construction; resolve it
        # lazily so spawned agents see the current connected server catalogue.
        for name in registry.names():
            descriptor = registry.descriptor(name)
            if descriptor is not None and descriptor.source is ToolSource.MCP:
                tool = registry.get(name)
                if tool is not None:
                    tools.append(tool)
        return tools
    registry = ToolRegistry(builtin_tools)
    for tool in skill_tools:
        registry.register(
            tool, ToolDescriptor.from_tool(tool, source=ToolSource.SKILL))
    for tool in extra_tools or []:
        try:
            registry.register(
                tool, ToolDescriptor.from_tool(tool, source=ToolSource.PLUGIN))
        except ValueError as exc:
            log(f"[plugins] 工具注册失败，已隔离: {getattr(tool, 'name', '?')}: {exc}")
    for tool in plugin_tools:
        try:
            registry.register(
                tool, ToolDescriptor.from_tool(tool, source=ToolSource.PLUGIN))
        except ValueError as exc:
            log(f"[plugins] 工具注册失败，已隔离: {tool.name}: {exc}")
    agent_definitions = AgentDefinitionManager(
        workspace_root, extra_paths=plugin_manager.agent_paths)
    if agent_definitions.list():
        log("[agents] 已加载 Agent Definition: "
            + ", ".join(item.name for item in agent_definitions.list()))
    for agent_issue in agent_definitions.issues:
        log(f"[agents] 跳过无效定义: {agent_issue.path}: {agent_issue.message}")
    def subagent_base_prompt(child_llm: LLMProvider) -> str:
        return build_subagent_system_prompt(
            profile=prompt_profile_setting,
            provider=child_llm,
            model=getattr(child_llm, "model", ""),
        )

    def subagent_catalog_fragments() -> Tuple[PromptFragment, ...]:
        fragments: List[PromptFragment] = []
        catalog = skills_fragment(skill_manager.prompt_index())
        if catalog is not None:
            fragments.append(catalog)
        if prompt_authoring_enabled:
            fragments.append(prompt_authoring_fragment())
        return tuple(fragments)

    if daemon_owned:
        from ...daemon.client import DaemonSubagentManager

        daemon_manager = DaemonSubagentManager(
            workspace_root,
            config_path=daemon_options.config_path,
            redactor=redactor,
            idle_timeout=daemon_options.idle_timeout,
        )
        daemon_manager.llm = llm
        daemon_manager.prompt_profile = prompt_profile_setting
        daemon_manager.prompt_release_id = prompt_release_setting
        daemon_manager.prompt_calibrations = tuple(prompt_calibrations)
        manager = cast(_SubagentManagerPort, daemon_manager)
    else:
        manager = cast(_SubagentManagerPort, SubagentManager(
            llm=llm,
            tool_factory=child_tool_factory,
            tool_context=tool_context,
            policy=approval_policy,
            bus=bus,
            redactor=redactor,
            max_steps=args.max_steps,
            context_budget=context_budget,   # 子代理长任务同样受压缩保护
            # Keep the legacy composite view; new assembly splits base (L1) and
            # the skill catalog (L6) into independent source-attributed fragments.
            system_prompt_provider=(
                lambda: skill_manager.apply_to_prompt(
                    subagent_base_prompt(manager.llm))),
            base_prompt_for_llm=subagent_base_prompt,
            prompt_profile=prompt_profile_setting,
            prompt_release_id=prompt_release_setting,
            prompt_calibrations=prompt_calibrations,
            catalog_fragments_provider=subagent_catalog_fragments,
            definitions=agent_definitions,
            memory_store=memory_store,
            llm_resolver=build_subagent_llm_resolver(
                args, config, lambda: manager.llm, redactor,
                enterprise_policy=policy),
            mcp_servers_provider=lambda: tuple(
                compose_mcp_config(config, dict(plugin_manager.mcp_servers))
            ),
        ))
    for tool in manager.build_tools():
        registry.register(
            tool, ToolDescriptor.from_tool(tool, source=ToolSource.SUBAGENT))
    output_style_config = OutputStyleConfig.from_config(
        config.get("output_styles"),
        workspace=workspace_root,
        cli_paths=getattr(args, "output_style_dir", ()) or (),
        cli_active=getattr(args, "output_style", None),
    )
    if getattr(args, "no_output_styles", False):
        output_style_config = OutputStyleConfig(
            enabled=False, active="", paths=output_style_config.paths,
            home=output_style_config.home)
    output_style_manager = OutputStyleManager(
        workspace_root, config=output_style_config)
    prompt_registry = PromptFragmentRegistry(
        base_fragments(prompt_profile_resolution))
    prompt_registry.register(cyber_risk_fragment())
    if memory_store is not None:
        prompt_registry.register(memory_contract_fragment())
    if workspace_context is not None:
        sync_rule_fragments(prompt_registry, workspace_context)
    # v3 keeps Memory out of the system Prompt Registry. It is assembled as a
    # separate, untrusted knowledge layer at request time.
    if context_options.engine != CONTEXT_ENGINE_V3:
        memory_fragment = memory_store.fragment()
        if memory_fragment is not None:
            prompt_registry.register(memory_fragment)
    if prompt_authoring_enabled:
        prompt_registry.register(prompt_authoring_fragment())
    output_style_manager.apply_to_registry(prompt_registry)
    prompt_snapshot = prompt_registry.snapshot()
    # prompt_lab is a read-only execution tool and must inspect the same
    # per-agent Registry used by PromptAssembler. ToolContext is copied for
    # every tool call by AgentLoop, so assigning it here propagates naturally.
    tool_context.prompt_registry = cast(
        PromptFragmentRegistryPort, prompt_registry)
    prompt_release_pipeline: Optional[PromptReleasePipelinePort] = None
    prompt_release_approval_service: Optional[PromptReleaseApprovalService] = None
    if prompt_authoring_enabled:
        release_store = PromptReleaseProposalStore(workspace_root)
        release_pipeline = PromptReleasePipeline(
            lambda: prompt_registry,
            isolation_context=tool_context,
            store=release_store,
        )
        prompt_release_pipeline = cast(
            PromptReleasePipelinePort, release_pipeline)
        prompt_release_approval_service = PromptReleaseApprovalService(
            release_store)
        tool_context.prompt_release_pipeline = prompt_release_pipeline
    directory_rules_enabled = (
        not args.no_context
        and context_options.include_rules
        and context_options.include_directory_rules
    )
    working_context_store = WorkingContextStore(
        workspace_root, redactor=redactor.redact)
    compact_checkpoint_store = (
        None if args.no_checkpoint else CompactCheckpointStore(
            workspace_root, redactor=redactor))
    context_checkpoint_store = (
        None if args.no_checkpoint else ContextCheckpointStore(
            workspace_root, redactor=redactor))
    context_sources = default_context_sources(
        rule_loader if directory_rules_enabled else None,
        working_context_store=working_context_store,
        engine=context_options.engine,
        memory_store=memory_store,
    )
    context_assembler = ContextAssembler(
        context_sources, engine=context_options.engine)
    directory_rule_source = next(
        (source for source in context_sources
         if isinstance(source, DirectoryRulesContextSource)),
        None,
    )
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        tool_context=tool_context,
        policy=approval_policy,
        bus=bus,
        system_prompt=prompt_snapshot.text,
        max_steps=args.max_steps,
        repeat_failure_limit=args.repeat_failure_limit,
        context_manager=context_manager,
        token_budget=args.token_budget,
        context_assembler=context_assembler,
        user_input_port=user_input_port,
        working_context_store=working_context_store,
        compact_checkpoint_store=compact_checkpoint_store,
    )
    if context_manager is not None:
        context_manager.prompt_surface_registry = loop.prompt_surface_registry
    # fork_context 数据源：getter 形式，/resume 重绑定 loop.history 后依然正确
    manager.get_parent_history = lambda: loop.history
    # Subagent instruction inheritance compares rule/policy hashes from the
    # parent's latest real request; history remains intentionally separate.
    manager.cache_ledger_provider = lambda: loop.last_context_cache_ledger
    loop.update_runtime_state(
        prompt_release_pipeline=prompt_release_pipeline,
        prompt_release_approval_service=prompt_release_approval_service,
        skill_manager=skill_manager,
        plugin_manager=plugin_manager,
        custom_command_manager=custom_commands,
        agent_definitions=agent_definitions,
        base_skill_paths=tuple(base_skill_paths),
        prompt_registry=prompt_registry,
        prompt_profile=prompt_profile_resolution.profile,
        prompt_profile_resolution=prompt_profile_resolution,
        prompt_profile_name=prompt_profile_resolution.name,
        prompt_profile_config=prompt_profile_setting,
        prompt_authoring_enabled=prompt_authoring_enabled,
        context_options=context_options,
        context_engine=context_options.engine,
        context_enabled=not args.no_context,
        workspace_context=workspace_context,
        rule_loader=rule_loader,
        directory_rule_source=directory_rule_source,
        lsp_manager=lsp_manager,
        output_style_manager=output_style_manager,
        web_runtime=web_runtime,
        browser_runtime=browser_runtime,
        notebook_options=notebook_options,
        migration_options=migration_options,
        status_line_renderer=StatusLineRenderer(
            StatusLineOptions.from_config(config.get("status_line"))),
    )
    loop.sync_context_attachments(include_mcp=False)
    context_coordinator = None
    if context_checkpoint_store is not None:
        context_coordinator = ContextCheckpointCoordinator(
            loop=loop,
            store=context_checkpoint_store,
            compact_store=compact_checkpoint_store,
            memory_store=memory_store,
            redactor=redactor,
            log=log,
        )
        context_coordinator.attach(bus)
    apply_prompt_snapshot(loop, prompt_snapshot)
    from ...review import review_prompt_surface
    from ...security_review.surfaces import security_review_prompt_surface

    loop.update_runtime_state(
        auxiliary_prompt_surfaces=(
            review_prompt_surface(),
            security_review_prompt_surface(),
        ),
    )
    refresh_prompt_surfaces(loop)

    suggest_threshold, auto_threshold = resolve_auto_memory_thresholds(
        args, config)
    auto_memory = AutoMemoryCoordinator(
        loop,
        memory_store,
        mode=resolve_auto_memory_mode(args, config),
        suggest_threshold=suggest_threshold,
        auto_threshold=auto_threshold,
        on_memory_changed=lambda: sync_memory_fragment(loop, memory_store),
        log=log,
    )
    auto_memory.attach(bus)
    loop.update_runtime_state(auto_memory=auto_memory)

    extension_registry = ExtensionRegistry()
    loop.update_runtime_state(extension_registry=extension_registry)

    def refresh_extensions_basic() -> Tuple[ExtensionRecord, ...]:
        result = replace_runtime_extensions(
            extension_registry,
            plugins=plugin_manager,
            skills=skill_manager,
            agents=agent_definitions,
            tools=registry,
        )
        refresh_prompt_surfaces(loop)
        return result

    refresh_extensions_basic()

    async def reload_plugins() -> Tuple[PluginRecord, ...]:
        old_tool_names = {tool.name for tool in plugin_manager.tools}
        records = await plugin_manager.reload()
        for name in old_tool_names:
            registry.unregister(name)
        for plugin_tool in plugin_manager.tools:
            try:
                registry.register(
                    plugin_tool,
                    ToolDescriptor.from_tool(
                        plugin_tool, source=ToolSource.PLUGIN))
            except ValueError as exc:
                log(f"[plugins] 工具注册失败，已隔离: {plugin_tool.name}: {exc}")
        skill_manager.set_extra_paths(
            [*plugin_manager.skill_paths, *loop.base_skill_paths],
            reload=True,
        )
        agent_definitions.set_extra_paths(
            plugin_manager.agent_paths, reload=True)
        refresh_skill_system_prompt(loop)
        loop.sync_context_attachments(include_mcp=True)
        refresh_extensions_basic()
        return records

    loop.update_runtime_state(
        extension_refresh=refresh_extensions_basic,
        plugin_reload=reload_plugins,
    )
    services = RuntimeServices(
        redactor=redactor,
        enterprise_policy=policy,
        approval_policy=approval_policy,
        principal=principal,
        context_manager=context_manager,
        persistence=PersistenceServices(
            memory_store=memory_store,
            working_context_store=working_context_store,
            compact_checkpoint_store=compact_checkpoint_store,
            context_checkpoint_store=context_checkpoint_store,
            context_checkpoint_coordinator=context_coordinator,
            sidechains=cast(
                Optional[SidechainStore], getattr(manager, "sidechains", None)),
            shell_jobs=shell_jobs,
            auto_memory=auto_memory,
        ),
        rule_loader=rule_loader,
    )
    RuntimeServiceBinder(services).bind(loop=loop, subagents=manager)
    return loop, manager


def build_model_switcher(
    args: argparse.Namespace,
    config: Dict[str, Any],
    loop: AgentLoop,
    usage: UsageTracker,
    subagents: Optional[_SubagentManagerPort] = None,
    redactor: Optional[SecretRedactor] = None,
    enterprise_policy: Optional[EnterprisePolicy] = None,
) -> Callable[[Optional[str]], str]:
    """/model 热切换：返回 callable(new_spec|None) -> 结果描述文本。

    new_spec 支持 "model"（沿用当前 provider）或 "provider/model"。
    切换动作：重建 provider（含重试包装）→ 替换 loop.llm 与压缩器 llm →
    同步 usage.model（成本核算）→ 预算未显式指定时按新模型窗口自适应。
    """
    def current_base() -> str:
        return getattr(loop.llm, "name", "?")

    def switch(new_spec: Optional[str]) -> str:
        if not new_spec:
            model = getattr(loop.llm, "model", "?")
            profile = getattr(loop, "prompt_profile_name", "") or "unknown"
            return (
                f"当前: {current_base()} / {model} · prompt {profile}"
                "（/model <名称> 或 <provider>/<名称> 切换）"
            )
        if "/" in new_spec:
            provider_name, model_name = new_spec.split("/", 1)
        else:
            provider_name, model_name = current_base(), new_spec
        if provider_name == "mock":
            return ("mock 模式请显式指定目标 provider，如: /model openai/gpt-4o-mini")
        try:
            if enterprise_policy is not None:
                enterprise_policy.validate_provider(provider_name)
            settings = resolve_settings(
                provider=provider_name, model=model_name,
                base_url=args.base_url, config=config)
            inner = create_provider(settings)
            if getattr(inner, "api_key", None) is None:
                return f"切换失败: {provider_name} 缺少 API key（设置对应环境变量后重试）"
            provider: LLMProvider = inner
            if args.retries > 0:
                provider = RetryingProvider(
                    inner, RetryPolicy(max_retries=args.retries),
                    on_retry=lambda a, d, e: print(
                        f"[retry] LLM 调用失败（{type(e).__name__}），第 {a} 次重试，{d:g}s 后…"))
        except Exception as e:
            return f"切换失败: {type(e).__name__}: {e}"

        try:
            calibration_workspace = loop.tool_context.workspace_root
            calibration_records = _load_prompt_calibrations(
                config, calibration_workspace)
            profile_resolution = resolve_prompt_profile(
                inner,
                getattr(inner, "model", ""),
                configured=prompt_profile_from_config(config),
                release_id=prompt_release_id_from_config(config),
                calibrations=calibration_records,
            )
        except ConfigError as exc:
            return f"切换失败: {exc}"
        loop.llm = provider
        if subagents is not None:
            subagents.llm = provider   # 之后 spawn 的子代理用新模型
            subagents.prompt_profile = prompt_profile_from_config(config)
            subagents.prompt_release_id = prompt_release_id_from_config(config)
            subagents.prompt_calibrations = calibration_records
        loop.update_runtime_state(
            prompt_profile=profile_resolution.profile,
            prompt_profile_resolution=profile_resolution,
            prompt_profile_name=profile_resolution.name,
        )
        prompt_note = ""
        registry = getattr(loop, "prompt_registry", None)
        if registry is not None:
            replace_base_fragments(
                registry,
                profile_resolution,
                provider=inner,
                model=getattr(inner, "model", ""),
            )
            refresh_registry_snapshot(loop)
            prompt_note = f"，prompt {profile_resolution.name}"
        budget_note = ""
        if loop.context_manager is not None:
            loop.context_manager.llm = provider
            if args.context_tokens is None:
                new_budget = (default_budget_for(getattr(inner, "model", None))
                              or DEFAULT_CONTEXT_BUDGET)
                loop.context_manager.max_tokens = new_budget
                budget_note = f"，压缩预算 {new_budget}"
        usage.model = getattr(inner, "model", None)
        telemetry_runtime = getattr(loop, "telemetry", None)
        if telemetry_runtime is not None:
            telemetry_runtime.set_identity(
                str(provider.name or ""),
                str(getattr(inner, "model", "") or ""),
            )
        # 新 provider 的 key 也纳入脱敏集合（旧 key 保留，避免历史旧内容漏网）
        if redactor is not None:
            redactor.add(getattr(inner, "api_key", None))
        return (
            f"已切换: {provider.name} / {getattr(inner, 'model', '?')}"
            f"{budget_note}{prompt_note}"
        )

    return switch


def _load_prompt_calibrations(
    config: Dict[str, Any],
    workspace: Path,
) -> Tuple[ModelProfileCalibration, ...]:
    calibrations: List[ModelProfileCalibration] = []
    for raw_path in prompt_calibration_paths_from_config(config):
        source = Path(raw_path).expanduser()
        if not source.is_absolute():
            source = Path(workspace).expanduser().resolve() / source
        try:
            baseline = load_prompt_baseline(source)
        except ValueError as exc:
            raise ConfigError(f"Prompt calibration 加载失败: {source}: {exc}") from exc
        calibrations.append(baseline.calibration)
    return tuple(calibrations)

