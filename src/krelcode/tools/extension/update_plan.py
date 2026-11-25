"""update_plan：发布结构化执行计划快照。"""

from __future__ import annotations

from typing import Any, Dict

from ...contracts.tool import ToolContext
from krelcode.contracts.tool import ToolResult
from ..prompt import ToolPromptExample

ALLOWED_STATUSES = ("pending", "in_progress", "completed")


class UpdatePlanTool:
    name = "update_plan"
    read_only = True
    description = (
        "创建或更新本任务的结构化执行计划。多步骤任务在开始和阶段变化时调用；"
        "简单单步任务无需调用。每次提交完整快照，最多一个步骤为 in_progress。"
    )
    prompt = """\
Use update_plan for multi-step work that benefits from visible progress, not for
trivial single-step tasks. Submit a complete current snapshot, keep at most one
step in_progress, mark completed work promptly, and revise the plan when new
evidence changes the approach. The plan is a status model, not a substitute for
doing or verifying the work.
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
                    {"step": "Inspect the parser and tests", "status": "completed"},
                    {"step": "Implement the bounded fix", "status": "in_progress"},
                    {"step": "Run focused verification", "status": "pending"},
                ],
                "explanation": "The evidence changed the implementation order.",
            },
            description=(
                "Submit the complete plan, with at most one in-progress step, "
                "when the approach or evidence changes."
            ),
        ),
    )
    prompt_version = "1"
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
                        "status": {
                            "type": "string",
                            "enum": list(ALLOWED_STATUSES),
                        },
                    },
                    "required": ["step", "status"],
                    "additionalProperties": False,
                },
            },
            "explanation": {
                "type": "string",
                "maxLength": 2_000,
                "description": "本次计划变更的简短说明",
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_items = args.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return ToolResult(content="计划 items 必须是非空数组", is_error=True)

        items = []
        statuses = []
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                return ToolResult(
                    content=f"计划第 {index + 1} 项必须是 object", is_error=True)
            step = str(raw.get("step", "")).strip()
            status = str(raw.get("status", "")).strip()
            if not step:
                return ToolResult(
                    content=f"计划第 {index + 1} 项缺少 step", is_error=True)
            if status not in ALLOWED_STATUSES:
                return ToolResult(
                    content=(
                        f"计划第 {index + 1} 项 status 非法: {status!r}；"
                        "允许 pending / in_progress / completed"
                    ),
                    is_error=True,
                )
            items.append(step)
            statuses.append(status)

        active_indexes = [
            index for index, status in enumerate(statuses)
            if status == "in_progress"
        ]
        if len(active_indexes) > 1:
            return ToolResult(
                content="计划最多只能有一个 in_progress 步骤", is_error=True)
        active_index = active_indexes[0] if active_indexes else -1
        explanation = str(args.get("explanation", "")).strip()
        item_tuple = tuple(items)
        status_tuple = tuple(statuses)

        await ctx.emit_plan(
            item_tuple,
            status_tuple,
            active_index,
            explanation,
        )

        completed = sum(status == "completed" for status in statuses)
        rendered = "\n".join(
            f"{_status_mark(status)} {index}. {step}"
            for index, (step, status) in enumerate(zip(items, statuses), 1)
        )
        summary = f"计划已更新（{completed}/{len(items)} 完成）\n{rendered}"
        if explanation:
            summary += f"\n说明：{explanation}"
        return ToolResult(
            content=summary,
            data={
                "items": list(item_tuple),
                "statuses": list(status_tuple),
                "active_index": active_index,
                "explanation": explanation,
            },
        )


def _status_mark(status: str) -> str:
    return {
        "pending": "[ ]",
        "in_progress": "[>]",
        "completed": "[x]",
    }[status]
