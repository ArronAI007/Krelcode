"""Bounded-turn limit messages."""


def max_steps_reached_text(max_steps: int) -> str:
    """Explain the per-turn safety stop and how to raise it."""
    return (
        f"[krelcode] 工具循环已达到单轮 {max_steps} 个模型步骤的安全上限，"
        f"本轮已中止。可用 --max-steps {max_steps * 2} 提高上限后重试。"
    )


__all__ = ["max_steps_reached_text"]
