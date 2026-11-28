"""Pure text projections for subagent status surfaces."""

from __future__ import annotations

from typing import Iterable, List

from ..contracts.delegation import DelegationKind
from .contracts import TERMINAL_STATUSES
from .definition import AgentDefinitionManager
from .models import AgentHandle
from .sidechain import SidechainStore


def format_status_lines(handles: Iterable[AgentHandle]) -> List[str]:
    lines = []
    for handle in handles:
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
        sequence = (
            f" · feed {handle.feed.latest_sequence}" if handle.feed else "")
        runtime_parts = []
        if handle.model:
            runtime_parts.append(f"model={handle.model}")
        if handle.permission_mode:
            runtime_parts.append(f"permission={handle.permission_mode}")
        if handle.mcp_servers:
            runtime_parts.append(f"mcp={','.join(handle.mcp_servers)}")
        runtime = " · " + " · ".join(runtime_parts) if runtime_parts else ""
        contract = handle.delegation_contract
        delegation = (
            f" · delegation={contract.delegation_id}"
            f" · relation={DelegationKind(contract.relation_kind).value}"
            if contract is not None else "")
        owner = (
            f" · owner={handle.responsibility_owner}"
            if handle.responsibility_owner else "")
        accepted = (
            " · accepted"
            if handle.delegation_result is not None
            and handle.delegation_result.accepted else "")
        line = (f"{handle.id} [{handle.status}] {handle.turns} 轮{queued}{branch}"
                f"{definition}{runtime}{delegation}{owner}{accepted}"
                f"{memory}{sequence} · 任务: "
                f"{task_preview.replace(chr(10), ' ')}")
        if handle.status in TERMINAL_STATUSES:
            line += f" · 结果: {handle.preview()}"
        lines.append(line)
    return lines


def format_definition_lines(definitions: AgentDefinitionManager) -> List[str]:
    lines = list(definitions.status_lines())
    lines.extend(
        f"! 跳过无效定义 {issue.path}: {issue.message}"
        for issue in definitions.issues)
    return lines


def format_sidechain_lines(
    sidechains: SidechainStore, limit: int = 50,
) -> List[str]:
    return [
        f"{info.agent_id} [{info.status or '-'}] run={info.run_id} "
        f"records={info.records} · {info.task[:60]} · {info.path}"
        for info in sidechains.list(limit=limit)
    ]


__all__ = [
    "format_definition_lines",
    "format_sidechain_lines",
    "format_status_lines",
]
