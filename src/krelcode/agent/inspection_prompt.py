"""Prompt-plan, source-attribution, conflict and why projections."""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from .inspection_helpers import (
    _bounded,
    _ids,
    _int_value,
    _is_memory_fragment,
    _join_provenance,
    _short_hash,
    _signed,
    _yes_no,
)
from .prompt_plan import PromptFragmentSnapshot, PromptPlanSnapshot

class ContextInspectionPromptMixin:
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
            lines.extend(self._prompt_tool_spec_lines(request_plan))
            lines.extend(self._prompt_conflict_lines(request_plan))
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

    def _prompt_tool_spec_lines(
        self,
        plan: PromptPlanSnapshot,
        *,
        records: Optional[Sequence[Mapping[str, object]]] = None,
        limit: int = 100,
    ) -> list[str]:
        """Render content-free ToolPromptSpec diagnostics.

        Tool descriptions, JSON Schemas, instructions and example bodies are
        intentionally omitted. The record is enough to explain which model
        contract was active, whether its wire projection changed and how much
        guidance accompanied it.
        """
        source = tuple(records) if records is not None else tuple(
            plan.tool_prompt_specs)
        if not source:
            return []
        shown, omitted = _bounded(source, limit)
        lines = [
            f"  tool prompt specs: {len(source)}"
            f" · records shown {len(shown)}"
        ]
        for record in shown:
            if not isinstance(record, Mapping):
                continue
            name = str(record.get("name", "") or "(unknown)")
            playbook = str(record.get("playbook_id", "") or "(none)")
            when_to_use = _int_value(record.get("when_to_use_count"))
            when_not_to_use = _int_value(record.get("when_not_to_use_count"))
            examples = _int_value(record.get("example_count"))
            lines.append(
                f"  [tool] {name}"
                f" · source={str(record.get('source', '') or '(unknown)')}"
                f" · playbook={playbook}"
                f" · guidance={when_to_use}+{when_not_to_use}"
                f" · instructions={_int_value(record.get('instructions_chars'))} chars"
                f" · examples={examples}"
                f" · wire={_short_hash(str(record.get('wire_hash', '') or ''))}"
                f" · content={_short_hash(str(record.get('content_hash', '') or ''))}")
        if omitted:
            lines.append(f"  … 另有 {omitted} 个 tool prompt spec 未展示")
        return lines

    def _prompt_conflict_lines(
        self,
        plan: PromptPlanSnapshot,
        *,
        records: Optional[Sequence[Mapping[str, object]]] = None,
        limit: int = 100,
    ) -> list[str]:
        """Render conflict metadata without instruction text or excerpts."""
        source = tuple(records) if records is not None else tuple(plan.conflicts)
        if not source:
            return []
        shown, omitted = _bounded(source, limit)
        lines = [
            f"  prompt conflicts: {len(source)}"
            f" · unresolved {sum(bool(item.get('unresolved', False)) for item in source)}"
            f" · analyzed {plan.conflict_analyzed_fragment_count}"
            f" · directives {plan.conflict_directive_count}"
            f" · analyzer hash {_short_hash(plan.conflict_hash)}",
        ]
        for item in shown:
            if not isinstance(item, Mapping):
                continue
            lines.append(
                f"  [conflict] {item.get('id', '?')}"
                f" · {item.get('severity', 'unknown')}/{item.get('status', 'unknown')}"
                f" · {item.get('code', 'unknown')}"
                f" · {item.get('left_fragment_id', '?')}"
                f" → {item.get('right_fragment_id', '?')}"
                f" · winner={item.get('winner_fragment_id', '(none)')}"
                f" · confidence={_int_value(item.get('confidence'))}"
                f" · topic={_short_hash(str(item.get('topic_hash', '') or ''))}"
                f" · resolution={item.get('resolution', '(unknown)')}")
        if omitted:
            lines.append(f"  … 另有 {omitted} 个 prompt conflict 未展示")
        return lines

    def conflict_lines(self, *, limit: int = 100) -> list[str]:
        """Explain the latest PromptPlan conflict report."""
        plan = self.prompt_plan
        if plan is None:
            return ["  [conflicts] 尚无最近请求的 Prompt conflict report"]
        if not plan.conflicts:
            return [
                f"  [conflicts] none"
                f" · analyzed {plan.conflict_analyzed_fragment_count}"
                f" · directives {plan.conflict_directive_count}"
                f" · analyzer hash {_short_hash(plan.conflict_hash)}",
            ]
        return self._prompt_conflict_lines(plan, limit=limit)

    def _related_conflict_lines(
        self,
        fragment_id: str,
        *,
        limit: int = 20,
    ) -> list[str]:
        plan = self.prompt_plan
        if plan is None:
            return []
        records = [
            item for item in plan.conflicts
            if fragment_id in {
                str(item.get("left_fragment_id", "")),
                str(item.get("right_fragment_id", "")),
                str(item.get("winner_fragment_id", "")),
            }
        ]
        return self._prompt_conflict_lines(plan, records=records, limit=limit)

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
        if needle in {"conflict", "conflicts"}:
            return True, self.conflict_lines()
        if needle.startswith("conflict:"):
            conflict_id = needle.split(":", 1)[1]
            records = tuple(
                item for item in plan.conflicts
                if str(item.get("id", "")) == conflict_id
            )
            if records:
                return True, self._prompt_conflict_lines(
                    plan, records=records)
        tool_specs = [
            item for item in plan.tool_prompt_specs
            if needle in {
                str(item.get("name", "")),
                f"tool:{item.get('name', '')}",
                f"tool_prompt_spec:{item.get('name', '')}",
            }
        ]
        if tool_specs:
            return True, self._prompt_tool_spec_lines(plan, records=tool_specs)
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
            provenance = dict(fragment.provenance)
            if provenance.get("source_role"):
                lines.append(
                    f"  authority: role={provenance.get('source_role')}"
                    f" · instruction={str(provenance.get('instruction', False)).lower()}"
                    f" · source={provenance.get('instruction_source', '(unknown)')}")
            profile_name = str(provenance.get("prompt_profile", "") or "")
            if profile_name:
                lines.append(
                    f"  prompt profile: {profile_name}"
                    f" · match={provenance.get('prompt_profile_match', '(unknown)')}"
                    f" · provider={provenance.get('prompt_provider', '') or '(none)'}"
                    f" · model={provenance.get('prompt_model', '') or '(none)'}")
                lines.append(
                    f"  base hash: {provenance.get('base_prompt_hash', '')}"
                    f" · section={provenance.get('prompt_section', fragment.id)}")
                lines.append(
                    f"  prompt release: {provenance.get('prompt_release_id', '') or '(unknown)'}"
                    f" · hash={provenance.get('prompt_release_hash', '') or '(none)'}"
                    f" · profile hash={provenance.get('prompt_profile_hash', '') or '(none)'}")
                lines.append(
                    f"  calibration: {provenance.get('profile_calibration_status', 'uncalibrated')}"
                    f" · id={provenance.get('profile_calibration_id', '') or '(none)'}"
                    f" · hash={provenance.get('profile_calibration_hash', '') or '(none)'}"
                    f" · {provenance.get('profile_calibration_reason', '') or '(no detail)'}")
            contract_version = str(
                provenance.get("contract_version", "") or "")
            if contract_version:
                lines.extend((
                    f"  mode contract: v{contract_version}"
                    f" · run={provenance.get('run_mode', '(unknown)')}"
                    f" · permission={provenance.get('permission_mode', '(unknown)')}"
                    f" · approval={provenance.get('approval_profile', '(unknown)')}",
                    f"  sandbox: backend={provenance.get('sandbox_backend', '(unknown)')}"
                    f" · network-controlled={provenance.get('sandbox_network_controlled', '?')}"
                    f" (allowed={provenance.get('sandbox_network_allowed', '?')})"
                    f" · os-enforced={provenance.get('sandbox_os_enforced', '?')}"
                    f" · protected-paths={provenance.get('protected_paths_enforced', '?')}",
                    f"  tool contract: eligible={provenance.get('eligible_tool_count', 0)}"
                    f" · excluded={provenance.get('excluded_tool_count', 0)}"
                    f" · read-only={provenance.get('read_only_tool_count', 0)}"
                    f" · mutating={provenance.get('mutating_tool_count', 0)}"
                    f" · local-read-only={provenance.get('local_read_only_tool_count', 0)}"
                    f" · loaded={provenance.get('loaded_tool_count', 0)}"
                    f" · advertised={provenance.get('advertised_tool_count', 0)}",
                    f"  contract hash: {provenance.get('contract_hash', '')}"
                    f" · exposure hash: {provenance.get('tool_exposure_hash', '')}",
                ))
            rule_scope = str(provenance.get("rule_scope", "") or "")
            if rule_scope:
                overrides = provenance.get("rule_overrides", ())
                override_text = ", ".join(str(item) for item in overrides) or "(none)"
                lines.extend((
                    f"  effective rule source: {fragment.id}"
                    f" · scope={rule_scope}"
                    f" · precedence={provenance.get('rule_precedence', '?')}"
                    f" · overrides={override_text}",
                    f"  rule files: {_join_provenance(provenance.get('rule_sources'))}",
                    f"  imports={provenance.get('rule_import_count', 0)}"
                    f" · issues={len(provenance.get('rule_issues', ()))}"
                    f" · conflict policy={provenance.get('rule_conflict_policy', '(unknown)')}",
                ))
                for issue in provenance.get("rule_issues", ()):
                    if not isinstance(issue, dict):
                        continue
                    lines.append(
                        f"  [rule issue] {issue.get('code', 'unknown')}"
                        f" · {issue.get('path', '?')}"
                        f" · {issue.get('message', '')}")
            if fragment.id in plan.dropped_items:
                lines.append("  budget: 该 fragment 同时记录在 dropped items 中")
            for hit in plan.memory_hits:
                if _is_memory_fragment(fragment):
                    lines.append(
                        f"  memory hit {hit.id} · source={hit.source}"
                        f" · scope={hit.scope or '(unknown)'}"
                        f" · kind={hit.kind or '(unknown)'}"
                        f" · origin={hit.origin or '(unknown)'}"
                        f" · {hit.tokens} tokens")
            if len(fragments) > 1:
                lines.append("  其他匹配: " + _ids(
                    [item.id for item in fragments[1:]]))
            lines.extend(self._related_conflict_lines(fragment.id))
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
                 if _is_memory_fragment(item)),
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

