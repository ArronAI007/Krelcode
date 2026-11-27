"""Session compatibility facade for the authoritative local event log."""

from ..storage.event_log import (
    EVENT_LOG_META_KEY,
    EVENT_LOG_VERSION,
    LocalEventLog,
)

__all__ = ["EVENT_LOG_META_KEY", "EVENT_LOG_VERSION", "LocalEventLog"]
