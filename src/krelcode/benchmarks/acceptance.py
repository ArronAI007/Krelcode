"""Final, fail-closed acceptance CLI and backward-compatible public surface.

The frozen contract/report model, probe helpers and runner live in focused
modules.  This facade keeps the historical import and monkeypatch paths stable.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .acceptance_checks import (
    _check,
    _parse_utc_timestamp,
    _report_metric,
    _report_mode,
    _safe_error,
    _timestamp_is_fresh,
)
from .acceptance_models import (
    FINAL_ACCEPTANCE_CONTRACT,
    FINAL_ACCEPTANCE_CONTRACT_VERSION,
    FINAL_ACCEPTANCE_LIVE_MAX_AGE_SECONDS,
    FINAL_ACCEPTANCE_SCHEMA_VERSION,
    FROZEN_FINAL_ACCEPTANCE_CONTRACT_HASH,
    AcceptanceArea,
    AcceptanceCheckResult,
    AcceptanceCheckStatus,
    AcceptanceEvidenceKind,
    AcceptanceRequirement,
    FinalAcceptanceContract,
    FinalAcceptanceReport,
    FinalAcceptanceStatus,
    _content_light_violations,
    _hash_payload,
)
from .acceptance_runner import FinalAcceptanceRunner
from .behavior_models import BehaviorReport
from ..agent.prompt.prompt_parity import PromptParityGateResult


def run_offline_acceptance(
    *,
    json_output: bool = False,
    include_sdk: bool = True,
    sdk_timeout: float = 30.0,
    workspace: Path | None = None,
    live_gate: PromptParityGateResult | None = None,
    live_report: BehaviorReport | None = None,
    live_max_age_seconds: float = FINAL_ACCEPTANCE_LIVE_MAX_AGE_SECONDS,
) -> int:
    report = FinalAcceptanceRunner(workspace=workspace).run(
        include_sdk=include_sdk,
        sdk_timeout=sdk_timeout,
        live_gate=live_gate,
        live_report=live_report,
        live_max_age_seconds=live_max_age_seconds,
    )
    if json_output:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render_text())
    return report.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m krelcode.benchmarks.acceptance",
        description=(
            "Run deterministic final acceptance. A live Prompt Parity Gate is "
            "still required for accepted status."
        ),
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-sdk", action="store_true")
    parser.add_argument("--sdk-timeout", type=float, default=30.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    return run_offline_acceptance(
        json_output=args.json,
        include_sdk=not args.no_sdk,
        sdk_timeout=args.sdk_timeout,
    )


__all__ = [
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
    "run_offline_acceptance",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
