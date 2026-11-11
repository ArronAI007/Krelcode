"""子代理工具：父代理 LLM 通过这四个工具委派/跟踪后台任务。

工具持有 manager 引用（构造时注入），不经过 ToolContext——
ToolContext 保持纯工作区语义。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..tools.base import ToolContext, ToolResult
from .manager import STATUS_RUNNING, SubagentManager


class SpawnAgentTool:
    name = "spawn_agent"
    description = (
        "创建一个后台子代理执行委派的子任务（独立上下文，与你的对话并行）。"
        "task 要自包含：子代理看不到你的对话历史。"
        "返回子代理 id；用 wait_agent 等待结果，list_agents 查看状态。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "自包含的任务描述"},
        },
        "required": ["task"],
    }

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        task = args.get("task")
        if not isinstance(task, str) or not task.strip():
            return ToolResult(content="task 必须是非空字符串", is_error=True)
        handle = await self.manager.spawn(task.strip())
        return ToolResult(
            content=f"已启动子代理 {handle.id}（后台运行）。"
                    f"用 wait_agent(agent_id=\"{handle.id}\") 等待结果。",
            data={"agent_id": handle.id},
        )


class WaitAgentTool:
    name = "wait_agent"
    description = (
        "等待子代理完成任务并返回其报告。timeout 秒数可选（默认 60，超时不取消任务，"
        "返回当前状态，可再次 wait）。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "spawn_agent 返回的 id"},
            "timeout": {"type": "number", "description": "最长等待秒数（默认 60）"},
        },
        "required": ["agent_id"],
    }

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        agent_id = str(args.get("agent_id", ""))
        timeout = args.get("timeout")
        try:
            timeout = float(timeout) if timeout is not None else 60.0
            timeout = max(0.1, min(timeout, 600.0))
        except (TypeError, ValueError):
            timeout = 60.0
        try:
            handle = await self.manager.wait(agent_id, timeout=timeout)
        except KeyError as e:
            return ToolResult(content=str(e), is_error=True)
        if handle.status == STATUS_RUNNING:
            return ToolResult(
                content=f"[{handle.id}] 仍在运行（等待 {timeout:g}s 超时）。可稍后再次 wait_agent。",
                data={"status": handle.status},
            )
        body = handle.result if handle.status == "completed" else (handle.error or "")
        return ToolResult(
            content=f"[{handle.id}] {handle.status}\n{body}",
            is_error=handle.status != "completed",
            data={"status": handle.status},
        )


class SendToAgentTool:
    name = "send_to_agent"
    description = (
        "给已完成/失败的子代理追加一轮任务（其上下文延续，记得之前的工作）。"
        "子代理仍在运行时会报错，请先 wait_agent。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "message": {"type": "string", "description": "追加的任务/指示"},
        },
        "required": ["agent_id", "message"],
    }

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        agent_id = str(args.get("agent_id", ""))
        message = args.get("message")
        if not isinstance(message, str) or not message.strip():
            return ToolResult(content="message 必须是非空字符串", is_error=True)
        try:
            handle = self.manager.send_input(agent_id, message.strip())
        except KeyError as e:
            return ToolResult(content=str(e), is_error=True)
        except RuntimeError as e:
            return ToolResult(content=str(e), is_error=True)
        return ToolResult(
            content=f"已向子代理 {handle.id} 追加任务（后台运行），用 wait_agent 等待。",
            data={"agent_id": handle.id},
        )


class ListAgentsTool:
    name = "list_agents"
    description = "列出全部子代理的状态（id/状态/轮数/任务与结果预览）。"
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        lines = self.manager.status_lines()
        if not lines:
            return ToolResult(content="（暂无子代理）")
        return ToolResult(content="\n".join(lines), data={"count": len(lines)})


def build_subagent_tools(manager: SubagentManager) -> List[Any]:
    return [
        SpawnAgentTool(manager),
        WaitAgentTool(manager),
        SendToAgentTool(manager),
        ListAgentsTool(manager),
    ]
