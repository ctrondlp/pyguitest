"""AT-SPI adapter tests against a stand-in dogtail.

dogtail needs a live accessibility bus, so these fake the module to check the
adapter's own logic: capability gating, the Wayland coordinate refusal, and
frame filtering.
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import pyguitest
from pyguitest.capabilities import Capability
from pyguitest.errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    ElementNotActionable,
    PyGUITestError,
)
from pyguitest.roles import Role, spellings
from pyguitest.session import SessionType, detect


class FakeState:
    """Stands in for a pyatspi StateSet."""

    def __init__(self, states=()):
        self._states = set(states)

    def contains(self, state):
        return state in self._states


class FakeRect:
    """Stands in for the BoundingBox pyatspi's getExtents returns."""

    def __init__(self, x, y, width, height):
        self.x = x
        self.y = y
        self.width = width
        self.height = height


class FakeComponent:
    """Stands in for pyatspi's Component interface on one node.

    getAccessibleAtPoint answers about the node's own children and returns
    None for a point outside the node, which is what the real one does and
    what lets element_at walk frames until one claims the point.
    """

    def __init__(self, node):
        self.node = node

    def getExtents(self, coord_type):
        x, y = self.node.position
        width, height = self.node.size
        return FakeRect(x, y, width, height)

    def getAccessibleAtPoint(self, x, y, coord_type):
        if not self.node.covers(x, y):
            return None
        for child in self.node.children:
            if child.covers(x, y):
                return child
        return None


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
        pid=0,
        component=True,
        dead=False,
        actions=None,
        click_raises=None,
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
        self._pid = pid
        self._component = component
        self.dead = dead
        self.actions = actions or {}
        self.actions_performed = []
        self._click_raises = click_raises
        for child in self.children:
            child.parent = self

    def covers(self, x, y):
        left, top = self.position
        width, height = self.size
        return left <= x < left + width and top <= y < top + height

    def queryComponent(self):
        if not self._component:
            raise NotImplementedError("no Component interface")
        return FakeComponent(self)

    def get_process_id(self):
        return self._pid

    def click(self):
        if self._click_raises is not None:
            raise self._click_raises
        self.clicked = True

    def doActionNamed(self, name):
        self.actions_performed.append(name)

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


class _DeadApplication:
    """An application that has exited: reading its children raises."""

    @property
    def children(self):
        raise RuntimeError("the application is gone")


class _RaisingSize:
    """Stands in for a node whose Component.size raises, like a dead ponytail."""

    def __init__(self, position, error):
        self.position = position
        self._error = error

    @property
    def size(self):
        raise self._error


class _RaisesOnDead:
    """A node fully reaped from the bus: even asking if it is dead fails."""

    @property
    def dead(self):
        raise RuntimeError("no accessibility bus for this node")


class FakePredicate:
    def __init__(self, roleName=None, name=None):
        self.roleName = roleName
        self.name = name

    def matches(self, node):
        return self.roleName in (None, node.roleName) and self.name in (None, node.name)


def fake_pyatspi():
    """The pieces of pyatspi this backend reads, as a stand-in module.

    pyatspi is not installed in this environment and is declared in no
    dependency list -- it is meant to come from the distro -- so every test
    that reaches a call needing it supplies this instead.
    """
    module = types.ModuleType("pyatspi")
    module.STATE_ACTIVE = "STATE_ACTIVE"
    module.DESKTOP_COORDS = 0
    module.WINDOW_COORDS = 1
    return module


def build_tree():
    button = FakeNode("OK", "push button", position=(10, 20), size=(80, 30), pid=4242)
    # Additional children of `frame`, not of `app` -- windows() only counts
    # an application's direct children, so these cannot change window counts
    # or geometry in tests that only look at gui.windows().
    cancel = FakeNode(
        "Cancel", "push button", sensitive=False, position=(110, 20), size=(80, 30)
    )
    notifications = FakeNode(
        "Enable notifications",
        "check box",
        description="Turn notifications on or off",
        position=(10, 70),
        size=(200, 24),
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


_ACCESSIBILITY_ENV = (
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "AT_SPI_DISPLAY",
    "AT_SPI_BUS_ADDRESS",
)
"""Everything the accessibility-bus probe reads out of the environment."""


def _environment(**values):
    """`os.environ` with the probe's own names replaced wholesale.

    A test that says which session it is testing cannot drift when the
    machine running it is a different one -- and the two sessions ask
    different questions of different tools: `WAYLAND_DISPLAY` being set is
    the whole of the difference between asking the session bus and reading
    the X11 root window.
    """
    scoped = {
        name: value
        for name, value in os.environ.items()
        if name not in _ACCESSIBILITY_ENV
    }
    scoped.update(values)
    return mock.patch.dict(os.environ, scoped, clear=True)


needs_af_unix = unittest.skipUnless(
    hasattr(socket, "AF_UNIX"), "no AF_UNIX on this platform"
)
"""For the few tests whose subject really is the unix socket layer.

Everything else states reachability with `_connectability` instead of staging
it, so the probe's *logic* is testable on a platform that has no unix sockets
at all -- which is the whole of why those tests are not simply skipped here.
"""


def _connectability(test, live=()):
    """Answer "can this address be connected to" from a set, not from a socket.

    The seam these tests were missing. Most of them are about the probe's
    decision logic -- which of libatspi's three sources gets consulted, what a
    refusal means, what is memoized -- and reachability is only the scaffolding
    that provokes the decision. Expressing "dead" as a real
    `unix:path=/nonexistent/...` handed that question to the host's socket
    stack, so on a machine with no `AF_UNIX` the logic could not be tested at
    all: `_address_connectable` rightly answers None ("cannot check") there,
    the verdict came back True, and seven tests failed on Windows CI for a
    reason that had nothing to do with what any of them assert.

    Stated as a fact instead, the same tests run everywhere and assert the
    same thing. `_live_bus` and `needs_af_unix` stay for the handful whose
    point *is* that a real connect happens.
    """
    live = set(live)

    def answer(address):
        if not address:
            return None
        return address in live

    patcher = mock.patch.object(test.atspi, "_address_connectable", side_effect=answer)
    patcher.start()
    test.addCleanup(patcher.stop)


def _live_bus(test):
    """A unix socket that accepts connections, as an address to connect to.

    Skips on a platform with no `AF_UNIX` -- Windows, where CPython does not
    define it (see `ipc._AF_UNIX`). Only for tests that need a *listening*
    socket; a test that merely needs an address to be reachable should say so
    with `_connectability` and stay portable.
    """
    if not hasattr(socket, "AF_UNIX"):
        test.skipTest("no AF_UNIX on this platform; nothing can listen")
    directory = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    path = os.path.join(directory, "bus")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    test.addCleanup(listener.close)
    listener.bind(path)
    listener.listen(1)
    return f"unix:path={path}"


_DEAD_BUS = "unix:path=/nonexistent/at-spi/bus"
"""An address nothing is listening on, which is what a bus that has gone
leaves behind: the socket file outlives its listener and a connect to it is
refused. That refusal is the state the probe exists to catch."""

_LIVE_BUS = "unix:path=/run/user/1000/at-spi/bus"
"""A plausible address that `_connectability` is told is reachable.

Deliberately never connected to: naming a path that happens to exist on the
machine running the suite is exactly the coupling this pair of constants
exists to remove."""


def _gdbus_answering(address):
    """A `gdbus call` result, in the tuple syntax gdbus prints."""
    return subprocess.CompletedProcess([], 0, f"('{address}',)\n".encode(), b"")


def _xprop_answering(address):
    """An `xprop -root AT_SPI_BUS` result, as xprop prints one."""
    return subprocess.CompletedProcess(
        [], 0, f'AT_SPI_BUS(STRING) = "{address}"\n'.encode(), b""
    )


_XPROP_NOTHING_SET = subprocess.CompletedProcess(
    [], 0, b"AT_SPI_BUS:  no such atom on any window.\n", b""
)
"""What `xprop` prints when nobody ever set the property, exiting 0 to say
it -- a session whose root window has no `AT_SPI_BUS` on it."""

_XPROP_NO_DISPLAY = subprocess.CompletedProcess(
    [], 1, b"", b"xprop:  unable to open display ':99'"
)
"""What it does for a display it cannot open, which is `XOpenDisplay`
returning NULL in libatspi -- also no address from X11."""


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
        # These are about the session-bus half, so the session they run on
        # has to be one that asks it: `WAYLAND_DISPLAY` set is what makes
        # libatspi skip the X11 root-window property. Pinned rather than
        # inherited, so the answers below do not depend on the machine.
        patcher = _environment(DISPLAY=":0", WAYLAND_DISPLAY="wayland-0")
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_an_address_nothing_is_listening_on_answers_no(self):
        # The bug this probe had. at-spi-bus-launcher outlives the bus it
        # launched and goes on answering GetAddress with that bus's address,
        # so "the launcher replied" and "libatspi can connect" are different
        # questions -- and libatspi dies on the second. Measured on a real
        # machine in that state: GetAddress answered, connecting was refused,
        # and `import dogtail.tree` aborted the process with SIGABRT.
        _connectability(self)  # nothing is reachable
        answer, _ = self._run(return_value=_gdbus_answering(_DEAD_BUS))
        self.assertIs(answer, False)

    def test_a_dead_address_is_not_memoized_as_reachable(self):
        _connectability(self)  # nothing is reachable
        with (
            mock.patch.object(
                self.atspi.shutil, "which", return_value="/usr/bin/gdbus"
            ),
            mock.patch.object(self.atspi.subprocess, "run") as run,
        ):
            run.return_value = _gdbus_answering(_DEAD_BUS)
            self.assertIs(self.atspi.a11y_bus_probe(), False)
            self.assertIs(self.atspi.a11y_bus_probe(), False)

    def test_an_address_that_accepts_a_connection_answers_yes(self):
        # A real listening socket rather than a patched helper: the thing
        # being asserted is that a connect actually happens.
        address = _live_bus(self)
        answer, _ = self._run(
            return_value=subprocess.CompletedProcess(
                [], 0, f"('{address}',)\n".encode(), b""
            )
        )
        self.assertIs(answer, True)

    def test_an_address_this_cannot_check_is_not_a_no(self):
        # "Could not ask" must not become "no" here any more than anywhere
        # else in this probe: a tcp address is one this has no cheap way to
        # test, and refusing AT-SPI over it would be the worse error.
        answer, _ = self._run(
            return_value=subprocess.CompletedProcess(
                [], 0, b"('tcp:host=localhost,port=12345',)\n", b""
            )
        )
        self.assertIs(answer, True)

    def test_the_address_is_parsed_out_of_gdbus_tuple_syntax(self):
        self.assertEqual(
            self.atspi._address_from("('unix:path=/run/user/1000/at-spi/bus',)\n"),
            "unix:path=/run/user/1000/at-spi/bus",
        )
        self.assertEqual(
            self.atspi._address_from(b"('unix:path=/tmp/bus',)\n"),
            "unix:path=/tmp/bus",
        )
        for unparseable in (None, "", b"", "no quotes here"):
            with self.subTest(reply=unparseable):
                self.assertIsNone(self.atspi._address_from(unparseable))

    @needs_af_unix
    def test_a_guid_suffix_does_not_hide_the_socket_path(self):
        # The form a real daemon hands out. Unpatched on purpose: this one is
        # about `_address_connectable` itself picking the path out of the
        # address, so it needs the real socket layer to answer -- and where
        # there is none, the honest answer is None ("cannot check"), not
        # False, which is why this skips rather than asserting.
        self.assertIs(
            self.atspi._address_connectable("unix:path=/nonexistent/bus,guid=abc123"),
            False,
        )

    @needs_af_unix
    def test_a_dead_first_alternative_does_not_condemn_the_list(self):
        # A D-Bus address is semicolon-separated alternatives and libdbus
        # walks them until one connects. Judging the list by its first entry
        # reported a reachable bus as unavailable and refused AT-SPI over a
        # bus that works.
        live = _live_bus(self)
        self.assertIs(
            self.atspi._address_connectable(f"unix:path=/nonexistent/bus;{live}"),
            True,
        )

    @needs_af_unix
    def test_a_list_with_nothing_alive_is_still_a_no(self):
        self.assertIs(
            self.atspi._address_connectable(
                "unix:path=/nonexistent/bus;unix:path=/also/nonexistent"
            ),
            False,
        )


class TestTheAddressLibatspiWouldConnectTo(unittest.TestCase):
    """Which of libatspi's three sources the probe asks, and in what order.

    `atspi_get_a11y_bus` reads `$AT_SPI_BUS_ADDRESS`, then the `AT_SPI_BUS`
    property on the root window of the display when `WAYLAND_DISPLAY` is
    unset, then `org.a11y.Bus.GetAddress` on the session bus, stopping at
    the first that yields an address. A probe that asks a source libatspi
    stops before is answering about a bus it never touches -- measured on
    this machine on 2026-09-22, and how the abort got through.

    Every test here asserts *which source was consulted*; whether an address
    answers is only what provokes the probe to move on. So reachability is
    stated once in setUp rather than staged with real sockets -- which keeps
    these runnable on a platform that has no unix sockets, and stops them
    depending on whether `_LIVE_BUS`'s path happens to exist on the machine
    running the suite.
    """

    def setUp(self):
        from pyguitest.backends import atspi

        self.atspi = atspi
        previous = atspi._A11Y_BUS_ANSWERED
        atspi._A11Y_BUS_ANSWERED = False
        self.addCleanup(setattr, atspi, "_A11Y_BUS_ANSWERED", previous)
        _connectability(self, live={_LIVE_BUS})

    def _probe(self, env, **replies):
        """Run the probe against canned replies, one per tool it may spawn.

        `which` reports exactly the tools given a reply, so leaving one out
        is how a test says a tool is not installed; `run` refuses anything
        nothing was arranged for, so a tool this should not have asked fails
        the test rather than getting a Mock. Returns the answer and the
        commands actually run.
        """
        commands = []

        def fake_run(command, **_kwargs):
            commands.append(command)
            for tool, reply in replies.items():
                if os.path.basename(command[0]) == tool:
                    # An exception is a reply too: subprocess.run raises for a
                    # timeout rather than returning one, and how each half of
                    # the probe treats that is its own rule worth testing.
                    if isinstance(reply, BaseException):
                        raise reply
                    return reply
            raise AssertionError(f"nothing arranged for {command}")

        def fake_which(tool):
            return f"/usr/bin/{tool}" if tool in replies else None

        with (
            _environment(**env),
            mock.patch.object(self.atspi.shutil, "which", side_effect=fake_which),
            mock.patch.object(self.atspi.subprocess, "run", side_effect=fake_run),
        ):
            return self.atspi.a11y_bus_probe(), commands

    def _asked(self, commands):
        """The tools a probe run consulted, by name."""
        return [os.path.basename(command[0]) for command in commands]

    def _display(self, command):
        """The display an `xprop` command was pointed at."""
        return command[command.index("-display") + 1]

    def test_a_wayland_session_never_reads_the_property(self):
        # XWayland on this desktop: both names set, so libatspi goes
        # straight to the session bus and whatever is on the root window is
        # not consulted -- the case that answered correctly before the fix.
        answer, commands = self._probe(
            {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"},
            gdbus=_gdbus_answering(_LIVE_BUS),
            xprop=_xprop_answering(_DEAD_BUS),  # arranged for, never asked
        )
        self.assertIs(answer, True)
        self.assertEqual(self._asked(commands), ["gdbus"])

    def test_a_dead_property_is_the_answer_libatspi_dies_on(self):
        # The recording's environment, `scoped_environment` having stripped
        # WAYLAND_DISPLAY: libatspi never asks the session bus here, so an
        # answer from one -- this test's live bus included -- is not the
        # bus it would connect to.
        answer, commands = self._probe(
            {"DISPLAY": ":0"},
            xprop=_xprop_answering(_DEAD_BUS),
            gdbus=_gdbus_answering(_LIVE_BUS),
        )
        self.assertIs(answer, False)
        self.assertEqual(self._asked(commands), ["xprop"])

    def test_a_live_property_is_reachable_without_the_session_bus(self):
        answer, commands = self._probe(
            {"DISPLAY": ":0"}, xprop=_xprop_answering(_LIVE_BUS)
        )
        self.assertIs(answer, True)
        self.assertEqual(self._asked(commands), ["xprop"])

    def test_the_display_is_read_the_way_libatspi_reads_it(self):
        # `spi_display_name` strips the screen suffix: the property lives
        # on the root window of the display, not of the screen.
        _, commands = self._probe(
            {"DISPLAY": ":0.1"}, xprop=_xprop_answering(_DEAD_BUS)
        )
        self.assertEqual(self._display(commands[0]), ":0")

    def test_at_spi_display_wins_over_display(self):
        _, commands = self._probe(
            {"DISPLAY": ":0", "AT_SPI_DISPLAY": ":7"},
            xprop=_xprop_answering(_DEAD_BUS),
        )
        self.assertEqual(self._display(commands[0]), ":7")

    def test_at_spi_display_alone_still_reads_the_property(self):
        # AT_SPI_DISPLAY set and DISPLAY unset. libatspi gates the X11
        # source on WAYLAND_DISPLAY alone and takes its display from
        # `spi_display_name`, which prefers AT_SPI_DISPLAY -- so it reads
        # the property here. Gating this probe on DISPLAY instead made it
        # skip the property, answer from the session bus, and call a bus
        # reachable while libatspi connected to the stale one and aborted:
        # the precise failure the probe exists to prevent, so it is asserted
        # by which tool gets asked rather than only by the verdict.
        answer, commands = self._probe(
            {"AT_SPI_DISPLAY": ":7"},
            xprop=_xprop_answering(_DEAD_BUS),
            gdbus=_gdbus_answering(_LIVE_BUS),
        )
        self.assertEqual(self._asked(commands), ["xprop"])
        self.assertEqual(self._display(commands[0]), ":7")
        self.assertIs(answer, False)

    def test_an_xprop_that_times_out_answers_no_rather_than_asking_elsewhere(self):
        # A slow X server is not an absent property: libatspi will still read
        # the address off that root window. Falling through to the session bus
        # would answer True about a bus it is never going to touch -- and
        # memoize it -- which is the failure this probe exists to prevent. The
        # session-bus half already answers a timeout with no; this is the
        # matching rule for the X11 half.
        answer, commands = self._probe(
            {"DISPLAY": ":0"},
            xprop=subprocess.TimeoutExpired("xprop", 5),
            gdbus=_gdbus_answering(_LIVE_BUS),  # arranged for, must not be asked
        )
        self.assertIs(answer, False)
        self.assertEqual(self._asked(commands), ["xprop"])

    def test_no_display_at_all_falls_through_to_the_session_bus(self):
        # The other half of that gate: with neither name set there is no
        # root window to read, so the property is skipped the way libatspi
        # skips it when XOpenDisplay fails, and the session bus answers.
        answer, commands = self._probe({}, gdbus=_gdbus_answering(_LIVE_BUS))
        self.assertEqual(self._asked(commands), ["gdbus"])
        self.assertIs(answer, True)

    def test_an_environment_address_ends_the_search(self):
        # libatspi's first source, taken as given: DISPLAY set and
        # WAYLAND_DISPLAY unset both ways, because neither is reached. The
        # address is still *checked*, by connecting -- that is the step
        # libatspi aborts on, whichever source named the address.
        answer, commands = self._probe(
            {"DISPLAY": ":0", "AT_SPI_BUS_ADDRESS": _DEAD_BUS},
            xprop=_xprop_answering(_LIVE_BUS),
            gdbus=_gdbus_answering(_LIVE_BUS),
        )
        self.assertIs(answer, False)
        self.assertEqual(commands, [])

    def test_an_empty_environment_address_is_not_an_address(self):
        # `address_env != NULL && *address_env != 0` in libatspi, which is
        # this: an empty name falls through to X11 rather than being
        # connected to.
        answer, commands = self._probe(
            {"DISPLAY": ":0", "AT_SPI_BUS_ADDRESS": ""},
            xprop=_xprop_answering(_LIVE_BUS),
        )
        self.assertIs(answer, True)
        self.assertEqual(self._asked(commands), ["xprop"])

    def test_a_property_nobody_set_sends_libatspi_on_to_the_session_bus(self):
        answer, commands = self._probe(
            {"DISPLAY": ":0"},
            xprop=_XPROP_NOTHING_SET,
            gdbus=_gdbus_answering(_DEAD_BUS),
        )
        self.assertIs(answer, False)
        self.assertEqual(self._asked(commands), ["xprop", "gdbus"])

    def test_a_display_that_cannot_be_opened_does_the_same(self):
        answer, commands = self._probe(
            {"DISPLAY": ":99"},
            xprop=_XPROP_NO_DISPLAY,
            gdbus=_gdbus_answering(_DEAD_BUS),
        )
        self.assertIs(answer, False)
        self.assertEqual(self._asked(commands), ["xprop", "gdbus"])

    def test_no_xprop_leaves_the_session_bus_to_answer(self):
        # An X11 session without x11-utils still has a bus, so that half is
        # asked -- and either answer has to be the bus's own, rather than a
        # no because a tool is missing.
        dead, commands = self._probe(
            {"DISPLAY": ":0"}, gdbus=_gdbus_answering(_DEAD_BUS)
        )
        self.assertIs(dead, False)
        self.assertEqual(self._asked(commands), ["gdbus"])
        live, commands = self._probe(
            {"DISPLAY": ":0"}, gdbus=_gdbus_answering(_LIVE_BUS)
        )
        self.assertIs(live, True)
        self.assertEqual(self._asked(commands), ["gdbus"])

    def test_no_tools_at_all_was_never_asked(self):
        # Neither installed, on a session that invokes both: still the
        # third answer, which `a11y_bus_reachable` reads as "try AT-SPI
        # anyway" rather than as a no.
        answer, commands = self._probe({"DISPLAY": ":0"})
        self.assertIsNone(answer)
        self.assertEqual(commands, [])


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

    def test_an_application_that_exited_is_skipped_not_fatal(self):
        # Mirrors element_at's own _DeadApplication test: an app closing
        # between the application list and reading its children is
        # ordinary, and every window this call is still able to answer
        # about is still answerable.
        gui = self.backend()
        gui._tree.root.children.insert(0, _DeadApplication())
        windows = gui.windows()
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].app_id, "gedit")


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
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            gui = self.backend()
            self.frame._active = True
            self.assertEqual(gui.active_window().title, "Document - Editor")

    def test_returns_none_when_no_frame_is_active(self):
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
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

    def test_a_button_is_found_whichever_name_atspi_gives_it(self):
        # at-spi2 renamed ATSPI_ROLE_PUSH_BUTTON to ATSPI_ROLE_BUTTON without
        # changing the integer, so the string a desktop reports depends on its
        # version. Measured on at-spi2-core 2.61.1: gnome-calculator publishes
        # enum 43 for all thirty of its buttons and the bus names it "button",
        # so Session.button -- which asks for "push button" -- matched none.
        gui = self.backend()
        self.assertEqual(
            [e.name for e in gui.find_elements(role="button")], ["OK", "Cancel"]
        )

    def test_a_button_reported_by_the_new_name_answers_to_the_old_one(self):
        # The direction that actually broke: the desktop says "button" and
        # Role.PUSH_BUTTON is what every existing script and Session.button
        # asks with.
        self.button.roleName = "button"
        gui = self.backend()
        found = gui.find_element(role=Role.PUSH_BUTTON, name="OK")
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "OK")

    def test_a_role_with_no_alias_still_has_to_match_exactly(self):
        # The aliasing is a fixed table of names at-spi2 itself moved, not
        # fuzzy matching: "check" must not find a "check box".
        gui = self.backend()
        self.assertEqual(gui.find_elements(role="check"), [])
        self.assertEqual(
            [e.name for e in gui.find_elements(role="check box")],
            ["Enable notifications"],
        )

    def test_click_needs_no_coordinates_or_injection(self):
        gui = self.backend()
        gui.find_element(name="OK").click()
        self.assertTrue(self.button.clicked)

    def test_click_falls_back_to_the_action_interface_without_ponytail(self):
        # dogtail's own click() is coordinate-based and needs GNOME's
        # ponytail daemon to synthesize it under Wayland -- absent on every
        # other Wayland compositor (confirmed live against KDE Plasma 6 /
        # KWin). Element.click() should recover via AT-SPI's own action
        # interface instead of surfacing dogtail's daemon-not-found error.
        node = FakeNode(
            name="5",
            role="push button",
            actions={"Press": {}, "SetFocus": {}},
            click_raises=RuntimeError(
                "Error in ponytail initiation might be cause by several reasons"
            ),
        )
        self.atspi.Element(node).click()
        self.assertEqual(node.actions_performed, ["Press"])
        self.assertFalse(node.clicked)

    def test_click_reraises_an_unrelated_runtime_error(self):
        # Only the ponytail failure is worked around -- anything else out of
        # dogtail's click() is a real error and must not be swallowed.
        node = FakeNode(
            name="5",
            role="push button",
            actions={"Press": {}},
            click_raises=RuntimeError("some other failure"),
        )
        with self.assertRaises(RuntimeError):
            self.atspi.Element(node).click()
        self.assertEqual(node.actions_performed, [])

    def test_click_reraises_ponytail_failure_with_no_usable_action(self):
        # KDE's QML-based Kickoff menu publishes its category labels with no
        # Action interface at all -- ShowMenu here stands in for "something
        # unrelated to clicking", same as having none. Raised as pyguitest's
        # own typed error, not dogtail's raw, GNOME-specific ponytail
        # RuntimeError, which names a daemon this compositor never had.
        node = FakeNode(
            name="5",
            role="push button",
            actions={"ShowMenu": {}},
            click_raises=RuntimeError(
                "Error in ponytail initiation might be cause by several reasons"
            ),
        )
        with self.assertRaises(ElementNotActionable) as ctx:
            self.atspi.Element(node).click()
        self.assertEqual(ctx.exception.role, "push button")
        self.assertEqual(ctx.exception.name, "5")
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

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

    def test_pid_reports_the_owning_process(self):
        gui = self.backend()
        self.assertEqual(gui.find_element(name="OK").pid, 4242)

    def test_an_unanswered_pid_is_none_rather_than_zero(self):
        # A bridge that does not publish one answers 0, which means "I do
        # not know" -- the same answer as not being asked, and the caller
        # comparing it against a window's pid must not read it as a
        # process id that happens to be zero.
        gui = self.backend()
        self.assertIsNone(gui.find_element(name="Cancel").pid)

    def test_a_bridge_that_raises_leaves_the_pid_unknown(self):
        gui = self.backend()
        element = gui.find_element(name="OK")
        element.node.get_process_id = lambda: (_ for _ in ()).throw(RuntimeError("no"))
        self.assertIsNone(element.pid)

    def test_alive_is_true_for_an_ordinary_live_node(self):
        gui = self.backend()
        self.assertTrue(gui.find_element(name="OK").alive)

    def test_alive_is_false_once_the_node_reports_dead(self):
        # The regression this exists for: every property raises from
        # wherever it is touched once the widget is gone -- an application
        # redraw, a closed dialog -- with nothing to check first.
        gui = self.backend()
        element = gui.find_element(name="OK")
        element.node.dead = True
        self.assertFalse(element.alive)

    def test_alive_is_false_when_the_node_is_fully_reaped(self):
        # Not merely marked dead -- gone from the bus entirely, so even
        # asking raises. That still has to answer False, not propagate.
        self.assertFalse(self.atspi.Element(_RaisesOnDead()).alive)


class TestElementGeometry(AtspiTestCase):
    """Extents and hit-testing: the coordinate half of the element API.

    Both are gated behind ELEMENT_GEOMETRY for the same reason geometry()
    is gated behind WINDOW_GEOMETRY -- a pure Wayland client is not told
    where it sits on screen, so the numbers exist but mean nothing.
    """

    def gui(self, session_type=SessionType.X11):
        return self.backend(session_type)

    def test_declared_under_x11_and_withheld_under_wayland(self):
        self.assertIn(Capability.ELEMENT_GEOMETRY, self.gui().capabilities)
        self.assertNotIn(
            Capability.ELEMENT_GEOMETRY,
            self.gui(SessionType.WAYLAND).capabilities,
        )

    def test_extents_returns_the_screen_rectangle(self):
        gui = self.gui()
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(gui.find_element(name="OK")), (10, 20, 80, 30))

    def test_extents_refuses_under_pure_wayland_with_the_reason(self):
        gui = self.gui(SessionType.WAYLAND)
        element = gui.find_element(name="OK")
        with self.assertRaises(CapabilityUnsupported) as ctx:
            gui.extents(element)
        self.assertIn("where it is on screen", str(ctx.exception))

    def test_an_element_with_no_rectangle_answers_none_rather_than_raising(self):
        # Having no Component interface is an ordinary fact about an
        # ordinary element, not a missing capability.
        gui = self.gui()
        element = gui.find_element(name="OK")
        element.node._component = False
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertIsNone(gui.extents(element))

    def test_an_empty_rectangle_is_none_too(self):
        # AT-SPI's extents are meaningful only while the element is
        # showing; an off-screen one reports a zero-size box, and handing
        # that back as a rectangle invites a click at its corner.
        gui = self.gui()
        element = gui.find_element(name="OK")
        element.node.size = (0, 0)
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertIsNone(gui.extents(element))

    def test_a_component_with_no_position_is_none_rather_than_int_min(self):
        """AT-SPI answers INT_MIN for x and y when a component is not showing.

        It reports that instead of failing, and usually with a 1x1 size, which
        slips past the zero-size check above. The result is a rectangle no
        caller can use: a click point derived from it is nowhere, a
        containment test against it is always false, and `_area` scores it 1
        -- the smallest possible, which `element_at` reads as *most specific*.

        Not an edge case. 177 of the 207 nodes in an ordinary gedit window
        report it, every one of them inside a popover or menu that has not
        been opened (GNOME Shell 51.rc, docs/validation.md).
        """
        gui = self.gui()
        element = gui.find_element(name="OK")
        element.node.position = (-(2**31), -(2**31))
        element.node.size = (1, 1)
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertIsNone(gui.extents(element))

    def test_a_window_dragged_off_screen_keeps_its_negative_coordinates(self):
        # The control for the test above: the threshold has to be far enough
        # below any real coordinate that an ordinary off-screen position is
        # still a position. A window pulled off the left of a multi-monitor
        # desktop lives in the thousands, not the millions.
        gui = self.gui()
        element = gui.find_element(name="OK")
        element.node.position = (-3000, -1200)
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(element), (-3000, -1200, 80, 30))

    def test_element_at_finds_the_element_under_the_point(self):
        gui = self.gui()
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.element_at(20, 30).name, "OK")
            self.assertEqual(gui.element_at(120, 30).name, "Cancel")

    def test_element_at_descends_past_the_frame_to_a_leaf(self):
        # The frame claims the point first; the answer must be the deepest
        # element that claims it, not the first.
        label = FakeNode("Save", "label", position=(20, 25), size=(20, 10))
        self.button.children.append(label)
        label.parent = self.button
        gui = self.gui()
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.element_at(25, 30).name, "Save")

    def test_element_at_is_none_where_nothing_covers_the_point(self):
        gui = self.gui()
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertIsNone(gui.element_at(4000, 4000))

    def test_a_cyclic_tree_terminates(self):
        # A node that answers the hit test with itself would otherwise
        # descend forever; the depth cap makes it answer that node.
        gui = self.gui()
        self.button.children.append(self.button)
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.element_at(20, 30).name, "OK")

    def test_an_application_that_raises_does_not_take_the_hit_test_with_it(self):
        # An application exiting mid-walk is ordinary. The point asked
        # about is still answerable by the applications that remain.
        gui = self.gui()
        gui._tree.root.children.insert(0, _DeadApplication())
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.element_at(20, 30).name, "OK")

    def test_element_at_refuses_under_pure_wayland_with_the_reason(self):
        gui = self.gui(SessionType.WAYLAND)
        with self.assertRaises(CapabilityUnsupported) as ctx:
            gui.element_at(20, 30)
        self.assertIn("where it is on screen", str(ctx.exception))


class TestElementDoubleClick(AtspiTestCase):
    """An element reaches the pointer through the session that found it.

    double_click cannot be an accessible action -- no toolkit publishes one
    -- so the gesture has to fall back to the pointer, and the pointer is
    the Session's. That makes the back-reference from element to session the
    thing worth pinning: whether elements carry it, whether walking the tree
    keeps it, and what happens when there is none.
    """

    def session(self, session_type=SessionType.X11):
        import dataclasses

        env = detect({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"})
        env = dataclasses.replace(env, session_type=session_type)
        return pyguitest.Session(self.atspi.AtspiBackend(env), env)

    def test_an_element_a_session_found_carries_that_session(self):
        gui = self.session()
        self.assertIs(gui.button("OK")._session, gui)

    def test_the_tree_root_is_bound_too(self):
        # root_element() bypasses elements(), so it has to bind as well --
        # otherwise an element reached through it looks exactly like a
        # bound one and fails only once double_click is called.
        gui = self.session()
        self.assertIs(gui.root_element()._session, gui)

    def test_an_element_found_through_another_inherits_the_session(self):
        # The locator form the recorder emits: a child of root_element().
        gui = self.session()
        child = gui.root_element().child(role=Role.PUSH_BUTTON, name="OK")
        self.assertEqual(child.name, "OK")
        self.assertIs(child._session, gui)

    def test_double_click_delegates_to_the_session(self):
        gui = self.session()
        element = gui.button("OK")
        with mock.patch.object(pyguitest.Session, "double_click_element") as sent:
            element.double_click()
        sent.assert_called_once_with(element)

    def test_an_element_taken_from_the_backend_says_what_to_do_instead(self):
        # A backend does not know which Session is above it, so this element
        # has nowhere to delegate to. Doing nothing would be the one
        # unacceptable answer.
        element = self.backend().find_elements(name="OK")[0]
        with self.assertRaises(PyGUITestError) as caught:
            element.double_click()
        message = str(caught.exception)
        self.assertIn("no session", message)
        self.assertIn("double_click_element", message)


if __name__ == "__main__":
    unittest.main()


class TestRoleSpellings(unittest.TestCase):
    """The table behind the role aliasing, on its own."""

    def test_the_two_button_spellings_are_one_role(self):
        self.assertEqual(spellings("button"), spellings("push button"))
        self.assertIn("button", spellings(Role.PUSH_BUTTON))

    def test_an_unaliased_role_is_only_itself(self):
        self.assertEqual(spellings("check box"), frozenset({"check box"}))

    def test_an_unknown_role_is_passed_through_rather_than_dropped(self):
        # A toolkit may publish a role this table has never heard of, and
        # asking for it has to keep working.
        self.assertEqual(spellings("gadget"), frozenset({"gadget"}))
