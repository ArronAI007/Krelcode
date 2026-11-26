"""Local-first Agent observability and ablation experiments.

The package is an outer adapter.  Agent, provider and tool layers never import
it, and failures in its journal/gateway cannot propagate into an Agent turn.
"""

from .journal import ObservabilitySink
from .models import EventEnvelope, RunManifest, RuntimeReleaseManifest

__all__ = [
    "EventEnvelope",
    "ObservabilitySink",
    "RunManifest",
    "RuntimeReleaseManifest",
]
