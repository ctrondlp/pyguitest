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

    **Only a single-output desktop is checked**, because `Screen` carries size
    and deliberately not position. A second version summed the output sizes,
    on the reasoning that no arrangement could exceed the sum -- wrong, as
    sway, Hyprland and RandR all let an output sit at an explicit coordinate
    with a gap before it: two 1920-wide outputs at x=0 and x=3000 make x=4000
    reachable while the sum says 3840. With one output the coordinate space
    *is* that output, from (0, 0), so the check runs there and steps aside
    everywhere else.
    """

    MEASURED = (242, 139, 656, 52013)

    def _session(self, extents, screens=((1920, 1080),), screen_info=True):
        session = pyguitest.Session(mock.Mock(), pyguitest.detect())
        session.extents = lambda element: extents
        session.screens = lambda: [_Screen(w, h) for w, h in screens]
        session.supports = lambda capability: (
            screen_info if capability is Capability.SCREEN_INFO else True
        )
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
        self.assertIn("1920x1080", message)

    def test_an_ordinary_rectangle_is_aimed_at_normally(self):
        session = self._session((100, 100, 80, 24))
        self.assertEqual(session._element_center(self._element(), "click"), (140, 112))

    def test_a_point_at_the_very_edge_is_allowed_through(self):
        # The bound is inclusive: a control flush with the bottom-right corner
        # is reachable, and refusing it would break a maximized window's last
        # pixel column.
        session = self._session((1918, 1078, 4, 4))
        self.assertEqual(
            session._element_center(self._element(), "click"), (1920, 1080)
        )

    def test_more_than_one_output_is_never_refused(self):
        # The sway/Hyprland/RandR case the summed-envelope version got wrong:
        # outputs can sit at explicit positions with gaps between them, so no
        # function of their *sizes* bounds the reachable space. (4000, 100) is
        # visible on an output placed at x=3000, while the sum says 3840.
        session = self._session(
            (3900, 50, 200, 100), screens=((1920, 1080), (1920, 1080))
        )
        self.assertEqual(session._element_center(self._element(), "click"), (4000, 100))

    def test_an_absurd_rectangle_is_allowed_through_on_several_outputs(self):
        # Deliberate, and the cost of the above: with no output positions
        # there is no bound to judge against, and "cannot tell" must not
        # become "cannot click".
        session = self._session(self.MEASURED, screens=((1920, 1080), (1920, 1080)))
        self.assertEqual(
            session._element_center(self._element(), "click"), (570, 26145)
        )

    def test_a_negative_coordinate_is_refused_on_a_single_output(self):
        # With one output the space starts at (0, 0), so a negative point is
        # off it -- and clamping would click whatever sits at the edge.
        session = self._session((-900, -400, 200, 100))
        with self.assertRaises(ElementNotActionable):
            session._element_center(self._element(), "click")

    def test_a_session_that_does_not_declare_screen_info_does_not_refuse(self):
        # The `supports()` guard, which is also what tells
        # scripts/gen-api-docs.py that SCREEN_INFO is optional here rather
        # than required for every element gesture.
        session = self._session(self.MEASURED, screen_info=False)
        self.assertEqual(
            session._element_center(self._element(), "click"), (570, 26145)
        )

    def test_a_session_that_cannot_enumerate_screens_does_not_refuse(self):
        # Declared but failing: "cannot tell" must not become "cannot click".
        session = pyguitest.Session(mock.Mock(), pyguitest.detect())
        session.extents = lambda element: self.MEASURED
        session.supports = lambda capability: True
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
