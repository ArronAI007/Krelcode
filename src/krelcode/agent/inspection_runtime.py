"""Runtime source, cache-ledger, cache and reminder projections."""

from __future__ import annotations

from .inspection_prompt import ContextInspectionPromptMixin
from .inspection_helpers import (
    _bounded,
    _ids,
    _short_hash,
    _signed,
    _yes_no,
)

class ContextInspectionRuntimeMixin(ContextInspectionPromptMixin):
    def sources_lines(self, *, limit: int = 100) -> list[str]:
        assembly = self.assembly
        if assembly is None:
            return ["  [sources] 尚无最近装配结果"]
        lines = [
            f"  [sources] 最近装配: {len(assembly.layers)} layers"
            f" · {assembly.source_count} 个 source 有贡献"
            f" · {assembly.fragment_count} fragments"
            f" · engine {assembly.engine or 'legacy/unknown'}"
            f" · cache {assembly.stable_prefix_chars} chars",
        ]
        shown, omitted = _bounded(assembly.layers, limit)
        for layer in shown:
            role = str(layer.metadata.get("source_role", "") or "")
            evidence_type = str(layer.metadata.get("evidence_type", "") or "")
            type_suffix = f"/{evidence_type}" if evidence_type else ""
            role_suffix = f" · role={role}{type_suffix}" if role else ""
            lines.append(
                f"  [{layer.priority}] {layer.id} · {layer.source}"
                f" · {layer.category} · {layer.tokens} tokens"
                f" · trust={layer.trust}"
                f"{role_suffix}"
                f" · {'cache-prefix' if layer.cacheable else 'dynamic'}")
        if omitted:
            lines.append(f"  … 另有 {omitted} 个 layer；使用 /context why <id> 定向查看")
        for issue in assembly.issues[:20]:
            lines.append(f"  [warn] {issue.source}: {issue.error}")
        if len(assembly.issues) > 20:
            lines.append(f"  … 另有 {len(assembly.issues) - 20} 个 source issue")
        if self.dropped_items:
            lines.append("  被裁剪: " + ", ".join(self.dropped_items[:50]))
        return lines

    def ledger_lines(self) -> list[str]:
        """Explain the complete request identity and cache-miss causes."""
        ledger = self.cache_ledger
        if ledger is None:
            return ["  [cache ledger] 尚无真实请求 ledger"]
        revisions = ", ".join(
            f"{item_id}@{revision}"
            for item_id, revision in ledger.attachment_revisions[:20]
        ) or "(none)"
        if len(ledger.attachment_revisions) > 20:
            revisions += (
                f" · … 另有 {len(ledger.attachment_revisions) - 20} 个")
        lines = [
            f"  [cache ledger] {ledger.owner or 'main'}"
            f" · {ledger.provider or '(unknown)'}"
            f"/{ledger.model or '(unknown)'}"
            f" · {ledger.turn_id or '(unknown turn)'}"
            f" · Step {ledger.step}",
            f"  stable {_short_hash(ledger.stable_hash)}"
            f" · dynamic {_short_hash(ledger.dynamic_hash)}"
            f" · instruction {_short_hash(ledger.instruction_hash)}"
            f" · evidence {_short_hash(ledger.evidence_hash)}",
            f"  tools {_short_hash(ledger.tool_catalog_hash)}"
            f" · mode contract {_short_hash(ledger.mode_contract_hash)}"
            f" · exposure {_short_hash(ledger.tool_exposure_hash)}"
            f" · history {_short_hash(ledger.history_digest)}"
            f" · provider request {_short_hash(ledger.provider_request_digest)}"
            f" · fidelity {ledger.wire_fidelity}",
            f"  complete request {_short_hash(ledger.complete_request_hash)}",
            f"  attachments generation {ledger.attachment_generation}"
            f" · {_ids(ledger.memory_evidence_ids, 20)} memory evidence"
            f" · revisions {revisions}",
            f"  compact {ledger.compact_checkpoint_id or '(none)'}"
            f" · working context sequence {ledger.working_context_sequence}",
        ]
        if ledger.mode_contract_hash:
            lines.append(
                f"  mode contract: run={ledger.run_mode or '(unknown)'}"
                f" · permission={ledger.permission_mode or '(unknown)'}"
                f" · hash {_short_hash(ledger.mode_contract_hash)}")
        diff = ledger.diff
        if diff.available:
            lines.append(
                f"  cache miss: {_yes_no(diff.cache_miss)}"
                f" · prefix miss: {_yes_no(diff.prefix_cache_miss)}"
                f" · previous {diff.previous_turn_id or '(unknown turn)'}"
                f" Step {diff.previous_step}")
            lines.append(
                "  reasons: " + (
                    ", ".join(diff.reasons) if diff.reasons else "(none)"))
            lines.append(
                f"  changed fields: {_ids(diff.changed_fields, 30)}")
        else:
            lines.append("  cache miss: 无上一请求 ledger 可比较")
        if ledger.policy_instruction_hash or ledger.parent_policy_instruction_hash:
            lines.append(
                f"  policy instruction {_short_hash(ledger.policy_instruction_hash)}"
                f" · parent {_short_hash(ledger.parent_policy_instruction_hash)}"
                f" · inheritance {ledger.instruction_inheritance}")
        if ledger.restored_from_checkpoint_id or ledger.resume_invalidations:
            lines.append(
                f"  restored from {ledger.restored_from_checkpoint_id or '(none)'}"
                f" · invalidations "
                f"{', '.join(ledger.resume_invalidations) or '(none)'}")
        return lines

    def cache_lines(self) -> list[str]:
        snapshot = self.snapshot
        previous = self.previous_snapshot
        request_plan = self.prompt_plan
        if snapshot is None and request_plan is None:
            return ["  [cache] 尚无真实请求快照"]
        lines: list[str] = []
        if request_plan is not None and (
            request_plan.fragments or request_plan.tool_catalog_hash
            or request_plan.engine
        ):
            stable_count = len(request_plan.stable_fragments)
            volatile_count = len(request_plan.volatile_fragments)
            lines.extend([
                f"  [cache] prompt plan {request_plan.turn_id or '(unknown turn)'}"
                f" · Step {request_plan.step}"
                f" · stable {stable_count}/{request_plan.stable_tokens} tokens"
                f" · volatile {volatile_count}/{request_plan.volatile_tokens} tokens",
                f"  stable hash {_short_hash(request_plan.stable_hash)}"
                f" · dynamic hash {_short_hash(request_plan.dynamic_hash)}"
                f" · full hash {_short_hash(request_plan.full_hash)}"
                f" · instruction {_short_hash(request_plan.instruction_hash)}"
                f" · evidence {_short_hash(request_plan.evidence_hash)}",
                f"  cache boundary {request_plan.cache_boundary_chars} chars"
                f" · tool catalog {_short_hash(request_plan.tool_catalog_hash)}"
                f" · tools {request_plan.tool_count}",
                f"  stable fragments: "
                f"{_ids([item.id for item in request_plan.stable_fragments], 30)}",
                f"  volatile fragments: "
                f"{_ids([item.id for item in request_plan.volatile_fragments], 30)}",
                f"  omitted: {_ids([item.id for item in request_plan.omitted], 30)}",
            ])
            if request_plan.diff.available:
                diff = request_plan.diff
                lines.extend([
                    f"  changed: {_yes_no(diff.stable_changed)} (stable)"
                    f" · dynamic {_yes_no(diff.dynamic_changed)}"
                    f" · full {_yes_no(diff.full_changed)}"
                    f" · tools {_yes_no(diff.tool_catalog_changed)}",
                    f"  previous: {diff.previous_turn_id or '(unknown turn)'}"
                    f" Step {diff.previous_step}"
                    f" · token Δ stable {_signed(diff.stable_token_delta)}"
                    f" / volatile {_signed(diff.volatile_token_delta)}",
                ])
        if snapshot is None:
            return lines
        lines.extend([
            f"  [cache] stable blocks {snapshot.stable_system_block_count}"
            f" · dynamic blocks {snapshot.dynamic_system_block_count}"
            f" · stable {snapshot.stable_tokens}"
            f" · volatile {snapshot.volatile_tokens}"
            f" · mode {snapshot.cache_mode}"
            f" ({snapshot.cache_requested_mode or 'unknown'}"
            f" → {snapshot.cache_negotiation_reason or 'unavailable'})",
            f"  capabilities: "
            f"{', '.join(snapshot.cache_supported_modes) or '(unknown)'}"
            f" · max breakpoints {snapshot.cache_max_breakpoints}"
            f" · fallback {'yes' if snapshot.cache_fallback else 'no'}",
            f"  stable hash {_short_hash(snapshot.stable_hash)}"
            f" · dynamic hash {_short_hash(snapshot.dynamic_hash)}"
            f" · full hash {_short_hash(snapshot.full_hash)}",
            f"  changed: {self.cache_change_text()} (stable)"
            f" · dynamic {self.dynamic_cache_change_text()}",
            f"  system boundary {snapshot.cache_boundary_chars} chars"
            f" · cache_control boundary {snapshot.cache_control_boundary} chars"
            f" · segments {snapshot.cache_segment_count}"
            f" ({snapshot.cache_control_segment_count} explicit)",
            f"  cacheable tools {snapshot.tool_cacheable_tokens}"
            f" · messages {snapshot.message_cacheable_tokens}",
            f"  breakpoint plan {snapshot.cache_breakpoint_count}"
            f"/{snapshot.cache_max_breakpoints}"
            f" · strategy {snapshot.cache_breakpoint_strategy or '(none)'}"
            f" · skipped {snapshot.cache_breakpoint_skipped_count}"
            f" · minimum "
            f"{snapshot.cache_breakpoint_plan.get('minimum_cacheable_tokens', 0)}"
            f" tokens",
        ])
        raw_breakpoints = snapshot.cache_breakpoint_plan.get("breakpoints", ())
        if isinstance(raw_breakpoints, (list, tuple)):
            for breakpoint in raw_breakpoints:
                if not isinstance(breakpoint, dict):
                    continue
                lines.append(
                    f"  [breakpoint] {breakpoint.get('id', '?')}"
                    f" · {breakpoint.get('stability', '?')}"
                    f" · cumulative {breakpoint.get('cumulative_tokens', 0)} tokens"
                    f" · +{breakpoint.get('incremental_tokens', 0)}"
                    f" · {_short_hash(str(breakpoint.get('digest', '')))}")
        if previous is not None:
            lines.append(
                f"  previous stable {_short_hash(previous.stable_hash)}"
                f" · full {_short_hash(previous.full_hash)}")
        for segment in snapshot.cache_segments[:30]:
            lines.append(
                f"  [{segment.kind}] {segment.tokens} tokens"
                f" · {segment.chars} chars"
                f" · {segment.stability or 'unknown'}"
                f" · {'cache_control' if segment.cache_control else 'implicit'}"
                f" · {_short_hash(segment.digest)}")
        if len(snapshot.cache_segments) > 30:
            lines.append(
                f"  … 另有 {len(snapshot.cache_segments) - 30} 个 segment")
        ledger = self.cache_ledger
        if ledger is not None:
            ledger_diff = ledger.diff
            lines.append(
                f"  [ledger] {ledger.owner or 'main'}"
                f" · complete {_short_hash(ledger.complete_request_hash)}"
                f" · attachment generation {ledger.attachment_generation}"
                f" · compact {ledger.compact_checkpoint_id or '(none)'}"
                f" · working sequence {ledger.working_context_sequence}")
            lines.append(
                f"  [ledger/cache miss] {_yes_no(ledger_diff.cache_miss)}"
                f" · reasons "
                f"{', '.join(ledger_diff.reasons) if ledger_diff.reasons else '(none)'}")
        return lines

    def reminder_lines(self) -> list[str]:
        """Explain this turn's reminder selection without exposing content."""
        catalog = getattr(self.loop, "reminder_catalog", None)
        selection = getattr(self.loop, "last_reminder_selection", None)
        catalog_size = int(getattr(catalog, "size", 0) or 0)
        if selection is None:
            return [
                f"  [reminders] 尚无真实回合选择 · catalog {catalog_size}",
                "  content-light=true · 下一步模型请求开始时按 turn 边界选择",
            ]
        lines = [
            f"  [reminders] catalog {selection.catalog_size}"
            f" · selected {len(selection.selected)}"
            f" · excluded {len(selection.excluded)}"
            f" · duplicates {selection.duplicate_count}"
            f" · {selection.selected_tokens}/{selection.budget_tokens} tokens",
            f"  selection hash {_short_hash(selection.content_hash)}"
            f" · catalog v{selection.catalog_version} · L12 · turn-only"
            f" · cacheable=false · content-light=true",
        ]
        if not selection.selected:
            lines.append("  selected: (none)")
        for item in selection.selected:
            spec = item.spec
            lines.append(
                f"  [selected] {item.id} · source={spec.source}"
                f" · trigger={spec.trigger} · trust={spec.trust}"
                f" · priority={spec.priority} · {item.tokens} tokens"
                f" · requested={str(item.requested).lower()}"
                f" · reason={item.reason}")
        for item in selection.excluded:
            lines.append(
                f"  [excluded] {item.id} · {item.reason}"
                f" · source={item.source or '(unknown)'}"
                f" · trigger={item.trigger or '(unknown)'}"
                f" · {item.tokens} tokens")
        return lines

