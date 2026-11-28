"""Deterministic, conflict-aware middleware pipeline for the Agent Loop."""

from __future__ import annotations

from typing import Iterable

from ...contracts.middleware import (
    AbilityManifest,
    ExtensionLifecyclePoint,
    LoopAbilityPort,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareEvaluation,
    MiddlewareFailurePolicy,
    MiddlewarePipelineResult,
    PatchScope,
)


class MiddlewarePipelineError(RuntimeError):
    """An ability manifest or dependency graph is invalid."""


class LoopMiddlewarePipeline:
    def __init__(
        self,
        abilities: Iterable[LoopAbilityPort] = (),
    ) -> None:
        self._abilities: dict[str, LoopAbilityPort] = {}
        self._ordered: tuple[LoopAbilityPort, ...] = ()
        for ability in abilities:
            manifest = ability.manifest
            if manifest.id in self._abilities:
                raise MiddlewarePipelineError(
                    f"Ability 重复注册: {manifest.id}")
            self._abilities[manifest.id] = ability
        self._ordered = self._resolve_order()

    @property
    def manifests(self) -> tuple[AbilityManifest, ...]:
        return tuple(ability.manifest for ability in self._ordered)

    def register(self, ability: LoopAbilityPort) -> None:
        manifest = ability.manifest
        if manifest.id in self._abilities:
            raise MiddlewarePipelineError(
                f"Ability 重复注册: {manifest.id}")
        self._abilities[manifest.id] = ability
        self._ordered = self._resolve_order()

    def unregister(self, ability_id: str) -> bool:
        if ability_id not in self._abilities:
            return False
        del self._abilities[ability_id]
        self._ordered = self._resolve_order()
        return True

    async def evaluate(
        self,
        point: ExtensionLifecyclePoint | str,
        context: MiddlewareContext,
    ) -> MiddlewarePipelineResult:
        lifecycle = ExtensionLifecyclePoint(point)
        if context.lifecycle_point is not lifecycle:
            context = MiddlewareContext(
                turn_id=context.turn_id,
                phase=context.phase,
                step=context.step,
                lifecycle_point=lifecycle,
                goal=context.goal,
                permission_mode=context.permission_mode,
                plan=context.plan,
                budget=context.budget,
                observation_ids=context.observation_ids,
                metadata=context.metadata,
            )
        evaluations: list[MiddlewareEvaluation] = []
        decisions: list[tuple[LoopAbilityPort, MiddlewareDecision]] = []
        for ability in self._ordered:
            manifest = ability.manifest
            if lifecycle not in manifest.lifecycle_points:
                continue
            try:
                decision = await ability.evaluate(context)
                self._validate_patch_scopes(manifest, decision)
            except Exception as exc:
                policy = MiddlewareFailurePolicy(manifest.failure_policy)
                error = f"{type(exc).__name__}: {exc}"
                decision = MiddlewareDecision(
                    action=(
                        MiddlewareAction.DENY
                        if policy is MiddlewareFailurePolicy.FAIL_CLOSED
                        else MiddlewareAction.ALLOW
                    ),
                    reason=(
                        f"ability failed ({policy.value}): {error}"
                        if policy is not MiddlewareFailurePolicy.FAIL_OPEN
                        else ""
                    ),
                    metadata={"error": error, "policy": policy.value},
                )
                evaluations.append(MiddlewareEvaluation(
                    ability_id=manifest.id,
                    ability_version=manifest.version,
                    priority=manifest.priority,
                    lifecycle_point=lifecycle,
                    action=MiddlewareAction(decision.action),
                    reason=decision.reason,
                    error=error,
                ))
                decisions.append((ability, decision))
                continue
            evaluations.append(MiddlewareEvaluation(
                ability_id=manifest.id,
                ability_version=manifest.version,
                priority=manifest.priority,
                lifecycle_point=lifecycle,
                action=MiddlewareAction(decision.action),
                reason=decision.reason,
                patch_count=len(decision.patches),
            ))
            decisions.append((ability, decision))

        denied = [
            decision for _ability, decision in decisions
            if decision.action is MiddlewareAction.DENY
        ]
        if denied:
            return MiddlewarePipelineResult(
                lifecycle_point=lifecycle,
                action=MiddlewareAction.DENY,
                reason="; ".join(
                    item.reason or "middleware denied" for item in denied),
                evaluations=tuple(evaluations),
            )
        short_circuit = next(
            (
                decision for _ability, decision in decisions
                if decision.action is MiddlewareAction.SHORT_CIRCUIT
            ),
            None,
        )
        patches = tuple(
            patch
            for _ability, decision in decisions
            if decision.action in {
                MiddlewareAction.PATCH,
                MiddlewareAction.ALLOW,
                MiddlewareAction.SHORT_CIRCUIT,
            }
            for patch in decision.patches
        )
        action = (
            MiddlewareAction.SHORT_CIRCUIT
            if short_circuit is not None
            else MiddlewareAction.PATCH if patches
            else MiddlewareAction.ALLOW
        )
        return MiddlewarePipelineResult(
            lifecycle_point=lifecycle,
            action=action,
            patches=patches,
            reason=short_circuit.reason if short_circuit is not None else "",
            short_circuit_text=(
                short_circuit.short_circuit_text
                if short_circuit is not None else ""),
            evaluations=tuple(evaluations),
        )

    def _resolve_order(self) -> tuple[LoopAbilityPort, ...]:
        manifests = {
            item.manifest.id: item.manifest
            for item in self._abilities.values()
        }
        for manifest in manifests.values():
            unknown = set(manifest.depends_on) - set(manifests)
            if unknown:
                raise MiddlewarePipelineError(
                    f"Ability {manifest.id} 依赖未知能力: "
                    + ", ".join(sorted(unknown)))
        resolved: list[str] = []
        pending = set(manifests)
        while pending:
            ready = [
                ability_id for ability_id in pending
                if set(manifests[ability_id].depends_on).issubset(resolved)
            ]
            if not ready:
                raise MiddlewarePipelineError(
                    "Ability 依赖存在环: " + ", ".join(sorted(pending)))
            ability_id = min(
                ready,
                key=lambda item: (-manifests[item].priority, item),
            )
            resolved.append(ability_id)
            pending.remove(ability_id)
        return tuple(self._abilities[item] for item in resolved)

    @staticmethod
    def _validate_patch_scopes(
        manifest: AbilityManifest,
        decision: MiddlewareDecision,
    ) -> None:
        allowed = set(manifest.write_scopes)
        for patch in decision.patches:
            if PatchScope(patch.scope) not in allowed:
                raise MiddlewarePipelineError(
                    f"Ability {manifest.id} 未声明 Patch 权限: {patch.scope}")


__all__ = ["LoopMiddlewarePipeline", "MiddlewarePipelineError"]
