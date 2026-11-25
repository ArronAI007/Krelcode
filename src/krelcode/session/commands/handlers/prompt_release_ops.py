"""Prompt release approval and telemetry commands."""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Optional, cast

from ....agent.context import build_workspace_context_bundle
from ....agent.context.context_inspection import ContextInspectionService
from ....agent.prompt.cyber_risk import prepend_cyber_risk_prompt
from ....agent.prompt.fragments import (
    GROUP_WORKSPACE,
    refresh_registry_snapshot,
    rule_fragment,
    skills_fragment,
    workspace_fragments,
)
from ....agent.prompt.prompt_authoring import PROMPT_AUTHORING_FRAGMENT_ID
from ....agent.prompt.prompt_release_pipeline import (
    PromptReleaseApprovalService,
    PromptReleasePipeline,
    PromptReleaseProposal,
)
from ....agent.prompts import build_system_prompt
from ....core.tokens import estimate_history_tokens
from ....core.messages import Message
from ....mcp.security import render_mcp_prompt_evidence
from ....safety.runtime import parse_permission_mode
from ....safety.rules import RuleDecision, RuleScope
from ..context import CommandContext
from ..sources import (
    sync_mcp_prompt_commands,
    sync_plugin_commands,
    sync_skill_commands,
)
from ..template import (
    COMMAND_TEMPLATE_VERSION,
    CommandArgumentMode,
    CommandRenderResult,
    CommandTemplate,
    split_reminder_ids,
)
from ..types import CommandResult, CommandSource

from .handler_utils import _ok


def _prompt_release_actor() -> str:
    # This handler is invoked by the foreground command layer.  Agent tools do
    # not have a route to these approve/reject methods.  The explicit override
    # exists for managed desktop/CI review agents that authenticate a human
    # operator outside Krelcode.
    return (
        os.environ.get("KRELCODE_HUMAN_APPROVER")
        or os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "local-user"
    ).strip()


def _prompt_release_lines(
    proposal: PromptReleaseProposal, *, detailed: bool = False
) -> list[str]:
    lines = [
        f"  [prompt-release] {proposal.state.value} · {proposal.proposal_id}",
        f"  candidate={proposal.candidate_hash[:16]}"
        f" · proposal={proposal.proposal_hash[:16]}"
        f" · profile={proposal.profile_name or '(pending)'}",
        f"  release={proposal.release_id}/{proposal.release_hash[:12]}"
        f" · approval_required=true · agent_may_approve=false",
    ]
    for stage in proposal.stages:
        suffix = f" · {stage.evidence_hash[:12]}" if stage.evidence_hash else ""
        lines.append(f"  [{stage.status.value:11s}] {stage.id}{suffix}")
        issue = stage.details.get("blocking_issue")
        if detailed and issue:
            lines.append(f"              blocking: {issue}")
        failed = stage.details.get("failed_checks")
        if detailed and isinstance(failed, (list, tuple)) and failed:
            lines.append("              failed: " + ", ".join(map(str, failed)))
    if proposal.state.value == "pending_approval":
        lines.append(
            "  approve: /prompt-release approve "
            f"{proposal.proposal_id} {proposal.candidate_hash} [reason]")
        lines.append(
            "  reject:  /prompt-release reject "
            f"{proposal.proposal_id} {proposal.candidate_hash} <reason>")
    lines.append(
        "  production_modified=false · applied=false"
        " · contains_prompt_content=false")
    return lines


async def handle_prompt_release(ctx: CommandContext) -> CommandResult:
    pipeline = cast(
        Optional[PromptReleasePipeline],
        ctx.loop.services.extensions.prompt_release_pipeline,
    )
    service = cast(
        Optional[PromptReleaseApprovalService],
        ctx.loop.services.extensions.prompt_release_approval_service,
    )
    if pipeline is None or service is None or pipeline.store is None:
        return _ok(
            "  （Prompt release pipeline 未启用；在 [prompt.authoring] enabled=true 后重启）")
    raw = ctx.request.arguments.strip()
    try:
        parts = shlex.split(raw) if raw else []
    except ValueError as exc:
        return _ok(f"  [prompt-release] 参数解析失败: {exc}")
    operation = parts[0].lower() if parts else "list"

    if operation == "list" and len(parts) == 1:
        proposals = service.list(limit=20)
        if not proposals:
            return _ok("  [prompt-release] 暂无 Proposal")
        lines = [f"  [prompt-release] {len(proposals)} 个最近 Proposal："]
        for proposal in proposals:
            lines.append(
                f"  [{proposal.state.value}] {proposal.proposal_id}"
                f" · candidate={proposal.candidate_hash[:12]}"
                f" · profile={proposal.profile_name or '(pending)'}")
        lines.append("  contains_prompt_content=false")
        return _ok(*lines)

    if operation == "status" and len(parts) <= 2:
        try:
            if len(parts) == 2:
                proposal = service.get(parts[1])
            else:
                proposals = service.list(limit=1)
                if not proposals:
                    return _ok("  [prompt-release] 暂无 Proposal")
                proposal = proposals[0]
        except (ValueError, OSError) as exc:
            return _ok(f"  [prompt-release] 无法读取 Proposal: {exc}")
        return _ok(*_prompt_release_lines(proposal, detailed=True))

    if operation in {"approve", "reject"} and len(parts) >= 3:
        proposal_id = parts[1]
        candidate_hash = parts[2]
        reason = " ".join(parts[3:]).strip()
        if operation == "reject" and not reason:
            return _ok("  用法: /prompt-release reject <id> <candidate-hash> <reason>")
        try:
            if operation == "approve":
                proposal = service.approve(
                    proposal_id,
                    candidate_hash,
                    approved_by=_prompt_release_actor(),
                    reason=reason or "human approved in Krelcode REPL",
                )
            else:
                proposal = service.reject(
                    proposal_id,
                    candidate_hash,
                    approved_by=_prompt_release_actor(),
                    reason=reason,
                )
        except (ValueError, OSError) as exc:
            return _ok(f"  [prompt-release] {operation} 失败: {exc}")
        if operation == "approve":
            return _ok(
                f"  [prompt-release] approved · {proposal.proposal_id}",
                "  approval artifact 已保存；production Prompt/Registry/config 未修改",
                *_prompt_release_lines(proposal, detailed=True))
        return _ok(
            f"  [prompt-release] rejected · {proposal.proposal_id}",
            *_prompt_release_lines(proposal, detailed=True))

    return _ok(
        "  用法: /prompt-release [list|status [id]|"
        "approve <id> <candidate-hash> [reason]|"
        "reject <id> <candidate-hash> <reason>]",
        "  Proposal 由 prompt_lab action=propose 生成；approve/reject 只能由前台人类命令执行。",
    )


async def handle_telemetry(ctx: CommandContext) -> CommandResult:
    runtime = ctx.loop.services.extensions.telemetry
    if runtime is None:
        return _ok("  [telemetry] 当前运行未初始化遥测运行时")
    parts = shlex.split(ctx.request.arguments)
    operation = parts[0].lower() if parts else "status"
    try:
        if operation == "status":
            status = runtime.status()
            queue = status.get("queue")
            if not isinstance(queue, Mapping):
                raise ValueError("telemetry queue status 无效")
            lines = [
                f"  [telemetry] mode={status['mode']} · "
                f"enabled={'yes' if status['effective_enabled'] else 'no'} · "
                f"policy_allowed={'yes' if status['policy_allowed'] else 'no'}",
                f"  sample_rate={status['sample_rate']} · "
                f"retention={status['retention_days']}d · "
                f"queue={queue['events']} events/{queue['bytes']} bytes",
                f"  queue_path={queue['path']}",
                "  content_collection=never",
            ]
            if status["last_error"]:
                lines.append(f"  last_error={status['last_error']}")
            return _ok(*lines)
        if operation == "enable":
            mode = parts[1].lower() if len(parts) > 1 else "local"
            preferences = runtime.enable(mode)
            lines = [
                f"  [telemetry] enabled · mode={preferences.mode}",
                "  content_collection=never",
            ]
            if preferences.mode == "remote" and not preferences.endpoint:
                lines.append("  remote endpoint 未配置")
            return _ok(*lines)
        if operation == "disable":
            runtime.disable()
            return _ok("  [telemetry] disabled")
        if operation == "summary":
            return _ok(
                "  [telemetry] local content-free summary",
                json.dumps(runtime.summary(), ensure_ascii=False, indent=2),
            )
        if operation == "export" and len(parts) >= 2:
            destination = os.path.expanduser(parts[1])
            export_path = (
                Path(ctx.workspace_root) / destination
                if not os.path.isabs(destination)
                else Path(destination)
            )
            exported = runtime.export(export_path)
            return _ok(f"  [telemetry] exported: {exported}")
        if operation == "clear":
            runtime.clear()
            return _ok("  [telemetry] local queue cleared")
        if operation == "send":
            sent = await runtime.flush()
            lines = [f"  [telemetry] sent={sent}"]
            if runtime.config.mode != "remote":
                lines.append("  remote endpoint 未启用；事件保留在本地队列")
            return _ok(*lines)
    except (OSError, ValueError) as exc:
        return _ok(f"  [telemetry] 操作失败: {exc}")
    return _ok(
        "  用法: /telemetry [status|enable [local|remote]|disable|"
        "summary|export <path>|clear|send]")
