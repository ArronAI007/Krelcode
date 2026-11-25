"""Hot-reloadable extension command, hook, skill, agent and plugin wiring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Tuple, cast

from ...agent.turn.runtime_services import (
    CommandServicePort,
    ExtensionRegistryServicePort,
    PluginServicePort,
    SkillServicePort,
)

from ...agent.prompt_surface_catalog import refresh_prompt_surfaces
from ...extensions import ExtensionRecord, ExtensionRegistry, replace_runtime_extensions
from ...plugins import PluginManager, PluginRecord
from ...session.commands import (
    sync_custom_commands,
    sync_mcp_prompt_commands,
    sync_plugin_commands,
    sync_skill_commands,
)
from ...session.commands.custom import CustomCommandManager
from ...session.commands.ports import SkillReloadPort
from ...session.restore import refresh_skill_system_prompt
from ...skills import SkillManager, SkillReloadReport
from ...subagent.definition import AgentDefinitionManager
from .context import ApplicationBuildContext
from .context_services import ContextServices
from .core import CoreServices


@dataclass(frozen=True)
class ExtensionServices:
    """Extension managers plus their single refresh/reload contract."""

    plugin_manager: PluginManager
    skill_manager: SkillManager
    agent_definitions: AgentDefinitionManager
    extension_registry: ExtensionRegistry
    custom_commands: CustomCommandManager
    refresh: Callable[[], Tuple[ExtensionRecord, ...]]
    reload_skills: SkillReloadPort
    reload_plugins: Callable[[], Awaitable[Tuple[PluginRecord, ...]]]
    reload_custom_commands: Callable[[], CustomCommandManager]


def build_extension_services(
    context: ApplicationBuildContext,
    core: CoreServices,
    context_services: ContextServices,
) -> ExtensionServices:
    """Synchronize extension surfaces and install their stable reload ports."""
    loop = core.loop
    supervisor = core.supervisor
    registry = context_services.input_processor.command_registry
    custom_commands = cast(CustomCommandManager, loop.custom_command_manager)

    loop.command_registry = cast(CommandServicePort, registry)
    sync_custom_commands(registry, custom_commands)
    sync_plugin_commands(registry, loop.plugin_manager)
    sync_skill_commands(registry, loop.skill_manager)
    sync_mcp_prompt_commands(registry, supervisor)

    plugin_manager = cast(PluginManager, loop.plugin_manager)
    skill_manager = cast(SkillManager, loop.skill_manager)
    agent_definitions = cast(AgentDefinitionManager, loop.agent_definitions)
    extension_registry = cast(ExtensionRegistry, loop.extension_registry)

    def refresh_extensions() -> Tuple[ExtensionRecord, ...]:
        result = replace_runtime_extensions(
            extension_registry,
            plugins=plugin_manager,
            skills=skill_manager,
            agents=agent_definitions,
            tools=loop.registry,
            commands=registry,
            hooks=context_services.hooks,
            mcp_servers=core.config.merged_mcp_config(),
            mcp_supervisor=supervisor,
        )
        refresh_prompt_surfaces(
            loop,
            hooks=context_services.hooks,
            mcp_supervisor=supervisor,
        )
        return result

    loop.extension_refresh = refresh_extensions

    async def refresh_mcp_prompt_commands() -> None:
        sync_mcp_prompt_commands(registry, supervisor)
        refresh_extensions()

    supervisor.on_prompts_changed = refresh_mcp_prompt_commands
    refresh_extensions()

    def reload_skills() -> SkillReloadReport:
        report = skill_manager.reload()
        refresh_skill_system_prompt(loop)
        sync_skill_commands(registry, loop.skill_manager)
        refresh_extensions()
        return report

    def reload_custom_commands() -> CustomCommandManager:
        custom_commands.reload()
        sync_custom_commands(registry, custom_commands)
        refresh_extensions()
        return custom_commands

    loop.custom_commands_reload = reload_custom_commands

    base_plugin_reload = cast(
        Callable[[], Awaitable[Tuple[PluginRecord, ...]]],
        loop.plugin_reload,
    )

    async def reload_plugins() -> Tuple[PluginRecord, ...]:
        records = await base_plugin_reload()
        context_services.refresh_hooks()
        if not context.args.no_mcp:
            await supervisor.sync(core.config.merged_mcp_config())
            await supervisor.refresh_prompts()
        sync_plugin_commands(registry, loop.plugin_manager)
        sync_mcp_prompt_commands(registry, supervisor)
        refresh_extensions()
        return records

    loop.plugin_reload = reload_plugins
    return ExtensionServices(
        plugin_manager=plugin_manager,
        skill_manager=skill_manager,
        agent_definitions=agent_definitions,
        extension_registry=extension_registry,
        custom_commands=custom_commands,
        refresh=refresh_extensions,
        reload_skills=cast(SkillReloadPort, reload_skills),
        reload_plugins=reload_plugins,
        reload_custom_commands=reload_custom_commands,
    )


__all__ = ["ExtensionServices", "build_extension_services"]
