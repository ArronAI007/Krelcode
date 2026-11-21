"""交互式子代理：独立 AgentLoop、实时 feed、控制与 sidechain。"""

from .builtin_agents import (
    CLAUDE_BUILTIN_AGENT_SPECS,
    DEFAULT_BUILTIN_SUBAGENT_NAME,
    BuiltinAgentSpec,
)
from .control import AgentControl
from .definition import AgentDefinition, AgentDefinitionManager
from .feed import AgentFeed, AgentFeedEntry, AgentFeedPage
from .manager import AgentHandle, SubagentManager
from .model_aliases import resolve_agent_model_spec
from .prompt import build_spawn_agent_prompt
from .sidechain import AgentSidechainWriter, SidechainStore
from .tools import build_subagent_tools

__all__ = [
    "AgentControl",
    "AgentDefinition",
    "AgentDefinitionManager",
    "AgentFeed",
    "AgentFeedEntry",
    "AgentFeedPage",
    "AgentHandle",
    "AgentSidechainWriter",
    "BuiltinAgentSpec",
    "CLAUDE_BUILTIN_AGENT_SPECS",
    "DEFAULT_BUILTIN_SUBAGENT_NAME",
    "SidechainStore",
    "SubagentManager",
    "build_spawn_agent_prompt",
    "build_subagent_tools",
    "resolve_agent_model_spec",
]
