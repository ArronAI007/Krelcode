"""Provider-neutral prompt-cache capability negotiation.

Providers advertise what their wire protocol can actually use, while the user
configuration expresses intent (``auto`` / ``on`` / ``off`` / ``implicit`` /
``explicit``).  Negotiation happens before request construction so an adapter
never emits cache-control fields that its provider or model does not support.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Tuple

from ..core.errors import ConfigError


CACHE_MODE_NONE = "none"
CACHE_MODE_IMPLICIT = "implicit"
CACHE_MODE_EXPLICIT = "explicit"
CACHE_MODES = (
    CACHE_MODE_NONE,
    CACHE_MODE_IMPLICIT,
    CACHE_MODE_EXPLICIT,
)

CACHE_PREFERENCE_AUTO = "auto"
CACHE_PREFERENCE_ON = "on"
CACHE_PREFERENCE_OFF = "off"
CACHE_PREFERENCES = (
    CACHE_PREFERENCE_AUTO,
    CACHE_PREFERENCE_ON,
    CACHE_PREFERENCE_OFF,
    CACHE_MODE_IMPLICIT,
    CACHE_MODE_EXPLICIT,
)

CACHE_REASON_DISABLED = "disabled_by_preference"
CACHE_REASON_NO_SUPPORT = "provider_cache_unsupported"
CACHE_REASON_AUTO_EXPLICIT = "auto_selected_explicit"
CACHE_REASON_AUTO_IMPLICIT = "auto_selected_implicit"
CACHE_REASON_REQUESTED = "requested_mode_selected"
CACHE_REASON_FALLBACK_IMPLICIT = "explicit_unsupported_fell_back_to_implicit"
CACHE_REASON_IMPLICIT_UNSUPPORTED = "requested_implicit_unsupported"

_MODE_RANK = {
    CACHE_MODE_NONE: 0,
    CACHE_MODE_IMPLICIT: 1,
    CACHE_MODE_EXPLICIT: 2,
}
_PREFERENCE_ALIASES = {
    "": CACHE_PREFERENCE_AUTO,
    "default": CACHE_PREFERENCE_AUTO,
    "true": CACHE_PREFERENCE_ON,
    "enabled": CACHE_PREFERENCE_ON,
    "yes": CACHE_PREFERENCE_ON,
    "false": CACHE_PREFERENCE_OFF,
    "disabled": CACHE_PREFERENCE_OFF,
    "no": CACHE_PREFERENCE_OFF,
    "none": CACHE_PREFERENCE_OFF,
}


def normalize_cache_preference(value: object = CACHE_PREFERENCE_AUTO) -> str:
    """Normalize a TOML/CLI cache preference to a stable policy value.

    ``prompt_cache = true`` keeps working as a shorthand for ``on`` while the
    richer string form allows per-provider diagnostics.
    """
    if value is None:
        return CACHE_PREFERENCE_AUTO
    if isinstance(value, bool):
        return CACHE_PREFERENCE_ON if value else CACHE_PREFERENCE_OFF
    normalized = str(value).strip().lower()
    normalized = _PREFERENCE_ALIASES.get(normalized, normalized)
    if normalized not in CACHE_PREFERENCES:
        allowed = ", ".join(CACHE_PREFERENCES)
        raise ConfigError(
            f"未知 prompt_cache 值: {value!r}（可用: {allowed}）")
    return normalized


@dataclass(frozen=True)
class CacheCapabilities:
    """Cache modes and wire constraints advertised by one provider adapter."""

    modes: Tuple[str, ...] = ()
    preferred_mode: str = CACHE_MODE_NONE
    max_breakpoints: int = 0
    reports_cache_read: bool = False
    reports_cache_creation: bool = False
    supports_ttl: bool = False

    def __post_init__(self) -> None:
        modes = tuple(dict.fromkeys(str(mode).strip().lower() for mode in self.modes))
        invalid = [mode for mode in modes if mode not in CACHE_MODES]
        if invalid:
            raise ValueError(f"未知 provider cache mode: {invalid[0]}")
        supported = tuple(
            mode for mode in (CACHE_MODE_IMPLICIT, CACHE_MODE_EXPLICIT)
            if mode in modes
        )
        preferred = str(self.preferred_mode or "").strip().lower()
        if not preferred or preferred == CACHE_MODE_NONE:
            preferred = (
                max(supported, key=lambda mode: _MODE_RANK[mode])
                if supported else CACHE_MODE_NONE
            )
        if preferred not in CACHE_MODES:
            raise ValueError(f"未知 preferred cache mode: {self.preferred_mode}")
        if preferred != CACHE_MODE_NONE and preferred not in supported:
            raise ValueError(
                f"preferred cache mode 未在 capabilities 中声明: {preferred}")
        if self.max_breakpoints < 0:
            raise ValueError("max_breakpoints 不能为负数")
        object.__setattr__(self, "modes", supported)
        object.__setattr__(self, "preferred_mode", preferred)

    @property
    def available_modes(self) -> Tuple[str, ...]:
        return (CACHE_MODE_NONE, *self.modes)

    @property
    def supports_cache(self) -> bool:
        return bool(self.modes)

    def supports(self, mode: str) -> bool:
        return mode == CACHE_MODE_NONE or mode in self.modes

    def negotiate(
        self,
        preference: object = CACHE_PREFERENCE_AUTO,
    ) -> "CacheNegotiation":
        """Resolve user intent against the provider's actual capabilities."""
        requested = normalize_cache_preference(preference)
        if requested == CACHE_PREFERENCE_OFF:
            return CacheNegotiation._build(
                self, requested, CACHE_MODE_NONE, False,
                CACHE_REASON_DISABLED)
        if not self.modes:
            return CacheNegotiation._build(
                self, requested, CACHE_MODE_NONE, False,
                CACHE_REASON_NO_SUPPORT)

        if requested in (CACHE_PREFERENCE_AUTO, CACHE_PREFERENCE_ON):
            effective = (
                self.preferred_mode
                if self.preferred_mode != CACHE_MODE_NONE
                else max(self.modes, key=lambda mode: _MODE_RANK[mode])
            )
            reason = (
                CACHE_REASON_AUTO_EXPLICIT
                if effective == CACHE_MODE_EXPLICIT
                else CACHE_REASON_AUTO_IMPLICIT
            )
            return CacheNegotiation._build(
                self, requested, effective, False, reason)

        if requested == CACHE_MODE_EXPLICIT:
            if CACHE_MODE_EXPLICIT in self.modes:
                return CacheNegotiation._build(
                    self, requested, CACHE_MODE_EXPLICIT, False,
                    CACHE_REASON_REQUESTED)
            if CACHE_MODE_IMPLICIT in self.modes:
                return CacheNegotiation._build(
                    self, requested, CACHE_MODE_IMPLICIT, True,
                    CACHE_REASON_FALLBACK_IMPLICIT)
            return CacheNegotiation._build(
                self, requested, CACHE_MODE_NONE, False,
                CACHE_REASON_NO_SUPPORT)

        # Explicitly requesting implicit cache must not silently introduce a
        # stronger explicit cache-control contract on the wire.
        if CACHE_MODE_IMPLICIT in self.modes:
            return CacheNegotiation._build(
                self, requested, CACHE_MODE_IMPLICIT, False,
                CACHE_REASON_REQUESTED)
        return CacheNegotiation._build(
            self, requested, CACHE_MODE_NONE, False,
            CACHE_REASON_IMPLICIT_UNSUPPORTED)


@dataclass(frozen=True)
class CacheNegotiation:
    """The immutable result of one capability negotiation."""

    requested_mode: str
    effective_mode: str
    supported_modes: Tuple[str, ...]
    fallback: bool = False
    reason: str = ""
    max_breakpoints: int = 0
    reports_cache_read: bool = False
    reports_cache_creation: bool = False
    supports_ttl: bool = False

    def __post_init__(self) -> None:
        if self.requested_mode not in CACHE_PREFERENCES:
            raise ValueError(f"未知 requested cache mode: {self.requested_mode}")
        if self.effective_mode not in CACHE_MODES:
            raise ValueError(f"未知 effective cache mode: {self.effective_mode}")
        object.__setattr__(self, "supported_modes", tuple(self.supported_modes))

    @classmethod
    def _build(
        cls,
        capabilities: CacheCapabilities,
        requested_mode: str,
        effective_mode: str,
        fallback: bool,
        reason: str,
    ) -> "CacheNegotiation":
        return cls(
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            supported_modes=capabilities.available_modes,
            fallback=fallback,
            reason=reason,
            max_breakpoints=capabilities.max_breakpoints,
            reports_cache_read=capabilities.reports_cache_read,
            reports_cache_creation=capabilities.reports_cache_creation,
            supports_ttl=capabilities.supports_ttl,
        )

    @property
    def enabled(self) -> bool:
        return self.effective_mode != CACHE_MODE_NONE

    @property
    def uses_explicit_control(self) -> bool:
        return self.effective_mode == CACHE_MODE_EXPLICIT

    def as_record(self) -> Mapping[str, object]:
        """Content-free diagnostics safe for logs, events and session records."""
        return {
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "supported_modes": list(self.supported_modes),
            "fallback": self.fallback,
            "reason": self.reason,
            "max_breakpoints": self.max_breakpoints,
            "reports_cache_read": self.reports_cache_read,
            "reports_cache_creation": self.reports_cache_creation,
            "supports_ttl": self.supports_ttl,
        }
