"""Read where the pointer is, via `org.freedesktop.portal.InputCapture`.

The read half of what `eiinput.py` is for the write direction. Every other
input capability in this package injects; this is the one that receives
real input from the user's own devices, which the portal only ever hands
over when the compositor decides to -- see `Session.
wait_for_pointer_activation` for exactly what that means and why it is not
a query.

The protocol driving is `libei.portal.InputCaptureSession`, not here, for
the same reason `RemoteDesktopSession` lives there rather than in
`eiinput.py`: it is ~700 lines of Request/Response plumbing that belongs to
one library, reusable by any caller of python-libei, not only this one.
What is here is the translation into pyguitest's own error vocabulary and
the one piece of business logic InputCaptureSession does not own --
choosing where to put the pointer barriers.

**Never live-tested.** See `InputCaptureSession`'s own docstring in
python-libei for why: verifying this needs a human to click through the
consent dialog and then accept that their pointer will be diverted away
from their own desktop for the length of the test. Unit-tested against a
fake backend only; see `tests/test_inputcapture.py`.
"""

from __future__ import annotations

from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable, PermissionRequired, PyGUITestError
from .base import GUIBackend

__all__ = ["InputCaptureBackend", "available"]

_DEFAULT_CAPABILITIES = 2  # POINTER, matching libei.portal.DeviceType


def _portal():
    """Import libei.portal, or return None if it cannot negotiate here.

    Mirrors eiinput.py's own `_portal()` exactly -- see that function's
    docstring for why both failure modes (python-libei absent, PyGObject
    absent) collapse to the same None.
    """
    try:
        from libei import portal
    except Exception:
        return None
    try:
        if not portal.is_available():
            return None
    except Exception:
        return None
    return portal


def available():
    """Whether python-libei's portal module is usable here.

    Also checks for `InputCaptureSession` itself, not only that `libei.
    portal` imports: the module has carried `RemoteDesktopSession` since
    0.3.0, so an installed-but-too-old python-libei would otherwise pass
    this check and only fail once this backend actually tried to negotiate
    -- an `AttributeError` from deep inside `__init__` instead of a clear
    answer here. `pyproject.toml`'s `eiinput` extra floor (0.5.0) exists
    to make this the untaken path, not the only guard against it.
    """
    portal = _portal()
    return portal is not None and hasattr(portal, "InputCaptureSession")


class InputCaptureBackend(GUIBackend):
    """One negotiated InputCapture session, serving `Capability.INPUT_CAPTURE`.

    Opt-in only, like `PortalBackend` and `LibeiBackend` -- construction can
    raise a real, interactive consent dialog, so a plain `connect()` must
    never reach this by surprise. Name it explicitly: ``connect(backend=
    "inputcapture")``.
    """

    name = "inputcapture"

    def __init__(
        self,
        connection=None,
        capabilities=_DEFAULT_CAPABILITIES,
        persist_mode=0,
        restore_token=None,
    ):
        """Negotiate an InputCapture portal session.

        `capabilities`/`persist_mode`/`restore_token` are the same three
        knobs `LibeiBackend`/`PortalBackend` already expose, with the same
        meaning -- see `LibeiBackend.__init__` for the persistence
        round-trip. `capabilities` defaults to POINTER alone: this backend
        has exactly one operation, and asking for KEYBOARD or TOUCHSCREEN
        too would only make the consent dialog list permissions nothing
        here uses.
        """
        portal = _portal()
        if portal is None:
            raise BackendUnavailable(
                "python-libei[portal] is not installed, or PyGObject is "
                "missing; pip install 'pyguitest[eiinput]' supplies both "
                "(see README)"
            )
        if not hasattr(portal, "InputCaptureSession"):
            raise BackendUnavailable(
                "this python-libei is too old for InputCaptureSession "
                "(needs 0.5.0+); pip install --upgrade "
                "'python-libei[portal]'"
            )
        self._portal = portal
        self.restore_token = None
        """The token to reuse next time, or None if the portal issued none."""
        try:
            self._session = portal.InputCaptureSession.negotiate(
                connection=connection,
                capabilities=portal.DeviceType(capabilities),
                persist_mode=portal.PersistMode(persist_mode),
                restore_token=restore_token,
            )
        except portal.PortalDeniedError as exc:
            raise PermissionRequired(
                Capability.INPUT_CAPTURE, self.name, str(exc)
            ) from exc
        except portal.PortalError as exc:
            # PortalVersionError, an unreachable session bus, or any other
            # portal-side failure -- none of them the user having said no.
            raise BackendUnavailable(str(exc)) from exc
        self.restore_token = self._session.restore_token

    @property
    def capabilities(self):
        """Just the one -- see the class docstring."""
        return CapabilitySet({Capability.INPUT_CAPTURE})

    def close(self):
        """Release the portal session and its EIS fd."""
        self._session.close()

    def _perimeter_barriers(self, zones):
        """Pointer barriers around the bounding box of every zone.

        Correct for the common case -- one zone, or several arranged as a
        simple rectangle -- and an approximation otherwise: the true outer
        boundary of a non-rectangular multi-monitor layout needs real
        polygon-boundary math, which nothing here has had the chance to
        verify against a real multi-monitor session (see the module
        docstring on why). A bounding box still places a barrier the
        pointer will cross on any edge movement in the common case; it can
        place one a screen-width early past an L-shaped layout's inner
        corner, which is flagged here rather than silently assumed away.
        """
        if not zones:
            return []
        min_x = min(x for _w, _h, x, _y in zones)
        min_y = min(y for _w, _h, _x, y in zones)
        max_x = max(x + w for w, _h, x, _y in zones)
        max_y = max(y + h for _w, h, x, y in zones)
        return [
            (1, min_x, min_y, max_x - 1, min_y),  # top
            (2, min_x, max_y, max_x - 1, max_y),  # bottom
            (3, min_x, min_y, min_x, max_y - 1),  # left
            (4, max_x, min_y, max_x, max_y - 1),  # right
        ]

    def wait_for_pointer_activation(self, timeout):
        """Block for one real crossing of a screen edge; see the Session docstring.

        Sets barriers around every zone's outer edge, enables capture,
        waits for the compositor to activate it, then hands input back to
        the desktop immediately -- `release()` is the very next call after
        reading the position, before anything else runs, to keep the
        exclusive-capture window as short as this can make it. A timeout
        (the ordinary outcome if nobody moves the pointer there) returns
        None rather than raising, matching every other `wait_for_*` method
        on Session; it never armed a real capture, so only `disable()` is
        needed on the way out, not `release()`.

        Raises PyGUITestError immediately, without ever calling `enable()`,
        if the compositor refused every barrier just requested --
        `set_pointer_barriers()` reports which ids it rejected, and a
        caller with none left standing would otherwise wait out the full
        timeout for an activation that could never come. Found live: the
        first real run of this method timed out twice in a row with
        nothing to say why, because this return value used to be
        discarded outright.
        """
        self.require(Capability.INPUT_CAPTURE)
        zone_set, zones = self._session.zones()
        barriers = self._perimeter_barriers(zones)
        failed = self._session.set_pointer_barriers(barriers, zone_set)
        if barriers and len(failed) == len(barriers):
            raise PyGUITestError(
                f"the compositor refused every pointer barrier "
                f"(failed_barriers={sorted(failed)} of "
                f"{sorted(b[0] for b in barriers)}); nothing could ever "
                f"trigger capture with none standing"
            )
        self._session.enable()
        try:
            activation = self._session.wait_for_activation(timeout)
        except self._portal.PortalTimeoutError:
            self._session.disable()
            return None
        self._session.release(activation.activation_id, activation.cursor_position)
        self._session.disable()
        if activation.cursor_position is None:
            # The spec permits an Activated with no cursor_position; this
            # method has nothing else to answer with if that happens.
            raise PyGUITestError(
                "the compositor activated input capture but reported no cursor position"
            )
        return activation.cursor_position
