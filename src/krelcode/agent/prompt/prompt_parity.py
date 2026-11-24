"""Frozen Prompt Parity contract and content-light benchmark gates.

This compatibility facade preserves the historical import path while the
frozen contract, accepted baselines and gate evaluator live in focused sibling
modules.  Baseline and gate reports remain metadata-only.
"""

from __future__ import annotations

from krelcode.agent.prompt.prompt_parity_baseline import (
    BaselineCaseResult,
    PROMPT_BASELINE_SCHEMA_VERSION,
    PromptParityBaseline,
    load_prompt_baseline,
    save_prompt_baseline,
)
from krelcode.agent.prompt.prompt_parity_contract import (
    FROZEN_PROMPT_PARITY_CONTRACT_HASH,
    FROZEN_PROMPT_PARITY_SUITE_HASH,
    PROMPT_PARITY_CONTRACT,
    PROMPT_PARITY_CONTRACT_VERSION,
    PROMPT_PARITY_MINIMUM_TRIALS,
    PROMPT_PARITY_SCHEMA_VERSION,
    ParityDimension,
    ParityRequirement,
    PromptParityContract,
    PromptParityStatus,
    contract_summary,
)
from krelcode.agent.prompt.prompt_parity_gate import (
    ParityGateCheck,
    PromptParityGateResult,
    evaluate_prompt_parity,
)


__all__ = [
    "BaselineCaseResult",
    "FROZEN_PROMPT_PARITY_CONTRACT_HASH",
    "FROZEN_PROMPT_PARITY_SUITE_HASH",
    "ParityDimension",
    "ParityGateCheck",
    "ParityRequirement",
    "PROMPT_BASELINE_SCHEMA_VERSION",
    "PROMPT_PARITY_CONTRACT",
    "PROMPT_PARITY_CONTRACT_VERSION",
    "PROMPT_PARITY_MINIMUM_TRIALS",
    "PROMPT_PARITY_SCHEMA_VERSION",
    "PromptParityBaseline",
    "PromptParityContract",
    "PromptParityGateResult",
    "PromptParityStatus",
    "contract_summary",
    "evaluate_prompt_parity",
    "load_prompt_baseline",
    "save_prompt_baseline",
]
