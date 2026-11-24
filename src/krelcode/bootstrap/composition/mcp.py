"""MCP root, sampler and layered configuration composition."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from ._deps import *  # noqa: F401,F403


def build_mcp_sampler(
    llm: LLMProvider,
) -> Callable[
    [Sequence[Mapping[str, Any]], Optional[Mapping[str, Any]]],
    Awaitable[Dict[str, str]],
]:
    """MCP sampling 回调：服务器请求 LLM 补全时，用当前 provider 完成。

    契约：async (messages, params) -> {"text": str, "model": str}
    messages 为 MCP 格式 [{"role","content":{"type":"text","text"}}]。
    """
    from ...core.messages import Message as Msg
    from ...llm.base import collect

    model_name = getattr(llm, "model", "krelcode")

    async def sampler(
        messages: Sequence[Mapping[str, Any]],
        params: Optional[Mapping[str, Any]],
    ) -> Dict[str, str]:
        msgs: List[Message] = []
        system_prompt = (params or {}).get("systemPrompt")
        if system_prompt:
            msgs.append(Msg.system(str(system_prompt)))
        for m in messages or []:
            content = m.get("content") or {}
            text = content.get("text", "") if isinstance(content, dict) else str(content)
            if m.get("role") == "assistant":
                msgs.append(Msg.assistant(text))
            else:
                msgs.append(Msg.user(text))
        if not msgs:
            msgs.append(Msg.user("(空的 sampling 请求)"))
        finished = await collect(llm, msgs)
        return {"text": finished.message.content or "", "model": model_name}

    return sampler


def build_mcp_roots(workspace_root: Path) -> List[Dict[str, Any]]:
    return [{"uri": workspace_root.as_uri(), "name": workspace_root.name}]


def compose_mcp_config(
    config: Dict[str, Any],
    plugin_servers: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge plugin MCP defaults with explicit layered configuration."""
    merged = dict(plugin_servers)
    configured = config.get("mcp_servers") or {}
    if isinstance(configured, dict):
        merged.update(configured)
    return merged

