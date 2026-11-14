"""SubagentManager：子代理生命周期管理。

每个子代理 = 一个独立 AgentLoop（自己的历史/事件总线/工具注册表），
共享父级的 provider、ToolContext（工作区）与审批策略。
后台执行用 asyncio.Task；父代理经 spawn/wait/send/list 四个工具驱动。

Step 23 增强（对齐 Codex multi_agent 语义）：
- **消息队列**：子代理运行中收到 send_input → 入队，当前任务完成后自动续跑，
  同一 Task 内排空队列（wait 的语义 = 队列排空）
- **interrupt**：取消当前 Task → 历史消毒（见 _sanitize_history）→ 立即
  开新 Task 处理打断消息；队列中未处理的消息保留
- **wait_many**：一次等多个子代理（asyncio.wait，超时不取消任务）

Step 41 增强——**git worktree 隔离**（isolated=True）：
- 子代理在独立 worktree（分支 krelcode/agent-<id>，位于
  <workspace>/.krelcode/worktrees/<id>）中工作，文件写入被路径沙箱
  天然限制在 worktree 内——多个子代理并行写同名文件互不干扰
- 任务排空后未提交变更自动 commit 到隔离分支（带 -c 身份兜底，
  不依赖用户 git config）；shutdown 回收 worktree，**分支保留**供
  merge/cherry-pick；主工作区全程不受影响
- 基于当前 HEAD 建分支（不含主工作区未提交改动）；要求工作区是
  git 仓库，否则 spawn 抛 ValueError（工具层转 is_error）

Step 42 增强——**fork_context**（依赖型子任务）：
- spawn(task, fork_context=True) 把父对话历史深拷贝进子代理
  （去掉父 system，换上子代理 system；子代理由此"看得见"任务背景）
- 尾部消毒：spawn 发生在父轮执行中，历史尾部必然是悬挂的
  assistant(tool_calls)（spawn 调用本身）→ sanitize_history 弹掉；
  若消毒后尾部是未答完的 user 消息（正是触发委派的那句），弹出并
  与子任务合并——既保上下文又保 user/assistant 严格交替（Anthropic 要求）

防递归：子代理的工具集由 tool_factory 提供（app 里 = default_tools()，
不含 spawn 系工具），委派深度硬限制为 1 层。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..agent.compression import ContextManager
from ..agent.context import _git
from ..agent.loop import DEFAULT_MAX_STEPS, AgentLoop
from ..agent.prompts import build_system_prompt
from ..session.autocommit import ensure_local_exclude, git_is_repo
from ..core.events import EventBus, SubagentFinished, SubagentSpawned
from ..core.redact import SecretRedactor
from ..llm.base import LLMProvider
from ..safety.policy import AllowAllPolicy
from ..tools.base import Tool, ToolContext
from ..tools.registry import ToolRegistry

SUBAGENT_SYSTEM_PROMPT = build_system_prompt(
    "You are a Krelcode SUBAGENT working on a delegated subtask.\n"
    "- Focus strictly on the task you were given; do not ask the user questions.\n"
    "- Use tools to do the work and verify results when possible.\n"
    "- Your final text message is the report handed back to the parent agent: "
    "make it dense and self-contained (what was done, key results, file paths, caveats)."
)

PREVIEW_CHARS = 160

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

INTERRUPT_MARKER = "\n[被打断，用户新指示] "


@dataclass
class AgentHandle:
    id: str
    task: str
    loop: AgentLoop
    status: str = STATUS_RUNNING
    result: Optional[str] = None
    error: Optional[str] = None
    turns: int = 0
    queue: List[str] = field(default_factory=list)
    task_obj: Optional[asyncio.Task] = field(default=None, repr=False)
    # 隔离模式（Step 41）：worktree 路径与分支名；非隔离为 None
    worktree: Optional[Path] = None
    branch: Optional[str] = None
    forked: bool = False   # Step 42：是否继承了父对话上下文

    def preview(self) -> str:
        text = self.result or self.error or ""
        if len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS] + "…"
        return text.replace("\n", " ")


# sanitize_history 已下沉到 core.messages（主代理取消修复与子代理打断共用）；
# 此处 re-export 保持既有导入路径兼容
from ..core.messages import Message, sanitize_history  # noqa: E402,F401


class SubagentManager:
    def __init__(
        self,
        llm: LLMProvider,
        tool_factory: Callable[[], List[Tool]],
        tool_context: ToolContext,
        policy=None,
        bus: Optional[EventBus] = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        context_budget: Optional[int] = None,
        redactor: Optional[SecretRedactor] = None,
        system_prompt_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self.llm = llm
        self.tool_factory = tool_factory
        self.tool_context = tool_context
        self.policy = policy or AllowAllPolicy()
        self.bus = bus or EventBus()
        self.max_steps = max_steps
        # 子代理同样需要上下文管理：长任务（多轮工具调用）历史会膨胀，
        # 无压缩将超出模型窗口导致 provider 400。None=不启用（向后兼容）。
        self.context_budget = context_budget
        self.redactor = redactor
        self.system_prompt_provider = system_prompt_provider
        self._handles: Dict[str, AgentHandle] = {}
        self._counter = 0
        # fork_context 数据源：app 组装根注入 lambda: loop.history
        # （用 getter 而非直接引用，/resume 重绑定 loop.history 后依然正确）
        self.get_parent_history: Optional[Callable[[], List[Message]]] = None

    # -- 生命周期 ---------------------------------------------------------------------

    async def spawn(self, task: str, isolated: bool = False,
                    fork_context: bool = False) -> AgentHandle:
        """创建一个后台子代理并立即开始执行任务。

        isolated=True：在独立 git worktree（分支 krelcode/agent-<id>）中执行，
        并行写文件互不干扰；完成后变更自动提交到分支。
        要求主工作区是 git 仓库，否则抛 ValueError。

        fork_context=True：深拷贝父对话历史进子代理（依赖型子任务用）。
        """
        self._counter += 1
        agent_id = f"a{self._counter}"
        worktree_path: Optional[Path] = None
        branch: Optional[str] = None
        tool_context = self.tool_context
        if isolated:
            worktree_path, branch = await self._create_worktree(agent_id)
            parent_sandbox = getattr(self.tool_context, "sandbox", None)
            tool_context = ToolContext(
                workspace_root=worktree_path,
                sandbox=(parent_sandbox.rebind(worktree_path)
                         if parent_sandbox is not None else None),
                sandbox_enabled=parent_sandbox is not None,
            )
        child_bus = EventBus()
        loop = AgentLoop(
            llm=self.llm,
            registry=ToolRegistry(self.tool_factory()),
            tool_context=tool_context,
            policy=self.policy,
            bus=child_bus,
            system_prompt=(
                self.system_prompt_provider()
                if self.system_prompt_provider is not None
                else SUBAGENT_SYSTEM_PROMPT
            ),
            max_steps=self.max_steps,
            # spawn 时以当时的 self.llm 建压缩器（/model 热切换后新建的子代理自动用新模型）
            context_manager=(ContextManager(self.llm, max_tokens=self.context_budget)
                             if self.context_budget else None),
            redactor=self.redactor,
        )
        effective_task = task
        forked = False
        if fork_context and self.get_parent_history is not None:
            forked_history, pending_user = self._fork_parent_history()
            if pending_user is not None:
                # 触发委派的未答完 user 消息与子任务合并（保角色交替）
                effective_task = f"{pending_user}\n\n[委派的子任务] {task}".strip()
            loop.history.extend(forked_history)
            forked = True
        handle = AgentHandle(id=agent_id, task=task, loop=loop,
                             worktree=worktree_path, branch=branch, forked=forked)
        self._handles[agent_id] = handle
        handle.task_obj = asyncio.create_task(self._run(handle, effective_task))
        await self.bus.emit(SubagentSpawned(agent_id, task))
        return handle

    def _fork_parent_history(self) -> Tuple[List[Message], Optional[str]]:
        """深拷贝父历史（去父 system、尾部消毒）。

        返回 (forked_messages, pending_user_text)：消毒后若尾部是未答完的
        user 消息则弹出并作为第二返回值（调用方与子任务合并，避免连续 user）。
        """
        source = self.get_parent_history() or []
        start = 1 if source and source[0].role == "system" else 0
        forked = [
            replace(m, tool_calls=list(m.tool_calls) if m.tool_calls else None)
            for m in source[start:]
        ]
        sanitize_history(forked)   # 弹掉悬挂的 assistant(tool_calls)/tool 尾部
        pending_user: Optional[str] = None
        if forked and forked[-1].role == "user":
            pending_user = forked.pop().content or ""
        return forked, pending_user

    # -- git worktree 隔离（Step 41）---------------------------------------------------

    async def _create_worktree(self, agent_id: str) -> Tuple[Path, str]:
        ws = self.tool_context.workspace_root
        if not await git_is_repo(ws):
            raise ValueError(
                "隔离子代理要求工作区是 git 仓库（先 git init，或不用 isolated）")
        # worktree 位于 .krelcode/ 下：确保本地 exclude，不污染主仓库 status
        await ensure_local_exclude(ws, ".krelcode/")
        branch = f"krelcode/agent-{agent_id}"
        path = ws / ".krelcode" / "worktrees" / agent_id
        path.parent.mkdir(parents=True, exist_ok=True)
        out = await _git(ws, "worktree", "add", str(path), "-b", branch)
        if out is None:
            raise ValueError(f"git worktree 创建失败（分支 {branch} 可能已存在）")
        return path, branch

    async def _finalize_isolated(self, handle: AgentHandle) -> None:
        """把 worktree 中未提交的变更存档提交到隔离分支（幂等）。"""
        wt = handle.worktree
        if wt is None or not wt.is_dir():
            return
        status = await _git(wt, "status", "--porcelain")
        if not status:
            return
        await _git(wt, "add", "-A")
        label = handle.task.strip().replace("\n", " ")[:60]
        # -c 身份兜底：不依赖用户 git config user.*
        await _git(wt,
                   "-c", "user.name=krelcode",
                   "-c", "user.email=agent@krelcode.local",
                   "commit", "-m", f"krelcode({handle.id}): {label}")

    async def _remove_worktree(self, handle: AgentHandle) -> None:
        """回收 worktree（分支保留，供 merge/cherry-pick）。"""
        if handle.worktree is None:
            return
        ws = self.tool_context.workspace_root
        await _git(ws, "worktree", "remove", "--force", str(handle.worktree))
        await _git(ws, "worktree", "prune")

    async def _run(self, handle: AgentHandle, message: str) -> None:
        """执行一条消息；完成后若队列非空则继续排空（同一 Task）。"""
        while True:
            handle.status = STATUS_RUNNING
            handle.result = None
            handle.error = None
            try:
                result = await handle.loop.run(message)
                handle.result = result
                handle.status = STATUS_COMPLETED
                handle.turns += 1
            except asyncio.CancelledError:
                handle.status = STATUS_CANCELLED
                handle.error = "任务被打断/取消"
                await self.bus.emit(
                    SubagentFinished(handle.id, handle.status, handle.preview()))
                raise
            except Exception as e:
                handle.status = STATUS_FAILED
                handle.error = f"{type(e).__name__}: {e}"
                handle.turns += 1
            await self.bus.emit(
                SubagentFinished(handle.id, handle.status, handle.preview()))
            if handle.queue:
                message = handle.queue.pop(0)
                continue
            if handle.worktree is not None:
                # 队列排空：把隔离分支上的工作存档（失败/完成都保留成果）
                await self._finalize_isolated(handle)
            return

    def get(self, agent_id: str) -> Optional[AgentHandle]:
        return self._handles.get(agent_id)

    def is_running(self, handle: AgentHandle) -> bool:
        return handle.task_obj is not None and not handle.task_obj.done()

    # -- 交互：send / interrupt -----------------------------------------------------------

    async def send_input(
        self, agent_id: str, message: str, interrupt: bool = False
    ) -> Tuple[AgentHandle, str]:
        """给子代理发消息。返回 (handle, action)。

        action ∈ started（空闲，立即开跑）/ queued（运行中，排队）/
        interrupted（打断当前任务，立即处理本消息）。
        """
        handle = self._handles.get(agent_id)
        if handle is None:
            raise KeyError(f"未知子代理: {agent_id}")

        if not self.is_running(handle):
            handle.task = message
            handle.task_obj = asyncio.create_task(self._run(handle, message))
            return handle, "started"

        if not interrupt:
            handle.queue.append(message)
            return handle, "queued"

        # interrupt：取消当前 Task → 消毒历史 → 立即处理新消息
        handle.task_obj.cancel()
        await asyncio.gather(handle.task_obj, return_exceptions=True)
        sanitize_history(handle.loop.history)
        # 历史尾部若是未答完的 user 消息，把打断指示合并进去
        # （避免连续两条 user 消息——Anthropic 要求角色交替）；
        # 合并后先弹出，_run→loop.run() 会把它作为新 user 消息重新追加
        if handle.loop.history and handle.loop.history[-1].role == "user":
            first = ((handle.loop.history[-1].content or "")
                     + INTERRUPT_MARKER + message)
            handle.loop.history.pop()
        else:
            first = message
        handle.task = message
        handle.task_obj = asyncio.create_task(self._run(handle, first))
        return handle, "interrupted"

    # -- 等待 -----------------------------------------------------------------------------

    async def wait(self, agent_id: str, timeout: Optional[float] = None) -> AgentHandle:
        """等待单个子代理队列排空；超时返回当前状态（不取消任务）。"""
        handle = self._handles.get(agent_id)
        if handle is None:
            raise KeyError(f"未知子代理: {agent_id}")
        await self._wait_handles([handle], timeout)
        return handle

    async def wait_many(
        self, agent_ids: List[str], timeout: Optional[float] = None
    ) -> List[AgentHandle]:
        """一次等待多个子代理；任一未知 id 抛 KeyError。"""
        handles = []
        for agent_id in agent_ids:
            handle = self._handles.get(agent_id)
            if handle is None:
                raise KeyError(f"未知子代理: {agent_id}")
            handles.append(handle)
        await self._wait_handles(handles, timeout)
        return handles

    @staticmethod
    async def _wait_handles(handles: List[AgentHandle], timeout: Optional[float]) -> None:
        tasks = [h.task_obj for h in handles if h.task_obj is not None and not h.task_obj.done()]
        if not tasks:
            return
        # asyncio.wait 超时不取消任务（与 shield 语义一致且支持多任务）
        await asyncio.wait(tasks, timeout=timeout)

    async def shutdown(self) -> None:
        """取消所有运行中的子代理并回收隔离 worktree（分支保留）。"""
        running = [h for h in self._handles.values() if self.is_running(h)]
        for handle in running:
            handle.task_obj.cancel()
        if running:
            await asyncio.gather(
                *(h.task_obj for h in running), return_exceptions=True)
        for handle in self._handles.values():
            if handle.worktree is not None:
                await self._finalize_isolated(handle)   # 被取消的也存档已有成果
                await self._remove_worktree(handle)

    # -- 观测 -------------------------------------------------------------------------

    def status_lines(self) -> List[str]:
        lines = []
        for handle in self._handles.values():
            task_preview = handle.task
            if len(task_preview) > 60:
                task_preview = task_preview[:60] + "…"
            queued = f" · 排队 {len(handle.queue)}" if handle.queue else ""
            branch = f" · 分支 {handle.branch}" if handle.branch else ""
            branch += " · 继承上下文" if handle.forked else ""
            line = (f"{handle.id} [{handle.status}] {handle.turns} 轮{queued}{branch} · "
                    f"任务: {task_preview.replace(chr(10), ' ')}")
            if handle.status in (STATUS_COMPLETED, STATUS_FAILED):
                line += f" · 结果: {handle.preview()}"
            lines.append(line)
        return lines

    def build_tools(self) -> List[Tool]:
        from .tools import build_subagent_tools
        return build_subagent_tools(self)
