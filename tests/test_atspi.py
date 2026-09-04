"""AT-SPI adapter tests against a stand-in dogtail.

dogtail needs a live accessibility bus, so these fake the module to check the
adapter's own logic: capability gating, the Wayland coordinate refusal, and
frame filtering.
"""

import re
import subprocess
import sys
import types
import unittest
from unittest import mock

from pyguitest.capabilities import Capability
from pyguitest.errors import BackendUnavailable, CapabilityUnsupported
from pyguitest.session import SessionType, detect


class FakeState:
    """Stands in for a pyatspi StateSet."""

    def __init__(self, states=()):
        self._states = set(states)

    def contains(self, state):
        return state in self._states


class FakeNode:
    def __init__(
        self,
        name="",
        role="filler",
        children=(),
        position=(0, 0),
        size=(0, 0),
        active=False,
        sensitive=True,
        description="",
    ):
        self.name = name
        self.roleName = role
        self.children = list(children)
        self.position = position
        self.size = size
        self.parent = None
        self.showing = True
        self.clicked = False
        self.focused = False
        self._active = active
        self.sensitive = sensitive
        self.description = description
        for child in self.children:
            child.parent = self

    def click(self):
        self.clicked = True

    def grabFocus(self):
        self.focused = True

    def getState(self):
        return FakeState({"STATE_ACTIVE"} if self._active else set())

    def findChildren(self, pred):
        # Real dogtail accepts a GenericPredicate (has .matches) or a plain
        # node -> bool function -- see AccessibleObject.find_all_descendants.
        # find_elements now builds the latter, so this fake needs to accept
        # both, exactly like the real thing.
        test = pred.matches if hasattr(pred, "matches") else pred
        out = []
        for child in self.children:
            if test(child):
                out.append(child)
            out.extend(child.findChildren(pred))
        return out

    def applications(self):
        return self.children


class _RaisingSize:
    """Stands in for a node whose Component.size raises, like a dead ponytail."""

    def __init__(self, position, error):
        self.position = position
        self._error = error

    @property
    def size(self):
        raise self._error


class FakePredicate:
    def __init__(self, roleName=None, name=None):
        self.roleName = roleName
        self.name = name

    def matches(self, node):
        return self.roleName in (None, node.roleName) and self.name in (None, node.name)


def build_tree():
    button = FakeNode("OK", "push button", position=(10, 20), size=(80, 30))
    # Additional children of `frame`, not of `app` -- windows() only counts
    # an application's direct children, so these cannot change window counts
    # or geometry in tests that only look at gui.windows().
    cancel = FakeNode("Cancel", "push button", sensitive=False)
    notifications = FakeNode(
        "Enable notifications",
        "check box",
        description="Turn notifications on or off",
    )
    frame = FakeNode(
        "Document - Editor",
        "frame",
        [button, cancel, notifications],
        (0, 0),
        (800, 600),
    )
    palette = FakeNode("Tools", "tool bar", [])
    app = FakeNode("gedit", "application", [frame, palette])
    return FakeNode("desktop", "desktop frame", [app]), frame, button


def install_fake_dogtail():
    root, frame, button = build_tree()
    dogtail = types.ModuleType("dogtail")
    tree = types.ModuleType("dogtail.tree")
    predicate = types.ModuleType("dogtail.predicate")
    tree.root = root
    predicate.GenericPredicate = FakePredicate
    dogtail.tree = tree
    dogtail.predicate = predicate
    modules = {
        "dogtail": dogtail,
        "dogtail.tree": tree,
        "dogtail.predicate": predicate,
    }
    return mock.patch.dict(sys.modules, modules), frame, button


class AtspiTestCase(unittest.TestCase):
    def setUp(self):
        patcher, self.frame, self.button = install_fake_dogtail()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import atspi

        self.atspi = atspi
        # The fake dogtail needs no accessibility bus, but _dogtail() now
        # probes for one before importing anything (see
        # a11y_bus_reachable). Left unpatched, every test here would go on
        # to ask the real session bus and answer False on any machine
        # without a desktop -- CI included.
        self.set_a11y_bus(True)

    def set_a11y_bus(self, reachable):
        """Pin the a11y-bus answer for one test, spawning nothing."""
        patcher = mock.patch.object(
            self.atspi, "a11y_bus_reachable", return_value=reachable
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def backend(self, session_type=SessionType.X11):
        env = detect({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"})
        import dataclasses

        return self.atspi.AtspiBackend(
            dataclasses.replace(env, session_type=session_type)
        )


class TestAvailability(AtspiTestCase):
    def test_available_when_dogtail_imports(self):
        self.assertTrue(self.atspi.available())

    def test_unreachable_accessibility_bus_declines_before_importing(self):
        # The import is what has to be avoided, not merely reported:
        # dogtail.tree builds its root from pyatspi at import time and
        # libatspi answers an unreachable bus with g_error(), which aborts
        # the process. Seen for real inside scripts/headless-session.sh.
        self.set_a11y_bus(False)
        self.assertFalse(self.atspi.available())

    def test_the_refusal_names_the_bus_rather_than_dogtail(self):
        self.set_a11y_bus(False)
        with self.assertRaises(BackendUnavailable) as caught:
            self.atspi.AtspiBackend()
        message = str(caught.exception)
        self.assertIn("org.a11y.Bus", message)
        self.assertNotIn("pip install", message)


class TestTheAccessibilityBusProbe(unittest.TestCase):
    """The probe itself: a subprocess, and what each way it can fail means.

    Two functions, on purpose. `a11y_bus_probe` keeps the third answer --
    "could not ask" -- for `pyguitest debug` to print, and
    `a11y_bus_reachable` folds it into yes for the decision, because
    refusing AT-SPI on a box whose bus may be fine is the worse error.
    """

    def setUp(self):
        from pyguitest.backends import atspi

        self.atspi = atspi
        previous = atspi._A11Y_BUS_ANSWERED
        atspi._A11Y_BUS_ANSWERED = False
        self.addCleanup(setattr, atspi, "_A11Y_BUS_ANSWERED", previous)

    def _run(self, **kwargs):
        with (
            mock.patch.object(
                self.atspi.shutil, "which", return_value="/usr/bin/gdbus"
            ),
            mock.patch.object(self.atspi.subprocess, "run", **kwargs) as run,
        ):
            return self.atspi.a11y_bus_probe(), run

    def test_a_successful_call_means_reachable(self):
        answer, run = self._run(
            return_value=subprocess.CompletedProcess([], 0, b"", b"")
        )
        self.assertIs(answer, True)
        self.assertIn("org.a11y.Bus.GetAddress", run.call_args[0][0])

    def test_a_failed_call_means_unreachable(self):
        answer, _ = self._run(
            return_value=subprocess.CompletedProcess([], 1, b"", b"error")
        )
        self.assertIs(answer, False)

    def test_a_timeout_answers_no(self):
        # The two ways of being wrong are not symmetric: a wrong "no" skips
        # a backend, a wrong "yes" core-dumps the caller's process.
        answer, _ = self._run(
            side_effect=subprocess.TimeoutExpired(cmd="gdbus", timeout=5)
        )
        self.assertIs(answer, False)

    def test_gdbus_that_cannot_be_spawned_was_never_asked(self):
        answer, _ = self._run(side_effect=OSError("boom"))
        self.assertIsNone(answer)

    def test_no_gdbus_at_all_answers_none_without_spawning(self):
        with mock.patch.object(self.atspi.shutil, "which", return_value=None):
            self.assertIsNone(self.atspi.a11y_bus_probe())

    def test_an_unasked_question_still_lets_the_backend_be_tried(self):
        # Restores the behaviour this probe replaced rather than disabling
        # AT-SPI on a box whose bus may be perfectly fine.
        with mock.patch.object(self.atspi, "a11y_bus_probe", return_value=None):
            self.assertTrue(self.atspi.a11y_bus_reachable())

    def test_only_a_definite_no_declines(self):
        for answer, expected in ((True, True), (None, True), (False, False)):
            with self.subTest(probe=answer):
                with mock.patch.object(
                    self.atspi, "a11y_bus_probe", return_value=answer
                ):
                    self.assertIs(self.atspi.a11y_bus_reachable(), expected)

    def test_a_no_is_not_memoized(self):
        # Asymmetric on purpose: a process that starts before its desktop
        # is ready would otherwise have AT-SPI permanently unavailable for
        # a reason nothing reports, and the failing probe is immediate.
        with (
            mock.patch.object(
                self.atspi.shutil, "which", return_value="/usr/bin/gdbus"
            ),
            mock.patch.object(self.atspi.subprocess, "run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 1, b"", b"")
            self.assertIs(self.atspi.a11y_bus_probe(), False)
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            self.assertIs(self.atspi.a11y_bus_probe(), True)
            self.assertEqual(run.call_count, 2)

    def test_the_answer_is_memoized(self):
        answer, run = self._run(
            return_value=subprocess.CompletedProcess([], 0, b"", b"")
        )
        self.assertIs(answer, True)
        self.assertEqual(run.call_count, 1)
        # A composite asks available() once per member build; a probe that
        # spawned a process each time would be paid for repeatedly.
        self.assertIs(self.atspi.a11y_bus_probe(), True)


class TestWaylandCoordinateHonesty(AtspiTestCase):
    def test_geometry_declared_under_x11(self):
        gui = self.backend(SessionType.X11)
        self.assertIn(Capability.WINDOW_GEOMETRY, gui.capabilities)

    def test_geometry_withheld_under_pure_wayland(self):
        # A Wayland client is never told its position on screen, so the extents
        # it reports through AT-SPI cannot be trusted as screen coordinates.
        gui = self.backend(SessionType.WAYLAND)
        self.assertNotIn(Capability.WINDOW_GEOMETRY, gui.capabilities)

    def test_geometry_call_explains_the_refusal(self):
        gui = self.backend(SessionType.WAYLAND)
        with self.assertRaises(CapabilityUnsupported) as ctx:
            gui.geometry(gui.windows()[0])
        self.assertIn("where it is on screen", str(ctx.exception))

    def test_geometry_returns_extents_under_x11(self):
        gui = self.backend(SessionType.X11)
        self.assertEqual(gui.geometry(gui.windows()[0]), (0, 0, 800, 600))

    def test_missing_ponytail_daemon_raises_typed_not_a_bare_runtime_error(self):
        # Live regression: dogtail's Component.get_size/get_position route
        # through its own gnome-ponytail-daemon on GNOME, even under X11/
        # XWayland where pyguitest already trusts the coordinates -- the
        # daemon being absent is a missing system dependency, not a Wayland
        # honesty problem, and must not leak dogtail's bare RuntimeError.
        gui = self.backend(SessionType.X11)
        window = gui.windows()[0]
        window.handle = _RaisingSize(
            self.frame.position,
            RuntimeError(
                "Error in ponytail initiation might be caused by several reasons"
            ),
        )
        with self.assertRaises(CapabilityUnsupported) as ctx:
            gui.geometry(window)
        self.assertIn("gnome-ponytail-daemon", str(ctx.exception))

    def test_a_different_ponytail_failure_mode_is_also_wrapped(self):
        # Second live regression, on the same machine: once the daemon was
        # installed, get_size() failed a completely different way --
        # dbus.exceptions.DBusException("GetWindows is not allowed"), not a
        # RuntimeError at all. There is no closed list of ponytail's failure
        # types to match against, so geometry() must wrap *any* exception
        # from this pair of calls, not just RuntimeError -- while still
        # keeping the original message visible for diagnosis.
        class DBusLikeError(Exception):
            pass

        gui = self.backend(SessionType.X11)
        window = gui.windows()[0]
        window.handle = _RaisingSize(
            self.frame.position,
            DBusLikeError(
                "org.freedesktop.DBus.Error.AccessDenied: GetWindows is not allowed"
            ),
        )
        with self.assertRaises(CapabilityUnsupported) as ctx:
            gui.geometry(window)
        self.assertIn("GetWindows is not allowed", str(ctx.exception))


class TestWindows(AtspiTestCase):
    def test_only_frames_count_as_windows(self):
        # The application also owns a tool bar; it is not a window.
        gui = self.backend()
        windows = gui.windows()
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].title, "Document - Editor")
        self.assertEqual(windows[0].app_id, "gedit")

    def test_window_list_works_where_no_wayland_protocol_does(self):
        # The practical reason this backend leads on GNOME: the accessibility
        # bus knows the frames even though Mutter exposes no foreign-toplevel.
        gui = self.backend(SessionType.WAYLAND)
        self.assertIn(Capability.WINDOW_LIST, gui.capabilities)
        self.assertEqual(len(gui.windows()), 1)

    def test_activate_grabs_focus(self):
        gui = self.backend()
        gui.activate_window(gui.windows()[0])
        self.assertTrue(self.frame.focused)

    def test_is_window_viewable_reads_the_showing_state(self):
        gui = self.backend()
        window = gui.windows()[0]
        self.assertTrue(gui.is_window_viewable(window))
        self.frame.showing = False
        self.assertFalse(gui.is_window_viewable(window))


class TestActiveWindow(AtspiTestCase):
    """A missing pyatspi must raise typed, not crash active_window().

    It is imported bare inside _state_active, not through the guarded
    _dogtail() pattern, and is declared in no dependency list -- only
    dogtail is pulled in by the atspi extra, with pyatspi meant to come
    from the distro. A box with dogtail but not pyatspi used to get a bare
    ImportError from active_window() on a backend that otherwise looked
    fully constructed.
    """

    def test_missing_pyatspi_raises_a_typed_error_not_a_bare_import_error(self):
        # pyatspi is genuinely not installed in this environment, so this
        # exercises the real failure path rather than a simulated one.
        with mock.patch.dict(sys.modules, {"pyatspi": None}):
            gui = self.backend()
            with self.assertRaises(CapabilityUnsupported) as ctx:
                gui.active_window()
            self.assertIn("pyatspi", str(ctx.exception))

    def test_active_window_is_found_once_pyatspi_is_available(self):
        fake_pyatspi = types.ModuleType("pyatspi")
        fake_pyatspi.STATE_ACTIVE = "STATE_ACTIVE"
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi}):
            gui = self.backend()
            self.frame._active = True
            self.assertEqual(gui.active_window().title, "Document - Editor")

    def test_returns_none_when_no_frame_is_active(self):
        fake_pyatspi = types.ModuleType("pyatspi")
        fake_pyatspi.STATE_ACTIVE = "STATE_ACTIVE"
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi}):
            gui = self.backend()
            self.assertIsNone(gui.active_window())


class TestElements(AtspiTestCase):
    def test_find_elements_by_role(self):
        gui = self.backend()
        found = gui.find_elements(role="push button")
        self.assertEqual([e.name for e in found], ["OK", "Cancel"])

    def test_find_element_returns_none_when_absent(self):
        gui = self.backend()
        self.assertIsNone(gui.find_element(role="slider"))

    def test_click_needs_no_coordinates_or_injection(self):
        gui = self.backend()
        gui.find_element(name="OK").click()
        self.assertTrue(self.button.clicked)

    def test_focused_reads_the_node_s_focus_state(self):
        gui = self.backend()
        button = gui.find_element(name="OK")
        self.assertFalse(button.focused)
        button.focus()
        self.assertTrue(button.focused)

    def test_element_exposes_role_name_and_ancestry(self):
        gui = self.backend()
        button = gui.find_element(name="OK")
        self.assertEqual(button.role, "push button")
        self.assertEqual(button.parent.name, "Document - Editor")
        self.assertTrue(button.parent.is_ancestor_of(button))
        self.assertFalse(button.is_ancestor_of(button.parent))

    def test_find_elements_filters_by_enabled(self):
        gui = self.backend()
        found = gui.find_elements(role="push button", enabled=False)
        self.assertEqual([e.name for e in found], ["Cancel"])

    def test_find_elements_filters_by_visible(self):
        gui = self.backend()
        self.frame.children[0].showing = False  # the "OK" button
        found = gui.find_elements(role="push button", visible=False)
        self.assertEqual([e.name for e in found], ["OK"])

    def test_find_elements_name_accepts_a_compiled_regex(self):
        gui = self.backend()
        found = gui.find_elements(name=re.compile("^Ca"))
        self.assertEqual([e.name for e in found], ["Cancel"])

    def test_find_elements_filters_by_description_exact_and_regex(self):
        gui = self.backend()
        exact = gui.find_elements(description="Turn notifications on or off")
        self.assertEqual([e.name for e in exact], ["Enable notifications"])
        pattern = gui.find_elements(description=re.compile("^Turn"))
        self.assertEqual([e.name for e in pattern], ["Enable notifications"])

    def test_find_elements_predicate_receives_a_real_element(self):
        gui = self.backend()
        found = gui.find_elements(role="push button", predicate=lambda e: not e.enabled)
        self.assertEqual([e.name for e in found], ["Cancel"])

    def test_find_elements_combines_filters(self):
        gui = self.backend()
        found = gui.find_elements(role="push button", name="OK", enabled=False)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
