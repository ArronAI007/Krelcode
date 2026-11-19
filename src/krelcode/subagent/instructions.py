"""Instruction layering and filtered memory for delegated workers.

Subagents inherit policy-bearing instruction layers (base, managed, user,
project and task-targeted directory rules), but never the parent's full chat
history. They are converted into the same PromptFragment contract used by the
main agent. Relevant memory is selected from the delegated task query and
injected as an L7 Evidence fragment, never as a runtime instruction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from ..agent.fragments import (
    GROUP_RUNTIME,
    GROUP_SYSTEM,
    LAYER_L0_MANAGED_POLICY,
    LAYER_L1_BASE_AGENT,
    LAYER_L2_USER_GLOBAL,
    LAYER_L3_PROJECT_RULES,
    LAYER_L4_AGENT_STYLE,
    LAYER_L7_EVIDENCE,
    STABILITY_SESSION,
    STABILITY_STATIC,
    STABILITY_TURN,
    TRUST_MANAGED,
    TRUST_PROJECT,
    TRUST_UNTRUSTED,
    TRUST_USER,
    PromptFragment,
)
from ..agent.rules import RuleLoader, extract_rule_targets
from ..agent.evidence import (
    InstructionSource,
    SourceRole,
    evidence_metadata,
    evidence_record_from_memory,
    instruction_source_for,
    render_evidence_set,
)
from ..agent.tokens import estimate_text_tokens
from ..core.redact import SecretRedactor
from .definition import AgentDefinition

MAX_MANAGED_INSTRUCTION_BYTES = 256_000


@dataclass(frozen=True)
class InstructionLayer:
    kind: str
    source: str
    content: str


@dataclass(frozen=True)
class _FragmentSpec:
    layer: str
    trust: str
    scope: str
    stability: str
    compactable: bool = True


_FRAGMENT_SPECS = {
    "managed": _FragmentSpec(
        LAYER_L0_MANAGED_POLICY,
        TRUST_MANAGED,
        "managed",
        STABILITY_STATIC,
        compactable=False,
    ),
    "managed-project": _FragmentSpec(
        LAYER_L0_MANAGED_POLICY,
        TRUST_MANAGED,
        "managed",
        STABILITY_STATIC,
        compactable=False,
    ),
    "base": _FragmentSpec(
        LAYER_L1_BASE_AGENT,
        TRUST_MANAGED,
        "managed",
        STABILITY_STATIC,
        compactable=False,
    ),
    "user": _FragmentSpec(
        LAYER_L2_USER_GLOBAL,
        TRUST_USER,
        "user",
        STABILITY_SESSION,
        compactable=False,
    ),
    "project": _FragmentSpec(
        LAYER_L3_PROJECT_RULES,
        TRUST_PROJECT,
        "project",
        STABILITY_SESSION,
        compactable=False,
    ),
    "directory": _FragmentSpec(
        LAYER_L3_PROJECT_RULES,
        TRUST_PROJECT,
        "project",
        STABILITY_SESSION,
        compactable=False,
    ),
    "definition": _FragmentSpec(
        LAYER_L4_AGENT_STYLE,
        TRUST_PROJECT,
        "project",
        STABILITY_SESSION,
        compactable=False,
    ),
}

_FRAGMENT_PRIORITY = {
    "managed": 0,
    "managed-project": 0,
    "base": 0,
    "user": 10,
    "project": 20,
    "directory": 30,
    "definition": 40,
}


@dataclass(frozen=True)
class InstructionBundle:
    layers: tuple[InstructionLayer, ...]
    rule_sources: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "\n\n".join(
            layer.content for layer in self.layers if layer.content.strip())

    def metadata(self) -> dict:
        layer_records = []
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
            "source_roles": layer_records,
        }

    def fragments(self) -> tuple[PromptFragment, ...]:
        """Convert instruction layers into the same fragments used by main."""
        fragments: list[PromptFragment] = []
        counts: dict[str, int] = {}
        for layer in self.layers:
            content = layer.content.strip()
            if not content:
                continue
            spec = _FRAGMENT_SPECS.get(layer.kind)
            if spec is None:
                spec = _FRAGMENT_SPECS["managed"]
            ordinal = counts.get(layer.kind, 0)
            counts[layer.kind] = ordinal + 1
            source = instruction_source_for(
                spec.trust,
                source=layer.source,
                origin=layer.kind,
            ) or InstructionSource.MANAGED
            fragments.append(PromptFragment(
                id=f"subagent:{layer.kind}:{ordinal}",
                source=layer.source,
                title=f"Subagent {layer.kind} instruction",
                content=content,
                group=GROUP_SYSTEM,
                priority=_FRAGMENT_PRIORITY.get(layer.kind, 50) + ordinal,
                cacheable=True,
                compactable=spec.compactable,
                toggleable=False,
                metadata={
                    "kind": layer.kind,
                    "source_role": SourceRole.INSTRUCTION.value,
                    "instruction_source": source.value,
                    "instruction": True,
                },
                scope=spec.scope,
                layer=spec.layer,
                stability=spec.stability,
                trust=spec.trust,
            ))
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
) -> InstructionBundle:
    root = Path(workspace_root).resolve()
    home_path = (Path(home) if home is not None else Path.home()).expanduser().resolve()
    layers: list[InstructionLayer] = []
    if base_prompt.strip():
        layers.append(InstructionLayer("base", "base", base_prompt.strip()))

    managed = _managed_instructions(root, home_path, managed_paths, redactor)
    layers.extend(managed)

    loader = RuleLoader(root, home=home_path)
    user = loader.load_user()
    if user.content:
        layers.append(InstructionLayer("user", "user_rules", user.content))
    project = loader.load_project()
    if project.content:
        layers.append(InstructionLayer("project", "project_rules", project.content))

    if task:
        task_bundle = loader.load_directory(_task_paths(root, task))
        if task_bundle.content:
            layers.append(InstructionLayer(
                "directory", "directory_rules", task_bundle.content))

    if definition is not None:
        layers.append(InstructionLayer(
            "definition",
            f"agent_definition:{definition.name}",
            definition.system_prompt_with_base(""),
        ))

    sources = []
    for bundle in (user, project):
        sources.extend(
            source.display_path for source in bundle.sources if not source.imported)
    return InstructionBundle(tuple(layers), tuple(dict.fromkeys(sources)))


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


def _managed_instructions(
    workspace_root: Path,
    home: Path,
    extra_paths: Sequence[Path],
    redactor: Optional[SecretRedactor],
) -> list[InstructionLayer]:
    candidates: list[tuple[str, Path]] = []
    configured = os.environ.get("KRELCODE_MANAGED_INSTRUCTIONS", "")
    for raw in configured.split(os.pathsep):
        if raw.strip():
            candidates.append(("managed", Path(raw.strip()).expanduser()))
    candidates.extend((
        ("managed", Path("/etc/krelcode/AGENTS.md")),
        ("managed", home / ".krelcode" / "managed" / "AGENTS.md"),
        ("managed-project", workspace_root / ".krelcode" / "managed" / "AGENTS.md"),
    ))
    candidates.extend(("managed", path) for path in extra_paths)
    result: list[InstructionLayer] = []
    seen: set[Path] = set()
    for kind, candidate in candidates:
        try:
            path = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            if path.stat().st_size > MAX_MANAGED_INSTRUCTION_BYTES:
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if redactor is not None:
            content = redactor.redact(content)
        if content.strip():
            result.append(InstructionLayer(kind, str(path), content.strip()))
    return result


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
