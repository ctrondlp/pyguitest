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
from pyguitest.errors import CapabilityUnsupported


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

    def test_an_element_with_no_session_requires_nothing(self):
        # Nothing to require; the route itself reports the real problem.
        require_click_route(_Element(), "pointer")


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
