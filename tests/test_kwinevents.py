"""KWinEventsBackend: the generator/queue logic, without a real KWin.

Constructed with connect=False throughout -- no D-Bus service is hosted
and no KWin script is loaded, leaving self._events as a plain list a test
appends to directly, standing in for what _handle_method_call would have
done on a real Notify call. That is deliberately the whole surface these
tests exercise: window_events()/wait_for_window()'s own logic, not
whether Gio can actually host a service or KWin can actually load a
script -- those are what scripts/validate-kwin-events.sh and
examples/_kwin_events_validate.py check against a real desktop.
"""

import unittest
from unittest import mock

try:
    from pyguitest.backends.kwinevents import KWinEventsBackend, available
except ImportError:
    available = None  # type: ignore[assignment]

from pyguitest.capabilities import Capability
from pyguitest.errors import CapabilityUnsupported


def _skip_without_pygobject(test):
    if available is None or not available():
        raise unittest.SkipTest("PyGObject is not installed")
    return test


class Recorder:
    """Records every argv a fake kdotool runner is asked to run."""

    def __init__(self, outputs=None):
        self.calls = []
        self.outputs = outputs or {}

    def __call__(self, argv):
        self.calls.append(argv)
        return self.outputs.get(tuple(argv), "")


class TestKWinEventsBackend(unittest.TestCase):
    def setUp(self):
        _skip_without_pygobject(self)

    def _backend(self, runner=None):
        return KWinEventsBackend(runner=runner, connect=False)

    def test_name(self):
        self.assertEqual(self._backend().name, "kwinevents")

    def test_capabilities_is_window_events_only(self):
        gui = self._backend()
        self.assertEqual(set(gui.capabilities), {Capability.WINDOW_EVENTS})

    def test_close_does_not_raise_with_nothing_connected(self):
        gui = self._backend()
        gui.close()  # must not raise, even though connect=False touched no D-Bus

    def test_window_events_drains_already_queued_events_in_order(self):
        gui = self._backend()
        gui._events.append(("new", "{uuid-1}", "First"))
        gui._events.append(("title", "{uuid-1}", "First, renamed"))
        gui._events.append(("close", "{uuid-1}", "First, renamed"))

        events = list(gui.window_events(timeout=0))
        self.assertEqual([e.change for e in events], ["new", "title", "close"])
        self.assertEqual(events[0].window.handle, "{uuid-1}")
        self.assertEqual(events[0].window.title, "First")
        self.assertEqual(events[1].window.title, "First, renamed")

    def test_window_events_yielded_window_names_this_backend(self):
        gui = self._backend()
        gui._events.append(("new", "{uuid-1}", "Title"))
        (event,) = list(gui.window_events(timeout=0))
        self.assertIs(event.window.backend, gui)

    def test_window_events_times_out_with_nothing_queued(self):
        gui = self._backend()
        events = list(gui.window_events(timeout=0.05))
        self.assertEqual(events, [])

    def test_window_events_requires_the_capability(self):
        gui = self._backend()
        gui._events.append(("new", "{uuid-1}", "Title"))
        gui.require = mock.Mock(
            side_effect=CapabilityUnsupported(Capability.WINDOW_EVENTS, gui.name)
        )
        with self.assertRaises(CapabilityUnsupported):
            next(gui.window_events(timeout=0))

    def test_wait_for_window_finds_an_existing_window_first(self):
        runner = Recorder(
            outputs={
                ("kdotool", "search", "."): "{uuid-1}\n{uuid-2}\n",
                ("kdotool", "getwindowname", "{uuid-1}"): "Calculator",
                ("kdotool", "getwindowname", "{uuid-2}"): "gedit",
            }
        )
        gui = self._backend(runner=runner)
        window = gui.wait_for_window("gedit", timeout=1)
        self.assertIsNotNone(window)
        self.assertEqual(window.handle, "{uuid-2}")
        self.assertEqual(window.title, "gedit")
        # Never touched window_events()'s wait path -- an existing match
        # returns immediately without needing any queued/incoming event.
        self.assertEqual(gui._events, [])

    def test_wait_for_window_falls_through_to_a_new_event(self):
        runner = Recorder(outputs={("kdotool", "search", "."): ""})
        gui = self._backend(runner=runner)
        gui._events.append(("new", "{uuid-3}", "gedit"))
        window = gui.wait_for_window("gedit", timeout=1)
        self.assertIsNotNone(window)
        self.assertEqual(window.handle, "{uuid-3}")

    def test_wait_for_window_ignores_a_close_event(self):
        runner = Recorder(outputs={("kdotool", "search", "."): ""})
        gui = self._backend(runner=runner)
        gui._events.append(("close", "{uuid-4}", "gedit"))
        window = gui.wait_for_window("gedit", timeout=0.05)
        self.assertIsNone(window)

    def test_wait_for_window_matches_a_title_event_too(self):
        runner = Recorder(outputs={("kdotool", "search", "."): ""})
        gui = self._backend(runner=runner)
        gui._events.append(("title", "{uuid-5}", "Untitled Document - gedit"))
        window = gui.wait_for_window("gedit", timeout=1)
        self.assertIsNotNone(window)
        self.assertEqual(window.handle, "{uuid-5}")

    def test_wait_for_window_times_out_with_no_match(self):
        runner = Recorder(outputs={("kdotool", "search", "."): ""})
        gui = self._backend(runner=runner)
        self.assertIsNone(gui.wait_for_window("nothing-matches-this", timeout=0.05))

    def test_existing_windows_pairs_handles_with_names(self):
        runner = Recorder(
            outputs={
                ("kdotool", "search", "."): "{uuid-1}\n{uuid-2}\n",
                ("kdotool", "getwindowname", "{uuid-1}"): "Calculator",
                ("kdotool", "getwindowname", "{uuid-2}"): "gedit",
            }
        )
        gui = self._backend(runner=runner)
        self.assertEqual(
            gui._existing_windows(),
            [("{uuid-1}", "Calculator"), ("{uuid-2}", "gedit")],
        )

    def test_existing_windows_skips_blank_lines(self):
        runner = Recorder(outputs={("kdotool", "search", "."): "\n{uuid-1}\n\n"})
        runner.outputs[("kdotool", "getwindowname", "{uuid-1}")] = "gedit"
        gui = self._backend(runner=runner)
        self.assertEqual(gui._existing_windows(), [("{uuid-1}", "gedit")])


if __name__ == "__main__":
    unittest.main()
