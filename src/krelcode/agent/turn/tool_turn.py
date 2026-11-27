"""Tool execution side of an agent turn.

``ToolBatchExecutor`` owns the safety and state boundaries around tool calls:
stream scheduling, schema validation, approval, hooks, output redaction,
large-output persistence, result events and history/working-context writes.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, cast

from krelcode.core.events import (
    ErrorOccurred,
    PermissionDenied,
    PlanUpdated,
    SecurityDecisionRecorded,
    ToolCallCompleted,
    ToolCallStarted,
    ToolOutputDelta,
    ToolProgress,
    UserInputRequested,
    UserInputResolved,
)
from krelcode.core.messages import (
    INVALID_ARGUMENTS_KEY,
    ImageBlock,
    Message,
    ResourceBlock,
    StructuredBlock,
    TextBlock,
    ToolCall,
    ToolResultBlock,
)
from krelcode.core.textutil import truncate_middle
from krelcode.contracts.governance import (
    AuditCorrelation,
    GovernanceDecision,
    GovernanceRequest,
    PolicyVersion,
    PluginTrustContext,
    PromptReleaseIdentity,
    SandboxProfile,
    SandboxResourceLimits,
    SecurityDecision,
    SecurityDecisionValue,
)
from krelcode.contracts.tool import (
    ToolContext,
    ToolDescriptor,
    ToolExecutionRuntime,
    ToolResult,
    ToolSource,
)
from krelcode.contracts.tool_execution import (
    ToolExecutionResult,
    ToolIntent,
)
from krelcode.tools.schema import validate_tool_arguments
from krelcode.agent.turn.dependencies import AgentDependencies
from krelcode.agent.turn.runtime_ports import ToolTurnHostPort
from krelcode.agent.turn.streaming_tools import StreamingToolExecutor
from krelcode.agent.turn.tool_pool import TurnToolPool
from krelcode.agent.context.working_context import compact_tool_metadata


class ToolBatchExecutor:
    """Execute and persist model-requested tools without owning the turn loop."""

    def __init__(
        self,
        host: ToolTurnHostPort,
        dependencies: AgentDependencies,
    ) -> None:
        self.host = host
        self.dependencies = dependencies

    def streaming_executor(self, pool: TurnToolPool) -> StreamingToolExecutor:
        return StreamingToolExecutor(pool, self.run_stream_tool)

    async def apply_results(
        self,
        tool_calls: List[ToolCall],
        results: List[ToolExecutionResult],
    ) -> Optional[str]:
        """Append results in model order and return a loop-guard reason."""
        host = self.host
        redactor = self.dependencies.redactor
        for tool_call, execution_result in zip(tool_calls, results):
            result = execution_result.to_tool_result()
            metadata = compact_tool_metadata(
                tool_call.name,
                result.data,
                is_error=result.is_error,
                turn_id=host._current_turn_id,
                arguments=tool_call.arguments,
                redactor=redactor.redact if redactor else None,
            )
            persisted_path = _persisted_output_path(result.content)
            if persisted_path:
                metadata["full_output_path"] = persisted_path
            host.history.append(
                Message.tool_result(
                    tool_call.id,
                    result.content,
                    blocks=list(result.blocks) or None,
                    tool_name=tool_call.name,
                    is_error=result.is_error,
                    metadata=metadata,
                )
            )
            self.dependencies.working_context_store.record_tool_result(
                tool_call,
                result,
                turn_id=host._current_turn_id,
            )
            signature = _call_signature(tool_call)
            if result.is_error:
                host._failure_counts[signature] = (
                    host._failure_counts.get(signature, 0) + 1)
                if (
                    host._failure_counts[signature]
                    >= self.dependencies.repeat_failure_limit
                ):
                    return (
                        f"同一调用连续失败 {host._failure_counts[signature]} 次: "
                        f"{signature}"
                    )
            else:
                host._failure_counts.pop(signature, None)
        return None

    async def run_stream_tool(self, tool_call: ToolCall) -> ToolExecutionResult:
        """Execute one streamed call and publish completion immediately."""
        await self.host._emit(ToolCallStarted(tool_call))
        result = await self.execute_one(tool_call)
        return await self.publish_tool_result(tool_call, result)

    async def publish_tool_result(
        self, tool_call: ToolCall, result: ToolExecutionResult,
    ) -> ToolExecutionResult:
        """Redact, persist and publish a tool result as soon as it completes."""
        redactor = self.dependencies.redactor
        legacy = result.to_tool_result()
        content = redactor.redact(legacy.content) if redactor else legacy.content
        inline_content = self.persist_large_output(tool_call, content)
        legacy.content = inline_content
        if redactor is not None:
            legacy.blocks = tuple(
                self._redact_tool_block(block) for block in legacy.blocks)
            legacy.data = cast(
                Dict[str, Any], _redact_json(legacy.data, redactor.redact))
        await self.host._emit(
            ToolCallCompleted(
                tool_call,
                inline_content,
                legacy.is_error,
                legacy.blocks,
                dict(legacy.data),
            )
        )
        return ToolExecutionResult.from_tool_result(
            legacy,
            runtime=result.runtime,
            diagnostics=result.diagnostics,
        )

    def _redact_tool_block(self, block: ToolResultBlock) -> ToolResultBlock:
        redactor = self.dependencies.redactor
        if redactor is None:
            return block
        redact = redactor.redact
        if isinstance(block, TextBlock):
            return TextBlock(redact(block.text))
        if isinstance(block, ImageBlock):
            return ImageBlock(block.media_type, block.data_b64, redact(block.alt))
        if isinstance(block, ResourceBlock):
            return ResourceBlock(
                uri=redact(block.uri),
                name=redact(block.name) if block.name else None,
                mime_type=block.mime_type,
                text=redact(block.text) if block.text else None,
                blob=block.blob,
            )
        if isinstance(block, StructuredBlock):
            return StructuredBlock(cast(
                Dict[str, Any], _redact_json(block.data, redact)))
        return block

    def persist_large_output(self, tool_call: ToolCall, content: str) -> str:
        """Store full large output and keep a truncated reference in history."""
        limit = self.dependencies.tool_output_inline_limit
        if len(content) <= limit:
            return content
        try:
            out_dir = (
                self.dependencies.workspace_root / ".krelcode" / "tool_outputs")
            out_dir.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in tool_call.name
            )
            call_id = tool_call.id or f"call{len(self.host.history)}"
            path = out_dir / f"{safe_name}_{call_id}.txt"
            path.write_text(content, encoding="utf-8")
        except OSError as e:
            return truncate_middle(content, limit) + (
                f"\n[注意: 输出落盘失败({e})，已就地截断]"
            )
        return (
            truncate_middle(content, limit)
            + f"\n[输出过长（{len(content)} 字符），全文已存盘: {path}]"
        )

    async def execute_one(self, tool_call: ToolCall) -> ToolExecutionResult:
        host = self.host
        deps = self.dependencies
        pool = host._current_tool_pool()
        descriptor = pool.descriptor(tool_call.name)
        prompt_spec = pool.prompt_spec(tool_call.name)
        unavailable_reason = pool.unavailable_reason(tool_call.name)
        if unavailable_reason is not None:
            runtime = (
                descriptor.resolved_execution_runtime
                if descriptor is not None else ToolExecutionRuntime.LOCAL
            )
            return ToolExecutionResult(
                content=(
                    f"工具在本轮不可用: {tool_call.name}"
                    f"（{unavailable_reason}；当前已暴露工具: "
                    f"{', '.join(pool.advertised_names()) or '无'}）"
                ),
                is_error=True,
                runtime=runtime,
                diagnostics={
                    "source": (
                        descriptor.source.value
                        if descriptor is not None else "unknown"),
                    "runtime": runtime.value,
                    "error_kind": "tool_unavailable",
                },
            )
        if descriptor is None or prompt_spec is None:
            return ToolExecutionResult(
                content=(
                    f"未知工具: {tool_call.name}（本轮可用工具: "
                    f"{', '.join(pool.advertised_names()) or '无'}）"
                ),
                is_error=True,
                runtime=ToolExecutionRuntime.LOCAL,
                diagnostics={"error_kind": "unknown_tool"},
            )
        runtime = descriptor.resolved_execution_runtime
        if not pool.is_executable(tool_call.name):
            return ToolExecutionResult(
                content=(
                    f"工具不可执行: {tool_call.name}（本轮可用工具: "
                    f"{', '.join(pool.advertised_names()) or '无'}）"
                ),
                is_error=True,
                runtime=runtime,
                diagnostics={
                    "source": descriptor.source.value,
                    "runtime": runtime.value,
                    "error_kind": "tool_not_executable",
                },
            )

        async def emit_progress(
            message: str, data: Dict[str, object],
        ) -> None:
            redactor = deps.redactor
            safe_message = redactor.redact(message) if redactor else message
            safe_data = (
                cast(Dict[str, object], _redact_json(data, redactor.redact))
                if redactor is not None else data)
            await host._emit(ToolProgress(
                tool_call.id, tool_call.name, safe_message, safe_data))

        async def emit_output(text: str, stream: str) -> None:
            if not text:
                return
            redactor = deps.redactor
            safe_text = redactor.redact(text) if redactor else text
            if safe_text:
                await host._emit(ToolOutputDelta(
                    tool_call.id, tool_call.name, safe_text, stream))

        async def request_user_input(
            prompt: str,
            options: tuple[str, ...],
            allow_free_text: bool,
        ) -> Optional[str]:
            port = deps.user_input_port
            if port is None:
                return None
            redactor = deps.redactor
            safe_prompt = redactor.redact(prompt) if redactor else prompt
            safe_options = tuple(
                redactor.redact(option) if redactor else option
                for option in options
            )
            turn_id = host._turn_tool_context.runtime.turn_id or "turn"
            request_id = f"{turn_id}:input:{next(host._input_counter)}"
            exchange = port.submit(
                safe_prompt,
                safe_options,
                allow_free_text=allow_free_text,
                request_id=request_id,
            )
            await host._emit(UserInputRequested(
                request_id=exchange.request_id,
                prompt=safe_prompt,
                options=safe_options,
                allow_free_text=allow_free_text,
            ))
            answer = await exchange.wait()
            await host._emit(UserInputResolved(
                request_id=exchange.request_id,
                answered=answer is not None,
            ))
            return answer

        async def emit_plan(
            items: tuple[str, ...],
            statuses: tuple[str, ...],
            active_index: int,
            explanation: str,
        ) -> None:
            redactor = deps.redactor
            if redactor is not None:
                redact: Callable[[str], str] = redactor.redact
            else:
                redact = lambda text: text
            deps.working_context_store.record_plan(
                items,
                statuses,
                active_index=active_index,
                explanation=redact(explanation),
                turn_id=host._current_turn_id,
            )
            await host._emit(PlanUpdated(
                items=tuple(redact(item) for item in items),
                active_index=active_index,
                statuses=tuple(redact(status) for status in statuses),
                explanation=redact(explanation),
                turn_id=host._current_turn_id,
            ))

        execution_ctx = replace(
            host._turn_tool_context,
            tool_descriptor=descriptor,
            tool_catalog=pool,
            progress_emitter=emit_progress,
            output_emitter=emit_output,
            input_requester=request_user_input,
            plan_emitter=emit_plan,
        )
        intent = ToolIntent(
            call=tool_call,
            descriptor=descriptor,
            parameters=prompt_spec.parameters,
            context=execution_ctx,
        )

        if tool_call.has_invalid_arguments:
            return ToolExecutionResult(
                content=(
                    "工具参数不是合法 JSON: "
                    f"{tool_call.arguments[INVALID_ARGUMENTS_KEY]!r}"
                ),
                is_error=True,
                runtime=runtime,
                diagnostics={
                    "source": descriptor.source.value,
                    "runtime": runtime.value,
                    "error_kind": "invalid_arguments_json",
                },
            )
        validation_errors = validate_tool_arguments(
            intent.parameters, tool_call.arguments)
        if validation_errors:
            rendered = [error.render() for error in validation_errors[:20]]
            if len(validation_errors) > 20:
                rendered.append(f"…另有 {len(validation_errors) - 20} 个错误")
            return ToolExecutionResult(
                content=(
                    f"工具参数校验失败: {tool_call.name}\n- "
                    + "\n- ".join(rendered)
                ),
                is_error=True,
                runtime=runtime,
                diagnostics={
                    "source": descriptor.source.value,
                    "runtime": runtime.value,
                    "error_kind": "schema_validation",
                },
            )
        governance_request = _governance_request(
            host, execution_ctx, descriptor, tool_call)
        governance_decision = (
            await deps.control_plane.evaluate(governance_request)
            if deps.control_plane is not None
            else GovernanceDecision(
                allowed=True,
                policy_version=PolicyVersion(source="agent-local"),
                permission_mode=governance_request.runtime.permission_mode,
                tool_allowlist=governance_request.runtime.tool_allowlist,
                sandbox_profile=governance_request.sandbox_profile,
                audit=governance_request.audit,
            )
        )
        execution_ctx = replace(
            execution_ctx,
            runtime=replace(
                execution_ctx.runtime,
                permission_mode=governance_decision.permission_mode,
            ),
            governance_decision=governance_decision,
        )
        if not governance_decision.allowed:
            await _emit_security_decision(
                host,
                tool_call,
                governance_decision.security_decision,
            )
            return ToolExecutionResult(
                content=f"治理控制面拒绝工具执行: {governance_decision.reason}",
                is_error=True,
                runtime=runtime,
                diagnostics={
                    "source": descriptor.source.value,
                    "runtime": runtime.value,
                    "error_kind": "governance_denied",
                    "governance_decision": governance_decision.decision_hash,
                    "security_decision": (
                        governance_decision.security_decision.decision_hash),
                },
            )
        pre_hook = await host._before_tool_hook(tool_call)
        if pre_hook.blocked:
            security_decision = _security_decision(
                governance_decision,
                tool_call,
                decision=SecurityDecisionValue.DENY,
                policy_id="pre-tool-hook",
                reason=pre_hook.reason,
            )
            await _emit_security_decision(host, tool_call, security_decision)
            return ToolExecutionResult(
                content=f"PreToolUse Hook 阻止执行: {pre_hook.reason}",
                is_error=True,
                data={"hook_blocked": True, "hook_trigger": "pre_tool_use"},
                runtime=runtime,
                diagnostics={
                    "source": descriptor.source.value,
                    "runtime": runtime.value,
                    "error_kind": "hook_blocked",
                    "security_decision": security_decision.decision_hash,
                },
            )

        policy_id = "governance"
        approval_id = ""
        policy_reason = ""
        if deps.policy is not None:
            await execution_ctx.emit_progress("安全策略检查中")
            try:
                policy_decision = await deps.policy.check(
                    tool_call, execution_ctx)
            except Exception as e:
                await host._emit(ErrorOccurred(str(e), "policy.check"))
                await host._emit(PermissionDenied(tool_call, str(e)))
                security_decision = _security_decision(
                    governance_decision,
                    tool_call,
                    decision=SecurityDecisionValue.DENY,
                    policy_id="policy.check",
                    reason=str(e),
                )
                await _emit_security_decision(
                    host, tool_call, security_decision)
                return ToolExecutionResult(
                    content=f"审批检查失败，已拒绝执行: {e}",
                    is_error=True,
                    runtime=runtime,
                    diagnostics={
                        "source": descriptor.source.value,
                        "runtime": runtime.value,
                        "error_kind": "policy_exception",
                        "security_decision": security_decision.decision_hash,
                    },
                )
            policy_id = policy_decision.policy_id
            approval_id = policy_decision.approval_id
            policy_reason = policy_decision.reason
            if not policy_decision.allowed:
                await host._emit(PermissionDenied(
                    tool_call, policy_decision.reason))
                security_decision = _security_decision(
                    governance_decision,
                    tool_call,
                    decision=SecurityDecisionValue.DENY,
                    policy_id=policy_id,
                    approval_id=approval_id,
                    reason=policy_reason,
                )
                await _emit_security_decision(
                    host, tool_call, security_decision)
                return ToolExecutionResult(
                    content=f"操作被安全策略拒绝: {policy_decision.reason}",
                    is_error=True,
                    runtime=runtime,
                    diagnostics={
                        "source": descriptor.source.value,
                        "runtime": runtime.value,
                        "error_kind": "permission_denied",
                        "security_decision": security_decision.decision_hash,
                    },
                )

        security_decision = _security_decision(
            governance_decision,
            tool_call,
            decision=SecurityDecisionValue.ALLOW,
            policy_id=policy_id,
            approval_id=approval_id,
            reason=policy_reason or governance_decision.reason,
        )
        governance_decision = replace(
            governance_decision,
            security_decision=security_decision,
        )
        execution_ctx = replace(
            execution_ctx,
            governance_decision=governance_decision,
        )
        intent = ToolIntent(
            call=tool_call,
            descriptor=descriptor,
            parameters=prompt_spec.parameters,
            context=execution_ctx,
        )
        await _emit_security_decision(host, tool_call, security_decision)
        await execution_ctx.emit_progress("开始执行")
        result = await deps.execution_runtime.execute(intent)
        result = replace(
            result,
            diagnostics={
                **dict(result.diagnostics),
                "security_decision": security_decision.decision_hash,
            },
        )
        if result.diagnostics.get("error_kind") == "runtime_exception":
            await host._emit(ErrorOccurred(
                result.content.removeprefix("工具执行出错: "),
                tool_call.name,
            ))

        post_hook = await host._after_tool_hook(
            tool_call, result.to_tool_result())
        contexts = (*pre_hook.context, *post_hook.context)
        if contexts:
            blocks = "\n\n".join(
                f"<hook_context>\n{item}\n</hook_context>" for item in contexts)
            result = replace(
                result,
                content=(result.content + "\n\n" + blocks).strip(),
            )
        return result


def _security_decision(
    governance: GovernanceDecision,
    tool_call: ToolCall,
    *,
    decision: SecurityDecisionValue,
    policy_id: str,
    approval_id: str = "",
    reason: str = "",
) -> SecurityDecision:
    return SecurityDecision(
        policy_id=policy_id,
        policy_version=governance.policy_version.version,
        resource=f"tool:{tool_call.name}",
        action="tool.execute",
        decision=decision,
        reason=reason,
        approval_id=approval_id,
        sandbox_profile=governance.sandbox_profile,
        trace_id=(
            governance.audit.trace_id
            or governance.audit.correlation_id
        ),
        evidence={
            "governance_decision_hash": governance.decision_hash,
            "policy_organization": governance.policy_version.organization,
            "policy_digest": governance.policy_version.digest,
        },
    )


async def _emit_security_decision(
    host: ToolTurnHostPort,
    tool_call: ToolCall,
    decision: SecurityDecision,
) -> None:
    await host._emit(SecurityDecisionRecorded(
        tool_call_id=tool_call.id,
        security=decision.to_dict(),
    ))


def _governance_request(
    host: ToolTurnHostPort,
    context: ToolContext,
    descriptor: ToolDescriptor,
    tool_call: ToolCall,
) -> GovernanceRequest:
    runtime = context.runtime
    sandbox = context.sandbox
    plugin_name = descriptor.plugin_name or ""
    plugin_required = descriptor.source is ToolSource.PLUGIN
    resolution = host.prompt_profile_resolution
    release_id = str(getattr(resolution, "release_id", "") or "")
    release_hash = str(getattr(resolution, "release_hash", "") or "")
    permission_profile = str(
        tool_call.arguments.get("sandbox_permission") or "auto")
    sandbox_limits = getattr(sandbox, "resource_limits", None)
    return GovernanceRequest(
        identity=context.principal,
        enterprise_policy=context.enterprise_policy,
        runtime=runtime,
        action="tool.execute",
        resource="tool",
        tool_name=tool_call.name,
        tool_arguments=tool_call.arguments,
        workspace_root=context.workspace_root,
        sandbox_profile=SandboxProfile(
            name=f"runtime:{runtime.permission_mode.value}",
            backend=str(getattr(sandbox, "backend", "none")),
            enabled=context.sandbox_enabled,
            network=bool(getattr(sandbox, "network", False)),
            permission_profile=permission_profile,
            resource_limits=SandboxResourceLimits(
                cpu_seconds=getattr(sandbox_limits, "cpu_seconds", None),
                memory_mb=getattr(sandbox_limits, "memory_mb", None),
                storage_mb=getattr(sandbox_limits, "storage_mb", None),
                process_limit=getattr(sandbox_limits, "process_limit", None),
            ),
        ),
        plugin_trust=PluginTrustContext(
            name=plugin_name,
            required=plugin_required,
            allowed=True,
            trust_status="loaded" if plugin_required else "",
        ),
        prompt_release=PromptReleaseIdentity(
            release_id=release_id,
            release_hash=release_hash,
            required=bool(release_id and release_hash),
        ),
        audit=AuditCorrelation(
            correlation_id=f"{host._current_turn_id}:{tool_call.id}",
            session_id=runtime.session_id or "",
            turn_id=host._current_turn_id,
            request_id=tool_call.id,
        ),
    )


def _call_signature(tool_call: ToolCall) -> str:
    """Tool name plus stable arguments for repeated-failure detection."""
    try:
        args = json.dumps(tool_call.arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = repr(tool_call.arguments)
    if len(args) > 200:
        args = args[:200] + "…"
    return f"{tool_call.name}({args})"


def _redact_json(
    value: object, redact: Callable[[str], str],
) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_json(item, redact) for item in value]
    if isinstance(value, dict):
        return {
            str(redact(str(key))): _redact_json(item, redact)
            for key, item in value.items()
        }
    return value


def _persisted_output_path(content: str) -> str:
    match = re.search(r"全文已存盘:\s*([^\]\n]+)", content or "")
    return match.group(1).strip()[:1_000] if match else ""


__all__ = ["ToolBatchExecutor"]
