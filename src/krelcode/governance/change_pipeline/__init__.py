"""Canonical change admission pipeline facade."""

from krelcode.safety.admission import (
    AdmissionGate,
    DEFAULT_ADMISSION_POLICIES,
    DefaultAdmissionGate,
)

__all__ = [
    "AdmissionGate",
    "DEFAULT_ADMISSION_POLICIES",
    "DefaultAdmissionGate",
]
