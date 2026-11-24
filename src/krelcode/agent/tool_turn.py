"""Tool execution side of an agent turn.

``ToolBatchExecutor`` owns the safety and state boundaries around tool calls:
stream scheduling, schema validation, approval, hooks, output redaction,
large-output persistence, result events and history/working-context writes.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Dict, List, Optional

from ..core.events import (
    ErrorOccurred,
    PermissionDenied,
    PlanUpdated,
    ToolCallCompleted,
    ToolCallStarted,
    ToolOutputDelta,
    ToolProgress,
    UserInputRequested,
    UserInputResolved,
)
from ..core.messages import (
    INVALID_ARGUMENTS_KEY,
    ImageBlock,
    Message,
    ResourceBlock,
    StructuredBlock,
    TextBlock,
    ToolCall,
)
from ..core.textutil import truncate_middle
from ..contracts.tool import ToolResult
from ..tools.schema import validate_tool_arguments
from .dependencies import AgentDependencies
from .runtime_ports import ToolTurnHostPort
from .streaming_tools import StreamingToolExecutor
from .tool_pool import TurnToolPool
from .working_context import compact_tool_metadata


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
        results: List[ToolResult],
    ) -> Optional[str]:
        """Append results in model order and return a loop-guard reason."""
        host = self.host
        redactor = self.dependencies.redactor
        for tool_call, result in zip(tool_calls, results):
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

    async def run_stream_tool(self, tool_call: ToolCall) -> ToolResult:
        """Execute one streamed call and publish completion immediately."""
        await self.host._emit(ToolCallStarted(tool_call))
        result = await self.execute_one(tool_call)
        await self.publish_tool_result(tool_call, result)
        return result

    async def publish_tool_result(
        self, tool_call: ToolCall, result: ToolResult,
    ) -> None:
        """Redact, persist and publish a tool result as soon as it completes."""
        redactor = self.dependencies.redactor
        content = redactor.redact(result.content) if redactor else result.content
        inline_content = self.persist_large_output(tool_call, content)
        result.content = inline_content
        if redactor is not None:
            result.blocks = tuple(
                self._redact_tool_block(block) for block in result.blocks)
            result.data = _redact_json(result.data, redactor.redact)
        await self.host._emit(
            ToolCallCompleted(
                tool_call,
                inline_content,
                result.is_error,
                result.blocks,
                dict(result.data),
            )
        )

    def _redact_tool_block(self, block):
        redact = self.dependencies.redactor.redact
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
            return StructuredBlock(_redact_json(block.data, redact))
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

    async def execute_one(self, tool_call: ToolCall) -> ToolResult:
        host = self.host
        deps = self.dependencies
        pool = host._current_tool_pool()
        tool = pool.get(tool_call.name)
        if tool is None or not pool.is_executable(tool_call.name):
            reason = pool.unavailable_reason(tool_call.name)
            if reason is not None:
                return ToolResult(
                    content=(
                        f"工具在本轮不可用: {tool_call.name}（{reason}；"
                        "当前已暴露工具: "
                        f"{', '.join(pool.advertised_names()) or '无'}）"
                    ),
                    is_error=True,
                )
            return ToolResult(
                content=(
                    f"未知工具: {tool_call.name}（本轮可用工具: "
                    f"{', '.join(pool.advertised_names()) or '无'}）"
                ),
                is_error=True,
            )

        async def emit_progress(
            message: str, data: Dict[str, object],
        ) -> None:
            redactor = deps.redactor
            safe_message = redactor.redact(message) if redactor else message
            safe_data = (
                _redact_json(data, redactor.redact)
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
            redact = redactor.redact if redactor else (lambda text: text)
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
            tool_descriptor=pool.descriptor(tool_call.name),
            tool_catalog=pool,
            progress_emitter=emit_progress,
            output_emitter=emit_output,
            input_requester=request_user_input,
            plan_emitter=emit_plan,
        )

        if tool_call.has_invalid_arguments:
            return ToolResult(
                content=(
                    "工具参数不是合法 JSON: "
                    f"{tool_call.arguments[INVALID_ARGUMENTS_KEY]!r}"
                ),
                is_error=True,
            )
        validation_errors = validate_tool_arguments(
            tool.parameters, tool_call.arguments)
        if validation_errors:
            rendered = [error.render() for error in validation_errors[:20]]
            if len(validation_errors) > 20:
                rendered.append(f"…另有 {len(validation_errors) - 20} 个错误")
            return ToolResult(
                content=(
                    f"工具参数校验失败: {tool_call.name}\n- "
                    + "\n- ".join(rendered)
                ),
                is_error=True,
            )
        args = dict(tool_call.arguments)
        pre_hook = await host._before_tool_hook(tool_call)
        if pre_hook.blocked:
            return ToolResult(
                content=f"PreToolUse Hook 阻止执行: {pre_hook.reason}",
                is_error=True,
                data={"hook_blocked": True, "hook_trigger": "pre_tool_use"},
            )

        if deps.policy is not None:
            await execution_ctx.emit_progress("安全策略检查中")
            try:
                decision = await deps.policy.check(tool_call, execution_ctx)
            except Exception as e:
                await host._emit(ErrorOccurred(str(e), "policy.check"))
                await host._emit(PermissionDenied(tool_call, str(e)))
                return ToolResult(
                    content=f"审批检查失败，已拒绝执行: {e}", is_error=True)
            if not decision.allowed:
                await host._emit(PermissionDenied(tool_call, decision.reason))
                return ToolResult(
                    content=f"操作被安全策略拒绝: {decision.reason}",
                    is_error=True,
                )

        await execution_ctx.emit_progress("开始执行")
        try:
            result = await tool.execute(args, execution_ctx)
        except Exception as e:
            await host._emit(ErrorOccurred(
                f"{type(e).__name__}: {e}", tool_call.name))
            result = ToolResult(
                content=f"工具执行出错: {type(e).__name__}: {e}",
                is_error=True,
            )

        post_hook = await host._after_tool_hook(tool_call, result)
        contexts = (*pre_hook.context, *post_hook.context)
        if contexts:
            blocks = "\n\n".join(
                f"<hook_context>\n{item}\n</hook_context>" for item in contexts)
            result.content = (result.content + "\n\n" + blocks).strip()
        return result


def _call_signature(tool_call: ToolCall) -> str:
    """Tool name plus stable arguments for repeated-failure detection."""
    try:
        args = json.dumps(tool_call.arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = repr(tool_call.arguments)
    if len(args) > 200:
        args = args[:200] + "…"
    return f"{tool_call.name}({args})"


def _redact_json(value, redact):
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
