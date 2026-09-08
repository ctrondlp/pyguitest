"""InputCaptureBackend tests against a stand-in libei.portal module.

Not exercised against a real portal anywhere in this file, and never will
be by a unit test: see InputCaptureSession's own docstring in python-libei
for why -- verifying it needs a human to click through a real consent
dialog and accept that their pointer will be diverted from their own
desktop for the length of the test. This file proves InputCaptureBackend's
own orchestration: negotiation, error translation, the barrier-perimeter
arithmetic, and the release-before-anything-else ordering the module
docstring promises.
"""

from __future__ import annotations

import enum
import types
import unittest
from typing import NamedTuple
from unittest import mock

from pyguitest.capabilities import Capability
from pyguitest.errors import BackendUnavailable, PermissionRequired, PyGUITestError


class FakePortalError(Exception):
    """Stands in for libei.portal.PortalError, the base of the hierarchy."""


class FakePortalDeniedError(FakePortalError):
    def __init__(self, step, message=None):
        super().__init__(message or f"{step} was not approved")
        self.step = step
        self.message = message


class FakePortalTimeoutError(FakePortalError):
    def __init__(self, step, timeout):
        super().__init__(f"{step} did not answer within {timeout:g}s")
        self.step = step
        self.timeout = timeout


class FakeDeviceType(enum.IntFlag):
    ALL_DEVICES = 0
    KEYBOARD = 1
    POINTER = 2
    TOUCHSCREEN = 4


class FakePersistMode(enum.IntEnum):
    NONE = 0
    WHILE_RUNNING = 1
    UNTIL_REVOKED = 2


class FakeActivation(NamedTuple):
    activation_id: int
    cursor_position: tuple[float, float] | None
    barrier_id: int | None


class FakeInputCaptureSession:
    """A negotiated session: zones, barriers, and activation on request."""

    def __init__(
        self,
        zones=None,
        zone_set=1,
        activation=None,
        restore_token=None,
        timeout_on_wait=False,
        failed_barriers=(),
    ):
        self.restore_token = restore_token
        self._zones = [(1920, 1080, 0, 0)] if zones is None else zones
        self._zone_set = zone_set
        self._activation = (
            FakeActivation(5, (960.0, 540.0), 1) if activation is None else activation
        )
        self._timeout_on_wait = timeout_on_wait
        self._failed_barriers = list(failed_barriers)
        self.calls: list[tuple] = []
        self.closed = False

    def zones(self, timeout=60.0):
        self.calls.append(("zones",))
        return self._zone_set, list(self._zones)

    def set_pointer_barriers(self, barriers, zone_set, timeout=60.0):
        self.calls.append(("set_pointer_barriers", barriers, zone_set))
        return list(self._failed_barriers)

    def enable(self, timeout=60.0):
        self.calls.append(("enable",))

    def disable(self, timeout=60.0):
        self.calls.append(("disable",))

    def release(self, activation_id, cursor_position=None, timeout=60.0):
        self.calls.append(("release", activation_id, cursor_position))

    def wait_for_activation(self, timeout=None):
        self.calls.append(("wait_for_activation", timeout))
        if self._timeout_on_wait:
            raise FakePortalTimeoutError("Activated", timeout or 0)
        return self._activation

    def close(self):
        self.closed = True


def install_fake_portal(session=None, error=None):
    """Patch _portal() to return a stand-in libei.portal module.

    Mirrors test_eiinput.py's install_fake_portal exactly: patching the
    probe rather than sys.modules keeps `from libei import portal` out of
    it, and it is the *only* thing that reached the real portal in this
    package's history -- see the incident recorded in python-libei's own
    tests/test_inputcapture.py for what skipping this looks like.
    """
    portal = types.ModuleType("libei.portal")
    portal.DeviceType = FakeDeviceType
    portal.PersistMode = FakePersistMode
    portal.PortalError = FakePortalError
    portal.PortalDeniedError = FakePortalDeniedError
    portal.PortalTimeoutError = FakePortalTimeoutError
    portal.is_available = lambda: True
    portal.calls = []

    def negotiate(**kwargs):
        portal.calls.append(kwargs)
        if error is not None:
            raise error
        return session if session is not None else FakeInputCaptureSession()

    portal.InputCaptureSession = types.SimpleNamespace(negotiate=negotiate)
    return (
        mock.patch("pyguitest.backends.inputcapture._portal", return_value=portal),
        portal,
    )


class TestAvailability(unittest.TestCase):
    def test_available_when_portal_is_available(self):
        portal_patcher, _portal = install_fake_portal()
        with portal_patcher:
            from pyguitest.backends import inputcapture

            self.assertTrue(inputcapture.available())

    def test_unavailable_when_portal_probe_returns_none(self):
        with mock.patch("pyguitest.backends.inputcapture._portal", return_value=None):
            from pyguitest.backends import inputcapture

            self.assertFalse(inputcapture.available())

    def test_unavailable_when_python_libei_is_too_old(self):
        # A python-libei installed before 0.5.0 imports libei.portal fine
        # and answers is_available() truthfully -- InputCaptureSession is
        # what it lacks, and that has to be checked for by name, not
        # inferred from is_available() alone.
        portal_patcher, portal = install_fake_portal()
        del portal.InputCaptureSession
        with portal_patcher:
            from pyguitest.backends import inputcapture

            self.assertFalse(inputcapture.available())


class TestNegotiation(unittest.TestCase):
    def test_successful_construction_reads_the_restore_token(self):
        portal_patcher, _portal = install_fake_portal(
            session=FakeInputCaptureSession(restore_token="tok-next")
        )
        with portal_patcher:
            from pyguitest.backends import inputcapture

            gui = inputcapture.InputCaptureBackend()
        self.assertEqual(gui.restore_token, "tok-next")

    def test_capabilities_persist_mode_and_restore_token_are_forwarded(self):
        portal_patcher, portal = install_fake_portal()
        with portal_patcher:
            from pyguitest.backends import inputcapture

            inputcapture.InputCaptureBackend(
                capabilities=FakeDeviceType.POINTER,
                persist_mode=2,
                restore_token="tok-old",
            )
        (call,) = portal.calls
        self.assertEqual(call["capabilities"], FakeDeviceType.POINTER)
        self.assertEqual(call["persist_mode"], FakePersistMode.UNTIL_REVOKED)
        self.assertEqual(call["restore_token"], "tok-old")

    def test_default_capabilities_is_pointer_alone(self):
        # This backend has exactly one operation; asking for KEYBOARD or
        # TOUCHSCREEN too would only widen what the consent dialog lists.
        portal_patcher, portal = install_fake_portal()
        with portal_patcher:
            from pyguitest.backends import inputcapture

            inputcapture.InputCaptureBackend()
        (call,) = portal.calls
        self.assertEqual(call["capabilities"], FakeDeviceType.POINTER)

    def test_a_declined_dialog_raises_permission_required(self):
        portal_patcher, _portal = install_fake_portal(
            error=FakePortalDeniedError("Start")
        )
        with portal_patcher:
            from pyguitest.backends import inputcapture

            with self.assertRaises(PermissionRequired) as ctx:
                inputcapture.InputCaptureBackend()
        self.assertIs(ctx.exception.capability, Capability.INPUT_CAPTURE)

    def test_any_other_portal_error_raises_backend_unavailable(self):
        portal_patcher, _portal = install_fake_portal(
            error=FakePortalError("no session bus")
        )
        with portal_patcher:
            from pyguitest.backends import inputcapture

            with self.assertRaises(BackendUnavailable):
                inputcapture.InputCaptureBackend()

    def test_missing_portal_raises_backend_unavailable(self):
        with mock.patch("pyguitest.backends.inputcapture._portal", return_value=None):
            from pyguitest.backends import inputcapture

            with self.assertRaises(BackendUnavailable):
                inputcapture.InputCaptureBackend()

    def test_too_old_python_libei_raises_a_clear_backend_unavailable(self):
        portal_patcher, portal = install_fake_portal()
        del portal.InputCaptureSession
        with portal_patcher:
            from pyguitest.backends import inputcapture

            with self.assertRaises(BackendUnavailable) as ctx:
                inputcapture.InputCaptureBackend()
        self.assertIn("0.5.0", str(ctx.exception))


class InputCaptureTestCase(unittest.TestCase):
    def setUp(self):
        portal_patcher, self.portal = install_fake_portal()
        portal_patcher.start()
        self.addCleanup(portal_patcher.stop)
        from pyguitest.backends import inputcapture

        self.module = inputcapture
        self.session = FakeInputCaptureSession()
        with mock.patch.object(
            self.portal.InputCaptureSession, "negotiate", return_value=self.session
        ):
            self.gui = inputcapture.InputCaptureBackend()


class TestCapabilities(InputCaptureTestCase):
    def test_declares_input_capture_only(self):
        self.assertEqual(set(self.gui.capabilities), {Capability.INPUT_CAPTURE})


class TestWaitForPointerActivation(InputCaptureTestCase):
    def test_returns_the_cursor_position(self):
        position = self.gui.wait_for_pointer_activation(timeout=5.0)
        self.assertEqual(position, (960.0, 540.0))

    def test_calls_happen_in_the_documented_order(self):
        self.gui.wait_for_pointer_activation(timeout=5.0)
        names = [call[0] for call in self.session.calls]
        self.assertEqual(
            names,
            [
                "zones",
                "set_pointer_barriers",
                "enable",
                "wait_for_activation",
                "release",
                "disable",
            ],
        )

    def test_release_happens_before_disable(self):
        # The module docstring's promise: release the instant the answer
        # has been read, before anything else -- not folded into cleanup.
        self.gui.wait_for_pointer_activation(timeout=5.0)
        names = [call[0] for call in self.session.calls]
        self.assertLess(names.index("release"), names.index("disable"))

    def test_release_is_called_with_the_activation_id_and_position(self):
        self.gui.wait_for_pointer_activation(timeout=5.0)
        release_call = next(c for c in self.session.calls if c[0] == "release")
        self.assertEqual(release_call[1:], (5, (960.0, 540.0)))

    def test_a_single_zone_gets_a_barrier_on_each_of_its_own_edges(self):
        self.gui.wait_for_pointer_activation(timeout=5.0)
        barriers_call = next(
            c for c in self.session.calls if c[0] == "set_pointer_barriers"
        )
        barriers, zone_set = barriers_call[1], barriers_call[2]
        self.assertEqual(zone_set, 1)
        positions = {b[1:] for b in barriers}
        self.assertEqual(
            positions,
            {
                (0, 0, 1919, 0),  # top
                (0, 1080, 1919, 1080),  # bottom
                (0, 0, 0, 1079),  # left
                (1920, 0, 1920, 1079),  # right
            },
        )
        # Every barrier_id is non-zero, and no two collide.
        ids = [b[0] for b in barriers]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(ids))

    def test_two_zones_get_the_bounding_box_perimeter(self):
        self.session._zones = [(1920, 1080, 0, 0), (1280, 1024, 1920, 0)]
        self.gui.wait_for_pointer_activation(timeout=5.0)
        barriers_call = next(
            c for c in self.session.calls if c[0] == "set_pointer_barriers"
        )
        positions = {b[1:] for b in barriers_call[1]}
        # Bounding box: x in [0, 3200), y in [0, 1080) (1024 < 1080).
        self.assertEqual(
            positions,
            {
                (0, 0, 3199, 0),
                (0, 1080, 3199, 1080),
                (0, 0, 0, 1079),
                (3200, 0, 3200, 1079),
            },
        )

    def test_a_timeout_returns_none_rather_than_raising(self):
        self.session._timeout_on_wait = True
        self.assertIsNone(self.gui.wait_for_pointer_activation(timeout=0.01))

    def test_every_barrier_refused_raises_instead_of_waiting_out_the_timeout(self):
        # Found live: the first two real runs timed out with nothing to say
        # why, because set_pointer_barriers()'s return value was discarded
        # entirely -- a caller with no barrier standing would otherwise
        # wait the full timeout for an activation that could never come.
        self.session._failed_barriers = [1, 2, 3, 4]
        with self.assertRaises(PyGUITestError) as ctx:
            self.gui.wait_for_pointer_activation(timeout=5.0)
        self.assertIn("refused", str(ctx.exception))

    def test_every_barrier_refused_never_calls_enable(self):
        self.session._failed_barriers = [1, 2, 3, 4]
        with self.assertRaises(PyGUITestError):
            self.gui.wait_for_pointer_activation(timeout=5.0)
        names = [call[0] for call in self.session.calls]
        self.assertNotIn("enable", names)
        self.assertNotIn("wait_for_activation", names)

    def test_a_partial_barrier_refusal_still_waits(self):
        # Some edges standing is still a real chance of activation -- only
        # a *total* refusal means nothing could ever trigger.
        self.session._failed_barriers = [2]  # only the bottom barrier
        position = self.gui.wait_for_pointer_activation(timeout=5.0)
        self.assertEqual(position, (960.0, 540.0))
        names = [call[0] for call in self.session.calls]
        self.assertIn("enable", names)

    def test_a_timeout_disables_but_never_releases(self):
        # Nothing was ever activated, so there is nothing to hand back --
        # calling release() on a timeout would be a Release with no real
        # activation_id.
        self.session._timeout_on_wait = True
        self.gui.wait_for_pointer_activation(timeout=0.01)
        names = [call[0] for call in self.session.calls]
        self.assertIn("disable", names)
        self.assertNotIn("release", names)

    def test_no_cursor_position_raises_rather_than_returning_none_silently(self):
        # None already means "timed out" -- reusing it for "activated but
        # the compositor sent no position" would make the two outcomes
        # indistinguishable to a caller.
        self.session._activation = FakeActivation(5, None, None)
        with self.assertRaises(PyGUITestError):
            self.gui.wait_for_pointer_activation(timeout=5.0)

    def test_no_zones_means_no_barriers_and_no_crash(self):
        self.session._zones = []
        self.gui.wait_for_pointer_activation(timeout=5.0)
        barriers_call = next(
            c for c in self.session.calls if c[0] == "set_pointer_barriers"
        )
        self.assertEqual(barriers_call[1], [])


class TestClose(InputCaptureTestCase):
    def test_close_delegates_to_the_session(self):
        self.gui.close()
        self.assertTrue(self.session.closed)


if __name__ == "__main__":
    unittest.main()
