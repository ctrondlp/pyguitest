"""A backend for sessions with no display server.

Supports exactly the tier-1 capabilities -- the 9 legacy functions that never
touched X in the first place. Useful for CI, and as the honest answer when
detection finds nothing: a session that refuses clearly beats one that appears
to work and silently does nothing.
"""

from ..capabilities import Capability, CapabilitySet
from .base import GUIBackend

__all__ = ["NullBackend"]


class NullBackend(GUIBackend):
    """A backend for sessions with no usable display server."""

    name = "null"

    def __init__(self, reason="no display server detected"):
        """Record why no real backend was available."""
        self.reason = reason

    @property
    def capabilities(self):
        """Only the tier-1 capabilities, which need no display server."""
        return CapabilitySet({Capability.PROCESS_LAUNCH, Capability.TIMING})

    def require(self, capability, reason=None):
        """Raise with the detection reason attached."""
        super().require(capability, reason or self.reason)
