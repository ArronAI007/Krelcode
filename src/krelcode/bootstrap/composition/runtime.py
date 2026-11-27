"""Agent runtime and model hot-switch composition."""

from __future__ import annotations

import argparse
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from krelcode.agent.context import CONTEXT_ENGINE_V3
from krelcode.agent.context.context_assembler import ContextAssembler
from krelcode.agent.context.compact_scheduler import BackgroundCompactScheduler
from krelcode.agent.context.context_checkpoint import (
    ContextCheckpointCoordinator,
    ContextCheckpointStore,
)
from krelcode.agent.context.context_source import (
    DirectoryRulesContextSource,
    default_context_sources,
)
from krelcode.agent.context.working_context import WorkingContextStore
from krelcode.agent.prompt.cyber_risk import cyber_risk_fragment
from krelcode.agent.prompt.fragments import (
    PromptFragmentRegistry,
    apply_prompt_snapshot,
    base_fragments,
    skills_fragment,
    sync_rule_fragments,
)
from krelcode.agent.prompt.memory_contract import memory_contract_fragment
from krelcode.agent.prompt.prompt_authoring import prompt_authoring_fragment
from krelcode.agent.prompt.prompt_lab_tool import PromptLabTool
from krelcode.agent.prompt.prompt_release_pipeline import (
    PromptReleaseApprovalService,
    PromptReleasePipeline,
    PromptReleaseProposalStore,
)
from krelcode.agent.prompt.prompt_surface_catalog import refresh_prompt_surfaces
from krelcode.agent.turn.loop import AgentLoop
from krelcode.governance.change_pipeline import DefaultAdmissionGate
from krelcode.agent.turn.runtime_services import (
    BrowserRuntimeServicePort,
    MCPServicePort,
    OutputStyleServicePort,
    PluginServicePort,
    WebRuntimeServicePort,
)
from krelcode.contracts.service_ports import PrincipalPort
from krelcode.contracts.tool import Tool, ToolDescriptor, ToolSource
from krelcode.enterprise.policy import EnterprisePolicy
from krelcode.extensions import ExtensionRegistry, replace_runtime_extensions
from krelcode.llm.base import LLMProvider
from krelcode.output_styles import OutputStyleConfig, OutputStyleManager
from krelcode.safety.policy import Approver
from krelcode.session.compact_checkpoint import CompactCheckpointStore
from krelcode.session.memory import sync_memory_fragment
from krelcode.session.memory_candidates import AutoMemoryCoordinator
from krelcode.session.memory_dream import (
    MemoryDreamCoordinator,
    MemoryDreamScheduler,
    MemoryDreamStore,
)
from krelcode.session.restore import refresh_skill_system_prompt
from krelcode.subagent.definition import AgentDefinitionManager
from krelcode.subagent.manager import SubagentManager
from krelcode.subagent.models import build_subagent_system_prompt
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.status_line import StatusLineOptions, StatusLineRenderer

from ...agent.prompt.fragments import PromptFragment
from ...contracts.service_ports import (
    PromptFragmentRegistryPort,
    PromptReleasePipelinePort,
)
from ...core.interactions import UserInputPort
from ...extensions import ExtensionRecord
from ...plugins import PluginRecord
from ...subagent.sidechain import SidechainStore
from .foundation import build_runtime_foundation
from .mcp import compose_mcp_config
from .memory import resolve_auto_memory_mode, resolve_auto_memory_thresholds
from .model_switcher import SubagentManagerPort as _SubagentManagerPort
from .services import PersistenceServices, RuntimeServiceBinder, RuntimeServices
from .subagents import build_subagent_llm_resolver
from .tools import default_tools


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
    foundation = await build_runtime_foundation(
        args,
        config,
        log=log,
        approver=approver,
        principal=principal,
        enterprise_policy=enterprise_policy,
    )
    workspace_root = foundation.workspace_root
    policy = foundation.enterprise_policy
    bus = foundation.bus
    llm = foundation.llm
    redactor = foundation.redactor
    context_options = foundation.context_options
    rule_loader = foundation.rule_loader
    workspace_context = foundation.workspace_context
    prompt_authoring_enabled = foundation.prompt_authoring_enabled
    prompt_profile_setting = foundation.prompt_profile_setting
    prompt_release_setting = foundation.prompt_release_setting
    prompt_calibrations = foundation.prompt_calibrations
    prompt_profile_resolution = foundation.prompt_profile_resolution
    memory_store = foundation.memory_store
    base_skill_paths = foundation.base_skill_paths
    custom_commands = foundation.custom_commands
    plugin_manager = foundation.plugin_manager
    plugin_tools = foundation.plugin_tools
    skill_manager = foundation.skill_manager
    skill_tools = foundation.skill_tools
    context_manager = foundation.context_manager
    context_budget = foundation.context_budget
    sandbox = foundation.sandbox
    tool_context = foundation.tool_context
    daemon_options = foundation.daemon_options
    daemon_owned = foundation.daemon_owned
    shell_jobs = foundation.shell_jobs
    approval_policy = foundation.approval_policy
    lsp_manager = foundation.lsp_manager
    web_runtime = foundation.web_runtime
    browser_runtime = foundation.browser_runtime
    notebook_options = foundation.notebook_options
    migration_options = foundation.migration_options
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
        registry.register(tool, ToolDescriptor.from_tool(tool, source=ToolSource.SKILL))
    for tool in extra_tools or []:
        try:
            registry.register(
                tool, ToolDescriptor.from_tool(tool, source=ToolSource.PLUGIN)
            )
        except ValueError as exc:
            log(f"[plugins] 工具注册失败，已隔离: {getattr(tool, 'name', '?')}: {exc}")
    for tool in plugin_tools:
        try:
            registry.register(
                tool, ToolDescriptor.from_tool(tool, source=ToolSource.PLUGIN)
            )
        except ValueError as exc:
            log(f"[plugins] 工具注册失败，已隔离: {tool.name}: {exc}")
    agent_definitions = AgentDefinitionManager(
        workspace_root, extra_paths=plugin_manager.agent_paths
    )
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
        manager = cast(
            _SubagentManagerPort,
            SubagentManager(
                llm=llm,
                tool_factory=child_tool_factory,
                tool_context=tool_context,
                policy=approval_policy,
                bus=bus,
                redactor=redactor,
                max_steps=args.max_steps,
                context_budget=context_budget,  # 子代理长任务同样受压缩保护
                # Keep the legacy composite view; new assembly splits base (L1) and
                # the skill catalog (L6) into independent source-attributed fragments.
                system_prompt_provider=(
                    lambda: skill_manager.apply_to_prompt(
                        subagent_base_prompt(manager.llm)
                    )
                ),
                base_prompt_for_llm=subagent_base_prompt,
                prompt_profile=prompt_profile_setting,
                prompt_release_id=prompt_release_setting,
                prompt_calibrations=prompt_calibrations,
                catalog_fragments_provider=subagent_catalog_fragments,
                definitions=agent_definitions,
                memory_store=memory_store,
                llm_resolver=build_subagent_llm_resolver(
                    args,
                    config,
                    lambda: manager.llm,
                    redactor,
                    enterprise_policy=policy,
                ),
                mcp_servers_provider=lambda: tuple(
                    compose_mcp_config(config, dict(plugin_manager.mcp_servers))
                ),
            ),
        )
    for tool in manager.build_tools():
        registry.register(
            tool, ToolDescriptor.from_tool(tool, source=ToolSource.SUBAGENT)
        )
    output_style_config = OutputStyleConfig.from_config(
        config.get("output_styles"),
        workspace=workspace_root,
        cli_paths=getattr(args, "output_style_dir", ()) or (),
        cli_active=getattr(args, "output_style", None),
    )
    if getattr(args, "no_output_styles", False):
        output_style_config = OutputStyleConfig(
            enabled=False,
            active="",
            paths=output_style_config.paths,
            home=output_style_config.home,
        )
    output_style_manager = OutputStyleManager(
        workspace_root, config=output_style_config
    )
    prompt_registry = PromptFragmentRegistry(base_fragments(prompt_profile_resolution))
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
    tool_context.prompt_registry = cast(PromptFragmentRegistryPort, prompt_registry)
    prompt_release_pipeline: Optional[PromptReleasePipelinePort] = None
    prompt_release_approval_service: Optional[PromptReleaseApprovalService] = None
    if prompt_authoring_enabled:
        release_store = PromptReleaseProposalStore(workspace_root)
        release_pipeline = PromptReleasePipeline(
            lambda: prompt_registry,
            isolation_context=tool_context,
            store=release_store,
        )
        prompt_release_pipeline = cast(PromptReleasePipelinePort, release_pipeline)
        prompt_release_approval_service = PromptReleaseApprovalService(
            release_store, admission_gate=DefaultAdmissionGate())
        tool_context.prompt_release_pipeline = prompt_release_pipeline
    directory_rules_enabled = (
        not args.no_context
        and context_options.include_rules
        and context_options.include_directory_rules
    )
    working_context_store = WorkingContextStore(
        workspace_root, redactor=redactor.redact
    )
    compact_checkpoint_store = (
        None
        if args.no_checkpoint
        else CompactCheckpointStore(workspace_root, redactor=redactor)
    )
    context_checkpoint_store = (
        None
        if args.no_checkpoint
        else ContextCheckpointStore(workspace_root, redactor=redactor)
    )
    context_sources = default_context_sources(
        rule_loader if directory_rules_enabled else None,
        working_context_store=working_context_store,
        engine=context_options.engine,
        memory_store=memory_store,
    )
    context_assembler = ContextAssembler(context_sources, engine=context_options.engine)
    directory_rule_source = next(
        (
            source
            for source in context_sources
            if isinstance(source, DirectoryRulesContextSource)
        ),
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
    loop.prompt_release_pipeline = prompt_release_pipeline
    loop.prompt_release_approval_service = prompt_release_approval_service
    loop.skill_manager = skill_manager
    loop.plugin_manager = cast(PluginServicePort, plugin_manager)
    loop.custom_command_manager = custom_commands
    loop.agent_definitions = agent_definitions
    loop.base_skill_paths = tuple(str(path) for path in base_skill_paths)
    loop.prompt_registry = prompt_registry
    loop.prompt_profile = prompt_profile_resolution.profile
    loop.prompt_profile_resolution = prompt_profile_resolution
    loop.prompt_profile_name = prompt_profile_resolution.name
    loop.prompt_profile_config = prompt_profile_setting
    loop.prompt_authoring_enabled = prompt_authoring_enabled
    loop.context_options = context_options
    loop.context_engine = context_options.engine
    loop.context_enabled = not args.no_context
    loop.workspace_context = workspace_context
    loop.rule_loader = rule_loader
    loop.directory_rule_source = directory_rule_source
    loop.lsp_manager = lsp_manager
    loop.output_style_manager = cast(OutputStyleServicePort, output_style_manager)
    loop.web_runtime = cast(WebRuntimeServicePort, web_runtime)
    loop.browser_runtime = cast(BrowserRuntimeServicePort, browser_runtime)
    loop.notebook_options = notebook_options
    loop.migration_options = migration_options
    loop.status_line_renderer = StatusLineRenderer(StatusLineOptions.from_config(config.get('status_line')))
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
    context_config = config.get("context") or {}
    if not isinstance(context_config, dict):
        context_config = {}
    if (
        context_manager is not None
        and bool(context_config.get("background_compact", True))
    ):
        try:
            background_compact_ratio = float(
                context_config.get("background_compact_ratio", 0.72))
        except (TypeError, ValueError):
            background_compact_ratio = 0.72
        compact_scheduler = BackgroundCompactScheduler(
            loop.history,
            context_manager,
            history_provider=lambda: loop.history,
            bus=bus,
            trigger_ratio=background_compact_ratio,
        )
        compact_scheduler.attach(bus)
        loop.compact_scheduler = compact_scheduler
    apply_prompt_snapshot(loop, prompt_snapshot)
    from ...review import review_prompt_surface
    from ...security_review.surfaces import security_review_prompt_surface

    loop.auxiliary_prompt_surfaces = (review_prompt_surface(), security_review_prompt_surface())
    refresh_prompt_surfaces(loop)

    suggest_threshold, auto_threshold = resolve_auto_memory_thresholds(args, config)
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
    loop.auto_memory = auto_memory
    memory_config = config.get("memory") or {}
    if not isinstance(memory_config, dict):
        memory_config = {}
    dream_mode = str(memory_config.get("dream_mode", "suggest")).lower()
    dream_coordinator = MemoryDreamCoordinator(
        memory_store,
        MemoryDreamStore(workspace_root),
    )
    loop.memory_dream = dream_coordinator
    if dream_mode not in {"off", "suggest"}:
        dream_mode = "suggest"
    if dream_mode == "suggest" and resolve_auto_memory_mode(args, config).value != "off":
        try:
            dream_idle_seconds = float(
                memory_config.get("dream_idle_seconds", 30.0))
        except (TypeError, ValueError):
            dream_idle_seconds = 30.0
        dream_scheduler = MemoryDreamScheduler(
            dream_coordinator,
            bus=bus,
            idle_seconds=dream_idle_seconds,
        )
        loop.memory_dream_scheduler = dream_scheduler

    extension_registry = ExtensionRegistry()
    loop.extension_registry = extension_registry

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
                    ToolDescriptor.from_tool(plugin_tool, source=ToolSource.PLUGIN),
                )
            except ValueError as exc:
                log(f"[plugins] 工具注册失败，已隔离: {plugin_tool.name}: {exc}")
        skill_manager.set_extra_paths(
            [*plugin_manager.skill_paths, *loop.base_skill_paths],
            reload=True,
        )
        agent_definitions.set_extra_paths(plugin_manager.agent_paths, reload=True)
        refresh_skill_system_prompt(loop)
        loop.sync_context_attachments(include_mcp=True)
        refresh_extensions_basic()
        return records

    loop.extension_refresh = refresh_extensions_basic
    loop.plugin_reload = reload_plugins
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
                Optional[SidechainStore], getattr(manager, "sidechains", None)
            ),
            shell_jobs=shell_jobs,
            auto_memory=auto_memory,
        ),
        rule_loader=rule_loader,
    )
    RuntimeServiceBinder(services).bind(loop=loop, subagents=manager)
    return loop, manager
