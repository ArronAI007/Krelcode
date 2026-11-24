"""Backward-compatible imports for the shared core token estimators."""

from krelcode.core.tokens import (
    CHARS_PER_TOKEN,
    estimate_history_tokens,
    estimate_message_tokens,
    estimate_text_tokens,
    is_cjk,
)

__all__ = [
    "CHARS_PER_TOKEN",
    "estimate_text_tokens",
    "estimate_message_tokens",
    "estimate_history_tokens",
    "is_cjk",
]
