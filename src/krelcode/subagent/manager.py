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
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ..agent.compression import ContextManager
from ..agent.context import _git
from ..agent.fragments import (
    PromptFragment,
    PromptFragmentRegistry,
    apply_prompt_snapshot,
)
from ..agent.loop import DEFAULT_MAX_STEPS, AgentLoop
from ..agent.tool_pool import normalize_tool_allowlist
from ..agent.prompts import (
    PROMPT_PROFILE_AUTO,
    build_system_prompt,
    resolve_prompt_profile,
)
from ..agent.working_context import WorkingContextStore
from ..core.events import (
    ContextAssembled,
    ContextCompressed,
    ContextCheckpointCreated,
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
from ..safety.contract import narrow_permission_mode
from ..safety.policy import AllowAllPolicy
from ..session.autocommit import ensure_local_exclude, git_is_repo
from ..session.context_checkpoint import (
    ContextCheckpointCoordinator,
    ContextCheckpointStore,
)
from ..tools.base import PermissionMode, RunMode, Tool, ToolContext, TurnAbortSignal
from ..tools.registry import ToolRegistry
from .control import AgentControl
from .definition import AgentDefinition, AgentDefinitionManager
from .feed import AgentFeed, AgentFeedPage
from .instructions import build_subagent_instructions, inherited_memory_fragment
from .sidechain import (
    AgentSidechainWriter,
    SidechainStore,
    read_sidechain,
)

SUBAGENT_ROLE_PROMPT = """\
You are a Krelcode SUBAGENT working on a delegated subtask.
- Focus strictly on the task you were given; do not ask the user questions.
- Use tools to do the work and verify results when possible.
- Your final text message is the report handed back to the parent agent: make it
  dense and self-contained (what was done, key results, file paths, caveats)."""


def build_subagent_system_prompt(
    *,
    profile: str = PROMPT_PROFILE_AUTO,
    provider: object = "",
    model: object = "",
) -> str:
    return build_system_prompt(
        SUBAGENT_ROLE_PROMPT,
        profile=profile,
        provider=provider,
        model=model,
    )


SUBAGENT_SYSTEM_PROMPT = build_subagent_system_prompt()

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
    context_checkpoint_id: str = ""
    inherited_memory_ids: Tuple[str, ...] = field(default_factory=tuple)
    instruction_layers: Tuple[str, ...] = field(default_factory=tuple)
    model: str = ""
    provider: str = ""
    prompt_profile: str = ""
    permission_mode: str = ""
    mcp_servers: Tuple[str, ...] = field(default_factory=tuple)
    kill_requested: bool = field(default=False, repr=False)
    cache_ledger_hash: str = ""

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
        memory_store=None,
        llm_resolver: Optional[Callable[[str], LLMProvider]] = None,
        mcp_servers_provider: Optional[Callable[[], Iterable[str]]] = None,
        base_prompt_provider: Optional[Callable[[], str]] = None,
        base_prompt_for_llm: Optional[Callable[[LLMProvider], str]] = None,
        prompt_profile: str = PROMPT_PROFILE_AUTO,
        catalog_fragments_provider: Optional[
            Callable[[], Iterable[PromptFragment]]
        ] = None,
        cache_ledger_provider: Optional[Callable[[], object | None]] = None,
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
        self.base_prompt_provider = base_prompt_provider
        self.base_prompt_for_llm = base_prompt_for_llm
        self.prompt_profile = prompt_profile
        self.catalog_fragments_provider = catalog_fragments_provider
        self.hook_port = hook_port
        self.definitions = definitions or AgentDefinitionManager(
            tool_context.workspace_root)
        self.sidechains = sidechain_store or SidechainStore(
            tool_context.workspace_root, redactor=redactor)
        if self.sidechains.redactor is None and redactor is not None:
            self.sidechains.redactor = redactor
        self.feed_max_entries = feed_max_entries
        self.feed_max_entry_chars = feed_max_entry_chars
        self.memory_store = memory_store
        self.llm_resolver = llm_resolver
        self.mcp_servers_provider = mcp_servers_provider
        self._handles: Dict[str, AgentHandle] = {}
        self._counter = 0
        self._exclude_checked = False
        self.get_parent_history: Optional[Callable[[], List[Message]]] = None
        self.cache_ledger_provider = cache_ledger_provider
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
        的默认值。Definition 的 permission/tool/MCP scope 只能相对父级收窄，
        审批与 Sandbox 继续继承父级。
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

        child_llm = self.llm
        if definition_obj is not None and definition_obj.model:
            if self.llm_resolver is None:
                raise ValueError(
                    f"Agent Definition {definition_obj.name!r} 配置了独立 model，"
                    "但当前运行环境没有 provider resolver")
            child_llm = self.llm_resolver(definition_obj.model)

        tools = list(self.tool_factory())
        selected_mcp: Optional[set[str]] = None
        if definition_obj is not None and definition_obj.mcp_servers is not None:
            available_mcp = set(self.mcp_servers_provider() if
                                self.mcp_servers_provider is not None else ())
            available_mcp.update(
                item for item in (_mcp_server_name(tool) for tool in tools) if item)
            requested = set(definition_obj.mcp_servers)
            if "*" not in requested:
                missing = sorted(requested - available_mcp)
                if missing:
                    raise ValueError(
                        f"Agent Definition {definition_obj.name!r} 引用了未知 MCP 服务器: "
                        + ", ".join(missing))
                selected_mcp = requested
            elif requested == {"*"}:
                selected_mcp = available_mcp
            tools = _restrict_mcp_tools(tools, selected_mcp)
        memory_entries = self._relevant_memory(task)
        memory_ids = tuple(str(entry.id) for entry in memory_entries)
        tools = self._restrict_memory_tools(tools, memory_ids)
        if definition_obj is not None and definition_obj.allowed_tools:
            available = {tool.name for tool in tools}
            allowed = normalize_tool_allowlist(definition_obj.allowed_tools)
            missing = sorted(allowed - available)
            if missing:
                raise ValueError(
                    f"Agent Definition {definition_obj.name!r} 引用了未知工具: "
                    + ", ".join(missing))
            # ToolSearch is a control-plane loader. Keep it available so an
            # allowed discoverable/deferred tool can still be loaded, while the
            # runtime allowlist prevents it from exposing anything else.
            tools = [
                tool for tool in tools
                if tool.name in allowed or tool.name == "tool_search"
            ]

        self._counter += 1
        agent_id = f"a{self._counter}"
        parent_runtime = self.tool_context.runtime
        requested_permission = (
            PermissionMode(definition_obj.permission_mode)
            if definition_obj is not None and definition_obj.permission_mode
            else parent_runtime.permission_mode
        )
        permission_mode = narrow_permission_mode(
            parent_runtime.permission_mode, requested_permission)
        effective_mcp_allowlist = (
            frozenset(selected_mcp)
            if selected_mcp is not None
            else parent_runtime.mcp_allowlist
        )
        if (
            selected_mcp is not None
            and parent_runtime.mcp_allowlist is not None
        ):
            effective_mcp_allowlist = (
                effective_mcp_allowlist & parent_runtime.mcp_allowlist)
            tools = _restrict_mcp_tools(tools, effective_mcp_allowlist)
        requested_tool_allowlist = (
            frozenset(normalize_tool_allowlist(definition_obj.allowed_tools))
            if definition_obj is not None and definition_obj.allowed_tools
            else parent_runtime.tool_allowlist
        )
        if (
            definition_obj is not None
            and definition_obj.allowed_tools
            and parent_runtime.tool_allowlist is not None
        ):
            requested_tool_allowlist = (
                requested_tool_allowlist
                & frozenset(normalize_tool_allowlist(
                    parent_runtime.tool_allowlist)))
        mcp_servers = tuple(sorted(effective_mcp_allowlist or ()))
        worktree_path: Optional[Path] = None
        branch: Optional[str] = None
        child_runtime = replace(
            parent_runtime,
            run_mode=RunMode.SUBAGENT,
            turn_id=None,
            session_id=f"subagent-{agent_id}",
            tool_allowlist=requested_tool_allowlist,
            permission_mode=permission_mode,
            alive_mcp_servers=(
                effective_mcp_allowlist
                if selected_mcp is not None
                else parent_runtime.alive_mcp_servers
            ),
            mcp_health_known=(
                True if selected_mcp is not None
                else parent_runtime.mcp_health_known
            ),
            mcp_allowlist=effective_mcp_allowlist,
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

        prompt_resolution = resolve_prompt_profile(
            child_llm,
            getattr(child_llm, "model", ""),
            configured=self.prompt_profile,
        )
        if self.base_prompt_for_llm is not None:
            base_prompt = self.base_prompt_for_llm(child_llm)
        else:
            prompt_provider = (
                self.base_prompt_provider or self.system_prompt_provider)
            base_prompt = (
                prompt_provider()
                if prompt_provider is not None
                else build_subagent_system_prompt(
                    profile=self.prompt_profile,
                    provider=child_llm,
                    model=getattr(child_llm, "model", ""),
                )
            )
        instruction_bundle = build_subagent_instructions(
            tool_context.workspace_root,
            task,
            base_prompt=base_prompt,
            definition=definition_obj,
            redactor=self.redactor,
            base_metadata={
                "prompt_profile": prompt_resolution.name,
                "prompt_profile_requested": prompt_resolution.requested,
                "prompt_profile_match": prompt_resolution.matched_by,
                "prompt_provider": prompt_resolution.provider,
                "prompt_model": prompt_resolution.model,
                "base_prompt_hash": prompt_resolution.base_prompt_hash,
            },
        )
        prompt_fragments = list(instruction_bundle.fragments())
        if self.catalog_fragments_provider is not None:
            prompt_fragments.extend(self.catalog_fragments_provider())
        memory_fragment = inherited_memory_fragment(memory_entries)
        if memory_fragment is not None:
            prompt_fragments.append(memory_fragment)
        prompt_registry = PromptFragmentRegistry(prompt_fragments)
        prompt_snapshot = prompt_registry.snapshot()
        own_working_context = WorkingContextStore(
            tool_context.workspace_root, redactor=self.redactor)
        loop = AgentLoop(
            llm=child_llm,
            registry=ToolRegistry(tools),
            tool_context=tool_context,
            policy=self.policy,
            bus=child_bus,
            max_steps=self.max_steps,
            context_manager=(ContextManager(child_llm, max_tokens=self.context_budget)
                             if self.context_budget else None),
            redactor=self.redactor,
            hook_port=child_hook_port,
            step_gate=control.wait_if_paused,
            working_context_store=own_working_context,
        )
        loop.prompt_registry = prompt_registry
        loop.prompt_profile = prompt_resolution.profile
        loop.prompt_profile_resolution = prompt_resolution
        loop.prompt_profile_name = prompt_resolution.name
        loop.subagent_definition_name = (
            definition_obj.name if definition_obj is not None else "")
        apply_prompt_snapshot(loop, prompt_snapshot)

        effective_task = task
        forked = bool(fork_context)
        loop.subagent_instruction_layers = instruction_bundle.layers
        loop.inherited_memory_ids = memory_ids
        loop.parent_history_inherited = False
        parent_ledger = (
            self.cache_ledger_provider()
            if self.cache_ledger_provider is not None else None
        )
        parent_policy_hash = str(getattr(
            parent_ledger, "policy_instruction_hash", "") or "")
        loop.cache_ledger_owner = f"subagent:{agent_id}"
        loop.parent_policy_instruction_hash = parent_policy_hash

        context_store_directory = (
            tool_context.workspace_root / ".krelcode" / "context-checkpoints"
            / "subagents" / agent_id
        )
        child_context_store = ContextCheckpointStore(
            tool_context.workspace_root,
            redactor=self.redactor,
            directory=context_store_directory,
        )
        loop.context_checkpoint_store = child_context_store
        child_context_coordinator = ContextCheckpointCoordinator(
            loop=loop,
            store=child_context_store,
            compact_store=None,
            memory_store=self.memory_store,
            capture_start=False,
            capture_workspace=False,
        )
        child_context_coordinator.attach(child_bus)
        loop.context_checkpoint_coordinator = child_context_coordinator

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
            "model": definition_obj.model if definition_obj else "",
            "resolved_model": str(getattr(child_llm, "model", "") or ""),
            "provider": str(getattr(child_llm, "name", "") or ""),
            "prompt_profile": prompt_resolution.name,
            "permission_mode": permission_mode.value,
            "mcp_servers": list(mcp_servers),
            "isolated": bool(isolated),
            "fork_context": bool(fork_context),
            "worktree": str(worktree_path) if worktree_path else "",
            "branch": branch or "",
            "parent_history_inherited": False,
            "inherited_memory_ids": list(memory_ids),
            "instruction_layers": instruction_bundle.metadata(),
            "parent_policy_instruction_hash": parent_policy_hash,
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
            inherited_memory_ids=memory_ids,
            instruction_layers=tuple(
                layer.kind for layer in instruction_bundle.layers),
            model=str(getattr(child_llm, "model", "") or ""),
            provider=str(getattr(child_llm, "name", "") or ""),
            prompt_profile=prompt_resolution.name,
            permission_mode=permission_mode.value,
            mcp_servers=mcp_servers,
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

    def _relevant_memory(self, task: str):
        if self.memory_store is None:
            return ()
        parent_tail = ""
        if self.get_parent_history is not None:
            for message in reversed(self.get_parent_history() or ()):
                if message.role != "user" or not message.content:
                    continue
                text = " ".join(str(message.content).split())
                if text.startswith("[上下文摘要]") or text.startswith("[用户插话]"):
                    continue
                parent_tail = text[:2_000]
                break
        query = "\n".join(item for item in (parent_tail, task) if item)
        return self.memory_store.search(query, limit=8) if query else ()

    @staticmethod
    def _restrict_memory_tools(tools: List[Tool], memory_ids: Tuple[str, ...]) -> List[Tool]:
        restricted = []
        allowed = frozenset(memory_ids)
        for tool in tools:
            if tool.name != "memory_search":
                restricted.append(tool)
                continue
            factory = getattr(tool, "restricted_to", None)
            if callable(factory):
                restricted.append(factory(allowed))
            elif allowed:
                restricted.append(tool)
        return restricted

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
        if isinstance(event, ContextAssembled):
            ledger = getattr(handle.loop, "last_context_cache_ledger", None)
            if (
                ledger is not None
                and ledger.complete_request_hash
                and ledger.complete_request_hash != handle.cache_ledger_hash
            ):
                handle.cache_ledger_hash = ledger.complete_request_hash
                if handle.sidechain is not None:
                    handle.sidechain.write({
                        "type": "context_cache_ledger",
                        "ledger": ledger.as_dict(),
                    })
        if isinstance(event, ContextCheckpointCreated):
            handle.context_checkpoint_id = event.checkpoint_id
            if handle.sidechain is not None:
                handle.sidechain.write({
                    "type": "context_checkpoint",
                    "checkpoint_id": event.checkpoint_id,
                    "turn_id": event.turn_id,
                    "phase": event.phase,
                    "history_hash": event.history_hash,
                })
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
            branch += " · 继承指令/Memory（非历史）" if handle.forked else ""
            definition = f" · 定义 {handle.definition}" if handle.definition else ""
            memory = (
                f" · Memory {len(handle.inherited_memory_ids)}"
                if handle.inherited_memory_ids else "")
            sequence = f" · feed {handle.feed.latest_sequence}" if handle.feed else ""
            runtime_parts = []
            if handle.model:
                runtime_parts.append(f"model={handle.model}")
            if handle.permission_mode:
                runtime_parts.append(f"permission={handle.permission_mode}")
            if handle.mcp_servers:
                runtime_parts.append(f"mcp={','.join(handle.mcp_servers)}")
            runtime = " · " + " · ".join(runtime_parts) if runtime_parts else ""
            line = (f"{handle.id} [{handle.status}] {handle.turns} 轮{queued}{branch}"
                    f"{definition}{runtime}{memory}{sequence} · 任务: "
                    f"{task_preview.replace(chr(10), ' ')}")
            if handle.status in TERMINAL_STATUSES:
                line += f" · 结果: {handle.preview()}"
            lines.append(line)
        return lines

    def build_tools(self) -> List[Tool]:
        from .tools import build_subagent_tools
        return build_subagent_tools(self)


def _mcp_server_name(tool: Tool) -> str:
    client = getattr(tool, "client", None)
    return str(getattr(client, "name", "") or "")


def _restrict_mcp_tools(tools: List[Tool], allowed: set[str]) -> List[Tool]:
    restricted: List[Tool] = []
    for tool in tools:
        server = _mcp_server_name(tool)
        if server and server not in allowed:
            continue
        restricted.append(tool)
    return restricted


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
            "strategy": event.strategy,
            "summary_source": event.summary_source,
            "generation": event.generation,
            "high_watermark_tokens": event.high_watermark_tokens,
            "low_watermark_tokens": event.low_watermark_tokens,
            "source_savings": dict(event.source_savings),
            "checkpoint_id": event.checkpoint_id,
        }
    if isinstance(event, ErrorOccurred):
        return "error", event.error, {"context": event.context}
    return None
