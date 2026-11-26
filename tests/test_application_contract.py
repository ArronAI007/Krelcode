"""Business/application contract shared by every Krelcode frontend."""

from __future__ import annotations

import pytest

from krelcode.contracts.application import (
    AcceptanceContract,
    ApplicationRequest,
    ApplicationRequestFactory,
    RiskLevel,
    RiskProfile,
    TaskBudget,
    TaskShape,
    TriggerType,
)


def test_application_request_normalizes_and_round_trips() -> None:
    request = ApplicationRequest(
        trigger="repl",  # type: ignore[arg-type]
        goal="  inspect   the parser  ",
        task_shape="single-agent",  # type: ignore[arg-type]
        acceptance=AcceptanceContract(criteria=("tests pass",)),
        risk=RiskProfile(level="medium", reasons=("writes files",)),
        budget=TaskBudget(max_steps=8, max_tokens=2_000),
        metadata={"source": "terminal"},
    )

    assert request.trigger is TriggerType.REPL
    assert request.task_shape is TaskShape.SINGLE_AGENT
    assert request.goal == "inspect the parser"
    assert request.risk.level is RiskLevel.MEDIUM
    assert ApplicationRequest.from_mapping(request.to_dict()) == request
    with pytest.raises(TypeError):
        request.metadata["source"] = "mutated"  # type: ignore[index]


def test_application_factory_maps_frontends_and_clamps_budget() -> None:
    triggers = {
        "terminal": TriggerType.REPL,
        "headless": TriggerType.HEADLESS,
        "stream-json": TriggerType.STREAM_JSON,
        "sdk": TriggerType.SDK,
        "daemon": TriggerType.DAEMON,
        "subagent": TriggerType.SUBAGENT,
        "webui": TriggerType.WEBUI,
    }
    for source, expected in triggers.items():
        request = ApplicationRequestFactory.from_input_source(
            source,
            goal="do work",
            budget=TaskBudget(max_steps=0, timeout_seconds=0),
        )
        assert request.trigger is expected
        assert request.budget.max_steps == 1
        assert request.budget.timeout_seconds == 0.001


def test_application_request_rejects_empty_goal_and_invalid_nested_values() -> None:
    with pytest.raises(ValueError, match="goal"):
        ApplicationRequestFactory.from_input_source("sdk", goal="   ")
    with pytest.raises(ValueError, match="AcceptanceContract"):
        AcceptanceContract(criteria=())
    with pytest.raises(ValueError):
        RiskProfile(level="impossible")


def test_application_request_metadata_is_copied_at_the_boundary() -> None:
    metadata = {"request_id": "r1"}
    request = ApplicationRequestFactory.from_input_source(
        "sdk", goal="work", metadata=metadata)
    metadata["request_id"] = "r2"

    assert request.metadata["request_id"] == "r1"
    assert request.metadata["input_source"] == "sdk"
    assert ApplicationRequestFactory.from_input_source(
        TriggerType.WEBUI,
        goal="observe",
    ).trigger is TriggerType.WEBUI
