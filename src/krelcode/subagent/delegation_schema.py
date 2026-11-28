"""Tool schema and normalization for structured subagent delegation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping, Optional

from ..contracts.delegation import DelegationContract
from ..contracts.tool import ToolContext
from .delegation_runtime import build_delegation_contract
from .delegation_policy import assess_delegation_value

SPAWN_AGENT_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": (
                "兼容字段：自包含委派说明，写明当前状态/心智模型、已验证事实、"
                "精确范围、交付物和验证方式；不得依赖父对话或要求子代理猜测。"
                "新调用应优先使用 delegation 结构化契约。"
            ),
        },
        "delegation": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "maxLength": 4_000},
                "scope": {
                    "type": "object",
                    "properties": {
                        "read_paths": {"type": "array", "items": {"type": "string"}},
                        "write_paths": {"type": "array", "items": {"type": "string"}},
                        "deny_paths": {"type": "array", "items": {"type": "string"}},
                        "systems": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": True,
                },
                "known_context": {
                    "type": "object",
                    "properties": {
                        "facts": {"type": "array", "items": {"type": "string"}},
                        "assumptions": {"type": "array", "items": {"type": "string"}},
                        "decisions": {"type": "array", "items": {"type": "string"}},
                        "completed_work": {"type": "array", "items": {"type": "string"}},
                        "blockers": {"type": "array", "items": {"type": "string"}},
                        "attempted": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": True,
                },
                "constraints": {"type": "array", "items": {"type": "string"}},
                "capabilities": {
                    "type": "object",
                    "properties": {
                        "definition": {"type": "string"},
                        "model": {"type": "string"},
                        "allowed_tools": {"type": "array", "items": {"type": "string"}},
                        "mcp_servers": {"type": "array", "items": {"type": "string"}},
                        "permission_mode": {"type": "string"},
                        "skills": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": True,
                },
                "budget": {
                    "type": "object",
                    "properties": {
                        "max_steps": {"type": "integer", "minimum": 1},
                        "max_tokens": {"type": "integer", "minimum": 1},
                        "timeout_seconds": {"type": "number", "minimum": 0.1},
                        "max_cost_usd": {"type": "number", "minimum": 0},
                        "max_tool_calls": {"type": "integer", "minimum": 1},
                        "max_high_risk_actions": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": True,
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "format": {"type": "string", "enum": ["text", "json"]},
                        "schema": {"type": "object"},
                        "required_fields": {"type": "array", "items": {"type": "string"}},
                        "evidence_required": {"type": "boolean"},
                        "max_chars": {"type": "integer", "minimum": 1},
                    },
                    "additionalProperties": True,
                },
                "failure_policy": {
                    "type": "string",
                    "enum": [
                        "fail_fast", "best_effort",
                        "retry_or_reassign", "escalate",
                    ],
                },
                "retry_policy": {
                    "type": "object",
                    "properties": {
                        "max_retries": {"type": "integer", "minimum": 0},
                        "allow_reassign": {"type": "boolean"},
                        "reassign_definition": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
                "acceptance": {
                    "type": "object",
                    "properties": {
                        "criteria": {"type": "array", "items": {"type": "string"}},
                        "required_evidence": {"type": "array", "items": {"type": "string"}},
                        "require_workspace_match": {"type": "boolean"},
                        "max_evidence_age_seconds": {"type": "number", "minimum": 1},
                    },
                    "additionalProperties": True,
                },
                "relation_kind": {
                    "type": "string",
                    "enum": ["delegation", "handoff"],
                },
                "handoff_state": {"type": "object"},
                "recovery_checkpoint_id": {"type": "string"},
            },
            "required": ["objective"],
            "additionalProperties": True,
        },
        "isolated": {"type": "boolean"},
        "fork_context": {"type": "boolean"},
        "definition": {"type": "string"},
    },
    "anyOf": [
        {"required": ["task"]},
        {"required": ["delegation"]},
    ],
    "additionalProperties": False,
}


def delegation_contract_from_args(
    args: Mapping[str, object],
    ctx: ToolContext,
) -> DelegationContract:
    raw = args.get("delegation")
    task = str(args.get("task") or "").strip()
    contract = build_delegation_contract(
        task=task,
        raw=raw if isinstance(raw, Mapping) else None,
        parent=ctx.delegation_parent,
    )
    requested_definition = str(args.get("definition") or "").strip()
    if requested_definition:
        contract = contract.with_capabilities(replace(
            contract.capabilities,
            definition=requested_definition,
        ))
    if isinstance(raw, Mapping):
        assessment = assess_delegation_value(contract)
        if not assessment.allowed:
            raise ValueError(
                "委派价值不足: " + ", ".join(assessment.reasons)
                + "；请本地执行，或补充隔离/专业化/并行/失败治理契约")
    return contract


__all__ = [
    "SPAWN_AGENT_PARAMETERS",
    "delegation_contract_from_args",
]
