"""ask_user：让模型在继续执行前向用户澄清或请求选择。"""

from __future__ import annotations

from typing import Any, Dict

from ..base import ToolContext, ToolResult


class AskUserTool:
    """把问题交给注入的 frontend port；没有前台时立即 fail-closed。"""

    name = "ask_user"
    read_only = True
    description = (
        "当任务缺少关键决策、存在歧义或需要用户确认方案时，向用户提出一个"
        "简短问题并等待回答。可提供少量选项；不要用它询问可从工作区自行查明"
        "的信息，也不要在子代理中调用。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "minLength": 1,
                "maxLength": 2_000,
                "description": "要询问用户的问题",
            },
            "options": {
                "type": "array",
                "maxItems": 8,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 300},
                "description": "可选项；用户也可输入编号。缺省表示自由文本",
            },
            "allow_free_text": {
                "type": "boolean",
                "default": True,
                "description": "有选项时是否仍允许自由文本回答",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        prompt = str(args.get("question", "")).strip()
        raw_options = args.get("options") or []
        options = tuple(
            str(option).strip() for option in raw_options if str(option).strip())
        allow_free_text = bool(args.get("allow_free_text", True))
        answer = await ctx.request_user_input(
            prompt,
            options,
            allow_free_text=allow_free_text,
        )
        if answer is None:
            return ToolResult(
                content=(
                    "用户未提供输入：当前没有可交互前台，或请求已被取消。"
                    "请基于已有信息继续；不要重复等待同一答案。"
                ),
                is_error=True,
                data={
                    "prompt": prompt,
                    "options": list(options),
                    "cancelled": True,
                },
            )

        index = next(
            (i for i, option in enumerate(options, 1) if option == answer), None)
        return ToolResult(
            content=f"用户回答：{answer}",
            data={
                "prompt": prompt,
                "answer": answer,
                "option_index": index,
            },
        )
