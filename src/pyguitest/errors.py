"""Exception hierarchy.

X11::GUITest signalled failure by returning zero. The audit found 19 functions
whose availability varies by compositor and 6 that are unavailable everywhere,
which makes a bare zero impossible to act on: the caller cannot tell "the click
missed" from "this desktop cannot click". Every failure here is therefore typed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .capabilities import Capability


class PyGUITestError(Exception):
    """Base for every error raised by this package."""


class BackendUnavailable(PyGUITestError):
    """No backend could drive the current session."""


class CapabilityUnsupported(PyGUITestError):
    """The active backend cannot perform this operation.

    Carries the capability and the reason so callers can skip rather than fail
    -- the intended pattern for test suites spanning several desktops.
    """

    def __init__(
        self,
        capability: Capability,
        backend: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Record which capability failed, on which backend, and why."""
        self.capability = capability
        self.backend = backend
        self.reason = reason
        where = f" on {backend}" if backend else ""
        why = f": {reason}" if reason else ""
        super().__init__(f"{capability.name} is unsupported{where}{why}")


class PermissionRequired(CapabilityUnsupported):
    """The operation exists but was not granted.

    Raised for a declined portal dialog or an inaccessible /dev/uinput -- both
    recoverable by user action, unlike CapabilityUnsupported generally.
    """


class PortalTimeout(PyGUITestError):
    """A portal request was accepted but never answered.

    Distinct from PermissionRequired, which means the user actively declined:
    here the portal took the call and no Response signal ever arrived. The
    ordinary cause is a consent dialog nobody answered; the one that makes
    this a typed error rather than a hang is a portal that died mid-request,
    leaving a caller waiting on a signal that can never come.
    """

    def __init__(self, method: str, timeout: float) -> None:
        """Record which portal method timed out, and after how long."""
        self.method = method
        self.timeout = timeout
        super().__init__(
            f"the portal did not answer {method} within {timeout:g}s "
            "(an unanswered consent dialog, or a portal that stopped responding)"
        )


class ElementNotFound(PyGUITestError):
    """No accessible element matched the search.

    Raised by the convenience finders rather than returning None, so a script
    fails where the mistake is rather than several lines later on an attribute
    of None.
    """


class WindowNotFound(PyGUITestError):
    """No window matched, or a handle refers to a window that has closed."""


class FocusMismatch(PyGUITestError):
    """The wrong element (or nothing) has keyboard focus.

    Raised by assert_focused/assert_tab_order -- unlike ElementNotFound, the
    element usually does exist; it simply is not the focused one.
    """


class AccessibilityViolation(PyGUITestError):
    """The accessible tree is missing names, or reuses one ambiguously.

    Raised by assert_accessible and friends. Unlike ElementNotFound the
    elements are all present -- the complaint is about how they are
    labelled, which is what a screen reader reads out and what this
    package's own locators match on.
    """


class ClipboardMismatch(PyGUITestError):
    """The clipboard does not hold the text that was expected.

    Raised by assert_clipboard. Distinguishes the selection at fault
    (clipboard proper or PRIMARY) in the message, since the two are
    independent and a caller reading the report should not have to guess
    which one was checked.
    """


class ImageNotFound(PyGUITestError):
    """No match for the template image cleared the similarity threshold."""
