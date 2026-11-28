"""Daemon-owned agent runtime lifecycle and RPC operations."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, cast

from ..contracts.runtime import RuntimeResourcePort, RuntimeResourceRegistryPort
from ..contracts.service_ports import ShellJobManagerPort
from ..core.events import (
    AgentEvent,
    SubagentFeedUpdated,
    SubagentFinished,
    SubagentSpawned,
    SubagentStateChanged,
)
from ..core.redact import SecretRedactor
from ..contracts.application import (
    ApplicationRequest,
    ApplicationRequestFactory,
    TaskShape,
    TriggerType,
)
from ..contracts.delegation import (
    DelegationContract,
    DelegationParentContext,
    DelegationResult,
)
from ..jobs import ShellJobManager
from .protocol import RPCError
from .server_runtime import (
    DaemonAgentRuntime,
    DaemonAgentRuntimeFactory,
    DaemonAgentRuntimeRequest,
)
from .storage import AgentRecordStore


class _AsyncCloseResource(RuntimeResourcePort):
    def __init__(
        self,
        name: str,
        closer: Callable[[], Awaitable[None]],
    ) -> None:
        self._name = name
        self._closer = closer
        self._closed = False

    @property
    def resource_name(self) -> str:
        return self._name

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._closer()


class DaemonAgentService:
    """Owns lazy runtime creation, event persistence and agent RPC dispatch."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        config_path: Optional[Path],
        config: Dict[str, Any],
        no_sandbox: bool,
        sandbox_backend: Optional[str],
        sandbox_network: Optional[bool],
        auto_approve: bool,
        permission_mode: Optional[str],
        runtime_factory: Optional[DaemonAgentRuntimeFactory],
        agent_store: AgentRecordStore,
        job_manager: ShellJobManager,
        redactor: SecretRedactor,
        instance_id: str,
        log: Callable[[str], None],
    ) -> None:
        self.workspace_root = workspace_root
        self.config_path = config_path
        self.config = config
        self.no_sandbox = no_sandbox
        self.sandbox_backend = sandbox_backend
        self.sandbox_network = sandbox_network
        self.auto_approve = auto_approve
        self.permission_mode = permission_mode
        self.runtime_factory = runtime_factory
        self.agent_store = agent_store
        self.job_manager = job_manager
        self.redactor = redactor
        self.instance_id = instance_id
        self.log = log
        self._agent_runtime: Optional[DaemonAgentRuntime] = None
        self._agents: Any = None
        self._agent_lock = asyncio.Lock()

    @property
    def runtime_loaded(self) -> bool:
        return self._agents is not None

    def has_active_work(self) -> bool:
        if self._agent_runtime is not None:
            list_tasks = getattr(
                self._agent_runtime.task_runtime, "list_tasks", None)
            if callable(list_tasks):
                try:
                    if any(
                        not state.status.terminal
                        for state in list_tasks()
                    ):
                        return True
                except Exception:
                    pass
        if self._agents is None:
            return False
        try:
            return any(
                self._agents.is_running(handle)
                for handle in self._agents._handles.values()
            )
        except Exception:
            return False

    async def shutdown(self) -> None:
        if self._agent_runtime is not None:
            preserve = getattr(
                self._agent_runtime.task_runtime,
                "preserve_for_restart",
                None,
            )
            if callable(preserve):
                try:
                    await preserve()
                except Exception as exc:
                    self.log(
                        f"[daemon:task] preserve for restart failed: {exc}")
            resources: RuntimeResourceRegistryPort = self._agent_runtime.resources
            try:
                await resources.close_all()
            except Exception:
                pass
            self._agent_runtime = None
            self._agents = None

    async def _ensure_runtime(self) -> Tuple[DaemonAgentRuntime, Any]:
        async with self._agent_lock:
            if self._agents is not None:
                if self._agent_runtime is None:
                    raise RuntimeError("daemon agent runtime 状态不一致")
                return self._agent_runtime, self._agents
            if self.runtime_factory is None:
                raise RuntimeError(
                    "daemon agent runtime 未配置；请由组合根注入 "
                    "agent_runtime_factory")
            runtime = await self.runtime_factory(DaemonAgentRuntimeRequest(
                workspace_root=self.workspace_root,
                config_path=self.config_path,
                config=dict(self.config),
                no_sandbox=self.no_sandbox,
                sandbox_backend=self.sandbox_backend,
                sandbox_network=self.sandbox_network,
                auto_approve=self.auto_approve,
                permission_mode=self.permission_mode,
                log=lambda text: self.log(f"[daemon:agent] {text}"),
            ))
            agents = runtime.agents
            redactor = runtime.redactor or self.redactor
            agents.redactor = redactor
            agents.sidechains.redactor = redactor
            # Child tools use the daemon's durable job manager, so jobs started
            # by subagents remain controllable after the spawning client leaves.
            if runtime.tool_context is not None:
                runtime.tool_context.shell_jobs = cast(
                    ShellJobManagerPort, self.job_manager)
            if getattr(agents, "tool_context", None) is not None:
                agents.tool_context.shell_jobs = self.job_manager
            runtime.resources.register(_AsyncCloseResource(
                "daemon_jobs",
                self.job_manager.shutdown,
            ))
            runtime.bus.subscribe(self.on_event)
            self._agent_runtime = runtime
            self._agents = agents
            return runtime, agents

    async def on_event(self, event: AgentEvent) -> None:
        try:
            if isinstance(event, SubagentSpawned):
                handle = self._agents.get(event.agent_id) if self._agents else None
                if handle is not None:
                    await asyncio.to_thread(
                        self.agent_store.upsert,
                        handle.id,
                        _agent_record(handle, self.instance_id),
                    )
                return
            agent_id = getattr(event, "agent_id", "")
            if not agent_id or self._agents is None:
                return
            handle = self._agents.get(agent_id)
            if handle is None:
                return
            values = _agent_record(handle, self.instance_id)
            if isinstance(event, SubagentStateChanged):
                values["reason"] = event.reason
            elif isinstance(event, SubagentFeedUpdated):
                values["latest_sequence"] = event.sequence
            elif isinstance(event, SubagentFinished):
                values["result"] = handle.result
                values["error"] = handle.error
            await asyncio.to_thread(self.agent_store.upsert, agent_id, values)
        except Exception as exc:
            self.agent_store.issues.append(f"agent event 持久化失败: {exc}")

    async def dispatch(self, operation: str, params: Dict[str, Any]) -> Any:
        if operation.startswith("task."):
            return await self._dispatch_task(operation, params)
        if operation == "list":
            return self.merged_agents()
        if operation == "definitions":
            _, manager = await self._ensure_runtime()
            return {"lines": manager.definition_lines()}
        if operation == "sidechains":
            _, manager = await self._ensure_runtime()
            return {"lines": manager.sidechain_lines(limit=int(params.get("limit") or 50))}
        if operation == "sidechain":
            _, manager = await self._ensure_runtime()
            path, records = manager.read_sidechain(_required(params, "agent_id"))
            return {"path": str(path), "records": list(records)}
        if operation == "spawn":
            _, manager = await self._ensure_runtime()
            task = _required(params, "task")
            contract_raw = params.get("delegation_contract")
            parent_raw = params.get("delegation_parent")
            parent_context = (
                DelegationParentContext.from_mapping(parent_raw)
                if isinstance(parent_raw, dict) else None
            )
            contract = (
                DelegationContract.from_mapping(
                    contract_raw,
                    parent=parent_context,
                    task_fallback=task,
                )
                if isinstance(contract_raw, dict) else None
            )
            handle = await manager.spawn(
                task,
                isolated=_optional_bool(params.get("isolated")),
                fork_context=_optional_bool(params.get("fork_context")),
                definition=(str(params.get("definition") or "").strip() or None),
                application_request=_daemon_application_request(
                    params,
                    goal=task,
                ),
                contract=contract,
                parent_context=parent_context,
            )
            await asyncio.to_thread(
                self.agent_store.upsert,
                handle.id,
                _agent_record(handle, self.instance_id),
            )
            return _handle_dict(handle)
        if operation in {
            "wait", "feed", "send_input", "pause", "resume", "kill",
            "retry", "record_result",
        }:
            agent_id = _required(params, "agent_id")
            durable = self.agent_store.load(agent_id)
            if operation == "feed" and (
                    durable is not None
                    and durable.get("status") not in {"running", "paused", "queued"}):
                return self._durable_feed(
                    agent_id,
                    after_sequence=int(params.get("after_sequence") or 0),
                    limit=int(params.get("limit") or 200),
                )
            if operation == "wait" and (
                    durable is not None
                    and durable.get("status") not in {"running", "paused", "queued"}):
                return durable
            _, manager = await self._ensure_runtime()
            if operation == "wait":
                handle = await manager.wait(
                    agent_id, timeout=float(params.get("timeout") or 60.0))
                await asyncio.to_thread(
                    self.agent_store.upsert,
                    handle.id,
                    _agent_record(handle, self.instance_id),
                )
                return _handle_dict(handle)
            if operation == "feed":
                page = manager.feed(
                    agent_id,
                    after_sequence=int(params.get("after_sequence") or 0),
                    limit=int(params.get("limit") or 200),
                )
                return {
                    "agent_id": page.agent_id,
                    "after_sequence": page.after_sequence,
                    "latest_sequence": page.latest_sequence,
                    "next_sequence": page.next_sequence,
                    "dropped_through": page.dropped_through,
                    "entries": [
                        {
                            "agent_id": entry.agent_id,
                            "sequence": entry.sequence,
                            "kind": entry.kind,
                            "text": entry.text,
                            "ts": entry.ts,
                            "data": entry.data,
                        }
                        for entry in page.entries
                    ],
                }
            if operation == "send_input":
                message = _required(params, "message")
                handle, action = await manager.send_input(
                    agent_id,
                    message,
                    interrupt=bool(params.get("interrupt")),
                    application_request=_daemon_application_request(
                        params,
                        goal=message,
                    ),
                )
            elif operation == "retry":
                handle = await manager.retry(
                    agent_id,
                    definition=str(params.get("definition") or ""),
                )
                action = "retried"
            elif operation == "record_result":
                result_raw = params.get("result")
                if not isinstance(result_raw, dict):
                    raise RPCError("record_result 缺少 result", code="invalid_request")
                handle = manager.get(agent_id)
                if handle is None:
                    raise RPCError(f"未知子代理: {agent_id}", code="not_found")
                await manager.record_delegation_result(
                    handle, DelegationResult.from_mapping(result_raw))
                action = "recorded"
            elif operation == "pause":
                handle = await manager.pause(agent_id)
                action = "paused"
            elif operation == "resume":
                handle = await manager.resume(agent_id)
                action = "resumed"
            else:
                handle = await manager.kill(agent_id)
                action = "killed"
            await asyncio.to_thread(
                self.agent_store.upsert,
                handle.id,
                _agent_record(handle, self.instance_id),
            )
            return {"action": action, "agent": _handle_dict(handle)}
        raise RPCError(f"未知 agent operation: {operation}", code="unknown_operation")

    async def _dispatch_task(
        self,
        operation: str,
        params: Dict[str, Any],
    ) -> Any:
        runtime, _ = await self._ensure_runtime()
        agent_runtime = runtime.agent_runtime
        task_runtime = runtime.task_runtime
        if agent_runtime is None:
            raise RPCError(
                "daemon task runtime 未配置", code="task_runtime_unavailable")
        task_id = str(params.get("task_id") or "").strip()
        if operation == "task.submit":
            if task_id:
                try:
                    attachment = task_runtime.attach(task_id)
                except KeyError:
                    attachment = None
                if attachment is not None:
                    return attachment.to_dict()
            message = _required(params, "message")
            raw_application = params.get("application")
            application = (
                ApplicationRequest.from_mapping(raw_application)
                if isinstance(raw_application, dict)
                else ApplicationRequestFactory.from_input_source(
                    "daemon",
                    goal=message,
                    task_shape=TaskShape.MULTI_AGENT,
                    metadata={"rpc": "task.submit"},
                )
            )
            query = agent_runtime.start_query(
                message,
                application_request=application,
                task_id=task_id,
                parent_task_id=str(params.get("parent_task_id") or ""),
                idempotency_key=str(params.get("idempotency_key") or ""),
            )
            return query.detach().to_dict()
        if operation == "task.list":
            list_tasks = getattr(task_runtime, "list_tasks", None)
            return [
                _task_state_dict(item)
                for item in (list_tasks() if callable(list_tasks) else ())
            ]
        if not task_id:
            raise RPCError("缺少 task_id", code="invalid_request")
        if operation == "task.attach":
            return task_runtime.attach(task_id).to_dict()
        if operation == "task.inspect":
            state = agent_runtime.inspect_task(task_id)
            return _task_state_dict(state) if state is not None else {}
        if operation == "task.events":
            return {
                "task_id": task_id,
                "events": list(agent_runtime.task_events(
                    task_id,
                    after_sequence=int(params.get("after_sequence") or 0),
                    limit=int(params.get("limit") or 1_000),
                )),
            }
        if operation == "task.cancel":
            return {"cancelled": await agent_runtime.cancel_task(task_id)}
        if operation == "task.pause":
            return {"paused": await agent_runtime.pause_task(task_id)}
        if operation == "task.resume":
            return {"resumed": await agent_runtime.resume_task(task_id)}
        if operation == "task.recover":
            query = await agent_runtime.recover_task(task_id, detach=True)
            return query.detach().to_dict()
        if operation == "task.compensate":
            effect_id = str(params.get("effect_id") or "").strip()
            if not effect_id:
                raise RPCError("缺少 effect_id", code="invalid_request")
            evidence_raw = params.get("evidence")
            return {
                "updated": agent_runtime.compensate_side_effect(
                    task_id,
                    effect_id,
                    succeeded=bool(params.get("succeeded", True)),
                    compensation=str(params.get("compensation") or ""),
                    evidence=(
                        dict(evidence_raw)
                        if isinstance(evidence_raw, dict) else {}
                    ),
                )
            }
        raise RPCError(
            f"未知 task operation: {operation}", code="unknown_operation")

    def _durable_feed(
        self, agent_id: str, *, after_sequence: int, limit: int,
    ) -> Dict[str, Any]:
        from ..subagent.sidechain import SidechainStore, read_sidechain

        store = SidechainStore(self.workspace_root, redactor=self.redactor)
        path = store.resolve(agent_id)
        if path is None:
            raise RPCError(f"未找到子代理 sidechain: {agent_id}", code="not_found")
        records = read_sidechain(path)
        entries = [
            {
                "agent_id": agent_id,
                "sequence": int(item.get("sequence") or 0),
                "kind": str(item.get("kind") or "event"),
                "text": str(item.get("text") or ""),
                "ts": float(item.get("ts") or 0.0),
                "data": dict(item.get("data") or {}),
            }
            for item in records
            if item.get("type") == "feed"
            and int(item.get("sequence") or 0) > max(0, int(after_sequence))
        ]
        entries = entries[:max(1, min(int(limit), 1_000))]
        latest = max(
            [int(item.get("sequence") or 0) for item in records
             if item.get("type") == "feed"] or [0])
        return {
            "agent_id": agent_id,
            "after_sequence": max(0, int(after_sequence)),
            "latest_sequence": latest,
            "next_sequence": entries[-1]["sequence"] if entries else max(0, int(after_sequence)),
            "dropped_through": 0,
            "entries": entries,
        }

    def merged_agents(self) -> List[Dict[str, Any]]:
        records = {str(item.get("agent_id")): item for item in self.agent_store.list()}
        if self._agents is not None:
            for handle in self._agents._handles.values():
                records[handle.id] = _agent_record(handle, self.instance_id)
        ordered = sorted(
            records.values(),
            key=lambda item: float(item.get("updated_at") or item.get("started_at") or 0),
            reverse=True,
        )
        return ordered


def _required(params: Dict[str, Any], key: str) -> str:
    value = str(params.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} 不能为空")
    return value


def _optional_bool(value: Any) -> Optional[bool]:
    return None if value is None else bool(value)


def _agent_record(handle: Any, instance_id: str) -> Dict[str, Any]:
    return {
        "agent_id": handle.id,
        "daemon_instance_id": instance_id,
        "task": handle.task,
        "definition": handle.definition,
        "status": handle.status,
        "turns": handle.turns,
        "result": handle.result,
        "error": handle.error,
        "worktree": str(handle.worktree) if handle.worktree else "",
        "branch": handle.branch or "",
        "forked": bool(handle.forked),
        "sidechain_path": str(handle.sidechain_path or ""),
        "model": handle.model,
        "provider": handle.provider,
        "permission_mode": handle.permission_mode,
        "mcp_servers": list(handle.mcp_servers),
        "latest_sequence": handle.feed.latest_sequence if handle.feed else 0,
        "queue": list(handle.queue),
        "started_at": getattr(handle, "started_at", None) or time.time(),
        "application": (
            handle.application_request.to_dict()
            if getattr(handle, "application_request", None) is not None else {}
        ),
        "delegation_contract": _delegation_contract_dict(handle),
        "delegation_relation": _delegation_relation_dict(handle),
        "handoff_contract": _handoff_contract_dict(handle),
        "responsibility_owner": getattr(
            handle, "responsibility_owner", ""),
        "retry_count": getattr(handle, "retry_count", 0),
        "context_checkpoint_id": getattr(
            handle, "context_checkpoint_id", ""),
        "finished_at": getattr(handle, "finished_at", 0.0),
        "delegation_result": (
            handle.delegation_result.to_dict()
            if getattr(handle, "delegation_result", None) is not None else {}
        ),
        "base_workspace_version": (
            handle.base_workspace_version.to_dict()
            if getattr(handle, "base_workspace_version", None) is not None
            else {}
        ),
        "result_workspace_version": (
            handle.result_workspace_version.to_dict()
            if getattr(handle, "result_workspace_version", None) is not None
            else {}
        ),
        "budget_usage": dict(getattr(handle, "budget_usage", {}) or {}),
    }


def _handle_dict(handle: Any) -> Dict[str, Any]:
    return {
        "agent_id": handle.id,
        "task": handle.task,
        "definition": handle.definition,
        "status": handle.status,
        "turns": handle.turns,
        "result": handle.result,
        "error": handle.error,
        "preview": handle.preview(),
        "worktree": str(handle.worktree) if handle.worktree else "",
        "branch": handle.branch or "",
        "forked": bool(handle.forked),
        "sidechain_path": str(handle.sidechain_path or ""),
        "model": handle.model,
        "provider": handle.provider,
        "permission_mode": handle.permission_mode,
        "mcp_servers": list(handle.mcp_servers),
        "queue": list(handle.queue),
        "latest_sequence": handle.feed.latest_sequence if handle.feed else 0,
        "application": (
            handle.application_request.to_dict()
            if getattr(handle, "application_request", None) is not None else {}
        ),
        "delegation_contract": _delegation_contract_dict(handle),
        "delegation_relation": _delegation_relation_dict(handle),
        "handoff_contract": _handoff_contract_dict(handle),
        "delegation_result": (
            handle.delegation_result.to_dict()
            if getattr(handle, "delegation_result", None) is not None else {}
        ),
        "responsibility_owner": getattr(
            handle, "responsibility_owner", ""),
        "base_workspace_version": (
            handle.base_workspace_version.to_dict()
            if getattr(handle, "base_workspace_version", None) is not None
            else {}
        ),
        "result_workspace_version": (
            handle.result_workspace_version.to_dict()
            if getattr(handle, "result_workspace_version", None) is not None
            else {}
        ),
        "budget_usage": dict(getattr(handle, "budget_usage", {}) or {}),
        "retry_count": getattr(handle, "retry_count", 0),
    }


def _task_state_dict(state: Any) -> Dict[str, Any]:
    return {
        "task_id": state.task_id,
        "turn_id": state.turn_id,
        "parent_task_id": state.parent_task_id,
        "root_task_id": state.root_task_id,
        "status": state.status.value,
        "sequence": state.sequence,
        "creator": state.creator,
        "executor": state.executor,
        "error": state.error,
        "event_cursor": state.event_cursor.to_dict(),
        "result": state.result.to_dict() if state.result is not None else None,
        "error_info": (
            state.error_info.to_dict()
            if state.error_info is not None else None),
        "continuation": (
            state.continuation.to_dict()
            if state.continuation is not None else None),
        "wait_kind": state.wait_kind,
        "wait_reason": state.wait_reason,
        "resume_token": state.resume_token,
        "side_effects": [item.to_dict() for item in state.side_effects],
        "recovery_decision": (
            state.recovery_decision.to_dict()
            if state.recovery_decision is not None else None),
    }


def _delegation_contract_dict(handle: Any) -> Dict[str, Any]:
    contract = getattr(handle, "delegation_contract", None)
    return cast(Dict[str, Any], contract.to_dict()) if contract is not None else {}


def _delegation_relation_dict(handle: Any) -> Dict[str, Any]:
    relation = getattr(handle, "delegation_relation", None)
    return cast(Dict[str, Any], relation.to_dict()) if relation is not None else {}


def _handoff_contract_dict(handle: Any) -> Dict[str, Any]:
    handoff = getattr(handle, "handoff_contract", None)
    return cast(Dict[str, Any], handoff.to_dict()) if handoff is not None else {}


def _daemon_application_request(
    params: Dict[str, Any],
    *,
    goal: str,
) -> ApplicationRequest:
    raw = params.get("application")
    request = (
        ApplicationRequest.from_mapping(raw)
        if isinstance(raw, dict)
        else ApplicationRequestFactory.from_input_source(
            "daemon",
            goal=goal,
            task_shape=TaskShape.MULTI_AGENT,
            metadata={"rpc": "agent"},
        )
    )
    return replace(
        request,
        trigger=TriggerType.DAEMON,
        goal=goal,
    )


__all__ = ["DaemonAgentService"]
