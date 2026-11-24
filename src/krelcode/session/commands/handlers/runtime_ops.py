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

__all__ = [
    "_format_skill_details",
    "_ok",
    "_parse_prompt_arguments",
    "_prompt_release_actor",
    "_prompt_release_lines",
    "_result_from_render",
    "_template_for_command",
    "handle_agents",
    "handle_allow",
    "handle_commands",
    "handle_compact",
    "handle_context",
    "handle_cost",
    "handle_custom_command",
    "handle_extensions",
    "handle_jobs",
    "handle_lsp",
    "handle_mcp",
    "handle_mcp_prompt_command",
    "handle_model",
    "handle_output_style",
    "handle_permissions",
    "handle_plugin_command",
    "handle_plugins",
    "handle_prompt_release",
    "handle_skill",
    "handle_skill_command",
    "handle_skills",
    "handle_statusline",
    "handle_telemetry",
    "handle_tools",
    "handle_undo",
    "handle_web",
]
