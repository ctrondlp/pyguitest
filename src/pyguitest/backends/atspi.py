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

from ..capabilities import Capability, CapabilitySet
from ..errors import BackendUnavailable, CapabilityUnsupported
from ..roles import Role
from ..session import SessionType
from .base import GUIBackend, Window

__all__ = ["AtspiBackend", "Element", "available"]


def _dogtail():
    """Import dogtail, or return None. Never raises on a missing optional dep.

    dogtail logs to stdout while importing -- notably a multi-line complaint
    about gnome-ponytail-daemon. That noise is swallowed so a first run is not
    alarming; it does not mean ponytail is irrelevant here, though -- geometry()
    needs it, and raises a typed error at call time if the daemon is actually
    missing when that is used. Set PYGUITEST_DOGTAIL_LOGS=1 to see the import
    noise.
    """
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
        """Whether a check box, radio button, or toggle is set."""
        return getattr(self.node, "checked", None)

    @property
    def selected(self):
        """Whether a list item, tab, or menu item is currently selected."""
        return getattr(self.node, "selected", None)

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


class AtspiBackend(GUIBackend):
    """Element automation over the accessibility bus."""

    name = "atspi"

    def __init__(self, environment=None):
        """Connect to the accessibility bus through dogtail."""
        modules = _dogtail()
        if modules is None:
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

    def find_elements(self, role=None, name=None, within=None):
        """Search the accessible tree by role and/or name."""
        self.require(Capability.ELEMENT_TREE)
        node = within.node if within is not None else self._tree.root
        pred = self._predicate.GenericPredicate(roleName=role, name=name)
        return [Element(n) for n in node.findChildren(pred)]

    def find_element(self, role=None, name=None, within=None):
        """The first match, or None."""
        matches = self.find_elements(role=role, name=name, within=within)
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
        node = window.handle if isinstance(window, Window) else window.node
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
        node = window.handle if isinstance(window, Window) else window.node
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
