"""update_plan：发布结构化执行计划快照。"""

from __future__ import annotations

from typing import Any, Dict, cast

from ...contracts.tool import ToolContext
from krelcode.contracts.tool import ToolResult
from krelcode.contracts.plan import (
    PlanControlMode,
    PlanNodeStatus,
)
from krelcode.contracts.plan_ops import (
    parse_plan_nodes,
    plan_snapshot_views,
    update_plan_control,
)
from ..prompt import ToolPromptExample

ALLOWED_STATUSES = tuple(status.value for status in PlanNodeStatus)


class UpdatePlanTool:
    name = "update_plan"
    read_only = True
    description = (
        "创建或更新本任务的结构化执行计划。多步骤任务在开始和阶段变化时调用；"
        "简单单步任务无需调用。每次提交完整快照；节点使用稳定 id，可声明依赖、"
        "并行组、owner、验收条件与证据。"
    )
    prompt = """\
Use update_plan for durable multi-step control, not hidden reasoning. Every node
uses a stable id and should declare dependencies, owner, acceptance checks and
evidence when completed. Exactly one node may be in_progress unless the active
nodes share the same parallel_group in planner_executor mode. Preserve completed
nodes across revisions and state the observation or fact that triggered a plan
change. planner_executor plans require planner, executor and verifier role
owners (for example "planner", "executor:backend", "verifier"). Use mode=todo
for short work, plan_mode for read-only exploration with human approval, and
planner_executor for dependent or parallel long-horizon work. The plan is a
control object, not proof that work is complete.
"""
    when_to_use = (
        "A task has several meaningful stages or benefits from visible progress tracking.",
        "The implementation approach changed enough that the current plan is stale.",
    )
    when_not_to_use = (
        "The task is a simple one-command or one-file operation.",
        "The tool would be used only to restate progress already obvious to the user.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Publish a multi-step implementation snapshot",
            input={
                "items": [
                    {
                        "id": "inspect",
                        "step": "Inspect the parser and tests",
                        "status": "completed",
                        "acceptance_checks": ["failure mode identified"],
                        "evidence_refs": ["grep:parser"],
                    },
                    {
                        "id": "implement",
                        "step": "Implement the bounded fix",
                        "status": "in_progress",
                        "depends_on": ["inspect"],
                    },
                    {
                        "id": "verify",
                        "step": "Run focused verification",
                        "status": "pending",
                        "depends_on": ["implement"],
                        "owner": "verifier",
                        "acceptance_checks": ["focused tests pass"],
                    },
                ],
                "explanation": "The evidence changed the implementation order.",
            },
            description=(
                "Submit the complete plan, with at most one in-progress step, "
                "when the approach or evidence changes."
            ),
        ),
    )
    prompt_version = "2"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                        },
                        "id": {"type": "string", "maxLength": 160},
                        "objective": {"type": "string", "maxLength": 500},
                        "status": {
                            "type": "string",
                            "enum": list(ALLOWED_STATUSES),
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "parallel_group": {"type": "string", "maxLength": 160},
                        "owner": {"type": "string", "maxLength": 160},
                        "acceptance_checks": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evidence_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["status"],
                    "additionalProperties": False,
                    "anyOf": [
                        {"required": ["step"]},
                        {"required": ["objective"]},
                    ],
                },
            },
            "explanation": {
                "type": "string",
                "maxLength": 2_000,
                "description": "本次计划变更的简短说明",
            },
            "mode": {
                "type": "string",
                "enum": [mode.value for mode in PlanControlMode],
            },
            "trigger_observation_id": {
                "type": "string",
                "maxLength": 160,
            },
            "trigger_fact": {
                "type": "string",
                "maxLength": 2_000,
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_items = args.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return ToolResult(content="计划 items 必须是非空数组", is_error=True)

        try:
            nodes = parse_plan_nodes(
                raw_items,
                previous=ctx.plan_control,
            )
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True)
        explanation = str(
            args.get("trigger_fact")
            or args.get("explanation", "")
        ).strip()
        raw_mode: object = (
            args.get("mode")
            or (
                ctx.plan_control.mode
                if ctx.plan_control is not None
                else ctx.plan_control_mode
            )
            or PlanControlMode.TODO.value
        )
        try:
            mode = (
                raw_mode
                if isinstance(raw_mode, PlanControlMode)
                else PlanControlMode(str(raw_mode))
            )
        except ValueError:
            return ToolResult(
                content=f"未知计划控制模式: {raw_mode!r}",
                is_error=True,
            )
        active = [
            node for node in nodes
            if node.status is PlanNodeStatus.IN_PROGRESS
        ]
        if len(active) > 1 and (
            len({node.parallel_group for node in active}) != 1
            or not active[0].parallel_group
            or mode is not PlanControlMode.PLANNER_EXECUTOR
        ):
            return ToolResult(
                content=(
                    "计划最多只能有一个 in_progress 步骤；"
                    "并行执行必须使用同一 parallel_group 和 "
                    "planner_executor 模式"
                ),
                is_error=True,
            )
        try:
            control = update_plan_control(
                ctx.plan_control,
                nodes,
                mode=mode,
                trigger_observation_id=str(
                    args.get("trigger_observation_id") or ""),
                trigger_fact=explanation,
            )
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True)
        item_tuple, status_tuple, active_index, _ = plan_snapshot_views(control)
        if ctx.plan_control_emitter is not None:
            await ctx.emit_plan_control(control)
        else:
            await ctx.emit_plan(
                item_tuple,
                status_tuple,
                active_index,
                explanation,
            )

        completed = sum(
            status == "completed" for status in status_tuple)
        rendered = "\n".join(
            f"{_status_mark(status)} {index}. {step}"
            for index, (step, status) in enumerate(
                zip(item_tuple, status_tuple), 1)
        )
        summary = (
            f"计划已更新（{completed}/{len(item_tuple)} 完成）\n{rendered}"
        )
        if explanation:
            summary += f"\n说明：{explanation}"
        return ToolResult(
            content=summary,
            data={
                "items": list(item_tuple),
                "statuses": list(status_tuple),
                "active_index": active_index,
                "explanation": explanation,
                "plan_control": cast(Any, control.as_dict()),
            },
        )


def _status_mark(status: str) -> str:
    return {
        "pending": "[ ]",
        "in_progress": "[>]",
        "completed": "[x]",
        "blocked": "[!]",
        "failed": "[✗]",
        "cancelled": "[-]",
    }[status]
