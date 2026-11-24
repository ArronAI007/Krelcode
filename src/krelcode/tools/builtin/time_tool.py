"""get_time：Step 2 的演示/自测工具，用于验证完整 tool-call 循环。

Step 3/4 加入真正的文件与 shell 工具后，它仍然保留（低成本、无害、方便回归）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...contracts.tool import ToolContext
from ..base import ToolResult
from ..prompt import ToolPromptExample


class GetTimeTool:
    read_only = True
    name = "get_time"
    description = "获取当前时间。可选参数 timezone（IANA 时区名，如 Asia/Shanghai）。"
    prompt = """\
Use get_time only when the current wall-clock date or time is required to answer
the user or interpret time-sensitive evidence. Pass an explicit IANA timezone when
the request requires one. Do not use it to measure command duration or replace
repository evidence.
"""
    when_to_use = (
        "The current date or time in a specific timezone is requested.",
        "A time-sensitive comparison needs an authoritative current timestamp.",
    )
    when_not_to_use = (
        "Elapsed or process timing is needed; use the relevant command/runtime measurement.",
        "Historical or repository state is needed instead of the current clock.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Resolve the current time in a project timezone",
            input={"timezone": "Asia/Shanghai"},
            description=(
                "Request an explicit IANA timezone when a current timestamp is "
                "needed for the user's locale."
            ),
        ),
    )
    prompt_version = "1"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "IANA 时区名，缺省为本地时区",
            }
        },
        "required": [],
    }

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        tz_name = args.get("timezone")
        try:
            tz = ZoneInfo(tz_name) if tz_name else None
        except (ZoneInfoNotFoundError, ValueError):
            return ToolResult(content=f"未知时区: {tz_name}", is_error=True)
        now = datetime.now(tz)
        return ToolResult(
            content=now.isoformat(timespec="seconds"),
            data={"tz": str(now.tzname()), "iso": now.isoformat(timespec="seconds")},
        )
