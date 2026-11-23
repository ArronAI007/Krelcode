"""Human-readable summaries for scheduled real-model regression runs.

The nightly workflow keeps the full content-light JSON report as its primary
evidence.  This module only renders aggregate status for CI logs and GitHub
step summaries; it never copies answers, tool arguments or Prompt bodies.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Mapping, Sequence


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _integer(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _load_payload(path: Path) -> Mapping[str, object]:
    try:
        raw = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取 nightly result: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("nightly result 不是合法 JSON") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("nightly result 顶层必须是 object")
    return raw


def render_nightly_summary(path: Path | str) -> str:
    """Render a bounded, content-light Markdown summary."""
    payload = _load_payload(Path(path))
    benchmark = _mapping(payload.get("benchmark"))
    if not benchmark and payload.get("kind") == "live_model_behavior":
        benchmark = payload
    gate = _mapping(payload.get("gate"))
    summary = _mapping(benchmark.get("summary"))
    cases = _mapping(benchmark.get("cases"))

    passed = bool(benchmark.get("passed", False))
    gate_present = bool(gate)
    gate_passed = bool(gate.get("passed", False)) if gate_present else None
    overall = passed and (gate_passed is not False)
    lines = [
        "## DeepSeek live behavior regression",
        "",
        f"- Overall: **{'PASS' if overall else 'FAIL'}**",
        f"- Provider/model: `{benchmark.get('provider', '?')}` / "
        f"`{benchmark.get('model', '?')}`",
        f"- Cases: {_integer(summary.get('stable_cases'))}/"
        f"{_integer(summary.get('cases'))} stable",
        f"- Trials: {_integer(summary.get('passed_trials'))}/"
        f"{_integer(summary.get('trials'))} passed",
        f"- Pass@k: {_integer(summary.get('pass_at_k'))}/"
        f"{_integer(summary.get('cases'))}",
        f"- Tool calls: {_integer(summary.get('tool_calls'))}",
        f"- Tokens: {_integer(summary.get('total_tokens'))}",
        f"- Estimated cost: ${_float(summary.get('cost_usd')):.6f}",
        "- Evidence: content-light report only",
    ]

    failed_cases = [
        str(case_id) for case_id, value in cases.items()
        if isinstance(value, Mapping) and not bool(value.get("stable", False))
    ]
    if failed_cases:
        lines.append("- Failed cases: `" + "`, `".join(sorted(failed_cases)) + "`")

    if gate_present:
        lines.extend([
            "",
            f"### Parity gate: {'PASS' if gate_passed else 'FAIL'}",
            f"- Baseline hash: `{str(gate.get('baseline_hash', '?'))[:12]}`",
            f"- Suite hash: `{str(gate.get('report_suite_hash', '?'))[:12]}`",
        ])
        failed_checks = [
            str(item.get("id", "?"))
            for item in gate.get("checks", ())
            if isinstance(item, Mapping) and not bool(item.get("passed", False))
        ]
        if failed_checks:
            lines.append("- Failed checks: `" + "`, `".join(failed_checks) + "`")

    errors = _mapping(summary.get("errors"))
    if errors:
        lines.append("- Runtime errors: `" + json.dumps(
            {str(key): _integer(value) for key, value in sorted(errors.items())},
            sort_keys=True,
        ) + "`")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m krelcode.benchmarks.nightly",
        description="Render a bounded summary from a nightly live benchmark result.",
    )
    parser.add_argument("--summary", required=True, metavar="RESULT_JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        print(render_nightly_summary(args.summary), end="")
    except ValueError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "render_nightly_summary"]
