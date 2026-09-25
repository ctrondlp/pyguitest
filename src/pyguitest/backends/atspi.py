"""Element automation, adapting dogtail.

dogtail is the established AT-SPI automation framework -- Red Hat's, used for
GNOME QA and still maintained. It already solves accessible-tree search,
predicates, retry-on-stale and action dispatch, so this module is an adapter
that maps dogtail's model onto the Capability interface, not a reimplementation.

This is the layer the audit put first: it needs neither the window geometry nor
the input permission that the other backends fight over, and it behaves the same
under X11 and Wayland -- the only backend with no per-compositor matrix.

One honest limitation. AT-SPI reports element coordinates via the Component
interface, but under a pure Wayland session a client does not know where it sits
on screen, so screen-relative extents are unreliable. WINDOW_GEOMETRY is
therefore declared only where those coordinates can be trusted.
"""

import contextlib
import io
import os
import re
import shutil
import socket
import subprocess
from typing import TYPE_CHECKING, Any

from ..capabilities import Capability, CapabilitySet
from ..errors import (
    BackendUnavailable,
    CapabilityUnsupported,
    ElementNotActionable,
    PyGUITestError,
)
from ..roles import Role, spellings
from ..session import SessionType
from .base import GUIBackend, Window

__all__ = [
    "AtspiBackend",
    "Element",
    "a11y_bus_probe",
    "a11y_bus_reachable",
    "available",
]


class _ProbeTimedOut(Exception):
    """One of the probe's subprocesses ran out of time.

    Carried as an exception rather than folded into "no address" because the
    two mean opposite things to the caller: a missing address sends libatspi
    on to the next source, while a question that timed out leaves this probe
    unable to say the address libatspi *will* use is safe. See the timeout
    rule in `a11y_bus_probe`.
    """


_AF_UNIX: int | None = getattr(socket, "AF_UNIX", None)
"""The Unix-socket address family, or None on a platform that has none.

CPython does not define `socket.AF_UNIX` on Windows, and naming it raises
`AttributeError` from inside `_address_connectable` -- which `except OSError`
does not catch, and which `pyguitest debug` would hit on any Windows box with
`AT_SPI_BUS_ADDRESS` set or an X server's `DISPLAY` in the environment, since
`_debug_data` asks this question on every platform.

Read through `getattr` into the family itself rather than kept as a `hasattr`
flag beside a bare `socket.AF_UNIX`, which is what this was. typeshed
declares `AF_UNIX` for every platform *except* win32, so naming it under a
flag mypy cannot follow is a failure of this package's own `mypy` gate on
Windows -- where mypy reads the host platform, so it is every run there --
and on no other platform. One value answering both "is it there" and "what
is it" leaves the runtime guard and the type check saying the same thing.

Not borrowed from `ipc._AF_UNIX`'s `getattr(..., -1)` default, because -1
does not degrade the way that reads: it is CPython's "use the default"
sentinel, so `socket.socket(-1, SOCK_STREAM)` succeeds and hands back an
AF_INET socket, and connecting *that* to a path string raises TypeError
rather than the OSError the caller treats as "nothing there"."""

_A11Y_BUS_TIMEOUT = 5
"""Seconds to wait for the accessibility-bus probe. Short because it runs
inside connect(): a probe that hangs would hang every session, and what it
waits for is a local round trip -- a D-Bus call, or `xprop` reading one
property off a display."""

_A11Y_BUS_ANSWERED = False
"""Memoized *positive* answer from a11y_bus_probe; a no is never cached.
Caching one would leave a process that started before its desktop did with
AT-SPI permanently unavailable, for a reason nothing reports -- and the
probe is a subprocess or two whose failing case fails immediately."""

_WAYLAND_COORDS = (
    "AT-SPI screen coordinates are unreliable in a pure Wayland session; "
    "a client is not told where it is on screen"
)
"""Why the coordinate capabilities are withheld, shared by all three calls
that can be refused for it so the three cannot drift apart."""

_MAX_DEPTH = 24
"""Descent limit for element_at, so a cyclic accessible tree cannot spin
forever. Deep enough for any real widget hierarchy; a tree that has not
bottomed out by here is malformed, and the node reached is still a truthful
answer, just not the deepest one."""


def _at_point(pyatspi, node, x, y):
    """The child of `node` at a screen point, or None."""
    return node.queryComponent().getAccessibleAtPoint(x, y, pyatspi.DESKTOP_COORDS)


def a11y_bus_reachable():
    """Whether to let anything import dogtail. True when in doubt.

    The gate `available()` and `AtspiBackend` use. It folds "could not
    ask" into yes on purpose: refusing AT-SPI on a box that may well have
    a working bus is the worse of the two errors. `a11y_bus_probe` keeps
    that third answer for anything reporting rather than deciding.
    """
    return a11y_bus_probe() is not False


def a11y_bus_probe():
    """Whether the accessibility bus answers: True, False, or None.

    None means the question could not be asked -- no `gdbus`, and no `xprop`
    or no display when the session is one that would read the X11 property --
    which is a different line in a bug report from "it answered", and the
    reason this is separate from `a11y_bus_reachable`.

    libatspi does not fail politely when it cannot reach the bus. It calls
    `g_error()`, which **aborts the process**, and `import dogtail.tree`
    reaches that path on the way in: the module builds its `root` at import
    time from `pyatspi.Registry.getDesktop(0)`. So on a session with no
    reachable bus, importing dogtail takes the caller's whole program down
    with a core dump, and no try/except around the import can prevent it.
    Found by running `connect()` inside `scripts/headless-session.sh` --
    exactly where CI would run, and where the a11y bus launcher could not
    be activated.

    The question therefore has to be answered *before* the import, by
    something whose death is not ours -- and what it has to ask about is
    **the address libatspi would itself connect to**, which is not one
    question. `atspi_get_a11y_bus` consults three sources in a fixed order
    and stops at the first that yields an address: `$AT_SPI_BUS_ADDRESS`,
    then the `AT_SPI_BUS` property on the root window of the display (read
    only when `WAYLAND_DISPLAY` is unset), then
    `org.a11y.Bus.GetAddress` on the session bus. It then connects, and
    the connect is what calls `g_error()`. Asking the session bus alone was
    this probe's own bug, twice over: at-spi-bus-launcher outlives the bus
    it launched and goes on answering with that bus's address, so a
    GetAddress that succeeds says the launcher is alive and nothing about
    the bus; and on a session that has a `DISPLAY` and no
    `WAYLAND_DISPLAY` -- exactly what a recording looks like, since
    `scoped_environment` strips `WAYLAND_DISPLAY` -- libatspi never asks
    the session bus at all. Measured on this developer's desktop,
    2026-09-22: the root-window property named
    `$XDG_RUNTIME_DIR/at-spi/bus`, connecting to it was refused, the
    session bus answered a different and live `bus_0`, and this probe said
    True while `Atspi.get_desktop(0)` aborted the process with SIGABRT.

    `gdbus` and `xprop` are subprocesses rather than Gio and Xlib in this
    process for a second reason as well -- see
    `session.toolkit_accessibility`, which shells out for precisely this:
    importing Gio caches the session bus for the life of the process, which
    breaks tests/test_portal_dbusmock.py.

    Only a yes is remembered; see _A11Y_BUS_ANSWERED. A *timeout* answers
    False: the cost of being wrong there is a skipped backend, and the cost
    of being wrong the other way is a core dump.
    """
    global _A11Y_BUS_ANSWERED
    if _A11Y_BUS_ANSWERED:
        return True
    verdict = _bus_verdict()
    if verdict is True:
        _A11Y_BUS_ANSWERED = True
    return verdict


def _bus_verdict() -> bool | None:
    """Whether the address libatspi would use accepts a connection.

    `atspi_get_a11y_bus`'s three sources in its own order, stopping where
    it stops: an address from the environment ends the search for libatspi,
    and so does one off the X11 root window. Nothing else is asked, because
    libatspi would not ask it -- and a probe that answers about a bus
    libatspi never touches is worse than no probe, which is how the abort
    above got through.

    None only when no source could be consulted at all; see
    `_session_bus_verdict`.
    """
    address = os.environ.get("AT_SPI_BUS_ADDRESS") or None
    if address is None and _x11_bus_applies():
        try:
            address = _x11_address()
        except _ProbeTimedOut:
            return False
    if address is None:
        return _session_bus_verdict()
    return _address_connectable(address) is not False


def _x11_bus_applies() -> bool:
    """Whether libatspi would read the X11 root-window property at all.

    The gate is `WAYLAND_DISPLAY` unset and a display to read from, which is
    why one machine answers on one kind of session and aborts on the other:
    an XWayland desktop sets both and never reads the property, and anything
    that unsets `WAYLAND_DISPLAY` -- the recorder's `scoped_environment`,
    a hand-run `env -u` -- swings libatspi onto a property nothing clears.
    Taken from libatspi's source rather than inferred from behaviour.

    Which display that is comes from `_x11_display()`, not from `DISPLAY`
    alone: libatspi asks `spi_display_name()`, which prefers
    `AT_SPI_DISPLAY`. Gating on `DISPLAY` being set made the
    `AT_SPI_DISPLAY`-only case -- that variable set, `DISPLAY` unset -- skip
    the property and answer from the session bus, reporting a reachable bus
    while libatspi read the stale property and aborted. That is the exact
    failure this probe exists to prevent, so the gate has to ask the same
    question `_x11_display()` does.
    """
    return "WAYLAND_DISPLAY" not in os.environ and _x11_display() is not None


def _x11_address() -> str | None:
    """The `AT_SPI_BUS` root-window property, or None if it cannot be read.

    libatspi's second source, and the one this probe went blind to. Nothing
    clears the property when the bus it names goes away, so it holds a
    leftover's address as often as a live bus's. `xprop` runs for the same
    reason `gdbus` does: a subprocess whose death is not ours.

    None for every way this can fail, because they mean one thing to
    libatspi -- no address from X11, and so on to the session bus. A display
    it cannot open (`xprop` exits 1) and a property nobody ever set
    (`no such atom on any window.`) are both that, which is why neither is
    answered as a no.
    """
    xprop = shutil.which("xprop")
    display = _x11_display()
    if xprop is None or display is None:
        return None
    try:
        probe = subprocess.run(
            [xprop, "-display", display, "-root", "AT_SPI_BUS"],
            capture_output=True,
            timeout=_A11Y_BUS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Not the same as "no property", and the difference decides whether
        # this probe can abort the process. A slow X server still has a root
        # window, and libatspi will still read the address off it -- so
        # falling through to the session bus here would answer confidently
        # about a bus libatspi is never going to touch, which is the exact
        # shape of the bug this rewrite exists to fix. The timeout rule in
        # `a11y_bus_probe` applies: answer no, and pay a skipped backend
        # rather than risk the core dump. `_session_bus_verdict` already
        # separates its two failures the same way.
        raise _ProbeTimedOut from None
    except OSError:
        return None  # no runnable xprop: still an unasked question
    return _address_from_x11(probe.stdout)


def _x11_display() -> str | None:
    """The display whose root window libatspi would read, or None.

    `spi_display_name`'s rule, mirrored because the two halves have to name
    the same display: `AT_SPI_DISPLAY` when it is set, taken as it stands,
    else `DISPLAY` with any screen suffix stripped. An empty one is no
    display at all -- `XOpenDisplay("")` fails -- and libatspi goes on to
    the session bus having read nothing.
    """
    display = os.environ.get("AT_SPI_DISPLAY")
    if display is None:
        display = os.environ.get("DISPLAY") or ""
        colon, dot = display.rfind(":"), display.rfind(".")
        if colon != -1 and dot > colon:
            display = display[:dot]
    return display or None


def _session_bus_verdict() -> bool | None:
    """`org.a11y.Bus.GetAddress` over `gdbus`: True, False, or None.

    libatspi's last source, and the only one that needs no display. None
    means no `gdbus` to ask with, or one on PATH that would not run: a
    question this never got to ask, which is not the same as a no.
    """
    gdbus = shutil.which("gdbus")
    if gdbus is None:
        return None
    try:
        probe = subprocess.run(
            [
                gdbus,
                "call",
                "--session",
                "--dest",
                "org.a11y.Bus",
                "--object-path",
                "/org/a11y/bus",
                "--method",
                "org.a11y.Bus.GetAddress",
            ],
            capture_output=True,
            timeout=_A11Y_BUS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return None  # on PATH but would not run: still an unasked question
    if probe.returncode != 0:
        return False
    # An address is not a bus. `org.a11y.Bus` is answered by
    # at-spi-bus-launcher, which survives the bus it launched and goes on
    # handing out that bus's address afterwards -- so GetAddress succeeding
    # says the launcher is alive, and libatspi needs the thing one step
    # further on. Measured on a machine in exactly that state: GetAddress
    # answered `unix:path=/run/user/1000/at-spi/bus`, connecting to it was
    # refused, and `import dogtail.tree` took the process down with SIGABRT
    # -- the abort this probe exists to prevent, waved through by the probe.
    return _address_connectable(_address_from(probe.stdout)) is not False


def _address_from(reply: bytes | str | None) -> str | None:
    """The bus address out of `gdbus call`'s tuple syntax, or None.

    `gdbus` prints a one-element tuple -- `('unix:path=/run/user/1000/at-spi/bus',)`
    -- and the address is the quoted half. None for anything that does not
    parse, which reads as "cannot ask" rather than as a failure.
    """
    if isinstance(reply, bytes):
        reply = reply.decode("utf-8", errors="replace")
    match = re.search(r"'([^']*)'", reply or "")
    return match.group(1) if match else None


def _address_from_x11(reply: bytes | str | None) -> str | None:
    """The bus address out of `xprop -root`'s output, or None.

    One property line -- `AT_SPI_BUS(STRING) =
    "unix:path=/run/user/1000/at-spi/bus"` -- and the address is the quoted
    half. None for everything else, which is the `gdbus` half's rule and
    for the same reason, plus two forms `xprop` has of its own: `AT_SPI_BUS:
    no such atom on any window.` for a property nobody set, and nothing at
    all for a display it could not open. Both leave libatspi without an X11
    address, so both go on to the session bus rather than answering no.
    """
    if isinstance(reply, bytes):
        reply = reply.decode("utf-8", errors="replace")
    match = re.search(r'=\s*"([^"]*)"', reply or "")
    return match.group(1) if match else None


def _address_connectable(address: str | None) -> bool | None:
    """Whether a D-Bus address accepts a connection: True, False, or None.

    None keeps this probe's third answer intact -- an address naming no
    unix socket (tcp, or a form not handled here) is one this cannot check
    cheaply, and "cannot ask" must not become "no". A plain socket connect
    rather than a second `gdbus` run: it answers the identical question at
    no process cost, and unlike importing anything it cannot itself abort.

    **Every** address in the list is tried, not just the first. A D-Bus
    address is semicolon-separated alternatives, and libdbus walks them until
    one connects -- so judging the list by its first entry alone reported a
    dead leading entry as an unreachable bus and refused AT-SPI that libdbus
    would have gone on to reach. False therefore means at least one unix
    address was tried and none accepted; None means none was there to try.
    """
    if not address or _AF_UNIX is None:
        # No AF_UNIX (Windows) means a unix address is one this cannot test,
        # which is the None case rather than a no. Not left to the connect
        # below to discover: `socket.socket(-1, ...)` does not fail, it builds
        # an *AF_INET* socket -- -1 is CPython's "use the default" sentinel --
        # and connecting that to a path string raises TypeError, which
        # `except OSError` does not catch.
        return None
    tried = False
    for entry in address.split(";"):
        for part in entry.split(","):
            key, _, value = part.partition("=")
            if key == "unix:path":
                target = value
            elif key == "unix:abstract":
                target = "\0" + value
            else:
                continue
            tried = True
            try:
                with socket.socket(_AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(_A11Y_BUS_TIMEOUT)
                    probe.connect(target)
                return True
            except OSError:
                break  # this alternative is dead; libdbus would try the next
    return False if tried else None


def _dogtail():
    """Import dogtail, or return None. Never raises on a missing optional dep.

    dogtail logs to stdout while importing -- notably a multi-line complaint
    about gnome-ponytail-daemon. That noise is swallowed so a first run is not
    alarming; it does not mean ponytail is irrelevant here, though -- geometry()
    needs it, and raises a typed error at call time if the daemon is actually
    missing when that is used. Set PYGUITEST_DOGTAIL_LOGS=1 to see the import
    noise.
    """
    # Before the import, not after: see a11y_bus_reachable on why an
    # unreachable bus makes this import fatal rather than raising.
    if not a11y_bus_reachable():
        return None
    quiet = not os.environ.get("PYGUITEST_DOGTAIL_LOGS")
    sink = io.StringIO()
    try:
        with (
            contextlib.redirect_stdout(sink) if quiet else contextlib.nullcontext(),
            contextlib.redirect_stderr(sink) if quiet else contextlib.nullcontext(),
        ):
            try:
                from dogtail import config as dogtail_config

                dogtail_config.config.logDebugToStdOut = False
                dogtail_config.config.logDebugToFile = False
            except Exception:
                pass
            from dogtail import predicate, tree  # noqa: F401
    except Exception:
        return None
    return tree, predicate


def available():
    """Whether the library this backend needs is importable."""
    return _dogtail() is not None


class Element:
    """One node of the accessible tree.

    Thin wrapper over a dogtail Node. Exposes the subset the Capability
    interface promises, so callers are not coupled to dogtail's API; `node`
    remains reachable for anything this does not cover.
    """

    __slots__ = ("node", "_session")

    def __init__(self, node, session=None):
        """Wrap one dogtail node, and the session it was found through.

        `session` is set by Session as it hands an element out, never by a
        backend: a dogtail node knows nothing of the session above it, and
        double_click needs one, since the pointer is the session's. Optional
        so that an element taken straight from a backend is still
        constructible -- it simply cannot double_click. Elements made while
        walking the tree inherit it from the one they came from; see parent,
        children and find.
        """
        self.node = node
        self._session = session

    @property
    def name(self):
        """The element's accessible name, such as a button's label."""
        return self.node.name

    @property
    def role(self):
        """The element's accessible role, such as 'push button'."""
        return self.node.roleName

    @property
    def parent(self):
        """The containing element, or None at the root."""
        parent = self.node.parent
        return Element(parent, self._session) if parent is not None else None

    @property
    def children(self):
        """The elements directly inside this one."""
        return [Element(child, self._session) for child in self.node.children]

    @property
    def visible(self):
        """Whether the element is currently showing."""
        return self.node.showing

    @property
    def enabled(self):
        """Whether the element accepts input, rather than being greyed out."""
        return getattr(self.node, "sensitive", True)

    @property
    def description(self):
        """The element's longer accessible description, often a tooltip."""
        return getattr(self.node, "description", "") or ""

    @property
    def text(self):
        """The element's text content, for text boxes and labels."""
        return getattr(self.node, "text", None)

    @property
    def value(self):
        """The numeric value of a slider, spinner, or progress bar."""
        return getattr(self.node, "value", None)

    @property
    def checked(self):
        """Whether a check box, radio button, or toggle is set.

        Reports a real (non-None) boolean for every element, not only the
        checkable ones -- AT-SPI's checked state is just unset elsewhere.
        Read `checkable` first to know whether this value means anything.
        """
        return getattr(self.node, "checked", None)

    @property
    def checkable(self):
        """Whether the element has a check box, radio button, or toggle."""
        return getattr(self.node, "checkable", False)

    @property
    def selected(self):
        """Whether a list item, tab, or menu item is currently selected.

        Same caveat as `checked`: this is a real boolean everywhere, not
        only on selectable elements. Read `selectable` first.
        """
        return getattr(self.node, "selected", None)

    @property
    def selectable(self):
        """Whether the element can be a list item, tab, or menu selection."""
        return getattr(self.node, "selectable", False)

    @property
    def expanded(self):
        """Whether a tree item or similar disclosure control is open.

        dogtail wires `checked` and `selected` to real AT-SPI states through
        its own `AccessibleState`, but never got as far as EXPANDED --
        `Node.collapsed` is the only trace of the pair, and GTK's own tree
        rows publish EXPANDABLE/EXPANDED, not COLLAPSED, so that property
        answers nothing useful here. Read directly off the node's own state
        set instead, the same primitive dogtail's own helper is built on --
        by each state's `.name`, not against `Atspi.StateType.EXPANDED`,
        so reading this needs no `gi.repository.Atspi` import of its own:
        every other property here stays within what dogtail's Node already
        gives, and `expanded`/`expandable` follow that rather than becoming
        the one pair that requires PyGObject wherever they are merely
        imported, extras declaration or not. Measured live against a GTK3
        GtkTreeView: a collapsed row's state set holds EXPANDABLE alone,
        and gains EXPANDED once opened. Same caveat as `checked`: read
        `expandable` first.
        """
        states = self._state_names()
        if "EXPANDABLE" not in states:
            return None
        return "EXPANDED" in states

    @property
    def expandable(self):
        """Whether the element can be expanded or collapsed, like a tree item."""
        return "EXPANDABLE" in self._state_names()

    def _state_names(self):
        """This node's AT-SPI states, by name -- `{"EXPANDABLE", ...}`.

        `state_set` is a GI enum list already; `.name` reads each member's
        own name without this module ever importing the enum type that
        defines it.

        A node that died since it was found answers with no states rather
        than raising: `tree_items()` reads this off every row it walks, and
        one row closing mid-walk should read as a leaf, not end the walk.
        """
        try:
            states = self.node.state_set or []
            return {getattr(state, "name", str(state)) for state in states}
        except Exception:  # noqa: BLE001 - a reaped node has no states to read
            return set()

    @property
    def focused(self):
        """Whether the element currently has keyboard focus."""
        return getattr(self.node, "focused", False)

    @property
    def actions(self):
        """The names of the actions this element offers, e.g. 'click'."""
        return sorted(getattr(self.node, "actions", {}) or {})

    @property
    def pid(self):
        """The process this element belongs to, or None.

        Not every bridge answers, and one that does not raises rather than
        returning nothing -- so does a node whose application has since
        exited. A pid of 0 is the bridge saying it does not know, which is
        the same answer as not being asked.
        """
        try:
            pid = int(self.node.get_process_id())
        except Exception:  # noqa: BLE001 - an unanswered pid is not an error
            return None
        return pid or None

    @property
    def alive(self):
        """Whether the underlying widget still exists.

        Goes through dogtail's own `Node.dead` rather than reimplementing
        it against a raw AT-SPI state flag: dogtail decides by asking the
        bus directly (a node with no children and no parent is as sure a
        sign as the API gives), which does not depend on a DEFUNCT flag
        having already been set. A node the bus can no longer reach at
        all -- fully reaped, not merely marked dead -- raises from that
        same call rather than answering; caught here and folded into the
        same False, since a query this is meant to make safe should not
        itself become the thing that raises.
        """
        try:
            return not self.node.dead
        except Exception:  # noqa: BLE001 - a dead node can fail several ways
            return False

    def click(self):
        """Act on the element directly -- no coordinates, no injection.

        Tries AT-SPI's own action interface first, where the element offers
        one. `doActionNamed` needs no coordinates or daemon at all, and it
        is the reliable one: GTK measured live (a checkbox and a radio
        button, both on the probe window in pyguitest-recorder) reports the
        whole row as the element's rectangle, not the small toggle the row
        actually reacts to, so dogtail's own coordinate click lands past it
        and changes nothing -- silently. `element.click()` returned
        normally having done nothing, while `element.do_action("click")` on
        the identical element toggled it every time; nothing was ever
        raised for a caller to catch, which is why the fix is in the
        ordering rather than another exception to handle.

        dogtail's own `Node.click()` -- tried second now, where no action
        interface answers -- is coordinate-based even on X11, and under
        Wayland it synthesizes that click through GNOME's ponytail daemon,
        absent on every other Wayland compositor (KDE/KWin, sway, Hyprland,
        niri), where it raises a RuntimeError before ever reaching AT-SPI.
        Caught below and turned into the same ElementNotActionable an
        element with no Action interface at all gets -- KDE's QML-based
        Kickoff menu does this for its category labels, and coordinate
        clicking still works there, which is the case this order keeps the
        coordinate path for.

        The same except used to also be how an element with no usable
        position was caught: dogtail injects at the widget's own rectangle,
        and AT-SPI's INT_MIN "not showing" sentinel made `check_coordinates`
        raise `ValueError: Attempting to generate a mouse event at negative
        coordinates: (-2147483647, -2147483647)` before AT-SPI was asked
        anything, measured replaying `gui.menu_item("Gamma").click()` into a
        GTK3 combo box's popup. Acting through the published action first
        means that path is not reached at all where one exists -- confirmed
        live, the same combo item selects correctly through `do_action` with
        no position of its own to give -- and the `ValueError` branch below
        is what remains for an element offering neither.
        """
        actions = self.node.actions or {}
        name = next((a for a in actions if a.lower() in ("click", "press")), None)
        if name is not None:
            self.node.doActionNamed(name)
            return
        try:
            self.node.click()
        except (RuntimeError, ValueError) as error:
            text = str(error).lower()
            # dogtail's `check_coordinates` wording, not any mention of the
            # word -- an unrelated error that happens to say "coordinates"
            # must surface as itself.
            if "ponytail" not in text and "negative coordinates" not in text:
                raise
            raise ElementNotActionable(self.role, self.name) from error

    def double_click(self):
        """Double-click the element: locate it, then inject the gesture.

        There is no accessible action to name here, the way `click` names
        one. A double-click is a gesture on the pointer, and no toolkit
        publishes one on the bus -- two `click()` calls are two bus round
        trips, slower than any toolkit's double-click interval, so the pair
        arrives as two single clicks and a double-clicked folder icon simply
        does not open.

        So the element stays the locator and the gesture falls back to the
        pointer, where this package's real double_click lives. That happens
        on the Session, which is the only thing holding a backend able to
        move a pointer -- hence the delegation below, and hence `_session`.

        Needs Capability.ELEMENT_GEOMETRY on top of what Session.double_click
        needs, since the rectangle has to be read to find the point. Raises
        PyGUITestError where `extents()` has no rectangle for this element,
        which is what double_click_element raises, and where there is no
        session to delegate to at all.
        """
        if self._session is None:
            raise PyGUITestError(
                f"{self.name!r} carries no session to double-click through -- "
                "it came from a backend directly rather than from a Session. "
                "Use gui.double_click_element(element), or take the element "
                "from the session instead: gui.button(...), gui.element(...), "
                "gui.root_element()"
            )
        self._session.double_click_element(self)

    def _bind_session(self, session):
        """Record which Session handed this element out.

        Named rather than left as a plain attribute, so that Session can
        offer it to any element type without knowing which backend built
        it: an element with no such method simply never gets a session, and
        double_click is the only thing that notices. Elements made while
        walking the tree pass on whatever their source carried, which is
        what keeps `gui.root_element().child(...)` able to double_click.
        """
        self._session = session

    def focus(self):
        """Give the element keyboard focus."""
        self.node.grabFocus()

    def set_text(self, text):
        """Replace the element's text content."""
        self.node.text = text

    def do_action(self, name):
        """Perform a named accessible action, such as "click" or "activate"."""
        self.node.doActionNamed(name)

    def select(self):
        """Select this element, for a list item, tab, or menu entry."""
        self.node.select()

    def expand(self):
        """Open this tree item or similar disclosure control.

        A no-op where `expanded` already reads True: GTK (measured; likely
        every AT-SPI toolkit, since the interface has no separate verbs)
        publishes one action, "expand or contract", that toggles rather
        than opening -- calling it on an already-open row would close it.
        """
        if self.expanded is True:
            return
        self._toggle_expansion()

    def collapse(self):
        """Close this tree item or similar disclosure control. See `expand`."""
        if self.expanded is False:
            return
        self._toggle_expansion()

    def _toggle_expansion(self):
        """Invoke whichever published action opens or closes this node.

        Named by substring rather than the exact string "expand or
        contract" this was measured with, the same defensiveness `click`
        already has for "click"/"press": an AT-SPI action's wording is the
        toolkit's, not a contract this package can rely on staying fixed.
        """
        actions = self.node.actions or {}
        name = next((a for a in actions if "expand" in a.lower()), None)
        if name is None:
            raise ElementNotActionable(
                self.role,
                self.name,
                f"{self.role} {self.name!r} offers no action naming "
                f"'expand'; it offers {', '.join(sorted(actions)) or 'none'}",
            )
        self.node.doActionNamed(name)

    def choose(self, option):
        """Pick `option` from this dropdown by its visible text.

        Uses the combo box's own value setter where the toolkit provides one,
        which is more reliable than clicking the popup open and hunting for
        the item.
        """
        self.node.combovalue = option

    def options(self):
        """The choices this dropdown or list offers, as Elements."""
        found = self.find(role=Role.MENU_ITEM)
        return found or self.find(role=Role.LIST_ITEM)

    def find(self, role=None, name=None):
        """Search this element's descendants by role and/or name."""
        from dogtail import predicate

        pred = predicate.GenericPredicate(roleName=role, name=name)
        return [Element(n, self._session) for n in self.node.findChildren(pred)]

    def child(self, role=None, name=None):
        """Return the first descendant matching role and/or name, or None."""
        matches = self.find(role=role, name=name)
        return matches[0] if matches else None

    def is_ancestor_of(self, other):
        """Whether `other` sits somewhere inside this element."""
        node = other.node
        while node is not None:
            node = node.parent
            if node == self.node:
                return True
        return False

    def __repr__(self):
        """The role and name, which is how an element is written in a script."""
        return f"Element({self.role!r}, {self.name!r})"


if TYPE_CHECKING:
    from .base import Element as _ElementInterface

    def _conforms(element: Element) -> _ElementInterface:
        """Check, statically only, that this Element satisfies the protocol.

        Session.element and the widget finders are annotated with
        base.Element, so this class is what makes those annotations true.
        Renaming or dropping a member here would otherwise surface as a
        type error in whoever called it, a module away from the cause.
        """
        return element


def _matches_text(value, wanted):
    """Whether `value` satisfies `wanted`.

    Exact match for a plain string, `.search()` for a compiled pattern --
    mirrors the regex convention Session.find_window already uses for
    window titles.
    """
    if isinstance(wanted, re.Pattern):
        return wanted.search(value or "") is not None
    return value == wanted


def _build_predicate(role, name, enabled, visible, description, predicate):
    """A `node -> bool` function for `Node.findChildren`.

    dogtail's find_all_descendants accepts a plain function exactly like a
    GenericPredicate instance (checked via isinstance(..., LambdaType), and
    an ordinary `def` satisfies that same check) -- this replaces the old
    GenericPredicate(roleName=role, name=name) call with one that also knows
    about state and arbitrary caller logic, without needing two code paths.
    """
    wanted = None if role is None else spellings(role)

    def matches(node):
        """Whether this node has the wanted role and matches the other filters."""
        # Compared against every spelling of the role, not just the one asked
        # for: at-spi2 renamed push button to button without changing the
        # integer, so the string a desktop reports depends on its version.
        if wanted is not None and node.roleName not in wanted:
            return False
        if name is not None and not _matches_text(node.name, name):
            return False
        if enabled is not None and bool(getattr(node, "sensitive", True)) != enabled:
            return False
        if visible is not None and bool(node.showing) != visible:
            return False
        if description is not None and not _matches_text(
            getattr(node, "description", "") or "", description
        ):
            return False
        if predicate is not None:
            return predicate(Element(node))
        return True

    return matches


_UNPLACED = -1_000_000
"""Below this, an x or y is a "no position" marker rather than a coordinate.

AT-SPI's own marker is INT_MIN (-2147483648), which is what GTK reports for a
component that is not showing. The threshold is deliberately far looser than
that exact value, so a toolkit picking a different large negative is caught
too, and deliberately far below any real coordinate: a window dragged off the
left of a multi-monitor desktop lives in the thousands, not the millions.
"""


class AtspiBackend(GUIBackend):
    """Element automation over the accessibility bus."""

    name = "atspi"

    def __init__(self, environment=None):
        """Connect to the accessibility bus through dogtail."""
        modules = _dogtail()
        if modules is None:
            if not a11y_bus_reachable():
                raise BackendUnavailable(
                    "the accessibility bus did not answer (the address "
                    "libatspi would connect to -- from "
                    "$AT_SPI_BUS_ADDRESS, the AT_SPI_BUS property on the X "
                    "root window, or org.a11y.Bus on the session bus -- was "
                    "not reachable). Install at-spi2-core, or start "
                    "at-spi-bus-launcher; a headless or container session "
                    "often has neither. Importing dogtail without it aborts "
                    "the process, so this refuses rather than trying"
                )
            raise BackendUnavailable(
                "dogtail is not installed; pip install 'pyguitest[atspi]'"
            )
        self._tree, self._predicate = modules
        self.environment = environment

    @property
    def _screen_coords_trustworthy(self):
        # A pure Wayland client is never told its position on screen, so the
        # extents it reports through AT-SPI cannot be trusted as screen
        # coordinates. Under X11 and XWayland they can.
        """Whether AT-SPI screen coordinates can be believed in this session."""
        return (
            self.environment is None
            or self.environment.session_type is not SessionType.WAYLAND
        )

    @property
    def capabilities(self):
        """Element access, plus window listing and geometry where trustworthy."""
        caps = {
            Capability.ELEMENT_TREE,
            Capability.ELEMENT_ACTION,
            Capability.WINDOW_LIST,
            Capability.WINDOW_STATE,
            Capability.WINDOW_ACTIVATE,
        }
        if self._screen_coords_trustworthy:
            caps.add(Capability.WINDOW_GEOMETRY)
            caps.add(Capability.ELEMENT_GEOMETRY)
        return CapabilitySet(caps)

    # -- elements ----------------------------------------------------------

    def root_element(self):
        """The root of the accessible tree."""
        self.require(Capability.ELEMENT_TREE)
        return Element(self._tree.root)

    def find_elements(
        self,
        role=None,
        name=None,
        within=None,
        enabled=None,
        visible=None,
        description=None,
        predicate=None,
    ):
        """Search the accessible tree.

        `name`/`description` take a plain string (exact match) or a compiled
        regex (`.search()`). `enabled`/`visible` filter on element state.
        `predicate` is an arbitrary `Element -> bool` for anything else --
        combined with `Element.parent`/`.children`/`.is_ancestor_of`, it
        covers ancestor/descendant queries without a dedicated relation API.
        """
        self.require(Capability.ELEMENT_TREE)
        node = within.node if within is not None else self._tree.root
        pred = _build_predicate(role, name, enabled, visible, description, predicate)
        return [Element(n) for n in node.findChildren(pred)]

    def find_element(
        self,
        role=None,
        name=None,
        within=None,
        enabled=None,
        visible=None,
        description=None,
        predicate=None,
    ):
        """The first match, or None."""
        matches = self.find_elements(
            role=role,
            name=name,
            within=within,
            enabled=enabled,
            visible=visible,
            description=description,
            predicate=predicate,
        )
        return matches[0] if matches else None

    def extents(self, element):
        """An element's (x, y, width, height) in screen coordinates, or None."""
        self.require(Capability.ELEMENT_GEOMETRY, _WAYLAND_COORDS)
        return self._extents(element.node)

    def element_at(self, x, y):
        """The most specific element at a screen point, or None.

        Every application is asked, because the accessible tree has no root
        that hit-tests: `get_accessible_at_point` answers about one
        component's own children, so the walk has to start at each frame
        and descend from whichever claim the point.

        The *smallest* of those answers wins, not the first. The tree
        carries no stacking order -- AT-SPI's z-order is optional and
        almost never implemented -- and application order is arbitrary, so
        specificity is the only signal available. It matters live: on GNOME
        the shell publishes a full-screen `panel`, so first-wins returned
        that same panel for every point on the desktop, whatever window was
        actually there. A caller needing certainty about which window it
        landed in should check the answer against `window_at`.

        Every answer along the way must survive its own extents: a node is
        only accepted where the rectangle it reports contains the point it
        was looked up at. A toolkit that reports widgets in *window*
        coordinates -- a native Wayland GTK4 client cannot do otherwise,
        since it is never told where it sits -- otherwise claims points
        hundreds of pixels away and answers them with a real, named widget.
        Seen live: a terminal's "New Terminal" button, extents (0, 0, 34,
        34), returned for a point at (49, 83). An application publishing no
        extents at all drops out for the same reason, since nothing it says
        can be checked.

        A whole application is skipped rather than believed if it raises --
        an application that exits mid-walk is ordinary, and taking the hit
        test down with it would make this fail for reasons having nothing
        to do with the point asked about.
        """
        self.require(Capability.ELEMENT_GEOMETRY, _WAYLAND_COORDS)
        pyatspi = self._pyatspi(Capability.ELEMENT_GEOMETRY)
        best = None
        best_area = None
        for app in self._tree.root.applications():
            for node in self._hits_in(pyatspi, app, x, y):
                area = self._area(node)
                if best is None or area < best_area:
                    best, best_area = node, area
        return Element(best) if best is not None else None

    def _hits_in(self, pyatspi, app, x, y):
        """The deepest believable node under each of one app's toplevels."""
        found = []
        try:
            for frame in app.children:
                if self._covers(frame, x, y):
                    found.append(self._descend(pyatspi, frame, x, y))
        except Exception:  # noqa: BLE001 - a dead application is not an error
            return found
        return found

    def _descend(self, pyatspi, node, x, y):
        """Follow the point down to the deepest node that really covers it."""
        for _ in range(_MAX_DEPTH):
            child = _at_point(pyatspi, node, x, y)
            if child is None or not self._covers(child, x, y):
                child = self._past_a_container(pyatspi, node, x, y)
            if child is None:
                return node
            node = child
        return node

    def _past_a_container(self, pyatspi, node, x, y):
        """A covering node one level further down, where the walk dead-ends.

        The descent assumes the tree nests geometrically: that a node
        covering the point has a child covering it too, which is what
        `getAccessibleAtPoint` is asked for. GtkNotebook breaks that, and
        it is not an exotic toolkit corner -- it is every application with
        tabs. A notebook publishes its page *contents* as children of the
        `page tab`, whose own rectangle is the little tab label at the top;
        so the page tab does not contain its own children, and the `page
        tab list` above it answers `getAccessibleAtPoint` with nothing at
        all for any point in the page body -- the point is in no tab's
        label. The walk stopped there and `element_at` answered with the
        tab list for every widget on the page.

        Measured on a GTK3 notebook: the page tab reports (133, 141, 54,
        30), its content filler (113, 175, 494, 312), and the `Save` button
        inside that (113, 217, 494, 34). Asked about the button's own
        centre, the tab list answered `None` and the page tab answered the
        filler correctly -- so one step through the tab is all that is
        missing.

        Only taken where the ordinary descent has already failed, so it
        costs nothing on the common path, and it asks each child rather
        than searching the subtree, so it adds one level of fan-out and not
        a walk. The node it returns still has to cover the point, which is
        the invariant `element_at` rests on: a toolkit reporting widgets in
        window coordinates -- a native Wayland client cannot do otherwise --
        is refused here exactly as it was before.
        """
        try:
            children = list(node)
        except Exception:  # noqa: BLE001 - a dead node has no children
            return None
        for child in children:
            # One unanswerable child -- no Component interface, or gone since
            # `list(node)` -- must not abandon the rest: uncaught, it escaped
            # to `_hits_in`, which drops the whole application, a worse
            # answer than the coarse node this step exists to refine.
            try:
                found = _at_point(pyatspi, child, x, y)
            except Exception:  # noqa: BLE001 - try the next child instead
                continue
            if found is not None and self._covers(found, x, y):
                return found
        return None

    def _covers(self, node, x, y):
        """Whether the node's own rectangle contains the point."""
        rect = self._extents(node)
        if rect is None:
            return False
        left, top, width, height = rect
        return left <= x < left + width and top <= y < top + height

    def _area(self, node):
        """How much screen the node covers. Only asked of a node that does."""
        rect = self._extents(node)
        return 0 if rect is None else rect[2] * rect[3]

    def _extents(self, node):
        """One node's screen rectangle, or None where it has no useful one.

        Goes to the Component interface rather than dogtail's own
        `Node.extents`, which retries in *window* coordinates whenever the
        screen ones come back at the origin. That heuristic rescues a
        Wayland client, which reports (0, 0) for everything -- but it hands
        back a rectangle in a different coordinate space with nothing to
        say so, and a caller comparing element rectangles against
        `geometry()`'s window one cannot tell the two apart. This backend
        answers in screen coordinates or not at all; ELEMENT_GEOMETRY is
        withheld in the session where they would be meaningless.
        """
        pyatspi = self._pyatspi(Capability.ELEMENT_GEOMETRY)
        try:
            rect = node.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
        except Exception:  # noqa: BLE001 - no Component interface, or a dead node
            return None
        if rect is None or rect.width <= 0 or rect.height <= 0:
            return None
        # A component that is not currently shown has no position to report,
        # and AT-SPI says so by answering INT_MIN for x and y rather than by
        # failing -- usually with a 1x1 size, which slips past the check
        # above. That is not a rectangle a caller can do anything with: a
        # click point derived from it is nowhere, a containment test against
        # it is always false, and `_area` scores it 1, the *smallest*
        # possible, which is what element_at treats as most specific.
        #
        # Not rare, and not confined to odd toolkits: 177 of the 207 nodes in
        # an ordinary gedit window report it, every one of them the contents
        # of a popover or menu that has not been opened. Measured on GNOME
        # Shell 51.rc; see docs/validation.md.
        if rect.x <= _UNPLACED or rect.y <= _UNPLACED:
            return None
        return (rect.x, rect.y, rect.width, rect.height)

    # -- windows -----------------------------------------------------------

    def windows(self):
        """Toplevel frames, gathered per application.

        Works on GNOME, where no foreign-toplevel protocol exists -- the
        accessibility bus knows the frames even though the compositor will not
        say. That is the practical reason this backend leads.
        """
        self.require(Capability.WINDOW_LIST)
        found = []
        for app in self._tree.root.applications():
            try:
                frames = app.children
            except Exception:  # noqa: BLE001 - a dead application is not an error
                # Mirrors _hits_in's own guard: an application exiting
                # between the app list and this read is ordinary, and
                # taking the whole listing down with it would make every
                # other caller (find_windows, wait_for_window, is_window_
                # open, wait_window_close) fail for reasons having nothing
                # to do with the window they actually asked about.
                continue
            for frame in frames:
                if frame.roleName in Role.WINDOW_ROLES:
                    found.append(
                        Window(
                            handle=frame,
                            backend=self,
                            title=frame.name or "",
                            app_id=app.name or "",
                        )
                    )
        return found

    def active_window(self):
        """The focused window, or None."""
        self.require(Capability.WINDOW_STATE)
        for window in self.windows():
            if window.handle.getState().contains(self._state_active()):
                return window
        return None

    def is_window_viewable(self, window):
        """Whether `window`'s frame is currently showing.

        Same accessible-tree state Element.visible already reads (dogtail's
        Node.showing, backed by AT-SPI's STATE_SHOWING) -- a window's frame is
        an accessible node like any other here.
        """
        self.require(Capability.WINDOW_STATE)
        # `window` is a Window (its .handle is the dogtail node, typed as
        # plain `object` since Window.handle is deliberately backend-private)
        # or already an Element's own dogtail node -- both branches are
        # really the same dogtail Node, which nothing here has a static type
        # for since dogtail is an optional runtime import.
        node: Any = window.handle if isinstance(window, Window) else window.node
        return bool(node.showing)

    def _pyatspi(self, capability):
        """The pyatspi module, or a typed error naming what wanted it.

        pyatspi is not in this package's dependency declarations at all --
        the atspi extra only pulls in dogtail, and pyatspi is meant to come
        from the distro (see README). available() checks only dogtail, so a
        box with dogtail installed but not the distro's pyatspi package
        would otherwise get a bare ImportError here, on a backend that
        looked fully constructed. Routed through the same typed-error
        pattern as every other unsupported operation instead.
        """
        try:
            import pyatspi
        except ImportError as exc:
            raise CapabilityUnsupported(
                capability,
                self.name,
                "pyatspi is not installed; install it via your distribution "
                "(see README)",
            ) from exc
        return pyatspi

    def _state_active(self):
        """The pyatspi constant marking an active window."""
        return self._pyatspi(Capability.WINDOW_STATE).STATE_ACTIVE

    def activate_window(self, window):
        """Give a window keyboard focus."""
        self.require(Capability.WINDOW_ACTIVATE)
        window.handle.grabFocus()

    def geometry(self, window):
        """A window's (x, y, width, height), where coordinates are reliable."""
        self.require(Capability.WINDOW_GEOMETRY, _WAYLAND_COORDS)
        node: Any = window.handle if isinstance(window, Window) else window.node
        try:
            x, y = node.position
            width, height = node.size
        except Exception as exc:
            # dogtail's Component.get_size/get_position route through its own
            # ponytail helper on GNOME -- a real system daemon and D-Bus call,
            # not a Python dependency, so _dogtail()'s import guard never sees
            # this coming. It can fail several distinct ways in the wild: the
            # daemon missing entirely (RuntimeError), or present but denied by
            # GNOME Shell's Introspect policy (dbus.exceptions.DBusException,
            # e.g. "GetWindows is not allowed") -- neither is a subclass of
            # the other, and there is no reasonably closed list to match
            # against. X11/XWayland still trust these coordinates (that is
            # what _screen_coords_trustworthy already decided); any failure
            # actually reading them is an availability problem, not a
            # Wayland-honesty one, so *every* exception from this pair of
            # calls gets the same typed treatment as the missing-pyatspi case
            # in _state_active, with the original error kept in the message
            # since which of ponytail's failure modes this is matters for
            # fixing it.
            raise CapabilityUnsupported(
                Capability.WINDOW_GEOMETRY,
                self.name,
                "dogtail could not read component geometry via "
                f"gnome-ponytail-daemon ({exc}); this needs the daemon "
                "installed and running, and GNOME Shell's window "
                "introspection permitted for it -- the exact requirement "
                "varies by GNOME version",
            ) from exc
        return (x, y, width, height)
