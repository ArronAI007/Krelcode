"""Canonical change admission pipeline facade."""

from .gate import (
    AdmissionGate,
    DEFAULT_ADMISSION_POLICIES,
    DefaultAdmissionGate,
)

__all__ = [
    "AdmissionGate",
    "DEFAULT_ADMISSION_POLICIES",
    "DefaultAdmissionGate",
]
