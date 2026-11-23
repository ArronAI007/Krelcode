"""Privacy-preserving real-user telemetry for Krelcode."""

from .config import (
    DEFAULT_SAMPLE_RATE,
    TELEMETRY_MODES,
    TelemetryConfig,
    TelemetryConfigError,
    TelemetryPreferenceStore,
    TelemetryPreferences,
    validate_endpoint,
)
from .privacy import (
    TELEMETRY_EVENT_TYPES,
    TELEMETRY_SCHEMA_VERSION,
    TelemetryPrivacyError,
    validate_event,
)
from .runtime import TelemetryRuntime, build_telemetry_runtime
from .store import TelemetryStore, TelemetryStoreError, TelemetryStoreStatus
from .transport import (
    HttpTelemetryTransport,
    TelemetryTransport,
    TelemetryTransportError,
)

__all__ = [
    "DEFAULT_SAMPLE_RATE",
    "HttpTelemetryTransport",
    "TELEMETRY_EVENT_TYPES",
    "TELEMETRY_MODES",
    "TELEMETRY_SCHEMA_VERSION",
    "TelemetryConfig",
    "TelemetryConfigError",
    "TelemetryPreferenceStore",
    "TelemetryPreferences",
    "TelemetryPrivacyError",
    "TelemetryRuntime",
    "TelemetryStore",
    "TelemetryStoreError",
    "TelemetryStoreStatus",
    "TelemetryTransport",
    "TelemetryTransportError",
    "build_telemetry_runtime",
    "validate_endpoint",
    "validate_event",
]
