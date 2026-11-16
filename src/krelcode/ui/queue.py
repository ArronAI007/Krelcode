"""兼容导出：队列实现已移入 session 层，不依赖任何 UI。"""

from ..session.prompt_queue import PromptQueue

__all__ = ["PromptQueue"]
