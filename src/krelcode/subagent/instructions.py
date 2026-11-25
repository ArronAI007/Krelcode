"""Instruction layering and filtered memory for delegated workers.

Subagents inherit policy-bearing instruction layers (base, managed, user,
project and task-targeted directory rules), but never the parent's full chat
history. Rules are represented by the same canonical fragment factory used by
the main agent, including ``managed:rules``, ``user:rules``, ``project:rules``,
``directory:rules`` and ``agent_definition:<name>``. Relevant memory is selected
from the delegated task query and injected as an L7 Evidence fragment, never as
a runtime instruction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from ..core.evidence import (
    InstructionSource,
    SourceRole,
    evidence_metadata,
    evidence_record_from_memory,
    instruction_source_for,
    render_evidence_set,
)
from ..agent.prompt.cyber_risk import cyber_risk_fragment
from ..agent.prompt.fragments import (
    GROUP_RUNTIME,
    GROUP_SYSTEM,
    LAYER_L1_BASE_AGENT,
    LAYER_L7_EVIDENCE,
    STABILITY_STATIC,
    STABILITY_TURN,
    TRUST_MANAGED,
    TRUST_UNTRUSTED,
    PromptFragment,
    agent_definition_fragment,
    rule_fragment,
)
from ..agent.rules import RuleLayer, RuleLoader, RuleScope, extract_rule_targets
from ..core.tokens import estimate_text_tokens
from ..core.redact import SecretRedactor
from .definition import AgentDefinition


@dataclass(frozen=True)
class InstructionLayer:
    kind: str
    source: str
    content: str


@dataclass(frozen=True)
class InstructionBundle:
    layers: tuple[InstructionLayer, ...]
    rule_sources: tuple[str, ...] = ()
    rule_layers: tuple[RuleLayer, ...] = ()
    base_metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n\n".join(
            layer.content for layer in self.layers if layer.content.strip())

    def metadata(self) -> dict[str, object]:
        layer_records: list[dict[str, object]] = []
        for layer in self.layers:
            trust = {
                "managed": "managed",
                "base": "managed",
                "user": "user",
                "project": "project",
                "directory": "project",
                "definition": "project",
            }.get(layer.kind, "managed")
            source = instruction_source_for(
                trust, source=layer.source, origin=layer.kind)
            layer_records.append({
                "kind": layer.kind,
                "source": layer.source,
                "source_role": SourceRole.INSTRUCTION.value,
                "instruction_source": (
                    source or InstructionSource.MANAGED).value,
            })
        return {
            "layers": [layer.kind for layer in self.layers],
            "sources": [layer.source for layer in self.layers],
            "rule_sources": list(self.rule_sources),
            "rule_layers": [{
                "scope": layer.scope.value,
                "sources": [
                    source.display_path for source in layer.bundle.sources],
                "imports": layer.bundle.import_count,
                "issues": len(layer.bundle.issues),
                "truncated": layer.bundle.truncated,
            } for layer in self.rule_layers],
            "source_roles": layer_records,
        }

    def fragments(self) -> tuple[PromptFragment, ...]:
        """Convert policy, base, canonical rules and definition to fragments."""
        fragments: list[PromptFragment] = [cyber_risk_fragment()]
        for layer in self.layers:
            if layer.kind == "base" and layer.content.strip():
                fragments.append(PromptFragment(
                    id="subagent:base:0",
                    source="base",
                    title="Subagent base instruction",
                    content=layer.content.strip(),
                    group=GROUP_SYSTEM,
                    priority=0,
                    cacheable=True,
                    compactable=False,
                    toggleable=False,
                    metadata={
                        "kind": "base",
                        "source_role": SourceRole.INSTRUCTION.value,
                        "instruction_source": InstructionSource.MANAGED.value,
                        "instruction": True,
                        **dict(self.base_metadata),
                    },
                    scope="managed",
                    layer=LAYER_L1_BASE_AGENT,
                    stability=STABILITY_STATIC,
                    trust=TRUST_MANAGED,
                ))
        fragments.extend(
            fragment for layer in self.rule_layers
            for fragment in (rule_fragment(layer),)
            if fragment is not None
        )
        for layer in self.layers:
            if layer.kind == "definition" and layer.content.strip():
                fragment = agent_definition_fragment(
                    layer.source.removeprefix("agent_definition:"),
                    layer.content,
                )
                if fragment is not None:
                    fragments.append(fragment)
        return tuple(fragments)


def build_subagent_instructions(
    workspace_root: Path,
    task: str,
    *,
    base_prompt: str,
    definition: Optional[AgentDefinition] = None,
    home: Optional[Path] = None,
    redactor: Optional[SecretRedactor] = None,
    managed_paths: Sequence[Path] = (),
    base_metadata: Optional[Mapping[str, object]] = None,
) -> InstructionBundle:
    root = Path(workspace_root).resolve()
    home_path = (Path(home) if home is not None else Path.home()).expanduser().resolve()
    loader = RuleLoader(
        root,
        home=home_path,
        redactor=redactor,
        managed_paths=managed_paths,
    )
    if definition is None or definition.inherit_project_rules:
        rule_layers = loader.load_stack(
            _task_paths(root, task) if task else (),
            include_directory=bool(task),
        ).layers
    else:
        # Claude Code's Explore and Plan agents intentionally omit CLAUDE.md
        # (and Krelcode's equivalent user/project/directory rules) for fast,
        # focused research. Managed policy remains authoritative.
        managed = loader.load_managed()
        rule_layers = (
            (RuleLayer(RuleScope.MANAGED, managed),)
            if managed.content
            else ()
        )

    layers: list[InstructionLayer] = []
    if base_prompt.strip():
        layers.append(InstructionLayer("base", "base", base_prompt.strip()))
    for rule_layer in rule_layers:
        if rule_layer.bundle.content:
            layers.append(InstructionLayer(
                rule_layer.scope.value,
                f"{rule_layer.scope.value}:rules",
                rule_layer.bundle.content,
            ))
    if definition is not None:
        layers.append(InstructionLayer(
            "definition",
            f"agent_definition:{definition.name}",
            definition.system_prompt_with_base(""),
        ))

    sources = tuple(dict.fromkeys(
        source.display_path
        for layer in rule_layers
        for source in layer.bundle.sources
        if not source.imported
    ))
    return InstructionBundle(
        tuple(layers),
        sources,
        rule_layers=tuple(rule_layers),
        base_metadata=dict(base_metadata or {}),
    )


def render_relevant_memory(entries: Sequence[object]) -> str:
    if not entries:
        return ""
    records = tuple(evidence_record_from_memory(entry) for entry in entries)
    return render_evidence_set(records, origin="subagent_retrieved_memory")


def inherited_memory_fragment(
    entries: Sequence[object],
) -> Optional[PromptFragment]:
    """Build one L7, non-instruction evidence fragment for a subagent."""
    records = tuple(evidence_record_from_memory(entry) for entry in entries)
    if not records:
        return None
    content = render_evidence_set(
        records, origin="subagent_retrieved_memory")
    hits = tuple({
        "id": record.id,
        "source": "memory",
        "scope": record.scope,
        "kind": record.kind,
        "origin": record.origin,
        "confidence": record.confidence,
        "tokens": estimate_text_tokens(record.content),
    } for record in records)
    metadata = evidence_metadata(
        records,
        origin="subagent_retrieved_memory",
        extra={
            "memory_entry_ids": tuple(record.id for record in records),
            "memory_tokens": estimate_text_tokens(content),
            "memory_hits": hits,
        },
    )
    return PromptFragment(
        id="subagent:inherited-memory",
        source="inherited_memory",
        title="Inherited memory evidence",
        content=content,
        group=GROUP_RUNTIME,
        priority=0,
        cacheable=False,
        compactable=True,
        toggleable=False,
        metadata=metadata,
        scope="session",
        layer=LAYER_L7_EVIDENCE,
        stability=STABILITY_TURN,
        trust=TRUST_UNTRUSTED,
    )


def _task_paths(workspace_root: Path, task: str) -> tuple[Path, ...]:
    # RuleLoader performs the actual bounded path extraction. Passing the task
    # through its history channel keeps this helper independent of intent shape.
    from ..core.messages import Message
    return extract_rule_targets(
        workspace_root, intent=None, history=(Message.user(task),))


__all__ = [
    "InstructionBundle",
    "InstructionLayer",
    "build_subagent_instructions",
    "inherited_memory_fragment",
    "render_relevant_memory",
]
