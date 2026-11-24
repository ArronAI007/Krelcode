"""Shared runtime service bindings for the composition root.

This module owns the final, idempotent wiring of cross-cutting services.  The
goal is not to hide dependencies behind a service locator, but to make the
security-sensitive edges explicit in one place:

* one :class:`SecretRedactor` instance reaches every persistence and output
  boundary;
* enterprise and approval policies are bound together;
* stores created before the agent loop are attached to it exactly once.

Feature modules still receive concrete ports during construction whenever
possible.  ``RuntimeServiceBinder`` exists for objects that can only be built
after the loop (for example the context checkpoint coordinator) or for older
compatibility attributes consumed by commands and embedders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, cast

from ._deps import *  # noqa: F401,F403
from ...subagent.sidechain import SidechainStore


@dataclass(frozen=True)
class PersistenceServices:
    """Persistent or history-bearing services sharing one redaction boundary."""

    memory_store: MemoryStore
    working_context_store: WorkingContextStore
    compact_checkpoint_store: Optional[CompactCheckpointStore]
    context_checkpoint_store: Optional[ContextCheckpointStore]
    context_checkpoint_coordinator: Optional[ContextCheckpointCoordinator]
    sidechains: Optional[SidechainStore]
    shell_jobs: object
    auto_memory: Optional[AutoMemoryCoordinator] = None


@dataclass(frozen=True)
class RuntimeServices:
    """Cross-cutting services owned by one bootstrapped runtime."""

    redactor: SecretRedactor
    enterprise_policy: EnterprisePolicy
    approval_policy: ApprovalPolicy
    principal: Optional[PrincipalPort]
    context_manager: Optional[ContextManager]
    persistence: PersistenceServices
    rule_loader: Optional[RuleLoader] = None


class RuntimeServiceBinder:
    """Bind shared services to runtime objects in a repeatable order.

    ``bind`` is intentionally idempotent.  Existing objects keep their identity
    and receive the same redactor/policy instances on every call, so callers may
    safely rebind after optional late-construction components are attached.
    """

    def __init__(self, services: RuntimeServices) -> None:
        self.services = services

    @property
    def redactor(self) -> SecretRedactor:
        return self.services.redactor

    def add_secret(self, secret: Optional[str]) -> None:
        self.redactor.add(secret)

    def add_secrets(self, secrets: Iterable[str]) -> None:
        for secret in secrets:
            self.add_secret(secret)

    def bind(
        self,
        *,
        loop: AgentLoop,
        subagents: Optional[object] = None,
    ) -> None:
        """Apply all shared bindings without changing runtime behaviour."""
        self._bind_persistence(loop)
        self._bind_redaction(loop, subagents)
        self._bind_policy(loop, subagents)
        loop.update_runtime_state(
            runtime_service_binder=self,
            runtime_services=self.services,
        )
        if loop.services.prompt.registry is not None:
            sync_memory_fragment(
                loop, self.services.persistence.memory_store)

    def _bind_persistence(self, loop: AgentLoop) -> None:
        persistence = self.services.persistence
        loop.update_runtime_state(
            memory_store=persistence.memory_store,
            context_checkpoint_store=persistence.context_checkpoint_store,
            context_checkpoint_coordinator=(
                persistence.context_checkpoint_coordinator),
            working_context_store=persistence.working_context_store,
            compact_checkpoint_store=persistence.compact_checkpoint_store,
        )
        loop.tool_context.shell_jobs = cast(
            Optional[ShellJobManagerPort], persistence.shell_jobs)

    def _bind_redaction(
        self,
        loop: AgentLoop,
        subagents: Optional[object],
    ) -> None:
        redactor = self.services.redactor
        loop.update_runtime_state(redactor=redactor)
        loop.services.context.working_context.redactor = redactor.redact
        if self.services.context_manager is not None:
            self.services.context_manager.redactor = redactor
        if self.services.rule_loader is not None:
            self.services.rule_loader.redactor = redactor

        persistence = self.services.persistence
        persistence.memory_store.redactor = redactor
        if persistence.compact_checkpoint_store is not None:
            persistence.compact_checkpoint_store.redactor = redactor
        if persistence.context_checkpoint_store is not None:
            persistence.context_checkpoint_store.redactor = redactor
        if persistence.context_checkpoint_coordinator is not None:
            persistence.context_checkpoint_coordinator.redactor = redactor
        if persistence.sidechains is not None:
            persistence.sidechains.redactor = redactor
        if persistence.auto_memory is not None:
            persistence.auto_memory.set_redactor(redactor)
        _set_redactor(persistence.shell_jobs, redactor)

        if subagents is not None:
            _set_redactor(subagents, redactor)
            sidechains = getattr(subagents, "sidechains", None)
            if sidechains is not None:
                sidechains.redactor = redactor

    def _bind_policy(
        self,
        loop: AgentLoop,
        subagents: Optional[object],
    ) -> None:
        enterprise_policy = self.services.enterprise_policy
        principal = self.services.principal
        loop.update_runtime_state(
            policy=self.services.approval_policy,
            enterprise_policy=enterprise_policy,
            principal=principal,
        )
        loop.tool_context.enterprise_policy = enterprise_policy
        loop.tool_context.principal = principal
        if subagents is None:
            return
        if hasattr(subagents, "policy"):
            setattr(subagents, "policy", self.services.approval_policy)
        setattr(subagents, "enterprise_policy", enterprise_policy)
        setattr(subagents, "principal", principal)


def _set_redactor(target: object, redactor: SecretRedactor) -> None:
    """Set a redactor on objects whose local ports intentionally stay small."""
    setattr(target, "redactor", redactor)


__all__ = [
    "PersistenceServices",
    "RuntimeServiceBinder",
    "RuntimeServices",
]
