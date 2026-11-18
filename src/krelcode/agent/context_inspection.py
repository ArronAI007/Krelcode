"""Content-free inspection of the latest real context assembly.

The command layer and TUI both need to explain which context sources contributed
to a provider request. This module is intentionally read-only: it never loads
rules, scans memory files or invokes context sources. It only projects snapshots
already retained by ``AgentLoop``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .context_source import ContextAssembly, ContextLayer
from .context_plan import ContextPlanSnapshot
from .tokens import estimate_history_tokens


def _short_hash(value: str) -> str:
    return (value or "(none)")[:16]


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
    ) -> list[str]:
        plan = self.plan
        if plan is None:
            return ["  [context plan] 尚无本会话请求计划"]
        layers, omitted_layers = _bounded(plan.layer_ids, layer_limit)
        sources, omitted_sources = _bounded(plan.sources, source_limit)
        categories, omitted_categories = _bounded(plan.categories, 30)
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
            f"  layers {len(plan.layer_ids)} · sources {len(plan.sources)}"
            f" · categories {', '.join(categories) or '(none)'}"
            + (f" · 另有 {omitted_categories} 个类别" if omitted_categories else ""),
            f"  trust: {', '.join(plan.trust_levels) or '(unknown)'}",
            "  layer ids: " + (", ".join(layers) or "(none)")
            + (f" · … 另有 {omitted_layers} 个" if omitted_layers else ""),
            "  sources: " + (", ".join(sources) or "(none)")
            + (f" · … 另有 {omitted_sources} 个" if omitted_sources else ""),
        ]
        return lines

    def why_lines(self, target: str) -> list[str]:
        needle = str(target or "").strip()
        if not needle:
            return ["  用法: /context why <layer-id|source|token-source>"]
        assembly = self.assembly
        snapshot = self.snapshot
        if assembly is None and snapshot is None:
            return ["  [context why] 尚无真实请求快照"]

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
            lines.append(
                f"  [{layer.priority}] {layer.id} · {layer.source}"
                f" · {layer.category} · {layer.tokens} tokens"
                f" · trust={layer.trust}"
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
        if snapshot is None:
            return ["  [cache] 尚无真实请求快照"]
        lines = [
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
        ]
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
