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

    def test_a_failing_script_load_releases_what_hosting_already_claimed(self):
        # The one construction ordering that leaks: _host_service() has
        # already owned the bus name and exported the object when the script
        # load fails, which the factory's own docstring names as a real
        # reason construction raises. No caller is ever handed the object, so
        # nothing can call close() -- the name stayed owned for the life of
        # the process, and because it was claimed without DO_NOT_QUEUE a
        # later connect() queued behind it and then blamed "another
        # instance", which was this same process.
        from pyguitest.errors import BackendUnavailable

        claimed = {}

        def fake_host(backend):
            backend._Gio = mock.Mock()
            backend._own_name_id = 7
            backend._registration_id = 3
            backend._connection = mock.Mock()
            claimed["gio"] = backend._Gio
            claimed["connection"] = backend._connection

        def failing_script(backend):
            raise BackendUnavailable("KWin would not load the script")

        with (
            mock.patch.object(KWinEventsBackend, "_host_service", fake_host),
            mock.patch.object(
                KWinEventsBackend, "_load_and_run_script", failing_script
            ),
            self.assertRaises(BackendUnavailable),
        ):
            KWinEventsBackend()
        claimed["gio"].bus_unown_name.assert_called_once_with(7)
        claimed["connection"].unregister_object.assert_called_once_with(3)

    def _ownership_harness(self, gui, verdicts, pushed=True):
        """Drive the real `_host_service` with a faked `bus_own_name`.

        Gio's real ordering, one callback per main-context iteration:
        `bus_acquired` first, as soon as the connection is up, and only
        afterwards the outcome of `RequestName` -- `name_acquired`, or
        `name_lost` for a name another connection holds. `verdicts` is what
        follows `bus_acquired`, so an empty list models an answer that has
        not come yet.

        Two things about the context are modelled rather than mocked away,
        because the wait's correctness is entirely about them:

        - `iteration(True)` **blocks**. With no callback left to dispatch
          and no source attached that could wake it, the fake raises
          instead of returning, since a real one would sit there for as
          long as the bus stayed quiet -- past any deadline a clock read
          between iterations could enforce. A timeout source attached to
          this same context is what the wait is bounded by, and the fake
          fires it exactly where GLib would: once nothing else is ready.
        - Gio delivers the callbacks on the **thread-default** context.
          `pushed` puts the working context there and leaves a different
          one as the global default, which raises if it is iterated -- so
          polling the wrong one fails the test rather than quietly timing
          out. `pushed=False` is the ordinary case, no context pushed at
          all, where `get_thread_default` answers None.

        Returns the list of timeout sources `_host_service` created, so a
        test can read the deadline it asked GLib for.
        """
        pending = []
        attached = []
        sources = []

        def bus_own_name(_bus, _name, _flags, on_bus, on_name, on_lost):
            connection = mock.Mock()
            connection.register_object.return_value = 3
            pending.append(lambda: on_bus(connection, _name))
            for verdict in verdicts:
                handler = on_name if verdict else on_lost
                pending.append(lambda handler=handler: handler(connection, _name))
            return 7

        class Context:
            """GLib's main context, as the ownership wait uses it."""

            def __init__(self, iterable=True):
                self.iterable = iterable
                self.iterations = 0

            def iteration(self, _may_block):
                assert self.iterable, (
                    "iterated a context Gio delivered nothing on: the "
                    "callbacks arrive on the thread-default one"
                )
                self.iterations += 1
                if pending:
                    pending.pop(0)()
                    return True
                if attached:
                    # Nothing else is ready, which is where a real
                    # iteration would block -- and where the attached
                    # deadline is the source that becomes ready.
                    attached[0]()
                    return True
                raise AssertionError(
                    "a blocking iteration with nothing attached that can wake "
                    "it: the wait would outlast its own deadline"
                )

        class Source:
            """A GLib timeout source, from `set_callback` to `destroy`."""

            def __init__(self, interval):
                self.interval = interval
                self.callback = None
                self.context = None
                self.destroyed = False

            def set_callback(self, callback):
                self.callback = callback

            def attach(self, context):
                self.context = context
                attached.append(self.callback)

            def destroy(self):
                self.destroyed = True
                if self.callback in attached:
                    attached.remove(self.callback)

        def timeout_source_new(interval):
            sources.append(Source(interval))
            return sources[-1]

        working = Context()
        other = Context(iterable=False)
        gui._Gio = mock.Mock()
        gui._Gio.DBusNodeInfo.new_for_xml.return_value.interfaces = [mock.Mock()]
        gui._Gio.bus_own_name.side_effect = bus_own_name
        gui._GLib = mock.Mock()
        gui._GLib.timeout_source_new.side_effect = timeout_source_new
        gui._GLib.MainContext.get_thread_default.return_value = (
            working if pushed else None
        )
        gui._GLib.MainContext.default.return_value = other if pushed else working
        return sources

    def test_hosting_waits_for_the_name_not_the_bus(self):
        # `bus_acquired` fires before `RequestName` is even sent, so it says
        # nothing about ownership; a name another session already holds is
        # answered `name_lost` an iteration later. Returning on the first
        # callback let a second process come up believing it owned the name
        # -- and sweep the live session's KWin script out from under it.
        from pyguitest.errors import BackendUnavailable

        gui = self._backend()
        self._ownership_harness(gui, verdicts=[False])
        with self.assertRaises(BackendUnavailable):
            gui._host_service()
        # The object was still exported on the connection first in hand;
        # close() is what releases it, as the constructor's cleanup does.
        self.assertEqual(gui._registration_id, 3)

    def test_hosting_returns_once_the_name_is_acquired(self):
        gui = self._backend()
        self._ownership_harness(gui, verdicts=[True])
        gui._host_service()
        self.assertEqual(gui._own_name_id, 7)
        self.assertEqual(gui._registration_id, 3)

    def test_hosting_does_not_take_a_pending_answer_for_ownership(self):
        # No verdict at all within the wait: not owned, not "probably ours".
        # The wait ends on the deadline source GLib fires, not on a clock
        # read between iterations -- the harness refuses an iteration that
        # nothing could wake, which is what the real one would block on.
        from pyguitest.backends import kwinevents
        from pyguitest.errors import BackendUnavailable

        gui = self._backend()
        sources = self._ownership_harness(gui, verdicts=[])
        with self.assertRaises(BackendUnavailable):
            gui._host_service()
        (deadline,) = sources
        self.assertEqual(deadline.interval, int(kwinevents._OWN_NAME_TIMEOUT * 1000))
        self.assertIs(
            deadline.context,
            gui._GLib.MainContext.get_thread_default.return_value,
            "the deadline has to wake the context the callbacks arrive on",
        )

    def test_hosting_waits_on_the_default_context_with_none_pushed(self):
        # The ordinary case: nothing has pushed a thread-default context,
        # `get_thread_default` answers None, and Gio delivers on the global
        # default one -- which is then what the wait and its deadline use.
        gui = self._backend()
        sources = self._ownership_harness(gui, verdicts=[True], pushed=False)
        gui._host_service()
        self.assertEqual(gui._own_name_id, 7)
        self.assertIs(sources[0].context, gui._GLib.MainContext.default.return_value)
        self.assertTrue(
            sources[0].destroyed, "the deadline outlived the wait it was bounding"
        )

    def _script_harness(self, gui, fail_on, copies=0, unloadable=True):
        """Drive the real `_load_and_run_script` with faked D-Bus.

        The test above replaces the whole helper, so it cannot see what
        happens *inside* it -- which is where the script gets loaded. This
        one lets `loadScript` succeed and fails a later step, which is the
        ordering that used to leave a script resident in KWin.

        `copies` is how many copies of the script KWin already holds under
        the plugin name -- what killed sessions leave behind. KWin's own
        `unloadScript` removes *one* of them per call and `isScriptLoaded`
        answers whether any is left, which is what the sweep is written
        against. `unloadable=False` is a KWin whose removals do not take,
        the case the sweep has to refuse rather than load on top of.
        """
        methods = []
        kwin = mock.Mock()
        resident = [copies]

        def call_sync(method, *args, **kwargs):
            methods.append(method)
            # The refusal is checked first, so `fail_on="loadScript"` really
            # fails the load rather than being answered by the branch below.
            if method == fail_on:
                raise RuntimeError(f"KWin refused {method}")
            if method == "loadScript":
                variant = mock.Mock()
                variant.unpack.return_value = (42,)
                return variant
            if method == "isScriptLoaded":
                return self._variant_answer(bool(resident[0]))
            if method == "unloadScript":
                removed = bool(resident[0]) and unloadable
                if removed:
                    resident[0] -= 1
                return self._variant_answer(removed)
            return mock.Mock()

        kwin.call_sync.side_effect = call_sync
        script = mock.Mock()
        script.call_sync.side_effect = call_sync

        gio = mock.Mock()
        if fail_on == "__proxy__":
            gio.DBusProxy.new_for_bus_sync.side_effect = [
                kwin,
                RuntimeError("no object at that path"),
            ]
        else:
            gio.DBusProxy.new_for_bus_sync.side_effect = [kwin, script]
        gui._Gio = gio
        gui._GLib = mock.Mock()
        return methods

    @staticmethod
    def _variant_answer(value):
        """A `GLib.Variant` reply, as far as this backend unpacks one."""
        variant = mock.Mock()
        variant.unpack.return_value = (value,)
        return variant

    @staticmethod
    def _after_the_load(methods):
        """The calls made once `loadScript` had been issued.

        The sweep before the load is an `unloadScript` too, so "an unload
        happened" is no longer evidence of a rollback; what the rollback
        tests need is an unload *after* the load.
        """
        return methods[methods.index("loadScript") + 1 :]

    def test_a_script_whose_proxy_cannot_be_built_is_unloaded_again(self):
        from pyguitest.errors import BackendUnavailable

        gui = self._backend()
        methods = self._script_harness(gui, fail_on="__proxy__")
        with self.assertRaises(BackendUnavailable):
            gui._load_and_run_script()
        self.assertIn(
            "unloadScript",
            self._after_the_load(methods),
            "the script was left loaded in KWin with nothing holding a handle",
        )

    def test_a_script_that_loads_but_will_not_run_is_unloaded_again(self):
        from pyguitest.errors import BackendUnavailable

        gui = self._backend()
        methods = self._script_harness(gui, fail_on="run")
        with self.assertRaises(BackendUnavailable):
            gui._load_and_run_script()
        self.assertIn("unloadScript", self._after_the_load(methods))

    def test_a_load_that_itself_fails_is_unloaded_too(self):
        """A load that raised here may still have taken in KWin.

        KWin registers the script before it replies with the id
        (`scripts.append(script); return id;`), so a reply that timed out
        or a connection that dropped mid-call leaves the script resident
        with this side believing nothing was loaded. This used to assert
        the opposite -- "nothing was loaded, so an unload would be a second
        false step" -- which is wrong on both counts: it may have been, and
        `unloadScript` on a plugin name nothing is loaded under returns
        False and does nothing. So the rollback runs unconditionally.
        """
        from pyguitest.errors import BackendUnavailable

        gui = self._backend()
        methods = self._script_harness(gui, fail_on="loadScript")
        with self.assertRaises(BackendUnavailable):
            gui._load_and_run_script()
        self.assertIn("unloadScript", self._after_the_load(methods))

    def test_a_predecessors_copy_is_swept_before_loading(self):
        """A session killed outright never unloads its script; the next one does.

        The closer runs only on `close()`, so a `SIGKILL`ed session leaves
        its copy in KWin, and KWin's own duplicate check does not catch the
        reload -- it compares the plugin name *as passed* (null, through the
        one-argument `loadScript`), which no registered script carries. Two
        copies then call `Notify` for every window event. The unload by
        plugin name before the load clears a predecessor's copy; the bus
        name owned before this runs is what guarantees the copy it clears
        is not a live session's.

        With nothing resident the sweep costs one `isScriptLoaded` and no
        unload at all, which is the ordinary startup.
        """
        gui = self._backend()
        methods = self._script_harness(gui, fail_on=None, copies=1)
        gui._load_and_run_script()
        self.assertEqual(
            methods,
            ["isScriptLoaded", "unloadScript", "isScriptLoaded", "loadScript", "run"],
        )

    def test_a_clean_kwin_is_asked_once_and_not_unloaded(self):
        gui = self._backend()
        methods = self._script_harness(gui, fail_on=None)
        gui._load_and_run_script()
        self.assertEqual(methods, ["isScriptLoaded", "loadScript", "run"])

    def test_every_resident_copy_is_swept_not_only_the_first(self):
        """Two killed sessions leave two copies; one unload removes one.

        KWin's `unloadScript` takes the first script matching the name and
        deletes that one (`scripting.cpp`, `findScript` then a single
        `deleteLater`), so a single sweep left a survivor -- and the new
        script went on top of it, which is the duplicate-event bug the
        sweep was added to prevent, reintroduced for anyone whose session
        had died twice.
        """
        gui = self._backend()
        methods = self._script_harness(gui, fail_on=None, copies=2)
        gui._load_and_run_script()
        self.assertEqual(methods.count("unloadScript"), 2)
        self.assertEqual(methods[-2:], ["loadScript", "run"])

    def test_a_copy_that_will_not_unload_refuses_rather_than_adding_another(self):
        """The sweep's own failure is not a reason to load anyway.

        A KWin whose removals do not take leaves a producer this session
        cannot silence, and loading on top of it would double every event
        -- silently, since both copies answer on the same bus name. The
        refusal names what is in the way instead.
        """
        from pyguitest.backends import kwinevents
        from pyguitest.errors import BackendUnavailable

        gui = self._backend()
        methods = self._script_harness(gui, fail_on=None, copies=1, unloadable=False)
        with (
            mock.patch.object(kwinevents, "_SWEEP_TIMEOUT", 0.05),
            mock.patch.object(kwinevents, "_SWEEP_POLL", 0.01),
            self.assertRaises(BackendUnavailable) as refusal,
        ):
            gui._load_and_run_script()
        self.assertIn("still loaded", str(refusal.exception))
        self.assertNotIn("loadScript", methods)

    def test_a_kwin_that_will_not_say_gets_the_single_best_effort_unload(self):
        """`isScriptLoaded` missing is not a reason to refuse a session.

        An older KWin, or one that answers the call with an error: the
        sweep cannot be checked, so it falls back to exactly what it did
        before the loop existed -- one unload, then the load.
        """
        gui = self._backend()
        methods = self._script_harness(gui, fail_on="isScriptLoaded")
        gui._load_and_run_script()
        self.assertEqual(
            methods, ["isScriptLoaded", "unloadScript", "loadScript", "run"]
        )

    def test_closing_unloads_the_script_it_ran(self):
        gui = self._backend()
        methods = self._script_harness(gui, fail_on=None)
        gui._load_and_run_script().close()
        self.assertEqual(methods[-2:], ["run", "unloadScript"])

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

    def test_wait_for_window_takes_the_last_of_several_same_titled_windows(self):
        # Session.find_window is find_windows(...)[-1] over the window list,
        # and on KDE that list comes from KdotoolBackend reading the same
        # `kdotool search .` in the same order. Taking the first match here
        # made the two calls name different windows for one title -- the
        # collision find_window itself was fixed for.
        runner = Recorder(
            outputs={
                ("kdotool", "search", "."): "{uuid-1}\n{uuid-2}\n{uuid-3}\n",
                ("kdotool", "getwindowname", "{uuid-1}"): "Untitled Document",
                ("kdotool", "getwindowname", "{uuid-2}"): "Calculator",
                ("kdotool", "getwindowname", "{uuid-3}"): "Untitled Document",
            }
        )
        gui = self._backend(runner=runner)
        window = gui.wait_for_window("Untitled Document", timeout=1)
        self.assertIsNotNone(window)
        self.assertEqual(window.handle, "{uuid-3}")

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
