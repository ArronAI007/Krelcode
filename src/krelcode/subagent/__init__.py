"""子代理/后台任务：把子任务委派给独立的 AgentLoop 并行执行。"""

from .manager import AgentHandle, SubagentManager
from .tools import build_subagent_tools

__all__ = ["AgentHandle", "SubagentManager", "build_subagent_tools"]
