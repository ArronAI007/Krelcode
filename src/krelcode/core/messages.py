"""规范化对话消息（wire-free）。

core 只定义与任何 provider 无关的规范格式；
与 OpenAI / Anthropic 等 wire format 的互转一律放在 llm/providers/ 各适配器内。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"

# 参数非法 JSON 时，适配器把原文塞进这个 key，工具执行层转成 is_error 结果回填，
# 让 LLM 自我修正（错误恢复原则）。这是所有 provider 适配器共同遵守的约定。
INVALID_ARGUMENTS_KEY = "__invalid_arguments__"


@dataclass(frozen=True)
class ImageRef:
    """用户消息附带的图片（多模态输入）。

    data_b64 在内存中携带；会话持久化只存 source_path（防 JSONL 膨胀），
    恢复时按路径重载，源文件已删则丢弃该图。wire 转换在各 provider
    adapter 内完成（OpenAI image_url / Anthropic source / Gemini inline_data）。
    """

    media_type: str            # image/png | image/jpeg | image/webp | image/gif
    data_b64: str
    source_path: Optional[str] = None


@dataclass(frozen=True)
class ToolCall:
    """assistant 消息里的一次工具调用请求。arguments 已解析为 dict。"""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_invalid_arguments(self) -> bool:
        return INVALID_ARGUMENTS_KEY in self.arguments


@dataclass
class Message:
    """一条对话消息。四种 role：system / user / assistant / tool。

    provider_meta：不透明的 provider 私有状态（如 Anthropic 扩展思考的
    thinking 块与签名——多轮工具调用时 API 要求原样回传）。
    约定：只有产生它的 provider adapter 可以读写；core/agent/tools
    对其内容零感知，仅负责携带与持久化（session codec）。
    """

    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None  # 仅 assistant
    tool_call_id: Optional[str] = None           # 仅 tool（结果回填）
    provider_meta: Optional[Dict[str, Any]] = None
    images: Optional[List[ImageRef]] = None      # 仅 user（多模态输入）

    # -- 便捷构造 -------------------------------------------------------------

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role=ROLE_SYSTEM, content=content)

    @classmethod
    def user(cls, content: str,
             images: Optional[List[ImageRef]] = None) -> "Message":
        return cls(role=ROLE_USER, content=content, images=images)

    @classmethod
    def assistant(
        cls,
        content: Optional[str] = None,
        tool_calls: Optional[List[ToolCall]] = None,
    ) -> "Message":
        return cls(role=ROLE_ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str) -> "Message":
        return cls(role=ROLE_TOOL, content=content, tool_call_id=tool_call_id)


def sanitize_history(history: List[Message]) -> None:
    """打断/取消后的历史消毒（原地）。

    取消可能发生在任意点，历史尾部可能残留：
    - assistant(tool_calls) 而对应 tool 结果缺失 → provider 会拒绝请求
    - 部分 tool 结果 → 与 assistant.tool_calls 不配对
    规则：从尾部级联弹出 tool 消息与带 tool_calls 的 assistant 消息，
    直到最后一条是「纯文本 assistant」或「user」为止。
    """
    while history:
        last = history[-1]
        if last.role == ROLE_TOOL:
            history.pop()
        elif last.role == ROLE_ASSISTANT and last.tool_calls:
            history.pop()
        else:
            break
