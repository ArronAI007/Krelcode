"""Compatibility facade for the live-model behavior benchmark domains.

The executable implementation lives in focused modules.  Existing imports from
``krelcode.benchmarks.behavior`` remain supported, including the private helper
seams used by tests and external adapters.
"""

from __future__ import annotations

import importlib
from typing import Any

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
    _file_contains,
    _file_returns,
    _is_required_test_command,
    _sandbox_check,
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
from .behavior_runner import BehaviorBenchmarkRunner
from .behavior_session import BehaviorSession
from .live_cli import _provider_execution_profile, add_cli_arguments, run_live_command
from .report_store import (
    DEFAULT_BEHAVIOR_REPORT_PATH,
    aggregate_behavior_report_files,
    save_behavior_report,
)


_PRIVATE_MODULES = {
    "_benchmark_digest": ".live_cli",
    "_configured_prompt_calibrations": ".live_cli",
    "_elapsed_ms": ".behavior_helpers",
    "_error_kind": ".behavior_runner",
    "_parity_payload": ".live_cli",
    "_safe_error": ".behavior_runner",
    "_text_report": ".live_cli",
}
# ``_scope_command_text`` predates this refactor and remains a compatibility
# alias; ``_scope_command_line`` is the clearer internal name in helpers.
_scope_command_text = _scope_command_line


def __getattr__(name: str) -> Any:
    module_name = _PRIVATE_MODULES.get(name)
    if module_name is not None:
        module = importlib.import_module(module_name, __package__)
        return getattr(module, name)
    raise AttributeError(name)


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
