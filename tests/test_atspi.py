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
    ElementNotFound,
    PyGUITestError,
)
from pyguitest.roles import Role, spellings
from pyguitest.session import SessionType, detect

_FAKE_SCREEN_COORDS = 0
"""`Atspi.CoordType.SCREEN`'s value, as a plain int: the fakes compare it
numerically and never need the real enum."""

_FAKE_WINDOW_COORDS = 1
"""`Atspi.CoordType.WINDOW`'s value, likewise -- what `fake_pyatspi` names
`WINDOW_COORDS`, and what `FakeNode.window_position` answers to."""


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
        x, y = self.node.rect_origin(coord_type)
        width, height = self.node.size
        return FakeRect(x, y, width, height)

    def getAccessibleAtPoint(self, x, y, coord_type):
        if not self.node.covers(x, y, coord_type):
            return None
        for child in self.node.children:
            if child.covers(x, y, coord_type):
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
        state_set=(),
        value=False,
        window_position=None,
    ):
        self.name = name
        self.text = None
        self.value = value
        self._combovalue = None
        self.roleName = role
        self.children = list(children)
        self.position = position
        self.window_position = window_position
        """Where the node says it is in WINDOW coordinates -- relative to its
        own toplevel -- where that differs from `position`, its SCREEN
        answer. None models the common case, WINDOW == SCREEN: every
        native Wayland client (never told where it is, so both are
        window-relative), and an X11 toolkit for a window at the screen's
        origin. The two differ for GTK4 everywhere (SCREEN is (0, 0) for
        every node, WINDOW real) and for GTK3 and Qt under X11 (SCREEN real,
        WINDOW relative to the client window); see
        `atspi._gi_window_coords`."""
        self.size = size
        self.parent = None
        self.showing = True
        self.clicked = False
        self.focused = False
        self._active = active
        self.sensitive = sensitive
        self.description = description
        self._pid = pid
        self.state_set = list(state_set)
        """Fake AT-SPI states, as plain names -- `Element._state_names` reads
        each real state's `.name`, so a bare string here stands in for one
        just as well as a real `Atspi.StateType` member does."""
        self._component = component
        self.dead = dead
        self.actions = actions or {}
        self.actions_performed = []
        self._click_raises = click_raises
        for child in self.children:
            child.parent = self

    def rect_origin(self, coord_type=_FAKE_SCREEN_COORDS):
        """`position`, or `window_position` asked in WINDOW coordinates."""
        if coord_type == _FAKE_WINDOW_COORDS and self.window_position is not None:
            return self.window_position
        return self.position

    def covers(self, x, y, coord_type=_FAKE_SCREEN_COORDS):
        left, top = self.rect_origin(coord_type)
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

    @property
    def combovalue(self):
        """Stands in for dogtail's combovalue: the last value written to it."""
        return self._combovalue

    @combovalue.setter
    def combovalue(self, value):
        self._combovalue = value

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

    def __len__(self):
        """Child count, as dogtail's `Node` answers it.

        dogtail makes a node a *sequence of its own children* -- `__len__`
        and `__getitem__`, no `__iter__` -- which is how `_past_a_container`
        reads them: `list(node)` walks `node[0]`, `node[1]`, ... until
        `IndexError`. A fake without the pair made that step see a node with
        no children, which is exactly the shape the code around it is written
        to treat as a dead node, so the one toolkit case that step exists for
        answered the tab list and looked like a passing negative control.
        """
        return len(self.children)

    def __getitem__(self, index):
        return self.children[index]

    def __bool__(self):
        # dogtail answers `self is not None`, deliberately not "has
        # children" -- so `if node:` is not a question about the tree, and
        # defining `__len__` above must not quietly turn it into one here.
        return True


class _PageTab(FakeNode):
    """A GtkNotebook page tab: a rectangle only as wide as its label.

    The one shape that breaks the geometric nesting `_descend` rests on. A
    notebook publishes a page's *contents* as children of the `page tab`,
    whose own rectangle is the little label at the top -- so the tab does not
    contain its own children, which every other node here does. The real
    `getAccessibleAtPoint` answers for a point in the page body anyway (Gtk's
    accessible hands that question on to the page), where the plain
    `FakeComponent` above refuses any point its own node does not cover --
    the check that makes it a stand-in for the ordinary case and not for this
    one, so the tab gets a component without it.
    """

    def queryComponent(self):
        if not self._component:
            raise NotImplementedError("no Component interface")
        return _PageTabComponent(self)


class _PageTabComponent(FakeComponent):
    def getAccessibleAtPoint(self, x, y, coord_type):
        for child in self.node.children:
            if child.covers(x, y):
                return child
        return None


class _WindowCoordsNode(FakeNode):
    """A node that answers a point it is nowhere near.

    What a toolkit reporting widgets in *window* coordinates does: a native
    Wayland client is never told where it sits, so its elements answer for a
    point hundreds of pixels away -- seen live as a terminal's "New Terminal"
    button, extents (0, 0, 34, 34), returned for a point at (49, 83).

    `_descend` refuses that by requiring every answer to contain the point,
    and the page-tab step beside it asks children directly, where the
    ordinary descent asks the parent for the child at the point -- so that is
    the one place the guard could be lost.
    """

    def queryComponent(self):
        if not self._component:
            raise NotImplementedError("no Component interface")
        return _WindowCoordsComponent(self)


class _WindowCoordsComponent(FakeComponent):
    def getAccessibleAtPoint(self, x, y, coord_type):
        return self.node.children[0] if self.node.children else None


class _ReapedStates(FakeNode):
    """A node gone from the bus: reading its state set raises."""

    @property
    def state_set(self):
        raise RuntimeError("the node is gone")

    @state_set.setter
    def state_set(self, value):
        pass


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
    module.DESKTOP_COORDS = _FAKE_SCREEN_COORDS
    module.WINDOW_COORDS = _FAKE_WINDOW_COORDS
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

    def backend(self, session_type=SessionType.X11, **kwargs):
        env = detect({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"})
        import dataclasses

        return self.atspi.AtspiBackend(
            dataclasses.replace(env, session_type=session_type), **kwargs
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

    def test_a_platform_without_unix_sockets_cannot_answer(self):
        """Windows defines no `socket.AF_UNIX`, so a unix address is unaskable.

        "Could not ask" has to stay the None the caller reads as "let libatspi
        decide" rather than a False that would refuse AT-SPI on a machine
        whose bus may well be there. Staged on the backend's own family
        rather than by deleting `socket.AF_UNIX`, so this covers the platform
        Windows compiles to and runs on every platform.
        """
        self.addCleanup(setattr, self.atspi, "_AF_UNIX", self.atspi._AF_UNIX)
        self.atspi._AF_UNIX = None
        self.assertIs(
            self.atspi._address_connectable("unix:path=/run/user/1000/at-spi/bus"),
            None,
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

    def test_a_message_box_and_a_file_dialog_are_windows(self):
        # Measured live on seven desktops: GTK3's GtkMessageDialog and Qt's
        # QMessageBox publish `alert`, GtkFileChooserDialog (and so Qt's
        # QFileDialog on a GTK platform theme) `file chooser` -- neither
        # `dialog`. Before the two were in WINDOW_ROLES, windows() left
        # them out and a driver waiting for a Save dialog to appear never
        # saw it.
        app = self.frame.parent
        for role, title in (("alert", "Unsaved changes"), ("file chooser", "Save")):
            toplevel = FakeNode(title, role, [], (0, 0), (400, 300))
            toplevel.parent = app
            app.children.append(toplevel)
        gui = self.backend()
        self.assertEqual(
            [w.title for w in gui.windows()],
            ["Document - Editor", "Unsaved changes", "Save"],
        )

    def test_a_window_carries_its_process_id(self):
        # Confirmed live (Debian 13, GNOME Wayland, no gnomeshell extension
        # installed): windows() built every Window with pid=None, the
        # base.Window default, because the constructor call here never
        # passed one. A driver matching find_windows(title=...) against a
        # known pid got None back every time and silently matched nothing
        # -- not because the window was not found, but because nothing
        # had ever read its pid off the frame node in the first place.
        self.frame._pid = 4343
        gui = self.backend()
        self.assertEqual(gui.windows()[0].pid, 4343)

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


class _HangingNameNode(FakeNode):
    """A frame whose `name` read never returns.

    The `.name` half of the live-confirmed GhostBSD hang `_run_bounded`
    exists for, one level up from `find_elements`'s `findChildren` (see
    `_HangingFindChildrenNode`): construction still works (only the getter
    hangs, and `__init__` only ever *sets* the name), so `windows()` reaches
    the read and must bound it.
    """

    @property
    def name(self):
        import time

        time.sleep(999)
        return ""  # pragma: no cover - never reached within the test timeout

    @name.setter
    def name(self, value):
        self._name = value


class _HangingRoleNode(FakeNode):
    """A frame whose `roleName` read never returns -- the role half."""

    @property
    def roleName(self):
        import time

        time.sleep(999)
        return ""  # pragma: no cover - never reached within the test timeout

    @roleName.setter
    def roleName(self, value):
        self._rolename = value


class _RaisingNameNode(FakeNode):
    """A frame whose `name` read fails outright (RuntimeError, not a hang).

    A frame dying mid-enumeration: `windows()` degrades it to an empty title
    rather than crashing, the same way the app-children read degrades a dead
    application.
    """

    @property
    def name(self):
        raise RuntimeError("name read failed")

    @name.setter
    def name(self, value):
        self._name = value


class TestWindowsBoundedReads(AtspiTestCase):
    """windows()'s per-frame `roleName`/`name`/`app.name` reads are bounded.

    They are the same shape of AT-SPI round trip as every `Element` property
    and `_frame_pid`, and were the one set this method left unbounded: a hang
    in any of them wedged windows() -- and `active_window`, `find_windows`,
    `wait_for_window`, `is_window_open`, `refresh_window`, which all funnel
    through it -- with no timeout and no recovery. See atspi.py `windows()`
    and `_run_bounded`.
    """

    def test_a_hung_frame_name_is_reported_not_hung(self):
        gui = self.backend(call_timeout=0.05)
        app = self.frame.parent
        hung = _HangingNameNode("x", "frame", [], (0, 0), (400, 300))
        hung.parent = app
        app.children.append(hung)
        self.addCleanup(app.children.remove, hung)
        with self.assertRaises(pyguitest.BackendCallTimeout):
            gui.windows()

    def test_a_hung_role_read_is_reported_not_hung(self):
        gui = self.backend(call_timeout=0.05)
        app = self.frame.parent
        hung = _HangingRoleNode("x", "frame", [], (0, 0), (400, 300))
        hung.parent = app
        app.children.append(hung)
        self.addCleanup(app.children.remove, hung)
        with self.assertRaises(pyguitest.BackendCallTimeout):
            gui.windows()

    def test_a_hung_app_name_is_reported_not_hung(self):
        gui = self.backend(call_timeout=0.05)
        child = FakeNode("Second window", "frame", [], (0, 0), (400, 300))
        hung_app = _HangingNameNode("x", "application", [child])
        child.parent = hung_app
        gui._tree.root.children.append(hung_app)
        self.addCleanup(gui._tree.root.children.remove, hung_app)
        with self.assertRaises(pyguitest.BackendCallTimeout):
            gui.windows()

    def test_an_ordinary_name_failure_degrades_to_an_empty_title(self):
        # A non-timeout read failure (a frame dying mid-walk) is not fatal:
        # the good window is still listed, and the broken frame still becomes
        # a window -- just with an empty title rather than taking windows()
        # down with it.
        gui = self.backend(call_timeout=0.05)
        app = self.frame.parent
        broken = _RaisingNameNode("x", "frame", [], (0, 0), (400, 300))
        broken.parent = app
        broken._pid = 77
        app.children.append(broken)
        self.addCleanup(app.children.remove, broken)
        titles = {w.title for w in gui.windows()}
        self.assertIn("Document - Editor", titles)
        self.assertIn("", titles)


class TestActiveWindow(AtspiTestCase):
    """active_window() must not depend on pyatspi being importable.

    pyatspi is declared in no dependency list -- only dogtail comes from
    the atspi extra, with pyatspi meant to arrive from the distribution --
    so a box can have a perfectly working accessibility stack without it.
    Confirmed live on Debian 13 and Pop!_OS/COSMIC, where `import pyatspi`
    fails with AttributeError (a broken install, not a missing one) and
    this call used to raise CapabilityUnsupported from inside a capability
    the backend had already advertised as available. Reading the state set
    by name needs neither pyatspi nor a camelCase dogtail alias.
    """

    def test_active_window_works_without_pyatspi(self):
        with mock.patch.dict(sys.modules, {"pyatspi": None}):
            gui = self.backend()
            self.frame.state_set = ["ACTIVE"]
            self.assertEqual(gui.active_window().title, "Document - Editor")

    def test_returns_none_when_no_frame_is_active(self):
        with mock.patch.dict(sys.modules, {"pyatspi": None}):
            gui = self.backend()
            self.frame.state_set = []
            self.assertIsNone(gui.active_window())

    def test_a_pyatspi_dependent_call_still_raises_typed_not_bare(self):
        """The paths that *do* need pyatspi keep their typed failure.

        active_window no longer needs it, but element geometry still does.
        The guarantee under test is the one that mattered originally: a box
        with dogtail and no usable pyatspi gets CapabilityUnsupported
        naming pyatspi, not a bare ImportError escaping from inside a
        backend that otherwise looks fully constructed.
        """
        with mock.patch.dict(sys.modules, {"pyatspi": None}):
            gui = self.backend()
            with self.assertRaises(CapabilityUnsupported) as ctx:
                gui._pyatspi(Capability.ELEMENT_GEOMETRY)
            self.assertIn("pyatspi", str(ctx.exception))


class TestElements(AtspiTestCase):
    def test_find_elements_by_role(self):
        gui = self.backend()
        found = gui.find_elements(role="push button")
        self.assertEqual([e.name for e in found], ["OK", "Cancel"])

    def test_find_element_returns_none_when_absent(self):
        gui = self.backend()
        self.assertIsNone(gui.find_element(role="slider"))

    def test_find_elements_accepts_several_roles_at_once(self):
        # role=(...) matches the union of each role's own spellings in one
        # search, rather than needing one find_elements() call per role --
        # see roles.spellings for why that is not just convenience.
        gui = self.backend()
        found = gui.find_elements(role=("push button", "check box"))
        self.assertEqual(
            {e.name for e in found}, {"OK", "Cancel", "Enable notifications"}
        )


class _HangingFindChildrenNode(FakeNode):
    """A node whose `findChildren` never returns, within any test budget.

    Stands in for the real, live-confirmed AT-SPI hang `BackendCallTimeout`
    exists for (a `QTreeView`, GhostBSD): the call simply never comes back,
    no exception, nothing to catch. A `threading.Event` the test can set is
    not needed here -- the worker thread `_run_bounded` starts is
    `daemon=True` precisely so a thread that never finishes (this one)
    cannot block the test process either.
    """

    def findChildren(self, pred):
        import time

        time.sleep(999)
        return []  # pragma: no cover - never reached within the test timeout


class _HangingExtentsComponent(FakeComponent):
    def getExtents(self, coord_type):
        import time

        time.sleep(999)
        return FakeRect(0, 0, 0, 0)  # pragma: no cover - never reached


class _HangingExtentsNode(FakeNode):
    """A node whose Component interface answers, but `getExtents` hangs.

    The other half of the same live-confirmed hang, one level down
    (`_extents`'s own `getExtents` call rather than `find_elements`'s
    `findChildren`).
    """

    def queryComponent(self):
        if not self._component:
            raise NotImplementedError("no Component interface")
        return _HangingExtentsComponent(self)


class TestBackendCallTimeout(AtspiTestCase):
    """A worker-thread-bounded call that genuinely never returns is reported.

    Reported as hung rather than silently taking the whole session down
    with it -- see atspi.py's `_run_bounded` and errors.py's
    `BackendCallTimeout` for the live incident this guards against.
    """

    def test_find_elements_raises_on_a_genuinely_hung_call(self):
        gui = self.backend(call_timeout=0.05)
        hung = _HangingFindChildrenNode("Model Tree", "tree")
        self.frame.children.append(hung)
        self.addCleanup(self.frame.children.remove, hung)
        with self.assertRaises(pyguitest.BackendCallTimeout):
            gui.find_elements(role="tree", within=self.atspi.Element(self.frame))

    def test_extents_raises_on_a_genuinely_hung_call(self):
        gui = self.backend(call_timeout=0.05)
        hung = _HangingExtentsNode("Model Tree", "tree", position=(1, 2), size=(3, 4))
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            with self.assertRaises(pyguitest.BackendCallTimeout):
                gui.extents(self.atspi.Element(hung))

    def test_an_ordinary_fast_call_is_unaffected(self):
        # A short call_timeout must not misfire against a call that
        # actually returns promptly -- the guard is for a call that never
        # comes back at all, not a blanket slowdown.
        gui = self.backend(call_timeout=0.05)
        found = gui.find_elements(role="push button")
        self.assertEqual([e.name for e in found], ["OK", "Cancel"])

    def test_a_quick_ordinary_failure_still_degrades_to_none(self):
        # An element with no Component interface at all still answers None,
        # same as before this guard existed -- only a genuine hang becomes
        # BackendCallTimeout, not every other way extents() can fail.
        gui = self.backend(call_timeout=0.05)
        no_component = FakeNode("No Geometry", "label", component=False)
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertIsNone(gui.extents(self.atspi.Element(no_component)))


class _FakeWindow:
    __slots__ = ("pid", "title")

    def __init__(self, pid, title=None):
        self.pid = pid
        self.title = title


_FIXTURE_FRAME_SIZE = (800, 600)
"""The fixture frame's size (see `build_tree`), which every fake geometry
source below reports for the window it lists: a compositor that agrees
with the toolkit about the window's size, so the only thing placement has
to add is the position. A source reporting some other size is modelling a
decoration, and `TestWaylandPlacement` does that on purpose."""


class _FakeWindowGeometrySource:
    """A sibling backend standing in for `gnomeshell`.

    Knows one window's real screen position, keyed by pid, and nothing
    else `atspi.py`'s `_source_rects` needs.
    """

    def __init__(self, pid, offset, size=_FIXTURE_FRAME_SIZE):
        self._pid = pid
        self._offset = offset
        self._size = size

    def windows(self):
        return [_FakeWindow(self._pid)]

    def geometry(self, window):
        return (*self._offset, *self._size)


class _FakeManyWindowsGeometrySource:
    """A sibling backend listing several toplevels for one application.

    What the `x11` backend really answers for an application with a main
    window and a dialog open: every toplevel, each carrying the same pid,
    in whatever order the window manager stacks them.
    """

    def __init__(self, pid, offsets, titles=None, sizes=None):
        titles = titles or [None] * len(offsets)
        sizes = sizes or [_FIXTURE_FRAME_SIZE] * len(offsets)
        self._listed = [
            (_FakeWindow(pid, title=title), (*offset, *size))
            for offset, title, size in zip(offsets, titles, sizes, strict=True)
        ]

    def windows(self):
        return [window for window, _ in self._listed]

    def geometry(self, window):
        return next(rect for w, rect in self._listed if w is window)


class _FakeTitleOnlyGeometrySource:
    """A sibling backend standing in for `cosmic`.

    `pid` is always `None` on every `Window` this offers -- not a gap in
    this fake, but the real thing: confirmed against
    `zcosmic_toplevel_info_v1`'s own protocol XML (v3) that no pid-bearing
    event exists there at all, so `cosmic.py`'s real `windows()` can never
    populate it either. Keyed by title instead, the only identity this
    source can ever offer.
    """

    def __init__(self, title, offset, size=_FIXTURE_FRAME_SIZE):
        self._title = title
        self._offset = offset
        self._size = size

    def windows(self):
        return [_FakeWindow(None, title=self._title)]

    def geometry(self, window):
        return (*self._offset, *self._size)


class TestWaylandElementGeometry(AtspiTestCase):
    """The Wayland-only fix: a wired-in window-geometry source.

    AT-SPI's own coordinates are window-relative on Wayland -- see
    `_element_coords_trustworthy` -- so ELEMENT_GEOMETRY is withheld there
    unless a sibling backend's real window position is wired in.
    """

    def test_element_geometry_is_withheld_on_wayland_with_no_source(self):
        gui = self.backend(SessionType.WAYLAND)
        self.assertNotIn(Capability.ELEMENT_GEOMETRY, gui.capabilities)

    def test_a_window_geometry_source_restores_element_geometry(self):
        self.frame._pid = 4242
        gui = self.backend(SessionType.WAYLAND)
        gui.set_window_geometry_source(_FakeWindowGeometrySource(4242, (0, 32)))
        self.assertIn(Capability.ELEMENT_GEOMETRY, gui.capabilities)

    def test_extents_adds_the_real_window_offset(self):
        # The button's own fixture position is (10, 20) -- see build_tree --
        # which is window-relative on Wayland; with the window's real
        # screen position wired in as (0, 32), the true screen position is
        # the sum of the two, not either alone.
        self.frame._pid = 4242
        gui = self.backend(SessionType.WAYLAND)
        gui.set_window_geometry_source(_FakeWindowGeometrySource(4242, (0, 32)))
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (10, 52, 80, 30))

    def test_no_matching_window_raises_capability_unsupported(self):
        # The source exists, but no window of its own has this pid. The
        # element's own rectangle is fine -- it is the *window* that has no
        # position to add -- so the answer is a typed refusal naming that,
        # not None: None means "this element has no rectangle", and a caller
        # unpacking it got `TypeError: cannot unpack non-iterable NoneType
        # object`, which names neither cause. Measured live on niri 26.04,
        # where tile_pos_in_workspace_view is null even for an on-screen
        # tiled window and every element in the tree hit this path.
        self.frame._pid = 4242
        gui = self.backend(SessionType.WAYLAND)
        gui.set_window_geometry_source(_FakeWindowGeometrySource(9999, (0, 32)))
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            with self.assertRaises(CapabilityUnsupported) as caught:
                gui.extents(button)
        self.assertIn("window-relative", str(caught.exception))
        self.assertIn("is_window_viewable", str(caught.exception))

    def test_falls_back_to_title_when_the_source_never_has_a_pid(self):
        # COSMIC, confirmed live and against the protocol XML (see
        # _FakeTitleOnlyGeometrySource's own docstring): the geometry
        # source's windows() always answers pid=None, not just this one.
        # frame._pid is deliberately left unset here (defaults to 0 in
        # FakeNode) to assert this path does not depend on the frame side
        # having a pid either -- only a title.
        gui = self.backend(SessionType.WAYLAND)
        gui.set_window_geometry_source(
            _FakeTitleOnlyGeometrySource("Document - Editor", (0, 32))
        )
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (10, 52, 80, 30))

    def test_pid_match_is_tried_before_the_title_fallback(self):
        # A source offering both a correctly-pid-matched window and a
        # same-titled-but-wrong one must use the pid match, not stumble
        # into the title fallback by processing windows() in the wrong
        # order.
        class _MixedSource:
            def windows(self):
                return [
                    _FakeWindow(4242, title="Document - Editor"),
                    _FakeWindow(None, title="Document - Editor"),
                ]

            def geometry(self, window):
                return (0, 32, 800, 600) if window.pid == 4242 else (999, 999, 800, 600)

        self.frame._pid = 4242
        gui = self.backend(SessionType.WAYLAND)
        gui.set_window_geometry_source(_MixedSource())
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (10, 52, 80, 30))

    def test_no_title_match_either_raises_capability_unsupported(self):
        # Same refusal when the title route misses too -- see the pid test
        # above for why this is a typed error rather than None.
        gui = self.backend(SessionType.WAYLAND)
        gui.set_window_geometry_source(
            _FakeTitleOnlyGeometrySource("Some Other Window", (0, 32))
        )
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            with self.assertRaises(CapabilityUnsupported):
                gui.extents(button)

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

    def test_element_find_matches_whichever_name_atspi_gives_it(self):
        # The same rename, one layer down. Element.find built a dogtail
        # GenericPredicate, which compares the role asked for against the one
        # spelling this desktop reports -- so on an at-spi2 that says "button",
        # find(role=Role.PUSH_BUTTON) matched nothing and said so by returning
        # an empty list, which is not an error. Measured live on at-spi2-core
        # 2.62.1 (Fedora 45): elements(role="push button") found two buttons in
        # a window whose own frame.find(role="push button") found none.
        # Only OK's spelling is changed, so this also pins that one search
        # matches both names at once -- a tree mid-upgrade, or a toolkit that
        # publishes one and an at-spi2 that renames the other.
        self.button.roleName = "button"
        gui = self.backend()
        frame = gui.find_element(role=Role.FRAME)
        self.assertEqual(
            [e.name for e in frame.find(role=Role.PUSH_BUTTON)], ["OK", "Cancel"]
        )

    def test_element_child_matches_whichever_name_atspi_gives_it(self):
        # child() is find()'s first result, and the spelling the recorder
        # emits into generated scripts -- root_element().child(role=...).
        self.button.roleName = "button"
        gui = self.backend()
        frame = gui.find_element(role=Role.FRAME)
        self.assertEqual(frame.child(role=Role.PUSH_BUTTON).name, "OK")

    def test_the_two_search_layers_agree_on_every_role_they_are_asked(self):
        # The invariant that would have caught the above, rather than one
        # assertion per role: a descendant search scoped to a window must
        # return what the same search unscoped returns, for every role in the
        # tree, whichever spelling the bus happens to use. Element.find and
        # find_elements had drifted onto two different matchers, and only one
        # of them knew about the rename.
        for spelling in ("push button", "button"):
            with self.subTest(spelling=spelling):
                self.button.roleName = spelling
                gui = self.backend()
                frame = gui.find_element(role=Role.FRAME)
                for role in (Role.PUSH_BUTTON, Role.CHECK_BOX, "button"):
                    scoped = sorted(e.name for e in frame.find(role=role))
                    unscoped = sorted(
                        e.name for e in gui.find_elements(role=role, within=frame)
                    )
                    self.assertEqual(scoped, unscoped, f"role={role!r}")

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
        # The OK button fixture publishes no actions, so this exercises the
        # coordinate fallback -- see the action-preferring tests below for
        # the path that exists precisely because this one is not reliable.
        gui = self.backend()
        gui.find_element(name="OK").click()
        self.assertTrue(self.button.clicked)

    def test_click_prefers_a_published_action_over_coordinates(self):
        # GTK measured live (a checkbox and a radio button, both on the
        # probe window in pyguitest-recorder): each reports its whole row as
        # its rectangle rather than the small toggle the row reacts to, so a
        # coordinate click lands past it and changes nothing -- no
        # exception, just a click that silently did not work.
        # `click_raises` a plain Exception here, outside the tuple `click()`
        # catches, so if dogtail's coordinate path were touched at all this
        # test would fail with that error rather than pass.
        node = FakeNode(
            name="Enabled",
            role="check box",
            actions={"Press": {}, "SetFocus": {}},
            click_raises=Exception("coordinates were used"),
        )
        self.atspi.Element(node).click()
        self.assertEqual(node.actions_performed, ["Press"])
        self.assertFalse(node.clicked)

    def test_click_acts_through_toggle_on_an_element_that_offers_nothing_else(self):
        # A GtkSwitch publishes `toggle` alone. Measured live on GNOME: the
        # recorder called it clickable (UIA's click() accepts toggle), this
        # backend did not, and the replay fell to a coordinate click that needs
        # the ponytail daemon.
        node = FakeNode(
            name="Notifications",
            role="toggle button",
            actions={"toggle": {}},
            click_raises=Exception("coordinates were used"),
        )
        self.atspi.Element(node).click()
        self.assertEqual(node.actions_performed, ["toggle"])
        self.assertFalse(node.clicked)

    def test_click_prefers_click_or_press_to_toggle_whatever_the_order(self):
        for actions, expected in (
            ({"toggle": {}, "click": {}}, "click"),
            ({"toggle": {}, "press": {}}, "press"),
            ({"press": {}, "toggle": {}}, "press"),
        ):
            with self.subTest(actions=list(actions)):
                node = FakeNode(name="X", role="toggle button", actions=actions)
                self.atspi.Element(node).click()
                self.assertEqual(node.actions_performed, [expected])

    def test_click_opens_a_submenu_via_its_show_menu_action(self):
        # A submenu entry's click is "open this menu": Qt publishes `Show Menu`
        # alone on its submenu items, and without it menu_item("Tools").click()
        # found no clickable verb and fell to the coordinate path, which hangs
        # where there is no pointer (a headless session).
        node = FakeNode(
            name="Tools",
            role="menu item",
            actions={"Show Menu": {}},
            click_raises=Exception("coordinates were used"),
        )
        self.atspi.Element(node).click()
        self.assertEqual(node.actions_performed, ["Show Menu"])
        self.assertFalse(node.clicked)

    def test_an_activate_only_element_still_clicks_by_coordinate(self):
        # `activate` on a text entry means focus it, not click it: only the
        # three click-like verbs are acted through.
        node = FakeNode(name="Name", role="text", actions={"activate": {}})
        self.atspi.Element(node).click()
        self.assertEqual(node.actions_performed, [])
        self.assertTrue(node.clicked)

    def test_activate_only_element_acts_through_it_when_not_text_bearing(self):
        # The other side of the same fact: GTK4's GtkExpander publishes
        # `activate` as its ONLY action -- no click, no press -- measured
        # live on pyguitest-torture-test's Containers page ("More
        # Options"). `activate` means "click me" there, not "the user
        # pressed Enter", so a non-text role acts through it rather than
        # falling to the coordinate route.
        node = FakeNode(
            name="More Options",
            role="push button",
            actions={"activate": {}},
            click_raises=Exception("coordinates were used"),
        )
        self.atspi.Element(node).click()
        self.assertEqual(node.actions_performed, ["activate"])
        self.assertFalse(node.clicked)

    def test_activate_is_tried_only_after_every_click_like_verb_misses(self):
        # An element offering BOTH must still prefer the click-like verb --
        # activate is the last resort, never a competing first choice.
        node = FakeNode(
            name="Toggle Me",
            role="toggle button",
            actions={"activate": {}, "toggle": {}},
        )
        self.atspi.Element(node).click()
        self.assertEqual(node.actions_performed, ["toggle"])

    def test_activate_via_action_with_no_click_like_verb_on_text_still_refuses(self):
        # via="action" forbids the pointer fallback. A text role offering
        # only `activate` must still refuse rather than silently treat
        # activate as a click.
        node = FakeNode(name="Name", role="text", actions={"activate": {}})
        with self.assertRaises(ElementNotActionable):
            self.atspi.Element(node).click(via="action")
        self.assertEqual(node.actions_performed, [])

    def test_click_matches_an_action_name_case_insensitively(self):
        node = FakeNode(name="Gamma", role="menu item", actions={"CLICK": {}})
        self.atspi.Element(node).click()
        self.assertEqual(node.actions_performed, ["CLICK"])

    def test_click_falls_back_to_the_action_interface_without_ponytail(self):
        # dogtail's own click() is coordinate-based and needs GNOME's
        # ponytail daemon to synthesize it under Wayland -- absent on every
        # other Wayland compositor (confirmed live against KDE Plasma 6 /
        # KWin). Reached only where no action was published, since one
        # being there is now tried first and never raises this.
        node = FakeNode(
            name="5",
            role="push button",
            actions={"SetFocus": {}},
            click_raises=RuntimeError(
                "Error in ponytail initiation might be cause by several reasons"
            ),
        )
        with self.assertRaises(ElementNotActionable) as ctx:
            self.atspi.Element(node).click()
        self.assertEqual(ctx.exception.role, "push button")
        self.assertEqual(ctx.exception.name, "5")
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

    def test_click_reraises_an_unrelated_runtime_error(self):
        # Only the ponytail failure is worked around -- anything else out of
        # dogtail's click() is a real error and must not be swallowed.
        node = FakeNode(
            name="5",
            role="push button",
            actions={"SetFocus": {}},
            click_raises=RuntimeError("some other failure"),
        )
        with self.assertRaises(RuntimeError):
            self.atspi.Element(node).click()
        self.assertEqual(node.actions_performed, [])

    def _ponytail_less_node(self):
        return FakeNode(
            name="Open",
            role="menu item",
            actions={"ShowMenu": {}},
            click_raises=RuntimeError(
                "Error in ponytail initiation might be caused by several reasons"
            ),
        )

    def _pointer_session(self, extents=(10, 10, 60, 20)):
        session = mock.Mock(click_via="auto")
        session.supports.return_value = True
        session.extents.return_value = extents
        return session

    def test_click_without_ponytail_takes_the_session_s_own_pointer(self):
        # Measured live on GhostBSD/X11 replaying a recording: dogtail refused
        # for want of gnome-ponytail-daemon while the session's x11 pointer
        # had just served thousands of gui.click() calls. The second rung is
        # that pointer, aimed by the session's own extents.
        node = self._ponytail_less_node()
        session = self._pointer_session()
        element = self.atspi.Element(node, session)
        element.click()
        session.click_element.assert_called_once_with(element)
        self.assertEqual(node.actions_performed, [])

    def test_click_without_a_pointer_in_the_session_refuses_up_front(self):
        # No second rung to try, so no extents lookup is paid for either, and
        # dogtail's account of the first rung is the cause.
        node = self._ponytail_less_node()
        session = self._pointer_session()
        session.supports.return_value = False
        with self.assertRaises(ElementNotActionable) as ctx:
            self.atspi.Element(node, session).click()
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)
        session.extents.assert_not_called()
        session.click_element.assert_not_called()

    def test_click_with_no_rectangle_chains_dogtail_s_refusal_underneath(self):
        # The refusal says what the session could not do; the cause names
        # the rung this desktop lacks. Without the chain a ponytail-less
        # desktop reads as "no session".
        node = self._ponytail_less_node()
        session = self._pointer_session(extents=None)
        with self.assertRaises(ElementNotActionable) as ctx:
            self.atspi.Element(node, session).click()
        self.assertIn("no rectangle", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)
        session.click_element.assert_not_called()

    def test_a_hang_in_the_pointer_rung_stays_a_hang(self):
        # The extents lookup is the very getExtents call measured hanging on
        # GhostBSD. A hang is not "cannot be clicked" and must not be
        # reported as one.
        node = self._ponytail_less_node()
        session = self._pointer_session()
        session.extents.side_effect = pyguitest.BackendCallTimeout("getExtents", 5)
        with self.assertRaises(pyguitest.BackendCallTimeout):
            self.atspi.Element(node, session).click()

    def test_click_falls_back_when_the_element_has_no_coordinates(self):
        # AT-SPI answers INT_MIN for x and y when a component is not showing,
        # and dogtail's own click() injects at that rectangle, so it raises
        # `ValueError: ... negative coordinates: (-2147483647, -2147483647)`
        # out of rawinput.py before AT-SPI is asked anything. Reached only
        # where no action was published -- a menu item that offers one, the
        # live case this was found from, never reaches dogtail's click() at
        # all any more; see test_click_prefers_a_published_action.
        node = FakeNode(
            name="Gamma",
            role="menu item",
            actions={"ShowMenu": {}},
            click_raises=ValueError(
                "Attempting to generate a mouse event at negative coordinates: "
                "(-2147483647,-2147483647)"
            ),
        )
        with self.assertRaises(ElementNotActionable) as ctx:
            self.atspi.Element(node).click()
        self.assertIsInstance(ctx.exception.__cause__, ValueError)

    def test_click_reraises_a_value_error_that_is_not_about_coordinates(self):
        # Only the missing-route failures are worked around. A ValueError out
        # of dogtail's click() for any other reason is a real error.
        node = FakeNode(
            name="Gamma",
            role="menu item",
            actions={"ShowMenu": {}},
            click_raises=ValueError("something else entirely"),
        )
        with self.assertRaises(ValueError):
            self.atspi.Element(node).click()
        self.assertEqual(node.actions_performed, [])

    def test_click_reraises_a_value_error_that_merely_mentions_coordinates(self):
        # Only dogtail's own "negative coordinates" refusal means "no usable
        # position". Any other ValueError is a real error, even one whose
        # message happens to use the word.
        node = FakeNode(
            name="Gamma",
            role="menu item",
            actions={"ShowMenu": {}},
            click_raises=ValueError("coordinates must be integers"),
        )
        with self.assertRaises(ValueError):
            self.atspi.Element(node).click()

    def test_click_via_action_only_uses_the_action(self):
        node = FakeNode(name="Enabled", role="check box", actions={"Press": {}})
        self.atspi.Element(node).click(via="action")
        self.assertEqual(node.actions_performed, ["Press"])
        self.assertFalse(node.clicked)

    def test_click_via_action_with_no_action_is_a_typed_refusal(self):
        # via="action" forbids the pointer fallback, so an element that
        # publishes no click/press/toggle/show-menu action raises rather than
        # being clicked by coordinate.
        node = FakeNode(name="Label", role="label")
        with self.assertRaises(ElementNotActionable) as ctx:
            self.atspi.Element(node).click(via="action")
        self.assertEqual(ctx.exception.role, "label")
        self.assertFalse(node.clicked)

    def test_click_via_pointer_skips_the_published_action(self):
        # via="pointer" is the user pathway only: even an element that
        # publishes a click action is clicked by coordinate instead.
        node = FakeNode(name="Enabled", role="check box", actions={"Press": {}})
        self.atspi.Element(node).click(via="pointer")
        self.assertEqual(node.actions_performed, [])
        self.assertTrue(node.clicked)

    def test_double_click_via_action_is_a_refusal(self):
        # No toolkit publishes a double-click action, so via="action" is an
        # honest refusal instead of the silent pointer click that the default
        # performs.
        node = FakeNode(name="OK", role="push button", actions={"Press": {}})
        with self.assertRaises(ElementNotActionable):
            self.atspi.Element(node).double_click(via="action")

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

    def test_role_reads_back_the_canonical_spelling_even_when_reported_as_the_new_one(
        self,
    ):
        # Confirmed live: a driver enumerating controls via one combined
        # find_elements(role=ACTABLE_ROLES) call, then reading element.role
        # back to rebuild its own (name, role) key, saw every push button
        # come back "button" on an at-spi2 that uses the new spelling --
        # matching none of its own (name, "push button") records. The old
        # per-role loop never hit this because it reused the role string it
        # searched for, not the element's own report.
        self.button.roleName = "button"
        gui = self.backend()
        found = gui.find_element(role="push button", name="OK")
        self.assertEqual(found.role, "push button")

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

    def test_choose_sets_the_text_child_on_an_editable_combo_box(self):
        # Qt publishes an editable combo's entry as a `text` child, and that
        # is the only selection path: the popup items expose no `click`
        # action and the box implements neither Selection nor Value, so the
        # combovalue fallback has nothing to click. Writing the entry is what
        # a person does, and it lands the same way.
        entry = FakeNode(role="text")
        combo = FakeNode(role="combo box", children=[FakeNode(role="list"), entry])
        self.atspi.Element(combo).choose("Sweden")
        self.assertEqual(entry.text, "Sweden")

    def test_choose_falls_back_to_combovalue_without_a_text_child(self):
        # A non-editable combo has no entry, so dogtail's combovalue -- which
        # finds the menu item and clicks it -- is the path that still works
        # where the toolkit publishes menu items with a `click` action.
        combo = FakeNode(role="combo box", children=[FakeNode(role="list")])
        self.atspi.Element(combo).choose("Pro")
        self.assertEqual(combo.combovalue, "Pro")

    def test_an_option_that_is_not_there_raises_this_packages_own_error(self):
        # dogtail's combovalue setter raises its own SearchError, which is not
        # a PyGUITestError -- so this one public call raised a catchable error
        # on Windows and macOS (both ElementNotFound) and an uncatchable
        # dogtail internal on Linux. Seen live choosing a name that was not
        # among a GTK combo's items.
        class _Refusing(FakeNode):
            @property
            def combovalue(self):
                return None

            @combovalue.setter
            def combovalue(self, value):
                raise _DogtailSearchError(f"No match for {value!r}")

        combo = _Refusing(role="combo box", children=[FakeNode(role="list")])
        with self.assertRaises(ElementNotFound) as caught:
            self.atspi.Element(combo).choose("Nope")
        self.assertIn("Nope", str(caught.exception))
        # Chained, so dogtail's own traceback is still reachable.
        self.assertIsInstance(caught.exception.__cause__, _DogtailSearchError)

    def test_an_option_that_is_there_keeps_the_failure_that_was_raised(self):
        # dogtail's combovalue setter finds the item and *then* clicks it, so
        # a failure from it is not necessarily a missing option. Reporting
        # "no option named Beta" for an option plainly listed beside it sent
        # the reader to check their own string instead of the widget that
        # refused the click.
        class _Refusing(FakeNode):
            @property
            def combovalue(self):
                return None

            @combovalue.setter
            def combovalue(self, value):
                raise _DogtailSearchError("the item refused its click action")

        combo = _Refusing(
            role="combo box",
            children=[
                FakeNode(name="Alpha", role="menu item"),
                FakeNode(name="Beta", role="menu item"),
            ],
        )
        with self.assertRaises(_DogtailSearchError):
            self.atspi.Element(combo).choose("Beta")

    def test_the_refusal_names_what_the_control_does_offer(self):
        # The method's documented advice is "when the text does not match,
        # options() is what to print" -- so the error prints it rather than
        # leaving the caller to.
        class _Refusing(FakeNode):
            @property
            def combovalue(self):
                return None

            @combovalue.setter
            def combovalue(self, value):
                raise _DogtailSearchError("no match")

        combo = _Refusing(
            role="combo box",
            children=[
                FakeNode(name="Alpha", role="menu item"),
                FakeNode(name="Beta", role="menu item"),
            ],
        )
        with self.assertRaises(ElementNotFound) as caught:
            self.atspi.Element(combo).choose("Gamma")
        self.assertIn("Alpha", str(caught.exception))
        self.assertIn("Beta", str(caught.exception))


class TestWaylandPlacement(AtspiTestCase):
    """Placing a WINDOW-coordinate rectangle: the decoration arithmetic.

    Every number here was measured live before the rule that reproduces
    it was written -- see `_client_inset` for the four cases and where
    each was seen. The fixture tree is built per test rather than taken
    from `build_tree`, because what is under test is the *shape* of a
    toplevel: how big the toolkit says its frame is against how big the
    compositor says the window is, and where the children start.
    """

    def install(self, toplevel, pid=4242):
        """Make `toplevel` the one window of the one application."""
        toplevel._pid = pid
        app = FakeNode("census", "application", [toplevel])
        root = FakeNode("desktop", "desktop frame", [app])
        self.atspi_tree_root = root
        sys.modules["dogtail.tree"].root = root

    def placed(self, source, name="OK"):
        gui = self.backend(SessionType.WAYLAND)
        gui.set_window_geometry_source(source)
        button = gui.find_element(name=name)
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            return gui.extents(button)

    def test_a_client_side_shadow_is_read_off_the_children(self):
        # GNOME, GTK3, Adwaita: the toolkit's frame is 472x289 for a window
        # the compositor places as 420x237 at (750, 438), the 52px being
        # the drop shadow GTK draws around a CSD window; the titlebar
        # starts at (26, 23) and the content below it at (34, 68). The
        # button at (34, 93) in the frame was measured at (758, 508) on
        # screen -- and 750 - 26 + 34 = 758, 438 - 23 + 93 = 508.
        button = FakeNode("OK", "push button", position=(34, 93), size=(80, 30))
        titlebar = FakeNode("", "panel", position=(26, 23), size=(420, 37))
        content = FakeNode("", "panel", [button], position=(34, 68), size=(404, 150))
        frame = FakeNode("Census", "frame", [titlebar, content], (0, 0), (472, 289))
        self.install(frame)
        source = _FakeWindowGeometrySource(4242, (750, 438), size=(420, 237))
        self.assertEqual(self.placed(source), (758, 508, 80, 30))

    def test_a_server_side_border_is_split_and_the_rest_is_titlebar(self):
        # GNOME, Qt: the compositor's window is 422x240 for a 420x200
        # frame -- Mutter's own titlebar and 1px border around a client
        # that draws neither. The border splits 1/1 between the sides and
        # the remaining 39 is the titlebar, so the client's origin sits at
        # (430, 335) inside the (429, 296) window; the button at (9, 165)
        # was measured at (439, 500).
        button = FakeNode("OK", "push button", position=(9, 165), size=(80, 30))
        frame = FakeNode("Census", "frame", [button], (0, 0), (420, 200))
        self.install(frame)
        source = _FakeWindowGeometrySource(4242, (429, 296), size=(422, 240))
        self.assertEqual(self.placed(source), (439, 500, 80, 30))

    def test_a_titlebar_alone_insets_only_the_vertical_axis(self):
        # KDE Breeze, every toolkit: 420x228 placed for a 420x200 frame,
        # no side border at all, so the full 28 is titlebar and the
        # horizontal axis is left exactly where the compositor put it.
        # GTK3's button at (8, 33) was measured at (438, 324).
        button = FakeNode("OK", "push button", position=(8, 33), size=(80, 30))
        frame = FakeNode("Census", "frame", [button], (0, 0), (420, 200))
        self.install(frame)
        source = _FakeWindowGeometrySource(4242, (430, 263), size=(420, 228))
        self.assertEqual(self.placed(source), (438, 324, 80, 30))

    def test_a_window_the_compositor_and_toolkit_agree_on_is_added_as_is(self):
        # GTK4 on GNOME: 420x200 on both sides, every element at (0, 0) on
        # screen and at its real offset in the window -- nothing to inset,
        # the window's position is the whole correction.
        button = FakeNode("OK", "push button", position=(8, 71), size=(80, 30))
        frame = FakeNode("Census", "frame", [button], (0, 0), (420, 200))
        self.install(frame)
        source = _FakeWindowGeometrySource(4242, (750, 456), size=(420, 200))
        self.assertEqual(self.placed(source), (758, 527, 80, 30))

    def test_a_shadow_margin_is_clamped_to_half_the_size_difference(self):
        # The children start 100px in, but the frame is only 60px bigger
        # than the window on each axis: a margin wider than the shadow's
        # near side is not a shadow, it is content laid out with padding.
        # A shadow is symmetric or offset downward, never toward the
        # top-left, so the near side is at most half the difference -- 30.
        button = FakeNode("OK", "push button", position=(100, 100), size=(80, 30))
        frame = FakeNode("Census", "frame", [button], (0, 0), (480, 260))
        self.install(frame)
        source = _FakeWindowGeometrySource(4242, (500, 400), size=(420, 200))
        self.assertEqual(self.placed(source), (570, 470, 80, 30))

    def test_an_assistant_whose_header_has_no_rectangle_is_bounded_by_half(self):
        # KaOS, niri, GTK 3.24: a GtkAssistant's frame is 335x209 for the
        # 283x157 window niri places at (82, 322) -- the 52px CSD shadow
        # again -- but its header panel reports no rectangle, so the first
        # child with one is the page's own content at (46, 80). Taken as
        # the margin that put the button 30px too high (measured by a
        # click sweep: placed centre (203, 367), real (203, 397)); bounded
        # by half the difference the button lands at (102, 376), 4px from
        # where Adwaita's real (26, 23) margin would put it.
        button = FakeNode(
            "Assistant Button", "push button", position=(46, 80), size=(243, 34)
        )
        page = FakeNode("", "filler", [button], position=(46, 80), size=(243, 34))
        actions = FakeNode("", "filler", position=(141, 140), size=(162, 34))
        header = FakeNode("", "panel", component=False)
        frame = FakeNode("", "frame", [page, actions, header], (0, 0), (335, 209))
        self.install(frame)
        source = _FakeWindowGeometrySource(4242, (82, 322), size=(283, 157))
        self.assertEqual(
            self.placed(source, name="Assistant Button"), (102, 376, 243, 34)
        )

    def test_a_shadow_no_child_measures_is_taken_as_symmetric(self):
        # The frame's direct child has no rectangle to read a margin off
        # (no Component interface), so the 60px difference is split
        # evenly: the origin is 30 inside the compositor's corner.
        button = FakeNode("OK", "push button", position=(100, 100), size=(80, 30))
        holder = FakeNode("", "panel", [button], component=False)
        frame = FakeNode("Census", "frame", [holder], (0, 0), (480, 260))
        self.install(frame)
        source = _FakeWindowGeometrySource(4242, (500, 400), size=(420, 200))
        self.assertEqual(self.placed(source), (570, 470, 80, 30))

    def test_a_toolkit_told_about_its_titlebar_needs_no_inset(self):
        # GTK3 under X11 (Mint, Cinnamon): the frame reports itself as
        # (0, -37, 420, 237) in WINDOW coordinates against a 420x200 client
        # window the x11 backend lists at (40, 97) -- the toolkit was told
        # about the 37px titlebar and put its WINDOW origin on the client's
        # corner already. The size difference is the titlebar, not a
        # shadow, and the origin says so; a shadow probe here would have
        # read the children's padding as an inset and shifted everything.
        gui = self.backend(SessionType.WAYLAND)
        frame = FakeNode("Census", "frame", [], (40, 97), (420, 237))
        self.assertEqual(
            gui._client_inset(frame, (0, -37, 420, 237), (40, 97, 420, 200)), (0, 0)
        )

    def test_a_dialog_whose_role_is_not_a_window_role_is_still_a_window(self):
        # AT-SPI hangs every toplevel straight off its application, so a
        # control under an application's direct child has that child for
        # its window whatever role the toolkit gave it -- a GTK3 `file
        # chooser` and an `alert` before they were in WINDOW_ROLES, or a
        # role no toolkit has published yet.
        button = FakeNode("OK", "push button", position=(8, 33), size=(80, 30))
        toplevel = FakeNode("Census", "custom toplevel", [button], (0, 0), (420, 200))
        self.install(toplevel)
        source = _FakeWindowGeometrySource(4242, (100, 100), size=(420, 200))
        self.assertEqual(self.placed(source), (108, 133, 80, 30))

    def test_a_file_chooser_is_placed_by_its_own_listing_not_the_main_window(self):
        # KDE, GTK3: a Save dialog 1186x963 in the toolkit's reckoning
        # (CSD shadow again, content at (45, 45)) over a main window of
        # 420x200; the x11 backend lists both under one pid, main window
        # first. The chooser's 1096x873 listing at (92, 0) is the only one
        # within a decoration of its size, so its Cancel button at
        # (45, 96) lands at (92, 51) rather than somewhere in the main
        # window.
        ok = FakeNode("OK", "push button", position=(8, 33), size=(80, 30))
        main = FakeNode("Census", "frame", [ok], (0, 0), (420, 200))
        cancel = FakeNode("Cancel", "push button", position=(45, 96), size=(80, 30))
        header = FakeNode("", "panel", position=(45, 45), size=(1096, 40))
        chooser = FakeNode(
            "Save", "file chooser", [header, cancel], (0, 0), (1186, 963)
        )
        chooser._pid = 4242
        self.install(main)
        self.atspi_tree_root.children[0].children.append(chooser)
        chooser.parent = self.atspi_tree_root.children[0]
        source = _FakeManyWindowsGeometrySource(
            4242, [(430, 263), (92, 0)], sizes=[(420, 228), (1096, 873)]
        )
        self.assertEqual(self.placed(source, name="Cancel"), (92, 51, 80, 30))
        self.assertEqual(self.placed(source, name="OK"), (438, 324, 80, 30))

    def test_the_listing_closest_in_size_outranks_the_first_listed(self):
        # Two toplevels, one pid, no titles to go on: the one the frame's
        # size fits is the frame, wherever the source listed it.
        button = FakeNode("OK", "push button", position=(8, 33), size=(80, 30))
        frame = FakeNode("Census", "frame", [button], (0, 0), (420, 200))
        self.install(frame)
        source = _FakeManyWindowsGeometrySource(
            4242, [(300, 300), (100, 100)], sizes=[(300, 120), (420, 200)]
        )
        self.assertEqual(self.placed(source), (108, 133, 80, 30))

    def test_the_listing_with_the_frames_own_title_outranks_the_rest(self):
        # Measured on GNOME, KDE and COSMIC before the ranking existed: a
        # dialog's controls placed by the main window's position, hundreds
        # of pixels off. Same size on both listings here, so only the
        # title can tell them apart.
        self.frame._pid = 4242
        source = _FakeManyWindowsGeometrySource(
            4242, [(300, 300), (0, 32)], titles=["Preferences", "Document - Editor"]
        )
        self.assertEqual(self.placed(source), (10, 52, 80, 30))

    def test_a_nameless_frame_ranks_its_untitled_listing_over_the_main_window(self):
        # Debian GNOME, GTK3: a GtkAssistant the application never titled,
        # 461x185 in the toolkit's reckoning (Mutter's 52px CSD shadow
        # around a 409x133 window the compositor lists at (436, 332), title
        # ""), with the application's main window 'Census gtk3' listed
        # first as 420x237 at (430, 298). Both listings are within a
        # decoration of the frame's size, and the main window's misfit
        # (41 + 52) is the *smaller* one, so by size alone it won: the
        # button at (46, 90) was placed at (450, 440), from the main
        # window's corner, where it sat at (456, 399) -- 436 - 26 + 46 and
        # 332 - 23 + 90, the shadow margin read off the titlebar at
        # (26, 23). An empty name matches an empty title: the assistant's
        # own listing ranks first, as a named frame's titled one would.
        button = FakeNode("OK", "push button", position=(46, 90), size=(369, 34))
        titlebar = FakeNode("", "panel", position=(26, 23), size=(409, 47))
        frame = FakeNode("", "frame", [button, titlebar], (0, 0), (461, 185))
        self.install(frame)
        source = _FakeManyWindowsGeometrySource(
            4242,
            [(430, 298), (436, 332)],
            titles=["Census gtk3", ""],
            sizes=[(420, 237), (409, 133)],
        )
        self.assertEqual(self.placed(source), (456, 399, 369, 34))

    def test_a_listing_wider_but_shorter_than_the_frame_is_not_a_candidate(self):
        # The same assistant with its own listing missing -- as it was on
        # every X11 box while the x11 backend dropped untitled windows. The
        # main window is within `_MAX_DECORATION` on both axes, but 41
        # narrower and 52 taller than the frame: decoration makes a window
        # larger than its content on both axes or smaller on both, never
        # one of each, so this is another toplevel and not a place to put
        # the frame. Refused, rather than placed 41 pixels into the wrong
        # window.
        button = FakeNode("OK", "push button", position=(46, 90), size=(369, 34))
        titlebar = FakeNode("", "panel", position=(26, 23), size=(409, 47))
        frame = FakeNode("", "frame", [button, titlebar], (0, 0), (461, 185))
        self.install(frame)
        source = _FakeManyWindowsGeometrySource(
            4242, [(430, 298)], titles=["Census gtk3"], sizes=[(420, 237)]
        )
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.placed(source)
        self.assertIn("window-relative", str(caught.exception))

    def test_a_nameless_frame_matches_no_title_on_a_source_without_pids(self):
        # The fallback for a source that lists no pid at all (COSMIC's
        # toplevel protocol has none) still needs a title to go on: an
        # empty name must not match every untitled window on the desktop.
        # Refused, the same as any frame that source cannot identify.
        button = FakeNode("OK", "push button", position=(8, 33), size=(80, 30))
        frame = FakeNode("", "frame", [button], (0, 0), (420, 200))
        self.install(frame)
        source = _FakeTitleOnlyGeometrySource("", (100, 100), size=(420, 200))
        with self.assertRaises(CapabilityUnsupported):
            self.placed(source)

    def test_a_listing_of_an_implausible_size_is_not_a_place_to_put_it(self):
        # The source lists the pid, but only a window nowhere near the
        # frame's size -- another of the application's toplevels. Refused
        # with the Wayland reason, the same as if nothing were listed, not
        # placed into the wrong window.
        self.frame._pid = 4242
        source = _FakeWindowGeometrySource(4242, (0, 32), size=(1280, 768))
        with self.assertRaises(CapabilityUnsupported) as caught:
            self.placed(source)
        self.assertIn("window-relative", str(caught.exception))
        self.assertIn("is_window_viewable", str(caught.exception))

    def test_a_window_moved_between_calls_is_followed(self):
        self.frame._pid = 4242
        gui = self.backend(SessionType.WAYLAND)
        source = _FakeWindowGeometrySource(4242, (0, 32))
        gui.set_window_geometry_source(source)
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (10, 52, 80, 30))
            source._offset = (400, 300)
            self.assertEqual(gui.extents(button), (410, 320, 80, 30))

    def test_a_node_outside_any_application_has_no_window(self):
        gui = self.backend(SessionType.WAYLAND)
        root = sys.modules["dogtail.tree"].root
        self.assertIsNone(gui._window_frame(root))
        self.assertIsNone(gui._window_frame(root.children[0]))
        self.assertIs(gui._window_frame(self.button), self.frame)


class TestAppCoordsTrustworthy(AtspiTestCase):
    """A toolkit that does not know where its window is, among ones that do.

    GTK4, confirmed live on every desktop: every element it owns answers
    (0, 0) in screen coordinates, frame and deeply nested child alike, on
    X11 exactly as on Wayland. And any native Wayland client does the same
    in a session this backend classifies as XWayland (`DISPLAY` and
    `WAYLAND_DISPLAY` both set -- KDE Plasma, a GNOME terminal), where Qt
    and GTK3 X11 clients in the same session report real positions.
    `_screen_coords_trustworthy` alone says "yes" to all of them, because
    the session is not Wayland; this is the narrower, per-application check
    that still catches them -- and what it does with a catch is not refuse
    the element but place it, the same way a Wayland session places every
    element: by its own WINDOW-coordinate rectangle, which every toolkit
    measured fills in truthfully, plus its toplevel's real position from
    the sibling backend.

    The fixture frame sits at (0, 0) and, unless a test says otherwise,
    answers the same rectangle in WINDOW coordinates: the signature of a
    toolkit never told where it is. A toolkit that *has* been told puts its
    WINDOW origin on the client window -- `window_position=(0, -37)` is
    GTK3 under X11 with a 37px titlebar, measured -- and is believed on
    that alone, without the sibling backend being asked. The sibling's
    listing decides only the remaining shape, a frame answering one and the
    same rectangle for both coordinate types *away* from the origin, and
    the tests of that path set `position` and `window_position` alike.
    """

    def test_a_toolkit_that_knows_its_screen_position_is_trusted(self):
        # The frame's WINDOW rectangle differs from its screen one -- a
        # toolkit that has been told where it is. extents() answers the
        # raw screen rectangle, unmodified, and the verdict is remembered;
        # the sibling backend's listing is never consulted (it lists the
        # window at the far side of the screen here, and that changes
        # nothing).
        self.frame._pid = 4242
        self.frame.window_position = (0, -37)
        gui = self.backend(SessionType.X11)
        gui.set_window_geometry_source(_FakeWindowGeometrySource(4242, (900, 700)))
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (10, 20, 80, 30))
        self.assertEqual(gui._app_coords_verified, {4242: True})

    def test_a_small_decoration_offset_is_within_tolerance(self):
        # A window manager's own titlebar/border can genuinely separate
        # what AT-SPI calls the frame from what the WM calls the window by
        # a few tens of pixels -- real, and not the bug this is for. The
        # frame answers the same rectangle for both coordinate types, off
        # the origin, so the listing is what decides.
        self.frame._pid = 4242
        self.frame.position = (5, 5)
        self.frame.window_position = (5, 5)
        gui = self.backend(SessionType.X11)
        gui.set_window_geometry_source(_FakeWindowGeometrySource(4242, (3, 28)))
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (10, 20, 80, 30))
        self.assertEqual(gui._app_coords_verified, {4242: True})

    def test_a_toolkit_that_knows_nothing_of_the_screen_is_placed(self):
        # The measured GTK4 shape: the frame answers (0, 0) in both
        # coordinate spaces, so the verdict needs no listing to compare
        # against -- and the geometry source says the window is really at
        # (200, 150). The button's own rectangle is good *within its
        # window*, so the answer is that rectangle placed there, not a
        # refusal.
        self.frame._pid = 4242
        gui = self.backend(SessionType.X11)
        gui.set_window_geometry_source(_FakeWindowGeometrySource(4242, (200, 150)))
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (210, 170, 80, 30))
        self.assertEqual(gui._app_coords_verified, {4242: False})

    def test_a_window_genuinely_at_the_origin_is_answered_identically(self):
        # The one shape the (0, 0)-in-both-spaces rule cannot tell from
        # GTK4: an undecorated window really sitting at the screen's
        # corner, whose WINDOW and SCREEN answers coincide for that reason.
        # It is "placed" rather than trusted, and the placement is the same
        # number the raw answer would have been -- the sibling backend
        # confirms the window is at (0, 0), and that is what gets added.
        self.frame._pid = 4242
        gui = self.backend(SessionType.X11)
        gui.set_window_geometry_source(_FakeWindowGeometrySource(4242, (0, 0)))
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (10, 20, 80, 30))
        self.assertEqual(gui._app_coords_verified, {4242: False})

    def test_a_placed_application_with_no_plausible_window_is_refused(self):
        # Placed, but nothing to place against: the source lists a window
        # for the pid whose size is nowhere near the frame's own -- one of
        # the application's *other* toplevels -- so there is no position
        # to add. The element is refused with the reason, not answered
        # with the wrong window's corner, and not None either, which would
        # read as "the element has no rectangle".
        self.frame._pid = 4242
        gui = self.backend(SessionType.X11)
        gui.set_window_geometry_source(
            _FakeWindowGeometrySource(4242, (200, 150), size=(1280, 768))
        )
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            with self.assertRaises(CapabilityUnsupported) as caught:
                gui.extents(button)
        self.assertIn("do not agree", str(caught.exception))
        self.assertIn("plausible size", str(caught.exception))
        self.assertIn("accessible action", str(caught.exception))
        self.assertEqual(gui._app_coords_verified, {4242: False})

    def test_no_geometry_source_leaves_x11_exactly_as_before(self):
        # The common case: no sibling backend offers WINDOW_GEOMETRY at
        # all, so there is nothing to check against, and every existing
        # X11 behaviour (the whole rest of this test file) must be
        # completely unaffected by this feature's existence.
        self.frame._pid = 4242
        gui = self.backend(SessionType.X11)
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (10, 20, 80, 30))

    def test_the_sibling_backend_is_asked_only_once_per_application(self):
        # The shape that needs the listing: one rectangle for both
        # coordinate types, off the origin.
        self.frame._pid = 4242
        self.frame.position = (5, 5)
        self.frame.window_position = (5, 5)
        gui = self.backend(SessionType.X11)
        source = _FakeWindowGeometrySource(4242, (0, 0))
        calls = []
        real_windows = source.windows

        def counted_windows():
            calls.append(1)
            return real_windows()

        source.windows = counted_windows
        gui.set_window_geometry_source(source)
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            gui.extents(button)
            gui.extents(self.atspi.Element(self.frame))
            gui.extents(button)
        self.assertEqual(
            len(calls),
            1,
            "the verdict is a fact about the application and must be "
            "cached, not re-verified on every element inside it",
        )

    def test_a_placed_application_is_placed_afresh_on_every_call(self):
        # The verdict is a fact about the application; the window's
        # position is not, it moves. A placed application asks the sibling
        # backend where its window is on every extents(), so a window
        # dragged between two calls is followed rather than remembered.
        self.frame._pid = 4242
        gui = self.backend(SessionType.X11)
        source = _FakeWindowGeometrySource(4242, (200, 150))
        gui.set_window_geometry_source(source)
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (210, 170, 80, 30))
            source._offset = (300, 350)
            self.assertEqual(gui.extents(button), (310, 370, 80, 30))
        self.assertEqual(gui._app_coords_verified, {4242: False})

    def test_a_dialog_is_checked_against_its_own_window_not_the_first_listed(self):
        # An application with several toplevels is listed several times by
        # the sibling backend, all under one pid, and pid cannot say which
        # of them the frame being checked is. The source here lists the
        # main window first, at (300, 300); the fixture frame -- standing
        # in for a dialog genuinely at (5, 5), answering that for both
        # coordinate types so the listing is what decides -- must match the
        # second listing rather than be condemned by the first, or every
        # dialog that opens more than 50px from its main window would mark
        # its whole application untrustworthy for the rest of the session.
        self.frame._pid = 4242
        self.frame.position = (5, 5)
        self.frame.window_position = (5, 5)
        gui = self.backend(SessionType.X11)
        gui.set_window_geometry_source(
            _FakeManyWindowsGeometrySource(4242, [(300, 300), (0, 0)])
        )
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (10, 20, 80, 30))
        self.assertEqual(gui._app_coords_verified, {4242: True})

    def test_a_screen_answer_matching_no_listed_window_is_placed_by_the_best(self):
        # Not a measured toolkit -- every real one caught so far answers
        # (0, 0) and is caught by the rule above before any listing is
        # read -- but the check's own contract: a frame answering one and
        # the same rectangle for both coordinate types, off the origin and
        # near none of its application's listed windows, is not believed,
        # and its WINDOW rectangle (good: the toolkit's own) is placed by
        # the best listing. With no title to go on and every listing the
        # frame's size, best is the first; the frame's own (5, 5) is where
        # its content starts inside that listing.
        self.frame._pid = 4242
        self.frame.position = (5, 5)
        self.frame.window_position = (5, 5)
        gui = self.backend(SessionType.X11)
        gui.set_window_geometry_source(
            _FakeManyWindowsGeometrySource(4242, [(300, 300), (120, 640)])
        )
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (305, 315, 80, 30))
        self.assertEqual(gui._app_coords_verified, {4242: False})

    def test_a_toolkit_that_tells_the_two_coordinate_types_apart_is_believed(self):
        # GhostBSD, marco, GTK3: an untitled GtkAssistant's frame at
        # (379, 306) on screen and (0, -37) in WINDOW coordinates, with the
        # compositor's listing holding only the application's main window
        # -- the assistant itself was not listed, the x11 backend then
        # dropping untitled windows. The main window was plausibly sized
        # (within a decoration of the assistant's on both axes, in the same
        # direction) and 337 pixels away, so the listing check condemned
        # the application, cached the verdict, and placed the assistant's
        # button by the main window: (139, 113) for a button at (401, 359).
        # A toolkit can only answer the two types differently by
        # subtracting an origin it was told, so the listing is not consulted
        # at all: the screen answer stands, and the verdict is remembered.
        self.frame._pid = 4242
        self.frame.position = (379, 306)
        self.frame.size = (266, 159)
        self.frame.window_position = (0, -37)
        self.button.position = (401, 359)
        gui = self.backend(SessionType.X11)
        gui.set_window_geometry_source(
            _FakeWindowGeometrySource(4242, (42, 93), size=(420, 200))
        )
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (401, 359, 80, 30))
        self.assertEqual(gui._app_coords_verified, {4242: True})

    def test_the_listing_with_the_frames_own_title_outranks_the_rest(self):
        # Several toplevels, one pid, every one the frame's size: the
        # listing whose title is the frame's own name is the one the frame
        # is, wherever the source put it in its order.
        self.frame._pid = 4242
        gui = self.backend(SessionType.X11)
        gui.set_window_geometry_source(
            _FakeManyWindowsGeometrySource(
                4242,
                [(300, 300), (200, 150)],
                titles=["Preferences", "Document - Editor"],
            )
        )
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (210, 170, 80, 30))

    def test_an_application_the_sibling_does_not_list_yet_is_not_cached(self):
        # Trusted for now, not for the session: a window the sibling
        # backend has not listed yet (still being mapped when an
        # `element_at` sweep passed over it) offers nothing to compare
        # against. Caching that as a verdict would let one sweep's timing
        # decide every later extents() on the application -- so the moment
        # the sibling does list it, at a position the frame's own answer
        # disagrees with, the disagreement must still be caught, and from
        # then on the element is placed by the listing (the frame's own
        # (5, 5) is where its content starts inside the listed window, so
        # the listing's (200, 150) places the button at (205, 165)). The
        # frame answers one rectangle for both coordinate types, off the
        # origin: the one shape whose verdict needs the listing at all.
        self.frame._pid = 4242
        self.frame.position = (5, 5)
        self.frame.window_position = (5, 5)
        gui = self.backend(SessionType.X11)
        source = _FakeWindowGeometrySource(0, (200, 150))
        gui.set_window_geometry_source(source)
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.extents(button), (10, 20, 80, 30))
            self.assertEqual(gui._app_coords_verified, {})
            source._pid = 4242
            self.assertEqual(gui.extents(button), (205, 165, 80, 30))
        self.assertEqual(gui._app_coords_verified, {4242: False})

    def test_element_at_asks_an_untrusted_toplevel_in_its_own_coordinates(self):
        # The hit test asks every application; one whose screen rectangles
        # cannot be believed is not passed over but asked in WINDOW
        # coordinates, with the point moved into them by the window's real
        # position. The GTK4 shape throughout: every node answers (0, 0)
        # on screen and its real offset within the window. Measured for
        # the caller that needs it -- the recorder's X11 capture resolving
        # a click on a GTK4 window, which before this found nothing.
        self.frame._pid = 4242
        self.button.position = (0, 0)
        self.button.window_position = (10, 20)
        cancel = self.frame.children[1]
        cancel.position = (0, 0)
        cancel.window_position = (110, 20)
        gui = self.backend(SessionType.X11)
        gui.set_window_geometry_source(_FakeWindowGeometrySource(4242, (200, 150)))
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.element_at(220, 180).name, "OK")
            self.assertEqual(gui.element_at(320, 180).name, "Cancel")
            # The point the toolkit *claims* every element is at, where
            # nothing of this window really is.
            self.assertIsNone(gui.element_at(20, 30))

    def test_element_at_leaves_out_a_toplevel_the_source_cannot_place(self):
        # Untrusted and unplaceable -- the source lists nothing of this
        # window's size -- so the toplevel's (0, 0) frame must not claim
        # the point and answer it with a widget that is nowhere near; the
        # point stays unanswered rather than the whole call raising for a
        # reason unrelated to what was asked about.
        self.frame._pid = 4242
        gui = self.backend(SessionType.X11)
        gui.set_window_geometry_source(
            _FakeWindowGeometrySource(4242, (200, 150), size=(1280, 768))
        )
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertIsNone(gui.element_at(20, 30))
            self.assertIsNone(gui.element_at(220, 180))

    def test_wayland_is_unaffected_by_this_check_entirely(self):
        # `_app_coords_trustworthy` must never even run on Wayland: that
        # session places every element without asking, and this check's
        # own tolerance-based comparison has no business deciding anything
        # there.
        self.frame._pid = 4242
        gui = self.backend(SessionType.WAYLAND)
        gui.set_window_geometry_source(_FakeWindowGeometrySource(4242, (0, 32)))
        button = gui.find_element(name="OK")
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            # Unaffected means: still the Wayland placement, not a
            # CapabilityUnsupported from the check and not the raw,
            # uncorrected (10, 20, 80, 30) either.
            self.assertEqual(gui.extents(button), (10, 52, 80, 30))
        self.assertEqual(gui._app_coords_verified, {})


class _DogtailSearchError(Exception):
    """Stands in for dogtail's SearchError, which is a plain Exception.

    Deliberately not imported from dogtail: these tests run against the fake
    tree with no dogtail installed, and what matters is only that the error is
    foreign to this package's taxonomy -- which any plain Exception models.
    """


class TestElementCheckable(AtspiTestCase):
    """checkable: the CHECKABLE state, or the role where there is none.

    GTK 3 publishes no CHECKABLE state at all. Measured on GTK 3.24.52 with
    at-spi2-core 2.62.1: a Gtk.CheckButton's whole state set is ENABLED,
    FOCUSABLE, SENSITIVE, SHOWING, VISIBLE, so this read False for every check
    box and radio button on the toolkit most Linux applications use, while
    `checked` tracked them correctly -- making the advice `checked` and
    `Session.expect_checked` both give ("read checkable first") skip working
    controls.
    """

    def test_the_published_state_is_believed_when_there_is_one(self):
        # On a role the fallback list does not name, so this can only be the
        # state talking: a toolkit that does publish CHECKABLE still decides.
        node = FakeNode(role="push button")
        node.checkable = True
        self.assertTrue(self.atspi.Element(node).checkable)

    def test_a_role_that_carries_a_check_state_falls_back_to_its_role(self):
        for role in (
            "check box",
            "radio button",
            "toggle button",
            "check menu item",
            "radio menu item",
        ):
            with self.subTest(role=role):
                node = FakeNode(role=role)
                self.assertFalse(getattr(node, "checkable", False))
                self.assertTrue(self.atspi.Element(node).checkable)

    def test_a_role_that_carries_no_check_state_stays_false(self):
        # The fallback is a fixed list of roles, not "anything clickable": a
        # push button and a label must not start claiming a checked state.
        for role in ("push button", "label", "entry", "menu item", "frame"):
            with self.subTest(role=role):
                self.assertFalse(self.atspi.Element(FakeNode(role=role)).checkable)

    def test_the_fallback_grants_no_checked_value_of_its_own(self):
        # Widening `checkable` must not touch `checked`: the role fallback
        # says "this is the kind of control that has a tick", and the state
        # read stays the only thing that says whether it is ticked.
        node = FakeNode(role="check box")
        self.assertTrue(self.atspi.Element(node).checkable)
        self.assertIsNone(self.atspi.Element(node).checked)

    def test_a_dogtail_node_answers_a_bool_so_none_is_not_a_signal_here(self):
        # The pin that matters for expect_checked's caveat: dogtail's own
        # `Node.checked` reads the state set and so answers a real bool for
        # *every* node, False when CHECKED is simply absent. None is only
        # what the absent-attribute default above produces, which no live
        # dogtail node reaches -- so on AT-SPI, unlike UIA and AX, `checked
        # is None` cannot tell "publishes no checked state" from "unchecked".
        # A test asserting checked=False can therefore pass against a
        # toolkit that never reported the state at all.
        node = FakeNode(role="check box")
        node.checked = False
        self.assertIs(self.atspi.Element(node).checked, False)
        node.checked = True
        self.assertIs(self.atspi.Element(node).checked, True)


class TestElementExpandCollapse(AtspiTestCase):
    """Opening and closing a tree item, through GTK's one toggling action.

    Measured live against a GTK3 GtkTreeView (see
    pyguitest-recorder/docs/developers/status.md): a collapsed row's state
    set holds EXPANDABLE alone and gains EXPANDED once opened, and the only
    published action is "expand or contract" -- one verb, not the separate
    expand/collapse this interface offers, which is exactly why `expand`
    and `collapse` have to check `expanded` before acting rather than just
    invoking the action.
    """

    def row(self, expanded=False, expandable=True, actions=None):
        states = []
        if expandable:
            states.append("EXPANDABLE")
        if expanded:
            states.append("EXPANDED")
        if actions is None:
            actions = {"expand or contract": ""}
        return self.atspi.Element(
            FakeNode(name="Documents", state_set=states, actions=actions)
        )

    def test_expandable_is_true_with_the_state(self):
        self.assertTrue(self.row(expandable=True).expandable)

    def test_expandable_is_false_without_the_state(self):
        self.assertFalse(self.row(expandable=False).expandable)

    def test_expanded_is_none_when_not_expandable(self):
        # Not merely "not True": a leaf row is not a closed disclosure
        # control, it is not one at all -- the same distinction `checked`
        # draws for an element with no Toggle pattern.
        self.assertIsNone(self.row(expandable=False).expanded)

    def test_expanded_is_false_for_a_collapsed_row(self):
        self.assertFalse(self.row(expanded=False).expanded)

    def test_expanded_is_true_for_an_open_row(self):
        self.assertTrue(self.row(expanded=True).expanded)

    def test_a_row_that_died_reads_as_a_leaf_rather_than_raising(self):
        # tree_items() reads expandable/expanded off every row it walks; a
        # row closing mid-walk must not end the whole walk with a bus error.
        element = self.atspi.Element(_ReapedStates(name="Gone"))
        self.assertFalse(element.expandable)
        self.assertIsNone(element.expanded)

    def test_expand_invokes_the_toggling_action(self):
        element = self.row(expanded=False)
        element.expand()
        self.assertEqual(element.node.actions_performed, ["expand or contract"])

    def test_expand_is_a_no_op_once_already_expanded(self):
        # The action toggles -- calling it on an open row would close it.
        element = self.row(expanded=True)
        element.expand()
        self.assertEqual(element.node.actions_performed, [])

    def test_collapse_invokes_the_toggling_action(self):
        element = self.row(expanded=True)
        element.collapse()
        self.assertEqual(element.node.actions_performed, ["expand or contract"])

    def test_collapse_is_a_no_op_once_already_collapsed(self):
        element = self.row(expanded=False)
        element.collapse()
        self.assertEqual(element.node.actions_performed, [])

    def test_expand_raises_where_no_expand_action_is_published(self):
        # A row with no matching action at all -- not this GTK's shape, but
        # the typed refusal has to hold for whatever toolkit lacks it.
        element = self.row(expanded=False, actions={"click": ""})
        with self.assertRaises(ElementNotActionable) as caught:
            element.expand()
        self.assertIn("Documents", str(caught.exception))


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

    def test_element_at_works_without_pyatspi(self):
        """`element_at` wanted one constant from pyatspi and nothing else.

        The same stacked fault `active_window` had, in a second method:
        a pyatspi whose own `import` fails -- confirmed live on Debian 13
        and Pop!_OS/COSMIC, where it raises AttributeError rather than
        ImportError -- made this refuse on a desktop whose accessibility
        stack was otherwise fine. `extents` had already routed around it
        with `Atspi.CoordType.SCREEN`; this had not. Patched to None rather
        than to a fake, so the fallback is what answers.
        """
        gui = self.gui()
        # The GI fallback is patched rather than executed. Importing a real
        # typelib into this process is a global side effect, and doing it
        # here was measured breaking a *later* `from gi.repository import
        # Gio` in the portal suite with "metaclass conflict", which that
        # backend then reported as "PyGObject is not installed". What this
        # test needs to know is only that the fallback is reached and its
        # value used when pyatspi is unavailable.
        with (
            mock.patch.dict(sys.modules, {"pyatspi": None}),
            mock.patch.object(
                self.atspi, "_gi_screen_coords", return_value=_FAKE_SCREEN_COORDS
            ) as coords,
        ):
            self.assertEqual(gui.element_at(20, 30).name, "OK")
            self.assertEqual(gui.element_at(120, 30).name, "Cancel")
        self.assertTrue(coords.called, "the pyatspi-free fallback was not used")

    def test_element_at_descends_past_the_frame_to_a_leaf(self):
        # The frame claims the point first; the answer must be the deepest
        # element that claims it, not the first.
        label = FakeNode("Save", "label", position=(20, 25), size=(20, 10))
        self.button.children.append(label)
        label.parent = self.button
        gui = self.gui()
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.element_at(25, 30).name, "Save")

    def test_element_at_walks_past_a_page_tab_that_does_not_nest(self):
        """A notebook's page tab is not where its children are.

        Measured on a GTK3 notebook: the page tab reports (133, 141, 54, 30),
        its content filler (113, 175, 494, 312) and the `Save` button inside
        that (113, 217, 494, 34). Asked about the button's own centre, the
        `page tab list` above the tab -- which does contain the point --
        answers `getAccessibleAtPoint` with nothing at all, since the point is
        inside no tab's label. The descent dead-ended there, so `element_at`
        answered the tab list for every widget on the page instead of the
        widget. Found recording such a window through pyguitest-recorder,
        where every control on a tab page came out as a bare coordinate
        rather than `gui.button("Save")`.
        """
        save = FakeNode("Save", "push button", position=(113, 217), size=(494, 34))
        filler = FakeNode("", "filler", [save], (113, 175), (494, 312))
        tab = _PageTab("Page 1", "page tab", [filler], (133, 141), (54, 30))
        tab_list = FakeNode("", "page tab list", [tab], (113, 175), (494, 312))
        notebook = FakeNode("Notes", "frame", [tab_list], (100, 140), (520, 360))
        app = FakeNode("gedit", "application", [notebook])
        gui = self.gui()
        gui._tree.root = FakeNode("desktop", "desktop frame", [app])
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.element_at(360, 234).name, "Save")

    def test_the_page_tab_step_survives_a_child_it_cannot_ask(self):
        # VITAL -- keep this test. The page-tab step asks each child of a
        # dead-ended node directly; one child with no Component interface
        # (or gone mid-walk) used to raise out to `_hits_in`, which drops the
        # whole application -- so element_at answered None where it used to
        # answer the coarse tab list, a regression the notebook fix caused.
        save = FakeNode("Save", "push button", position=(113, 217), size=(494, 34))
        filler = FakeNode("", "filler", [save], (113, 175), (494, 312))
        tab = _PageTab("Page 1", "page tab", [filler], (133, 141), (54, 30))
        mute = FakeNode("", "page tab", component=False)
        tab_list = FakeNode("", "page tab list", [mute, tab], (113, 175), (494, 312))
        notebook = FakeNode("Notes", "frame", [tab_list], (100, 140), (520, 360))
        app = FakeNode("gedit", "application", [notebook])
        gui = self.gui()
        gui._tree.root = FakeNode("desktop", "desktop frame", [app])
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            self.assertEqual(gui.element_at(360, 234).name, "Save")

    def test_the_page_tab_step_refuses_a_child_that_misses_the_point(self):
        """The step under `_descend` must not reopen the window-coordinates hole.

        It asks each of a dead-ended node's children directly, where the
        ordinary descent asks the parent for the child at the point, so an
        answer that does not contain the point is refused here or nowhere.
        Staged as the live shape: a node that covers the point, a child that
        does not, and that child answering with a node 9000 pixels away --
        which is precisely what `_at_point` is asked about on a toolkit
        reporting window coordinates.
        """
        away = FakeNode("Away", "label", position=(9000, 9000), size=(10, 10))
        middle = _WindowCoordsNode("Middle", "filler", [away], (5, 5), (10, 10))
        holder = FakeNode("Holder", "frame", [middle], (0, 0), (800, 600))
        app = FakeNode("gedit", "application", [holder])
        gui = self.gui()
        gui._tree.root = FakeNode("desktop", "desktop frame", [app])
        with mock.patch.dict(sys.modules, {"pyatspi": fake_pyatspi()}):
            # The point is inside `holder` and nowhere near either child; the
            # answer has to be the node that really covers it.
            self.assertEqual(gui.element_at(150, 150).name, "Holder")

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


class _RoleCountingNode(FakeNode):
    """A FakeNode that counts how many times roleName is actually read.

    Pins the round-trip reduction in Element.role: role is one AT-SPI round
    trip, and before the fix a single `.role` read fetched it twice (once
    directly, once inside the entry-as-text check) with no memoization.
    """

    def __init__(self, role="push button"):
        super().__init__(role=role)
        self._role_reads = 0

    @property
    def roleName(self):
        self._role_reads += 1
        return self._rolename

    @roleName.setter
    def roleName(self, value):
        self._rolename = value


class TestRoleIsReadOnce(unittest.TestCase):
    def test_role_reads_rolename_once_and_memoizes(self):
        from pyguitest.backends.atspi import Element

        node = _RoleCountingNode(role="push button")
        el = Element(node)
        self.assertEqual(el.role, "push button")
        self.assertEqual(
            node._role_reads, 1, "one .role read should be one roleName fetch"
        )
        # Memoized: further reads cost nothing.
        _ = el.role
        _ = el.role
        self.assertEqual(
            node._role_reads, 1, ".role must be memoized, role is immutable"
        )


try:
    import gi as _gi_probe

    _gi_probe.require_version("Atspi", "2.0")
    from gi.repository import Atspi as _RealAtspi  # noqa: F401

    _HAVE_ATSPI = True
except Exception:
    _HAVE_ATSPI = False


class _CollectionRoot(FakeNode):
    """A root that answers an AT-SPI Collection query instead of a tree walk.

    Advertises the Collection interface and returns a fixed match list from
    get_matches, so find_elements takes the fast path. findChildren sets a
    flag, so a test can prove the walk was *not* used.
    """

    def __init__(self, matches=(), **kw):
        kw.setdefault("role", "application")
        super().__init__(**kw)
        self._matches = list(matches)
        self.walked = False

    def get_interfaces(self):
        return ["Accessible", "Action", "Collection", "Component"]

    def get_matches(self, rule, order, count, traverse):
        return list(self._matches)

    def findChildren(self, pred):
        self.walked = True
        return super().findChildren(pred)


class TestCollectionFastPath(AtspiTestCase):
    """find_elements uses the AT-SPI Collection interface where available.

    It applies the same predicate to the candidates, and falls back to the
    node-by-node walk everywhere else -- never losing a match.
    """

    def _gui_with_root(self, root):
        gui = self.backend()
        gui._tree.root = root
        return gui

    @unittest.skipUnless(_HAVE_ATSPI, "needs the real Atspi typelib")
    def test_uses_collection_and_not_the_walk(self):
        ok = FakeNode("OK", "push button")
        cancel = FakeNode("Cancel", "push button")
        root = _CollectionRoot(matches=[ok, cancel])
        gui = self._gui_with_root(root)
        found = gui.find_elements(role="push button")
        self.assertEqual(sorted(e.name for e in found), ["Cancel", "OK"])
        self.assertFalse(root.walked, "fast path must not fall back to the walk")

    @unittest.skipUnless(_HAVE_ATSPI, "needs the real Atspi typelib")
    def test_the_predicate_still_filters_collection_candidates(self):
        # Collection applies the role filter server-side; name/visible/etc.
        # are still applied to the candidates, identically to the walk.
        ok = FakeNode("OK", "push button")
        cancel = FakeNode("Cancel", "push button")
        hidden = FakeNode("Ghost", "push button")
        hidden.showing = False
        root = _CollectionRoot(matches=[ok, cancel, hidden])
        gui = self._gui_with_root(root)
        self.assertEqual(
            [e.name for e in gui.find_elements(role="push button", name="OK")], ["OK"]
        )
        self.assertEqual(
            sorted(e.name for e in gui.find_elements(role="push button", visible=True)),
            ["Cancel", "OK"],
        )

    @unittest.skipUnless(_HAVE_ATSPI, "needs the real Atspi typelib")
    def test_entry_role_pulls_in_text_for_old_at_spi(self):
        from pyguitest.backends.atspi import _atspi_role_ids

        ids = _atspi_role_ids("entry")
        self.assertIn(_RealAtspi.Role.ENTRY, ids)
        self.assertIn(_RealAtspi.Role.TEXT, ids)

    @unittest.skipUnless(_HAVE_ATSPI, "needs the real Atspi typelib")
    def test_unmappable_role_declines_the_fast_path(self):
        from pyguitest.backends.atspi import _atspi_role_ids

        self.assertIsNone(_atspi_role_ids("no such role"))

    def test_falls_back_to_the_walk_without_a_collection_interface(self):
        # Plain FakeNodes have no get_interfaces -> the walk, everywhere
        # (this one must pass in the bare-stdlib CI job too).
        root, _frame, _button = build_tree()
        gui = self._gui_with_root(root)
        names = [e.name for e in gui.find_elements(role="push button")]
        self.assertIn("OK", names)

    def test_falls_back_to_the_walk_when_the_collection_query_cannot_run(self):
        # get_matches raising (real Atspi) OR the typelib being absent (bare
        # CI) both land on the walk with the right result -- the observable
        # behaviour this asserts is identical either way.
        class _Raising(_CollectionRoot):
            def get_matches(self, *a):
                raise RuntimeError("broken Collection")

        ok = FakeNode("OK", "push button")
        root = _Raising(matches=[], children=[ok])
        gui = self._gui_with_root(root)
        found = gui.find_elements(role="push button")
        self.assertEqual([e.name for e in found], ["OK"])
        self.assertTrue(root.walked)


if __name__ == "__main__":
    unittest.main()


class TestElementValue(unittest.TestCase):
    """`value` is a number or None -- never dogtail's raw `False`.

    dogtail answers `False` for anything with no Value interface, which is
    neither of the two shapes the `float | None` annotation promises. Measured
    live on GNOME and KDE before the fix: every role but `slider`, `level bar`
    and `scroll bar` came back a bool, so `value is not None` was true for a
    push button on Linux and false for one on Windows and macOS.
    """

    def setUp(self):
        patcher, self.frame, self.button = install_fake_dogtail()
        patcher.start()
        self.addCleanup(patcher.stop)
        from pyguitest.backends import atspi

        self.atspi = atspi

    def test_a_control_with_no_value_interface_answers_none(self):
        """The regression: dogtail's `False` must not reach a caller."""
        node = FakeNode(name="Save", role="push button", value=False)
        self.assertIsNone(self.atspi.Element(node).value)

    def test_a_slider_answers_a_float(self):
        """The case that must keep working."""
        node = FakeNode(name="Volume", role="slider", value=0.4)
        self.assertEqual(self.atspi.Element(node).value, 0.4)
        self.assertIsInstance(self.atspi.Element(node).value, float)

    def test_an_integer_value_is_widened_to_float(self):
        """So the type is the annotation's, whatever the toolkit sent."""
        node = FakeNode(name="Pages", role="spin button", value=7)
        value = self.atspi.Element(node).value
        self.assertEqual(value, 7.0)
        self.assertIsInstance(value, float)

    def test_true_is_not_mistaken_for_the_number_one(self):
        """A bool is excluded even though Python counts it as an int.

        Without the isinstance check, `True` would widen to 1.0 and a ticked
        check box would read as a position of 1.0 -- which is exactly how a
        tick gets compared against a slider's range by accident.
        """
        node = FakeNode(name="Bold", role="check box", value=True)
        self.assertIsNone(self.atspi.Element(node).value)

    def test_a_string_value_is_not_a_number(self):
        """A toolkit putting text there is answering a different question."""
        node = FakeNode(name="Name", role="entry", value="Ada")
        self.assertIsNone(self.atspi.Element(node).value)

    def test_an_unparseable_value_is_none_rather_than_an_error(self):
        """Reading a property should not be the thing that raises."""
        node = FakeNode(name="Odd", role="slider", value=object())
        self.assertIsNone(self.atspi.Element(node).value)
