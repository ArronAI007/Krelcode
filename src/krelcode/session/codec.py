"""Compatibility import for :mod:`krelcode.core.codec`."""

from ..core.codec import (
    history_from_dicts,
    history_to_dicts,
    message_from_dict,
    message_to_dict,
)

__all__ = [
    "history_from_dicts",
    "history_to_dicts",
    "message_from_dict",
    "message_to_dict",
]
