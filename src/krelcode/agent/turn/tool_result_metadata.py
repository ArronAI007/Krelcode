"""Build bounded acceptance and working-context metadata for tool results."""

from __future__ import annotations

import time
from typing import Callable, Mapping, Optional

from ...contracts.tool import SideEffectKind, ToolDescriptor
from ...agent.context.working_context import compact_tool_metadata


def build_tool_result_metadata(
    tool_name: str,
    data: Optional[Mapping[str, object]],
    *,
    is_error: bool,
    turn_id: str,
    arguments: Optional[Mapping[str, object]],
    descriptor: Optional[ToolDescriptor],
    redactor: Optional[Callable[[str], str]],
) -> dict[str, object]:
    metadata = compact_tool_metadata(
        tool_name,
        data,
        is_error=is_error,
        turn_id=turn_id,
        arguments=arguments,
        redactor=redactor,
    )
    metadata["observed_at"] = time.time()
    if descriptor is not None:
        side_effect = SideEffectKind(descriptor.side_effect.kind)
        metadata["side_effect_kind"] = side_effect.value
        metadata["replay_safe"] = descriptor.side_effect.replay_safe
    return metadata


__all__ = ["build_tool_result_metadata"]
