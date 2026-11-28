"""Build the complete runtime release identity recorded by observability."""

from __future__ import annotations

from dataclasses import asdict
from typing import Iterable, Mapping

from ...agent.turn.loop import AgentLoop
from ...agent.context.context_policy import ContextBudgetPolicy
from ...agent.context.rehydration import CompactionRehydrationPolicy
from ...contracts.runtime import (
    RUNTIME_RELEASE_SCHEMA_VERSION,
    RuntimeReleaseManifest,
)
from ...contracts.agent import AGENT_CONTRACT_SCHEMA_VERSION
from ...enterprise.policy import EnterprisePolicy
from ...mcp.supervisor import MCPSupervisor
from ...observability.models import (
    SCHEMA_VERSION,
    stable_hash,
)
from ...plugins.manager import PluginManager
from ...session.memory import MemoryStore
from ...session.memory_models import (
    INJECT_CHARS,
    INJECT_ENTRIES,
    MAX_ENTRIES,
    MAX_ENTRY_CHARS,
    MAX_INDEX_CHARS,
    MAX_INDEX_ENTRIES,
    MAX_TOPIC_CHARS,
    MemoryClass,
    MEMORY_SCOPE_ORDER,
    MEMORY_TYPE_ORDER,
    STRUCTURED_MEMORY_TYPES,
)
from ...skills.manager import SkillManager


def build_runtime_release_manifest(
    *,
    code_version: str,
    run_mode: str,
    loop: AgentLoop,
    supervisor: MCPSupervisor,
    plugin_manager: PluginManager,
    skill_manager: SkillManager,
    memory_store: MemoryStore,
    enterprise_policy: EnterprisePolicy,
    tool_names: Iterable[str],
) -> RuntimeReleaseManifest:
    """Capture immutable component identities from the assembled runtime."""
    prompt_resolution = loop.prompt_profile_resolution
    prompt_snapshot = loop.prompt_snapshot
    registry = loop.registry
    names = tuple(sorted(str(name) for name in tool_names))
    tool_prompt_hashes = {
        name: str(getattr(registry.prompt_spec(name), "content_hash", "") or "")
        for name in names
    }
    agent_definitions = []
    manager = loop.agent_definitions
    if manager is not None:
        for definition in manager.list():
            agent_definitions.append({
                "name": definition.name,
                "source": definition.source,
                "description": definition.description,
                "system_prompt_hash": stable_hash(definition.system_prompt),
                "allowed_tools": list(definition.allowed_tools),
                "model": definition.model,
                "permission_mode": definition.permission_mode,
                "mcp_servers": (
                    list(definition.mcp_servers)
                    if definition.mcp_servers is not None else None
                ),
                "inherit_project_rules": definition.inherit_project_rules,
            })

    prompt_fragments = []
    if prompt_snapshot is not None:
        prompt_fragments = [
            {
                "id": fragment.id,
                "source": fragment.source,
                "layer": fragment.layer,
                "trust": fragment.trust,
                "content_hash": stable_hash(fragment.content),
            }
            for fragment in prompt_snapshot.fragments
        ]

    plugin_records = []
    for record in plugin_manager.records():
        plugin_records.append({
            "name": record.name,
            "version": record.version,
            "status": record.status,
            "scope": record.scope,
            "manifest_version": record.manifest_version,
            "trust_status": record.trust_status,
            "signer": record.signer,
            "key_id": record.key_id,
            "registry": record.registry,
            "integrity_digest": record.integrity_digest,
            "components": record.components_count,
            "dependencies": list(record.dependencies),
        })
    plugin_records.sort(key=lambda item: str(item["name"]))

    skills = []
    for skill in skill_manager.list():
        try:
            skill_hash = stable_hash(
                skill.skill_file.read_text(encoding="utf-8"))
        except OSError:
            skill_hash = ""
        skills.append({
            "name": skill.name,
            "version": skill.version,
            "source": skill.source,
            "description": skill.description,
            "content_hash": skill_hash,
            "allowed_tools": list(skill.allowed_tools),
            "user_invocable": skill.user_invocable,
            "disable_model_invocation": skill.disable_model_invocation,
            "owner": skill.owner,
            "applicability": skill.applicability,
            "required_tools": list(skill.required_tools),
            "required_permissions": list(skill.required_permissions),
            "required_environment": list(skill.required_environment),
            "acceptance_contract": list(skill.acceptance_contract),
            "dependencies": list(skill.dependencies),
            "tests": list(skill.tests),
            "supported_models": list(skill.supported_models),
            "deprecated": skill.deprecated,
            "release_ready": skill.release_ready,
            "asset": skill.asset.to_dict() if skill.asset is not None else {},
        })
    skills.sort(key=lambda item: str(item["name"]))

    mcp_status = sorted(
        (
            {
                key: value for key, value in dict(item).items()
                if key != "alive"
            }
            for item in supervisor.status()
        ),
        key=lambda item: str(item.get("name", "")),
    )
    policy_metadata = enterprise_policy.audit_metadata()
    context_options = loop.context_options
    memory_schema = {
        "schema": "memory-v2",
        "max_entries": MAX_ENTRIES,
        "max_entry_chars": MAX_ENTRY_CHARS,
        "max_index_entries": MAX_INDEX_ENTRIES,
        "max_index_chars": MAX_INDEX_CHARS,
        "max_topic_chars": MAX_TOPIC_CHARS,
        "inject_entries": INJECT_ENTRIES,
        "inject_chars": INJECT_CHARS,
        "scopes": [scope.value for scope in MEMORY_SCOPE_ORDER],
        "types": [item.value for item in MEMORY_TYPE_ORDER],
        "structured_types": sorted(
            item.value for item in STRUCTURED_MEMORY_TYPES),
        "memory_classes": [item.value for item in MemoryClass],
        "lifecycle_fields": [
            "last_verified_at", "use_count", "success_count",
            "failure_count", "last_outcome",
        ],
        "asset_envelope": "v1",
        "backend": "jsonl+markdown",
        "store_present": memory_store is not None,
    }
    return RuntimeReleaseManifest(
        agent_definition={
            "name": "krelcode",
            "kind": "main",
            "run_mode": run_mode,
            "prompt_profile": getattr(
                prompt_resolution, "name", loop.prompt_profile_name),
            "definitions": agent_definitions,
            "definitions_hash": stable_hash(agent_definitions),
            "contract_schema_version": AGENT_CONTRACT_SCHEMA_VERSION,
        },
        prompt_release={
            "release_id": getattr(prompt_resolution, "release_id", ""),
            "release_hash": getattr(prompt_resolution, "release_hash", ""),
            "profile_hash": getattr(prompt_resolution, "profile_hash", ""),
            "calibration_id": getattr(
                prompt_resolution, "calibration_id", ""),
            "calibration_hash": getattr(
                prompt_resolution, "calibration_hash", ""),
            "calibration_status": getattr(
                prompt_resolution, "calibration_status", ""),
            "snapshot_generation": (
                prompt_snapshot.generation
                if prompt_snapshot is not None else 0),
            "snapshot_hash": stable_hash(prompt_fragments),
            "fragments": prompt_fragments,
        },
        model_profile={
            "provider": str(getattr(loop.llm, "name", "") or ""),
            "model": str(getattr(loop.llm, "model", "") or ""),
            "profile": getattr(prompt_resolution, "name", ""),
            "profile_requested": getattr(
                prompt_resolution, "requested", ""),
            "profile_match": getattr(prompt_resolution, "matched_by", ""),
            "profile_hash": getattr(prompt_resolution, "profile_hash", ""),
        },
        tool_catalog={
            "hash": stable_hash(names),
            "count": len(names),
            "names": list(names),
            "prompt_spec_hashes": tool_prompt_hashes,
        },
        mcp_servers={
            "hash": stable_hash(mcp_status),
            "count": len(mcp_status),
            "servers": mcp_status,
        },
        plugins={
            "hash": stable_hash(plugin_records),
            "count": len(plugin_records),
            "records": plugin_records,
        },
        skills={
            "hash": stable_hash(skills),
            "count": len(skills),
            "records": skills,
        },
        policy_version={
            **dict(policy_metadata),
            "digest": stable_hash(policy_metadata),
        },
        memory_schema=memory_schema,
        context_engine={
            **(
                asdict(context_options)
                if context_options is not None else {"engine": "disabled"}
            ),
            "policy_version": "context-budget-v1",
            "budget_policy": _context_budget_record(
                loop.context_assembler.budget_policy
                if loop.context_assembler is not None
                else ContextBudgetPolicy()
            ),
            "rehydration_policy": _rehydration_policy_record(
                loop.dependencies.compaction_rehydration_policy),
            "enabled": bool(loop.context_enabled),
        },
        code_version={
            "krelcode": code_version,
            "runtime_release_schema": RUNTIME_RELEASE_SCHEMA_VERSION,
            "observability_schema": SCHEMA_VERSION,
        },
    )


def _context_budget_record(
    policy: ContextBudgetPolicy,
) -> dict[str, object]:
    return {
        "max_tokens": policy.max_tokens,
        "category_caps": dict(policy.category_caps),
        "required_categories": list(policy.required_categories),
    }


def _rehydration_policy_record(
    policy: CompactionRehydrationPolicy,
) -> dict[str, object]:
    return {
        "max_files": policy.max_files,
        "max_file_tokens": policy.max_file_tokens,
        "max_total_file_tokens": policy.max_total_file_tokens,
        "max_skills": policy.max_skills,
        "max_skill_tokens": policy.max_skill_tokens,
        "max_total_skill_tokens": policy.max_total_skill_tokens,
        "max_total_tokens": policy.max_total_tokens,
        "prefer_modified_files": policy.prefer_modified_files,
    }


__all__ = ["build_runtime_release_manifest"]
