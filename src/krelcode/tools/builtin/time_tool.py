"""get_time：Step 2 的演示/自测工具，用于验证完整 tool-call 循环。

Step 3/4 加入真正的文件与 shell 工具后，它仍然保留（低成本、无害、方便回归）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..base import ToolContext, ToolResult


class GetTimeTool:
    name = "get_time"
    description = "获取当前时间。可选参数 timezone（IANA 时区名，如 Asia/Shanghai）。"
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
