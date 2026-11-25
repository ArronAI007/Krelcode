"""Isolated production-agent session used by live behavior scenarios."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..agent.context.attachments import ContextAttachmentKind, attachment_id
from ..agent.context.compression import ContextManager
from ..agent.context import ContextOptions, build_workspace_context_bundle
from ..agent.context.context_assembler import ContextAssembler
from ..agent.context.context_source import default_context_sources
from ..agent.prompt.cyber_risk import cyber_risk_fragment
from ..agent.prompt.fragments import (
    PromptFragmentRegistry,
    apply_prompt_snapshot,
    base_fragments,
    sync_rule_fragments,
)
from ..agent.turn.loop import AgentLoop
from ..agent.prompts import resolve_prompt_profile
from ..agent.prompt.prompt_surface_catalog import refresh_prompt_surfaces
from ..agent.rules import RuleLoader
from ..agent.usage import UsageTracker
from ..agent.context.working_context import WorkingContextStore
from ..bootstrap.composition.tools import default_tools
from ..core.events import EventBus
from ..core.redact import SecretRedactor
from ..llm.base import LLMProvider
from ..safety.policy import SandboxPolicy
from ..safety.sandbox import ExecutionSandbox
from ..session.lifecycle import SessionLifecycle
from ..session.memory import MemoryKind, MemoryScope, MemoryStore
from ..contracts.tool import PermissionMode, RunMode, RuntimeSnapshot, ToolContext
from ..tools.registry import ToolRegistry
from .behavior_models import (
    BehaviorEventObserver,
    MemorySeed,
    WorkspaceDiff,
    _IGNORED_WORKSPACE_PARTS,
)


class BehaviorSession:
    """One isolated live-agent scene with production components wired in."""

    def __init__(
        self,
        workspace_root: Path,
        home: Path,
        provider: LLMProvider,
        *,
        max_steps: int = 14,
        context_max_tokens: int = 30_000,
        keep_recent: int = 10,
        sandbox_backend: str = "auto",
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.home = Path(home).resolve()
        self.provider = provider
        self.max_steps = max(1, int(max_steps))
        self.context_max_tokens = max(4_000, int(context_max_tokens))
        self.keep_recent = max(2, int(keep_recent))
        self.sandbox_backend = sandbox_backend
        self.memory_store: MemoryStore | None = None
        self.loop: AgentLoop | None = None
        self.observer = BehaviorEventObserver()
        self.usage = UsageTracker(model=getattr(provider, "model", None))
        self.redactor = SecretRedactor()
        key = getattr(provider, "api_key", None)
        if key:
            self.redactor.add(key)
        self._initial_hashes: dict[str, str] = {}
        self._turn_counter = 0

    async def build(self, memory_entries: Sequence[MemorySeed] = ()) -> None:
        self.memory_store = MemoryStore(
            self.workspace_root,
            redactor=self.redactor,
            path=(
                self.workspace_root / ".krelcode" / "memory" / "entries.jsonl"),
            user_path=self.home / ".krelcode" / "memory" / "entries.jsonl",
            session_id="live-behavior",
        )
        for item in memory_entries:
            self.memory_store.add(
                item.text,
                scope=item.scope,
                kind=item.kind,
                source=item.source,
                pinned=item.pinned,
                deduplicate=True,
            )

        context_options = ContextOptions(
            engine="v3",
            include_rules=True,
            include_git=True,
            include_tree=True,
            include_directory_rules=True,
        )
        rule_loader = RuleLoader(
            self.workspace_root,
            home=self.home,
            limits=context_options.rule_limits(),
            imports_enabled=context_options.rules_imports,
            managed_paths=(
                self.home / ".krelcode" / "managed" / "AGENTS.md",
            ),
            redactor=self.redactor,
        )
        workspace_context = await build_workspace_context_bundle(
            self.workspace_root,
            options=context_options,
            rule_loader=rule_loader,
        )
        profile = resolve_prompt_profile(
            self.provider, getattr(self.provider, "model", ""))
        prompt_registry = PromptFragmentRegistry(base_fragments(profile))
        prompt_registry.register(cyber_risk_fragment())
        sync_rule_fragments(prompt_registry, workspace_context)
        prompt_snapshot = prompt_registry.snapshot()

        working_context = WorkingContextStore(
            self.workspace_root, redactor=self.redactor.redact)
        context_sources = default_context_sources(
            rule_loader,
            working_context_store=working_context,
            engine="v3",
            memory_store=self.memory_store,
        )
        context_manager = ContextManager(
            self.provider,
            max_tokens=self.context_max_tokens,
            keep_recent=self.keep_recent,
            working_context_provider=lambda: working_context.snapshot(),
            redactor=self.redactor,
        )
        bus = EventBus()
        self.observer.attach(bus)
        self.usage.attach(bus)

        tool_registry = ToolRegistry(default_tools(self.memory_store))
        sandbox = ExecutionSandbox(
            self.workspace_root,
            backend=self.sandbox_backend,
            network=False,
        )
        tool_context = ToolContext(
            workspace_root=self.workspace_root,
            sandbox=sandbox,
            sandbox_enabled=True,
            runtime=RuntimeSnapshot(
                run_mode=RunMode.PRINT,
                permission_mode=PermissionMode.AUTO_APPROVE,
            ),
        )
        policy = SandboxPolicy(
            auto_approve=True,
            rules_path=self.workspace_root / ".krelcode" / "allow_rules.json",
            bus=bus,
            workspace_root=self.workspace_root,
        )
        loop = AgentLoop(
            llm=self.provider,
            registry=tool_registry,
            tool_context=tool_context,
            policy=policy,
            bus=bus,
            system_prompt=prompt_snapshot.text,
            max_steps=self.max_steps,
            context_manager=context_manager,
            context_assembler=ContextAssembler(context_sources, engine="v3"),
            working_context_store=working_context,
            redactor=self.redactor,
        )
        loop.prompt_registry = prompt_registry
        loop.prompt_snapshot = prompt_snapshot
        loop.prompt_profile = profile.profile
        loop.prompt_profile_resolution = profile
        loop.prompt_profile_name = profile.name
        loop.context_options = context_options
        loop.context_engine = 'v3'
        loop.context_enabled = True
        loop.workspace_context = workspace_context
        loop.rule_loader = rule_loader
        loop.memory_store = self.memory_store
        loop.skill_manager = None
        loop.sync_context_attachments(include_mcp=False)
        apply_prompt_snapshot(loop, prompt_snapshot)
        refresh_prompt_surfaces(loop)
        self.loop = loop
        self._initial_hashes = self._snapshot_workspace()

    async def run_turn(self, prompt: str) -> str:
        if self.loop is None:
            raise RuntimeError("BehaviorSession 尚未 build")
        self._turn_counter += 1
        answer = await self.loop.run(
            prompt, turn_id=f"behavior-turn-{self._turn_counter}")
        return self.redactor.redact(answer)

    def workspace_diff(
        self,
        baseline: Mapping[str, str] | None = None,
    ) -> WorkspaceDiff:
        before = dict(self._initial_hashes if baseline is None else baseline)
        after = self._snapshot_workspace()
        created = tuple(sorted(set(after) - set(before)))
        deleted = tuple(sorted(set(before) - set(after)))
        modified = tuple(sorted(
            path for path in set(before).intersection(after)
            if before[path] != after[path]
        ))
        return WorkspaceDiff(created, modified, deleted)

    def snapshot(self) -> dict[str, str]:
        return self._snapshot_workspace()

    def _snapshot_workspace(self) -> dict[str, str]:
        files: dict[str, str] = {}
        for path in sorted(self.workspace_root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.workspace_root)
            if any(part in _IGNORED_WORKSPACE_PARTS for part in rel.parts):
                continue
            try:
                files[rel.as_posix()] = hashlib.sha256(
                    path.read_bytes()).hexdigest()
            except OSError:
                continue
        return files

    def context_facts(self) -> dict[str, object]:
        loop = self.loop
        if loop is None or loop.last_context_plan is None:
            return {}
        plan = loop.last_context_plan.prompt_plan
        active_prompt_fragments = tuple(
            fragment.id for fragment in plan.fragments
            if fragment.id.startswith((
                "core:base",
                "tool_playbook:",
                "workflow:",
                "managed:rules",
                "user:rules",
                "project:rules",
                "directory:rules",
            ))
        )
        facts: dict[str, object] = {
            "prompt_fragments": len(plan.fragments),
            "active_prompt_fragments": active_prompt_fragments,
            "stable_tokens": plan.stable_tokens,
            "volatile_tokens": plan.volatile_tokens,
            "cache_boundary_chars": plan.cache_boundary_chars,
            "stable_hash": plan.stable_hash[:16],
            "full_hash": plan.full_hash[:16],
        }
        registry = loop.services.prompt.surface_registry
        health = getattr(registry, "health", lambda: {})()
        if isinstance(health, Mapping):
            facts["prompt_surfaces"] = int(health.get("surfaces", 0) or 0)
            facts["observed_prompt_surfaces"] = int(
                health.get("observed", 0) or 0)
            facts["stale_prompt_surfaces"] = int(
                health.get("stale", 0) or 0)
            facts["prompt_surface_contract_failures"] = int(
                health.get("contract_failures", 0) or 0)
        snapshot = getattr(registry, "snapshot", None)
        if callable(snapshot):
            facts["prompt_surface_hash"] = snapshot().content_hash[:16]
        return facts
