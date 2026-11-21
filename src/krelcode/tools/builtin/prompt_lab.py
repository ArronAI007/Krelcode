"""prompt_lab: deterministic, read-only Prompt engineering operations."""

from __future__ import annotations

import json
from typing import Any, Dict

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
        "generate a reviewable Prompt candidate, compare two fragments or inline "
        "instructions, and evaluate a candidate against PromptAuthoringContract. "
        "It never writes Prompt files or applies candidate changes."
    )
    prompt = (
        "Use inspect to see active Fragment provenance, placement, token counts, "
        "hashes, and conflicts without returning bodies. Use generate for a local "
        "candidate scaffold, then evaluate it. Use compare for two Fragment IDs, "
        "sources, or inline instruction texts. Evaluation and comparison are "
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
            title="Compare two instructions",
            input={
                "action": "compare",
                "left": "Use tabs for indentation.",
                "right": "Use spaces for indentation.",
            },
            description="Reuse the production conflict analyzer.",
        ),
    )
    prompt_version = "1"
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
                "description": "generate/evaluate: Prompt candidate specification.",
                "properties": _SPEC_SCHEMA["properties"],
                "additionalProperties": False,
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = str(args.get("action") or "").strip().lower()
        service = PromptLabService(lambda: getattr(ctx, "prompt_registry", None))
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
            "",
            "```json",
            json.dumps(candidate, ensure_ascii=False, indent=2),
            "```",
        ]
        issue_lines = PromptLabTool._issue_lines(report)
        if issue_lines:
            lines.extend(("", *issue_lines))
        lines.append("No Prompt source or registry entry was modified.")
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
        lines = [
            f"PromptLab evaluate: {'PASS' if report['passed'] else 'FAIL'}"
            f" · errors={report['error_count']}"
            f" · warnings={report['warning_count']}"
            f" · conflicts={report['conflict_count']}"
            f" · unresolved={report['unresolved_conflict_count']}",
            f"request_hash: {report['request_hash']}",
        ]
        lines.extend(PromptLabTool._issue_lines(report))
        lines.append("body_returned=false")
        return "\n".join(lines)

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
