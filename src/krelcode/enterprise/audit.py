"""Append-only compliance audit log.

This is intentionally separate from the resumable session recorder.  Session
JSONL is optimized for replay and may be deleted/archived with a session;
enterprise audit evidence is append-only, sequence-numbered and hash-chained.

The writer records content-light, redacted event summaries.  It never copies
credentials, access tokens or full tool output into the audit channel, and the
``required`` mode is fail-closed: if an audit append cannot be persisted the
originating agent run fails instead of executing without evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from ..core.events import (
    AgentEvent,
    ApprovalRequested,
    ErrorOccurred,
    PermissionDenied,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from ..core.events import EventBus
from ..core.redact import SecretRedactor


AUDIT_SCHEMA = 1
AUDIT_MODES = ("off", "hash-chain", "hmac")
DEFAULT_MAX_VALUE_CHARS = 4_000
_SENSITIVE_KEY_RE = __import__("re").compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|client[_-]?secret|secret|password|passwd|cookie|"
    r"authorization|proxy[_-]?authorization|private[_-]?key)"
)


class AuditError(RuntimeError):
    """Audit initialization/append/verification failure."""


def _safe_text(value: Any, *, redactor: SecretRedactor,
               max_chars: int = DEFAULT_MAX_VALUE_CHARS) -> str:
    text = str(value if value is not None else "")
    text = redactor.redact(text)
    if len(text) > max_chars:
        text = text[:max_chars] + f"…（截断 {len(text) - max_chars} chars）"
    return text


def _redact_value(value: Any, redactor: SecretRedactor, *, depth: int = 0) -> Any:
    """Redact string leaves while retaining JSON-safe structure."""
    if depth > 12:
        return "[depth-limit]"
    if isinstance(value, str):
        return _safe_text(value, redactor=redactor)
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = _safe_text(raw_key, redactor=redactor, max_chars=120)
            if _SENSITIVE_KEY_RE.search(str(raw_key)):
                result[key] = "***"
            else:
                result[key] = _redact_value(
                    raw_value, redactor, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _redact_value(item, redactor, depth=depth + 1)
            for item in value
        ]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _safe_text(value, redactor=redactor)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise AuditError(f"审计布尔值无效: {value!r}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _digest(prev_hash: str, payload: Mapping[str, Any]) -> str:
    material = prev_hash.encode("ascii") + canonical_json(payload).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _hmac_digest(prev_hash: str, payload: Mapping[str, Any], key: bytes) -> str:
    material = prev_hash.encode("ascii") + canonical_json(payload).encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class AuditConfig:
    enabled: bool = False
    required: bool = False
    path: Path = Path(".krelcode/audit/audit.jsonl")
    mode: str = "hash-chain"
    hmac_key_env: str = ""
    max_record_bytes: int = 64_000

    @classmethod
    def from_mapping(
        cls,
        value: Optional[Mapping[str, Any]],
        *,
        workspace: Optional[Path] = None,
    ) -> "AuditConfig":
        raw = value or {}
        if not isinstance(raw, Mapping):
            raise AuditError("enterprise.audit 必须是 TOML table")
        enabled = _as_bool(raw.get("enabled"), False)
        required = _as_bool(raw.get("required"), False)
        mode = str(raw.get("mode", "hash-chain") or "hash-chain").lower()
        if mode not in AUDIT_MODES:
            raise AuditError("enterprise.audit.mode 必须是 off/hash-chain/hmac")
        if required and not enabled:
            raise AuditError("enterprise.audit.required=true 时必须启用 audit.enabled")
        if required and mode == "off":
            raise AuditError("enterprise.audit.required=true 时 mode 不能为 off")
        path_raw = str(raw.get("path", ".krelcode/audit/audit.jsonl") or
                       ".krelcode/audit/audit.jsonl")
        path = Path(path_raw).expanduser()
        if not path.is_absolute():
            path = Path(workspace or Path.cwd()).expanduser().resolve() / path
        try:
            max_bytes = max(1_024, int(raw.get("max_record_bytes", 64_000)))
        except (TypeError, ValueError) as exc:
            raise AuditError("enterprise.audit.max_record_bytes 必须是整数") from exc
        return cls(
            enabled=enabled,
            required=required,
            path=path,
            mode=mode,
            hmac_key_env=str(raw.get("hmac_key_env", "") or "").strip(),
            max_record_bytes=max_bytes,
        )

    @property
    def enabled_and_not_off(self) -> bool:
        return self.enabled and self.mode != "off"


@dataclass(frozen=True)
class AuditVerification:
    valid: bool
    records: int
    path: Path
    first_error: str = ""
    last_hash: str = ""
    last_sequence: int = 0

    def as_record(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "records": self.records,
            "path": str(self.path),
            "first_error": self.first_error,
            "last_hash": self.last_hash,
            "last_sequence": self.last_sequence,
        }


def _last_chain_state(path: Path) -> tuple[int, str]:
    if not path.is_file():
        return 0, "0" * 64
    last_valid: Optional[Dict[str, Any]] = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                parsed = json.loads(line)
                if isinstance(parsed, dict) and parsed.get("hash"):
                    last_valid = parsed
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"审计文件初始化失败: {path}: {exc}") from exc
    if last_valid is None:
        return 0, "0" * 64
    try:
        sequence = int(last_valid.get("seq", 0))
        digest = str(last_valid["hash"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuditError(f"审计文件最后记录无效: {path}") from exc
    return max(0, sequence), digest


class ComplianceAuditor:
    """An EventBus subscriber that writes an append-only audit chain."""

    def __init__(
        self,
        path: Path,
        *,
        organization: str = "",
        principal: str = "",
        session_id: str = "",
        policy_version: str = "",
        config: Optional[AuditConfig] = None,
        redactor: Optional[SecretRedactor] = None,
        hmac_key: Optional[bytes] = None,
        required: bool = False,
        enabled: bool = True,
        max_record_bytes: int = 64_000,
    ) -> None:
        self.path = Path(path)
        self.organization = str(organization)
        self.principal = str(principal)
        self.session_id = str(session_id)
        self.policy_version = str(policy_version)
        self.config = config or AuditConfig(
            enabled=enabled, required=required, path=self.path,
            mode="hash-chain", max_record_bytes=max_record_bytes)
        self.redactor = redactor or SecretRedactor()
        self.hmac_key = hmac_key
        self.required = bool(required or self.config.required)
        self.enabled = bool(
            enabled and self.config.enabled and self.config.mode != "off")
        self.max_record_bytes = max(1_024, int(max_record_bytes or 64_000))
        self._unsub: Optional[object] = None
        self._lock = threading.Lock()
        self._sequence = 0
        self._prev_hash = "0" * 64
        self._initialized = False

    @classmethod
    def from_config(
        cls,
        value: Optional[Mapping[str, Any]],
        *,
        workspace: Path,
        organization: str = "",
        principal: str = "",
        session_id: str = "",
        policy_version: str = "",
        redactor: Optional[SecretRedactor] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> "ComplianceAuditor":
        config = AuditConfig.from_mapping(value, workspace=workspace)
        env = os.environ if environ is None else environ
        key = None
        if config.mode == "hmac":
            env_name = config.hmac_key_env
            raw_key = str(env.get(env_name, "") or "")
            if not raw_key:
                raise AuditError(
                    f"启用 HMAC 审计但环境变量 {env_name} 未设置")
            key = raw_key.encode("utf-8")
            if redactor is not None:
                redactor.add(raw_key)
        return cls(
            config.path,
            organization=organization,
            principal=principal,
            session_id=session_id,
            policy_version=policy_version,
            config=config,
            redactor=redactor,
            hmac_key=key,
            required=config.required,
            enabled=config.enabled and config.mode != "off",
        )

    def initialize(self) -> None:
        if self._initialized:
            return
        if not self.enabled:
            self._initialized = True
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
            if self.path.is_file() and self.path.stat().st_size:
                verification = verify_audit_file(
                    self.path, hmac_key=self.hmac_key)
                if not verification.valid:
                    raise AuditError(
                        f"审计日志完整性校验失败: {verification.first_error}")
                self._sequence = verification.last_sequence
                self._prev_hash = verification.last_hash or ("0" * 64)
                with self.path.open("rb") as handle:
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        with self.path.open("ab") as append:
                            append.write(b"\n")
            else:
                self._sequence, self._prev_hash = 0, "0" * 64
            self._initialized = True
        except Exception as exc:
            if self.required:
                raise AuditError(f"审计日志无法初始化: {self.path}: {exc}") from exc
            self.enabled = False

    def attach(self, bus: EventBus) -> None:
        self.initialize()
        self._unsub = bus.subscribe(self.on_event)
        if self.enabled:
            self.record("audit_started", {
                "organization": self.organization,
                "policy_version": self.policy_version,
                "session_id": self.session_id,
            })

    def detach(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def on_event(self, event: AgentEvent) -> None:
        event_session = str(getattr(event, "session_id", "") or "")
        if event_session:
            self.session_id = event_session
        record = _event_audit_record(event, self.redactor)
        if record is None:
            return
        self.record(record[0], record[1])

    def _payload(
        self,
        event_type: str,
        data: Mapping[str, Any],
        *,
        timestamp: Optional[float] = None,
    ) -> Dict[str, Any]:
        return {
            "schema": AUDIT_SCHEMA,
            "seq": self._sequence + 1,
            "event_id": uuid.uuid4().hex,
            "ts": time.time() if timestamp is None else float(timestamp),
            "org": self.organization,
            "policy_version": self.policy_version,
            "principal": self.principal,
            "session_id": self.session_id,
            "type": str(event_type),
            "data": _redact_value(data, self.redactor),
        }

    def record(
        self,
        event_type: str,
        data: Optional[Mapping[str, Any]] = None,
        *,
        timestamp: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Append one record; return the written record when enabled."""
        if not self.enabled:
            return None
        with self._lock:
            if not self._initialized:
                self.initialize()
            payload = self._payload(event_type, data or {}, timestamp=timestamp)
            prev_hash = self._prev_hash
            payload["prev_hash"] = prev_hash
            if self.config.mode == "hmac":
                if self.hmac_key is None:
                    if self.required:
                        raise AuditError("HMAC 审计缺少密钥")
                    self.enabled = False
                    return None
                base_payload = {
                    key: value for key, value in payload.items()
                    if key not in {"hash", "hmac"}
                }
                payload["hmac"] = _hmac_digest(
                    prev_hash, base_payload, self.hmac_key)
                # Keep a normal hash chain too; it lets offline clients verify
                # ordering even when they intentionally omit the HMAC key.
                payload["hash"] = _digest(prev_hash, base_payload)
            else:
                payload["hash"] = _digest(prev_hash, {
                    key: value for key, value in payload.items()
                    if key != "hash"
                })
            line = canonical_json(payload)
            if len(line.encode("utf-8")) > self.max_record_bytes:
                if self.required:
                    raise AuditError("审计记录超过 max_record_bytes")
                return None
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            except OSError as exc:
                if self.required:
                    raise AuditError(f"审计记录写入失败: {self.path}: {exc}") from exc
                self.enabled = False
                return None
            self._sequence = int(payload["seq"])
            self._prev_hash = str(payload["hash"])
            return payload

    def export(self, destination: Path) -> Path:
        """Copy the audit chain to an explicitly requested export path."""
        destination = Path(destination).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            data = self.path.read_bytes() if self.path.is_file() else b""
            destination.write_bytes(data)
            os.chmod(destination, 0o600)
        except OSError as exc:
            raise AuditError(f"审计导出失败: {destination}: {exc}") from exc
        return destination


def _event_audit_record(
    event: AgentEvent, redactor: SecretRedactor,
) -> Optional[tuple[str, Dict[str, Any]]]:
    """Map typed agent events to content-light audit summaries."""
    if isinstance(event, RunStarted):
        return "user_input", {
            "turn_id": event.turn_id,
            "session_id": event.session_id,
            "content": _safe_text(event.user_input, redactor=redactor),
        }
    if isinstance(event, ToolCallStarted):
        return "tool_call", {
            "tool_call_id": event.tool_call.id,
            "tool": event.tool_call.name,
            "arguments": _redact_value(event.tool_call.arguments, redactor),
        }
    if isinstance(event, ToolCallCompleted):
        return "tool_result", {
            "tool_call_id": event.tool_call.id,
            "tool": event.tool_call.name,
            "is_error": bool(event.is_error),
            "content": _safe_text(event.content, redactor=redactor),
        }
    if isinstance(event, ApprovalRequested):
        return "approval_requested", {
            "tool_call_id": event.tool_call.id,
            "tool": event.tool_call.name,
            "reason": _safe_text(event.reason, redactor=redactor),
        }
    if isinstance(event, PermissionDenied):
        return "permission_denied", {
            "tool_call_id": event.tool_call.id,
            "tool": event.tool_call.name,
            "reason": _safe_text(event.reason, redactor=redactor),
        }
    if isinstance(event, RunFinished):
        return "run_finished", {
            "turn_id": event.turn_id,
            "final_text": _safe_text(event.final_text, redactor=redactor),
        }
    if isinstance(event, RunCancelled):
        return "run_cancelled", {
            "turn_id": event.turn_id,
            "reason": _safe_text(event.reason, redactor=redactor),
        }
    if isinstance(event, ErrorOccurred):
        return "error", {
            "error": _safe_text(event.error, redactor=redactor),
            "context": _safe_text(event.context, redactor=redactor),
        }
    return None


def _verify_record(
    record: Mapping[str, Any],
    *,
    previous_hash: str,
    previous_seq: int,
    hmac_key: Optional[bytes],
) -> tuple[bool, str, str, int]:
    if not isinstance(record, Mapping):
        return False, "记录不是 JSON object", previous_hash, previous_seq
    if record.get("schema") != AUDIT_SCHEMA:
        return False, "审计 schema 不兼容", previous_hash, previous_seq
    if not str(record.get("event_id", "") or "").strip():
        return False, "缺少 event_id", previous_hash, previous_seq
    if not str(record.get("type", "") or "").strip():
        return False, "缺少 event type", previous_hash, previous_seq
    try:
        seq = int(record.get("seq"))
    except (TypeError, ValueError):
        return False, "缺少合法 seq", previous_hash, previous_seq
    if seq != previous_seq + 1:
        return False, f"seq 不连续: 期望 {previous_seq + 1}, 实际 {seq}", previous_hash, previous_seq
    if str(record.get("prev_hash", "")) != previous_hash:
        return False, f"第 {seq} 条 prev_hash 断链", previous_hash, previous_seq
    payload = {key: value for key, value in record.items() if key != "hash"}
    expected = _digest(previous_hash, {
        key: value for key, value in payload.items() if key != "hmac"
    })
    if not hmac.compare_digest(str(record.get("hash", "")), expected):
        return False, f"第 {seq} 条 hash 无效", previous_hash, previous_seq
    if "hmac" in record:
        if hmac_key is None:
            return False, f"第 {seq} 条需要 HMAC 密钥", previous_hash, previous_seq
        expected_hmac = _hmac_digest(previous_hash, {
            key: value for key, value in payload.items() if key != "hmac"
        }, hmac_key)
        if not hmac.compare_digest(str(record["hmac"]), expected_hmac):
            return False, f"第 {seq} 条 HMAC 无效", previous_hash, previous_seq
    return True, "", str(record["hash"]), seq


AuditLog = ComplianceAuditor


def verify_audit_file(
    path: Path,
    *,
    hmac_key: Optional[bytes] = None,
) -> AuditVerification:
    """Verify sequence, hash links and optional HMAC for a JSONL audit log."""
    path = Path(path).expanduser()
    if not path.is_file():
        return AuditVerification(False, 0, path, "审计文件不存在")
    previous_hash = "0" * 64
    previous_seq = 0
    count = 0
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    return AuditVerification(
                        False, count, path, f"第 {line_number} 行 JSON 无效: {exc}",
                        previous_hash, previous_seq)
                ok, error, previous_hash, previous_seq = _verify_record(
                    record,
                    previous_hash=previous_hash,
                    previous_seq=previous_seq,
                    hmac_key=hmac_key,
                )
                if not ok:
                    return AuditVerification(
                        False, count, path, error, previous_hash, previous_seq)
                count += 1
    except OSError as exc:
        return AuditVerification(False, count, path, str(exc), previous_hash, previous_seq)
    return AuditVerification(True, count, path, "", previous_hash, previous_seq)
