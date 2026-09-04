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
import subprocess
from typing import TYPE_CHECKING, Any

from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable, CapabilityUnsupported
from ..roles import Role
from ..session import SessionType
from .base import GUIBackend, Window

__all__ = [
    "AtspiBackend",
    "Element",
    "a11y_bus_probe",
    "a11y_bus_reachable",
    "available",
]


_A11Y_BUS_TIMEOUT = 5
"""Seconds to wait for the accessibility-bus probe. Short because it runs
inside connect(): a probe that hangs would hang every session, and what it
waits for is a local D-Bus round trip."""

_A11Y_BUS_ANSWERED = False
"""Memoized *positive* answer from a11y_bus_probe; a no is never cached.
Caching one would leave a process that started before its desktop did with
AT-SPI permanently unavailable, for a reason nothing reports -- and the
probe is one cheap subprocess whose failing case fails immediately."""


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

    None means the question could not be asked -- no `gdbus` -- which is
    a different line in a bug report from "it answered", and the reason
    this is separate from `a11y_bus_reachable`.

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
    something whose death is not ours: `gdbus`, making the same
    `org.a11y.Bus.GetAddress` call libatspi makes. A subprocess rather than
    Gio in-process for a second reason as well -- see
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
    _A11Y_BUS_ANSWERED = True
    return True


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

    __slots__ = ("node",)

    def __init__(self, node):
        """Wrap one dogtail node."""
        self.node = node

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
        return Element(parent) if parent is not None else None

    @property
    def children(self):
        """The elements directly inside this one."""
        return [Element(child) for child in self.node.children]

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
    def focused(self):
        """Whether the element currently has keyboard focus."""
        return getattr(self.node, "focused", False)

    @property
    def actions(self):
        """The names of the actions this element offers, e.g. 'click'."""
        return sorted(getattr(self.node, "actions", {}) or {})

    def click(self):
        """Act on the element directly -- no coordinates, no injection."""
        self.node.click()

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
        return [Element(n) for n in self.node.findChildren(pred)]

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

    def matches(node):
        if role is not None and node.roleName != role:
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


class AtspiBackend(GUIBackend):
    """Element automation over the accessibility bus."""

    name = "atspi"

    def __init__(self, environment=None):
        """Connect to the accessibility bus through dogtail."""
        modules = _dogtail()
        if modules is None:
            if not a11y_bus_reachable():
                raise BackendUnavailable(
                    "the accessibility bus did not answer (org.a11y.Bus on "
                    "the session bus). Install at-spi2-core, or start "
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
            for frame in app.children:
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

    def _state_active(self):
        """The pyatspi constant marking an active window.

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
                Capability.WINDOW_STATE,
                self.name,
                "pyatspi is not installed; install it via your distribution "
                "(see README)",
            ) from exc
        return pyatspi.STATE_ACTIVE

    def activate_window(self, window):
        """Give a window keyboard focus."""
        self.require(Capability.WINDOW_ACTIVATE)
        window.handle.grabFocus()

    def geometry(self, window):
        """A window's (x, y, width, height), where coordinates are reliable."""
        self.require(
            Capability.WINDOW_GEOMETRY,
            "AT-SPI screen coordinates are unreliable in a pure Wayland "
            "session; a client is not told where it is on screen",
        )
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
