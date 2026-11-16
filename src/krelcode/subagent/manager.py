"""子代理生命周期、交互式控制、实时 feed 与 sidechain 持久化。

每个子代理拥有独立 AgentLoop、history、ToolRegistry 和 child EventBus，共享
父级 provider、工作区与审批策略。Pause 是协作式的 step 边界暂停，Kill 才
取消 Task；Feed 使用稳定 sequence 供 UI/SDK 游标读取，sidechain 独立落盘。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..agent.compression import ContextManager
from ..agent.context import _git
from ..agent.loop import DEFAULT_MAX_STEPS, AgentLoop
from ..agent.prompts import build_system_prompt
from ..core.events import (
    ContextCompressed,
    ErrorOccurred,
    EventBus,
    LLMStreamDelta,
    LLMThinkingDelta,
    QueryPhaseChanged,
    RunCancelled,
    RunFinished,
    RunStarted,
    StepStarted,
    SubagentFeedUpdated,
    SubagentFinished,
    SubagentSpawned,
    SubagentStateChanged,
    ToolCallCompleted,
    ToolCallStarted,
    ToolOutputDelta,
    ToolProgress,
    UserSteered,
)
from ..core.messages import Message, sanitize_history
from ..core.redact import SecretRedactor
from ..llm.base import LLMProvider
from ..safety.policy import AllowAllPolicy
from ..session.autocommit import ensure_local_exclude, git_is_repo
from ..tools.base import RunMode, Tool, ToolContext, TurnAbortSignal
from ..tools.registry import ToolRegistry
from .control import AgentControl
from .definition import AgentDefinition, AgentDefinitionManager
from .feed import AgentFeed, AgentFeedPage
from .sidechain import (
    AgentSidechainWriter,
    SidechainInfo,
    SidechainStore,
    read_sidechain,
)

SUBAGENT_SYSTEM_PROMPT = build_system_prompt(
    "You are a Krelcode SUBAGENT working on a delegated subtask.\n"
    "- Focus strictly on the task you were given; do not ask the user questions.\n"
    "- Use tools to do the work and verify results when possible.\n"
    "- Your final text message is the report handed back to the parent agent: "
    "make it dense and self-contained (what was done, key results, file paths, caveats)."
)

PREVIEW_CHARS = 160

STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_KILLED = "killed"

TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_KILLED,
})

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
    worktree: Optional[Path] = None
    branch: Optional[str] = None
    forked: bool = False
    definition: str = ""
    feed: Optional[AgentFeed] = field(default=None, repr=False)
    control: AgentControl = field(default_factory=AgentControl, repr=False)
    sidechain_path: Optional[Path] = None
    sidechain: Optional[AgentSidechainWriter] = field(default=None, repr=False)
    kill_requested: bool = field(default=False, repr=False)

    def preview(self) -> str:
        text = self.result or self.error or ""
        if len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS] + "…"
        return text.replace("\n", " ")


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
        hook_port=None,
        definitions: Optional[AgentDefinitionManager] = None,
        sidechain_store: Optional[SidechainStore] = None,
        feed_max_entries: int = 2_000,
        feed_max_entry_chars: int = 24_000,
    ) -> None:
        self.llm = llm
        self.tool_factory = tool_factory
        self.tool_context = tool_context
        self.policy = policy or AllowAllPolicy()
        self.bus = bus or EventBus()
        self.max_steps = max_steps
        self.context_budget = context_budget
        self.redactor = redactor
        self.system_prompt_provider = system_prompt_provider
        self.hook_port = hook_port
        self.definitions = definitions or AgentDefinitionManager(
            tool_context.workspace_root)
        self.sidechains = sidechain_store or SidechainStore(
            tool_context.workspace_root, redactor=redactor)
        if self.sidechains.redactor is None and redactor is not None:
            self.sidechains.redactor = redactor
        self.feed_max_entries = feed_max_entries
        self.feed_max_entry_chars = feed_max_entry_chars
        self._handles: Dict[str, AgentHandle] = {}
        self._counter = 0
        self._exclude_checked = False
        self.get_parent_history: Optional[Callable[[], List[Message]]] = None
        self._closed = False

    # -- 生命周期 -----------------------------------------------------------------

    async def spawn(
        self,
        task: str,
        isolated: Optional[bool] = None,
        fork_context: Optional[bool] = None,
        *,
        definition: Optional[str] = None,
    ) -> AgentHandle:
        """创建并启动后台子代理。

        definition 指向用户定义文件；未传 isolated/fork_context 时使用定义中
        的默认值。Definition allowlist 仅缩小工具集合，不改变审批与 Sandbox。
        """
        if self._closed:
            raise RuntimeError("SubagentManager 已关闭")
        if self.sidechains.redactor is None and self.redactor is not None:
            self.sidechains.redactor = self.redactor
        if not self._exclude_checked:
            self._exclude_checked = True
            if await git_is_repo(self.tool_context.workspace_root):
                await ensure_local_exclude(
                    self.tool_context.workspace_root, ".krelcode/")
        definition_obj: Optional[AgentDefinition] = None
        if definition:
            try:
                definition_obj = self.definitions.get(definition)
            except KeyError as exc:
                raise ValueError(str(exc)) from exc
        if isolated is None:
            isolated = definition_obj.default_isolated if definition_obj else False
        if fork_context is None:
            fork_context = (
                definition_obj.default_fork_context if definition_obj else False)

        tools = list(self.tool_factory())
        if definition_obj is not None and definition_obj.allowed_tools:
            available = {tool.name for tool in tools}
            missing = sorted(set(definition_obj.allowed_tools) - available)
            if missing:
                raise ValueError(
                    f"Agent Definition {definition_obj.name!r} 引用了未知工具: "
                    + ", ".join(missing))
            allowed = set(definition_obj.allowed_tools)
            tools = [tool for tool in tools if tool.name in allowed]

        self._counter += 1
        agent_id = f"a{self._counter}"
        worktree_path: Optional[Path] = None
        branch: Optional[str] = None
        child_runtime = replace(
            self.tool_context.runtime,
            run_mode=RunMode.SUBAGENT,
            turn_id=None,
            tool_allowlist=(
                frozenset(definition_obj.allowed_tools)
                if definition_obj is not None and definition_obj.allowed_tools
                else self.tool_context.runtime.tool_allowlist
            ),
        )
        tool_context = replace(
            self.tool_context,
            runtime=child_runtime,
            abort=TurnAbortSignal(),
        )
        if isolated:
            worktree_path, branch = await self._create_worktree(agent_id)
            parent_sandbox = getattr(self.tool_context, "sandbox", None)
            tool_context = ToolContext(
                workspace_root=worktree_path,
                sandbox=(parent_sandbox.rebind(worktree_path)
                         if parent_sandbox is not None else None),
                sandbox_enabled=parent_sandbox is not None,
                runtime=child_runtime,
                services=self.tool_context.services,
                shell_jobs=getattr(self.tool_context, "shell_jobs", None),
            )

        control = AgentControl()
        child_bus = EventBus()
        child_hook_port = self.hook_port
        if child_hook_port is not None and hasattr(child_hook_port, "rebind"):
            child_hook_port = child_hook_port.rebind(tool_context.workspace_root)

        base_prompt = (
            self.system_prompt_provider()
            if self.system_prompt_provider is not None
            else SUBAGENT_SYSTEM_PROMPT
        )
        system_prompt = (
            definition_obj.system_prompt_with_base(base_prompt)
            if definition_obj is not None else base_prompt
        )
        loop = AgentLoop(
            llm=self.llm,
            registry=ToolRegistry(tools),
            tool_context=tool_context,
            policy=self.policy,
            bus=child_bus,
            system_prompt=system_prompt,
            max_steps=self.max_steps,
            context_manager=(ContextManager(self.llm, max_tokens=self.context_budget)
                             if self.context_budget else None),
            redactor=self.redactor,
            hook_port=child_hook_port,
            step_gate=control.wait_if_paused,
        )

        effective_task = task
        forked = False
        if fork_context and self.get_parent_history is not None:
            forked_history, pending_user = self._fork_parent_history()
            if pending_user is not None:
                effective_task = f"{pending_user}\n\n[委派的子任务] {task}".strip()
            loop.history.extend(forked_history)
            forked = True

        feed = AgentFeed(
            agent_id,
            redactor=self.redactor,
            max_entries=self.feed_max_entries,
            max_entry_chars=self.feed_max_entry_chars,
        )
        sidechain = self.sidechains.create(agent_id, {
            "task": task,
            "definition": definition_obj.name if definition_obj else "",
            "definition_source": definition_obj.source if definition_obj else "",
            "definition_path": str(definition_obj.path) if definition_obj and definition_obj.path else "",
            "allowed_tools": list(definition_obj.allowed_tools) if definition_obj else [],
            "isolated": bool(isolated),
            "fork_context": bool(fork_context),
            "worktree": str(worktree_path) if worktree_path else "",
            "branch": branch or "",
        })
        handle = AgentHandle(
            id=agent_id,
            task=task,
            loop=loop,
            worktree=worktree_path,
            branch=branch,
            forked=forked,
            definition=definition_obj.name if definition_obj else "",
            feed=feed,
            control=control,
            sidechain_path=sidechain.path,
            sidechain=sidechain,
        )
        self._handles[agent_id] = handle
        child_bus.subscribe(
            lambda event, current=handle: self._on_child_event(current, event))
        sidechain.append_message("user", effective_task, source="initial_task")
        sidechain.append_state(STATUS_RUNNING, reason="spawn")
        handle.task_obj = asyncio.create_task(self._run(handle, effective_task))
        await self.bus.emit(SubagentSpawned(
            agent_id, task, definition=handle.definition))
        return handle

    def _fork_parent_history(self) -> Tuple[List[Message], Optional[str]]:
        source = self.get_parent_history() or []
        start = 1 if source and source[0].role == "system" else 0
        forked = [
            replace(m, tool_calls=list(m.tool_calls) if m.tool_calls else None)
            for m in source[start:]
        ]
        sanitize_history(forked)
        pending_user: Optional[str] = None
        if forked and forked[-1].role == "user":
            pending_user = forked.pop().content or ""
        return forked, pending_user

    # -- child feed ---------------------------------------------------------------

    async def _on_child_event(self, handle: AgentHandle, event) -> None:
        if handle.feed is None:
            return
        now = time.time()
        if isinstance(event, (ToolCallCompleted, RunFinished, RunCancelled,
                              ErrorOccurred)):
            await self._publish_feed_entries(handle, handle.feed.flush_all(ts=now))
        mapped = _feed_event(event)
        if mapped is None:
            return
        kind, text, data = mapped
        entry = handle.feed.add(kind, text, ts=now, data=data)
        if entry is not None:
            await self._publish_feed_entries(handle, (entry,))

    async def _publish_feed_entries(self, handle: AgentHandle, entries) -> None:
        for entry in entries:
            if handle.sidechain is not None:
                handle.sidechain.append_feed(entry)
            await self.bus.emit(SubagentFeedUpdated(
                handle.id, entry.sequence, entry.kind, entry.text, entry.data))

    # -- git worktree 隔离 --------------------------------------------------------

    async def _create_worktree(self, agent_id: str) -> Tuple[Path, str]:
        ws = self.tool_context.workspace_root
        if not await git_is_repo(ws):
            raise ValueError(
                "隔离子代理要求工作区是 git 仓库（先 git init，或不用 isolated）")
        await ensure_local_exclude(ws, ".krelcode/")
        branch = f"krelcode/agent-{agent_id}"
        path = ws / ".krelcode" / "worktrees" / agent_id
        path.parent.mkdir(parents=True, exist_ok=True)
        out = await _git(ws, "worktree", "add", str(path), "-b", branch)
        if out is None:
            raise ValueError(f"git worktree 创建失败（分支 {branch} 可能已存在）")
        return path, branch

    async def _finalize_isolated(self, handle: AgentHandle) -> None:
        wt = handle.worktree
        if wt is None or not wt.is_dir():
            return
        status = await _git(wt, "status", "--porcelain")
        if not status:
            return
        await _git(wt, "add", "-A")
        label = handle.task.strip().replace("\n", " ")[:60]
        await _git(wt,
                   "-c", "user.name=krelcode",
                   "-c", "user.email=agent@krelcode.local",
                   "commit", "-m", f"krelcode({handle.id}): {label}")

    async def _remove_worktree(self, handle: AgentHandle) -> None:
        if handle.worktree is None:
            return
        ws = self.tool_context.workspace_root
        await _git(ws, "worktree", "remove", "--force", str(handle.worktree))
        await _git(ws, "worktree", "prune")

    # -- 运行状态机 ---------------------------------------------------------------

    async def _set_state(self, handle: AgentHandle, status: str, reason: str = "") -> None:
        if handle.status == status:
            return
        handle.status = status
        if handle.sidechain is not None:
            handle.sidechain.append_state(
                status, reason=reason, turns=handle.turns,
                result=handle.result, error=handle.error)
        await self.bus.emit(SubagentStateChanged(handle.id, status, reason))

    async def _emit_finished(self, handle: AgentHandle) -> None:
        if handle.sidechain is not None:
            handle.sidechain.append_history(handle.loop.history, turn=handle.turns)
        await self.bus.emit(SubagentFinished(
            handle.id, handle.status, handle.preview()))

    async def _run(self, handle: AgentHandle, message: str) -> None:
        """执行一条消息；队列非空时在同一 Task 内继续排空。"""
        while True:
            handle.result = None
            handle.error = None
            await self._set_state(handle, STATUS_RUNNING, "执行任务")
            try:
                result = await handle.loop.run(message)
                handle.result = result
                handle.turns += 1
                await self._set_state(handle, STATUS_COMPLETED, "本轮完成")
            except asyncio.CancelledError:
                killed = handle.kill_requested
                handle.status = STATUS_KILLED if killed else STATUS_CANCELLED
                handle.error = "用户终止子代理" if killed else "任务被打断/取消"
                if handle.sidechain is not None:
                    handle.sidechain.append_state(
                        handle.status, reason=handle.error, turns=handle.turns,
                        error=handle.error)
                    handle.sidechain.append_history(
                        handle.loop.history, turn=handle.turns)
                await self.bus.emit(SubagentStateChanged(
                    handle.id, handle.status, handle.error))
                await self.bus.emit(SubagentFinished(
                    handle.id, handle.status, handle.preview()))
                raise
            except Exception as exc:
                handle.error = f"{type(exc).__name__}: {exc}"
                handle.turns += 1
                await self._set_state(handle, STATUS_FAILED, handle.error)
            await self._emit_finished(handle)

            if handle.queue:
                message = handle.queue.pop(0)
                if handle.sidechain is not None:
                    handle.sidechain.append_message(
                        "user", message, source="queue")
                continue
            if handle.worktree is not None:
                await self._finalize_isolated(handle)
            return

    def get(self, agent_id: str) -> Optional[AgentHandle]:
        return self._handles.get(agent_id)

    def is_running(self, handle: AgentHandle) -> bool:
        return handle.task_obj is not None and not handle.task_obj.done()

    # -- 交互：send / pause / resume / kill --------------------------------------

    async def send_input(
        self, agent_id: str, message: str, interrupt: bool = False
    ) -> Tuple[AgentHandle, str]:
        handle = self._require_handle(agent_id)
        if handle.status == STATUS_KILLED:
            raise RuntimeError(f"子代理 {agent_id} 已被终止，不能追加任务")

        if not self.is_running(handle):
            handle.kill_requested = False
            handle.task = message
            if handle.sidechain is not None:
                handle.sidechain.append_message("user", message, source="input")
            handle.task_obj = asyncio.create_task(self._run(handle, message))
            return handle, "started"

        if not interrupt:
            handle.queue.append(message)
            if handle.sidechain is not None:
                handle.sidechain.append_message("user", message, source="queued")
            return handle, "queued"

        if handle.control.paused:
            handle.control.resume()
        handle.task_obj.cancel()
        await asyncio.gather(handle.task_obj, return_exceptions=True)
        sanitize_history(handle.loop.history)
        if handle.loop.history and handle.loop.history[-1].role == "user":
            first = ((handle.loop.history[-1].content or "")
                     + INTERRUPT_MARKER + message)
            handle.loop.history.pop()
        else:
            first = message
        handle.kill_requested = False
        handle.task = message
        if handle.sidechain is not None:
            handle.sidechain.append_message("user", message, source="interrupt")
        handle.task_obj = asyncio.create_task(self._run(handle, first))
        return handle, "interrupted"

    async def pause(self, agent_id: str) -> AgentHandle:
        handle = self._require_handle(agent_id)
        if not self.is_running(handle):
            raise ValueError(f"子代理 {agent_id} 当前未运行，无法暂停")
        if handle.control.pause():
            await self._set_state(handle, STATUS_PAUSED, "用户暂停")
        return handle

    async def resume(self, agent_id: str) -> AgentHandle:
        handle = self._require_handle(agent_id)
        if not handle.control.paused:
            raise ValueError(f"子代理 {agent_id} 当前未暂停")
        handle.control.resume()
        await self._set_state(handle, STATUS_RUNNING, "用户恢复")
        return handle

    async def kill(self, agent_id: str) -> AgentHandle:
        handle = self._require_handle(agent_id)
        if not self.is_running(handle):
            raise ValueError(f"子代理 {agent_id} 当前未运行，无法终止")
        handle.kill_requested = True
        handle.control.resume()
        await self._set_state(handle, STATUS_KILLED, "用户终止")
        assert handle.task_obj is not None
        handle.task_obj.cancel()
        await asyncio.gather(handle.task_obj, return_exceptions=True)
        if handle.worktree is not None:
            await self._finalize_isolated(handle)
            await self._remove_worktree(handle)
        return handle

    # -- 等待 ---------------------------------------------------------------------

    async def wait(self, agent_id: str, timeout: Optional[float] = None) -> AgentHandle:
        handle = self._require_handle(agent_id)
        await self._wait_handles([handle], timeout)
        return handle

    async def wait_many(
        self, agent_ids: List[str], timeout: Optional[float] = None
    ) -> List[AgentHandle]:
        handles = [self._require_handle(agent_id) for agent_id in agent_ids]
        await self._wait_handles(handles, timeout)
        return handles

    @staticmethod
    async def _wait_handles(handles: List[AgentHandle], timeout: Optional[float]) -> None:
        tasks = [h.task_obj for h in handles
                 if h.task_obj is not None and not h.task_obj.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    async def shutdown(self) -> None:
        """取消所有运行/暂停子代理，存档 worktree 并关闭 sidechain。"""
        if self._closed:
            return
        self._closed = True
        active = [h for h in self._handles.values() if self.is_running(h)]
        for handle in active:
            assert handle.task_obj is not None
            handle.task_obj.cancel()
        if active:
            await asyncio.gather(
                *(h.task_obj for h in active), return_exceptions=True)
        for handle in self._handles.values():
            if handle.worktree is not None:
                await self._finalize_isolated(handle)
                await self._remove_worktree(handle)
            if handle.sidechain is not None:
                handle.sidechain.append_state(
                    handle.status, reason="manager_shutdown",
                    turns=handle.turns, result=handle.result, error=handle.error)
                handle.sidechain.append_history(
                    handle.loop.history, turn=handle.turns)
                handle.sidechain.close()

    # -- Feed / Definition / Sidechain --------------------------------------------

    def feed(
        self, agent_id: str, after_sequence: int = 0, limit: int = 200,
    ) -> AgentFeedPage:
        handle = self._require_handle(agent_id)
        if handle.feed is None:
            raise ValueError(f"子代理 {agent_id} 没有 feed")
        return handle.feed.page(after_sequence, limit)

    def definition_lines(self) -> List[str]:
        lines = list(self.definitions.status_lines())
        lines.extend(
            f"! 跳过无效定义 {issue.path}: {issue.message}"
            for issue in self.definitions.issues)
        return lines

    def sidechain_lines(self, limit: int = 50) -> List[str]:
        return [
            f"{info.agent_id} [{info.status or '-'}] run={info.run_id} "
            f"records={info.records} · {info.task[:60]} · {info.path}"
            for info in self.sidechains.list(limit=limit)
        ]

    def read_sidechain(self, value: str) -> Tuple[Path, Tuple[dict, ...]]:
        path = self.sidechains.resolve(value)
        if path is None:
            raise ValueError(f"未找到 sidechain: {value}")
        return path, read_sidechain(path)

    # -- 观测 ---------------------------------------------------------------------

    def _require_handle(self, agent_id: str) -> AgentHandle:
        handle = self._handles.get(agent_id)
        if handle is None:
            raise KeyError(f"未知子代理: {agent_id}")
        return handle

    def status_lines(self) -> List[str]:
        lines = []
        for handle in self._handles.values():
            task_preview = handle.task
            if len(task_preview) > 60:
                task_preview = task_preview[:60] + "…"
            queued = f" · 排队 {len(handle.queue)}" if handle.queue else ""
            branch = f" · 分支 {handle.branch}" if handle.branch else ""
            branch += " · 继承上下文" if handle.forked else ""
            definition = f" · 定义 {handle.definition}" if handle.definition else ""
            sequence = f" · feed {handle.feed.latest_sequence}" if handle.feed else ""
            line = (f"{handle.id} [{handle.status}] {handle.turns} 轮{queued}{branch}"
                    f"{definition}{sequence} · 任务: {task_preview.replace(chr(10), ' ')}")
            if handle.status in TERMINAL_STATUSES:
                line += f" · 结果: {handle.preview()}"
            lines.append(line)
        return lines

    def build_tools(self) -> List[Tool]:
        from .tools import build_subagent_tools
        return build_subagent_tools(self)


def _feed_event(event) -> Optional[Tuple[str, str, Dict[str, object]]]:
    if isinstance(event, RunStarted):
        return "user_message", event.user_input, {
            "turn_id": event.turn_id, "session_id": event.session_id}
    if isinstance(event, StepStarted):
        return "step_started", f"step {event.step}", {"step": event.step}
    if isinstance(event, QueryPhaseChanged):
        return "phase", event.detail or event.phase, {"phase": event.phase}
    if isinstance(event, LLMStreamDelta):
        return "assistant_delta", event.text, {}
    if isinstance(event, LLMThinkingDelta):
        return "thinking_delta", event.text, {}
    if isinstance(event, ToolCallStarted):
        args = json.dumps(event.tool_call.arguments, ensure_ascii=False, sort_keys=True)
        return "tool_started", f"{event.tool_call.name} {args}", {
            "tool_call_id": event.tool_call.id,
            "tool_name": event.tool_call.name,
            "arguments": event.tool_call.arguments,
        }
    if isinstance(event, ToolProgress):
        return "tool_progress", event.message, {
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            **dict(event.data),
        }
    if isinstance(event, ToolOutputDelta):
        return "tool_output", event.text, {
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "stream": event.stream,
        }
    if isinstance(event, ToolCallCompleted):
        return "tool_completed", event.content, {
            "tool_call_id": event.tool_call.id,
            "tool_name": event.tool_call.name,
            "is_error": event.is_error,
            **dict(event.data),
        }
    if isinstance(event, RunFinished):
        return "run_finished", event.final_text, {"turn_id": event.turn_id}
    if isinstance(event, RunCancelled):
        return "run_cancelled", event.reason, {"turn_id": event.turn_id}
    if isinstance(event, UserSteered):
        return "user_steered", event.text, {}
    if isinstance(event, ContextCompressed):
        return "context_compressed", "history compressed", {
            "before_tokens": event.before_tokens,
            "after_tokens": event.after_tokens,
        }
    if isinstance(event, ErrorOccurred):
        return "error", event.error, {"context": event.context}
    return None
