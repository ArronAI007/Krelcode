"""Agent-to-application acceptance evidence adapter."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterable, Optional

from ...contracts.acceptance import AcceptanceEvidence, AcceptanceResult
from ...contracts.agent import AgentContract, DeliverableKind
from ...contracts.artifact import ArtifactStorePort
from ...contracts.runtime import ArtifactIdentity
from ...core.messages import Message
from ...tools.schema import validate_tool_arguments

_TEST_COMMAND = re.compile(
    r"(?i)(?:^|[\s;&|])(?:"
    r"pytest|py\.test|tox|nox|unittest|"
    r"(?:python\d*|py)\s+-m\s+(?:pytest|unittest)|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test|"
    r"(?:cargo|go|dotnet)\s+test|"
    r"(?:mvn|gradle|gradlew|mvnw)\b.*\btest|"
    r"make\s+test|rspec|phpunit"
    r")\b",
)


class AcceptanceGateError(RuntimeError):
    """The production task did not satisfy its frozen acceptance contract."""

    def __init__(self, result: AcceptanceResult) -> None:
        super().__init__(
            f"AgentContract 验收未通过: {result.status.value}"
            + (f" · {result.reason}" if result.reason else "")
        )
        self.result = result


class AcceptanceReviewRequired(AcceptanceGateError):
    """Acceptance is incomplete and requires an explicit human decision."""


def acceptance_evidence_from_history(
    history: Iterable[Message],
) -> tuple[AcceptanceEvidence, ...]:
    evidence: list[AcceptanceEvidence] = []
    for message in history:
        if message.role != "tool":
            continue
        content = message.content or ""
        name = message.tool_name or "tool"
        evidence.append(AcceptanceEvidence(
            kind="tool-result",
            source=name,
            passed=not bool(message.tool_is_error),
            summary=f"{len(content)} chars",
            metadata={
                "tool_call_id": message.tool_call_id or "",
                "tool_name": name,
            },
        ))
        command = str((message.tool_meta or {}).get("command") or "")
        if (
            name in {"run_command", "job_output"}
            and not message.tool_is_error
            and _TEST_COMMAND.search(command)
        ):
            evidence.append(AcceptanceEvidence(
                kind="test-result",
                source=name,
                passed=True,
                summary=content[:240],
                metadata={
                    "tool_call_id": message.tool_call_id or "",
                    "command": command[:500],
                },
            ))
        artifact_id = str((message.tool_meta or {}).get("artifact_id") or "")
        digest = str((message.tool_meta or {}).get("digest") or "")
        if artifact_id and digest:
            evidence.append(AcceptanceEvidence(
                kind="artifact",
                source=name,
                passed=not bool(message.tool_is_error),
                summary=f"artifact {artifact_id[:80]}",
                artifact=ArtifactIdentity(
                    kind="tool-output",
                    id=artifact_id,
                    version=digest[:16],
                    digest=digest,
                ),
                digest=digest,
                metadata={"tool_call_id": message.tool_call_id or ""},
            ))
    return tuple(evidence)


def deliverable_evidence_from_contract(
    contract: AgentContract,
    workspace_root: Path,
    final_response: str,
    *,
    artifact_store: Optional[ArtifactStorePort] = None,
) -> tuple[AcceptanceEvidence, ...]:
    """Resolve every declared deliverable against real workspace/artifact state."""
    evidence: list[AcceptanceEvidence] = []
    workspace = Path(workspace_root).resolve()
    response = str(final_response or "")
    for requirement in contract.deliverables.requirements:
        kind = DeliverableKind(requirement.kind)
        if kind is DeliverableKind.RESPONSE:
            evidence.append(AcceptanceEvidence(
                kind="deliverable",
                source="agent.contract",
                passed=bool(response.strip()),
                summary=f"{len(response)} chars",
                deliverable_id=requirement.id,
                digest=hashlib.sha256(
                    response.encode("utf-8")).hexdigest(),
            ))
            continue
        if requirement.artifact_id:
            if artifact_store is None:
                evidence.append(_failed_deliverable(
                    requirement.id, "artifact store 未配置"))
                continue
            try:
                ref = artifact_store.stat(requirement.artifact_id)
            except Exception as exc:
                evidence.append(_failed_deliverable(
                    requirement.id,
                    f"Artifact 不可用: {type(exc).__name__}"))
                continue
            evidence.append(AcceptanceEvidence(
                kind="deliverable",
                source="agent.contract",
                passed=True,
                summary=f"artifact {ref.artifact_id}",
                artifact=ArtifactIdentity(
                    kind="deliverable",
                    id=ref.artifact_id,
                    version=ref.digest[:16],
                    digest=ref.digest,
                    metadata={"deliverable_id": requirement.id},
                ),
                deliverable_id=requirement.id,
                digest=ref.digest,
                metadata={"artifact_id": ref.artifact_id},
            ))
            continue
        if kind is DeliverableKind.JSON and not requirement.path:
            data = response.encode("utf-8")
            passed, summary = _json_deliverable_status(
                data, requirement.schema)
            artifact = None
            if requirement.artifact_required:
                if artifact_store is None:
                    passed = False
                    summary = "artifact store 未配置"
                else:
                    ref = artifact_store.put(
                        data,
                        media_type=requirement.media_type or "application/json",
                        name=f"{requirement.id}.json",
                        metadata={
                            "deliverable_id": requirement.id,
                            "contract_id": contract.contract_id,
                        },
                    )
                    artifact = ArtifactIdentity(
                        kind="deliverable",
                        id=ref.artifact_id,
                        version=ref.digest[:16],
                        digest=ref.digest,
                        metadata={"deliverable_id": requirement.id},
                    )
            evidence.append(AcceptanceEvidence(
                kind="deliverable",
                source="agent.contract",
                passed=passed,
                summary=summary,
                artifact=artifact,
                deliverable_id=requirement.id,
                digest=hashlib.sha256(data).hexdigest(),
                metadata={"kind": kind.value, "source": "final_response"},
            ))
            continue
        path, path_error = _safe_deliverable_path(
            workspace, requirement.path)
        if path_error:
            evidence.append(_failed_deliverable(requirement.id, path_error))
            continue
        if path is None:
            evidence.append(_failed_deliverable(
                requirement.id, "交付物缺少路径"))
            continue
        if not path.exists():
            evidence.append(_failed_deliverable(
                requirement.id, f"交付物不存在: {requirement.path}"))
            continue
        if kind is DeliverableKind.DIRECTORY or path.is_dir():
            if not path.is_dir():
                evidence.append(_failed_deliverable(
                    requirement.id, "交付物不是目录"))
                continue
            if requirement.artifact_required:
                evidence.append(_failed_deliverable(
                    requirement.id,
                    "目录交付物需要声明可寻址的归档文件",
                ))
                continue
            evidence.append(AcceptanceEvidence(
                kind="deliverable",
                source="agent.contract",
                passed=True,
                summary=f"directory {requirement.path}",
                deliverable_id=requirement.id,
            ))
            continue
        if not path.is_file():
            evidence.append(_failed_deliverable(
                requirement.id, "交付物不是普通文件"))
            continue
        data = path.read_bytes()
        if (
            requirement.max_bytes is not None
            and len(data) > requirement.max_bytes
        ):
            evidence.append(_failed_deliverable(
                requirement.id,
                f"交付物超过大小上限: {len(data)} > {requirement.max_bytes}",
            ))
            continue
        digest = hashlib.sha256(data).hexdigest()
        passed = True
        summary = f"{len(data)} bytes"
        if kind is DeliverableKind.JSON:
            passed, summary = _json_deliverable_status(
                data, requirement.schema)
        artifact = None
        if requirement.artifact_required:
            if artifact_store is None:
                passed = False
                summary = "artifact store 未配置"
            else:
                ref = artifact_store.put(
                    data,
                    media_type=(
                        requirement.media_type
                        or "application/octet-stream"),
                    name=requirement.path,
                    metadata={
                        "deliverable_id": requirement.id,
                        "contract_id": contract.contract_id,
                    },
                )
                artifact = ArtifactIdentity(
                    kind="deliverable",
                    id=ref.artifact_id,
                    version=ref.digest[:16],
                    digest=ref.digest,
                    metadata={
                        "deliverable_id": requirement.id,
                        "media_type": ref.media_type,
                    },
                )
        evidence.append(AcceptanceEvidence(
            kind="deliverable",
            source="agent.contract",
            passed=passed,
            summary=summary,
            artifact=artifact,
            deliverable_id=requirement.id,
            digest=digest,
            metadata={
                "path": requirement.path,
                "kind": kind.value,
            },
        ))
    return tuple(evidence)


def _safe_deliverable_path(
    workspace: Path,
    raw: str,
) -> tuple[Optional[Path], str]:
    text = str(raw or "").strip()
    if not text:
        return None, ""
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(workspace)
    except (OSError, ValueError):
        return None, f"交付物路径越界: {text}"
    return resolved, ""


def _json_deliverable_status(
    data: bytes,
    schema: object,
) -> tuple[bool, str]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, f"JSON 交付物无法解析: {type(exc).__name__}"
    if isinstance(schema, dict) and schema:
        errors = validate_tool_arguments(schema, value)
        if errors:
            return False, "JSON Schema 校验失败: " + "; ".join(
                error.render() for error in errors[:10])
    return True, "JSON Schema 校验通过"


def _failed_deliverable(
    deliverable_id: str,
    reason: str,
) -> AcceptanceEvidence:
    return AcceptanceEvidence(
        kind="deliverable",
        source="agent.contract",
        passed=False,
        summary=reason,
        deliverable_id=deliverable_id,
    )


__all__ = [
    "acceptance_evidence_from_history",
    "AcceptanceGateError",
    "AcceptanceReviewRequired",
    "deliverable_evidence_from_contract",
]
