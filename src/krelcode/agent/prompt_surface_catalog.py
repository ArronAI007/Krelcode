"""Adapters that populate ``PromptSurfaceRegistry`` from runtime components.

The builders in this module read existing Prompt-owned objects and derive only
metadata, hashes, and token estimates.  They never render Provider messages or
mutate Prompt content.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Mapping, Optional

from .evidence import EvidenceSource, InstructionSource, SourceRole
from .prompt_surface import (
    PromptSurfaceDescriptor,
    PromptSurfaceKind,
    PromptSurfaceRegistry,
    main_prompt_surface,
    prompt_content_hash,
    prompt_structure_hash,
)
from .prompt_surface_contract import (
    COMPACT_SURFACE_CONTRACT,
    MCP_INSTRUCTIONS_SURFACE_CONTRACT,
    MCP_PROMPT_SURFACE_CONTRACT,
    REVIEW_SURFACE_CONTRACT,
    contract_for_hook,
)
from .tokens import estimate_text_tokens
from .workflows import DEFAULT_TASK_WORKFLOW_CATALOG, TaskClassification


MAX_SKILL_SURFACE_BYTES = 1_000_000


def _scope_values(source: str) -> tuple[str, str]:
    normalized = str(source or "").strip().lower()
    if normalized in {"builtin", "managed"}:
        return InstructionSource.MANAGED.value, "managed"
    if normalized == "user":
        return InstructionSource.USER.value, "user"
    if normalized in {"project", "plugin", "custom", "local"}:
        return InstructionSource.PROJECT.value, "project"
    return InstructionSource.PROJECT.value, "project"


def _file_digest_and_size(path: Optional[Path], fallback: str) -> tuple[str, int]:
    if path is None:
        return prompt_content_hash(fallback), len(fallback.encode("utf-8"))
    try:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_SKILL_SURFACE_BYTES:
                    return prompt_content_hash(
                        f"{path}:{fallback}:{size}:oversized"), size
                digest.update(chunk)
        return digest.hexdigest(), size
    except OSError:
        return prompt_content_hash(fallback), len(fallback.encode("utf-8"))


def _tool_prompt_text(spec: object) -> str:
    values = [str(getattr(spec, "description", "") or "")]
    values.append(str(getattr(spec, "instructions", "") or ""))
    values.extend(str(item) for item in (getattr(spec, "when_to_use", ()) or ()))
    values.extend(str(item) for item in (getattr(spec, "when_not_to_use", ()) or ()))
    for example in getattr(spec, "examples", ()) or ():
        values.append(str(getattr(example, "title", "") or ""))
        values.append(str(getattr(example, "description", "") or ""))
        values.append(repr(getattr(example, "input", {}) or {}))
    return "\n".join(value for value in values if value)


def tool_prompt_surfaces(tools: object) -> tuple[PromptSurfaceDescriptor, ...]:
    if tools is None:
        return ()
    entries = getattr(tools, "prompt_entries", None)
    if callable(entries):
        values = tuple(entries())
        candidates = tuple(
            (tool, descriptor, spec) for tool, descriptor, spec in values)
    else:
        candidates = tuple((None, None, spec) for spec in tools)
    surfaces: list[PromptSurfaceDescriptor] = []
    for tool, runtime_descriptor, spec in candidates:
        source = str(getattr(runtime_descriptor, "source", "") or "builtin")
        source_value = getattr(
            getattr(runtime_descriptor, "source", None), "value", source)
        if source_value == "mcp":
            role = SourceRole.EVIDENCE
            authority = EvidenceSource.MCP.value
            trust = "untrusted"
            scope = "server"
        elif source_value == "plugin":
            role = SourceRole.INSTRUCTION
            authority = InstructionSource.PROJECT.value
            trust = "project"
            scope = "project"
        else:
            role = SourceRole.INSTRUCTION
            authority = InstructionSource.MANAGED.value
            trust = "managed"
            scope = "managed"
        record = spec.as_record() if hasattr(spec, "as_record") else {}
        metadata = {
            "runtime_source": source_value,
            "wire_hash": str(getattr(spec, "wire_hash", "") or ""),
            "when_to_use_count": int(record.get("when_to_use_count", 0) or 0),
            "when_not_to_use_count": int(record.get("when_not_to_use_count", 0) or 0),
            "example_count": int(record.get("example_count", 0) or 0),
            "instruction_chars": int(record.get("instruction_chars", 0) or 0),
            "playbook_id": str(getattr(spec, "playbook_id", "") or ""),
            "read_only": bool(getattr(runtime_descriptor, "read_only", False)),
            "mcp_server": str(getattr(runtime_descriptor, "mcp_server", "") or ""),
        }
        security_record = getattr(tool, "security_record", None)
        if source_value == "mcp" and callable(security_record):
            try:
                metadata["security"] = security_record()
            except Exception:
                metadata["security"] = {
                    "trust": "untrusted",
                    "instruction": False,
                    "requires_validation": True,
                }
        surfaces.append(PromptSurfaceDescriptor(
            id=f"tool:{getattr(spec, 'name', '')}",
            kind=PromptSurfaceKind.TOOL,
            source=f"tool:{source_value}:{getattr(spec, 'name', '')}",
            owner="ToolRegistry",
            version=str(getattr(spec, "version", "") or ""),
            role=role,
            authority=authority,
            trust=trust,
            stability="session",
            scope=scope,
            content_hash=str(getattr(spec, "content_hash", "") or prompt_content_hash(
                _tool_prompt_text(spec))),
            token_estimate=estimate_text_tokens(_tool_prompt_text(spec)),
            description=str(getattr(spec, "description", "") or ""),
            capabilities=(
                "model_facing_schema",
                "provider_description",
                "tool_guidance",
            ),
            metadata=metadata,
        ))
    return tuple(surfaces)


def workflow_prompt_surfaces(
    catalog: object = DEFAULT_TASK_WORKFLOW_CATALOG,
) -> tuple[PromptSurfaceDescriptor, ...]:
    definitions = tuple(getattr(catalog, "definitions", ()) or ())
    surfaces: list[PromptSurfaceDescriptor] = []
    for definition in definitions:
        classification = TaskClassification(
            definition.task_type, "medium", ("catalog",))
        rendered = definition.render(classification)
        surfaces.append(PromptSurfaceDescriptor(
            id=f"workflow:{definition.id}",
            kind=PromptSurfaceKind.WORKFLOW,
            source=f"workflow:{definition.id}",
            owner="TaskWorkflowCatalog",
            version=definition.version,
            role=SourceRole.INSTRUCTION,
            authority=InstructionSource.MANAGED.value,
            trust="managed",
            stability="turn",
            scope="managed",
            content_hash=definition.definition_hash,
            token_estimate=estimate_text_tokens(rendered),
            description=definition.objective,
            capabilities=("task_workflow", "turn_only", "non_cacheable"),
            metadata={
                "task_type": definition.task_type.value,
                "steps": len(definition.steps),
                "completion_criteria": len(definition.completion),
                "avoid_rules": len(definition.avoid),
            },
        ))
    return tuple(surfaces)


def subagent_prompt_surfaces(
    manager: object | None,
) -> tuple[PromptSurfaceDescriptor, ...]:
    definitions = tuple(getattr(manager, "list", lambda: ())() or ())
    surfaces: list[PromptSurfaceDescriptor] = []
    for definition in definitions:
        authority, trust = _scope_values(
            str(getattr(definition, "source", "") or "project"))
        surfaces.append(PromptSurfaceDescriptor(
            id=f"subagent:{definition.name}",
            kind=PromptSurfaceKind.SUBAGENT,
            source=f"subagent:{getattr(definition, 'source', '') or 'unknown'}:{definition.name}",
            owner="AgentDefinitionManager",
            role=SourceRole.INSTRUCTION,
            authority=InstructionSource.AGENT_DEFINITION.value,
            trust=trust,
            stability="session",
            scope=str(getattr(definition, "source", "") or authority),
            content_hash=prompt_content_hash(definition.system_prompt),
            token_estimate=estimate_text_tokens(definition.system_prompt),
            description=definition.description,
            capabilities=(
                "isolated_history",
                "instruction_inheritance",
                "prompt_assembler",
            ),
            metadata={
                "source": str(getattr(definition, "source", "") or ""),
                "path": str(getattr(definition, "path", "") or ""),
                "allowed_tools": tuple(getattr(definition, "allowed_tools", ()) or ()),
                "model": str(getattr(definition, "model", "") or ""),
                "permission_mode": str(getattr(definition, "permission_mode", "") or ""),
                "inherit_project_rules": bool(
                    getattr(definition, "inherit_project_rules", True)),
            },
        ))
    return tuple(surfaces)


def output_style_prompt_surfaces(
    manager: object | None,
) -> tuple[PromptSurfaceDescriptor, ...]:
    styles = tuple(getattr(manager, "styles", ()) or ())
    active_name = str(getattr(manager, "active_name", "") or "")
    surfaces: list[PromptSurfaceDescriptor] = []
    for style in styles:
        authority, trust = _scope_values(str(getattr(style, "scope", "") or "user"))
        content = str(getattr(style, "content", "") or "")
        surfaces.append(PromptSurfaceDescriptor(
            id=f"output-style:{style.name}",
            kind=PromptSurfaceKind.OUTPUT_STYLE,
            source=f"output-style:{getattr(style, 'scope', '')}:{style.name}",
            owner="OutputStyleManager",
            role=SourceRole.INSTRUCTION,
            authority=authority,
            trust=trust,
            stability="session",
            scope=str(getattr(style, "scope", "") or authority),
            content_hash=prompt_content_hash(content),
            token_estimate=estimate_text_tokens(content),
            description=str(getattr(style, "description", "") or ""),
            available=bool(content.strip()),
            capabilities=("response_style", "l4_fragment"),
            metadata={
                "active": style.name == active_name,
                "path": str(getattr(style, "path", "") or ""),
                "keep_coding_instructions": bool(
                    getattr(style, "keep_coding_instructions", False)),
            },
        ))
    return tuple(surfaces)


def auxiliary_prompt_surfaces() -> tuple[PromptSurfaceDescriptor, ...]:
    from .compression import SUMMARY_SYSTEM_PROMPT
    from ..review import REVIEW_SYSTEM_PROMPT
    from ..security_review.prompt import SECURITY_REVIEW_SYSTEM_PROMPT
    from ..security_review.report import SECURITY_REVIEW_SURFACE_CONTRACT

    return (
        PromptSurfaceDescriptor(
            id="aux:compact",
            kind=PromptSurfaceKind.COMPACT,
            source="agent.compression",
            owner="ContextManager",
            version="structured_compact_v2",
            role=SourceRole.INSTRUCTION,
            authority=InstructionSource.MANAGED.value,
            trust="managed",
            stability="turn",
            scope="managed",
            content_hash=prompt_content_hash(SUMMARY_SYSTEM_PROMPT),
            token_estimate=estimate_text_tokens(SUMMARY_SYSTEM_PROMPT),
            description="Structured compact JSON handoff contract.",
            capabilities=("json_schema", "summary_merge", "fallback_summary"),
            metadata={"no_tools": True, "bounded_output": True},
            contract=COMPACT_SURFACE_CONTRACT,
        ),
        PromptSurfaceDescriptor(
            id="aux:security-review",
            kind=PromptSurfaceKind.REVIEW,
            source="security_review.runner",
            owner="SecurityReviewRunner",
            version="security-review-v1",
            role=SourceRole.INSTRUCTION,
            authority=InstructionSource.MANAGED.value,
            trust="managed",
            stability="static",
            scope="managed",
            content_hash=prompt_content_hash(SECURITY_REVIEW_SYSTEM_PROMPT),
            token_estimate=estimate_text_tokens(SECURITY_REVIEW_SYSTEM_PROMPT),
            description="Defensive Security Review with a structured report contract.",
            capabilities=(
                "workflow",
                "read_only_tools",
                "json_schema",
                "security_gate",
            ),
            metadata={
                "run_mode": "review",
                "permission_mode": "read-only",
                "workflow": "security-review",
                "structured_report": True,
            },
            contract=SECURITY_REVIEW_SURFACE_CONTRACT,
        ),
        PromptSurfaceDescriptor(
            id="aux:review",
            kind=PromptSurfaceKind.REVIEW,
            source="review.cli",
            owner="ReviewRunner",
            role=SourceRole.INSTRUCTION,
            authority=InstructionSource.MANAGED.value,
            trust="managed",
            stability="static",
            scope="managed",
            content_hash=prompt_content_hash(REVIEW_SYSTEM_PROMPT),
            token_estimate=estimate_text_tokens(REVIEW_SYSTEM_PROMPT),
            description="Read-only git diff review contract.",
            capabilities=("read_only_tools", "structured_verdict"),
            metadata={"permission_mode": "read-only", "max_steps": 12},
            contract=REVIEW_SURFACE_CONTRACT,
        ),
    )


def hook_prompt_surfaces(hooks: object | None) -> tuple[PromptSurfaceDescriptor, ...]:
    specs = tuple(getattr(hooks, "specs", ()) or ())
    surfaces: list[PromptSurfaceDescriptor] = []
    for index, spec in enumerate(specs, 1):
        if str(getattr(spec, "kind", "") or "") != "prompt":
            continue
        source = str(getattr(spec, "source", "") or "config")
        authority, trust = _scope_values(
            "project" if source == "plugin" else "user")
        prompt = str(getattr(spec, "prompt", "") or "")
        trigger = str(getattr(spec, "on", "") or "")
        surfaces.append(PromptSurfaceDescriptor(
            id=f"hook:{trigger}:{index}",
            kind=PromptSurfaceKind.HOOK,
            source=f"hook:{source}:{trigger}:{index}",
            owner="HookRunner",
            role=SourceRole.INSTRUCTION,
            authority=authority,
            trust=trust,
            stability="session",
            scope=source,
            content_hash=prompt_content_hash(prompt),
            token_estimate=estimate_text_tokens(prompt),
            description=f"Prompt Hook for {trigger or 'unknown trigger'}",
            capabilities=("hook_decision", "additional_context", "text_or_json"),
            metadata={
                "trigger": trigger,
                "timeout": float(getattr(spec, "timeout", 0.0) or 0.0),
                "tools": tuple(getattr(spec, "tools", ()) or ()),
                "plugin_root": str(getattr(spec, "plugin_root", "") or ""),
            },
            contract=contract_for_hook(
                float(getattr(spec, "timeout", 0.0) or 60.0)),
        ))
    return tuple(surfaces)


def custom_command_prompt_surfaces(
    manager: object | None,
) -> tuple[PromptSurfaceDescriptor, ...]:
    commands = tuple(getattr(manager, "list", lambda: ())() or ())
    surfaces: list[PromptSurfaceDescriptor] = []
    for command in commands:
        source = str(getattr(command, "source", "") or "project")
        authority, trust = _scope_values(source)
        prompt = str(getattr(command, "prompt", "") or "")
        surfaces.append(PromptSurfaceDescriptor(
            id=f"custom-command:{command.name}",
            kind=PromptSurfaceKind.CUSTOM_COMMAND,
            source=f"custom-command:{source}:{getattr(command, 'path', '')}",
            owner="CustomCommandManager",
            role=SourceRole.INSTRUCTION,
            authority=authority,
            trust=trust,
            stability="session",
            scope=source,
            content_hash=prompt_content_hash(prompt),
            token_estimate=estimate_text_tokens(prompt),
            description=str(getattr(command, "description", "") or ""),
            capabilities=("slash_command", "argument_substitution"),
            metadata={
                "path": str(getattr(command, "path", "") or ""),
                "argument_hint": str(getattr(command, "argument_hint", "") or ""),
                "allowed_tools": tuple(getattr(command, "allowed_tools", ()) or ()),
            },
        ))
    return tuple(surfaces)


def skill_prompt_surfaces(manager: object | None) -> tuple[PromptSurfaceDescriptor, ...]:
    skills = tuple(getattr(manager, "list", lambda: ())() or ())
    surfaces: list[PromptSurfaceDescriptor] = []
    for skill in skills:
        source = str(getattr(skill, "source", "") or "project")
        authority, trust = _scope_values(source)
        path = getattr(skill, "skill_file", None)
        fallback = (
            f"{getattr(skill, 'name', '')}:{getattr(skill, 'description', '')}:"
            f"{getattr(skill, 'version', '')}:{path}")
        content_hash, size = _file_digest_and_size(path, fallback)
        surfaces.append(PromptSurfaceDescriptor(
            id=f"skill:{source}:{skill.name}",
            kind=PromptSurfaceKind.SKILL,
            source=f"skill:{source}:{path}",
            owner="SkillManager",
            version=str(getattr(skill, "version", "") or ""),
            role=SourceRole.INSTRUCTION,
            authority=authority,
            trust=trust,
            stability="session",
            scope=source,
            content_hash=content_hash,
            token_estimate=max(1, size // 4) if size else 0,
            description=str(getattr(skill, "description", "") or ""),
            capabilities=(
                "model_invocable" if not getattr(skill, "disable_model_invocation", False)
                else "model_invocation_disabled",
                "user_invocable" if getattr(skill, "user_invocable", True)
                else "user_invocation_disabled",
                "resources",
            ),
            metadata={
                "path": str(path or ""),
                "argument_hint": str(getattr(skill, "argument_hint", "") or ""),
                "allowed_tools": tuple(getattr(skill, "allowed_tools", ()) or ()),
                "size_bytes": size,
            },
        ))
    return tuple(surfaces)


def plugin_command_prompt_surfaces(
    manager: object | None,
) -> tuple[PromptSurfaceDescriptor, ...]:
    commands = tuple(getattr(manager, "commands", ()) or ())
    surfaces: list[PromptSurfaceDescriptor] = []
    for command in commands:
        prompt = str(getattr(command, "prompt", "") or "")
        plugin_name = str(getattr(command, "plugin_name", "") or "")
        surfaces.append(PromptSurfaceDescriptor(
            id=f"plugin-command:{plugin_name}:{getattr(command, 'local_name', command.name)}",
            kind=PromptSurfaceKind.PLUGIN_COMMAND,
            source=f"plugin:{plugin_name}:{getattr(command, 'path', '')}",
            owner="PluginManager",
            role=SourceRole.INSTRUCTION,
            authority=InstructionSource.PROJECT.value,
            trust="project",
            stability="session",
            scope="project",
            content_hash=prompt_content_hash(prompt),
            token_estimate=estimate_text_tokens(prompt),
            description=str(getattr(command, "description", "") or ""),
            capabilities=("slash_command", "argument_substitution", "plugin"),
            metadata={
                "plugin": plugin_name,
                "path": str(getattr(command, "path", "") or ""),
                "argument_hint": str(getattr(command, "argument_hint", "") or ""),
                "allowed_tools": tuple(getattr(command, "allowed_tools", ()) or ()),
            },
        ))
    return tuple(surfaces)


def mcp_prompt_surfaces(
    supervisor: object | None,
) -> tuple[PromptSurfaceDescriptor, ...]:
    if supervisor is None:
        return ()
    surfaces: list[PromptSurfaceDescriptor] = []
    records = tuple(getattr(supervisor, "prompt_records", lambda: ())() or ())
    for record in records:
        server = str(record.get("server", "") or "")
        name = str(record.get("prompt_name", "") or "")
        description = str(record.get("description", "") or "")
        arguments = tuple(record.get("argument_names", ()) or ())
        surfaces.append(PromptSurfaceDescriptor(
            id=f"mcp-prompt:{server}:{name}",
            kind=PromptSurfaceKind.MCP_PROMPT,
            source=f"mcp:{server}",
            owner="MCPSupervisor",
            role=SourceRole.EVIDENCE,
            authority=EvidenceSource.MCP.value,
            trust="untrusted",
            stability="session",
            scope="server",
            content_hash=prompt_structure_hash({
                "server": server,
                "name": name,
                "description": description,
                "arguments": arguments,
            }),
            token_estimate=0,
            description=description or f"MCP Prompt {name}",
            capabilities=("user_invoked_template", "requires_validation"),
            metadata={
                "server": server,
                "remote_name": name,
                "command_name": str(record.get("command_name", "") or ""),
                "argument_names": arguments,
                "body_loaded_on_invocation": True,
            },
            contract=MCP_PROMPT_SURFACE_CONTRACT,
        ))
    instructions = getattr(supervisor, "server_instructions", None)
    if callable(instructions):
        for server, text in tuple(instructions() or ()):
            surfaces.append(PromptSurfaceDescriptor(
                id=f"mcp-instructions:{server}",
                kind=PromptSurfaceKind.MCP_PROMPT,
                source=f"mcp:{server}:instructions",
                owner="MCPSupervisor",
                role=SourceRole.EVIDENCE,
                authority=EvidenceSource.MCP.value,
                trust="untrusted",
                stability="session",
                scope="server",
                content_hash=prompt_content_hash(text),
                token_estimate=estimate_text_tokens(text),
                description="MCP server instructions injected as evidence.",
                capabilities=("server_instructions", "requires_validation"),
                metadata={"server": server},
                contract=MCP_INSTRUCTIONS_SURFACE_CONTRACT,
            ))
    return tuple(surfaces)


def build_prompt_surface_collections(
    *,
    prompt_snapshot: object | None = None,
    prompt_plan: object | None = None,
    system_prompt: str = "",
    prompt_profile: str = "",
    prompt_release: str = "",
    provider: str = "",
    model: str = "",
    tools: object | None = None,
    workflows: object = DEFAULT_TASK_WORKFLOW_CATALOG,
    agents: object | None = None,
    output_styles: object | None = None,
    hooks: object | None = None,
    custom_commands: object | None = None,
    skills: object | None = None,
    plugins: object | None = None,
    mcp_supervisor: object | None = None,
) -> Mapping[str, tuple[PromptSurfaceDescriptor, ...]]:
    return {
        "main": (main_prompt_surface(
            system_prompt,
            prompt_snapshot=prompt_snapshot,
            prompt_plan=prompt_plan,
            profile=prompt_profile,
            release=prompt_release,
            provider=provider,
            model=model,
        ),),
        "tools": tool_prompt_surfaces(tools),
        "workflows": workflow_prompt_surfaces(workflows),
        "subagents": subagent_prompt_surfaces(agents),
        "auxiliary": auxiliary_prompt_surfaces(),
        "output_styles": output_style_prompt_surfaces(output_styles),
        "hooks": hook_prompt_surfaces(hooks),
        "custom_commands": custom_command_prompt_surfaces(custom_commands),
        "skills": skill_prompt_surfaces(skills),
        "plugin_commands": plugin_command_prompt_surfaces(plugins),
        "mcp_prompts": mcp_prompt_surfaces(mcp_supervisor),
    }


def refresh_prompt_surfaces(
    loop: object,
    *,
    hooks: object | None = None,
    mcp_supervisor: object | None = None,
) -> PromptSurfaceRegistry:
    """Rebuild the runtime catalogue without touching Provider-bound state."""
    registry = getattr(loop, "prompt_surface_registry", None)
    if not isinstance(registry, PromptSurfaceRegistry):
        registry = PromptSurfaceRegistry()
        setattr(loop, "prompt_surface_registry", registry)

    snapshot = getattr(loop, "prompt_snapshot", None)
    system_prompt = str(getattr(snapshot, "text", "") or "")
    if not system_prompt:
        history = tuple(getattr(loop, "history", ()) or ())
        if history and getattr(history[0], "role", "") == "system":
            system_prompt = str(getattr(history[0], "content", "") or "")

    resolution = getattr(loop, "prompt_profile_resolution", None)
    prompt_assembler = getattr(loop, "prompt_assembler", None)
    workflows = getattr(
        prompt_assembler, "task_workflow_catalog", DEFAULT_TASK_WORKFLOW_CATALOG)
    effective_hooks = (
        hooks if hooks is not None else getattr(loop, "hook_port", None))
    effective_mcp = (
        mcp_supervisor if mcp_supervisor is not None
        else getattr(loop, "mcp_supervisor", None))
    collections = build_prompt_surface_collections(
        prompt_snapshot=snapshot,
        prompt_plan=getattr(loop, "last_prompt_plan", None),
        system_prompt=system_prompt,
        prompt_profile=str(getattr(loop, "prompt_profile_name", "") or ""),
        prompt_release=str(getattr(resolution, "release_id", "") or ""),
        provider=str(getattr(getattr(loop, "llm", None), "name", "") or ""),
        model=str(getattr(getattr(loop, "llm", None), "model", "") or ""),
        tools=getattr(loop, "registry", None),
        workflows=workflows,
        agents=getattr(loop, "agent_definitions", None),
        output_styles=getattr(loop, "output_style_manager", None),
        hooks=effective_hooks,
        custom_commands=getattr(loop, "custom_command_manager", None),
        skills=getattr(loop, "skill_manager", None),
        plugins=getattr(loop, "plugin_manager", None),
        mcp_supervisor=effective_mcp,
    )
    registry.replace_collections(collections)
    return registry


__all__ = [
    "build_prompt_surface_collections",
    "refresh_prompt_surfaces",
    "tool_prompt_surfaces",
    "workflow_prompt_surfaces",
]
