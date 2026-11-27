"""Stream-json 协议：SessionEvent 与 UTF-8 JSONL 消息之间的纯适配层。

协议是 CLI/SDK 的边界，不进入 AgentLoop。每行一个 JSON 对象，查询事件
保留 ``turn_id`` 与单调 ``sequence``；终态统一为 ``result``，因此上层无需
理解 Python 内部事件类。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from ..core.events import (
    AcceptanceEvaluated,
    ApprovalRequested,
    CompactCheckpointCreated,
    ContextAssembled,
    ContextCompressed,
    ContextDropped,
    ContextMicrocompacted,
    ErrorOccurred,
    HookTriggered,
    KnowledgeLoaded,
    LLMResponseReceived,
    LLMStreamDelta,
    LLMThinkingDelta,
    MemoryCandidateObserved,
    MemoryLoaded,
    MemoryWritten,
    PermissionDenied,
    PlanUpdated,
    QueryPhaseChanged,
    RunCancelled,
    RunFinished,
    RunStarted,
    SecurityDecisionRecorded,
    ShellJobFinished,
    ShellJobOutput,
    ShellJobStarted,
    StepStarted,
    SubagentFeedUpdated,
    SubagentFinished,
    SubagentSpawned,
    SubagentStateChanged,
    ToolCallCompleted,
    ToolCallStarted,
    ToolOutputDelta,
    ToolProgress,
    UserInputRequested,
    UserInputResolved,
    UserSteered,
)
from ..core.messages import ImageRef, Message, ToolCall
from ..agent.turn.query import QueryEvent
from ..contracts.application import (
    ApplicationRequest,
    ApplicationRequestFactory,
)
from .stream_json_schema import (
    STREAM_JSON_PROTOCOL_NAME as PROTOCOL_NAME,
    STREAM_JSON_PROTOCOL_VERSION as PROTOCOL_VERSION,
    StreamJsonSchemaError,
    validate_input_message,
    validate_stream_event,
)


class StreamJsonProtocolError(ValueError):
    """输入消息不符合 stream-json 协议。"""


@dataclass(frozen=True)
class StreamUserMessage:
    content: str
    request_id: str = ""
    images: tuple[ImageRef, ...] = ()
    application: Optional[ApplicationRequest] = field(
        default=None, compare=False)


@dataclass(frozen=True)
class StreamControlMessage:
    subtype: str
    request_id: str = ""
    decision: str = ""
    action: str = ""
    content: str = ""


@dataclass(frozen=True)
class StreamJsonMetadata:
    session_id: Optional[str]
    cwd: str
    provider: str
    model: str
    tools: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    permission_mode: str = "default"
    run_mode: str = "json"


class StreamJsonEncoder:
    """把内部事件编码为稳定的 JSON-compatible message。"""

    def __init__(self, metadata: StreamJsonMetadata) -> None:
        self.metadata = metadata

    def init_message(self) -> Dict[str, Any]:
        payload = {
            "type": "system",
            "subtype": "init",
            "protocol": PROTOCOL_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "session_id": self.metadata.session_id,
            "cwd": self.metadata.cwd,
            "provider": self.metadata.provider,
            "model": self.metadata.model,
            "mode": self.metadata.run_mode,
            "permission_mode": self.metadata.permission_mode,
            "tools": list(self.metadata.tools),
            "mcp_servers": list(self.metadata.mcp_servers),
        }
        validate_stream_event(payload)
        return payload

    def session_end_message(self, reason: str = "eof") -> Dict[str, Any]:
        payload = {
            "type": "system",
            "subtype": "session_end",
            "session_id": self.metadata.session_id,
            "reason": reason,
        }
        validate_stream_event(payload)
        return payload

    def encode(
        self,
        query_event: QueryEvent,
        *,
        result_extra: Optional[Mapping[str, Any]] = None,
        extra_fields: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        event = query_event.event
        payload: Dict[str, Any]
        terminal_fields: Optional[Dict[str, Any]] = None

        if isinstance(event, RunStarted):
            payload = {
                "type": "system",
                "subtype": "turn_started",
                "content": event.user_input,
                "application": dict(event.application),
            }
        elif isinstance(event, AcceptanceEvaluated):
            payload = {
                "type": "acceptance",
                "subtype": "evaluated",
                "acceptance": dict(event.acceptance),
            }
        elif isinstance(event, StepStarted):
            payload = {"type": "step", "subtype": "started", "step": event.step}
        elif isinstance(event, QueryPhaseChanged):
            payload = {
                "type": "query",
                "subtype": "phase_changed",
                "phase": event.phase,
                "detail": event.detail,
            }
        elif isinstance(event, LLMStreamDelta):
            payload = {
                "type": "assistant",
                "subtype": "text_delta",
                "text": event.text,
            }
        elif isinstance(event, LLMThinkingDelta):
            payload = {
                "type": "assistant",
                "subtype": "thinking_delta",
                "text": event.text,
            }
        elif isinstance(event, LLMResponseReceived):
            payload = {
                "type": "assistant",
                "subtype": "message",
                "message": _message_payload(event.message),
                "usage": dict(event.usage),
            }
        elif isinstance(event, ToolCallStarted):
            payload = {
                "type": "tool",
                "subtype": "started",
                "tool_call": _tool_call_payload(event.tool_call),
            }
        elif isinstance(event, ToolProgress):
            payload = {
                "type": "tool",
                "subtype": "progress",
                "tool_call_id": event.tool_call_id,
                "tool_name": event.tool_name,
                "message": event.message,
                "data": dict(event.data),
            }
        elif isinstance(event, ToolOutputDelta):
            payload = {
                "type": "tool",
                "subtype": "output_delta",
                "tool_call_id": event.tool_call_id,
                "tool_name": event.tool_name,
                "text": event.text,
                "stream": event.stream,
            }
        elif isinstance(event, ToolCallCompleted):
            payload = {
                "type": "tool",
                "subtype": "completed",
                "tool_call": _tool_call_payload(event.tool_call),
                "content": event.content,
                "is_error": event.is_error,
                "data": dict(event.data),
            }
        elif isinstance(event, ApprovalRequested):
            payload = {
                "type": "permission",
                "subtype": "requested",
                "tool_call": _tool_call_payload(event.tool_call),
                "reason": event.reason,
                "permission_id": event.request_id or event.tool_call.id,
            }
        elif isinstance(event, PermissionDenied):
            payload = {
                "type": "permission",
                "subtype": "denied",
                "tool_call": _tool_call_payload(event.tool_call),
                "reason": event.reason,
            }
        elif isinstance(event, SecurityDecisionRecorded):
            payload = {
                "type": "permission",
                "subtype": "security_decision",
                "tool_call_id": event.tool_call_id,
                "security": dict(event.security),
            }
        elif isinstance(event, ContextAssembled):
            payload = {
                "type": "context",
                "subtype": "assembled",
                "event": "context_assembled",
                "snapshot": event.snapshot.as_record(),
            }
            if event.ledger is not None:
                payload["ledger"] = dict(event.ledger)
        elif isinstance(event, ContextDropped):
            payload = {
                "type": "context",
                "subtype": "dropped",
                "event": "context_dropped",
                "items": list(event.items),
                "reason": event.reason,
            }
        elif isinstance(event, MemoryLoaded):
            payload = {
                "type": "memory",
                "subtype": "loaded",
                "event": "memory_loaded",
                "entry_ids": list(event.entry_ids),
                "scope_counts": dict(event.scope_counts),
                "tokens": event.tokens,
                "source": event.source,
            }
        elif isinstance(event, KnowledgeLoaded):
            payload = {
                "type": "knowledge",
                "subtype": "loaded",
                "event": "knowledge_loaded",
                "document_ids": list(event.document_ids),
                "chunk_ids": list(event.chunk_ids),
                "sources": list(event.sources),
                "scope_counts": dict(event.scope_counts),
                "tokens": event.tokens,
            }
        elif isinstance(event, MemoryCandidateObserved):
            payload = {
                "type": "memory",
                "subtype": "candidate",
                "event": "memory_candidate",
                "candidate_id": event.candidate_id,
                "status": event.status,
                "trigger": event.trigger,
                "confidence": event.confidence,
                "conflict_count": event.conflict_count,
                "digest": event.digest,
            }
        elif isinstance(event, MemoryWritten):
            payload = {
                "type": "memory",
                "subtype": "written",
                "event": "memory_written",
                "memory_id": event.memory_id,
                "action": event.action,
                "scope": event.scope,
                "kind": event.kind,
                "tokens": event.tokens,
                "source": event.source,
            }
        elif isinstance(event, CompactCheckpointCreated):
            payload = {
                "type": "compact",
                "subtype": "checkpoint",
                "event": "compact_checkpoint",
                "checkpoint_id": event.checkpoint_id,
                "generation": event.generation,
                "before_tokens": event.before_tokens,
                "after_tokens": event.after_tokens,
                "summary_digest": event.summary_digest,
                "strategy": event.strategy,
                "trigger": event.trigger,
                "source_savings": dict(event.source_savings),
            }
        elif isinstance(event, ContextMicrocompacted):
            payload = {
                "type": "context",
                "subtype": "microcompacted",
                "trigger": event.trigger,
                "before_tokens": event.before_tokens,
                "after_tokens": event.after_tokens,
                "compacted_count": event.compacted_count,
                "preserved_count": event.preserved_count,
                "protected_count": event.protected_count,
                "summarized_count": event.summarized_count,
                "referenced_count": event.referenced_count,
            }
        elif isinstance(event, ContextCompressed):
            payload = {
                "type": "context",
                "subtype": "compressed",
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
        elif isinstance(event, HookTriggered):
            payload = {
                "type": "hook",
                "subtype": "triggered",
                "trigger": event.trigger,
                "matched": event.matched,
                "blocked": event.blocked,
                "tool": event.tool_name,
                "reason": event.reason,
                "system_message": event.system_message,
            }
        elif isinstance(event, (ShellJobStarted, ShellJobOutput, ShellJobFinished)):
            payload = self._job_payload(event)
        elif isinstance(event, SubagentSpawned):
            payload = {
                "type": "subagent",
                "subtype": "started",
                "agent_id": event.agent_id,
                "task": event.task,
                "definition": event.definition,
            }
        elif isinstance(event, SubagentFeedUpdated):
            payload = {
                "type": "subagent",
                "subtype": "feed",
                "agent_id": event.agent_id,
                "agent_sequence": event.sequence,
                "kind": event.kind,
                "text": event.text,
                "data": event.data,
            }
        elif isinstance(event, SubagentStateChanged):
            payload = {
                "type": "subagent",
                "subtype": "state",
                "agent_id": event.agent_id,
                "status": event.status,
                "reason": event.reason,
            }
        elif isinstance(event, SubagentFinished):
            payload = {
                "type": "subagent",
                "subtype": "finished",
                "agent_id": event.agent_id,
                "status": event.status,
                "preview": event.preview,
            }
        elif isinstance(event, UserSteered):
            payload = {
                "type": "user",
                "subtype": "steered",
                "content": event.text,
            }
        elif isinstance(event, PlanUpdated):
            payload = {
                "type": "plan",
                "subtype": "updated",
                "items": list(event.items),
                "active_index": event.active_index,
                "statuses": list(event.statuses),
                "explanation": event.explanation,
            }
        elif isinstance(event, UserInputRequested):
            payload = {
                "type": "input",
                "subtype": "requested",
                "input_id": event.request_id,
                "prompt": event.prompt,
                "options": list(event.options),
                "allow_free_text": event.allow_free_text,
            }
        elif isinstance(event, UserInputResolved):
            payload = {
                "type": "input",
                "subtype": "resolved",
                "input_id": event.request_id,
                "answered": event.answered,
            }
        elif isinstance(event, RunFinished):
            terminal_fields = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": event.final_text,
            }
            payload = {**terminal_fields, **dict(result_extra or {})}
        elif isinstance(event, RunCancelled):
            terminal_fields = {
                "type": "result",
                "subtype": "cancelled",
                "is_error": True,
                "result": event.reason,
            }
            payload = {**terminal_fields, **dict(result_extra or {})}
        elif isinstance(event, ErrorOccurred):
            payload = {
                "type": "error",
                "subtype": "occurred",
                "error": event.error,
                "context": event.context,
            }
        else:
            payload = {
                "type": "event",
                "subtype": type(event).__name__,
            }

        if extra_fields:
            payload.update(dict(extra_fields))
        if terminal_fields is not None:
            # result 的语义字段不能由 request_id 或其他扩展元数据覆盖。
            payload.update(terminal_fields)
        # Envelope 字段最后写入，扩展字段不能伪造顺序或 turn 身份。
        payload.update({
            "turn_id": query_event.turn_id,
            "sequence": query_event.sequence,
            "session_id": self.metadata.session_id,
        })
        validate_stream_event(payload)
        return payload

    def job_event_message(self, event: ShellJobStarted | ShellJobOutput | ShellJobFinished) -> Dict[str, Any]:
        payload = self._job_payload(event)
        if isinstance(event, ShellJobOutput):
            # Job 自身 cursor 使用 sequence；QueryEvent envelope 中则保留 job_sequence
            # 以免两者在 encoder.encode() 中互相覆盖。
            payload["sequence"] = event.sequence
        payload["session_id"] = self.metadata.session_id
        validate_stream_event(payload)
        return payload

    @staticmethod
    def _job_payload(
        event: ShellJobStarted | ShellJobOutput | ShellJobFinished,
    ) -> Dict[str, Any]:
        if isinstance(event, ShellJobStarted):
            return {
                "type": "job",
                "subtype": "started",
                "job_id": event.job_id,
                "command": event.command,
                "pid": event.pid,
                "cwd": event.cwd,
            }
        if isinstance(event, ShellJobOutput):
            return {
                "type": "job",
                "subtype": "output_delta",
                "job_id": event.job_id,
                "job_sequence": event.sequence,
                "text": event.text,
            }
        return {
            "type": "job",
            "subtype": "finished",
            "job_id": event.job_id,
            "command": event.command,
            "status": event.status,
            "exit_code": event.exit_code,
            "preview": event.preview,
        }

    def result_message(
        self,
        *,
        subtype: str,
        result: str,
        is_error: bool,
        turn_id: str = "",
        sequence: Optional[int] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "type": "result",
            "subtype": subtype,
            "is_error": is_error,
            "result": result,
            "turn_id": turn_id,
            "session_id": self.metadata.session_id,
        }
        if sequence is not None:
            payload["sequence"] = sequence
        payload.update(dict(extra or {}))
        # 终态与 envelope 字段不可被额外元数据覆盖。
        payload.update({
            "type": "result",
            "subtype": subtype,
            "is_error": is_error,
            "result": result,
            "turn_id": turn_id,
            "session_id": self.metadata.session_id,
        })
        if sequence is not None:
            payload["sequence"] = sequence
        validate_stream_event(payload)
        return payload


def dumps_message(message: Mapping[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def parse_input_line(line: str) -> StreamUserMessage | StreamControlMessage:
    """解析 stdin 的一行；兼容 string 与 content-block 形式的内容。"""
    text = line.strip()
    if not text:
        raise StreamJsonProtocolError("空输入行")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StreamJsonProtocolError(f"JSON 解析失败: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise StreamJsonProtocolError("输入必须是 JSON 对象")

    message_type = value.get("type")
    if message_type == "control":
        subtype = str(value.get("subtype", "")).strip()
        if subtype in {"end", "interrupt"}:
            _validate_input_envelope(value)
            return StreamControlMessage(subtype)
        if subtype == "permission_response":
            request_id = _control_request_id(value, subtype)
            decision = str(value.get("decision", "")).strip().lower()
            if decision not in {"allow", "deny", "always"}:
                raise StreamJsonProtocolError(
                    "permission_response.decision 必须为 allow/deny/always")
            _validate_input_envelope(value)
            return StreamControlMessage(
                subtype=subtype,
                request_id=request_id,
                decision=decision,
            )
        if subtype == "user_input_response":
            request_id = _control_request_id(value, subtype)
            action = str(value.get("action", "")).strip().lower()
            if action not in {"accept", "cancel"}:
                raise StreamJsonProtocolError(
                    "user_input_response.action 必须为 accept/cancel")
            content = value.get("content", "")
            if content is None:
                content = ""
            if not isinstance(content, str):
                raise StreamJsonProtocolError(
                    "user_input_response.content 必须是字符串")
            content = content.strip()
            if len(content) > 32_000:
                raise StreamJsonProtocolError(
                    "user_input_response.content 超过 32000 字符限制")
            if action == "accept" and not content:
                raise StreamJsonProtocolError(
                    "user_input_response.accept 需要非空 content")
            if action == "cancel" and content:
                raise StreamJsonProtocolError(
                    "user_input_response.cancel 不能携带 content")
            _validate_input_envelope(value)
            return StreamControlMessage(
                subtype=subtype,
                request_id=request_id,
                action=action,
                content=content,
            )
        else:
            raise StreamJsonProtocolError(f"未知 control.subtype: {subtype!r}")
    if message_type != "user":
        raise StreamJsonProtocolError("type 必须为 user 或 control")

    envelope = value.get("message")
    if envelope is not None:
        if not isinstance(envelope, dict):
            raise StreamJsonProtocolError("message 必须是对象")
        if envelope.get("role", "user") != "user":
            raise StreamJsonProtocolError("message.role 必须为 user")
        raw_content = envelope.get("content")
        request_id = str(
            value.get("request_id", envelope.get("request_id", ""))).strip()
        raw_application = value.get(
            "application", envelope.get("application"))
    else:
        raw_content = value.get("content")
        request_id = str(value.get("request_id", "")).strip()
        raw_application = value.get("application")

    content, images = _parse_content(raw_content)
    if not content.strip() and not images:
        raise StreamJsonProtocolError("user.content 不能为空")
    application = _parse_application(
        raw_application,
        content=content,
        request_id=request_id,
    )
    _validate_input_envelope(value)
    return StreamUserMessage(
        content=content,
        request_id=request_id,
        images=tuple(images),
        application=application,
    )


def _validate_input_envelope(value: Mapping[str, Any]) -> None:
    try:
        validate_input_message(value)
    except StreamJsonSchemaError as exc:
        raise StreamJsonProtocolError(str(exc)) from exc


def _control_request_id(value: Mapping[str, Any], subtype: str) -> str:
    request_id = str(value.get("request_id", "")).strip()
    if not request_id:
        raise StreamJsonProtocolError(f"{subtype}.request_id 不能为空")
    if len(request_id) > 512:
        raise StreamJsonProtocolError(f"{subtype}.request_id 超过长度限制")
    return request_id


def _parse_content(raw_content: Any) -> tuple[str, list[ImageRef]]:
    if isinstance(raw_content, str):
        return raw_content, []
    if not isinstance(raw_content, list):
        raise StreamJsonProtocolError("user.content 必须是字符串或 content block 数组")

    text_parts: list[str] = []
    images: list[ImageRef] = []
    for block in raw_content:
        if isinstance(block, str):
            text_parts.append(block)
            continue
        if not isinstance(block, dict):
            raise StreamJsonProtocolError("content block 必须是对象")
        block_type = block.get("type")
        if block_type in {"text", "input_text"}:
            text = block.get("text")
            if not isinstance(text, str):
                raise StreamJsonProtocolError("text block 缺少 text 字符串")
            text_parts.append(text)
            continue
        if block_type == "image":
            source = block.get("source")
            if isinstance(source, dict):
                media_type = source.get("media_type")
                data_b64 = source.get("data")
                source_path = source.get("path")
            else:
                media_type = block.get("media_type")
                data_b64 = block.get("data")
                source_path = block.get("source_path")
            if not isinstance(media_type, str) or not isinstance(data_b64, str):
                raise StreamJsonProtocolError("image block 缺少 media_type/data")
            images.append(ImageRef(
                media_type=media_type,
                data_b64=data_b64,
                source_path=str(source_path) if source_path else None,
            ))
            continue
        raise StreamJsonProtocolError(f"不支持的 content block: {block_type!r}")
    return "\n".join(text_parts), images


def _parse_application(
    raw: object,
    *,
    content: str,
    request_id: str,
) -> Optional[ApplicationRequest]:
    if raw is not None:
        if not isinstance(raw, Mapping):
            raise StreamJsonProtocolError("application 必须是对象")
        try:
            return ApplicationRequest.from_mapping(raw)
        except (TypeError, ValueError) as exc:
            raise StreamJsonProtocolError(
                f"application 无效: {exc}") from exc
    return ApplicationRequestFactory.from_input_source(
        "stream-json",
        goal=content or "[image attachment]",
        metadata={"request_id": request_id} if request_id else None,
    )


def _tool_call_payload(tool_call: ToolCall) -> Dict[str, Any]:
    return {
        "id": tool_call.id,
        "name": tool_call.name,
        "arguments": dict(tool_call.arguments),
    }


def _message_payload(message: Message) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "role": message.role,
        "content": message.content,
    }
    if message.tool_calls:
        payload["tool_calls"] = [
            _tool_call_payload(tool_call) for tool_call in message.tool_calls
        ]
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    return payload
