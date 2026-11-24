"""The single request-level prompt assembly path.

Prompt fragments are collected from the fragment registry and ContextSources,
ordered into the fixed L0-L12 hierarchy, and converted once into the exact
provider-bound messages.  ``ContextAssembly`` remains available as the
source-attribution compatibility view consumed by ``/context`` and session
checkpoints, but production requests no longer assemble messages independently.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Callable, Mapping, Optional, Sequence, Tuple

from krelcode.core.messages import Message
from krelcode.llm.base import (
    SYSTEM_BLOCK_CHANNEL_EVIDENCE,
    SYSTEM_BLOCK_CHANNEL_INSTRUCTION,
    SYSTEM_PROMPT_BLOCKS_META_KEY,
    EvidenceChannel,
)
from krelcode.tools.prompt import ToolPromptSpec
from krelcode.agent.attachments import ContextAttachment
from krelcode.agent.context_assembler import ContextAssembler
from krelcode.agent.context_source import (
    ContextAssembly,
    ContextRequest,
    default_context_sources,
    select_prompt_fragments,
)
from krelcode.agent.prompt.fragments import (
    LAYER_L1_BASE_AGENT,
    LAYER_L12_REMINDER,
    STABILITY_SESSION,
    SYSTEM_PROMPT_LAYERS,
    TRUST_MANAGED,
    PromptCacheBoundary,
    PromptFragment,
    PromptFragmentRegistry,
    PromptSnapshot,
)
from krelcode.agent.prompt.prompt_channels import (
    _build_evidence_blocks,
    _fragment_channel,
    _build_system_prompt_blocks,
    _evidence_anchor_index,
)
from krelcode.agent.prompt.prompt_conflicts import PromptConflictAnalyzer
from krelcode.agent.prompt.prompt_models import (
    PromptAssemblyRequest,
    PromptLayerPlan,
    PromptPlan,
    ProviderMessages,
)
from krelcode.agent.prompt.prompt_plan import PromptOmissionSnapshot
from krelcode.agent.prompt.prompt_rendering import (
    _advertised_tool_names,
    _annotate_fragment_authority,
    _compatibility_assembly,
    _conversation_fragments,
    _current_user_message,
    _dedupe_omissions,
    _empty_dynamic_assembly,
    _fragment_from_context_layer,
    _layer_plans,
    _normalize_tool_prompt_specs,
    _ordered_fragments,
    _prompt_omission_details,
    _select_attachment_fragments,
    _token_contributions,
)
from krelcode.agent.reminders import SelectedReminder
from krelcode.agent.tokens import estimate_text_tokens
from krelcode.agent.prompt.tool_playbooks import (
    DEFAULT_TOOL_PLAYBOOK_CATALOG,
    ToolPlaybookCatalog,
)
from krelcode.agent.workflows import DEFAULT_TASK_WORKFLOW_CATALOG, TaskWorkflowCatalog


class PromptAssembler:
    """Own the only path from fragments/context to ``ProviderMessages``."""

    def __init__(
        self,
        context_assembler: Optional[ContextAssembler] = None,
        *,
        registry_provider: Optional[
            Callable[[], Optional[PromptFragmentRegistry]]
        ] = None,
        tool_playbook_catalog: Optional[ToolPlaybookCatalog] = None,
        task_workflow_catalog: Optional[TaskWorkflowCatalog] = None,
        conflict_analyzer: Optional[PromptConflictAnalyzer] = None,
    ) -> None:
        self.context_assembler = context_assembler or ContextAssembler(
            default_context_sources())
        self.registry_provider = registry_provider
        self.tool_playbook_catalog = (
            tool_playbook_catalog or DEFAULT_TOOL_PLAYBOOK_CATALOG)
        self.task_workflow_catalog = (
            task_workflow_catalog or DEFAULT_TASK_WORKFLOW_CATALOG)
        self.conflict_analyzer = conflict_analyzer or PromptConflictAnalyzer()
        self.engine = str(
            getattr(self.context_assembler, "engine", "v3") or "v3")

    async def assemble(self, request: PromptAssemblyRequest) -> PromptPlan:
        context_request = request.context_request
        snapshot = (
            request.prompt_snapshot
            or context_request.prompt_snapshot
            or self._snapshot_from_registry()
        )
        prompt_fragments, omitted, scope_filter = self._prompt_fragments(
            snapshot, context_request)
        prompt_omissions = _prompt_omission_details(
            snapshot, context_request, omitted)
        tool_prompt_specs = self.tool_playbook_catalog.resolve_prompt_specs(
            _normalize_tool_prompt_specs(request.tools))
        workflow_fragments: tuple[PromptFragment, ...] = ()
        if context_request.task_workflow is not None:
            workflow_fragments = (self.task_workflow_catalog.fragment(
                context_request.task_workflow),)
        playbook_fragments = (
            self.tool_playbook_catalog.fragments_for_prompt_specs(
                tool_prompt_specs)
            if tool_prompt_specs else
            self.tool_playbook_catalog.fragments_for_tools(
                _advertised_tool_names((), context_request.tool_pool))
        )
        attachment_fragments, attachment_omissions = _select_attachment_fragments(
            request.attachments, context_request)
        if not scope_filter and any(
            item.reason == "file-explanation-scope"
            for item in attachment_omissions
        ):
            scope_filter = "file_explanation"
        all_omissions = _dedupe_omissions(
            (*prompt_omissions, *attachment_omissions))

        # ContextSources contribute only the dynamic layers here.  The prompt
        # source is disabled by using an empty snapshot/system prompt so the
        # exact same source implementations can be reused without a second
        # prompt rendering path.
        dynamic_request = replace(
            context_request,
            system_prompt="",
            system_provider_meta=None,
            prompt_snapshot=None,
        )
        dynamic_assembly = await self.context_assembler.assemble(dynamic_request)
        dynamic_fragments = tuple(
            _fragment_from_context_layer(layer)
            for layer in dynamic_assembly.layers
        )

        return self._finalize_plan(
            context_request=context_request,
            snapshot=snapshot,
            prompt_fragments=prompt_fragments,
            playbook_fragments=playbook_fragments,
            workflow_fragments=workflow_fragments,
            attachment_fragments=attachment_fragments,
            dynamic_fragments=dynamic_fragments,
            dynamic_assembly=dynamic_assembly,
            all_omissions=all_omissions,
            scope_filter=scope_filter,
            tool_prompt_specs=tool_prompt_specs,
            per_turn_reminders=request.per_turn_reminders,
        )

    def build_isolated_plan(
        self,
        *,
        context_request: ContextRequest,
        snapshot: PromptSnapshot,
        attachments: Sequence[ContextAttachment] = (),
        per_turn_reminders: Tuple[str | SelectedReminder, ...] = (),
    ) -> PromptPlan:
        """Build a temporary plan without collecting live ContextSources.

        This is the synchronous, side-effect-free subset used by Candidate
        Prompt isolation.  It still uses the production ordering, authority,
        conflict, channel and cache-boundary implementation through
        ``_finalize_plan``; it does not write or activate the snapshot.
        """
        prompt_fragments, omitted, scope_filter = self._prompt_fragments(
            snapshot, context_request)
        prompt_omissions = _prompt_omission_details(
            snapshot, context_request, omitted)
        attachment_fragments, attachment_omissions = _select_attachment_fragments(
            attachments, context_request)
        if not scope_filter and any(
            item.reason == "file-explanation-scope"
            for item in attachment_omissions
        ):
            scope_filter = "file_explanation"
        all_omissions = _dedupe_omissions(
            (*prompt_omissions, *attachment_omissions))
        return self._finalize_plan(
            context_request=context_request,
            snapshot=snapshot,
            prompt_fragments=prompt_fragments,
            attachment_fragments=attachment_fragments,
            dynamic_assembly=_empty_dynamic_assembly(self.engine),
            all_omissions=all_omissions,
            scope_filter=scope_filter,
            per_turn_reminders=per_turn_reminders,
        )

    def _finalize_plan(
        self,
        *,
        context_request: ContextRequest,
        snapshot: Optional[PromptSnapshot],
        prompt_fragments: Sequence[PromptFragment],
        dynamic_assembly: ContextAssembly,
        all_omissions: Sequence[PromptOmissionSnapshot],
        scope_filter: str,
        playbook_fragments: Sequence[PromptFragment] = (),
        workflow_fragments: Sequence[PromptFragment] = (),
        attachment_fragments: Sequence[PromptFragment] = (),
        dynamic_fragments: Sequence[PromptFragment] = (),
        tool_prompt_specs: Sequence[ToolPromptSpec] = (),
        per_turn_reminders: Sequence[str | SelectedReminder] = (),
    ) -> PromptPlan:
        current_user = _current_user_message(context_request.history)
        conversation_fragments = _conversation_fragments(
            context_request.history,
            current_user,
            tuple(per_turn_reminders),
        )
        fragments = _ordered_fragments(
            tuple(
                _annotate_fragment_authority(fragment)
                for fragment in (
                    *prompt_fragments,
                    *playbook_fragments,
                    *workflow_fragments,
                    *attachment_fragments,
                    *dynamic_fragments,
                    *conversation_fragments,
                )
            )
        )
        conflict_report = self.conflict_analyzer.analyze(fragments)

        prompt_fragments_for_request = tuple(
            fragment for fragment in fragments
            if fragment.layer in SYSTEM_PROMPT_LAYERS
            or fragment.layer == LAYER_L12_REMINDER
        )
        instruction_fragments = tuple(
            fragment for fragment in prompt_fragments_for_request
            if _fragment_channel(fragment) == SYSTEM_BLOCK_CHANNEL_INSTRUCTION
        )
        evidence_fragments = tuple(
            fragment for fragment in prompt_fragments_for_request
            if _fragment_channel(fragment) == SYSTEM_BLOCK_CHANNEL_EVIDENCE
        )
        system_blocks = _build_system_prompt_blocks(instruction_fragments)
        evidence_blocks = _build_evidence_blocks(evidence_fragments)
        system_text = system_blocks.merged_text
        stable_offset = system_blocks.cache_control_boundary
        stable_ids = tuple(
            block.id for block in system_blocks.stable_system_blocks)
        stable_set = set(stable_ids)
        volatile_ids = tuple(
            fragment.id for fragment in fragments
            if fragment.id not in stable_set
        )
        cache = PromptCacheBoundary(
            offset=stable_offset,
            stable_tokens=estimate_text_tokens(system_blocks.stable_text),
            stable_fragments=stable_ids,
            volatile_fragments=volatile_ids,
            stable_hash=system_blocks.stable_hash,
            full_hash=hashlib.sha256(system_text.encode("utf-8")).hexdigest(),
            dynamic_hash=system_blocks.dynamic_hash,
        )
        layers = _layer_plans(fragments)
        registry_and_attachment_fragments = (
            *prompt_fragments,
            *playbook_fragments,
            *workflow_fragments,
            *attachment_fragments,
        )
        contributions = _token_contributions(
            registry_and_attachment_fragments,
            dynamic_assembly.token_contributions,
        )
        assembly = _compatibility_assembly(
            prompt_fragments=registry_and_attachment_fragments,
            dynamic_assembly=dynamic_assembly,
            system_text=system_text,
            cache=cache,
            contributions=contributions,
            fragment_count=len(fragments),
            omitted=tuple(item.id for item in all_omissions),
            scope_filter=scope_filter,
            snapshot=snapshot,
            engine=self.engine,
        )
        history = tuple(
            message for message in context_request.history
            if message.role != "system"
        )
        return PromptPlan(
            fragments=fragments,
            layers=layers,
            system_text=system_text,
            history=history,
            current_user=current_user,
            cache=cache,
            generation=max(
                int(getattr(snapshot, "generation", 0) or 0),
                0,
            ),
            engine=self.engine,
            system_blocks=system_blocks,
            evidence_blocks=evidence_blocks,
            token_contributions=contributions,
            omitted_fragments=tuple(item.id for item in all_omissions),
            omitted_details=tuple(all_omissions),
            issues=dynamic_assembly.issues,
            context_assembly=assembly,
            tool_prompt_specs=tuple(tool_prompt_specs),
            conflict_report=conflict_report,
        )

    def provider_messages(
        self,
        plan: PromptPlan,
        *,
        tools: Optional[Sequence[Mapping[str, object]]] = None,
    ) -> ProviderMessages:
        messages = list(plan.history)
        evidence_blocks = plan.evidence_blocks
        if plan.system_text:
            provider_meta = dict(plan.provider_meta or {})
            if plan.system_blocks is not None:
                provider_meta[SYSTEM_PROMPT_BLOCKS_META_KEY] = plan.system_blocks
            system = Message.system(
                plan.system_text,
                provider_meta=provider_meta or None,
            )
            messages.insert(0, system)
        anchor_index = _evidence_anchor_index(messages, plan.current_user)
        evidence_channel = EvidenceChannel(
            blocks=evidence_blocks,
            anchor_message_index=anchor_index,
        )
        return ProviderMessages(
            messages=tuple(messages),
            tools=tuple(tools or ()),
            cache_boundary=plan.cache,
            system_blocks=plan.system_blocks,
            instruction_blocks=plan.instruction_blocks,
            evidence_channel=evidence_channel,
            tool_prompt_specs=plan.tool_prompt_specs,
            conflict_report=plan.conflict_report,
        )

    def _snapshot_from_registry(self) -> Optional[PromptSnapshot]:
        if self.registry_provider is None:
            return None
        registry = self.registry_provider()
        return registry.snapshot() if registry is not None else None

    def _prompt_fragments(
        self,
        snapshot: Optional[PromptSnapshot],
        request: ContextRequest,
    ) -> tuple[
        Tuple[PromptFragment, ...], Tuple[str, ...], str
    ]:
        if snapshot is not None:
            if self.engine == "v3":
                return select_prompt_fragments(snapshot, request)
            return tuple(snapshot.fragments), (), ""
        text = (request.system_prompt or "").strip()
        if not text:
            return (), (), ""
        return (
            PromptFragment(
                id="legacy:system",
                source="system",
                title="Legacy system prompt",
                content=text,
                group="system",
                layer=LAYER_L1_BASE_AGENT,
                priority=0,
                cacheable=False,
                compactable=False,
                toggleable=False,
                scope="managed",
                stability=STABILITY_SESSION,
                trust=TRUST_MANAGED,
            ),
        ), (), ""


__all__ = [
    "PromptAssembler",
    "PromptAssemblyRequest",
    "PromptLayerPlan",
    "PromptPlan",
    "ProviderMessages",
]
