"""Cross-shard aggregation without importing benchmark runner code."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Sequence

from .behavior_cases import BEHAVIOR_CASES
from .behavior_models import (
    BehaviorReport,
    BehaviorTrialResult,
    _report_int,
)


def aggregate_behavior_reports(
    reports: Sequence[BehaviorReport],
    *,
    require_complete: bool = True,
) -> BehaviorReport:
    """Merge disjoint deterministic shards into one behavior report."""
    if not reports:
        raise ValueError("至少需要一个 benchmark shard report")
    first = reports[0]
    comparable_keys = {
        "krelcode_version", "python_version", "platform",
        "parity_contract_version", "parity_contract_hash",
        "suite_name", "trials", "max_steps", "context_max_tokens",
        "sandbox_backend", "trial_timeout_seconds", "prompt_release_id",
        "prompt_release_hash", "prompt_profile", "prompt_profile_hash",
        "profile_calibration_id", "profile_calibration_hash",
        "profile_calibration_status", "profile_calibration_reason",
        "provider_base_url_hash", "provider_generation_hash",
        "provider_timeout_seconds", "provider_max_tokens",
    }
    for report in reports:
        if report.schema_version != first.schema_version:
            raise ValueError("benchmark shard schema 不一致")
        if (report.provider, report.model) != (first.provider, first.model):
            raise ValueError("benchmark shard provider/model 不一致")
        for key in comparable_keys:
            if report.profile.get(key) != first.profile.get(key):
                raise ValueError(f"benchmark shard profile 不一致: {key}")

    expected_trials = _report_int(first.profile.get("trials", 0), "profile.trials")
    expected_suite_size = _report_int(
        first.profile.get("suite_size", 0), "profile.suite_size")
    by_key: dict[tuple[str, int], BehaviorTrialResult] = {}
    shard_labels: list[str] = []
    for report in reports:
        shard = str(report.profile.get("shard", "") or "")
        if shard:
            shard_labels.append(shard)
        for result in report.results:
            key = (result.case_id, result.trial)
            if key in by_key:
                raise ValueError(
                    f"benchmark shard 重叠: {result.case_id} trial {result.trial}")
            by_key[key] = result
    if not by_key:
        raise ValueError("benchmark shard 不包含结果")

    case_ids = tuple(dict.fromkeys(result.case_id for result in by_key.values()))
    known_order = {
        case.id: index for index, case in enumerate(BEHAVIOR_CASES)
    }
    case_ids = tuple(sorted(
        case_ids,
        key=lambda item: (known_order.get(item, len(known_order)), item),
    ))
    for case_id in case_ids:
        trials = sorted(
            result.trial for result in by_key.values()
            if result.case_id == case_id)
        if expected_trials and trials != list(range(1, expected_trials + 1)):
            raise ValueError(
                f"benchmark shard 缺少 {case_id} 的 trial 1..{expected_trials}")
    if require_complete and expected_suite_size and len(case_ids) != expected_suite_size:
        raise ValueError(
            f"benchmark shard 覆盖不完整: {len(case_ids)}/{expected_suite_size}")

    ordered_results = tuple(
        by_key[(case_id, trial)]
        for case_id in case_ids
        for trial in range(1, (expected_trials or max(
            result.trial for result in by_key.values()
            if result.case_id == case_id)) + 1)
    )
    profile = dict(first.profile)
    profile["cases"] = list(case_ids)
    profile["selected_cases"] = len(case_ids)
    if expected_suite_size:
        profile["suite_size"] = expected_suite_size
    profile["trials"] = expected_trials or max(
        result.trial for result in ordered_results)
    profile["shard"] = "aggregate"
    profile["shards"] = sorted(dict.fromkeys(shard_labels))
    case_by_id = {case.id: case for case in BEHAVIOR_CASES}
    if all(case_id in case_by_id for case_id in case_ids):
        suite_payload = json.dumps(
            [case_by_id[case_id].as_dict() for case_id in case_ids],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        profile["suite_hash"] = hashlib.sha256(suite_payload).hexdigest()

    generated_at = datetime.now(timezone.utc).isoformat()
    duration_ms = sum(max(0, report.duration_ms) for report in reports)
    provisional = BehaviorReport(
        schema_version=first.schema_version,
        generated_at=generated_at,
        provider=first.provider,
        model=first.model,
        passed=False,
        results=ordered_results,
        duration_ms=duration_ms,
        profile=profile,
    )
    passed = bool(ordered_results) and all(
        bool(item.get("stable")) for item in provisional.case_summary().values())
    return replace(provisional, passed=passed)
