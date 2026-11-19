"""Content-free inspection of the latest real context assembly.

The command layer and TUI both need to explain which context sources contributed
to a provider request. This module is intentionally read-only: it never loads
rules, scans memory files or invokes context sources. It only projects snapshots
already retained by ``AgentLoop``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .context_source import ContextAssembly, ContextLayer
from .context_plan import ContextPlanSnapshot
from .prompt_plan import (
    PromptFragmentSnapshot,
    PromptPlanSnapshot,
)
from .tokens import estimate_history_tokens


def _short_hash(value: str) -> str:
    return (value or "(none)")[:16]


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


def _ids(items: Sequence[str], limit: int = 20) -> str:
    if not items:
        return "(none)"
    shown = ", ".join(items[:limit])
    return shown + (f" · … 另有 {len(items) - limit} 个"
                    if len(items) > limit else "")


def _bounded(items: Sequence[object], limit: int) -> tuple[list[object], int]:
    if limit <= 0 or len(items) <= limit:
        return list(items), 0
    return list(items[:limit]), len(items) - limit


def _layer_memory_metadata(layer: ContextLayer) -> tuple[tuple[str, ...], dict[str, int], int]:
    metadata = layer.metadata or {}
    raw_ids = metadata.get("memory_entry_ids", ())
    ids = tuple(str(item) for item in raw_ids) if isinstance(
        raw_ids, (list, tuple, set)) else ()
    raw_scopes = metadata.get("memory_scope_counts", {})
    scopes = {
        str(key): max(0, int(value))
        for key, value in dict(raw_scopes).items()
    } if isinstance(raw_scopes, dict) else {}
    tokens = max(0, int(metadata.get("memory_tokens", 0) or 0))
    return ids, scopes, tokens


@dataclass(frozen=True)
class ContextInspectionService:
    """Project the last request's snapshot, plan and assembly into text."""

    loop: object

    @property
    def snapshot(self):
        return getattr(self.loop, "last_context_snapshot", None)

    @property
    def assembly(self) -> Optional[ContextAssembly]:
        return getattr(self.loop, "last_context_assembly", None)

    @property
    def plan(self) -> Optional[ContextPlanSnapshot]:
        return getattr(self.loop, "last_context_plan", None)

    @property
    def prompt_plan(self) -> Optional[PromptPlanSnapshot]:
        plan = self.plan
        return plan.prompt_plan if plan is not None else None

    @property
    def previous_snapshot(self):
        return getattr(self.loop, "previous_context_snapshot", None)

    @property
    def dropped_items(self) -> tuple[str, ...]:
        items: list[str] = []
        snapshot = self.snapshot
        if snapshot is not None:
            items.extend(str(item) for item in snapshot.dropped_items)
        workspace = getattr(self.loop, "workspace_context", None)
        for item in getattr(workspace, "omitted", ()) or ():
            value = f"workspace:{item}"
            if value not in items:
                items.append(value)
        return tuple(dict.fromkeys(items))

    @property
    def cache_changed(self) -> Optional[bool]:
        current = self.snapshot
        previous = self.previous_snapshot
        if current is None or previous is None:
            return None
        return bool(
            current.stable_hash
            and previous.stable_hash
            and current.stable_hash != previous.stable_hash
        )

    @property
    def dynamic_cache_changed(self) -> Optional[bool]:
        current = self.snapshot
        previous = self.previous_snapshot
        if current is None or previous is None:
            return None
        return bool(
            current.dynamic_hash
            and previous.dynamic_hash
            and current.dynamic_hash != previous.dynamic_hash
        )

    @property
    def memory_selection(self) -> tuple[tuple[str, ...], dict[str, int], int]:
        assembly = self.assembly
        if assembly is None:
            return (), {}, 0
        for layer in assembly.layers:
            ids, scopes, tokens = _layer_memory_metadata(layer)
            if ids or scopes or tokens:
                return ids, scopes, tokens
        return (), {}, 0

    def summary_lines(self, *, source_limit: int = 8) -> list[str]:
        snapshot = self.snapshot
        if snapshot is None:
            total = estimate_history_tokens(getattr(self.loop, "history", []))
            budget = getattr(getattr(self.loop, "context_manager", None),
                             "max_tokens", None)
            return [
                f"  [context] 尚无真实请求快照 · 历史约 {total} tokens"
                f" · 预算 {budget or '未启用压缩'}",
            ]

        sources = sorted(
            snapshot.source_tokens.items(), key=lambda item: (-item[1], item[0]))
        shown, omitted = _bounded(sources, source_limit)
        source_text = " · ".join(f"{name} {tokens}" for name, tokens in shown)
        memory_ids, memory_scopes, memory_tokens = self.memory_selection
        lines = [
            f"  [context] 最近请求 · {snapshot.turn_id or '(unknown turn)'}"
            f" · Step {snapshot.step}"
            f" · engine {snapshot.context_engine or 'unknown'}",
            f"  输入 {snapshot.estimated_input_tokens} tokens"
            f" / 预算 {snapshot.budget_tokens or '未启用'}"
            f" · 使用率 {snapshot.usage_ratio:.1%}" if snapshot.usage_ratio is not None
            else f"  输入 {snapshot.estimated_input_tokens} tokens"
            f" / 预算 {snapshot.budget_tokens or '未启用'}",
            f"  分项: system {snapshot.system_tokens}"
            f" · workspace {snapshot.workspace_tokens}"
            f" · memory {snapshot.memory_tokens}"
            f" · history {snapshot.history_tokens}"
            f" · runtime {snapshot.runtime_tokens}"
            f" · tools {snapshot.tool_schema_tokens}"
            f" · overhead {snapshot.overhead_tokens}",
            f"  stable {snapshot.stable_tokens} / volatile {snapshot.volatile_tokens}"
            f" · cache {snapshot.cache_mode}"
            f" · boundary {snapshot.cache_boundary_chars} chars",
            f"  stable hash {_short_hash(snapshot.stable_hash)}"
            f" · dynamic hash {_short_hash(snapshot.dynamic_hash)}"
            f" · full hash {_short_hash(snapshot.full_hash)}"
            f" · {self.cache_change_text()}",
            f"  source {snapshot.source_count} · fragment {snapshot.fragment_count}"
            f" · tools {snapshot.tool_count}",
            f"  来源: {source_text or '(none)'}"
            + (f" · 另有 {omitted} 个已省略" if omitted else ""),
            f"  memory 命中: {len(memory_ids)} 条"
            f" · {memory_tokens or snapshot.memory_tokens} tokens"
            f" · scopes {self._scope_text(memory_scopes)}",
            f"  被裁剪: {', '.join(self.dropped_items) if self.dropped_items else '无'}",
        ]
        manager = getattr(self.loop, "context_manager", None)
        high = getattr(manager, "high_watermark_tokens", 0) if manager else 0
        low = getattr(manager, "low_watermark_tokens", 0) if manager else 0
        if high or low:
            lines.append(f"  compact 水位: high {high} · low {low}")
        return lines

    def plan_lines(
        self,
        *,
        layer_limit: int = 100,
        source_limit: int = 100,
        fragment_limit: int = 100,
    ) -> list[str]:
        plan = self.plan
        if plan is None:
            return ["  [context plan] 尚无本会话请求计划"]
        layers, omitted_layers = _bounded(plan.layer_ids, layer_limit)
        sources, omitted_sources = _bounded(plan.sources, source_limit)
        categories, omitted_categories = _bounded(plan.categories, 30)
        request_plan = plan.prompt_plan
        lines = [
            f"  [context plan] {plan.turn_id or '(unknown turn)'}"
            f" · Step {plan.step} · {plan.token_count} tokens"
            f" · engine {plan.engine or 'legacy/unknown'}",
            f"  stable hash {_short_hash(plan.stable_hash)}"
            f" · dynamic hash {_short_hash(plan.dynamic_hash)}"
            f" · full hash {_short_hash(plan.full_hash)}",
            f"  history digest {_short_hash(plan.history_digest)}"
            f" · working-context sequence {plan.working_context_sequence}"
            + (f" · restored←{plan.restored_from}" if plan.restored_from else ""),
        ]
        if request_plan is not None and (
            request_plan.fragments or request_plan.tool_catalog_hash
            or request_plan.engine
        ):
            lines.extend(self._prompt_distribution_lines(
                request_plan, fragment_limit=fragment_limit))
            lines.extend(self._prompt_omission_lines(request_plan))
            lines.extend(self._prompt_memory_lines(request_plan))
            lines.extend(self._prompt_diff_lines(request_plan))
        lines.extend([
            f"  layers {len(plan.layer_ids)} · sources {len(plan.sources)}"
            f" · categories {', '.join(categories) or '(none)'}"
            + (f" · 另有 {omitted_categories} 个类别" if omitted_categories else ""),
            f"  trust: {', '.join(plan.trust_levels) or '(unknown)'}",
            "  layer ids: " + (", ".join(layers) or "(none)")
            + (f" · … 另有 {omitted_layers} 个" if omitted_layers else ""),
            "  sources: " + (", ".join(sources) or "(none)")
            + (f" · … 另有 {omitted_sources} 个" if omitted_sources else ""),
        ])
        return lines

    def _prompt_distribution_lines(
        self,
        plan: PromptPlanSnapshot,
        *,
        fragment_limit: int,
    ) -> list[str]:
        fragments, omitted = _bounded(plan.fragments, fragment_limit)
        stable_count = sum(1 for item in plan.fragments if item.stable)
        volatile_count = len(plan.fragments) - stable_count
        stable_sources: dict[str, int] = {}
        volatile_sources: dict[str, int] = {}
        for fragment in plan.fragments:
            target = stable_sources if fragment.stable else volatile_sources
            target[fragment.source] = target.get(fragment.source, 0) + fragment.tokens

        def source_tokens(values: dict[str, int]) -> str:
            ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))
            return " · ".join(f"{name} {tokens}" for name, tokens in ordered[:20])

        lines = [
            f"  prompt engine {plan.engine or 'unknown'}"
            f" · fragment {len(plan.fragments)}"
            f" · stable {stable_count}/{plan.stable_tokens} tokens"
            f" · volatile {volatile_count}/{plan.volatile_tokens} tokens",
            f"  stable sources: {source_tokens(stable_sources) or '(none)'}",
            f"  volatile sources: {source_tokens(volatile_sources) or '(none)'}",
            f"  cache boundary {plan.cache_boundary_chars} chars"
            f" · fragment tokens {plan.fragment_tokens}"
            f" · tools {plan.tool_count}"
            f" · tool catalog {_short_hash(plan.tool_catalog_hash)}",
        ]
        for fragment in fragments:
            if not isinstance(fragment, PromptFragmentSnapshot):
                continue
            placement = "stable" if fragment.stable else "volatile"
            lines.append(
                f"  [{placement}] {fragment.id} · {fragment.source}"
                f" · scope={fragment.scope} · {fragment.tokens} tokens"
                f" · {fragment.layer} · {fragment.stability}"
                f" · trust={fragment.trust}"
                f" · cacheable={str(fragment.cacheable).lower()}")
        if omitted:
            lines.append(f"  … 另有 {omitted} 个 fragment 未展示")
        return lines

    def _prompt_omission_lines(self, plan: PromptPlanSnapshot) -> list[str]:
        if not plan.omitted:
            return ["  omitted: 无"]
        lines = [f"  omitted: {len(plan.omitted)} 项"]
        for item in plan.omitted[:50]:
            lines.append(
                f"  [omitted/{item.reason or 'unspecified'}] {item.id}"
                f" · {item.source or '(unknown)'}"
                f" · scope={item.scope or '(unknown)'}"
                f" · {item.tokens} tokens"
                + (f" · {item.layer}" if item.layer else ""))
        if len(plan.omitted) > 50:
            lines.append(f"  … 另有 {len(plan.omitted) - 50} 个裁剪项")
        return lines

    def _prompt_memory_lines(self, plan: PromptPlanSnapshot) -> list[str]:
        if not plan.memory_hits:
            return ["  memory hits: 无"]
        lines = [f"  memory hits: {len(plan.memory_hits)}"]
        for hit in plan.memory_hits[:30]:
            lines.append(
                f"  [memory] {hit.id} · source={hit.source}"
                f" · scope={hit.scope or '(unknown)'}"
                f" · kind={hit.kind or '(unknown)'}"
                f" · origin={hit.origin or '(unknown)'}"
                f" · {hit.tokens} tokens")
        if len(plan.memory_hits) > 30:
            lines.append(f"  … 另有 {len(plan.memory_hits) - 30} 条 memory hit")
        return lines

    def _prompt_diff_lines(self, plan: PromptPlanSnapshot) -> list[str]:
        diff = plan.diff
        if not diff.available:
            return ["  diff: 无上一请求可比较"]
        lines = [
            f"  diff ← {diff.previous_turn_id or '(unknown turn)'}"
            f" Step {diff.previous_step}"
            f" · changed={_yes_no(diff.changed)}"
            f" · tokens stable {_signed(diff.stable_token_delta)}"
            f" / volatile {_signed(diff.volatile_token_delta)}",
            f"  added: {_ids(diff.added_fragments)}"
            f" · removed: {_ids(diff.removed_fragments)}"
            f" · changed: {_ids(diff.changed_fragments)}",
            f"  hash changed: stable={_yes_no(diff.stable_changed)}"
            f" · dynamic={_yes_no(diff.dynamic_changed)}"
            f" · full={_yes_no(diff.full_changed)}"
            f" · history={_yes_no(diff.history_changed)}"
            f" · tools={_yes_no(diff.tool_catalog_changed)}"
            f" · memory={_yes_no(diff.memory_changed)}"
            f" · omitted={_yes_no(diff.omitted_changed)}",
        ]
        return lines

    def why_lines(self, target: str) -> list[str]:
        needle = str(target or "").strip()
        if not needle:
            return [
                "  用法: /context why "
                "<fragment-id|source|memory-id|layer-id|token-source|omitted-id>"
            ]
        assembly = self.assembly
        snapshot = self.snapshot
        request_plan = self.prompt_plan
        if assembly is None and snapshot is None and request_plan is None:
            return ["  [context why] 尚无真实请求快照"]

        prompt_match, prompt_lines = self._prompt_why_lines(
            request_plan, needle)
        if prompt_match:
            return prompt_lines

        exact: list[ContextLayer] = []
        partial: list[ContextLayer] = []
        if assembly is not None:
            for layer in assembly.layers:
                if needle == layer.id or needle == layer.source:
                    exact.append(layer)
                elif needle in layer.id or needle in layer.source:
                    partial.append(layer)
        matches = exact or partial
        lines: list[str] = []
        if matches:
            layer = matches[0]
            lines.extend([
                f"  [context why] {layer.id}",
                f"  source {layer.source} · category {layer.category}"
                f" · trust {layer.trust} · origin {layer.origin or '(unspecified)'}"
                f" · priority {layer.priority} · {layer.tokens} tokens",
                f"  cache {'prefix' if layer.cacheable else 'dynamic'}"
                f" · compactable {layer.compactable}"
                f" · stable-boundary {layer.stable_prefix_chars}",
                "  metadata keys: "
                + (", ".join(sorted(str(key) for key in layer.metadata)) or "(none)"),
            ])
            if layer.metadata.get("source_role"):
                lines.insert(2, (
                    "  authority: "
                    f"role={layer.metadata.get('source_role', 'unknown')}"
                    f" · type={layer.metadata.get('evidence_type', '(unspecified)')}"
                    " · instruction="
                    f"{str(bool(layer.metadata.get('instruction', False))).lower()}"
                    " · requires-validation="
                    f"{str(bool(layer.metadata.get('requires_validation', False))).lower()}"
                ))
            for contribution in layer.effective_token_contributions():
                lines.append(
                    f"  token ← {contribution.source}"
                    f" [{contribution.category}] {contribution.tokens}")
            if len(matches) > 1:
                lines.append(
                    "  其他匹配: " + ", ".join(item.id for item in matches[1:20]))
        elif snapshot is not None and needle in snapshot.source_tokens:
            lines.append(
                f"  [context why] token source {needle}"
                f" · {snapshot.source_tokens[needle]} tokens"
                " · 该来源在最近请求中有 token 贡献，但没有独立 layer")
        elif needle in self.dropped_items:
            lines.append(
                f"  [context why] {needle} 在最近请求中被裁剪"
                " · 未计入 provider 请求正文")
        else:
            return [f"  [context why] 未找到: {needle}"]

        if self.plan is not None:
            plan_has_layer = needle in self.plan.layer_ids
            plan_has_source = needle in self.plan.sources
            lines.append(
                f"  plan: {'包含 layer' if plan_has_layer else '未记录为该 layer'}"
                f" · {'包含 source' if plan_has_source else '未记录为该 source'}")
        return lines

    def _prompt_why_lines(
        self,
        plan: Optional[PromptPlanSnapshot],
        needle: str,
    ) -> tuple[bool, list[str]]:
        if plan is None:
            return False, []
        fragment_exact = [
            item for item in plan.fragments
            if needle == item.id or needle == item.source
        ]
        fragment_partial = [
            item for item in plan.fragments
            if needle in item.id or needle in item.source
        ]
        fragments = fragment_exact or fragment_partial
        if fragments:
            fragment = fragments[0]
            placement = "stable-prefix" if fragment.stable else "volatile"
            lines = [
                f"  [context why] {fragment.id}",
                f"  source {fragment.source} · scope {fragment.scope}"
                f" · layer {fragment.layer} · stability {fragment.stability}"
                f" · trust {fragment.trust} · {fragment.tokens} tokens",
                f"  cache {placement} · cacheable "
                f"{str(fragment.cacheable).lower()}"
                f" · content hash {_short_hash(fragment.content_hash)}",
            ]
            if fragment.id in plan.dropped_items:
                lines.append("  budget: 该 fragment 同时记录在 dropped items 中")
            for hit in plan.memory_hits:
                if (
                    fragment.id == "memory:scoped"
                    or fragment.source in {"memory", "memory_retrieval"}
                ):
                    lines.append(
                        f"  memory hit {hit.id} · source={hit.source}"
                        f" · scope={hit.scope or '(unknown)'}"
                        f" · kind={hit.kind or '(unknown)'}"
                        f" · origin={hit.origin or '(unknown)'}"
                        f" · {hit.tokens} tokens")
            if len(fragments) > 1:
                lines.append("  其他匹配: " + _ids(
                    [item.id for item in fragments[1:]]))
            if plan.diff.available:
                lines.append(
                    f"  previous request: {plan.diff.previous_turn_id}"
                    f" Step {plan.diff.previous_step}")
            return True, lines

        memory_hits = [item for item in plan.memory_hits if needle == item.id]
        if memory_hits:
            hit = memory_hits[0]
            memory_fragment = next(
                (item for item in plan.fragments
                 if item.id == "memory:scoped"
                 or item.source in {"memory", "memory_retrieval"}),
                None,
            )
            return True, [
                f"  [context why] {hit.id}",
                f"  memory hit · source={hit.source}"
                f" · scope={hit.scope or '(unknown)'}"
                f" · kind={hit.kind or '(unknown)'}"
                f" · origin={hit.origin or '(unknown)'}"
                f" · {hit.tokens} tokens",
                "  authority: role=evidence · instruction=false"
                " · requires-validation=true",
                (
                    f"  fragment {memory_fragment.id} · "
                    f"{memory_fragment.tokens} tokens · volatile"
                    if memory_fragment is not None
                    else "  fragment: 未记录独立 memory fragment"
                ),
            ]

        omission_exact = [
            item for item in plan.omitted
            if needle == item.id or needle == item.source
        ]
        omission_partial = [
            item for item in plan.omitted
            if needle in item.id or needle in item.source
        ]
        omissions = omission_exact or omission_partial
        if omissions:
            item = omissions[0]
            return True, [
                f"  [context why] {item.id}",
                f"  source {item.source or '(unknown)'}"
                f" · scope {item.scope or '(unknown)'}"
                f" · layer {item.layer or '(unknown)'}"
                f" · stability {item.stability or '(unknown)'}"
                f" · trust {item.trust or '(unknown)'}"
                f" · {item.tokens} tokens",
                f"  omitted: {item.reason or 'unspecified'}"
                " · 未计入 provider 请求正文",
            ]
        return False, []

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
                f" · full hash {_short_hash(request_plan.full_hash)}",
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
            f" · mode {snapshot.cache_mode}",
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
        ])
        if previous is not None:
            lines.append(
                f"  previous stable {_short_hash(previous.stable_hash)}"
                f" · full {_short_hash(previous.full_hash)}")
        for segment in snapshot.cache_segments[:30]:
            lines.append(
                f"  [{segment.kind}] {segment.tokens} tokens"
                f" · {segment.chars} chars"
                f" · {'cache_control' if segment.cache_control else 'implicit'}"
                f" · {_short_hash(segment.digest)}")
        if len(snapshot.cache_segments) > 30:
            lines.append(
                f"  … 另有 {len(snapshot.cache_segments) - 30} 个 segment")
        return lines

    def memory_metadata(self) -> tuple[tuple[str, ...], dict[str, int], int]:
        return self.memory_selection

    def cache_change_text(self) -> str:
        changed = self.cache_changed
        if changed is None:
            return "无上一请求可比较"
        return "是" if changed else "否"

    def dynamic_cache_change_text(self) -> str:
        changed = self.dynamic_cache_changed
        if changed is None:
            return "无上一请求可比较"
        return "是" if changed else "否"

    @staticmethod
    def _scope_text(scopes: dict[str, int]) -> str:
        if not scopes:
            return "(none)"
        return " · ".join(
            f"{scope} {count}" for scope, count in sorted(scopes.items()))
