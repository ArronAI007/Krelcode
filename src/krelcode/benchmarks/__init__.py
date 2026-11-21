"""Offline structure and live model behavior benchmarks for Krelcode."""

from __future__ import annotations

__all__ = [
    "BEHAVIOR_BENCHMARK_SCHEMA_VERSION",
    "BEHAVIOR_CASES",
    "BENCHMARK_CASES",
    "FROZEN_PROMPT_PARITY_CONTRACT_HASH",
    "FROZEN_PROMPT_PARITY_SUITE_HASH",
    "PROMPT_BASELINE_SCHEMA_VERSION",
    "PROMPT_BENCHMARK_SCHEMA_VERSION",
    "PROMPT_PARITY_CONTRACT",
    "PROMPT_PARITY_CONTRACT_VERSION",
    "PROMPT_PARITY_MINIMUM_TRIALS",
    "PROMPT_PARITY_SCHEMA_VERSION",
    "PROMPT_TUNING_SCHEMA_VERSION",
    "AcceptanceArea",
    "AcceptanceCheckResult",
    "AcceptanceCheckStatus",
    "AcceptanceEvidenceKind",
    "AcceptanceRequirement",
    "FINAL_ACCEPTANCE_CONTRACT",
    "FINAL_ACCEPTANCE_CONTRACT_VERSION",
    "FINAL_ACCEPTANCE_LIVE_MAX_AGE_SECONDS",
    "FINAL_ACCEPTANCE_SCHEMA_VERSION",
    "FROZEN_FINAL_ACCEPTANCE_CONTRACT_HASH",
    "FinalAcceptanceContract",
    "FinalAcceptanceReport",
    "FinalAcceptanceRunner",
    "FinalAcceptanceStatus",
    "BehaviorBenchmarkRunner",
    "BehaviorCase",
    "BehaviorCheck",
    "BehaviorErrorKind",
    "BehaviorMetric",
    "BehaviorReport",
    "BehaviorTrialResult",
    "FailureAttribution",
    "FailureCause",
    "FailureTrajectoryAnalyzer",
    "ParityDimension",
    "ParityGateCheck",
    "ParityRequirement",
    "PromptBenchmarkCase",
    "PromptBenchmarkCheck",
    "PromptBenchmarkMetric",
    "PromptBenchmarkMode",
    "PromptBenchmarkReport",
    "PromptBenchmarkResult",
    "PromptBenchmarkRunner",
    "PromptParityBaseline",
    "PromptParityContract",
    "PromptParityGateResult",
    "PromptParityStatus",
    "PromptTuningCandidate",
    "PromptTuningReport",
    "TuningRisk",
    "analyze_failure_trajectories",
    "contract_summary",
    "evaluate_prompt_parity",
    "load_prompt_baseline",
    "run_offline_acceptance",
    "main",
    "save_prompt_baseline",
]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(name)
    if (
        name.startswith((
            "Acceptance",
            "FinalAcceptance",
            "FINAL_ACCEPTANCE",
            "FROZEN_FINAL_ACCEPTANCE",
        ))
        or name == "run_offline_acceptance"
    ):
        from . import acceptance

        return getattr(acceptance, name)
    if name.startswith("Behavior") or name.startswith("BEHAVIOR_"):
        from . import behavior

        return getattr(behavior, name)
    if name.startswith(("Parity", "PromptParity", "PROMPT_PARITY", "PROMPT_BASELINE", "FROZEN_PROMPT")) or name in {
        "contract_summary", "evaluate_prompt_parity", "load_prompt_baseline",
        "save_prompt_baseline",
    }:
        from . import parity

        return getattr(parity, name)
    if (
        name.startswith(("Failure", "PromptTuning", "Tuning", "PROMPT_TUNING_"))
        or name == "analyze_failure_trajectories"
    ):
        from . import tuning

        return getattr(tuning, name)
    from . import prompt

    return getattr(prompt, name)
