"""Deterministic lifecycle middleware, ordering and state-transition matrix."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.turn.loop_state import (
    AgentLoopTerminated,
    AgentLoopStateMachine,
    LoopTransitionError,
)
from krelcode.agent.turn.middleware import (
    LoopMiddlewarePipeline,
    MiddlewarePipelineError,
)
from krelcode.contracts.loop import LoopPhase
from krelcode.contracts.middleware import (
    AbilityManifest,
    ExtensionLifecyclePoint,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareFailurePolicy,
    PatchOperation,
    PatchScope,
    StatePatch,
)
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.contracts.tool import (
    SideEffectKind,
    ToolDescriptor,
    ToolSideEffectContract,
)
from krelcode.core.messages import Message, ToolCall


class AbilityProbe:
    def __init__(
        self,
        ability_id: str,
        *,
        priority: int = 0,
        depends_on: tuple[str, ...] = (),
        points: tuple[ExtensionLifecyclePoint, ...] = (
            ExtensionLifecyclePoint.PREPARE_BEFORE,
        ),
        action: MiddlewareAction = MiddlewareAction.ALLOW,
        patches: tuple[StatePatch, ...] = (),
        fail_policy: MiddlewareFailurePolicy = MiddlewareFailurePolicy.FAIL_CLOSED,
        raises: bool = False,
        calls: list[str] | None = None,
    ) -> None:
        self._manifest = AbilityManifest(
            id=ability_id,
            priority=priority,
            depends_on=depends_on,
            lifecycle_points=points,
            write_scopes=tuple(patch.scope for patch in patches),
            failure_policy=fail_policy,
        )
        self.action = action
        self.patches = patches
        self.raises = raises
        self.calls = calls if calls is not None else []

    @property
    def manifest(self) -> AbilityManifest:
        return self._manifest

    async def evaluate(self, context: MiddlewareContext) -> MiddlewareDecision:
        self.calls.append(self.manifest.id)
        if self.raises:
            raise RuntimeError(f"{self.manifest.id} failed")
        return MiddlewareDecision(
            action=self.action,
            patches=self.patches,
            short_circuit_text=(
                "middleware result"
                if self.action is MiddlewareAction.SHORT_CIRCUIT else ""),
        )


def _context(point: ExtensionLifecyclePoint) -> MiddlewareContext:
    return MiddlewareContext(
        turn_id="turn-1",
        phase="prepare",
        step=1,
        lifecycle_point=point,
    )


def test_middleware_order_respects_dependencies_then_priority() -> None:
    calls: list[str] = []
    pipeline = LoopMiddlewarePipeline([
        AbilityProbe("high", priority=100, calls=calls),
        AbilityProbe("dependent", priority=200, depends_on=("low",), calls=calls),
        AbilityProbe("low", priority=1, calls=calls),
    ])

    asyncio.run(pipeline.evaluate(
        ExtensionLifecyclePoint.PREPARE_BEFORE, _context(
            ExtensionLifecyclePoint.PREPARE_BEFORE)))

    assert calls == ["high", "low", "dependent"]


def test_deny_wins_conflict_and_patches_are_discarded() -> None:
    patch = StatePatch(
        scope=PatchScope.STATE_METADATA,
        operation=PatchOperation.SET,
        path="value",
        value="patched",
    )
    pipeline = LoopMiddlewarePipeline([
        AbilityProbe(
            "patch",
            priority=200,
            action=MiddlewareAction.PATCH,
            patches=(patch,),
        ),
        AbilityProbe(
            "deny",
            priority=100,
            action=MiddlewareAction.DENY,
        ),
    ])
    result = asyncio.run(pipeline.evaluate(
        ExtensionLifecyclePoint.PREPARE_BEFORE,
        _context(ExtensionLifecyclePoint.PREPARE_BEFORE),
    ))

    assert result.action is MiddlewareAction.DENY
    assert result.patches == ()


def test_failure_policies_and_patch_scope_enforcement() -> None:
    pipeline = LoopMiddlewarePipeline([
        AbilityProbe("open", raises=True, fail_policy=MiddlewareFailurePolicy.FAIL_OPEN),
        AbilityProbe(
            "warn", priority=1, raises=True,
            fail_policy=MiddlewareFailurePolicy.WARN),
    ])
    result = asyncio.run(pipeline.evaluate(
        ExtensionLifecyclePoint.PREPARE_BEFORE,
        _context(ExtensionLifecyclePoint.PREPARE_BEFORE),
    ))
    assert result.action is MiddlewareAction.ALLOW
    assert any(item.error for item in result.evaluations)

    invalid = AbilityProbe(
        "invalid_patch",
        action=MiddlewareAction.PATCH,
        patches=(StatePatch(
            scope=PatchScope.RUNTIME_CONTEXT,
            operation=PatchOperation.APPEND,
            path="",
            value="context",
        ),),
    )
    invalid._manifest = AbilityManifest(
        id="invalid_patch",
        lifecycle_points=(ExtensionLifecyclePoint.PREPARE_BEFORE,),
        write_scopes=(),
    )
    denied = asyncio.run(LoopMiddlewarePipeline([invalid]).evaluate(
        ExtensionLifecyclePoint.PREPARE_BEFORE,
        _context(ExtensionLifecyclePoint.PREPARE_BEFORE),
    ))
    assert denied.action is MiddlewareAction.DENY
    assert "未声明 Patch 权限" in denied.reason


def test_loop_commits_context_and_metadata_patches(tmp_path: Path) -> None:
    ability = AbilityProbe(
        "prepare-context",
        action=MiddlewareAction.PATCH,
        patches=(
            StatePatch(
                scope=PatchScope.RUNTIME_CONTEXT,
                operation=PatchOperation.APPEND,
                path="",
                value="middleware context",
            ),
            StatePatch(
                scope=PatchScope.STATE_METADATA,
                operation=PatchOperation.SET,
                path="checked",
                value=True,
            ),
        ),
    )
    pipeline = LoopMiddlewarePipeline([ability])
    loop = AgentLoop(
        llm=MockProvider(scripted=["done"]),
        registry=ToolRegistry([]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        middleware_pipeline=pipeline,
    )

    assert asyncio.run(loop.run("task")) == "done"
    assert "middleware context" in loop.runtime_context
    assert loop.extension_metadata["checked"] is True
    assert loop.middleware_evaluations


def test_loop_short_circuit_and_fail_closed_are_deterministic(
    tmp_path: Path,
) -> None:
    short = AbilityProbe(
        "short",
        points=(ExtensionLifecyclePoint.TASK_START,),
        action=MiddlewareAction.SHORT_CIRCUIT,
    )
    loop = AgentLoop(
        llm=MockProvider(scripted=[]),
        registry=ToolRegistry([]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        middleware_pipeline=LoopMiddlewarePipeline([short]),
    )
    assert asyncio.run(loop.run("task")) == "middleware result"
    assert loop.loop_state.phase is LoopPhase.COMPLETED

    denied = AbilityProbe(
        "denied",
        points=(ExtensionLifecyclePoint.TASK_START,),
        action=MiddlewareAction.DENY,
    )
    blocked_loop = AgentLoop(
        llm=MockProvider(scripted=[]),
        registry=ToolRegistry([]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        middleware_pipeline=LoopMiddlewarePipeline([denied]),
    )
    with pytest.raises(AgentLoopTerminated) as caught:
        asyncio.run(blocked_loop.run("task"))
    assert caught.value.termination.reason.value == "policy_denied"


def test_state_commit_runs_after_tool_results_are_applied(
    tmp_path: Path,
) -> None:
    (tmp_path / "input.txt").write_text("committed", encoding="utf-8")
    ability = AbilityProbe(
        "state-commit",
        points=(ExtensionLifecyclePoint.STATE_COMMIT,),
        action=MiddlewareAction.PATCH,
        patches=(StatePatch(
            scope=PatchScope.STATE_METADATA,
            operation=PatchOperation.SET,
            path="tool_state_committed",
            value=True,
        ),),
    )
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[
                ToolCall("read-1", "read_file", {"path": "input.txt"}),
            ]),
            "done",
        ]),
        registry=ToolRegistry([ReadFileTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        middleware_pipeline=LoopMiddlewarePipeline([ability]),
    )

    assert asyncio.run(loop.run("read then finish")) == "done"
    assert ability.calls == ["state-commit"]
    assert loop.extension_metadata["tool_state_committed"] is True
    assert any(
        message.role == "tool" and message.tool_call_id == "read-1"
        for message in loop.history
    )


def test_exhaustive_state_transition_matrix() -> None:
    allowed = {
        LoopPhase.PREPARE: {
            LoopPhase.MODEL, LoopPhase.VERIFY, LoopPhase.WAITING,
            LoopPhase.FAILED,
        },
        LoopPhase.MODEL: {
            LoopPhase.ACT, LoopPhase.VERIFY, LoopPhase.WAITING,
            LoopPhase.FAILED,
        },
        LoopPhase.ACT: {
            LoopPhase.OBSERVE, LoopPhase.VERIFY, LoopPhase.WAITING,
            LoopPhase.FAILED,
        },
        LoopPhase.OBSERVE: {
            LoopPhase.PREPARE, LoopPhase.ACT, LoopPhase.VERIFY,
            LoopPhase.FAILED,
        },
        LoopPhase.VERIFY: {
            LoopPhase.COMPLETED, LoopPhase.PREPARE, LoopPhase.WAITING,
            LoopPhase.FAILED,
        },
        LoopPhase.WAITING: {
            LoopPhase.PREPARE, LoopPhase.MODEL, LoopPhase.ACT,
            LoopPhase.VERIFY, LoopPhase.FAILED,
        },
        LoopPhase.COMPLETED: set(),
        LoopPhase.FAILED: set(),
    }

    async def noop(_event) -> None:
        return None

    for source in LoopPhase:
        for target in LoopPhase:
            state = AgentLoopStateMachine("turn-matrix", noop)
            state.phase = source
            state.resume_phase = source
            if target is source or target in allowed[source]:
                asyncio.run(state.transition(target, "matrix"))
                assert state.phase is target
            else:
                with pytest.raises(LoopTransitionError):
                    asyncio.run(state.transition(target, "matrix"))


def test_tool_side_effect_contract_is_explicit_and_replay_safe_by_default() -> None:
    read_descriptor = ToolDescriptor.from_tool(ReadFileTool())
    write_descriptor = ToolDescriptor.from_tool(WriteFileTool())
    assert read_descriptor.side_effect.kind is SideEffectKind.NONE
    assert read_descriptor.side_effect.replay_safe is True
    assert write_descriptor.side_effect.kind is SideEffectKind.FILE_MUTATION
    assert write_descriptor.side_effect.replay_safe is False
    assert write_descriptor.side_effect.preflight_check
    assert write_descriptor.side_effect.compensation

    class DeclaredWriteTool(WriteFileTool):
        side_effect_contract = ToolSideEffectContract(
            kind=SideEffectKind.FILE_MUTATION,
            replay_safe=True,
            idempotent=True,
            preflight_check="compare content hash",
        )

    declared = ToolDescriptor.from_tool(DeclaredWriteTool())
    assert declared.side_effect.replay_safe is True
    assert declared.side_effect.idempotent is True
