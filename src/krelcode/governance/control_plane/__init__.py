"""Canonical control-plane facade."""

from krelcode.safety.control_plane import ControlPlane, DefaultControlPlane
from .helpers import governance_for_change

__all__ = ["ControlPlane", "DefaultControlPlane", "governance_for_change"]
