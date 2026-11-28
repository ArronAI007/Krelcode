"""Serialization and atomic persistence for LocalTaskRuntime state."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Mapping, Optional

from krelcode.contracts.application import ApplicationRequest
from krelcode.contracts.agent import AgentContract
from krelcode.contracts.runtime import (
    HarnessReleaseIdentity,
    RuntimeCheckpointRef,
    RuntimeIdentity,
    RuntimeLease,
    RuntimeRetryPolicy,
    RuntimeSandboxBinding,
    RuntimeTask,
    RuntimeTaskState,
    RuntimeTaskStatus,
)
from krelcode.contracts.task_lifecycle import (
    TaskCancelMode,
    TaskContinuation,
    TaskErrorInfo,
    TaskEventCursor,
    TaskRecoveryDecision,
    TaskRecoveryCheck,
    TaskResultLocation,
    TaskSideEffectRecord,
)
from krelcode.core.messages import ImageRef
from krelcode.core.redact import SecretRedactor

RUNTIME_TASK_STATE_VERSION = 3
LEGACY_RUNTIME_TASK_STATE_VERSION = 1
PREVIOUS_RUNTIME_TASK_STATE_VERSION = 2


def build_runtime_task_payload(
    owner: str,
    task_specs: Mapping[str, RuntimeTask],
    states: Mapping[str, RuntimeTaskState],
) -> dict[str, object]:
    return {
        "version": RUNTIME_TASK_STATE_VERSION,
        "owner": owner,
        "updated_at": time.time(),
        "tasks": {
            task_id: {
                "task": task_to_mapping(task_specs[task_id]),
                "state": state_to_mapping(state),
            }
            for task_id, state in states.items()
            if task_id in task_specs
        },
    }


def persist_runtime_task_payload(
    path: Path,
    payload: Mapping[str, object],
    redactor: Optional[SecretRedactor],
) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        line = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str)
        if redactor is not None:
            line = redactor.redact(line)
        temporary.write_text(line + "\n", encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)
    except (OSError, TypeError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


def load_runtime_task_records(
    path: Path,
) -> tuple[dict[str, RuntimeTask], dict[str, RuntimeTaskState], int]:
    if not path.is_file():
        return {}, {}, 1
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}, 1
    if not isinstance(raw, Mapping) or raw.get("version") not in {
        LEGACY_RUNTIME_TASK_STATE_VERSION,
        PREVIOUS_RUNTIME_TASK_STATE_VERSION,
        RUNTIME_TASK_STATE_VERSION,
    }:
        return {}, {}, 1
    records = raw.get("tasks")
    if not isinstance(records, Mapping):
        return {}, {}, 1
    tasks: dict[str, RuntimeTask] = {}
    states: dict[str, RuntimeTaskState] = {}
    maximum = 0
    for raw_id, record in records.items():
        if not isinstance(record, Mapping):
            continue
        try:
            task = task_from_mapping(record.get("task"))
            state = state_from_mapping(record.get("state"))
        except (TypeError, ValueError, KeyError):
            continue
        task_id = str(raw_id)
        if state.task_id != task_id:
            continue
        tasks[task_id] = task
        states[task_id] = state
        maximum = max(maximum, task_number(task_id))
    return tasks, states, maximum + 1


def task_number(task_id: str) -> int:
    suffix = task_id.rsplit("-", 1)[-1]
    return int(suffix) if suffix.isdigit() else 0


def task_to_mapping(task: RuntimeTask) -> dict[str, object]:
    return {
        "task_id": task.task_id,
        "parent_task_id": task.parent_task_id,
        "root_task_id": task.root_task_id,
        "idempotency_key": task.idempotency_key,
        "creator": task.creator,
        "executor": task.executor,
        "turn_id": task.turn_id,
        "message": task.message,
        "application": task.application.to_dict(),
        "images": [
            {
                "media_type": image.media_type,
                "data_b64": image.data_b64,
                "source_path": image.source_path,
            }
            for image in task.images
        ],
        "tool_allowlist": (
            list(task.tool_allowlist)
            if task.tool_allowlist is not None else None),
        "reminder_ids": list(task.reminder_ids),
        "identity": identity_to_mapping(task.identity),
        "harness_release": release_to_mapping(task.harness_release),
        "retry_policy": retry_to_mapping(task.retry_policy),
        "sandbox": sandbox_to_mapping(task.sandbox),
        "deadline_at": task.deadline_at,
        "resume_from": (
            checkpoint_to_mapping(task.resume_from)
            if task.resume_from is not None else None),
        "metadata": dict(task.metadata),
        "agent_contract": (
            task.agent_contract.to_dict()
            if task.agent_contract is not None else None),
        "input_refs": [
            checkpoint_to_mapping(item) for item in task.input_refs
        ],
        "continuation": (
            task.continuation.to_dict()
            if task.continuation is not None else None),
        "cancel_mode": TaskCancelMode(task.cancel_mode).value,
    }


def task_from_mapping(raw: object) -> RuntimeTask:
    if not isinstance(raw, Mapping):
        raise TypeError("runtime task 记录缺失")
    application = raw.get("application")
    if not isinstance(application, Mapping):
        raise TypeError("runtime task application 缺失")
    images_raw = raw.get("images")
    images = tuple(
        ImageRef(
            media_type=str(item.get("media_type") or ""),
            data_b64=str(item.get("data_b64") or ""),
            source_path=(
                str(item["source_path"])
                if item.get("source_path") is not None else None),
        )
        for item in (images_raw if isinstance(images_raw, list) else ())
        if isinstance(item, Mapping)
    )
    allowlist_raw = raw.get("tool_allowlist")
    metadata_raw = raw.get("metadata")
    input_refs_raw = raw.get("input_refs")
    return RuntimeTask(
        turn_id=str(raw.get("turn_id") or ""),
        message=str(raw.get("message") or ""),
        application=ApplicationRequest.from_mapping(application),
        images=images,
        tool_allowlist=(
            tuple(str(item) for item in allowlist_raw)
            if isinstance(allowlist_raw, list) else None),
        reminder_ids=tuple(
            str(item) for item in (raw.get("reminder_ids") or ())
            if isinstance(item, (str, int, float))),
        identity=identity_from_mapping(raw.get("identity")),
        harness_release=release_from_mapping(raw.get("harness_release")),
        retry_policy=retry_from_mapping(raw.get("retry_policy")),
        sandbox=sandbox_from_mapping(raw.get("sandbox")),
        deadline_at=_optional_float(raw.get("deadline_at")),
        resume_from=checkpoint_from_mapping(raw.get("resume_from")),
        metadata=(
            dict(metadata_raw)
            if isinstance(metadata_raw, Mapping) else {}),
        agent_contract=(
            AgentContract.from_dict(contract_raw)
            if isinstance(
                (contract_raw := raw.get("agent_contract")), Mapping
            ) else None
        ),
        task_id=str(raw.get("task_id") or ""),
        parent_task_id=str(raw.get("parent_task_id") or ""),
        root_task_id=str(raw.get("root_task_id") or ""),
        idempotency_key=str(raw.get("idempotency_key") or ""),
        creator=str(raw.get("creator") or ""),
        executor=str(raw.get("executor") or ""),
        input_refs=tuple(
            checkpoint
            for item in (
                input_refs_raw
                if isinstance(input_refs_raw, list) else ()
            )
            if (checkpoint := checkpoint_from_mapping(item)) is not None
        ),
        continuation=(
            TaskContinuation.from_mapping(raw.get("continuation"))
            if isinstance(raw.get("continuation"), Mapping) else None
        ),
        cancel_mode=str(raw.get("cancel_mode") or "cooperative"),
    )


def state_to_mapping(state: RuntimeTaskState) -> dict[str, object]:
    lease = None
    if state.lease is not None:
        lease = {
            "lease_id": state.lease.lease_id,
            "task_id": state.lease.task_id,
            "owner": state.lease.owner,
            "acquired_at": state.lease.acquired_at,
            "expires_at": state.lease.expires_at,
        }
    return {
        "task_id": state.task_id,
        "turn_id": state.turn_id,
        "status": state.status.value,
        "lease": lease,
        "checkpoint": (
            checkpoint_to_mapping(state.checkpoint)
            if state.checkpoint is not None else None),
        "sequence": state.sequence,
        "error": state.error,
        "attempts": state.attempts,
        "deadline_at": state.deadline_at,
        "started_at": state.started_at,
        "paused_at": state.paused_at,
        "resume_from_checkpoint_id": state.resume_from_checkpoint_id,
        "updated_at": state.updated_at,
        "wait_reason": state.wait_reason,
        "wait_kind": state.wait_kind,
        "resume_token": state.resume_token,
        "waiting_since": state.waiting_since,
        "resumed_at": state.resumed_at,
        "parent_task_id": state.parent_task_id,
        "root_task_id": state.root_task_id,
        "creator": state.creator,
        "executor": state.executor,
        "event_cursor": state.event_cursor.to_dict(),
        "result": state.result.to_dict() if state.result is not None else None,
        "error_info": (
            state.error_info.to_dict()
            if state.error_info is not None else None),
        "continuation": (
            state.continuation.to_dict()
            if state.continuation is not None else None),
        "side_effects": [
            item.to_dict() for item in state.side_effects
        ],
        "cancel_mode": TaskCancelMode(state.cancel_mode).value,
        "cancel_requested": state.cancel_requested,
        "recovery_decision": (
            state.recovery_decision.to_dict()
            if state.recovery_decision is not None else None),
    }


def state_from_mapping(raw: object) -> RuntimeTaskState:
    if not isinstance(raw, Mapping):
        raise TypeError("runtime task state 缺失")
    lease_raw = raw.get("lease")
    side_effects_raw = raw.get("side_effects")
    lease = None
    if isinstance(lease_raw, Mapping):
        lease = RuntimeLease(
            lease_id=str(lease_raw.get("lease_id") or ""),
            task_id=str(lease_raw.get("task_id") or ""),
            owner=str(lease_raw.get("owner") or ""),
            acquired_at=float(str(lease_raw.get("acquired_at") or 0.0)),
            expires_at=float(str(lease_raw.get("expires_at") or 0.0)),
        )
    return RuntimeTaskState(
        task_id=str(raw.get("task_id") or ""),
        turn_id=str(raw.get("turn_id") or ""),
        status=RuntimeTaskStatus(str(raw.get("status") or "interrupted")),
        lease=lease,
        checkpoint=checkpoint_from_mapping(raw.get("checkpoint")),
        sequence=int(str(raw.get("sequence") or 0)),
        error=str(raw.get("error") or ""),
        attempts=int(str(raw.get("attempts") or 0)),
        deadline_at=_optional_float(raw.get("deadline_at")),
        started_at=_optional_float(raw.get("started_at")),
        paused_at=_optional_float(raw.get("paused_at")),
        resume_from_checkpoint_id=str(
            raw.get("resume_from_checkpoint_id") or ""),
        updated_at=float(str(raw.get("updated_at") or 0.0)),
        wait_reason=str(raw.get("wait_reason") or ""),
        wait_kind=str(raw.get("wait_kind") or ""),
        resume_token=str(raw.get("resume_token") or ""),
        waiting_since=_optional_float(raw.get("waiting_since")),
        resumed_at=_optional_float(raw.get("resumed_at")),
        parent_task_id=str(raw.get("parent_task_id") or ""),
        root_task_id=str(raw.get("root_task_id") or ""),
        creator=str(raw.get("creator") or ""),
        executor=str(raw.get("executor") or ""),
        event_cursor=TaskEventCursor.from_mapping(raw.get("event_cursor")),
        result=(
            TaskResultLocation.from_mapping(raw.get("result"))
            if isinstance(raw.get("result"), Mapping) else None
        ),
        error_info=(
            TaskErrorInfo.from_mapping(raw.get("error_info"))
            if isinstance(raw.get("error_info"), Mapping) else None
        ),
        continuation=(
            TaskContinuation.from_mapping(raw.get("continuation"))
            if isinstance(raw.get("continuation"), Mapping) else None
        ),
        side_effects=tuple(
            TaskSideEffectRecord.from_mapping(item)
            for item in (
                side_effects_raw
                if isinstance(side_effects_raw, list) else ()
            )
            if isinstance(item, Mapping)
        ),
        cancel_mode=str(raw.get("cancel_mode") or "cooperative"),
        cancel_requested=bool(raw.get("cancel_requested", False)),
        recovery_decision=_recovery_decision_from_mapping(
            raw.get("recovery_decision")),
    )


def identity_to_mapping(identity: RuntimeIdentity) -> dict[str, object]:
    return {
        "principal_id": identity.principal_id,
        "tenant_id": identity.tenant_id,
        "organization_id": identity.organization_id,
        "auth_method": identity.auth_method,
        "scopes": list(identity.scopes),
        "metadata": dict(identity.metadata),
    }


def identity_from_mapping(raw: object) -> RuntimeIdentity:
    record = raw if isinstance(raw, Mapping) else {}
    scopes = record.get("scopes")
    metadata = record.get("metadata")
    return RuntimeIdentity(
        principal_id=str(record.get("principal_id") or "local"),
        tenant_id=str(record.get("tenant_id") or "local"),
        organization_id=str(record.get("organization_id") or ""),
        auth_method=str(record.get("auth_method") or "local"),
        scopes=(
            tuple(str(item) for item in scopes)
            if isinstance(scopes, list) else ()),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def release_to_mapping(release: HarnessReleaseIdentity) -> dict[str, object]:
    return {
        "release_id": release.release_id,
        "release_hash": release.release_hash,
        "prompt_release_id": release.prompt_release_id,
        "prompt_release_hash": release.prompt_release_hash,
        "agent_definition_id": release.agent_definition_id,
        "policy_version": release.policy_version,
        "metadata": dict(release.metadata),
    }


def release_from_mapping(raw: object) -> HarnessReleaseIdentity:
    record = raw if isinstance(raw, Mapping) else {}
    metadata = record.get("metadata")
    return HarnessReleaseIdentity(
        release_id=str(record.get("release_id") or ""),
        release_hash=str(record.get("release_hash") or ""),
        prompt_release_id=str(record.get("prompt_release_id") or ""),
        prompt_release_hash=str(record.get("prompt_release_hash") or ""),
        agent_definition_id=str(record.get("agent_definition_id") or ""),
        policy_version=str(record.get("policy_version") or ""),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def retry_to_mapping(retry: RuntimeRetryPolicy) -> dict[str, object]:
    return {
        "max_attempts": retry.max_attempts,
        "backoff_seconds": retry.backoff_seconds,
        "max_backoff_seconds": retry.max_backoff_seconds,
        "retry_on_failure": retry.retry_on_failure,
        "retry_on_timeout": retry.retry_on_timeout,
    }


def retry_from_mapping(raw: object) -> RuntimeRetryPolicy:
    record = raw if isinstance(raw, Mapping) else {}
    return RuntimeRetryPolicy(
        max_attempts=int(str(record.get("max_attempts") or 1)),
        backoff_seconds=float(str(record.get("backoff_seconds") or 0.0)),
        max_backoff_seconds=float(
            str(record.get("max_backoff_seconds") or 30.0)),
        retry_on_failure=bool(record.get("retry_on_failure")),
        retry_on_timeout=bool(record.get("retry_on_timeout")),
    )


def sandbox_to_mapping(sandbox: RuntimeSandboxBinding) -> dict[str, object]:
    return {
        "profile": sandbox.profile,
        "backend": sandbox.backend,
        "enabled": sandbox.enabled,
        "network": sandbox.network,
        "permission_profile": sandbox.permission_profile,
        "write_scope": sandbox.write_scope,
        "isolation_level": sandbox.isolation_level,
        "cpu_seconds": sandbox.cpu_seconds,
        "memory_mb": sandbox.memory_mb,
        "storage_mb": sandbox.storage_mb,
        "process_limit": sandbox.process_limit,
        "metadata": dict(sandbox.metadata),
    }


def sandbox_from_mapping(raw: object) -> RuntimeSandboxBinding:
    record = raw if isinstance(raw, Mapping) else {}
    metadata = record.get("metadata")
    return RuntimeSandboxBinding(
        profile=str(record.get("profile") or "workspace"),
        backend=str(record.get("backend") or "auto"),
        enabled=bool(record.get("enabled", True)),
        network=bool(record.get("network", False)),
        permission_profile=str(record.get("permission_profile") or "workspace"),
        write_scope=str(record.get("write_scope") or "workspace"),
        isolation_level=str(record.get("isolation_level") or "os"),
        cpu_seconds=_optional_int(record.get("cpu_seconds")),
        memory_mb=_optional_int(record.get("memory_mb")),
        storage_mb=_optional_int(record.get("storage_mb")),
        process_limit=_optional_int(record.get("process_limit")),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def checkpoint_to_mapping(
    checkpoint: RuntimeCheckpointRef,
) -> dict[str, object]:
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "kind": checkpoint.kind,
        "session_id": checkpoint.session_id,
        "turn_id": checkpoint.turn_id,
        "task_id": checkpoint.task_id,
        "digest": checkpoint.digest,
        "path": checkpoint.path,
        "metadata": dict(checkpoint.metadata),
    }


def checkpoint_from_mapping(raw: object) -> Optional[RuntimeCheckpointRef]:
    if not isinstance(raw, Mapping):
        return None
    metadata = raw.get("metadata")
    return RuntimeCheckpointRef(
        checkpoint_id=str(raw.get("checkpoint_id") or ""),
        kind=str(raw.get("kind") or "context"),
        session_id=str(raw.get("session_id") or ""),
        turn_id=str(raw.get("turn_id") or ""),
        task_id=str(raw.get("task_id") or ""),
        digest=str(raw.get("digest") or ""),
        path=str(raw.get("path") or ""),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def _optional_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _recovery_decision_from_mapping(
    raw: object,
) -> Optional[TaskRecoveryDecision]:
    if not isinstance(raw, Mapping):
        return None
    checks_raw = raw.get("checks")
    checks = tuple(
        _recovery_check_from_mapping(item)
        for item in (checks_raw if isinstance(checks_raw, list) else ())
        if isinstance(item, Mapping)
    )
    continuation_raw = raw.get("continuation")
    metadata_raw = raw.get("metadata")
    return TaskRecoveryDecision(
        allowed=bool(raw.get("allowed", False)),
        checks=checks,
        requires_user=bool(raw.get("requires_user", False)),
        continuation=(
            TaskContinuation.from_mapping(continuation_raw)
            if isinstance(continuation_raw, Mapping) else None
        ),
        metadata=(
            dict(metadata_raw) if isinstance(metadata_raw, Mapping) else {}),
    )


def _recovery_check_from_mapping(
    raw: Mapping[str, object],
) -> TaskRecoveryCheck:
    evidence_raw = raw.get("evidence")
    return TaskRecoveryCheck(
        code=str(raw.get("code") or "unknown"),
        passed=bool(raw.get("passed", False)),
        reason=str(raw.get("reason") or ""),
        evidence=(
            dict(evidence_raw) if isinstance(evidence_raw, Mapping) else {}
        ),
    )


def _optional_float(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


__all__ = [
    "LEGACY_RUNTIME_TASK_STATE_VERSION",
    "PREVIOUS_RUNTIME_TASK_STATE_VERSION",
    "RUNTIME_TASK_STATE_VERSION",
    "build_runtime_task_payload",
    "load_runtime_task_records",
    "persist_runtime_task_payload",
]
