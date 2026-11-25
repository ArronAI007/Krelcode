"""Snapshot access, summary projection and common formatting state."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Protocol, cast

from krelcode.core.context_usage import ContextUsageSnapshot
from krelcode.agent.context.context_cache_ledger import ContextCacheLedger
from krelcode.agent.context.context_source import ContextAssembly
from krelcode.agent.context.context_plan import ContextPlanSnapshot
from krelcode.agent.context.inspection_helpers import (
    _bounded,
    _layer_memory_metadata,
    _short_hash,
)
from krelcode.agent.prompt.prompt_plan import PromptPlanSnapshot
from krelcode.core.tokens import estimate_history_tokens

if TYPE_CHECKING:
    from krelcode.agent.turn.runtime_services import AgentServices
    from krelcode.agent.turn.runtime_state import AgentRuntimeState
    from krelcode.core.messages import Message


class ContextInspectionHostPort(Protocol):
    services: "AgentServices"
    runtime_state: "AgentRuntimeState"
    history: list["Message"]


class ContextInspectionCoreMixin:
    loop: object

    @property
    def _host(self) -> ContextInspectionHostPort:
        return cast(ContextInspectionHostPort, self.loop)

    @property
    def snapshot(self) -> Optional[ContextUsageSnapshot]:
        return self._host.runtime_state.context.last_context_snapshot

    @property
    def assembly(self) -> Optional[ContextAssembly]:
        return self._host.runtime_state.context.last_context_assembly

    @property
    def plan(self) -> Optional[ContextPlanSnapshot]:
        return self._host.runtime_state.context.last_context_plan

    @property
    def prompt_plan(self) -> Optional[PromptPlanSnapshot]:
        plan = self.plan
        return plan.prompt_plan if plan is not None else None

    @property
    def previous_snapshot(self) -> Optional[ContextUsageSnapshot]:
        return self._host.runtime_state.context.previous_context_snapshot

    @property
    def cache_ledger(self) -> Optional[ContextCacheLedger]:
        ledger = self._host.runtime_state.context.last_context_cache_ledger
        if ledger is not None:
            return ledger
        plan = self.plan
        ledger = plan.cache_ledger if plan is not None else None
        return ledger if ledger is not None and ledger.has_request_identity else None

    @property
    def dropped_items(self) -> tuple[str, ...]:
        items: list[str] = []
        snapshot = self.snapshot
        if snapshot is not None:
            items.extend(str(item) for item in snapshot.dropped_items)
        workspace = self._host.runtime_state.context.workspace_context
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
            total = estimate_history_tokens(self._host.history)
            manager = self._host.services.context.manager
            budget = getattr(manager, "max_tokens", None)
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
        if self.prompt_plan is not None:
            lines.append(
                f"  prompt conflicts: {self.prompt_plan.conflict_count}"
                f" · unresolved {self.prompt_plan.unresolved_conflict_count}"
                f" · analyzer hash {_short_hash(self.prompt_plan.conflict_hash)}")
        manager = self._host.services.context.manager
        high = getattr(manager, "high_watermark_tokens", 0) if manager else 0
        low = getattr(manager, "low_watermark_tokens", 0) if manager else 0
        if high or low:
            lines.append(f"  compact 水位: high {high} · low {low}")
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
