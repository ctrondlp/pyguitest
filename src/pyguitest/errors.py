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


class VersionTooOld(PyGUITestError):
    """The installed pyguitest is older than the floor a script declared.

    Raised by require_version(). Exists for scripts that outlive the machine
    they were written on: a generated or shared script states the release it
    was checked against and an older install fails right at that line, naming
    both versions and the upgrade command, instead of part way through a run
    with an AttributeError on an API the older release does not have.
    """

    def __init__(self, minimum: str, installed: str) -> None:
        """Record the floor the script declared and the version actually here."""
        self.minimum = minimum
        self.installed = installed
        super().__init__(
            f"this script requires pyguitest >= {minimum}, but {installed} is "
            f"installed -- upgrade with `pip install 'pyguitest>={minimum}'`"
        )


# What every CapabilityUnsupported message ends with. It lives in the
# exception rather than at the raise sites because it is the same sentence at
# all of them, and the one that matters most is the plainest: a capability no
# backend here serves. That message used to name what failed and nothing else,
# so the commonest failure in the package read like a bug in it.
_DOCTOR_HINT = ". Run `pyguitest doctor` to see what this desktop is missing."

# PermissionRequired's version of the same tail. No install closes that one --
# the capability is there and the grant is not -- so this names the action the
# user has to take, and keeps the report for what is left after it.
_GRANT_HINT = (
    ". Grant it to the process running the test and retry; `pyguitest doctor` "
    "names what is still missing."
)


class CapabilityUnsupported(PyGUITestError):
    """The active backend cannot perform this operation.

    Carries the capability and the reason so callers can skip rather than fail
    -- the intended pattern for test suites spanning several desktops.

    The message also carries what to do about it. Raised from just over sixty
    places, and before this tail existed the most common one was the only
    failure in the package that said what went wrong without one word about
    the fix.
    """

    def __init__(
        self,
        capability: Capability,
        backend: str | None = None,
        reason: str | None = None,
        hint: str | None = None,
    ) -> None:
        """Record which capability failed, on which backend, why, and the fix.

        `hint` replaces the last sentence for a call site that knows something
        more specific than "run doctor"; leaving it out is the ordinary case.
        """
        self.capability = capability
        self.backend = backend
        self.reason = reason
        self.hint = _DOCTOR_HINT if hint is None else hint
        where = f" on {backend}" if backend else ""
        why = f": {reason}" if reason else ""
        super().__init__(f"{capability.name} is unsupported{where}{why}{self.hint}")


class PermissionRequired(CapabilityUnsupported):
    """The operation exists but was not granted.

    Raised for a declined portal dialog or an inaccessible /dev/uinput -- both
    recoverable by user action, unlike CapabilityUnsupported generally.
    """

    def __init__(
        self,
        capability: Capability,
        backend: str | None = None,
        reason: str | None = None,
        hint: str | None = None,
    ) -> None:
        """Record the capability, and that this one is granted rather than installed."""
        super().__init__(
            capability,
            backend=backend,
            reason=reason,
            hint=_GRANT_HINT if hint is None else hint,
        )


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


class BackendCallTimeout(PyGUITestError):
    """A single backend call ran past its own time budget and was abandoned.

    Distinct from the `timeout` on a `wait_for_*`/`wait_until` call, which
    bounds how long *polling* waits for a condition to become true between
    repeated calls to the backend. This is the narrower, stranger failure
    underneath that: one single call -- not a loop of them -- simply never
    returns at all, with no exception and nothing left to poll. Confirmed
    live against AT-SPI/dogtail (GhostBSD, a `QTreeView` in a torture-test
    fixture): one `find_elements()` call touching that one widget's subtree
    locked up completely -- not slow, stuck -- and took the whole calling
    script down with it, since every `wait_for_*` timeout in that script was
    only ever checking its deadline *between* calls to a function that
    itself never returned control to Python at all. `signal.alarm` does not
    reliably help here either: confirmed live that it did not interrupt this
    specific hang, which is stuck inside a blocking native (GI/D-Bus) call
    that never hands control back to the interpreter loop, and CPython only
    delivers a pending signal between bytecode instructions.

    The call is not actually killed when this is raised -- nothing in Python
    can forcibly stop another thread's blocking native call -- so it may
    still be running in the background afterward; see whichever backend
    raised this for what that means there.
    """

    def __init__(self, what: str, timeout: float) -> None:
        """Record what call this was and the budget it exceeded."""
        self.what = what
        self.timeout = timeout
        super().__init__(
            f"{what} did not return within {timeout:g}s and was abandoned "
            "(the call itself hung, not merely ran slowly -- see "
            "BackendCallTimeout's own docstring)"
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


class ElementNotActionable(PyGUITestError):
    """Element.click() has no way left to act on this element.

    The typed answer for "this element is here and cannot be pressed", as
    against ElementNotFound ("it is not there") and CapabilityUnsupported
    ("this backend cannot do that at all"). The wording is the backend's,
    which is why one is passed in: what can go wrong is a property of the
    platform's accessibility model, not of this package.

    On Linux it is dogtail's own coordinate click needing GNOME's
    gnome-ponytail-daemon -- absent on every other Wayland compositor -- and
    the element's AT-SPI actions holding neither "click" nor "press" to fall
    back to. Seen on KDE's QML-based Kickoff menu, whose category labels
    expose no Action interface at all; coordinate-based clicking still works
    there, only the coordinate-free path does not. On Windows it is a UIA
    element publishing none of the Invoke, Toggle or LegacyIAccessible
    patterns, which leaves no accessible action to perform either.
    """

    def __init__(self, role: str, name: str, reason: str | None = None) -> None:
        """Record which element could not be acted on, and why not.

        `reason`, given, replaces the AT-SPI wording -- so the type stays the
        same thing to a caller catching it on either platform, while the
        sentence says what happened on the one they are running.
        """
        self.role = role
        self.name = name
        if reason is None:
            reason = (
                f"{role} {name!r} offers no click or press action, and this "
                "compositor's coordinate click needs GNOME's "
                "gnome-ponytail-daemon -- click by coordinate instead, e.g. "
                "gui.extents(element) then gui.click()"
            )
        super().__init__(reason)
