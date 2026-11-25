"""Headless Security Review runner and CLI execution path."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List, Optional

from ..agent.turn.loop import AgentLoop
from ..agent.prompt.prompt_surface_catalog import refresh_prompt_surfaces
from ..agent.prompt.prompt_surface_contract import PromptSurfaceExecutor, observe_surface_result
from ..agent.usage import UsageTracker
from ..core.events import EventBus, PermissionDenied, ToolCallCompleted
from ..core.redact import SecretRedactor
from ..llm.base import LLMProvider
from ..llm.providers.mock import MockProvider
from ..llm.registry import create_provider, resolve_settings
from ..llm.retry import RetryPolicy, RetryingProvider
from ..safety.policy import SandboxPolicy
from ..contracts.tool import PermissionMode, RunMode, RuntimeSnapshot, ToolContext
from .prompt import (
    SECURITY_REVIEW_SYSTEM_PROMPT,
    build_security_review_input,
)
from .surfaces import security_review_prompt_surface
from .report import (
    SECURITY_REVIEW_SURFACE_CONTRACT,
    SecurityReviewReport,
    SecurityReviewReportError,
    normalize_security_report,
)
from .tools import (
    SECURITY_REVIEW_TOOL_NAMES,
    build_security_review_registry,
)


DEFAULT_FAIL_ON = "high"
_EMPTY_REPORT = {
    "schema_version": "security-review-report-v1",
    "summary": "No actionable security issue was identified in the provided scope.",
    "overall_severity": "info",
    "verdict": "pass",
    "findings": [],
    "coverage": {
        "reviewed_files": [],
        "reviewed_areas": [],
        "notes": "Review completed within the available read-only evidence.",
    },
    "limitations": [],
}


class _SecurityReviewSummary:
    def __init__(self) -> None:
        self.tools_used: List[str] = []
        self.denied = 0

    def on_event(self, event) -> None:
        if isinstance(event, ToolCallCompleted):
            self.tools_used.append(event.tool_call.name)
        elif isinstance(event, PermissionDenied):
            self.denied += 1


def build_security_review_llm(
    args: argparse.Namespace,
    config: dict,
) -> LLMProvider:
    if getattr(args, "mock", False) or getattr(args, "provider", None) == "mock":
        return MockProvider(
            fallback=json.dumps(_EMPTY_REPORT, ensure_ascii=False))
    settings = resolve_settings(
        provider=getattr(args, "provider", None),
        model=getattr(args, "model", None),
        base_url=getattr(args, "base_url", None),
        config=config,
    )
    provider = create_provider(settings)
    retries = getattr(args, "retries", 2)
    if retries > 0:
        provider = RetryingProvider(provider, RetryPolicy(max_retries=retries))
    return provider


def _validate_report_workspace(
    report: SecurityReviewReport,
    workspace: Path,
) -> None:
    """Reject findings that cite non-existent or escaped workspace paths."""
    root = workspace.resolve()
    reviewed = set(report.coverage.reviewed_files)
    reviewed.update(finding.file for finding in report.findings)
    line_limits: dict[str, int] = {}
    for raw_name in sorted(reviewed):
        candidate = (root / raw_name).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise SecurityReviewReportError(
                f"报告路径越出工作区: {raw_name}") from exc
        if not candidate.is_file():
            raise SecurityReviewReportError(
                f"报告引用了不存在的文件: {raw_name}")
        try:
            size = candidate.stat().st_size
            if size <= 5_000_000:
                line_limits[raw_name] = candidate.read_text(
                    encoding="utf-8", errors="replace").count("\n") + 1
        except OSError as exc:
            raise SecurityReviewReportError(
                f"无法校验报告文件 {raw_name}: {exc}") from exc
    for finding in report.findings:
        line_limit = line_limits.get(finding.file)
        if line_limit is None:
            continue
        if finding.line is not None and finding.line > line_limit:
            raise SecurityReviewReportError(
                f"{finding.id} 的 line 超过 {finding.file} 总行数")
        if finding.end_line is not None and finding.end_line > line_limit:
            raise SecurityReviewReportError(
                f"{finding.id} 的 end_line 超过 {finding.file} 总行数")


def _usage_payload(usage: UsageTracker) -> dict[str, object]:
    return {
        "calls": usage.session.calls,
        "input": usage.session.input,
        "output": usage.session.output,
        "total": usage.session.total,
    }


async def run_security_review(
    args: argparse.Namespace,
    llm: Optional[LLMProvider] = None,
) -> int:
    from ..config import load_config_layers

    workspace = Path(args.workspace).resolve()
    bundle = load_config_layers(
        workspace=workspace,
        explicit_path=(
            Path(args.config) if getattr(args, "config", None) else None),
    )
    json_mode = getattr(args, "output_format", "text") == "json"
    fail_on = str(getattr(args, "fail_on", DEFAULT_FAIL_ON) or DEFAULT_FAIL_ON)
    staged = bool(getattr(args, "staged", False))
    focus = str(getattr(args, "focus", "") or "")

    # A clean workspace needs no Provider or API key.
    if await build_security_review_input(workspace, staged, focus) is None:
        if json_mode:
            print(json.dumps({
                "ok": True,
                "report": None,
                "reason": "no_changes",
                "scope": {
                    "mode": "staged" if getattr(args, "staged", False)
                    else "working-tree",
                    "workspace": str(workspace),
                },
            }, ensure_ascii=False))
        else:
            print("没有可审查的 git 变更（工作区干净）。")
        return 0

    provider = llm or build_security_review_llm(args, bundle.data)
    redactor = SecretRedactor()
    redactor.add(getattr(provider, "api_key", None))
    review_input = await build_security_review_input(
        workspace,
        staged,
        focus,
        redactor=redactor,
    )
    if review_input is None:  # The change set disappeared between snapshots.
        return 0

    bus = EventBus()
    summary = _SecurityReviewSummary()
    bus.subscribe(summary.on_event)
    usage = UsageTracker(model=getattr(provider, "model", None))
    usage.attach(bus)
    loop = AgentLoop(
        llm=provider,
        registry=build_security_review_registry(),
        tool_context=ToolContext(
            workspace_root=workspace,
            runtime=RuntimeSnapshot(
                run_mode=RunMode.REVIEW,
                permission_mode=PermissionMode.READ_ONLY,
                tool_allowlist=SECURITY_REVIEW_TOOL_NAMES,
            ),
        ),
        policy=SandboxPolicy(approver=None, bus=bus),
        bus=bus,
        system_prompt=SECURITY_REVIEW_SYSTEM_PROMPT,
        max_steps=max(1, min(24, int(getattr(args, "max_steps", 16) or 16))),
    )
    loop.redactor = redactor
    loop.auxiliary_prompt_surfaces = (security_review_prompt_surface(),)
    refresh_prompt_surfaces(loop)

    async def run_surface(safe_prompt: object) -> str:
        return await loop.run(str(safe_prompt))

    surface = await PromptSurfaceExecutor().execute(
        "aux:security-review",
        SECURITY_REVIEW_SURFACE_CONTRACT,
        review_input.prompt,
        run_surface,
        redactor=redactor,
        observer=lambda _surface_id, result: observe_surface_result(
            loop.services.prompt.surface_registry, result),
    )

    report: Optional[SecurityReviewReport] = None
    report_error = ""
    if surface.ok:
        try:
            report = normalize_security_report(surface.value)
            _validate_report_workspace(report, workspace)
        except SecurityReviewReportError as exc:
            report_error = str(exc)

    scope = {
        "mode": review_input.mode,
        "workspace": str(workspace),
        "focus": review_input.focus,
        "changed_files": list(review_input.changed_files),
        "untracked_files": list(review_input.untracked_files),
    }
    if not surface.ok or report is None:
        error = report_error or surface.error or surface.status
        if json_mode:
            print(json.dumps({
                "ok": False,
                "error": error,
                "scope": scope,
                "surface_contract": {
                    "status": surface.status,
                    "timed_out": surface.timed_out,
                    "truncated": surface.truncated,
                    "schema_valid": surface.schema_valid,
                },
            }, ensure_ascii=False))
        else:
            print(f"Security Review 错误：{error}")
        return 1

    gate_failed = report.gate_failed(fail_on)
    if json_mode:
        print(json.dumps({
            "ok": True,
            "report": report.to_dict(),
            "scope": scope,
            "gate": {
                "fail_on": fail_on,
                "failed": gate_failed,
            },
            "usage": _usage_payload(usage),
            "cost_usd": usage.cost_usd,
            "tools_used": summary.tools_used,
            "denied": summary.denied,
        }, ensure_ascii=False))
    else:
        print(report.render_text())
        print(
            f"\n[gate] fail-on={fail_on} · "
            f"{'FAILED' if gate_failed else 'PASSED'}")
        if usage.session.calls:
            print(f"[usage] {usage.session.text()}{usage.cost_note()}")
    return 1 if gate_failed else 0


__all__ = [
    "DEFAULT_FAIL_ON",
    "build_security_review_llm",
    "run_security_review",
]
