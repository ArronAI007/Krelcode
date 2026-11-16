"""子代理工具：父代理 LLM 通过这四个工具委派/跟踪后台任务。

工具持有 manager 引用（构造时注入），不经过 ToolContext——
ToolContext 保持纯工作区语义。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..tools.base import ToolContext, ToolResult
from .manager import STATUS_PAUSED, STATUS_RUNNING, SubagentManager


class SpawnAgentTool:
    name = "spawn_agent"
    description = (
        "创建一个后台子代理执行委派的子任务（独立上下文，与你的对话并行）。"
        "task 要自包含：子代理看不到你的对话历史。"
        "多个子代理要并行写文件时，传 isolated=true 让每个在独立 git worktree "
        "分支中工作（互不冲突，改动提交到 krelcode/agent-<id> 分支）。"
        "子代理始终继承 Base/Managed/User/Project 指令和与本任务相关的 Memory，"
        "但不会继承父代理完整对话历史。fork_context 仅保留兼容标记。"
        "返回子代理 id；用 wait_agent 等待结果，list_agents 查看状态。"
        "可传 definition 使用 ~/.krelcode/agents 或项目 .krelcode/agents 中的自定义定义。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "自包含的任务描述"},
            "isolated": {"type": "boolean",
                         "description": "true=在独立 git worktree 分支中执行（需工作区为 git 仓库）"},
            "fork_context": {"type": "boolean",
                             "description": "兼容标记；子代理只继承指令与任务相关 Memory，不复制完整历史"},
            "definition": {"type": "string",
                           "description": "可选 Agent Definition 名称（自定义系统提示与工具范围）"},
        },
        "required": ["task"],
    }

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        task = args.get("task")
        if not isinstance(task, str) or not task.strip():
            return ToolResult(content="task 必须是非空字符串", is_error=True)
        try:
            handle = await self.manager.spawn(
                task.strip(),
                isolated=(bool(args["isolated"]) if "isolated" in args else None),
                fork_context=(bool(args["fork_context"]) if "fork_context" in args else None),
                definition=(str(args["definition"]).strip()
                            if args.get("definition") else None),
            )
        except (ValueError, RuntimeError) as e:
            return ToolResult(content=str(e), is_error=True)
        extra = (f"，隔离分支 {handle.branch}（worktree: {handle.worktree}）"
                 if handle.branch else "")
        if handle.definition:
            extra += f"，定义 {handle.definition}"
        if handle.forked:
            extra += "，已继承指令与筛选 Memory（不含完整历史）"
        return ToolResult(
            content=f"已启动子代理 {handle.id}（后台运行）{extra}。"
                    f"用 wait_agent(agent_id=\"{handle.id}\") 等待结果。",
            data={"agent_id": handle.id, "branch": handle.branch,
                  "forked": handle.forked, "definition": handle.definition,
                  "sidechain": str(handle.sidechain_path or "")},
        )


class WaitAgentTool:
    read_only = True
    name = "wait_agent"
    description = (
        "等待子代理完成任务并返回其报告。agent_id 支持逗号分隔多个 id"
        "（一次等多个，各自返回状态与报告）。timeout 秒数可选（默认 60，"
        "超时不取消任务，返回当前状态，可再次 wait）。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string",
                         "description": "spawn_agent 返回的 id；逗号分隔可等多个"},
            "timeout": {"type": "number", "description": "最长等待秒数（默认 60）"},
        },
        "required": ["agent_id"],
    }

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_id = str(args.get("agent_id", ""))
        agent_ids = [t.strip() for t in raw_id.replace("，", ",").split(",") if t.strip()]
        if not agent_ids:
            return ToolResult(content="agent_id 不能为空", is_error=True)
        timeout = args.get("timeout")
        try:
            timeout = float(timeout) if timeout is not None else 60.0
            timeout = max(0.1, min(timeout, 600.0))
        except (TypeError, ValueError):
            timeout = 60.0
        try:
            if len(agent_ids) == 1:
                handles = [await self.manager.wait(agent_ids[0], timeout=timeout)]
            else:
                handles = await self.manager.wait_many(agent_ids, timeout=timeout)
        except (KeyError, RuntimeError) as e:
            return ToolResult(content=str(e), is_error=True)

        sections = []
        all_completed = True
        for handle in handles:
            if handle.status in (STATUS_RUNNING, STATUS_PAUSED):
                all_completed = False
                label = "已暂停" if handle.status == STATUS_PAUSED else "仍在运行"
                sections.append(
                    f"[{handle.id}] {label}（等待 {timeout:g}s 超时）。可稍后再次 wait_agent。")
            else:
                body = handle.result if handle.status == "completed" else (handle.error or "")
                if handle.status != "completed":
                    all_completed = False
                if handle.branch:
                    body += (f"\n[隔离模式: 改动已提交到分支 {handle.branch}，"
                             f"可 git merge/cherry-pick 合入；会话结束后 worktree 自动回收]")
                sections.append(f"[{handle.id}] {handle.status}\n{body}")
        if len(agent_ids) == 1:
            # 单 id：保持与旧版完全一致的内容格式
            handle = handles[0]
            if handle.status in (STATUS_RUNNING, STATUS_PAUSED):
                return ToolResult(content=sections[0],
                                  data={"status": handle.status})
            return ToolResult(
                content=sections[0], is_error=handle.status != "completed",
                data={"status": handle.status})
        return ToolResult(
            content="\n---\n".join(sections),
            is_error=not all_completed,
            data={"statuses": {h.id: h.status for h in handles}},
        )


class SendToAgentTool:
    name = "send_to_agent"
    description = (
        "给子代理追加任务（其上下文延续，记得之前的工作）。"
        "子代理空闲时立即开始；运行中默认排队（完成后自动处理）；"
        "interrupt=true 则打断当前任务立即处理本消息。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "message": {"type": "string", "description": "追加的任务/指示"},
            "interrupt": {"type": "boolean",
                          "description": "true=打断运行中的任务立即处理（默认 false=排队）"},
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
            handle, action = await self.manager.send_input(
                agent_id, message.strip(), interrupt=bool(args.get("interrupt", False)))
        except (KeyError, RuntimeError) as e:
            return ToolResult(content=str(e), is_error=True)
        if action == "queued":
            return ToolResult(
                content=(f"子代理 {handle.id} 正在运行，消息已排队"
                         f"（当前队列 {len(handle.queue)} 条），完成后自动处理。"
                         f"用 wait_agent 等待。"),
                data={"agent_id": handle.id, "action": action},
            )
        if action == "interrupted":
            return ToolResult(
                content=f"已打断子代理 {handle.id}，新消息立即处理。用 wait_agent 等待。",
                data={"agent_id": handle.id, "action": action},
            )
        return ToolResult(
            content=f"已向子代理 {handle.id} 追加任务（后台运行），用 wait_agent 等待。",
            data={"agent_id": handle.id, "action": action},
        )


class GetAgentFeedTool:
    read_only = True
    name = "get_agent_feed"
    description = (
        "读取子代理实时事件流的增量。after_sequence 使用上次返回的 "
        "next_sequence；所有文本已脱敏，sequence 单调递增。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string"},
            "after_sequence": {"type": "integer", "description": "只返回此游标之后的事件"},
            "limit": {"type": "integer", "description": "最多返回条数（默认 100）"},
        },
        "required": ["agent_id"],
    }

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        agent_id = str(args.get("agent_id", ""))
        try:
            after = max(0, int(args.get("after_sequence", 0)))
            limit = max(1, min(int(args.get("limit", 100)), 500))
            page = self.manager.feed(agent_id, after_sequence=after, limit=limit)
        except (KeyError, ValueError, TypeError) as exc:
            return ToolResult(content=str(exc), is_error=True)
        return ToolResult(
            content=page.render(),
            data={
                "agent_id": page.agent_id,
                "next_sequence": page.next_sequence,
                "latest_sequence": page.latest_sequence,
                "truncated": page.truncated,
            },
        )


class PauseAgentTool:
    name = "pause_agent"
    description = "在下一个安全 step 边界暂停子代理；保留 Task、上下文和消息队列。"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {"agent_id": {"type": "string"}},
        "required": ["agent_id"],
    }

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            handle = await self.manager.pause(str(args.get("agent_id", "")))
        except (KeyError, ValueError) as exc:
            return ToolResult(content=str(exc), is_error=True)
        return ToolResult(
            content=f"子代理 {handle.id} 已请求暂停（将在安全边界停止推进）。",
            data={"agent_id": handle.id, "status": handle.status},
        )


class ResumeAgentTool:
    name = "resume_agent"
    description = "恢复已暂停的子代理；从原上下文和暂停点继续，不重建历史。"
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {"agent_id": {"type": "string"}},
        "required": ["agent_id"],
    }

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            handle = await self.manager.resume(str(args.get("agent_id", "")))
        except (KeyError, ValueError) as exc:
            return ToolResult(content=str(exc), is_error=True)
        return ToolResult(
            content=f"子代理 {handle.id} 已恢复运行。",
            data={"agent_id": handle.id, "status": handle.status},
        )


class KillAgentTool:
    name = "kill_agent"
    description = (
        "终止整个子代理（与 interrupt 不同：不会继续处理原任务）。"
        "已有 history/feed/sidechain 会保留。"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {"agent_id": {"type": "string"}},
        "required": ["agent_id"],
    }

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            handle = await self.manager.kill(str(args.get("agent_id", "")))
        except (KeyError, ValueError) as exc:
            return ToolResult(content=str(exc), is_error=True)
        return ToolResult(
            content=(f"子代理 {handle.id} 已终止；sidechain: "
                     f"{handle.sidechain_path or '-'}"),
            data={"agent_id": handle.id, "status": handle.status},
        )


class ListAgentDefinitionsTool:
    read_only = True
    name = "list_agent_definitions"
    description = "列出用户自定义 Agent Definition（名称、来源、工具范围）。"
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, manager: SubagentManager) -> None:
        self.manager = manager

    async def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        lines = self.manager.definition_lines()
        if not lines:
            return ToolResult(content="（未发现 Agent Definition）")
        return ToolResult(content="\n".join(lines), data={"count": len(lines)})


class ListAgentsTool:
    read_only = True
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
        GetAgentFeedTool(manager),
        PauseAgentTool(manager),
        ResumeAgentTool(manager),
        KillAgentTool(manager),
        ListAgentDefinitionsTool(manager),
    ]
