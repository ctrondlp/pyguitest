"""LibeiBackend against the real native libei/libeis, entirely in-process.

Uses `libei.eis.Eis.create_for_fd()` as a fake compositor, exactly like
python-libei's own tests/test_integration_socketpair.py: real ei/eis
libraries, no portal, no consent dialog, no real compositor. This is the
layer that actually proves `pointer_motion_absolute`/`button`/`scroll_delta`
round-trip through the real wire protocol -- test_eiinput.py's fakes can only
prove LibeiBackend's own orchestration is shaped correctly.

Skips itself via unittest.SkipTest in setUpClass, the same way and for the
same reason test_portal_dbusmock.py does: this suite's other tests run under
plain `python3 -m unittest discover` with no dependencies at all, and that
runner reports a module-level pytest skip as a load *error*, not a skip.

Still does not exercise the real RemoteDesktop portal/liboeffis round-trip --
no automated test in this repository can click the consent dialog. See
eiinput.py's module docstring for what has and hasn't been verified live.
"""

from __future__ import annotations

import select
import time
import unittest

try:
    from libei import ei, eis
except ImportError:
    ei = None
    eis = None

from pyguitest.backends.eiinput import LibeiBackend


class LibeiIntegrationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if ei is None or eis is None:
            raise unittest.SkipTest(
                "python-libei is not importable, so the only real-libei test "
                "in this suite is being skipped. Install it with "
                "`pip install python-libei` (or `pip install '.[eiinput]'`), "
                "or run this suite with a checkout's src/ on PYTHONPATH"
            )
        if not (ei.is_available() and eis.is_available()):
            raise unittest.SkipTest("native libei/libeis are not installed")
        # liboeffis is deliberately NOT required: eiinput.py negotiates the
        # portal itself over Gio and never imports oeffis at all.

    def _connected_sender(self, seat_capabilities):
        """A real ei.Sender wired to a real eis.Eis fake-compositor server.

        Drives both sides' dispatch loops until the client sees SEAT_ADDED,
        binds `seat_capabilities`, and the server hands back a resumed
        device -- the same handshake python-libei's own
        test_pointer_motion_round_trips_through_negotiated_device performs.
        """
        server = eis.Eis.create_for_fd()
        client_fd = server.add_client()
        sender = ei.Sender.create_for_fd(client_fd, name="pyguitest-test")

        device_created = False
        client_device = None
        deadline = time.monotonic() + 10.0
        fds = {server.fd: server, sender.fd: sender}
        while client_device is None and time.monotonic() < deadline:
            ready, _, _ = select.select(list(fds), [], [], 0.2)
            for fd in ready:
                fds[fd].dispatch()

            for server_event in server.events:
                if server_event.event_type is eis.EventType.CLIENT_CONNECT:
                    server_event.client.connect()
                    seat = server_event.client.new_seat("test-seat")
                    seat.configure_capabilities(seat_capabilities)
                    seat.add()
                elif (
                    server_event.event_type is eis.EventType.SEAT_BIND
                    and not device_created
                ):
                    device_created = True
                    device = server_event.seat.new_device()
                    device.configure(
                        name="pyguitest-test-pointer", capabilities=seat_capabilities
                    )
                    device.add()
                    device.resume()

            for sender_event in sender.events:
                if (
                    sender_event.event_type is ei.EventType.SEAT_ADDED
                    and sender_event.seat is not None
                ):
                    offered = tuple(
                        cap
                        for cap in ei.DeviceCapability
                        if cap in sender_event.seat.capabilities
                    )
                    sender_event.seat.bind(offered)
                elif sender_event.event_type is ei.EventType.DEVICE_RESUMED:
                    client_device = sender_event.device

        self.assertIsNotNone(client_device, "device never reached DEVICE_RESUMED")
        return sender, client_device


@unittest.skipIf(ei is None, "python-libei is not installed")
class TestPointerMotionRoundTrips(LibeiIntegrationTestCase):
    def test_absolute_pointer_motion_round_trips_through_a_real_device(self):
        caps = (
            ei.DeviceCapability.POINTER_ABSOLUTE,
            ei.DeviceCapability.BUTTON,
            ei.DeviceCapability.SCROLL,
        )
        sender, device = self._connected_sender(caps)
        try:
            gui = LibeiBackend(sender=sender, device=device)
            gui.move_mouse(123, 45)
            gui.press_button(1)
            gui.release_button(1)
            gui.scroll(dx=0, dy=3)
        finally:
            sender.dispatch()


if __name__ == "__main__":
    unittest.main()
