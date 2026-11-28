"""Context turn coordination.

This module owns the context side of one model request: attachment refresh,
prompt/context assembly, exact request accounting, deterministic
microcompaction, structured compaction and cache-ledger bookkeeping.  The
``AgentLoop`` facade only decides when a turn needs a model request.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Callable, List, Mapping, Optional, Sequence, cast

from krelcode.core.context_usage import ContextUsageSnapshot
from krelcode.core.events import (
    CompactCheckpointCreated,
    ContextAssembled,
    ContextCompressed,
    ContextDropped,
    ContextMicrocompacted,
    KnowledgeLoaded,
    MemoryLoaded,
)
from krelcode.core.messages import Message, sanitize_history
from krelcode.llm.base import PreparedLLMRequest, ToolSpec, supports_keyword_argument
from krelcode.agent.context.attachments import (
    ContextAttachment,
    ContextAttachmentDiff,
    ContextAttachmentKind,
    ContextAttachmentStore,
    agent_catalog_attachment,
    attachment_id,
    date_attachment,
    environment_attachments,
    mcp_instructions_attachment,
    mode_permission_contract_attachment,
    skill_catalog_attachment,
    token_budget_reminder_attachment,
)
from krelcode.agent.context.workspace import WorkspaceContext, build_workspace_context_bundle
from krelcode.agent.context.context_cache_ledger import ContextCacheLedger
from krelcode.agent.context.context_plan import ContextPlanSnapshot, history_digest
from krelcode.agent.context.context_source import ContextAssembly, ContextRequest, MemoryContextSource
from krelcode.agent.context.context_request import resolve_runtime_identity
from krelcode.agent.context.context_turn_observability import (
    dropped_context_items,
    knowledge_selection,
    memory_selection,
)
from krelcode.agent.context.context_usage import build_context_usage_snapshot
from krelcode.agent.context.compression import CompactResult
from krelcode.agent.turn.dependencies import AgentDependencies
from krelcode.agent.prompt.fragments import (
    is_rule_fragment,
    refresh_registry_snapshot,
    sync_rule_fragments,
    workspace_rule_fragments,
)
from krelcode.agent.pricing import match_context_window
from krelcode.agent.prompt.prompt_assembler import PromptAssemblyRequest
from krelcode.agent.prompt.prompt_models import PromptPlan
from krelcode.agent.prompt.prompt_plan import PromptPlanSnapshot
from krelcode.agent.prompt.prompt_surface import PromptSurfaceRegistry, prompt_content_hash, update_observation
from krelcode.agent.turn.runtime_ports import ContextTurnHostPort
from krelcode.agent.turn.reminders import (
    DEFAULT_REMINDER_BUDGET_TOKENS,
    ReminderContext,
    ReminderSelection,
)
from krelcode.core.tokens import estimate_history_tokens
from krelcode.agent.context.tool_microcompact import MicrocompactReport
from krelcode.safety.contract import build_mode_permission_contract


INTERRUPTED_NOTE = "[已被用户中止]"


@dataclass(frozen=True)
class ContextTurnRequest:
    """One fully budgeted provider request ready for the model executor."""

    messages: List[Message]
    context_assembly: ContextAssembly
    snapshot: ContextUsageSnapshot
    prepared: Optional[PreparedLLMRequest]


class ContextTurnCoordinator:
    """Prepare and account for one model request without streaming it."""

    def __init__(self, host: ContextTurnHostPort, dependencies: AgentDependencies) -> None:
        self.host = host
        self.dependencies = dependencies
        self._initialize_observability_state()

    def _initialize_observability_state(self) -> None:
        host = self.host
        host.last_prompt_plan = None
        host.last_provider_messages = None
        host.last_context_assembly = None
        host.last_context_snapshot = None
        host.previous_context_snapshot = None
        host.last_context_plan = None
        host.last_context_cache_ledger = None
        host.previous_context_cache_ledger = None
        host.cache_ledger_owner = 'main'
        host.parent_policy_instruction_hash = ''
        host.last_context_restore = None
        host.context_integrity = None
        host.context_workspace_fingerprint = None
        host.restored_workspace_fingerprint = None
        host.restored_context_checkpoint_id = ''
        host.restored_memory_context = ()
        host.restored_relevant_memory_ids = ()
        host.pending_context_refresh = False
        host.context_attachments = ContextAttachmentStore()
        host.last_attachment_diff = ContextAttachmentDiff(0, 0)
        host.last_mode_permission_contract = None
        host.last_microcompact_report = None
        host.last_compact_checkpoint = None

    def reset(self) -> None:
        host = self.host
        host.last_prompt_plan = None
        host.last_provider_messages = None
        host.last_context_assembly = None
        host.last_context_snapshot = None
        host.previous_context_snapshot = None
        host.last_context_cache_ledger = None
        host.previous_context_cache_ledger = None
        host.last_microcompact_report = None
        host.last_compact_checkpoint = None
        host.last_attachment_diff = ContextAttachmentDiff(0, 0)
        host.context_attachments.clear()

    async def refresh_workspace_context(self) -> WorkspaceContext:
        """Reload rules and replace only changed environment attachments."""
        host = self.host
        options = getattr(host, "context_options", None)
        rule_loader = getattr(host, "rule_loader", None)
        bundle = await build_workspace_context_bundle(
            self.dependencies.workspace_root,
            options=options,
            rule_loader=rule_loader,
        )
        host.workspace_context = bundle
        registry = getattr(host, "prompt_registry", None)
        if registry is not None:
            rules = workspace_rule_fragments(bundle)
            current = tuple(
                fragment for fragment in registry.list()
                if is_rule_fragment(fragment)
            )
            if current != rules:
                sync_rule_fragments(registry, bundle)
                refresh_registry_snapshot(host)
        self.sync_context_attachments()
        host.pending_context_refresh = False
        current_plan = getattr(host, "last_context_plan", None)
        if current_plan is not None:
            host.last_context_plan = replace(current_plan, history_digest=history_digest(host.history))
        return bundle

    def sync_context_attachments(
        self,
        *,
        include_environment: bool = True,
        include_catalogs: bool = True,
        include_mcp: bool = True,
        include_contract: bool = True,
        include_reminders: bool = True,
    ) -> ContextAttachmentDiff:
        """Replace only attachments whose semantic content changed."""
        host = self.host
        previous = host.context_attachments.snapshot()

        if include_environment:
            try:
                bundle = host.workspace_context
                environment_ids = {
                    attachment_id(ContextAttachmentKind.ENVIRONMENT_SNAPSHOT),
                    attachment_id(ContextAttachmentKind.ENVIRONMENT_DELTA),
                }
                has_environment = (
                    bundle is not None
                    and any(section.name != "rules" for section in bundle.sections)
                )
                if has_environment and bundle is not None:
                    snapshot_id = attachment_id(
                        ContextAttachmentKind.ENVIRONMENT_SNAPSHOT)
                    changed = environment_attachments(
                        bundle, host.context_attachments.get(snapshot_id))
                    for item in changed:
                        host.context_attachments.upsert(item)
                else:
                    for item_id in environment_ids:
                        host.context_attachments.remove(item_id)
            except Exception:
                pass

        if include_catalogs:
            self._sync_catalog_attachment(
                ContextAttachmentKind.SKILL_CATALOG_DELTA,
                getattr(host, "skill_manager", None),
                skill_catalog_attachment,
            )
            self._sync_catalog_attachment(
                ContextAttachmentKind.AGENT_CATALOG_DELTA,
                getattr(host, "agent_definitions", None),
                agent_catalog_attachment,
            )

        if include_mcp:
            self._sync_catalog_attachment(
                ContextAttachmentKind.MCP_INSTRUCTIONS_DELTA,
                getattr(host, "mcp_supervisor", None),
                mcp_instructions_attachment,
            )

        if include_contract:
            self.sync_mode_permission_contract()

        if include_reminders:
            try:
                today = (
                    self.dependencies.today_provider()
                    if self.dependencies.today_provider is not None else None
                )
            except Exception:
                today = None
            host.context_attachments.upsert(date_attachment(today))
            token_budget_id = attachment_id(
                ContextAttachmentKind.TOKEN_BUDGET_REMINDER)
            if self.dependencies.token_budget is None:
                host.context_attachments.remove(token_budget_id)
            else:
                host.context_attachments.upsert(
                    token_budget_reminder_attachment(
                        self.dependencies.token_budget,
                        host._token_total,
                    ))

        diff = host.context_attachments.diff(previous)
        host.last_attachment_diff = diff
        return diff

    def sync_mode_permission_contract(self) -> None:
        """Refresh L5 from the same live boundary used to execute tools."""
        host = self.host
        item_id = attachment_id(
            ContextAttachmentKind.MODE_PERMISSION_CONTRACT)
        try:
            context = host._turn_tool_context
            contract = build_mode_permission_contract(
                context.runtime,
                host._current_tool_pool().exposure_snapshot(),
                sandbox=context.sandbox,
                sandbox_enabled=context.sandbox_enabled,
                policy=self.dependencies.policy,
                subagent_definition=getattr(
                    host, "subagent_definition_name", ""),
            )
        except Exception:
            host.last_mode_permission_contract = None
            host.context_attachments.remove(item_id)
            return
        host.last_mode_permission_contract = contract
        host.context_attachments.upsert(
            mode_permission_contract_attachment(contract))
        host.context_attachments.remove(
            attachment_id(ContextAttachmentKind.MODE_REMINDER))

    def _sync_catalog_attachment(
        self,
        kind: ContextAttachmentKind,
        manager: object | None,
        builder: Callable[..., Optional[ContextAttachment]],
    ) -> None:
        host = self.host
        item_id = attachment_id(kind)
        if manager is None:
            host.context_attachments.remove(item_id)
            return
        try:
            item = builder(manager, host.context_attachments.get(item_id))
        except Exception:
            return
        if item is None:
            host.context_attachments.remove(item_id)
        else:
            host.context_attachments.upsert(item)

    async def microcompact_tools(
        self,
        *,
        trigger: str = "manual",
    ) -> MicrocompactReport:
        """Run the free tool-result tier before considering LLM compaction."""
        host = self.host
        report = self.dependencies.tool_microcompactor.compact(
            host.history,
            trigger=trigger,
            current_turn_id=host._current_turn_id,
        )
        host.last_microcompact_report = report
        if report.changed:
            await host._emit(ContextMicrocompacted(
                trigger=report.trigger,
                before_tokens=report.before_tokens,
                after_tokens=report.after_tokens,
                compacted_count=report.compacted_count,
                preserved_count=report.preserved_count,
                protected_count=report.protected_count,
                summarized_count=report.summarized_count,
                referenced_count=report.referenced_count,
            ))
        return report

    async def compress_context(
        self,
        force: bool = False,
        *,
        focus: str = "",
        request_tokens: Optional[int] = None,
        output_reserve_tokens: int = 0,
        model_context_window: Optional[int] = None,
        pre_microcompacted: bool = False,
    ) -> bool:
        """Unified compression entry: exact budget, Hooks and events."""
        host = self.host
        manager = self.dependencies.context_manager
        if manager is None:
            return False
        history_tokens = estimate_history_tokens(host.history)
        before = max(history_tokens, int(request_tokens or 0))
        extra_tokens = max(0, before - history_tokens)
        needs_compression = force or manager.over_budget(
            host.history,
            extra_tokens=extra_tokens,
            output_reserve_tokens=output_reserve_tokens,
            model_context_window=model_context_window,
        )
        if needs_compression:
            await host.run_lifecycle_hook(
                "pre_compact", {"before_tokens": str(before)})
        pre_compact_history = deepcopy(host.history)
        manager.last_compact_result = None
        host.last_compact_checkpoint = None
        changed = await manager.maybe_compress(
            host.history,
            force=force,
            extra_tokens=extra_tokens,
            output_reserve_tokens=output_reserve_tokens,
            model_context_window=model_context_window,
            microcompact=not pre_microcompacted,
            current_turn_id=host._current_turn_id,
            focus=focus,
        )
        report = manager.last_microcompact_report
        if report is not None and report.changed and not pre_microcompacted:
            host.last_microcompact_report = report
            await host._emit(ContextMicrocompacted(
                trigger=report.trigger,
                before_tokens=report.before_tokens,
                after_tokens=report.after_tokens,
                compacted_count=report.compacted_count,
                preserved_count=report.preserved_count,
                protected_count=report.protected_count,
                summarized_count=report.summarized_count,
                referenced_count=report.referenced_count,
            ))
        if not changed:
            return False
        after = estimate_history_tokens(host.history) + extra_tokens
        result = cast(Optional[CompactResult], manager.last_compact_result)
        checkpoint = None
        store = self.dependencies.compact_checkpoint_store
        if result is not None and store is not None:
            working = self.dependencies.working_context_store.snapshot().as_checkpoint_record()
            checkpoint = store.record(
                pre_compact_history=pre_compact_history,
                summary=result.summary,
                trigger=result.trigger,
                before_tokens=before,
                after_tokens=after,
                session_id=host._turn_tool_context.runtime.session_id or "",
                turn_id=host._current_turn_id,
                boundary={
                    "head_end": result.boundary.head_end,
                    "middle_start": result.boundary.middle_start,
                    "middle_end": result.boundary.middle_end,
                    "tail_start": result.boundary.tail_start,
                    "tail_end": result.boundary.tail_end,
                    "turn_aligned": result.boundary.turn_aligned,
                    "safe_adjusted": result.boundary.safe_adjusted,
                },
                source_savings=result.savings_dict(),
                working_context=working,
            )
            host.last_compact_checkpoint = checkpoint
            if checkpoint is not None:
                await host._emit(CompactCheckpointCreated(
                    checkpoint_id=checkpoint.id,
                    generation=result.generation,
                    before_tokens=before,
                    after_tokens=after,
                    summary_digest=checkpoint.summary.digest,
                    strategy="structured_summary",
                    trigger=result.trigger,
                    source_savings=result.source_savings,
                ))
        await host.run_lifecycle_hook(
            "post_compact",
            {"before_tokens": str(before), "after_tokens": str(after)},
        )
        await host._emit(ContextCompressed(
            before,
            after,
            strategy="structured_summary" if result is not None else "evict_tools",
            summary_source=result.summary_source if result is not None else "",
            generation=result.generation if result is not None else 0,
            high_watermark_tokens=(
                result.high_watermark_tokens if result is not None
                else manager.high_watermark_tokens),
            low_watermark_tokens=(
                result.low_watermark_tokens if result is not None
                else manager.low_watermark_tokens),
            source_savings=(
                result.source_savings if result is not None else ()),
            checkpoint_id=checkpoint.id if checkpoint is not None else "",
        ))
        return True

    def repair_after_cancel(self) -> None:
        sanitize_history(self.host.history)
        if self.host.history and self.host.history[-1].role == "user":
            self.host.history.append(Message.assistant(INTERRUPTED_NOTE))

    def select_turn_reminders(self) -> ReminderSelection:
        """Select reminders once per turn, never once per model step."""
        host = self.host
        if host._turn_reminder_selection is not None:
            return host._turn_reminder_selection
        workflow = host._turn_workflow
        runtime = host._turn_tool_context.runtime
        tool_pool = host._active_tool_pool
        context = ReminderContext(
            requested_reminder_ids=host._turn_requested_reminder_ids,
            task_type=(
                workflow.task_type.value if workflow is not None else ""),
            workflow_id=(
                workflow.definition.id if workflow is not None else ""),
            intent=host._turn_intent.mode,
            permission_mode=runtime.permission_mode.value,
            run_mode=runtime.run_mode.value,
            active_tools=(
                tuple(tool_pool.names()) if tool_pool is not None else ()),
            token_usage_ratio=self.current_context_usage_ratio(),
        )
        selection = self.dependencies.reminder_catalog.select(
            context,
            budget_tokens=DEFAULT_REMINDER_BUDGET_TOKENS,
        )
        host._turn_reminder_selection = selection
        host.last_reminder_selection = selection
        return selection

    def current_context_usage_ratio(self) -> float:
        manager = self.dependencies.context_manager
        max_tokens = max(int(getattr(manager, "max_tokens", 0) or 0), 0)
        if not max_tokens:
            return 0.0
        return min(
            1.0, estimate_history_tokens(self.host.history) / max_tokens)

    def observe_prompt_surfaces(
        self,
        prompt_plan: PromptPlan,
        tool_specs: Sequence[object],
        step: int,
    ) -> None:
        """Record content-light actual renders without changing the request."""
        host = self.host
        registry = getattr(host, "prompt_surface_registry", None)
        if not isinstance(registry, PromptSurfaceRegistry):
            return
        system_tokens = sum(
            int(getattr(fragment, "tokens", 0) or 0)
            for fragment in prompt_plan.fragments
        )
        update_observation(
            registry,
            "main:agent",
            content_hash=prompt_plan.full_hash,
            tokens=system_tokens,
            turn_id=host._current_turn_id,
            step=step,
        )
        for spec in tool_specs:
            name = str(getattr(spec, "name", "") or "")
            if not name:
                continue
            update_observation(
                registry,
                f"tool:{name}",
                content_hash=str(getattr(spec, "content_hash", "") or ""),
                tokens=int(getattr(spec, "token_estimate", 0) or 0),
                turn_id=host._current_turn_id,
                step=step,
            )
        for fragment in prompt_plan.fragments:
            fragment_id = str(getattr(fragment, "id", "") or "")
            if not fragment_id.startswith("reminder:"):
                continue
            update_observation(
                registry,
                fragment_id,
                content_hash=prompt_content_hash(
                    getattr(fragment, "content", "")),
                tokens=int(getattr(fragment, "tokens", 0) or 0),
                turn_id=host._current_turn_id,
                step=step,
            )

    async def build_llm_request(
        self,
        *,
        step: int,
        prompt_specs: Sequence[object],
        specs: Sequence[ToolSpec],
        tools: Optional[Sequence[ToolSpec]],
    ) -> tuple[List[Message], ContextAssembly, Optional[PreparedLLMRequest]]:
        host = self.host
        deps = self.dependencies
        self.sync_context_attachments()
        attachment_snapshot = host.context_attachments.snapshot()
        memory_store = getattr(host, "memory_store", None)
        context_engine = str(
            getattr(
                host,
                "context_engine",
                getattr(deps.context_assembler, "engine", "v3"),
            ) or "v3")
        if (
            context_engine == "v3"
            and memory_store is not None
            and "memory" not in deps.context_assembler.source_names
        ):
            deps.context_assembler.register(MemoryContextSource(memory_store))
        base_message = (
            host.history[0]
            if host.history and host.history[0].role == "system" else None
        )
        resolved_principal = resolve_runtime_identity(
            deps.tool_context.principal)
        context_request = ContextRequest(
            turn_id=host._current_turn_id,
            step=step,
            history=tuple(host.history),
            workspace_root=deps.workspace_root,
            system_prompt=(base_message.content or "") if base_message else "",
            system_provider_meta=(
                base_message.provider_meta if base_message else None),
            prompt_snapshot=getattr(host, "prompt_snapshot", None),
            tool_pool=host._current_tool_pool(),
            intent=host._turn_intent,
            task_workflow=host._turn_workflow,
            runtime_context=tuple(host.runtime_context),
            working_context=deps.working_context_store.snapshot(),
            relevant_memory=tuple(host.restored_memory_context),
            application_request=host.application_request,
            agent_contract=host.agent_contract,
            principal=resolved_principal,
            project_id=str(deps.workspace_root),
            session_id=deps.tool_context.runtime.session_id or "",
        )
        reminder_selection = self.select_turn_reminders()
        prompt_plan = await deps.prompt_assembler.assemble(
            PromptAssemblyRequest(
                context_request=context_request,
                prompt_snapshot=getattr(host, "prompt_snapshot", None),
                tools=tuple(prompt_specs or ()),
                per_turn_reminders=reminder_selection.selected,
                attachments=attachment_snapshot.items,
            ))
        context_assembly = prompt_plan.context_assembly
        if context_assembly is None:
            raise RuntimeError("PromptAssembler 未产出 ContextAssembly 兼容视图")
        previous_context_plan = getattr(host, "last_context_plan", None)
        previous_prompt_plan = (
            previous_context_plan.prompt_plan
            if previous_context_plan is not None else None
        )
        prompt_plan_snapshot = PromptPlanSnapshot.from_prompt_plan(
            prompt_plan,
            turn_id=host._current_turn_id,
            step=step,
            history_digest=history_digest(host.history),
            tools=tuple(prompt_specs or ()),
            dropped_items=dropped_context_items(host),
            previous=previous_prompt_plan,
        )
        host.last_prompt_plan = prompt_plan
        self.observe_prompt_surfaces(
            prompt_plan, tuple(prompt_specs or ()), step)
        working_snapshot = deps.working_context_store.snapshot()
        host.last_context_plan = ContextPlanSnapshot.from_assembly(context_assembly, history=host.history, turn_id=host._current_turn_id, step=step, working_context_sequence=working_snapshot.sequence, prompt_plan=prompt_plan_snapshot)
        provider_messages = deps.prompt_assembler.provider_messages(
            prompt_plan, tools=tuple(specs or ()))
        host.last_provider_messages = provider_messages
        messages = list(provider_messages.messages)
        prepared: Optional[PreparedLLMRequest] = None
        prepare = getattr(deps.llm, "prepare_request", None)
        stream_prepared = getattr(deps.llm, "stream_prepared", None)
        supported = getattr(deps.llm, "supports_prepared_requests", True)
        if supported and callable(prepare) and callable(stream_prepared):
            capabilities = getattr(deps.llm, "capabilities", None)
            if (
                getattr(capabilities, "supports_evidence_channel", False)
                and supports_keyword_argument(prepare, "evidence")
            ):
                prepared = prepare(
                    messages,
                    tools,
                    evidence=provider_messages.evidence_channel,
                )
            else:
                prepared = prepare(messages, tools)
        return messages, context_assembly, prepared

    def finalize_context_cache_ledger(
        self, context_snapshot: ContextUsageSnapshot,
    ) -> Optional[ContextCacheLedger]:
        """Bind the final, actually-sent request to one content-free ledger."""
        host = self.host
        context_plan = host.last_context_plan
        if context_plan is None:
            return None
        prompt_plan = context_plan.prompt_plan
        previous = host.last_context_cache_ledger
        current = ContextCacheLedger.from_components(
            prompt_plan=prompt_plan,
            attachment_snapshot=host.context_attachments.snapshot(),
            compact_checkpoint_id=str(getattr(
                host.last_compact_checkpoint, "id", "") or ""),
            working_context_sequence=context_plan.working_context_sequence,
            provider=(
                context_snapshot.provider
                or str(getattr(self.dependencies.llm, "name", "") or "")),
            model=str(getattr(self.dependencies.llm, "model", "") or ""),
            provider_request_digest=context_snapshot.request_digest,
            wire_fidelity=context_snapshot.wire_fidelity,
            owner=getattr(host, "cache_ledger_owner", "main"),
            parent_policy_instruction_hash=getattr(
                host, "parent_policy_instruction_hash", ""),
        ).compared_with(previous)
        host.previous_context_cache_ledger = previous
        host.last_context_cache_ledger = current
        host.last_context_plan = replace(context_plan, cache_ledger=current)
        return current

    def request_snapshot(
        self,
        *,
        step: int,
        specs: Sequence[ToolSpec],
        messages: List[Message],
        context_assembly: ContextAssembly,
        prepared: Optional[PreparedLLMRequest],
    ) -> ContextUsageSnapshot:
        host = self.host
        manager = self.dependencies.context_manager
        return build_context_usage_snapshot(
            messages,
            turn_id=host._current_turn_id,
            step=step,
            tool_specs=specs,
            context_assembly=context_assembly,
            budget_tokens=(manager.max_tokens if manager is not None else None),
            model_context_window=match_context_window(
                getattr(self.dependencies.llm, "model", None)),
            prepared_request=prepared,
            provider_name=str(
                getattr(self.dependencies.llm, "name", "") or ""),
            dropped_items=dropped_context_items(host),
        )

    async def prepare_model_request(
        self,
        *,
        step: int,
        prompt_specs: Sequence[object],
        specs: Sequence[ToolSpec],
        tools: Optional[Sequence[ToolSpec]],
    ) -> ContextTurnRequest:
        """Assemble and budget one exact request, including fallback compaction."""
        d = self.dependencies
        messages, context_assembly, prepared = await self.build_llm_request(
            step=step,
            prompt_specs=prompt_specs,
            specs=specs,
            tools=tools,
        )
        context_snapshot = self.request_snapshot(
            step=step,
            specs=specs,
            messages=messages,
            context_assembly=context_assembly,
            prepared=prepared,
        )

        if d.context_manager is not None and not context_snapshot.input_fits:
            micro_report = await self.microcompact_tools(trigger="auto")
            if micro_report.changed:
                messages, context_assembly, prepared = await self.build_llm_request(
                    step=step,
                    prompt_specs=prompt_specs,
                    specs=specs,
                    tools=tools,
                )
                context_snapshot = self.request_snapshot(
                    step=step,
                    specs=specs,
                    messages=messages,
                    context_assembly=context_assembly,
                    prepared=prepared,
                )
            if not context_snapshot.input_fits:
                changed = await self.compress_context(
                    request_tokens=context_snapshot.estimated_input_tokens,
                    output_reserve_tokens=context_snapshot.output_reserve_tokens,
                    model_context_window=context_snapshot.model_context_window,
                    pre_microcompacted=True,
                )
                if changed:
                    messages, context_assembly, prepared = await self.build_llm_request(
                        step=step,
                        prompt_specs=prompt_specs,
                        specs=specs,
                        tools=tools,
                    )
                    context_snapshot = self.request_snapshot(
                        step=step,
                        specs=specs,
                        messages=messages,
                        context_assembly=context_assembly,
                        prepared=prepared,
                    )

        host = self.host
        host.previous_context_snapshot = host.last_context_snapshot
        host.last_context_assembly = context_assembly
        host.last_context_snapshot = context_snapshot
        ledger = self.finalize_context_cache_ledger(context_snapshot)
        await host._emit(ContextAssembled(
            context_snapshot,
            ledger=ledger.as_dict() if ledger is not None else None,
        ))
        if context_snapshot.dropped_items:
            await host._emit(ContextDropped(
                items=tuple(context_snapshot.dropped_items),
                reason="budget",
            ))
        memory_ids, memory_scopes, memory_tokens = memory_selection(
            context_assembly)
        if memory_ids or memory_scopes or memory_tokens:
            await host._emit(MemoryLoaded(
                entry_ids=memory_ids,
                scope_counts=tuple(sorted(memory_scopes.items())),
                tokens=memory_tokens,
            ))
        document_ids, chunk_ids, knowledge_sources, knowledge_scopes, knowledge_tokens = (
            knowledge_selection(context_assembly)
        )
        if document_ids or chunk_ids or knowledge_sources or knowledge_tokens:
            await host._emit(KnowledgeLoaded(
                document_ids=document_ids,
                chunk_ids=chunk_ids,
                sources=knowledge_sources,
                scope_counts=knowledge_scopes,
                tokens=knowledge_tokens,
            ))
        return ContextTurnRequest(
            messages=messages,
            context_assembly=context_assembly,
            snapshot=context_snapshot,
            prepared=prepared,
        )



__all__ = ["ContextTurnCoordinator", "ContextTurnRequest", "INTERRUPTED_NOTE"]
