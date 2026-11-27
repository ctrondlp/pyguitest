"""The `via=` click route: resolution, capability gating, and the session default.

`via=` is the knob that lets a test choose how `element.click()` acts: through
the accessible action only ("action"), through the pointer only ("pointer"),
or through today's ladder ("auto"). This file covers the two shared helpers the
three backends call -- `resolve_click_route` and `require_click_route` -- and
the session-wide `click_via` default they read. The per-backend routing lives
in each backend's own test file.
"""

from __future__ import annotations

import unittest
from unittest import mock

import pyguitest
from pyguitest import Capability
from pyguitest.backends.base import (
    CLICK_ROUTES,
    require_click_route,
    resolve_click_route,
)
from pyguitest.errors import CapabilityUnsupported, ElementNotActionable


class _Element:
    """An element carrying (or not) the session that handed it out."""

    def __init__(self, session=None):
        self._session = session


class _Session:
    """A session stub with a `click_via` and a `require` that records and raises."""

    def __init__(self, click_via="auto", capabilities=()):
        self.click_via = click_via
        self._capabilities = set(capabilities)
        self.required = []

    def require(self, *capabilities):
        for capability in capabilities:
            if capability not in self._capabilities:
                raise CapabilityUnsupported(capability, "fake", "not offered")
            self.required.append(capability)


class TestResolveClickRoute(unittest.TestCase):
    def test_none_uses_the_session_default(self):
        element = _Element(_Session(click_via="pointer"))
        self.assertEqual(resolve_click_route(element, None), "pointer")

    def test_none_without_a_session_is_auto(self):
        self.assertEqual(resolve_click_route(_Element(), None), "auto")

    def test_an_explicit_route_wins_over_the_session(self):
        element = _Element(_Session(click_via="pointer"))
        self.assertEqual(resolve_click_route(element, "action"), "action")

    def test_an_unknown_route_is_refused(self):
        # A caller who spelled a route wrongly should hear about it, not get a
        # silent "auto".
        with self.assertRaises(ValueError):
            resolve_click_route(_Element(), "middle")

    def test_every_documented_route_is_accepted(self):
        for route in CLICK_ROUTES:
            self.assertEqual(resolve_click_route(_Element(), route), route)


class TestTheSessionDefaultIsCheckedWhereItIsSet(unittest.TestCase):
    """`click_via` is validated on assignment, not on the first click.

    `resolve_click_route` only sees the session default when an element click
    passes no `via` of its own, so a misspelling in `connect(...)` used to be
    reported from somewhere in the middle of a test rather than from the line
    that made it -- and a suite that only ever passes `via=` explicitly never
    heard about it at all.
    """

    def _session(self, **kwargs):
        return pyguitest.Session(pyguitest.NullBackend(), pyguitest.detect(), **kwargs)

    def test_the_default_is_auto(self):
        self.assertEqual(self._session().click_via, "auto")

    def test_every_documented_route_is_accepted(self):
        for route in CLICK_ROUTES:
            self.assertEqual(self._session(click_via=route).click_via, route)

    def test_a_misspelled_route_is_refused_at_construction(self):
        with self.assertRaises(ValueError) as caught:
            self._session(click_via="pointr")
        self.assertIn("pointr", str(caught.exception))

    def test_a_misspelled_route_is_refused_mid_run_too(self):
        # The documented use: one assignment switches a whole suite's policy.
        session = self._session()
        with self.assertRaises(ValueError):
            session.click_via = "keyboard"
        # And the good value it had is still the one in force.
        self.assertEqual(session.click_via, "auto")


class TestRequireClickRoute(unittest.TestCase):
    def test_pointer_requires_the_pointer_half(self):
        session = _Session(
            capabilities=(
                Capability.ELEMENT_GEOMETRY,
                Capability.POINTER_MOVE,
                Capability.POINTER_BUTTON,
            )
        )
        require_click_route(_Element(session), "pointer")
        self.assertEqual(
            set(session.required),
            {
                Capability.ELEMENT_GEOMETRY,
                Capability.POINTER_MOVE,
                Capability.POINTER_BUTTON,
            },
        )

    def test_action_requires_the_action(self):
        session = _Session(capabilities=(Capability.ELEMENT_ACTION,))
        require_click_route(_Element(session), "action")
        self.assertEqual(session.required, [Capability.ELEMENT_ACTION])

    def test_pointer_without_a_pointer_backend_fails_loudly(self):
        # The whole point of via="pointer": a headless or consent-less session
        # fails at the call rather than a no-op later.
        with self.assertRaises(CapabilityUnsupported):
            require_click_route(_Element(_Session()), "pointer")

    def test_auto_requires_nothing(self):
        session = _Session()
        require_click_route(_Element(session), "auto")
        self.assertEqual(session.required, [])

    def test_via_pointer_without_a_session_is_refused(self):
        # The assumption this used to encode -- "nothing to require; the
        # route itself reports the real problem" -- held on UIA and AX and
        # not on AT-SPI, where `_can_drive_a_pointer(None)` is False and the
        # call fell to dogtail's own coordinate click, which that backend
        # measures as going nowhere and raising nothing on GNOME/Wayland.
        with self.assertRaises(CapabilityUnsupported):
            require_click_route(_Element(), "pointer")

    def test_the_other_routes_still_require_nothing_without_a_session(self):
        # Only the pointer route needs a session to aim with; `action` is the
        # element's own, and `auto` declares nothing by design.
        require_click_route(_Element(), "action")
        require_click_route(_Element(), "auto")


class TestSessionClickVia(unittest.TestCase):
    def _session(self, **kwargs):
        backend = mock.Mock()
        backend.name = "fake"
        backend.capabilities = frozenset()
        return pyguitest.Session(backend, pyguitest.detect(), **kwargs)

    def test_defaults_to_auto(self):
        self.assertEqual(self._session().click_via, "auto")

    def test_constructor_argument(self):
        self.assertEqual(self._session(click_via="pointer").click_via, "pointer")

    def test_settable_mid_run(self):
        session = self._session()
        session.click_via = "action"
        self.assertEqual(session.click_via, "action")


if __name__ == "__main__":
    unittest.main()


class _Screen:
    def __init__(self, width, height):
        self.width, self.height = width, height


class TestAnUnreachableAimIsRefused(unittest.TestCase):
    """A rectangle bigger than the desktop aimed the pointer off it, silently.

    `move_mouse` posts whatever it is given with no bounds check, so the click
    landed wherever the window server clamped it -- on whatever happened to be
    there. Measured on macOS 26.7, where AX reports a text area's *document*
    extent rather than its viewport: a 4000-line document in a 673x439
    TextEdit window published extents (242, 139, 656, 52013) on a 1920x1080
    screen, aiming at (570, 26145), about 26,000 pixels below the display.

    Checked in `_element_center`, which is where the point the pointer is sent
    to is computed. A first version guarded a rectangle read earlier, in
    `click_by_pointer`, while the gesture went on to read extents again --
    so the validated point and the injected point could differ.

    The bound is the *summed* width and height of the outputs, not each
    output's own rectangle, because `Screen` carries size and deliberately
    not position. Comparing against each output from the origin rejects a
    point genuinely visible on a second monitor, and rejects every negative
    coordinate, which is where an output left of the primary lives.
    """

    MEASURED = (242, 139, 656, 52013)

    def _session(self, extents, screens=((1920, 1080),)):
        session = pyguitest.Session(mock.Mock(), pyguitest.detect())
        session.extents = lambda element: extents
        session.screens = lambda: [_Screen(w, h) for w, h in screens]
        return session

    def _element(self):
        element = mock.Mock()
        element.role, element.name = "text", "Body"
        return element

    def test_the_measured_macos_rectangle_is_refused(self):
        session = self._session(self.MEASURED)
        with self.assertRaises(ElementNotActionable) as caught:
            session._element_center(self._element(), "click")
        message = str(caught.exception)
        self.assertIn("(570, 26145)", message)
        self.assertIn("no arrangement", message)

    def test_an_ordinary_rectangle_is_aimed_at_normally(self):
        session = self._session((100, 100, 80, 24))
        self.assertEqual(session._element_center(self._element(), "click"), (140, 112))

    def test_a_point_on_a_second_output_is_allowed_through(self):
        # Two side-by-side 1920-wide outputs: (2100, 100) is visible on the
        # second one. Checking each output's dimensions from the origin would
        # reject it, because 2100 exceeds both widths.
        session = self._session(
            (2000, 50, 200, 100), screens=((1920, 1080), (1920, 1080))
        )
        self.assertEqual(session._element_center(self._element(), "click"), (2100, 100))

    def test_a_negative_coordinate_is_allowed_through(self):
        # Where an output left of the primary lives.
        session = self._session(
            (-900, -400, 200, 100), screens=((1920, 1080), (1920, 1080))
        )
        self.assertEqual(
            session._element_center(self._element(), "click"), (-800, -350)
        )

    def test_a_session_that_cannot_enumerate_screens_does_not_refuse(self):
        # "Cannot tell" must not become "cannot click": X11 reports no scale
        # and some compositions provide no SCREEN_INFO at all.
        session = pyguitest.Session(mock.Mock(), pyguitest.detect())
        session.extents = lambda element: self.MEASURED
        session.screens = lambda: (_ for _ in ()).throw(
            CapabilityUnsupported(Capability.SCREEN_INFO, "fake")
        )
        self.assertEqual(
            session._element_center(self._element(), "click"), (570, 26145)
        )

    def test_an_empty_screen_list_does_not_refuse(self):
        session = self._session(self.MEASURED, screens=())
        self.assertEqual(
            session._element_center(self._element(), "click"), (570, 26145)
        )
