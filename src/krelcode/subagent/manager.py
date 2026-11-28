"""子代理生命周期、交互式控制、实时 feed 与 sidechain 持久化。

每个子代理拥有独立 AgentLoop、history、ToolRegistry 和 child EventBus，共享
父级 provider、工作区与审批策略。Pause 是协作式的 step 边界暂停，Kill 才
取消 Task；Feed 使用稳定 sequence 供 UI/SDK 游标读取，sidechain 独立落盘。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import (
    Callable,
    cast,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

from ..agent.context.compression import ContextManager
from ..agent.prompt.fragments import (
    PromptFragment,
    PromptFragmentRegistry,
    apply_prompt_snapshot,
)
from ..agent.turn.loop import DEFAULT_MAX_STEPS, AgentLoop
from ..agent.turn.runtime_state import InstructionLayerPort
from ..agent.turn.tool_pool import normalize_tool_allowlist
from ..agent.prompt.prompt_release import ModelProfileCalibration
from ..agent.prompts import PROMPT_PROFILE_AUTO, resolve_prompt_profile
from ..agent.context.working_context import WorkingContextStore
from ..core.events import (
    AgentEvent,
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
from ..core.hooks import HookPort
from ..core.messages import Message, sanitize_history
from ..core.redact import SecretRedactor
from ..llm.base import LLMProvider
from ..safety.contract import narrow_permission_mode
from ..safety.policy import AllowAllPolicy, ApprovalPolicy
from ..agent.context.context_checkpoint import (
    ContextCheckpointCoordinator,
    ContextCheckpointStore,
)
from ..contracts.tool import PermissionMode, RunMode, Tool, ToolContext, TurnAbortSignal
from ..contracts.application import (
    ApplicationRequest,
    ApplicationRequestFactory,
    TaskShape,
)
from ..contracts.agent import AgentContractFactory
from ..contracts.artifact import ArtifactStorePort
from ..contracts.delegation import (
    DelegationContract,
    DelegationKind,
    DelegationParentContext,
    DelegationRelation,
    DelegationResult,
    DelegationStatus,
    HandoffContract,
)
from ..contracts.runtime import (
    HarnessReleaseIdentity,
    RuntimeIdentity,
    RuntimeSandboxBinding,
)
from ..contracts.service_ports import PromptFragmentRegistryPort
from ..tools.registry import ToolRegistry
from .builtin_agents import DEFAULT_BUILTIN_SUBAGENT_NAME
from .authority import build_subagent_authority, paths_overlap
from .control import AgentControl
from .definition import AgentDefinition, AgentDefinitionManager
from .feed import AgentFeed, AgentFeedEntry
from .feed_projection import project_feed_event as _feed_event
from .instructions import build_subagent_instructions, inherited_memory_fragment
from .models import (
    INTERRUPT_MARKER,
    SUBAGENT_ROLE_PROMPT,
    SUBAGENT_SYSTEM_PROMPT,
    AgentHandle,
    build_subagent_system_prompt,
)
from .execution import run_handle
from .delegation_runtime import (
    build_delegation_contract,
    prepare_delegation_runtime,
    render_delegation_task,
)
from .manager_reads import MemoryStorePort, SubagentReadMixin
from .manager_control import SubagentControlMixin
from .sidechain import SidechainStore
from .tool_scope import (
    mcp_server_name,
    restrict_mcp_tools,
    restrict_memory_tools,
)
from .contracts import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_KILLED,
    STATUS_PAUSED,
    STATUS_RUNNING,
)
from .worktrees import SubagentWorktreeManager
from .workspace_version import capture_workspace_version


class SubagentManager(SubagentReadMixin, SubagentControlMixin):
    def __init__(
        self,
        llm: LLMProvider,
        tool_factory: Callable[[], List[Tool]],
        tool_context: ToolContext,
        policy: Optional[ApprovalPolicy] = None,
        bus: Optional[EventBus] = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        context_budget: Optional[int] = None,
        redactor: Optional[SecretRedactor] = None,
        system_prompt_provider: Optional[Callable[[], str]] = None,
        hook_port: Optional[HookPort] = None,
        definitions: Optional[AgentDefinitionManager] = None,
        sidechain_store: Optional[SidechainStore] = None,
        feed_max_entries: int = 2_000,
        feed_max_entry_chars: int = 24_000,
        memory_store: Optional[MemoryStorePort] = None,
        llm_resolver: Optional[Callable[[str], LLMProvider]] = None,
        mcp_servers_provider: Optional[Callable[[], Iterable[str]]] = None,
        base_prompt_provider: Optional[Callable[[], str]] = None,
        base_prompt_for_llm: Optional[Callable[[LLMProvider], str]] = None,
        prompt_profile: str = PROMPT_PROFILE_AUTO,
        prompt_release_id: str = "",
        prompt_calibrations: Sequence[ModelProfileCalibration] = (),
        catalog_fragments_provider: Optional[
            Callable[[], Iterable[PromptFragment]]
        ] = None,
        cache_ledger_provider: Optional[Callable[[], object | None]] = None,
        artifact_store: Optional[ArtifactStorePort] = None,
        max_concurrent: int = 8,
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
        self.prompt_release_id = prompt_release_id
        self.prompt_calibrations = tuple(prompt_calibrations)
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
        self.artifact_store = artifact_store
        self.max_concurrent = max(1, int(max_concurrent))
        self._spawn_slots = asyncio.Semaphore(self.max_concurrent)
        self._handles: Dict[str, AgentHandle] = {}
        self._counter = 0
        self.worktrees = SubagentWorktreeManager(tool_context.workspace_root)
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
        application_request: Optional[ApplicationRequest] = None,
        contract: Optional[DelegationContract] = None,
        parent_context: Optional[DelegationParentContext] = None,
    ) -> AgentHandle:
        """创建并启动后台子代理。

        definition 省略时使用内置 general-purpose；显式名称可指向内置或用户
        定义文件。未传 isolated/fork_context 时使用定义中的默认值。Definition
        的 permission/tool/MCP scope 只能相对父级收窄，审批与 Sandbox 继续
        继承父级。
        """
        if self._closed:
            raise RuntimeError("SubagentManager 已关闭")
        parent_context = parent_context or (
            contract.parent if contract is not None
            else DelegationParentContext(
                parent_session_id=(
                    self.tool_context.runtime.session_id or ""),
                owner="parent",
            )
        )
        if not parent_context.workspace_version.available:
            parent_context = replace(
                parent_context,
                workspace_version=capture_workspace_version(
                    self.tool_context.workspace_root),
            )
        application_request = application_request or (
            ApplicationRequestFactory.from_input_source(
                "subagent",
                goal=task or "[subagent task]",
                task_shape=TaskShape.MULTI_AGENT,
            )
        )
        contract = contract or build_delegation_contract(
            task=task,
            parent=parent_context,
            application_request=application_request,
        )
        if not contract.parent.workspace_version.available:
            contract = replace(
                contract,
                parent=parent_context,
                contract_hash="",
                delegation_id="",
            )
        if self.sidechains.redactor is None and self.redactor is not None:
            self.sidechains.redactor = self.redactor
        await self.worktrees.ensure_workspace_ready()
        definition_name = (
            str(definition or contract.capabilities.definition).strip()
            or DEFAULT_BUILTIN_SUBAGENT_NAME
        )
        try:
            definition_obj: Optional[AgentDefinition] = self.definitions.get(
                definition_name)
        except KeyError as exc:
            raise ValueError(str(exc)) from exc
        if isolated is None:
            isolated = definition_obj.default_isolated if definition_obj else False
        if fork_context is None:
            fork_context = (
                definition_obj.default_fork_context if definition_obj else False)

        child_llm = self.llm
        requested_model = (
            contract.capabilities.model
            or (definition_obj.model if definition_obj is not None else "")
        )
        if requested_model:
            if self.llm_resolver is None:
                raise ValueError(
                    f"DelegationCapabilities 配置了独立 model {requested_model!r}，"
                    "但当前运行环境没有 provider resolver")
            child_llm = self.llm_resolver(requested_model)

        tools = list(self.tool_factory())
        selected_mcp: Optional[set[str]] = None
        requested_mcp_servers = (
            contract.capabilities.mcp_servers
            if contract.capabilities.mcp_servers
            else (
                definition_obj.mcp_servers
                if definition_obj is not None else None
            )
        )
        if requested_mcp_servers is not None:
            available_mcp = set(self.mcp_servers_provider() if
                                self.mcp_servers_provider is not None else ())
            available_mcp.update(
                item for item in (mcp_server_name(tool) for tool in tools) if item)
            requested = set(requested_mcp_servers)
            if "*" not in requested:
                missing = sorted(requested - available_mcp)
                if missing:
                    raise ValueError(
                        "DelegationCapabilities 引用了未知 MCP 服务器: "
                        + ", ".join(missing))
                selected_mcp = requested
            elif requested == {"*"}:
                selected_mcp = available_mcp
            if selected_mcp is not None:
                tools = restrict_mcp_tools(tools, selected_mcp)
        memory_entries = self._relevant_memory(task)
        memory_ids = tuple(str(entry.id) for entry in memory_entries)
        tools = restrict_memory_tools(tools, memory_ids)
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
        if contract.capabilities.allowed_tools:
            contract_allowed = frozenset(normalize_tool_allowlist(
                contract.capabilities.allowed_tools))
            available = {tool.name for tool in tools}
            missing = sorted(contract_allowed - available)
            if missing:
                raise ValueError(
                    "DelegationCapabilities 引用了当前定义不可用或未知工具: "
                    + ", ".join(missing))
            tools = [
                tool for tool in tools
                if tool.name in contract_allowed or tool.name == "tool_search"
            ]

        self._counter += 1
        agent_id = f"a{self._counter}"
        parent_runtime = self.tool_context.runtime
        requested_permission = (
            PermissionMode(contract.capabilities.permission_mode)
            if contract.capabilities.permission_mode
            else PermissionMode(definition_obj.permission_mode)
            if definition_obj is not None and definition_obj.permission_mode
            else parent_runtime.permission_mode
        )
        permission_mode = narrow_permission_mode(
            parent_runtime.permission_mode, requested_permission)
        parent_mcp_allowlist = parent_runtime.mcp_allowlist
        effective_mcp_allowlist: Optional[frozenset[str]]
        if selected_mcp is not None:
            narrowed_mcp_allowlist = frozenset(selected_mcp)
            if parent_mcp_allowlist is not None:
                narrowed_mcp_allowlist &= parent_mcp_allowlist
            effective_mcp_allowlist = narrowed_mcp_allowlist
            tools = restrict_mcp_tools(tools, narrowed_mcp_allowlist)
        else:
            effective_mcp_allowlist = parent_mcp_allowlist
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
            assert requested_tool_allowlist is not None
            requested_tool_allowlist = (
                requested_tool_allowlist
                & frozenset(normalize_tool_allowlist(
                    parent_runtime.tool_allowlist)))
        mcp_servers = tuple(sorted(effective_mcp_allowlist or ()))
        effective_contract, application_request = prepare_delegation_runtime(
            contract,
            parent=parent_context,
            tools=tools,
            mcp_servers=mcp_servers,
            permission_mode=permission_mode.value,
            definition_name=(
                definition_obj.name if definition_obj is not None
                else definition_name),
            model=str(getattr(child_llm, "model", "") or ""),
            application_request=application_request,
        )
        if not isolated and effective_contract.scope.write_paths:
            for active in self._handles.values():
                if not self.is_running(active) or active.delegation_contract is None:
                    continue
                if paths_overlap(
                    effective_contract.scope.write_paths,
                    active.delegation_contract.scope.write_paths,
                ):
                    raise ValueError(
                        "非隔离子代理的写入范围与活动子代理重叠: "
                        f"{active.id}；请启用 isolated=true 或缩小 write_paths")
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
                frozenset(effective_mcp_allowlist or ())
                if selected_mcp is not None else parent_runtime.alive_mcp_servers
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
            skill_allowlist=(
                frozenset(effective_contract.capabilities.skills)
                if effective_contract.capabilities.skills else None
            ),
        )
        if isolated:
            worktree_path, branch = await self.worktrees.create(agent_id)
            parent_sandbox = getattr(self.tool_context, "sandbox", None)
            tool_context = ToolContext(
                workspace_root=worktree_path,
                sandbox=(parent_sandbox.rebind(worktree_path)
                         if parent_sandbox is not None else None),
                sandbox_enabled=parent_sandbox is not None,
                runtime=child_runtime,
                services=self.tool_context.services,
                shell_jobs=getattr(self.tool_context, "shell_jobs", None),
                skill_allowlist=(
                    frozenset(effective_contract.capabilities.skills)
                    if effective_contract.capabilities.skills else None
                ),
            )
        parent_sandbox = getattr(self.tool_context, "sandbox", None)
        sandbox_binding = RuntimeSandboxBinding(
            profile="subagent",
            backend=str(getattr(parent_sandbox, "backend", "static")),
            enabled=self.tool_context.sandbox_enabled,
            network=bool(getattr(parent_sandbox, "network", False)),
            permission_profile=permission_mode.value,
        )
        principal = self.tool_context.principal
        authority = build_subagent_authority(
            effective_contract,
            tools=tools,
            mcp_servers=mcp_servers,
            network_allowed=bool(
                getattr(parent_sandbox, "network", False)),
        )
        agent_contract = AgentContractFactory.from_application(
            application_request,
            principal=RuntimeIdentity(
                principal_id=str(
                    getattr(principal, "subject", "") or "local"),
                auth_method=str(
                    getattr(principal, "auth_method", "") or "local"),
            ),
            harness_release=HarnessReleaseIdentity(
                agent_definition_id=(
                    definition_obj.name if definition_obj is not None else "")),
            sandbox=sandbox_binding,
            tool_allowlist=tuple(tool.name for tool in tools),
            mcp_allowlist=mcp_servers,
            authority=authority,
            metadata={
                "subagent_id": agent_id,
                "delegation_id": effective_contract.delegation_id,
                "delegation_contract_hash": effective_contract.contract_hash,
                "parent_task_id": parent_context.parent_task_id,
                "parent_turn_id": parent_context.parent_turn_id,
                "parent_session_id": parent_context.parent_session_id,
                "relation_kind": DelegationKind(
                    effective_contract.relation_kind).value,
            },
        )
        relation_kind = DelegationKind(effective_contract.relation_kind)
        responsibility_owner = (
            f"subagent:{agent_id}"
            if relation_kind is DelegationKind.HANDOFF
            else parent_context.owner
        )
        delegation_relation = DelegationRelation(
            delegation_id=effective_contract.delegation_id,
            parent_task_id=parent_context.parent_task_id,
            parent_turn_id=parent_context.parent_turn_id,
            parent_session_id=parent_context.parent_session_id,
            agent_id=agent_id,
            kind=relation_kind,
            from_owner=parent_context.owner,
            responsibility_owner=responsibility_owner,
            recovery_checkpoint_id=effective_contract.recovery_checkpoint_id,
        )
        handoff_contract = None
        if relation_kind is DelegationKind.HANDOFF:
            handoff_contract = HandoffContract(
                handoff_id="",
                delegation_id=effective_contract.delegation_id,
                from_owner=parent_context.owner,
                to_owner=f"subagent:{agent_id}",
                recovery_checkpoint_id=(
                    effective_contract.recovery_checkpoint_id),
                state_digest="",
                state=(
                    dict(effective_contract.handoff_state)
                    or effective_contract.known_context.to_dict()
                ),
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
            release_id=self.prompt_release_id,
            calibrations=self.prompt_calibrations,
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
                "prompt_release_id": prompt_resolution.release_id,
                "prompt_release_hash": prompt_resolution.release_hash,
                "prompt_profile_hash": prompt_resolution.profile_hash,
                "profile_calibration_id": prompt_resolution.calibration_id,
                "profile_calibration_hash": prompt_resolution.calibration_hash,
                "profile_calibration_status": prompt_resolution.calibration_status,
                "profile_calibration_reason": prompt_resolution.calibration_reason,
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
        # Each subagent gets its own Prompt Registry. Bind it to the child
        # execution context before constructing the loop so prompt_lab never
        # inspects the parent agent's registry or history.
        tool_context.prompt_registry = cast(
            PromptFragmentRegistryPort, prompt_registry)
        own_working_context = WorkingContextStore(
            tool_context.workspace_root,
            redactor=self.redactor.redact if self.redactor is not None else None,
        )
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
            artifact_store=self.artifact_store,
        )
        loop.prompt_registry = prompt_registry
        loop.prompt_profile = prompt_resolution.profile
        loop.prompt_profile_resolution = prompt_resolution
        loop.prompt_profile_name = prompt_resolution.name
        loop.subagent_definition_name = definition_obj.name if definition_obj is not None else ''
        apply_prompt_snapshot(loop, prompt_snapshot)

        effective_task = render_delegation_task(effective_contract)
        forked = bool(fork_context)
        loop.subagent_instruction_layers = cast(
            tuple[InstructionLayerPort, ...], instruction_bundle.layers
        )
        loop.inherited_memory_ids = memory_ids
        loop.parent_history_inherited = False
        parent_ledger = (
            self.cache_ledger_provider()
            if self.cache_ledger_provider is not None else None
        )
        parent_policy_hash = str(getattr(
            parent_ledger, "policy_instruction_hash", "") or "")
        loop.cache_ledger_owner = f'subagent:{agent_id}'
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
            "prompt_release_id": prompt_resolution.release_id,
            "prompt_release_hash": prompt_resolution.release_hash,
            "prompt_profile_hash": prompt_resolution.profile_hash,
            "profile_calibration_id": prompt_resolution.calibration_id,
            "profile_calibration_hash": prompt_resolution.calibration_hash,
            "profile_calibration_status": prompt_resolution.calibration_status,
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
            "delegation_contract": effective_contract.to_dict(),
            "delegation_relation": delegation_relation.to_dict(),
            "handoff_contract": (
                handoff_contract.to_dict()
                if handoff_contract is not None else {}
            ),
            "application": (
                application_request.to_dict()
                if application_request is not None else {}
            ),
        })
        handle = AgentHandle(
            id=agent_id,
            task=task,
            loop=loop,
            started_at=time.time(),
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
            application_request=application_request,
            agent_contract=agent_contract,
            delegation_contract=effective_contract,
            delegation_relation=delegation_relation,
            handoff_contract=handoff_contract,
            responsibility_owner=responsibility_owner,
            base_workspace_version=parent_context.workspace_version,
        )
        self._handles[agent_id] = handle
        child_bus.subscribe(partial(self._on_child_event, handle))
        sidechain.append_message("user", effective_task, source="initial_task")
        sidechain.append_state(STATUS_RUNNING, reason="spawn")
        await self._spawn_slots.acquire()
        handle.global_slot_acquired = True
        try:
            handle.task_obj = asyncio.create_task(
                self._run(handle, effective_task))
        except Exception:
            self._spawn_slots.release()
            handle.global_slot_acquired = False
            raise
        await self.bus.emit(SubagentSpawned(
            agent_id,
            task,
            definition=handle.definition,
            delegation_id=effective_contract.delegation_id,
            parent_task_id=parent_context.parent_task_id,
            parent_turn_id=parent_context.parent_turn_id,
            parent_session_id=parent_context.parent_session_id,
            relation_kind=relation_kind.value,
            responsibility_owner=responsibility_owner,
            contract_hash=effective_contract.contract_hash,
        ))
        return handle

    # -- child feed ---------------------------------------------------------------

    async def shutdown(self) -> None:
        """取消所有运行/暂停子代理，存档 worktree 并关闭 sidechain。"""
        if self._closed:
            return
        self._closed = True
        active = [h for h in self._handles.values() if self.is_running(h)]
        active_tasks: List[asyncio.Task[None]] = []
        for handle in active:
            task = handle.task_obj
            if task is None:
                continue
            task.cancel()
            active_tasks.append(task)
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        for handle in self._handles.values():
            if handle.worktree is not None:
                await self.worktrees.finalize(handle)
                await self.worktrees.remove(handle)
            if handle.sidechain is not None:
                handle.sidechain.append_state(
                    handle.status, reason="manager_shutdown",
                    turns=handle.turns, result=handle.result, error=handle.error)
                handle.sidechain.append_history(
                    handle.loop.history, turn=handle.turns)
                handle.sidechain.close()

    # -- 工具池 -------------------------------------------------------------------

    def build_tools(self) -> List[Tool]:
        from .tools import build_subagent_tools
        return build_subagent_tools(self)
