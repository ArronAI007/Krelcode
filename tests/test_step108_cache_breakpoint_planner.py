"""Step 108: adaptive explicit prompt-cache breakpoint planning."""

from __future__ import annotations

import json

import pytest

from krelcode.agent.context_usage import build_context_usage_snapshot
from krelcode.core.messages import Message
from krelcode.llm.cache import CACHE_MODE_IMPLICIT, CACHE_MODE_NONE
from krelcode.llm.cache_planner import (
    CACHE_BREAKPOINT_KIND_MESSAGES,
    CACHE_BREAKPOINT_KIND_SYSTEM,
    CACHE_BREAKPOINT_KIND_TOOLS,
    CACHE_BREAKPOINT_REASON_BELOW_MINIMUM,
    CACHE_BREAKPOINT_REASON_DISABLED,
    CACHE_BREAKPOINT_SKIP_CAPACITY,
    CacheBreakpointCandidate,
    CacheBreakpointPlanner,
)
from krelcode.llm.providers.anthropic import (
    AnthropicProvider,
    minimum_cacheable_tokens_for_model,
)
from krelcode.llm.registry import ProviderSettings, create_provider


def _candidate(
    candidate_id: str,
    kind: str,
    order: int,
    cumulative: int,
    *,
    item_index: int = -1,
    anchor: bool = False,
    moving: bool = False,
    incremental: int | None = None,
) -> CacheBreakpointCandidate:
    return CacheBreakpointCandidate(
        id=candidate_id,
        kind=kind,
        order=order,
        item_index=item_index,
        cumulative_tokens=cumulative,
        incremental_tokens=(
            cumulative if incremental is None else incremental),
        chars=cumulative * 4,
        digest=candidate_id,
        stability="static" if anchor else "session",
        anchor=anchor,
        moving=moving,
    )


def _marker_count(payload: dict) -> int:
    count = 0
    for tool in payload.get("tools", []):
        count += int(isinstance(tool, dict) and bool(tool.get("cache_control")))
    for block in payload.get("system", []):
        count += int(isinstance(block, dict) and bool(block.get("cache_control")))
    for message in payload.get("messages", []):
        content = message.get("content") if isinstance(message, dict) else None
        count += int(
            isinstance(content, list)
            and bool(content)
            and isinstance(content[-1], dict)
            and bool(content[-1].get("cache_control"))
        )
    return count


def test_planner_selects_stable_anchors_frontier_and_history_checkpoint():
    candidates = (
        _candidate("tools:0", CACHE_BREAKPOINT_KIND_TOOLS, 0, 1_200, anchor=True),
        _candidate(
            "system:0", CACHE_BREAKPOINT_KIND_SYSTEM, 1, 1_600,
            item_index=0, anchor=True),
        _candidate(
            "messages:0", CACHE_BREAKPOINT_KIND_MESSAGES, 2, 1_900,
            item_index=0, moving=True),
        _candidate(
            "messages:1", CACHE_BREAKPOINT_KIND_MESSAGES, 3, 2_500,
            item_index=1, moving=True),
        _candidate(
            "messages:2", CACHE_BREAKPOINT_KIND_MESSAGES, 4, 3_100,
            item_index=2, moving=True),
        _candidate(
            "messages:3", CACHE_BREAKPOINT_KIND_MESSAGES, 5, 4_000,
            item_index=3, moving=True),
    )
    plan = CacheBreakpointPlanner().plan(
        candidates,
        mode="explicit",
        max_breakpoints=4,
        minimum_cacheable_tokens=1_024,
    )

    assert plan.breakpoint_count == 4
    assert [item.id for item in plan.breakpoints] == [
        "tools:0", "system:0", "messages:1", "messages:3"]
    assert plan.breakpoints[-1].moving is True
    assert plan.selected_prefix_tokens == 4_000
    assert plan.headroom_breakpoints == 0
    assert plan.skipped_by_reason[CACHE_BREAKPOINT_SKIP_CAPACITY] == 2
    assert plan.as_record()["strategy"] == "adaptive-prefix-v1"


def test_planner_single_slot_uses_moving_frontier_and_is_deterministic():
    candidates = (
        _candidate("tools:0", CACHE_BREAKPOINT_KIND_TOOLS, 0, 400, anchor=True),
        _candidate(
            "system:0", CACHE_BREAKPOINT_KIND_SYSTEM, 1, 1_600,
            item_index=0, anchor=True),
        _candidate(
            "messages:0", CACHE_BREAKPOINT_KIND_MESSAGES, 2, 2_400,
            item_index=0, moving=True),
    )
    planner = CacheBreakpointPlanner()
    first = planner.plan(
        candidates, mode="explicit", max_breakpoints=1,
        minimum_cacheable_tokens=1_024)
    second = planner.plan(
        reversed(candidates), mode="explicit", max_breakpoints=1,
        minimum_cacheable_tokens=1_024)

    assert [item.id for item in first.breakpoints] == ["messages:0"]
    assert first.as_record() == second.as_record()


def test_planner_fails_closed_below_minimum_or_when_cache_is_not_explicit():
    candidates = (
        _candidate("tools:0", CACHE_BREAKPOINT_KIND_TOOLS, 0, 700, anchor=True),
        _candidate(
            "system:0", CACHE_BREAKPOINT_KIND_SYSTEM, 1, 1_000,
            item_index=0, anchor=True),
    )
    planner = CacheBreakpointPlanner()
    below = planner.plan(
        candidates, mode="explicit", max_breakpoints=4,
        minimum_cacheable_tokens=1_024)
    implicit = planner.plan(
        candidates, mode=CACHE_MODE_IMPLICIT, max_breakpoints=4,
        minimum_cacheable_tokens=0)
    disabled = planner.plan(
        candidates, mode=CACHE_MODE_NONE, max_breakpoints=4,
        minimum_cacheable_tokens=0)

    assert below.breakpoints == ()
    assert below.reason == CACHE_BREAKPOINT_REASON_BELOW_MINIMUM
    assert below.skipped_by_reason == {"below-minimum": 2}
    assert implicit.breakpoints == ()
    assert implicit.reason == CACHE_BREAKPOINT_REASON_DISABLED
    assert disabled.reason == CACHE_BREAKPOINT_REASON_DISABLED


def test_planner_skips_individual_candidates_below_minimum():
    candidates = (
        _candidate("tools:0", CACHE_BREAKPOINT_KIND_TOOLS, 0, 200, anchor=True),
        _candidate(
            "system:0", CACHE_BREAKPOINT_KIND_SYSTEM, 1, 400,
            item_index=0, anchor=True),
        _candidate(
            "messages:0", CACHE_BREAKPOINT_KIND_MESSAGES, 2, 700,
            item_index=0, moving=True),
        _candidate(
            "messages:1", CACHE_BREAKPOINT_KIND_MESSAGES, 3, 1_100,
            item_index=1, moving=True),
    )

    plan = CacheBreakpointPlanner().plan(
        candidates, mode="explicit", max_breakpoints=4,
        minimum_cacheable_tokens=500)

    assert [item.id for item in plan.breakpoints] == [
        "messages:0", "messages:1"]
    assert plan.skipped_by_reason == {"below-minimum": 2}


def test_planner_rejects_invalid_candidate_contract():
    with pytest.raises(ValueError, match="必须包含 id"):
        CacheBreakpointCandidate("", "tools", 0, 0)
    with pytest.raises(ValueError, match="未知 cache breakpoint kind"):
        CacheBreakpointCandidate("bad", "unknown", 0, 0)


def test_anthropic_uses_adaptive_markers_within_negotiated_limit():
    stable = "STABLE POLICY " * 180
    dynamic = "DYNAMIC TURN " * 180
    tools = [{
        "name": "read_file",
        "description": "Read one file safely " * 240,
        "parameters": {"type": "object", "properties": {}},
    }]
    messages = [Message.system(
        f"{stable}\n\n{dynamic}",
        provider_meta={"prompt_cache_boundaries": [len(stable)]},
    )]
    for index in range(8):
        role_message = (
            Message.user(f"user request {index} " + "x" * 1_600)
            if index % 2 == 1 else
            Message.assistant(f"assistant response {index} " + "y" * 1_600)
        )
        messages.append(role_message)
    provider = AnthropicProvider(
        model="claude-x", api_key="k", cache_min_tokens=1_024)
    request = provider.build_request(messages, tools)
    payload = json.loads(request["body"])
    plan = request["cache_plan"]
    prepared = provider.prepare_request(messages, tools)
    snapshot = build_context_usage_snapshot(
        messages,
        turn_id="step108",
        step=1,
        tool_specs=tools,
        prepared_request=prepared,
    )

    assert _marker_count(payload) == 4
    assert plan.breakpoint_count == 4
    assert [item.kind for item in plan.breakpoints].count("messages") == 2
    assert plan.breakpoints[-1].id == f"messages:{len(payload['messages']) - 1}"
    assert any(item.kind == "tools" for item in plan.breakpoints)
    assert any(item.kind == "system" for item in plan.breakpoints)
    assert snapshot.cache_breakpoint_count == 4
    assert snapshot.cache_control_segment_count == 4
    assert snapshot.cache_breakpoint_strategy == "adaptive-prefix-v1"
    record = json.dumps(snapshot.as_record(), ensure_ascii=False)
    assert "STABLE POLICY" not in record
    assert "user request" not in record
    assert "assistant response" not in record


def test_anthropic_skips_markers_below_model_threshold():
    provider = AnthropicProvider(
        model="claude-x", api_key="k", cache_min_tokens=100_000)
    request = provider.build_request(
        [Message.system("SMALL STABLE"), Message.user("small request")],
        [{
            "name": "read_file",
            "description": "read",
            "parameters": {"type": "object", "properties": {}},
        }],
    )
    payload = json.loads(request["body"])
    plan = request["cache_plan"]

    assert _marker_count(payload) == 0
    assert plan.breakpoint_count == 0
    assert plan.reason == CACHE_BREAKPOINT_REASON_BELOW_MINIMUM
    assert plan.minimum_cacheable_tokens == 100_000


def test_anthropic_counts_dynamic_system_gap_before_message_threshold():
    stable = "稳" * 100
    dynamic = "动" * 800
    provider = AnthropicProvider(
        model="claude-x", api_key="k", cache_min_tokens=500)
    request = provider.build_request(
        [Message.system(
            f"{stable}{dynamic}",
            provider_meta={"prompt_cache_boundaries": [len(stable)]},
        ), Message.user("请回复")],
        None,
    )
    payload = json.loads(request["body"])
    plan = request["cache_plan"]

    assert _marker_count(payload) == 1
    assert [item.id for item in plan.breakpoints] == ["messages:0"]
    assert plan.skipped_by_reason == {"below-minimum": 1}
    assert plan.breakpoints[0].cumulative_tokens >= 900
    assert "cache_control" not in payload["system"][1]


def test_anthropic_model_thresholds_and_config_override():
    assert minimum_cacheable_tokens_for_model("claude-sonnet-4-5") == 1024
    assert minimum_cacheable_tokens_for_model("claude-3-5-haiku-latest") == 2048

    provider = create_provider(ProviderSettings(
        provider="anthropic",
        model="claude-sonnet-4-5",
        api_key="k",
        extra={"prompt_cache": True, "cache_min_tokens": 0},
    ))
    assert isinstance(provider, AnthropicProvider)
    assert provider.cache_min_tokens == 0
