"""Compatibility facade for built-in command handler domains.

The historical module remains importable, but handler implementations now live
in cohesive domain modules.  Import through this facade or the domain modules;
``session.commands.handlers`` is intentionally decoupled from this file.
"""

from __future__ import annotations

from .command_ops import (
    handle_agents,
    handle_commands,
    handle_jobs,
    handle_mcp,
    handle_statusline,
    handle_tools,
)
from .configuration_ops import (
    handle_allow,
    handle_cost,
    handle_lsp,
    handle_model,
    handle_output_style,
    handle_permissions,
    handle_undo,
    handle_web,
)
from .context_ops import handle_compact, handle_context
from .extension_ops import (
    _format_skill_details,
    _parse_prompt_arguments,
    handle_custom_command,
    handle_extensions,
    handle_mcp_prompt_command,
    handle_plugin_command,
    handle_plugins,
    handle_skill,
    handle_skill_command,
    handle_skills,
)
from .handler_utils import _ok, _result_from_render, _template_for_command
from .prompt_release_ops import (
    _prompt_release_actor,
    _prompt_release_lines,
    handle_prompt_release,
    handle_telemetry,
)

__all__ = [name for name in globals() if name.startswith("handle_") or name.startswith("_") and name not in {"__all__", "__annotations__", "__builtins__", "__cached__", "__doc__", "__file__", "__loader__", "__name__", "__package__", "__spec__"}]
