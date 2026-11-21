"""prompt_lab: deterministic, read-only Prompt engineering operations."""

from __future__ import annotations

import json
from typing import Any, Dict

from ...agent.prompt_candidate import PROMPT_CANDIDATE_SCHEMA
from ...agent.prompt_lab import PromptLabError, PromptLabService
from ..base import ToolContext, ToolResult, ToolTier
from ..prompt import ToolPromptExample


_SPEC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "objective": {"type": "string", "maxLength": 8000},
        "target_fragment_id": {"type": "string", "maxLength": 160},
        "target_layer": {"type": "string", "maxLength": 120},
        "behavior": {"type": "string", "maxLength": 8000},
        "non_goals": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "evidence_policy": {"type": "string", "maxLength": 4000},
        "output_contract": {"type": "string", "maxLength": 4000},
        "instruction_source": {"type": "string", "maxLength": 80},
        "tool_policy": {"type": "string", "maxLength": 4000},
        "items": {"type": "array", "items": {"type": "object"}},
        "tools": {
            "type": "array",
            "items": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "object"},
                ],
            },
        },
        "permission_mode": {
            "type": "string",
            "enum": ["default", "read-only", "auto-approve", "plan"],
        },
        "tool_scope": {
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
        },
        "sandbox_policy": {"type": "string", "maxLength": 4000},
        "approval_policy": {"type": "string", "maxLength": 4000},
        "validation_cases": {"type": "array", "items": {"type": "object"}},
        "success_metrics": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
_SPEC_SCHEMA["properties"].update(PROMPT_CANDIDATE_SCHEMA["properties"])


class PromptLabTool:
    """Inspect, generate, compare, and evaluate Prompts without side effects."""

    name = "prompt_lab"
    read_only = True
    tool_tier = ToolTier.CORE
    aliases = ("prompt engineering", "prompt lab")
    search_terms = (
        "prompt", "prompt engineering", "fragment", "generate", "compare",
        "evaluate", "conflict", "authoring",
    )
    description = (
        "Read-only Prompt engineering lab. Inspect Prompt Fragments and hashes, "
        "generate a canonical, reviewable Prompt candidate, compare two fragments "
        "or inline instructions, and evaluate a candidate through a temporary "
        "PromptPlan. Evaluation runs six isolation gates: current failure, stable "
        "regression, instruction precedence, evidence injection, permission/"
        "Sandbox, and cache stability. Passing only permits human review and a "
        "separate Prompt Release decision. It never writes Prompt files, mutates "
        "the Registry, or applies a candidate."
    )
    prompt = (
        "Use inspect to see active Fragment provenance, placement, token counts, "
        "hashes, and conflicts without returning bodies. Use generate for a local "
        "candidate using the closed Candidate Prompt Schema, then evaluate it. "
        "Use compare for two Fragment IDs, sources, or inline instruction texts. "
        "Canonical evaluation first assembles an in-memory temporary PromptPlan "
        "and must pass all six isolation gates before human review. Release still "
        "requires a separate manual decision. Evaluation and comparison are "
        "content-light. Generated candidate text is returned for review only and "
        "is never applied automatically."
    )
    when_to_use = (
        "The user asks to design, inspect, compare, review, or validate a Prompt.",
        "PromptAuthoringContract is enabled and a candidate needs deterministic checks.",
    )
    when_not_to_use = (
        "The task only needs normal repository inspection or code edits.",
        "A candidate is expected to be written to disk without human review.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Inspect the current Prompt registry",
            input={"action": "inspect"},
            description="Return metadata, hashes and conflicts, not Prompt bodies.",
        ),
        ToolPromptExample(
            title="Evaluate a canonical candidate",
            input={
                "action": "evaluate",
                "spec": {
                    "target_fragment": "core:base:tool-discipline",
                    "objective": "Reduce unrelated file reads.",
                    "proposed_prompt": "Read only the files needed by the request.",
                    "constraints": ["Do not expand tool authority."],
                    "expected_behavior": ["Read fewer unrelated files."],
                    "risk": "low",
                    "evaluation_cases": [
                        {
                            "id": "success",
                            "kind": "success",
                            "metric": "assert unrelated file reads == 0",
                            "machine_verifiable": True,
                        },
                        {
                            "id": "regression",
                            "kind": "regression",
                            "metric": "assert focused tests exit_code == 0",
                            "machine_verifiable": True,
                        },
                        {
                            "id": "security",
                            "kind": "security",
                            "metric": "assert output contains no credential-shaped text",
                            "machine_verifiable": True,
                        },
                    ],
                    "rollback_plan": "Discard the unapplied candidate review.",
                },
            },
            description=(
                "Run the six temporary-PromptPlan isolation gates; passing "
                "does not apply or release the candidate."),
        ),
        ToolPromptExample(
            title="Compare two instructions",
            input={
                "action": "compare",
                "left": "Use tabs for indentation.",
                "right": "Use spaces for indentation.",
            },
            description="Reuse the production conflict analyzer.",
        ),
    )
    prompt_version = "2"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["inspect", "generate", "compare", "evaluate"],
                "description": "Prompt engineering operation to run.",
            },
            "target": {
                "type": "string",
                "description": "inspect: optional Fragment ID/source/title substring.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "inspect: maximum metadata records; default 50.",
            },
            "left": {
                "type": "string",
                "description": "compare: exact Fragment ID/source or inline text.",
            },
            "right": {
                "type": "string",
                "description": "compare: exact Fragment ID/source or inline text.",
            },
            "spec": {
                "type": "object",
                "description": (
                    "generate/evaluate: canonical Candidate Prompt Schema or "
                    "legacy PromptAuthoringRequest-compatible input."),
                "properties": _SPEC_SCHEMA["properties"],
                "additionalProperties": False,
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = str(args.get("action") or "").strip().lower()
        service = PromptLabService(
            lambda: getattr(ctx, "prompt_registry", None),
            isolation_context=ctx,
        )
        try:
            if action == "inspect":
                try:
                    raw_limit = args.get("limit", 50)
                    limit = max(1, min(int(50 if raw_limit is None else raw_limit), 100))
                except (TypeError, ValueError) as exc:
                    raise PromptLabError(f"invalid inspect limit: {exc}") from exc
                result = service.inspect(str(args.get("target") or ""), limit=limit)
                content = self._render_inspect(result)
            elif action == "generate":
                if "spec" not in args:
                    raise PromptLabError("generate 需要 spec")
                result = service.generate(args.get("spec"))
                content = self._render_generate(result)
            elif action == "compare":
                if "left" not in args or "right" not in args:
                    raise PromptLabError("compare 需要 left 与 right")
                result = service.compare(args.get("left"), args.get("right"))
                content = self._render_compare(result)
            elif action == "evaluate":
                if "spec" not in args:
                    raise PromptLabError("evaluate 需要 spec")
                result = service.evaluate(args.get("spec"))
                content = self._render_evaluate(result)
            else:
                raise PromptLabError(
                    "action 必须是 inspect/generate/compare/evaluate")
        except (PromptLabError, ValueError, TypeError) as exc:
            return ToolResult(
                content=f"prompt_lab {action or '?'} failed: {exc}",
                is_error=True,
                data={"action": action, "error": type(exc).__name__},
            )
        return ToolResult(content=content, data=result)

    @staticmethod
    def _render_inspect(result: Dict[str, Any]) -> str:
        lines = [
            f"PromptLab inspect: {result['enabled']}/{result['total']} enabled"
            f" · matched {result['matched']}"
            f" · registry generation {result['registry_generation']}"
            f" · authoring={'on' if result['prompt_authoring_enabled'] else 'off'}",
        ]
        records = tuple(result.get("records", ()))
        for record in records[:20]:
            state = "on " if record["enabled"] else "off"
            cache = "cache" if record["cacheable"] else "dynamic"
            conflicts = len(record.get("conflict_ids", ()))
            lines.append(
                f"[{state}] {record['id']} · {record['source']}"
                f" · {record['layer']} · {record['tokens']} tokens"
                f" · {cache} · conflicts={conflicts}"
                f" · hash={str(record['content_hash'])[:12]}")
        if len(records) > 20:
            lines.append(f"… 另有 {len(records) - 20} 条 metadata 未展示")
        report = result["conflict_report"]
        lines.append(
            f"conflicts: {report.get('conflict_count', 0)}"
            f" · unresolved: {report.get('unresolved_count', 0)}"
            f" · body_returned=false")
        return "\n".join(lines)

    @staticmethod
    def _render_generate(result: Dict[str, Any]) -> str:
        candidate = result["candidate"]
        report = result["evaluation"]
        lines = [
            "PromptLab generate: candidate created (NOT applied)",
            f"candidate hash: {result['candidate_hash']}",
            f"evaluation: {'passed' if report['passed'] else 'needs work'}"
            f" · errors={report['error_count']}"
            f" · warnings={report['warning_count']}",
            *PromptLabTool._isolation_lines(report, body_returned=True),
            "",
            "```json",
            json.dumps(candidate, ensure_ascii=False, indent=2),
            "```",
        ]
        issue_lines = PromptLabTool._issue_lines(report)
        if issue_lines:
            lines.extend(("", *issue_lines))
        lines.append("No Prompt source, Registry entry, or Release was modified.")
        return "\n".join(lines)

    @staticmethod
    def _render_compare(result: Dict[str, Any]) -> str:
        left = result["left"]
        right = result["right"]
        lines = [
            "PromptLab compare",
            f"left: {left['id']} · {left['layer']} · {left['tokens']} tokens"
            f" · hash={str(left['content_hash'])[:12]}"
            f" · origin={left['origin']}",
            f"right: {right['id']} · {right['layer']} · {right['tokens']} tokens"
            f" · hash={str(right['content_hash'])[:12]}"
            f" · origin={right['origin']}",
            f"identical={str(result['identical_content']).lower()}"
            f" · conflicts={result['conflict_count']}"
            f" · unresolved={result['unresolved_count']}",
        ]
        for conflict in result["conflict_report"].get("conflicts", ()):
            lines.append(
                f"[conflict] {conflict['code']} · {conflict['status']}"
                f" · {conflict['left_fragment_id']} →"
                f" {conflict['right_fragment_id']}"
                f" · winner={conflict['winner_fragment_id'] or '(none)'}")
        lines.append("body_returned=false")
        return "\n".join(lines)

    @staticmethod
    def _render_evaluate(result: Dict[str, Any]) -> str:
        report = result["report"]
        legacy = bool(result.get("legacy_authoring_only"))
        release_passed = bool(result.get("passed"))
        if legacy and report["passed"]:
            headline = "PASS (authoring only; not Release eligible)"
        else:
            headline = "PASS" if release_passed else "FAIL"
        lines = [
            f"PromptLab evaluate: {headline}"
            f" · authoring={'PASS' if report['passed'] else 'FAIL'}"
            f" · errors={report['error_count']}"
            f" · warnings={report['warning_count']}"
            f" · conflicts={report['conflict_count']}"
            f" · unresolved={report['unresolved_conflict_count']}",
            f"request_hash: {report['request_hash']}",
            *PromptLabTool._isolation_lines(result),
        ]
        lines.extend(PromptLabTool._issue_lines(report))
        lines.append("body_returned=false")
        lines.append(
            "production_modified=false · applied=false · release_requires_manual_review=true")
        return "\n".join(lines)

    @staticmethod
    def _isolation_lines(
        result: Dict[str, Any], *, body_returned: bool = False,
    ) -> tuple[str, ...]:
        isolation = result.get("isolation")
        if not isinstance(isolation, dict):
            return (
                "isolation: NOT RUN · legacy authoring-only input · Release blocked",
            )
        checks = tuple(isolation.get("checks", ()))
        failed = tuple(isolation.get("failed_checks", ()))
        review_allowed = bool(isolation.get("review_allowed"))
        lines = [
            f"isolation: {'PASS' if isolation.get('passed') else 'FAIL'}"
            f" · mode={isolation.get('mode')}"
            f" · {sum(1 for check in checks if check.get('passed'))}/{len(checks)} gates",
            (
                "review: eligible for human review · Release requires separate manual approval"
                if review_allowed else
                "review: BLOCKED · Release blocked"
            ),
        ]
        for check in checks:
            state = "PASS" if check.get("passed") else "FAIL"
            issues = ", ".join(str(item) for item in check.get("issue_codes", ()))
            suffix = f" · {issues}" if issues else ""
            lines.append(f"[{state}] {check.get('id', 'unknown')}{suffix}")
        if failed:
            lines.append("failed checks: " + ", ".join(str(item) for item in failed))
        plan = isolation.get("candidate_plan")
        if isinstance(plan, dict):
            lines.append(
                f"temporary plan: fragments={plan.get('fragment_count', 0)}"
                f" · stable={plan.get('stable_hash', '')[:12] or '(empty)'}"
                f" · full={plan.get('full_hash', '')[:12] or '(empty)'}"
                f" · cache boundary={plan.get('cache_boundary_chars', 0)} chars")
        body_state = (
            "candidate body returned for review only"
            if body_returned else "candidate body not returned"
        )
        lines.append(
            f"production_modified=false · applied=false · {body_state}")
        return tuple(lines)

    @staticmethod
    def _issue_lines(report: Dict[str, Any]) -> tuple[str, ...]:
        lines = []
        for issue in report.get("issues", ())[:50]:
            target = issue.get("item_id") or issue.get("field") or "-"
            lines.append(
                f"[{issue['severity']}] {issue['code']} · {target}"
                f" · {issue['message']}")
        if len(report.get("issues", ())) > 50:
            lines.append("… validation issues truncated")
        return tuple(lines)


__all__ = ["PromptLabTool"]
