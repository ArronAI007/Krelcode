"""Compatibility facade for the live-model behavior benchmark domains.

The executable implementation lives in focused modules.  Existing imports from
``krelcode.benchmarks.behavior`` remain supported, including the private helper
seams used by tests and external adapters.
"""

from __future__ import annotations

from ..agent.prompts import (
    CURRENT_PROMPT_RELEASE,
    prompt_calibration_paths_from_config,
    prompt_profile_from_config,
    prompt_release_id_from_config,
    resolve_prompt_profile,
)
from ..config import load_config_layers
from ..llm.registry import create_provider, resolve_settings
from .behavior_cases import BEHAVIOR_CASES
from .behavior_helpers import (
    _answer_has_int,
    _answer_has_zero_division_risk,
    _answer_reports_test_success,
    _check,
    _elapsed_ms,
    _file_contains,
    _file_returns,
    _is_required_test_command,
    _sandbox_check,
    _scope_command_line as _scope_command_text,
    _scope_command_line,
    _scope_violations,
    _tool_path,
    _workspace_check,
    _write,
)
from .behavior_models import (
    BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
    BehaviorCase,
    BehaviorCheck,
    BehaviorErrorKind,
    BehaviorMetric,
    BehaviorOutcome,
    BehaviorReport,
    BehaviorTrialResult,
    MemorySeed,
    ToolObservation,
    WorkspaceDiff,
)
from .behavior_reporting import aggregate_behavior_reports
from .behavior_runner import (
    BehaviorBenchmarkRunner,
    _error_kind,
    _safe_error,
)
from .behavior_session import BehaviorSession
from .live_cli import (
    _benchmark_digest,
    _configured_prompt_calibrations,
    _parity_payload,
    _provider_execution_profile,
    _text_report,
    add_cli_arguments,
    run_live_command,
)
from .report_store import (
    DEFAULT_BEHAVIOR_REPORT_PATH,
    aggregate_behavior_report_files,
    save_behavior_report,
)



__all__ = [
    "BEHAVIOR_BENCHMARK_SCHEMA_VERSION",
    "BEHAVIOR_CASES",
    "BehaviorBenchmarkRunner",
    "BehaviorCase",
    "BehaviorCheck",
    "BehaviorErrorKind",
    "BehaviorMetric",
    "BehaviorOutcome",
    "BehaviorReport",
    "BehaviorSession",
    "BehaviorTrialResult",
    "DEFAULT_BEHAVIOR_REPORT_PATH",
    "MemorySeed",
    "ToolObservation",
    "WorkspaceDiff",
    "add_cli_arguments",
    "aggregate_behavior_report_files",
    "aggregate_behavior_reports",
    "create_provider",
    "load_config_layers",
    "resolve_settings",
    "run_live_command",
    "save_behavior_report",
]
