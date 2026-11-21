"""子代理工具：父代理 LLM 通过这四个工具委派/跟踪后台任务。

工具持有 manager 引用（构造时注入），不经过 ToolContext——
ToolContext 保持纯工作区语义。
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..tools.base import ToolContext, ToolResult
from ..tools.prompt import ToolPromptExample
from .manager import STATUS_PAUSED, STATUS_RUNNING, SubagentManager
from .prompt import SPAWN_AGENT_PROMPT_VERSION, build_spawn_agent_prompt


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
        "省略 definition 时默认使用 builtin general-purpose。"
        "可显式选择 Explore、Plan、general-purpose、claude、statusline-setup、"
        "claude-code-guide 或 verifier；也可使用 ~/.krelcode/agents 与项目 "
        ".krelcode/agents 中的自定义定义。"
    )
    prompt = build_spawn_agent_prompt(include_fork=True)
    prompt_version = SPAWN_AGENT_PROMPT_VERSION
    when_to_use = (
        "An independent, bounded subtask can run in the background without the "
        "parent's full conversation history.",
        "Parallel read-only investigation or an isolated worktree task will "
        "materially shorten the critical path.",
        "A non-trivial implementation needs independent adversarial verification; "
        "use definition=\"verifier\" with the exact scope, baseline, observed "
        "checks, and claims to challenge.",
    )
    when_not_to_use = (
        "The next step depends immediately on the result, so a local blocking "
        "tool call is simpler.",
        "The work needs the parent's full private history or cannot be described "
        "as a self-contained handoff.",
        "The parent would duplicate the same work while the child runs.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Delegate a bounded regression fix",
            input={
                "task": (
                    "In src/example/parser.py, parse_header() drops quoted commas. "
                    "Reproduce with tests/test_parser.py::test_quoted_comma. Update "
                    "parser.py and add one focused regression test; do not refactor "
                    "other tokenization paths. Run the focused test and parser suite. "
                    "Return changed paths, test results, and remaining uncertainty."
                ),
                "isolated": True,
            },
            description=(
                "Names the current state, exact entry points, boundaries, deliverable, "
                "and verification so the child does not guess."
            ),
        ),
        ToolPromptExample(
            title="Fork with an explicit state handoff",
            input={
                "task": (
                    "Confirmed: SessionStore already writes provider_meta; resume still "
                    "drops it while rebuilding from JSONL in session/restore.py. "
                    "Completed: failing test added. Remaining: fix restore mapping and "
                    "verify legacy records still load. Do not edit provider adapters. "
                    "Run the session tests and report any compatibility risk."
                ),
                "fork_context": True,
            },
            description=(
                "For fork_context=true, hand-carry confirmed state, completed work, "
                "remaining scope, and checks because the parent history is not copied."
            ),
        ),
        ToolPromptExample(
            title="Request independent adversarial verification",
            input={
                "task": (
                    "Verify the current refactor of src/example/parser.py against "
                    "HEAD. Claim: behavior is unchanged and all existing parser tests "
                    "still pass without edits. Baseline: parser suite passed before the "
                    "change. Challenge hidden behavior drift, public API compatibility, "
                    "error/default/order changes, and any weakened tests. Run the "
                    "original tests unchanged and return the structured verification report."
                ),
                "definition": "verifier",
            },
            description=(
                "Uses the built-in verifier to challenge an implementation claim with "
                "independent evidence instead of trusting the implementer's summary."
            ),
        ),
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "自包含委派说明：写明当前状态/心智模型、已验证事实与假设、"
                    "为什么委派、精确文件/符号/命令/错误、禁止范围、交付物和验证方式；"
                    "不得依赖父对话或要求子代理猜测。"
                ),
            },
            "isolated": {"type": "boolean",
                         "description": "true=在独立 git worktree 分支中执行（需工作区为 git 仓库）"},
            "fork_context": {"type": "boolean",
                             "description": "兼容标记；子代理只继承指令与任务相关 Memory，不复制完整历史"},
            "definition": {"type": "string",
                           "description": (
                               "可选 Agent Definition 名称；省略时使用 general-purpose。"
                               "内置值: Explore、Plan、general-purpose、claude、"
                               "statusline-setup、claude-code-guide、verifier")},
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
    prompt = """\
Wait for subagents when their result is required for the next step. Prefer bounded
waits and continue useful non-overlapping work instead of polling repeatedly. A
timeout reports current state and does not cancel the child. Inspect the final
status and report; do not treat an intermediate feed message as a completed result.
"""
    when_to_use = (
        "The parent's next action depends on one or more delegated results.",
        "A child is expected to complete soon and a bounded blocking wait is cheaper than polling.",
    )
    when_not_to_use = (
        "Useful independent parent work can proceed while the child runs.",
        "No agent id is known or the task should be managed rather than merely waited on.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Wait briefly for delegated results",
            input={"agent_id": "agent-01,agent-02", "timeout": 30},
            description=(
                "Use a bounded wait only when the parent's next action depends on "
                "the child reports; a timeout does not cancel them."
            ),
        ),
    )
    prompt_version = "1"
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
    prompt = """\
Use send_to_agent to add constraints, corrections, or a follow-up task to an
existing child while preserving its context. Queue by default so current work is
not lost; interrupt only when the current direction is unsafe or materially wrong.
Send a concise, self-contained instruction and do not duplicate a message already
queued unless changed evidence requires it.
"""
    when_to_use = (
        "A running or idle child needs new constraints, a correction, or a follow-up task.",
        "The current direction is unsafe or materially wrong and must be interrupted.",
    )
    when_not_to_use = (
        "The child does not exist or should be resumed first.",
        "The message merely repeats an instruction already queued without new evidence.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Queue a corrective follow-up",
            input={
                "agent_id": "agent-01",
                "message": (
                    "Limit changes to src/example/parser.py and its focused test. "
                    "Do not modify public APIs; rerun the parser suite afterward."
                ),
                "interrupt": False,
            },
            description=(
                "Preserve the child's context and current step by queuing a concise "
                "correction instead of interrupting unnecessarily."
            ),
        ),
    )
    prompt_version = "1"
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
    prompt = """\
Use get_agent_feed for incremental progress visibility and diagnosis of a known
child. Advance after_sequence instead of rereading events, and keep reads bounded.
Feed events are evidence about progress; they are not the final result and do not
prove task completion.
"""
    when_to_use = (
        "A child's recent progress or failure needs diagnosis before waiting or intervening.",
        "New events are needed after a known sequence cursor.",
    )
    when_not_to_use = (
        "The child's final result is required; use wait_agent.",
        "The full event history would be dumped without a diagnostic purpose.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Inspect new child events for diagnosis",
            input={
                "agent_id": "agent-01",
                "after_sequence": 12,
                "limit": 50,
            },
            description=(
                "Advance the event cursor and keep the feed bounded when diagnosing "
                "progress or a failure."
            ),
        ),
    )
    prompt_version = "1"
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
    prompt = """\
Use pause_agent when a running child's direction must be inspected before more work
continues, or when work must stop temporarily without discarding its context. Pause
occurs at the next safe step boundary; inspect the feed/state before deciding to
resume, send new input, or kill the child.
"""
    when_to_use = (
        "The child's current direction needs inspection before it continues.",
        "Temporary suspension is preferable to losing context or terminating the child.",
    )
    when_not_to_use = (
        "The child is obsolete, unrecoverable, or unsafe; kill it instead.",
        "The concern can be corrected immediately with a bounded send_to_agent message.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Pause at the next safe step boundary",
            input={"agent_id": "agent-01"},
            description=(
                "Stop further progress temporarily while preserving task context "
                "for inspection or correction."
            ),
        ),
    )
    prompt_version = "1"
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
    prompt = """\
Use resume_agent only after the concern that caused a pause is resolved and the
child still has a valid task. Resume continues the existing context and pause point;
do not use it to restart or silently broaden the task.
"""
    when_to_use = (
        "A paused child is still relevant and the blocking concern is resolved.",
        "The child should continue from its preserved context.",
    )
    when_not_to_use = (
        "The child is completed, failed, obsolete, or unsafe.",
        "The task scope changed enough that a fresh self-contained spawn is safer.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Resume a corrected paused child",
            input={"agent_id": "agent-01"},
            description=(
                "Continue from the preserved pause point only after the blocking "
                "concern has been resolved."
            ),
        ),
    )
    prompt_version = "1"
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
    prompt = """\
Use kill_agent when a child is obsolete, unsafe, unrecoverable, duplicated by a
completed result, or no longer worth its resource use. This terminates the entire
task rather than interrupting one step. Preserve and report any useful partial
result, feed, or sidechain evidence.
"""
    when_to_use = (
        "The child's task is no longer needed or is unsafe to continue.",
        "The child cannot recover and continued execution would waste resources.",
    )
    when_not_to_use = (
        "A temporary pause, correction, or new instruction can recover the work.",
        "The child is already complete and its result can simply be collected.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Terminate an obsolete child task",
            input={"agent_id": "agent-01"},
            description=(
                "Kill only when the whole task is obsolete or unsafe; preserve any "
                "useful partial feed and sidechain evidence."
            ),
        ),
    )
    prompt_version = "1"
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
    description = "列出内置与用户自定义 Agent Definition（名称、来源、工具范围）。"
    prompt = """\
Use list_agent_definitions before spawning when specialized agent roles may exist.
Inspect names, sources, and available tool scopes, then choose the narrowest role
that fits the task. Do not guess a definition name without checking the catalogue.
"""
    when_to_use = (
        "A specialized role may better fit a bounded task.",
        "The available custom agent definitions or their tool scopes are uncertain.",
    )
    when_not_to_use = (
        "The task is ordinary work that the main agent can perform directly.",
        "The appropriate role is already known and no catalogue check is needed.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Inspect available specialized roles",
            input={},
            description=(
                "Check definition names and tool scopes before choosing a role for "
                "a bounded delegated task."
            ),
        ),
    )
    prompt_version = "1"
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
    prompt = """\
Use list_agents to recover the current child-agent set and status before managing,
waiting for, resuming, or assuming a result. Treat previews as status evidence, not
as final deliverables, and avoid re-spawning an already active duplicate.
"""
    when_to_use = (
        "The current child agents or their states are uncertain.",
        "The parent is resuming, compacting, or reconciling parallel delegated work.",
    )
    when_not_to_use = (
        "A specific known child's final result is needed; use wait_agent.",
        "No delegation has occurred and status is irrelevant to the task.",
    )
    prompt_examples = (
        ToolPromptExample(
            title="Recover the child-agent roster",
            input={},
            description=(
                "Inspect current ids and statuses before waiting, resuming, sending "
                "input, or assuming that delegated work completed."
            ),
        ),
    )
    prompt_version = "1"
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
