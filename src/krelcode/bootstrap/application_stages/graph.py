"""Final declarative assembly of the application RuntimeGraph."""

from __future__ import annotations

from ..composition import CoreRuntime, ExtensionRuntime, RuntimeGraph
from .context_services import ContextServices
from .core import CoreServices
from .extensions import ExtensionServices
from .interactions import InteractionServices
from .session import SessionServices


def build_runtime_graph(
    core: CoreServices,
    context: ContextServices,
    extensions: ExtensionServices,
    session: SessionServices,
    interactions: InteractionServices,
) -> RuntimeGraph:
    """Assemble runtime-ready records without performing service I/O."""
    return RuntimeGraph(
        core=CoreRuntime(
            runtime=core.runtime,
            supervisor=core.supervisor,
            checkpoints=session.checkpoints,
            usage=session.usage,
            model_switch=session.model_switch,
            redactor=core.redactor,
            input_processor=context.input_processor,
            approval_broker=interactions.approval_broker,
            input_broker=interactions.input_broker,
            hooks=context.hooks,
            telemetry_runtime=session.telemetry_runtime,
            auditor=core.auditor,
            loop=core.loop,
        ),
        extensions=ExtensionRuntime(
            plugin_manager=extensions.plugin_manager,
            skill_manager=extensions.skill_manager,
            agent_definitions=extensions.agent_definitions,
            extension_registry=extensions.extension_registry,
            custom_commands=extensions.custom_commands,
            refresh=extensions.refresh,
            reload_skills=extensions.reload_skills,
            reload_plugins=extensions.reload_plugins,
            reload_custom_commands=extensions.reload_custom_commands,
        ),
    )


__all__ = ["build_runtime_graph"]
