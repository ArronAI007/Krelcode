"""System-prompt and evidence channel construction."""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from krelcode.core.messages import Message
from krelcode.llm.base import (
    SYSTEM_BLOCK_CHANNEL_EVIDENCE,
    SYSTEM_BLOCK_CHANNEL_INSTRUCTION,
    EvidenceBlock,
    SystemPromptBlock,
    SystemPromptBlocks,
)
from krelcode.core.evidence import SourceRole
from krelcode.agent.prompt.fragments import (
    GROUP_WORKSPACE,
    LAYER_L7_EVIDENCE,
    WORKSPACE_CLOSE,
    WORKSPACE_OPEN,
    PromptFragment,
)


def _build_system_prompt_blocks(
    fragments: Sequence[PromptFragment],
) -> SystemPromptBlocks:
    stable: list[SystemPromptBlock] = []
    dynamic: list[SystemPromptBlock] = []
    stable_open = True
    for fragment in fragments:
        content = _wire_fragment_content(fragment)
        if not content:
            continue
        channel = _fragment_channel(fragment)
        metadata = _prompt_block_metadata(fragment)
        block = SystemPromptBlock(
            id=fragment.id,
            text=content,
            source=fragment.source,
            stability=fragment.stability,
            trust=fragment.trust,
            cacheable=fragment.cacheable,
            channel=channel,
            scope=fragment.scope,
            evidence_type=(
                str(metadata.get("evidence_type", "") or "")
                if channel == SYSTEM_BLOCK_CHANNEL_EVIDENCE else ""),
            requires_validation=(
                bool(metadata.get("requires_validation", True))
                if channel == SYSTEM_BLOCK_CHANNEL_EVIDENCE else False),
            metadata=metadata,
        )
        if stable_open and fragment.cacheable:
            stable.append(block)
        else:
            stable_open = False
            dynamic.append(block)
    blocks = SystemPromptBlocks(
        stable_system_blocks=tuple(stable),
        dynamic_system_blocks=tuple(dynamic),
    )
    return blocks


def _build_evidence_blocks(
    fragments: Sequence[PromptFragment],
) -> Tuple[EvidenceBlock, ...]:
    """Build the independent evidence channel from L7 fragments."""
    blocks: list[EvidenceBlock] = []
    for fragment in fragments:
        content = fragment.content.strip()
        if not content:
            continue
        metadata = _prompt_block_metadata(fragment)
        blocks.append(EvidenceBlock(
            id=fragment.id,
            text=content,
            source=fragment.source,
            evidence_type=str(metadata.get("evidence_type", "") or ""),
            scope=fragment.scope,
            origin=str(metadata.get("evidence_origin", "") or ""),
            trust=fragment.trust,
            requires_validation=bool(
                metadata.get("requires_validation", True)),
            metadata=metadata,
        ))
    return tuple(blocks)


def _evidence_anchor_index(
    messages: Sequence[Message],
    current_user: Optional[Message],
) -> int:
    """Return the transient user-message index that owns this turn's evidence."""
    if current_user is None:
        return -1
    for index, message in enumerate(messages):
        if message is current_user:
            return index
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "user" and message == current_user:
            return index
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            return index
    return -1


_PROMPT_BLOCK_METADATA_KEYS = frozenset({
    "source_role",
    "instruction",
    "instruction_source",
    "evidence_type",
    "evidence_sources",
    "evidence_ids",
    "evidence_count",
    "evidence_origin",
    "requires_validation",
    "confidence_min",
    "confidence_max",
    "prompt_section",
    "prompt_section_required",
    "prompt_profile",
    "prompt_profile_requested",
    "prompt_profile_match",
    "prompt_provider",
    "prompt_model",
    "base_prompt_hash",
    "prompt_release_id",
    "prompt_release_hash",
    "prompt_profile_hash",
    "profile_calibration_id",
    "profile_calibration_hash",
    "profile_calibration_status",
    "profile_calibration_reason",
    "tool_playbook",
    "tool_playbook_version",
    "tool_playbook_hash",
    "tool_playbook_content_hash",
    "tool_playbook_tools",
    "tool_playbook_active_tools",
    "task_workflow",
    "task_workflow_version",
    "task_workflow_hash",
    "task_workflow_content_hash",
    "task_type",
    "task_confidence",
    "task_signals",
    "mcp_server",
    "mcp_servers",
    "attachment_id",
    "attachment_kind",
    "attachment_revision",
    "contract_version",
    "contract_hash",
    "run_mode",
    "permission_mode",
    "approval_profile",
    "approval_precedence",
    "approval_requires_approver",
    "approval_auto_approves_routine",
    "approval_fail_closed",
    "side_effect_policy",
    "plan_behavior",
    "sandbox_enabled",
    "sandbox_backend",
    "sandbox_network_allowed",
    "sandbox_network_controlled",
    "sandbox_os_enforced",
    "sandbox_shell_write_scope",
    "sandbox_file_write_scope",
    "protected_paths_enforced",
    "sensitive_reads_protected",
    "tool_allowlist_active",
    "mcp_allowlist_active",
    "mcp_scope",
    "subagent",
    "subagent_definition",
    "tool_total_count",
    "eligible_tool_count",
    "excluded_tool_count",
    "read_only_tool_count",
    "mutating_tool_count",
    "local_read_only_tool_count",
    "loaded_tool_count",
    "advertised_tool_count",
    "tool_exclusion_categories",
    "tool_exposure_hash",
})


def _fragment_channel(fragment: PromptFragment) -> str:
    metadata = fragment.metadata
    if (
        str(metadata.get("source_role", "") or "").strip().lower()
        == SourceRole.EVIDENCE.value
        or bool(metadata.get("evidence_type"))
        or fragment.layer == LAYER_L7_EVIDENCE
    ):
        return SYSTEM_BLOCK_CHANNEL_EVIDENCE
    return SYSTEM_BLOCK_CHANNEL_INSTRUCTION


def _prompt_block_metadata(fragment: PromptFragment) -> dict[str, object]:
    """Keep only inspectable provenance; never copy arbitrary rule/tool data."""
    return {
        key: value
        for key, value in fragment.metadata.items()
        if key in _PROMPT_BLOCK_METADATA_KEYS
    }


def _render_system_fragments(
    fragments: Sequence[PromptFragment],
) -> tuple[str, int, tuple[str, ...]]:
    text = ""
    stable_end = 0
    stable_open = True
    stable_ids: list[str] = []
    for fragment in fragments:
        content = _wire_fragment_content(fragment)
        if not content:
            continue
        separator = "\n\n" if text else ""
        text += separator + content
        if stable_open and fragment.cacheable:
            stable_end = len(text)
            stable_ids.append(fragment.id)
        else:
            stable_open = False
    return text, stable_end, tuple(stable_ids)


def _wire_fragment_content(fragment: PromptFragment) -> str:
    content = fragment.content.strip()
    if not content:
        return ""
    if (
        fragment.group == GROUP_WORKSPACE
        and "<workspace_context>" not in content
    ):
        return f"{WORKSPACE_OPEN}\n{content}\n{WORKSPACE_CLOSE}"
    return content
