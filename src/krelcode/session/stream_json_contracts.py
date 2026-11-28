"""ApplicationRequest and AgentContract parsing for stream-json input."""

from __future__ import annotations

from typing import Mapping, Optional

from ..contracts.agent import AgentContract
from ..contracts.application import (
    ApplicationRequest,
    ApplicationRequestFactory,
)


def parse_contracts(
    raw_application: object,
    raw_contract: object,
    *,
    content: str,
    request_id: str,
) -> tuple[Optional[ApplicationRequest], Optional[AgentContract]]:
    application = _parse_application(raw_application)
    contract = _parse_agent_contract(raw_contract)
    if (
        contract is not None
        and application is not None
        and contract.intent != application
    ):
        raise ValueError("application 与 agent_contract.intent 不一致")
    if contract is not None:
        application = contract.intent
    elif application is None:
        application = ApplicationRequestFactory.from_input_source(
            "stream-json",
            goal=content or "[image attachment]",
            metadata={"request_id": request_id} if request_id else None,
        )
    return application, contract


def _parse_application(raw: object) -> Optional[ApplicationRequest]:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("application 必须是对象")
    try:
        return ApplicationRequest.from_mapping(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"application 无效: {exc}") from exc


def _parse_agent_contract(raw: object) -> Optional[AgentContract]:
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("agent_contract 必须是对象")
    try:
        return AgentContract.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"agent_contract 无效: {exc}") from exc


__all__ = ["parse_contracts"]
