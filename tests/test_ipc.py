"""Wire-protocol tests for the compositor transports.

These exercise the real framing over a socketpair, so the i3-ipc encoding is
verified without a running compositor -- the part most likely to be wrong,
since it is a binary format written from the specification. niri's framing is
line-delimited JSON instead, where the risk is reassembling a reply that does
not arrive in one recv().
"""

import json
import socket
import struct
import threading
import unittest

from pyguitest import ipc

MAGIC = b"i3-ipc"
HEADER = struct.Struct("=II")


def frame(payload, message_type):
    body = json.dumps(payload).encode()
    return MAGIC + HEADER.pack(len(body), message_type) + body


class TestSwayFraming(unittest.TestCase):
    def setUp(self):
        self.server, client = socket.socketpair()
        self.ipc = ipc.SwaySocket(path="<pair>", sock=client)
        self.addCleanup(self.server.close)
        self.addCleanup(self.ipc.close)

    def test_request_encodes_magic_length_and_type(self):
        self.server.sendall(frame({"ok": True}, ipc.GET_TREE))
        self.ipc.get_tree()
        sent = self.server.recv(4096)
        self.assertTrue(sent.startswith(MAGIC))
        length, message_type = HEADER.unpack(sent[len(MAGIC) : len(MAGIC) + 8])
        self.assertEqual(message_type, ipc.GET_TREE)
        self.assertEqual(length, 0)

    def test_reply_payload_is_parsed(self):
        self.server.sendall(frame({"id": 1, "nodes": []}, ipc.GET_TREE))
        self.assertEqual(self.ipc.get_tree(), {"id": 1, "nodes": []})

    def test_run_command_sends_its_payload(self):
        self.server.sendall(frame([{"success": True}], ipc.RUN_COMMAND))
        self.ipc.run_command("[con_id=7] focus")
        sent = self.server.recv(4096)
        self.assertIn(b"[con_id=7] focus", sent)
        length, message_type = HEADER.unpack(sent[len(MAGIC) : len(MAGIC) + 8])
        self.assertEqual(message_type, ipc.RUN_COMMAND)
        self.assertEqual(length, len("[con_id=7] focus"))

    def test_a_reply_split_across_packets_is_reassembled(self):
        # TCP-style short reads are the classic socket bug; the payload here is
        # deliberately delivered in two pieces.
        payload = frame({"name": "x" * 500}, ipc.GET_TREE)

        def feed():
            self.server.sendall(payload[:20])
            self.server.sendall(payload[20:])

        threading.Thread(target=feed, daemon=True).start()
        self.assertEqual(self.ipc.get_tree()["name"], "x" * 500)

    def test_bad_magic_is_rejected(self):
        self.server.sendall(b"XXXXXX" + HEADER.pack(2, 4) + b"{}")
        with self.assertRaises(OSError) as ctx:
            self.ipc.get_tree()
        self.assertIn("magic", str(ctx.exception))

    def test_closed_connection_raises(self):
        self.server.close()
        with self.assertRaises(OSError):
            self.ipc.get_tree()

    def test_subscribe_skips_the_acknowledgement_and_yields_events(self):
        self.server.sendall(frame({"success": True}, ipc.SUBSCRIBE))
        self.server.sendall(
            frame({"change": "new", "container": {"id": 3}}, ipc.EVENT_FLAG | 3)
        )
        events = ipc.SwaySocket.subscribe(self.ipc, ["window"])
        self.assertEqual(next(events)["change"], "new")

    def test_no_deadline_genuinely_blocks_past_the_ambient_default_timeout(self):
        # Regression: every socket carries DEFAULT_TIMEOUT from __init__ (a
        # real, auto-connected one; simulated here since the sock= test path
        # bypasses that). subscribe(deadline=None) must override it with an
        # explicit settimeout(None) before each read, or "waits
        # indefinitely" silently becomes "waits DEFAULT_TIMEOUT" -- exactly
        # the bug this whole timeout feature was added to fix, reintroduced
        # on the one path advertised as unbounded.
        self.ipc._sock.settimeout(0.05)
        self.server.sendall(frame({"success": True}, ipc.SUBSCRIBE))

        def send_after_delay():
            import time

            time.sleep(0.2)  # longer than the simulated ambient timeout
            self.server.sendall(
                frame({"change": "new", "container": {"id": 3}}, ipc.EVENT_FLAG | 3)
            )

        threading.Thread(target=send_after_delay, daemon=True).start()
        events = ipc.SwaySocket.subscribe(self.ipc, ["window"])
        self.assertEqual(next(events)["change"], "new")

    def test_default_timeout_is_restored_after_subscribing(self):
        # A request() made on the same connection afterwards must stay
        # protected by DEFAULT_TIMEOUT, not be left permanently blocking.
        self.server.sendall(frame({"success": True}, ipc.SUBSCRIBE))
        self.server.sendall(
            frame({"change": "new", "container": {"id": 3}}, ipc.EVENT_FLAG | 3)
        )
        events = ipc.SwaySocket.subscribe(self.ipc, ["window"], deadline=0)
        list(events)  # deadline=0 (already past) ends the generator at once
        self.assertEqual(self.ipc._sock.gettimeout(), ipc.DEFAULT_TIMEOUT)


class TestDiscovery(unittest.TestCase):
    def test_a_failed_connect_closes_the_socket_rather_than_leaking_it(self):
        # Regression: a socket() that failed to connect() was never closed.
        import os
        from unittest import mock

        created = []
        real_socket = socket.socket

        def recording_socket(*a, **kw):
            sock = real_socket(*a, **kw)
            created.append(sock)
            return sock

        with mock.patch.dict(
            os.environ, {"SWAYSOCK": "/nonexistent/path.sock"}, clear=True
        ):
            with mock.patch("socket.socket", side_effect=recording_socket):
                with self.assertRaises(OSError):
                    ipc.SwaySocket()

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0]._closed)

    def test_sway_socket_needs_an_environment_variable(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(OSError):
                ipc.SwaySocket()

    def test_hyprland_needs_an_instance_signature(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(OSError):
                ipc._hypr_dir()

    def test_connect_returns_none_when_nothing_is_available(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("shutil.which", return_value=None):
                self.assertIsNone(ipc.connect_sway())
                self.assertIsNone(ipc.connect_hyprland())
                self.assertIsNone(ipc.connect_niri())

    def test_niri_socket_needs_an_environment_variable(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(OSError):
                ipc.NiriSocket()

    def test_niri_cli_fallback_is_refused_without_the_env_signature(self):
        # Same rule as swaymsg: the tool being installed proves nothing
        # about which compositor is actually running.
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("shutil.which", return_value="/usr/bin/niri"):
                self.assertIsNone(ipc.connect_niri())

    def test_niri_cli_fallback_works_once_the_socket_is_advertised(self):
        import os
        from unittest import mock

        env = {"NIRI_SOCKET": "/nonexistent/niri.sock"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("shutil.which", return_value="/usr/bin/niri"):
                self.assertIsInstance(ipc.connect_niri(), ipc.NiriCLI)

    def test_sway_cli_fallback_is_refused_without_sway_env_signature(self):
        # Regression: swaymsg merely being on PATH was enough to fall back
        # to it, even on a Hyprland session where sway is not the running
        # compositor and swaymsg would just fail on every call.
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("shutil.which", return_value="/usr/bin/swaymsg"):
                self.assertIsNone(ipc.connect_sway())

    def test_sway_cli_fallback_works_once_the_socket_is_advertised(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"SWAYSOCK": "/run/sway.sock"}, clear=True):
            with mock.patch("shutil.which", return_value="/usr/bin/swaymsg"):
                self.assertIsInstance(ipc.connect_sway(), ipc.SwayCLI)


class TestNiriWire(unittest.TestCase):
    """niri's line protocol, over a real socketpair."""

    def _transport(self, *reply_lines):
        """A NiriSocket wired to a server that answers with `reply_lines`."""
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.received = []

        def serve():
            buffer = b""
            while b"\n" not in buffer:
                chunk = server.recv(4096)
                if not chunk:
                    return
                buffer += chunk
            self.received.append(buffer)
            for line in reply_lines:
                server.sendall(line)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)

        transport = ipc.NiriSocket(path="<pair>")
        transport._connect = lambda: client
        return transport

    def test_a_request_is_one_json_line_and_the_reply_is_unwrapped(self):
        transport = self._transport(b'{"Ok":{"Windows":[{"id":7}]}}\n')
        self.assertEqual(transport.windows(), [{"id": 7}])
        # Bare enum variants go on the wire as bare strings, not objects.
        self.assertEqual(self.received[0], b'"Windows"\n')

    def test_an_action_is_externally_tagged(self):
        transport = self._transport(b'{"Ok":"Handled"}\n')
        self.assertEqual(transport.action("FocusWindow", id=7), "Handled")
        self.assertEqual(
            json.loads(self.received[0]), {"Action": {"FocusWindow": {"id": 7}}}
        )

    def test_the_error_arm_raises_rather_than_returning_a_payload(self):
        transport = self._transport(b'{"Err":"no such window"}\n')
        with self.assertRaises(OSError) as caught:
            transport.windows()
        self.assertIn("no such window", str(caught.exception))

    def test_a_reply_split_across_reads_is_reassembled(self):
        # A long window list does not arrive in one recv(), and splitting
        # mid-token would make json.loads fail on a valid reply.
        transport = self._transport(b'{"Ok":{"Win', b'dows":[{"id":7}]}}\n')
        self.assertEqual(transport.windows(), [{"id": 7}])

    def test_two_events_arriving_in_one_read_are_yielded_separately(self):
        transport = self._transport(
            b'{"Ok":"Handled"}\n',
            b'{"WindowClosed":{"id":1}}\n{"WindowClosed":{"id":2}}\n',
        )
        events = []
        for event in transport.event_stream():
            events.append(event)
            if len(events) == 2:
                break
        self.assertEqual(
            events,
            [{"WindowClosed": {"id": 1}}, {"WindowClosed": {"id": 2}}],
        )
        self.assertEqual(self.received[0], b'"EventStream"\n')

    def test_an_unrecognisable_reply_is_an_error_not_a_silent_none(self):
        transport = self._transport(b'{"Something":1}\n')
        with self.assertRaises(OSError):
            transport.windows()


class TestNiriCLI(unittest.TestCase):
    def test_json_output_is_already_unwrapped(self):
        # `niri msg --json` prints the payload, not the Ok envelope, so the
        # CLI transport must not try to unwrap it a second time.
        calls = []

        def runner(argv):
            calls.append(argv)
            return json.dumps([{"id": 7}])

        transport = ipc.NiriCLI(runner=runner)
        self.assertEqual(transport.windows(), [{"id": 7}])
        self.assertEqual(calls[0], ["niri", "msg", "--json", "windows"])

    def test_actions_are_spelled_in_kebab_case_with_long_options(self):
        calls = []
        transport = ipc.NiriCLI(runner=lambda argv: calls.append(argv) or "")
        transport.action("FocusWindow", id=7)
        self.assertEqual(
            calls[0], ["niri", "msg", "action", "focus-window", "--id", "7"]
        )

    def test_malformed_event_lines_are_skipped(self):
        lines = ['{"WindowClosed":{"id":1}}\n', "\n", "not json\n"]
        transport = ipc.NiriCLI(streamer=lambda argv, deadline=None: iter(lines))
        self.assertEqual(list(transport.event_stream()), [{"WindowClosed": {"id": 1}}])

    def test_a_size_change_becomes_a_positional_argument(self):
        # Regression: SizeChange is an externally tagged enum over the
        # socket ({"SetFixed": 800}) and every argument here was
        # stringified into a long option, so resize_window emitted
        # `--change "{'SetFixed': 800}"` -- an argument no CLI parses.
        # niri takes the change positionally, in its own spelling.
        calls = []
        transport = ipc.NiriCLI(runner=lambda argv: calls.append(argv) or "")
        transport.action("SetWindowWidth", id=7, change={"SetFixed": 800})
        self.assertEqual(
            calls[0],
            ["niri", "msg", "action", "set-window-width", "--id", "7", "800"],
        )

    def test_every_size_change_variant_has_a_cli_spelling(self):
        for variant, amount, expected in (
            ("SetFixed", 800, "800"),
            ("SetProportion", 50, "50%"),
            ("AdjustFixed", 10, "+10"),
            ("AdjustFixed", -10, "-10"),
            ("AdjustProportion", 5, "+5%"),
        ):
            with self.subTest(variant=variant, amount=amount):
                self.assertEqual(ipc._size_change({variant: amount}), expected)

    def test_an_unknown_enum_argument_is_refused_rather_than_stringified(self):
        # Stringifying it is precisely the bug above; failing loudly is
        # the only other honest option.
        with self.assertRaises(ValueError):
            ipc._size_change({"NoSuchVariant": 1})


class TestCLIFallback(unittest.TestCase):
    def test_sway_cli_parses_json_output(self):
        transport = ipc.SwayCLI(runner=lambda argv: json.dumps({"id": 1}))
        self.assertEqual(transport.get_tree(), {"id": 1})

    def test_stream_closes_stdout_when_the_process_ends(self):
        # Regression: _stream terminated the process without closing its
        # stdout pipe.
        import subprocess
        from unittest import mock

        created = []
        real_popen = subprocess.Popen

        def recording_popen(*a, **kw):
            process = real_popen(*a, **kw)
            created.append(process)
            return process

        with mock.patch("subprocess.Popen", side_effect=recording_popen):
            list(ipc.SwayCLI._stream(["printf", "hello\n"]))

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].stdout.closed)

    def test_hyprland_cli_parses_json_output(self):
        transport = ipc.HyprlandCLI(
            runner=lambda argv: json.dumps([{"address": "0x1"}])
        )
        self.assertEqual(transport.clients(), [{"address": "0x1"}])

    def test_sway_cli_subscribe_skips_malformed_lines(self):
        lines = ['{"change":"new"}\n', "\n", "garbage\n", '{"change":"close"}\n']
        transport = ipc.SwayCLI(streamer=lambda argv, deadline=None: iter(lines))
        changes = [e["change"] for e in transport.subscribe(["window"])]
        self.assertEqual(changes, ["new", "close"])


if __name__ == "__main__":
    unittest.main()
