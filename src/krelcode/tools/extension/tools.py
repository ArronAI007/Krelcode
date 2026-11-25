"""扩展工具工厂。"""

from __future__ import annotations

from typing import List

from krelcode.contracts.tool import Tool
from .ask_user import AskUserTool
from .update_plan import UpdatePlanTool


def extension_tools() -> List[Tool]:
    return [AskUserTool(), UpdatePlanTool()]
