"""Deterministic provider and isolated workspace fixture for Prompt benchmarks."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Mapping, Sequence

from ..agent.compression import ContextManager
from ..agent.context import ContextOptions, ContextSection, WorkspaceContext
from ..agent.context_assembler import ContextAssembler
from ..agent.context_source import default_context_sources
from ..agent.cyber_risk import cyber_risk_fragment
from ..agent.fragments import PromptFragmentRegistry, base_fragment, sync_rule_fragments
from ..agent.loop import AgentLoop
from ..agent.rules import RuleLoader
from ..agent.tokens import estimate_text_tokens
from ..agent.working_context import WorkingContextStore
from ..core.events import EventBus
from ..core.messages import Message
from ..core.redact import SecretRedactor
from ..llm.base import (
    EvidenceChannel,
    PreparedLLMRequest,
    ProviderCapabilities,
    StreamFinished,
    TextDelta,
    resolve_evidence_channel,
    resolve_system_prompt_blocks,
)
from ..safety.policy import AllowAllPolicy
from ..session.compact_checkpoint import CompactCheckpointStore
from ..session.memory import MemoryKind, MemoryScope, MemoryStore
from ..contracts.tool import ToolContext
from ..tools.registry import ToolRegistry
from .prompt_helpers import _message_payload


class _BenchmarkProvider:
    """Prepared provider with deterministic responses and exact wire digest."""

    name = "benchmark-prepared"
    model = "benchmark-model"
    capabilities = ProviderCapabilities(
        supports_tools=True,
        supports_streaming=True,
        supports_evidence_channel=True,
    )

    def __init__(self, responses: Sequence[str] = ()) -> None:
        self._responses = deque(responses)
        self.default_response = "benchmark response"
        self.last_prepared: PreparedLLMRequest | None = None
        self.executed: list[PreparedLLMRequest] = []

    def prepare_request(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, object]] | None = None,
        *,
        evidence: EvidenceChannel | None = None,
    ) -> PreparedLLMRequest:
        blocks = resolve_system_prompt_blocks(messages)
        evidence_channel = resolve_evidence_channel(messages, evidence)
        payload = {
            "provider": self.name,
            "model": self.model,
            "messages": [_message_payload(message) for message in messages],
            "tools": list(tools or ()),
            "evidence": {
                "anchor_message_index": evidence_channel.anchor_message_index,
                "blocks": [block.text for block in evidence_channel.blocks],
            },
        }
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str,
            separators=(",", ":"),
        ).encode("utf-8")
        prepared = PreparedLLMRequest(
            provider=self.name,
            model=self.model,
            url="https://benchmark.invalid/v1/chat",
            headers={},
            body=body,
            request_body_tokens=estimate_text_tokens(body.decode("utf-8")),
            payload_digest=hashlib.sha256(body).hexdigest(),
            cache_mode="explicit" if blocks is not None else "none",
            stable_cache_chars=blocks.stable_chars if blocks is not None else 0,
            stable_cache_hash=blocks.stable_hash if blocks is not None else "",
            dynamic_cache_chars=blocks.dynamic_chars if blocks is not None else 0,
            dynamic_cache_hash=blocks.dynamic_hash if blocks is not None else "",
            stable_system_block_count=(
                len(blocks.stable_system_blocks)
                if blocks is not None else 0),
            dynamic_system_block_count=(
                len(blocks.dynamic_system_blocks)
                if blocks is not None else 0),
            cache_control_boundary=(
                blocks.cache_control_boundary if blocks is not None else 0),
            instruction_system_block_count=(
                len(blocks.instruction_blocks) if blocks is not None else 0),
            evidence_block_count=len(evidence_channel.blocks),
            evidence_chars=evidence_channel.chars,
            evidence_anchor_message_index=evidence_channel.anchor_message_index,
            instruction_hash=(
                blocks.instruction_hash if blocks is not None else ""),
            evidence_hash=evidence_channel.hash,
        )
        self.last_prepared = prepared
        return prepared

    async def stream_prepared(self, prepared: PreparedLLMRequest):
        self.executed.append(prepared)
        text = self._responses.popleft() if self._responses else self.default_response
        yield TextDelta(text)
        yield StreamFinished(message=Message.assistant(text), finish_reason="stop", usage={})

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, object]] | None = None,
        *,
        evidence: EvidenceChannel | None = None,
    ):
        prepared = self.prepare_request(messages, tools, evidence=evidence)
        async for event in self.stream_prepared(prepared):
            yield event


class _BenchmarkFixture:
    """One isolated workspace shared by a benchmark scenario's components."""

    def __init__(
        self,
        root: Path,
        *,
        provider: _BenchmarkProvider | None = None,
        context_manager: ContextManager | None = None,
        compact_store: CompactCheckpointStore | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.home = self.root / "home"
        self.target = self.root / "src" / "a.py"
        self.provider = provider or _BenchmarkProvider()
        self.context_manager = context_manager
        self.compact_store = compact_store
        self._create_workspace()
        self.memory = MemoryStore(
            self.root,
            path=self.root / ".krelcode" / "memory" / "entries.jsonl",
            user_path=self.home / ".krelcode" / "memory" / "entries.jsonl",
            session_id="benchmark-source",
        )
        self.memory_entry = self.memory.add(
            "The benchmark project validates prompt evidence with pytest.",
            scope=MemoryScope.PROJECT,
            kind=MemoryKind.KNOWLEDGE,
            source="benchmark",
            pinned=True,
        )
        self.options = ContextOptions(
            engine="v3",
            include_git=True,
            include_tree=True,
            include_directory_rules=True,
        )
        self.rule_loader = RuleLoader(
            self.root,
            home=self.home,
            managed_paths=(self.root / ".krelcode" / "managed" / "AGENTS.md",),
        )
        self.workspace = self._workspace_context()
        self.registry = PromptFragmentRegistry([
            cyber_risk_fragment(), base_fragment()])
        sync_rule_fragments(self.registry, self.workspace)
        self.loop = self.make_loop(
            context_manager=context_manager,
            compact_store=compact_store,
        )

    def _create_workspace(self) -> None:
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "README.md").write_text(
            "unrelated repository documentation\n", encoding="utf-8")
        (self.root / "src" / "a.py").write_text(
            "def answer():\n    return 42\n", encoding="utf-8")
        (self.root / "src" / "b.py").write_text(
            "def unrelated():\n    return 'noise'\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text(
            "Project benchmark policy: use pytest.\n", encoding="utf-8")
        (self.root / "src" / "AGENTS.md").write_text(
            "Directory benchmark policy: inspect src/a.py directly.\n",
            encoding="utf-8")
        managed = self.root / ".krelcode" / "managed"
        managed.mkdir(parents=True, exist_ok=True)
        (managed / "AGENTS.md").write_text(
            "Managed benchmark policy: never modify protected paths.\n",
            encoding="utf-8")
        user_rules = self.home / ".krelcode"
        user_rules.mkdir(parents=True, exist_ok=True)
        (user_rules / "AGENTS.md").write_text(
            "User benchmark preference: keep evidence non-authoritative.\n",
            encoding="utf-8")

    def _workspace_context(self) -> WorkspaceContext:
        stack = self.rule_loader.load_stack(
            (self.target,), include_directory=True)
        sections = (
            ContextSection(
                "git", "Git: branch benchmark-old\nstatus: clean",
                priority=20, source="benchmark-git"),
            ContextSection(
                "tree",
                "Workspace tree: README.md, src/a.py, src/b.py",
                priority=50,
                source="benchmark-tree",
            ),
        )
        text = "<workspace_context>\n" + "\n\n".join(
            section.content for section in sections) + "\n</workspace_context>"
        return WorkspaceContext(
            root=self.root,
            sections=sections,
            text=text,
            max_tokens=6_000,
            total_tokens=estimate_text_tokens(text),
            rule_bundle=stack.combined_bundle,
            rule_layers=stack.layers,
        )

    def make_loop(
        self,
        *,
        context_manager: ContextManager | None = None,
        compact_store: CompactCheckpointStore | None = None,
    ) -> AgentLoop:
        working = WorkingContextStore(self.root)
        sources = default_context_sources(
            self.rule_loader,
            working_context_store=working,
            engine="v3",
            memory_store=self.memory,
        )
        registry = PromptFragmentRegistry(self.registry.list())
        snapshot = registry.snapshot()
        loop = AgentLoop(
            llm=self.provider,
            registry=ToolRegistry(),
            tool_context=ToolContext(
                workspace_root=self.root,
                sandbox_enabled=False,
            ),
            policy=AllowAllPolicy(),
            bus=EventBus(),
            system_prompt=snapshot.text,
            context_manager=context_manager,
            context_assembler=ContextAssembler(sources, engine="v3"),
            compact_checkpoint_store=compact_store,
            working_context_store=working,
        )
        loop.prompt_registry = registry
        loop.prompt_snapshot = snapshot
        loop.workspace_context = self.workspace
        loop.context_options = self.options
        loop.context_engine = 'v3'
        loop.context_enabled = True
        loop.rule_loader = self.rule_loader
        loop.memory_store = self.memory
        loop.skill_manager = None
        loop.sync_context_attachments(include_mcp=False)
        return loop
