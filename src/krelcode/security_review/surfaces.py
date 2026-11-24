"""Prompt-surface descriptors owned by the Security Review adapter."""

from __future__ import annotations

from ..agent.evidence import EvidenceSource, InstructionSource, SourceRole
from ..agent.prompt_surface import PromptSurfaceDescriptor, PromptSurfaceKind
from ..agent.prompt_surface_catalog import prompt_content_hash
from ..agent.tokens import estimate_text_tokens
from .prompt import SECURITY_REVIEW_SYSTEM_PROMPT
from .report import SECURITY_REVIEW_SURFACE_CONTRACT


def security_review_prompt_surface() -> PromptSurfaceDescriptor:
    return PromptSurfaceDescriptor(
        id="aux:security-review",
        kind=PromptSurfaceKind.REVIEW,
        source="security_review.runner",
        owner="SecurityReviewRunner",
        version="security-review-v1",
        role=SourceRole.INSTRUCTION,
        authority=InstructionSource.MANAGED.value,
        trust="managed",
        stability="static",
        scope="managed",
        content_hash=prompt_content_hash(SECURITY_REVIEW_SYSTEM_PROMPT),
        token_estimate=estimate_text_tokens(SECURITY_REVIEW_SYSTEM_PROMPT),
        description="Defensive Security Review with a structured report contract.",
        capabilities=(
            "workflow",
            "read_only_tools",
            "json_schema",
            "security_gate",
        ),
        metadata={
            "run_mode": "review",
            "permission_mode": "read-only",
            "workflow": "security-review",
            "structured_report": True,
        },
        contract=SECURITY_REVIEW_SURFACE_CONTRACT,
    )


__all__ = ["security_review_prompt_surface"]
