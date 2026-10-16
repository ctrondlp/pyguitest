"""Window control through compositor IPC.

The audit's largest gap: 19 legacy functions need per-desktop code because no
Wayland protocol carries window geometry or placement. Compositors that expose
an IPC socket answer all of it.

These backends take a *transport* rather than shelling out. For sway and
Hyprland that is a unix socket speaking the documented protocol (see ipc.py),
which needs no tool installed, spawns no process per query, and can hold a
connection open to stream events. The CLI transports remain as fallbacks.

kdotool is the exception: KWin has no simple socket protocol, and driving its
scripting API over D-Bus is real work that kdotool already does.
"""

import re
import subprocess
import time
from collections import namedtuple

from ..capabilities import Capability, CapabilitySet
from ..errors import CapabilityUnsupported, PyGUITestError, WindowNotFound
from ..ipc import DEFAULT_TIMEOUT
from .base import GUIBackend, Screen, Window

__all__ = [
    "SwayBackend",
    "HyprlandBackend",
    "NiriBackend",
    "KdotoolBackend",
    "WindowEvent",
    "for_compositor",
    "for_tool",
]

WindowEvent = namedtuple("WindowEvent", "change window")
"""A window lifecycle event. `change` is the compositor's own verb --
"new", "close", "focus", "title" -- and `window` is the affected Window."""

_COMMON = {
    Capability.WINDOW_LIST,
    Capability.WINDOW_STATE,
    Capability.WINDOW_GEOMETRY,
    Capability.WINDOW_PLACEMENT,
    Capability.WINDOW_RESIZE,
    Capability.WINDOW_ACTIVATE,
    Capability.WINDOW_PID,
    Capability.WINDOW_AT_POINT,
    Capability.WINDOW_MINIMIZE,
    Capability.SCREEN_INFO,
}


class _WindowBackend(GUIBackend):
    """Shared hit-testing and handle resolution."""

    def _handle(self, window):
        """The backend-native handle for a Window or raw handle."""
        return window.handle if isinstance(window, Window) else window

    def _windows_with_geometry(self):
        """(Window, (x, y, width, height)) for every window.

        The default calls geometry() once per window -- one IPC round trip
        and one tree walk each. A subclass whose transport can hand back
        every rectangle from a single fetch overrides this so window_at
        does not pay for n round trips and O(n^2) parsing on one hit-test.
        """
        return [(w, self.geometry(w)) for w in self.windows()]

    def window_at(self, x, y, screen=0):
        """Hit-test a coordinate.

        Computed from geometry rather than asked of the compositor: the last
        match wins, matching X11::GUITest's convention that windows arrive in
        stacking order and the topmost is last.
        """
        self.require(Capability.WINDOW_AT_POINT)
        match = None
        for window, (wx, wy, width, height) in self._windows_with_geometry():
            if wx <= x < wx + width and wy <= y < wy + height:
                match = window
        return match


class SwayBackend(_WindowBackend):
    """sway, and other compositors speaking the i3 IPC protocol."""

    name = "sway"

    def __init__(self, transport):
        """Drive sway through `transport`, a socket or CLI adapter."""
        self.transport = transport

    def close(self):
        """Close the transport."""
        self.transport.close()

    @property
    def capabilities(self):
        # The only backend here with an event subscription, so the only one
        # that can replace polling.
        """Full window control, plus the event subscription only sway offers."""
        return CapabilitySet(_COMMON | {Capability.WINDOW_EVENTS})

    def _views(self, node):
        """Every view in the tree. Layout containers have no pid.

        Filtered on pid alone: a window that has not yet set its title is
        exactly the one a wait_for_window caller is racing against, and
        requiring a name too made it invisible to windows() until then.
        """
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            yield from self._views(child)
        if node.get("pid") is not None:
            yield node

    def _window(self, node):
        """Build a Window from a sway tree node."""
        return Window(
            handle=node["id"],
            backend=self,
            title=node.get("name") or "",
            app_id=node.get("app_id")
            or node.get("window_properties", {}).get("class", ""),
            pid=node.get("pid"),
        )

    def windows(self):
        """Every open window."""
        self.require(Capability.WINDOW_LIST)
        return [self._window(n) for n in self._views(self.transport.get_tree())]

    def geometry(self, window):
        """A window's (x, y, width, height) in screen coordinates."""
        self.require(Capability.WINDOW_GEOMETRY)
        handle = self._handle(window)
        for node in self._views(self.transport.get_tree()):
            if node["id"] == handle:
                rect = node["rect"]
                return (rect["x"], rect["y"], rect["width"], rect["height"])
        raise WindowNotFound(f"no window with id {handle!r}")

    def is_window_viewable(self, window):
        """Whether `window` is currently visible.

        sway's own tree field, documented in sway-ipc(7): "visible" is
        present only on window nodes and means exactly this.
        """
        self.require(Capability.WINDOW_STATE)
        handle = self._handle(window)
        for node in self._views(self.transport.get_tree()):
            if node["id"] == handle:
                return bool(node.get("visible", True))
        raise WindowNotFound(f"no window with id {handle!r}")

    def _windows_with_geometry(self):
        """One get_tree() call, reused for the whole window list and rects.

        window_at's default calls geometry() once per window, and each of
        those re-fetches and re-walks the entire tree -- n round trips and
        O(n^2) parsing for one hit-test.
        """
        result = []
        for node in self._views(self.transport.get_tree()):
            rect = node["rect"]
            geometry = (rect["x"], rect["y"], rect["width"], rect["height"])
            result.append((self._window(node), geometry))
        return result

    def active_window(self):
        """The focused window, or None."""
        self.require(Capability.WINDOW_STATE)
        for node in self._views(self.transport.get_tree()):
            if node.get("focused"):
                return self._window(node)
        return None

    def _command(self, window, command):
        """Run a sway command scoped to one window."""
        return self.transport.run_command(f"[con_id={self._handle(window)}] {command}")

    def activate_window(self, window):
        """Raise and focus a window."""
        self.require(Capability.WINDOW_ACTIVATE)
        return self._command(window, "focus")

    def move_window(self, window, x, y):
        """Move a window's top-left corner to (x, y).

        `floating enable` first, chained via "," so it shares this call's
        `[con_id=]` criteria: confirmed live that a freshly opened window is
        tiled by default, where `move absolute position` silently no-ops
        against the tiling layout instead of placing it. Already-floating
        windows are unaffected -- sway's own `floating enable` is a no-op
        once a container is floating.
        """
        self.require(Capability.WINDOW_PLACEMENT)
        return self._command(window, f"floating enable, move absolute position {x} {y}")

    def resize_window(self, window, width, height):
        """Resize a window to `width` by `height`.

        `floating enable` first -- see `move_window`; a tiled window's
        `resize set` is constrained by its siblings rather than by this
        call's request.
        """
        self.require(Capability.WINDOW_RESIZE)
        return self._command(window, f"floating enable, resize set {width} {height}")

    def minimize_window(self, window, minimized=True):
        """Minimize via the scratchpad.

        sway has no minimize state -- the scratchpad is its equivalent, a
        hidden holding area. Restoring raises the window on the current
        workspace, which is close to but not identical to un-minimizing in
        place.
        """
        self.require(Capability.WINDOW_MINIMIZE)
        return self._command(
            window, "move scratchpad" if minimized else "scratchpad show"
        )

    def _subscription_transport(self):
        """A transport dedicated to this one subscription.

        The socket transport supports either request/reply or an event
        stream at a time, not both -- once subscribe() puts it in streaming
        mode, the next windows() or geometry() call on the *same* connection
        reads an event frame as its own reply. A CLI transport already
        spawns its own `swaymsg -t subscribe` process per call, so only the
        socket needs a second connection; opening one fails safely back to
        sharing the original rather than breaking the subscription outright.
        """
        path = getattr(self.transport, "path", None)
        if path is not None:
            from .. import ipc

            try:
                return ipc.SwaySocket(path=path)
            except OSError:
                pass
        return self.transport

    def window_events(self, timeout=None):
        """Yield WindowEvents as the compositor reports them.

        Replaces the polling behind WaitWindowLike and WaitWindowClose. The
        audit called this the one window capability Wayland does better than
        X11: the compositor pushes, so there is no interval to tune and no
        race between polls.

        `timeout`, in seconds, bounds the whole subscription; the generator
        simply ends (rather than raising) once it expires. None waits
        indefinitely.
        """
        self.require(Capability.WINDOW_EVENTS)
        subscriber = self._subscription_transport()
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            for payload in subscriber.subscribe(["window"], deadline=deadline):
                container = payload.get("container") or {}
                yield WindowEvent(
                    change=payload.get("change", ""),
                    window=Window(
                        handle=container.get("id"),
                        backend=self,
                        title=container.get("name") or "",
                        app_id=container.get("app_id") or "",
                        pid=container.get("pid"),
                    ),
                )
        finally:
            if subscriber is not self.transport:
                subscriber.close()

    def wait_for_window(self, title, timeout=None):
        """Block until a window whose title matches `title` appears.

        Checks existing windows first, so one already open is not missed --
        the race the polled version had. `timeout` is a bound in seconds on
        the total wait; None (the default) blocks indefinitely. Previously
        accepted and silently ignored -- an editor that never launched hung
        the caller forever with no way out.
        """
        self.require(Capability.WINDOW_EVENTS)
        pattern = re.compile(title)
        for existing in self.windows():
            if pattern.search(existing.title):
                return existing
        for event in self.window_events(timeout=timeout):
            if event.change in ("new", "title") and pattern.search(event.window.title):
                return event.window
        return None

    def screens(self):
        """Every output, with size and scale."""
        self.require(Capability.SCREEN_INFO)
        return [
            Screen(
                index=i,
                width=o["rect"]["width"],
                height=o["rect"]["height"],
                scale=o.get("scale", 1.0),
                name=o.get("name", ""),
            )
            for i, o in enumerate(self.transport.get_outputs())
        ]


_LUA_DISPATCH_REFUSALS = (
    "hl.dispatch",
    "non-legacy parsers",
    "expected near",
    "syntax might need to be updated",
)
"""Fragments of Hyprland's reply when it parsed a dispatch as Lua and failed.

Matched as substrings of the daemon's own error text rather than against a
version number: the two syntaxes are told apart by asking, which keeps
working across whatever the next Hyprland does to its parser.
"""


def _refused_as_lua(reply):
    """Whether `reply` is Hyprland complaining that a dispatch was not Lua."""
    text = str(reply or "").lower()
    return any(marker in text for marker in _LUA_DISPATCH_REFUSALS)


class HyprlandBackend(_WindowBackend):
    """Hyprland, over its request socket."""

    name = "hyprland"

    def __init__(self, transport):
        """Drive Hyprland through `transport`, a socket or CLI adapter."""
        self.transport = transport
        self._lua_dispatch = None
        """Whether this daemon wants dispatches as Lua: None until asked.

        Set by the first `_dispatch`, from the daemon's own answer rather
        than a version check. See `_dispatch`.
        """

    def close(self):
        """Close the transport."""
        self.transport.close()

    @property
    def capabilities(self):
        # No event subscription yet: Hyprland streams events on a second
        # socket, which this does not speak.
        """Full window control; no event subscription yet."""
        return CapabilitySet(_COMMON)

    def _window(self, client):
        """Build a Window from a Hyprland client record."""
        return Window(
            handle=client["address"],
            backend=self,
            title=client.get("title") or "",
            app_id=client.get("class") or "",
            pid=client.get("pid"),
        )

    def windows(self):
        """Every open window."""
        self.require(Capability.WINDOW_LIST)
        return [self._window(c) for c in self.transport.clients()]

    def geometry(self, window):
        """A window's (x, y, width, height) in screen coordinates."""
        self.require(Capability.WINDOW_GEOMETRY)
        handle = self._handle(window)
        for client in self.transport.clients():
            if client["address"] == handle:
                return (*client["at"], *client["size"])
        raise WindowNotFound(f"no window at address {handle!r}")

    def _shown_workspaces(self):
        """Ids of every workspace some monitor is displaying, or None.

        A monitor shows its `activeWorkspace` and, when one is open, a
        `specialWorkspace`; a closed special reports id 0, which is no
        workspace. None where no monitor reports a workspace at all -- an
        older daemon, or a transport that does not carry the field -- so
        the caller can tell "nothing is shown" from "nothing is known".
        """
        shown = set()
        known = False
        for monitor in self.transport.monitors() or []:
            for key in ("activeWorkspace", "specialWorkspace"):
                workspace = monitor.get(key)
                if not isinstance(workspace, dict) or "id" not in workspace:
                    continue
                known = True
                if workspace["id"]:
                    shown.add(workspace["id"])
        return shown if known else None

    def _viewable(self, client, shown):
        """Whether one client record is on screen right now.

        `mapped` false is a window the user cannot see at all; `hidden` true
        is Hyprland's flag for a window folded away inside a group's tabs.
        Neither says anything about *workspaces*: measured live on 0.56.2, a
        window moved to an inactive workspace, or minimized onto a closed
        special workspace, reports mapped=true hidden=false and keeps its
        full rectangle, so reading those two fields alone called a minimized
        window viewable. The third test is whether some monitor is
        displaying the window's workspace -- skipped only where the daemon
        reports no workspaces to compare against.
        """
        if not client.get("mapped", True) or client.get("hidden", False):
            return False
        workspace = client.get("workspace")
        if shown is None or not isinstance(workspace, dict) or "id" not in workspace:
            return True
        return workspace["id"] in shown

    def is_window_viewable(self, window):
        """Whether `window` is currently visible: mapped, not folded, shown.

        See `_viewable` for what each of the three means and why the
        workspace is part of it.
        """
        self.require(Capability.WINDOW_STATE)
        handle = self._handle(window)
        shown = self._shown_workspaces()
        for client in self.transport.clients():
            if client["address"] == handle:
                return self._viewable(client, shown)
        raise WindowNotFound(f"no window at address {handle!r}")

    def _windows_with_geometry(self):
        """One clients() call, reused for the whole window list and rects.

        Only windows that are on screen take part in the hit-test: a window
        minimized onto a special workspace, or sitting on an inactive one,
        keeps its last rectangle in `clients`, and would otherwise be
        answered for a point that visibly has a different window under it.
        """
        shown = self._shown_workspaces()
        return [
            (self._window(c), (*c["at"], *c["size"]))
            for c in self.transport.clients()
            if self._viewable(c, shown)
        ]

    def active_window(self):
        """The focused window, or None."""
        self.require(Capability.WINDOW_STATE)
        client = self.transport.active_window()
        if not client or not client.get("address"):
            return None
        return self._window(client)

    def _dispatch(self, window, command, params="", lua=None):
        """Send a dispatch request targeting one window, in this daemon's syntax.

        The legacy syntax is `<command> <params>,address:0x...` with no space
        after the comma, so `params` must already end with one when present.
        Current Hyprland parses a dispatch as Lua and rejects that form --
        `focuswindow address:0x...` comes back as "')' expected near
        'address'" -- so `lua` carries the same request as a Lua dispatcher
        expression, built from the target selector.

        Which one to send is discovered, not assumed. The legacy string goes
        out first; if the reply is a Lua parse complaint
        (`_refused_as_lua`), this daemon is Lua-based, that is remembered,
        and the Lua form is sent instead and for every later call. A rejected
        legacy request changes nothing on the daemon, so discovery costs one
        wasted request on a modern Hyprland and nothing on an older one.

        Found when a torture-test run that had passed the day before failed
        with 27 interactions that never reported: `activate_window` was
        answering with an error string instead of focusing, so no window had
        focus and every injected key went nowhere.
        """
        target = f"address:{self._handle(window)}"
        legacy = f"{command} {params}{target}"
        if self._lua_dispatch is None:
            reply = self.transport.dispatch(legacy)
            if not _refused_as_lua(reply):
                self._lua_dispatch = False
                return self._checked(legacy, reply)
            self._lua_dispatch = True
        request = lua(target) if self._lua_dispatch and lua is not None else legacy
        return self._checked(request, self.transport.dispatch(request))

    @staticmethod
    def _checked(request, reply):
        """Return `reply` if the daemon accepted `request`, else raise.

        Hyprland answers a dispatch with the single word "ok" when it did
        what was asked, in both the legacy and the Lua syntax; anything
        else is its account of why it did not -- "warning: =[C]:-1:
        hl.focus: window not found", "error: ..." (measured live on
        0.56.2), "Invalid dispatcher" on older daemons. Nothing above this
        method reads the reply, so before this check a refused request was
        a silent no-op: the call returned and the window stayed where it
        was, and the first symptom was three layers away, as keys typed
        into a window that had never been focused. A window the daemon
        cannot find is `WindowNotFound`, as everywhere else in this module.
        """
        text = str(reply or "").strip()
        if text.lower() == "ok":
            return reply
        if "not found" in text.lower():
            raise WindowNotFound(f"hyprland: {text} (request: {request})")
        raise PyGUITestError(f"hyprland refused {request!r}: {text or 'no reply'}")

    def activate_window(self, window):
        """Raise and focus a window."""
        self.require(Capability.WINDOW_ACTIVATE)
        return self._dispatch(
            window,
            "focuswindow",
            lua=lambda target: f'hl.dsp.focus({{ window = "{target}" }})',
        )

    def _float(self, window):
        """Take a window out of the tiling layout so it can be placed.

        A tiled window's position and size belong to the layout, and
        Hyprland answers "ok" to a pixel move or resize of one without
        changing anything -- measured live on 0.56.2, where a freshly
        opened foot stayed at (12, 38) through `window.move` until it was
        floated, then went exactly where it was told. Same fact and same
        fix as `SwayBackend.move_window`'s `floating enable`. Already
        floating windows are unaffected.
        """
        return self._dispatch(
            window,
            "setfloating",
            lua=lambda target: (
                f'hl.dsp.window.float({{ action = "on", window = "{target}" }})'
            ),
        )

    def move_window(self, window, x, y):
        """Move a window's top-left corner to (x, y), floating it first."""
        self.require(Capability.WINDOW_PLACEMENT)
        self._float(window)
        return self._dispatch(
            window,
            "movewindowpixel",
            f"exact {x} {y},",
            lua=lambda target: (
                f'hl.dsp.window.move({{ x = {x}, y = {y}, window = "{target}" }})'
            ),
        )

    def resize_window(self, window, width, height):
        """Resize a window to `width` by `height`, floating it first.

        `hl.dsp.window.resize` takes the *size* as x and y, which reads oddly
        next to `move` taking a position in the same two names, but is what
        the dispatcher accepts: "Expected positions (x & y) or
        keep_aspect_ratio", verified live resizing to 900x700.
        """
        self.require(Capability.WINDOW_RESIZE)
        self._float(window)
        return self._dispatch(
            window,
            "resizewindowpixel",
            f"exact {width} {height},",
            lua=lambda target: (
                f"hl.dsp.window.resize({{ x = {width}, y = {height}, "
                f'window = "{target}" }})'
            ),
        )

    def minimize_window(self, window, minimized=True):
        """Minimize by moving to a special workspace.

        Hyprland has no minimize state either. Restoring brings the window to
        the *active* workspace, not the one it came from -- the original is
        not recorded.

        Restoring resolves the active workspace id via activeworkspace and
        dispatches the plain (non-silent) movetoworkspace to it. The
        previous form -- movetoworkspacesilent with "e+0" -- had two bugs at
        once: "silent" moves the window without showing it, which is the
        opposite of what a restore should do, and "e+0" is the
        relative-next-empty-workspace selector, not "the current one".
        Falls back to that old form if activeworkspace is unavailable on an
        older Hyprland, rather than failing outright.

        In the Lua syntax "silent" is spelled `follow = false`, which is
        what Hyprland's own and Omarchy's own bindings use. It is not
        `silent = true`: the dispatcher ignores a key it does not know, and
        a move that is not told otherwise *follows* the window -- so with
        the wrong key a focused window minimized straight into an open
        special workspace, still on screen. Measured live on 0.56.2, where
        `follow = false` leaves the monitor's specialWorkspace at id 0.
        """
        self.require(Capability.WINDOW_MINIMIZE)
        if minimized:
            return self._dispatch(
                window,
                "movetoworkspacesilent",
                "special:minimized,",
                lua=lambda target: (
                    'hl.dsp.window.move({ workspace = "special:minimized", '
                    f'follow = false, window = "{target}" }})'
                ),
            )
        try:
            workspace = self.transport.active_workspace()["id"]
        except (AttributeError, KeyError, TypeError):
            return self._dispatch(
                window,
                "movetoworkspacesilent",
                "e+0,",
                lua=lambda target: (
                    'hl.dsp.window.move({ workspace = "e+0", follow = false, '
                    f'window = "{target}" }})'
                ),
            )
        return self._dispatch(
            window,
            "movetoworkspace",
            f"{workspace},",
            lua=lambda target: (
                f'hl.dsp.window.move({{ workspace = "{workspace}", '
                f'window = "{target}" }})'
            ),
        )

    def screens(self):
        """Every monitor, with size and scale."""
        self.require(Capability.SCREEN_INFO)
        return [
            Screen(
                index=i,
                width=m["width"],
                height=m["height"],
                scale=m.get("scale", 1.0),
                name=m.get("name", ""),
            )
            for i, m in enumerate(self.transport.monitors())
        ]


class NiriBackend(_WindowBackend):
    """niri, over its JSON IPC socket.

    The one backend here that cannot place a window. niri is a scrolling
    tiler: a window's position falls out of the layout, so there is no
    action that moves one to a coordinate, and the capability set says so
    rather than failing at call time. Resizing is a different matter --
    SetWindowWidth and SetWindowHeight work on tiled windows -- which is
    why WINDOW_RESIZE exists apart from WINDOW_PLACEMENT.

    There is no minimize either, and no scratchpad to stand in for one the
    way sway and Hyprland have.
    """

    name = "niri"

    def __init__(self, transport):
        """Drive niri through `transport`, a socket or CLI adapter."""
        self.transport = transport

    def close(self):
        """Close the transport."""
        self.transport.close()

    @property
    def capabilities(self):
        """Everything but placement and minimize, plus an event stream."""
        return CapabilitySet(
            (_COMMON - {Capability.WINDOW_PLACEMENT, Capability.WINDOW_MINIMIZE})
            | {Capability.WINDOW_RESIZE, Capability.WINDOW_EVENTS}
        )

    def _window(self, record):
        """Build a Window from a niri window record."""
        return Window(
            handle=record["id"],
            backend=self,
            title=record.get("title") or "",
            app_id=record.get("app_id") or "",
            pid=record.get("pid"),
        )

    def windows(self):
        """Every open window."""
        self.require(Capability.WINDOW_LIST)
        return [self._window(r) for r in self.transport.windows()]

    def _output_origins(self):
        """Workspace id -> the (x, y) origin of the output holding it.

        niri reports a window's position within its workspace view, not on
        the screen, so turning one into a screen coordinate needs the
        logical origin of the output that workspace lives on. Workspaces
        name their output; outputs carry the origin.
        """
        outputs = self.transport.outputs() or {}
        origins = {}
        for workspace in self.transport.workspaces() or []:
            logical = (outputs.get(workspace.get("output")) or {}).get("logical") or {}
            origins[workspace.get("id")] = (logical.get("x", 0), logical.get("y", 0))
        return origins

    @staticmethod
    def _rect(record, origin):
        """(x, y, width, height) for one window, or None if off-view.

        tile_pos_in_workspace_view is null for a window scrolled out of the
        current view or sitting on an inactive workspace -- niri genuinely
        does not have a position for it, so there is nothing to report.

        It is also null for an ordinary tiled window that *is* on screen,
        measured on niri 26.04, which is why `is_window_viewable` no longer
        reads this field. The consequence for callers that need a rectangle
        is unavoidable rather than hidden: `geometry` refuses with the
        reason, and `window_at` can answer None over a window that is
        visibly there, because a window with no rectangle cannot be
        hit-tested. Nothing here invents one.
        """
        layout = record.get("layout") or {}
        tile = layout.get("tile_pos_in_workspace_view")
        if tile is None:
            return None
        width, height = layout.get("window_size") or (0, 0)
        offset_x, offset_y = layout.get("window_offset_in_tile") or (0, 0)
        return (
            int(origin[0] + tile[0] + offset_x),
            int(origin[1] + tile[1] + offset_y),
            int(width),
            int(height),
        )

    def geometry(self, window):
        """A window's (x, y, width, height) in screen coordinates."""
        self.require(Capability.WINDOW_GEOMETRY)
        handle = self._handle(window)
        origins = self._output_origins()
        for record in self.transport.windows():
            if record["id"] == handle:
                rect = self._rect(
                    record, origins.get(record.get("workspace_id"), (0, 0))
                )
                if rect is None:
                    raise CapabilityUnsupported(
                        Capability.WINDOW_GEOMETRY,
                        self.name,
                        f"niri reports no position for window {handle!r} "
                        "(tile_pos_in_workspace_view is null), so there is "
                        "nothing to answer with. That happens for a window "
                        "scrolled out of the view or on an inactive "
                        "workspace -- and, measured on niri 26.04, for an "
                        "ordinary tiled window that is on screen, so it does "
                        "not by itself mean the window is hidden: ask "
                        "is_window_viewable for that",
                    )
                return rect
        raise WindowNotFound(f"no window with id {handle!r}")

    def is_window_viewable(self, window):
        """Whether `window` is currently visible.

        A tile position means visible. The absence of one is ambiguous, and
        the two causes need opposite answers, so it is not read alone.

        The assumption used to be that niri gives a tile a position within
        the workspace view exactly when it is on screen. Live niri disproved
        it: on 26.04 a kitty window that was focused, alone on its output,
        on the active workspace and plainly visible reported
        `tile_pos_in_workspace_view: null`, so reading that field answered
        False for the window the user was looking at -- a wrong answer, not
        a refusal. Reading the workspace instead gets the other case wrong,
        because a window scrolled out of the view sits on an active
        workspace just the same.

        So a null is taken as "scrolled away" only where niri gave some
        other window on that workspace a position, which shows the field is
        in use; where nothing on the workspace has one, the field is telling
        us nothing and the question becomes whether that workspace is the
        one being displayed.
        """
        self.require(Capability.WINDOW_STATE)
        handle = self._handle(window)
        records = self.transport.windows()
        for record in records:
            if record["id"] != handle:
                continue
            layout = record.get("layout") or {}
            if layout.get("tile_pos_in_workspace_view") is not None:
                return True
            workspace_id = record.get("workspace_id")
            if any(
                (other.get("layout") or {}).get("tile_pos_in_workspace_view")
                is not None
                for other in records
                if other.get("workspace_id") == workspace_id
            ):
                # niri is populating the field on this workspace, so a null
                # here means this window really is scrolled out of the view.
                return False
            # Nothing on the workspace has a position: the field is not in
            # use on this niri, so it says nothing about this window. Fall
            # back to whether the workspace is the one being displayed.
            for workspace in self.transport.workspaces() or []:
                if workspace.get("id") == workspace_id:
                    return bool(workspace.get("is_active"))
            return False
        raise WindowNotFound(f"no window with id {handle!r}")

    def _windows_with_geometry(self):
        """One windows() call and one origin lookup for the whole hit-test.

        Windows niri reports no position for are dropped rather than given a
        fake rectangle. Usually that is because they cannot be under the
        pointer, being scrolled away or on another workspace; on a niri that
        reports no position for tiled windows in view (26.04, measured) it
        means the hit-test has nothing to offer at all and answers None. A
        wrong rectangle would be worse than no answer -- see `_rect`.
        """
        origins = self._output_origins()
        result = []
        for record in self.transport.windows():
            rect = self._rect(record, origins.get(record.get("workspace_id"), (0, 0)))
            if rect is not None:
                result.append((self._window(record), rect))
        return result

    def active_window(self):
        """The focused window, or None."""
        self.require(Capability.WINDOW_STATE)
        for record in self.transport.windows():
            if record.get("is_focused"):
                return self._window(record)
        return None

    def activate_window(self, window):
        """Raise and focus a window."""
        self.require(Capability.WINDOW_ACTIVATE)
        return self.transport.action("FocusWindow", id=self._handle(window))

    def resize_window(self, window, width, height):
        """Resize a window to `width` by `height`.

        Two actions, because niri sizes the axes separately. SetFixed is
        the absolute arm of its SizeChange enum; the others adjust.
        """
        self.require(Capability.WINDOW_RESIZE)
        handle = self._handle(window)
        self.transport.action(
            "SetWindowWidth", id=handle, change={"SetFixed": int(width)}
        )
        return self.transport.action(
            "SetWindowHeight", id=handle, change={"SetFixed": int(height)}
        )

    def window_events(self, timeout=None):
        """Yield WindowEvents as niri reports them.

        niri's event names are mapped onto the same verbs the sway backend
        yields -- "new", "close", "focus", "title" -- so wait_for_window and
        every caller above it stay backend-agnostic.

        WindowOpenedOrChanged is one event for both cases, and niri does not
        say which it was. It is reported as "new" the first time a given id
        is seen on this stream and "title" afterwards, which is what a
        caller waiting for a window or for a title change actually needs.
        """
        self.require(Capability.WINDOW_EVENTS)
        deadline = None if timeout is None else time.monotonic() + timeout
        known = set()
        for payload in self.transport.event_stream(deadline=deadline):
            if not isinstance(payload, dict) or len(payload) != 1:
                continue
            name, body = next(iter(payload.items()))
            body = body or {}
            if name == "WindowOpenedOrChanged":
                record = body.get("window") or {}
                handle = record.get("id")
                change = "title" if handle in known else "new"
                known.add(handle)
                yield WindowEvent(change=change, window=self._window(record))
            elif name == "WindowClosed":
                handle = body.get("id")
                known.discard(handle)
                yield WindowEvent(
                    change="close", window=Window(handle=handle, backend=self)
                )
            elif name == "WindowFocusChanged":
                handle = body.get("id")
                if handle is None:  # focus moved to a layer-shell surface
                    continue
                yield WindowEvent(
                    change="focus", window=Window(handle=handle, backend=self)
                )

    def wait_for_window(self, title, timeout=None):
        """Block until a window whose title matches `title` appears."""
        self.require(Capability.WINDOW_EVENTS)
        pattern = re.compile(title)
        for existing in self.windows():
            if pattern.search(existing.title):
                return existing
        for event in self.window_events(timeout=timeout):
            if event.change in ("new", "title") and pattern.search(event.window.title):
                return event.window
        return None

    def screens(self):
        """Every output, with size and scale.

        Outputs arrive as a name -> record mapping rather than a list, and
        a disabled output has no logical rectangle at all, so it is skipped
        rather than reported as a zero-sized screen.
        """
        self.require(Capability.SCREEN_INFO)
        screens: list[Screen] = []
        for name, output in sorted((self.transport.outputs() or {}).items()):
            logical = output.get("logical")
            if not logical:
                continue
            screens.append(
                Screen(
                    index=len(screens),
                    width=logical.get("width", 0),
                    height=logical.get("height", 0),
                    scale=logical.get("scale", 1.0),
                    name=output.get("name", name),
                )
            )
        return screens


class KdotoolBackend(_WindowBackend):
    """KDE Plasma, via kdotool.

    The one place a CLI adapter is still the right answer: KWin exposes no
    simple socket, and kdotool already implements the D-Bus scripting dance.
    Output is xdotool-style text, and window ids are KWin UUIDs in braces.
    """

    name = "kdotool"

    def __init__(self, runner=None):
        """Drive kdotool, optionally through an injected `runner`."""
        self._runner = runner or self._run

    def _run(self, argv):
        """Run `argv` and return stdout, raising if it fails or hangs."""
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=DEFAULT_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise PyGUITestError(f"{' '.join(argv)} timed out") from exc
        if result.returncode != 0:
            raise PyGUITestError(
                f"{' '.join(argv)} failed ({result.returncode}): "
                f"{result.stderr.strip() or 'no output'}"
            )
        return result.stdout

    @property
    def capabilities(self):
        """Window control minus pid lookup, outputs, and events."""
        return CapabilitySet(_COMMON - {Capability.WINDOW_PID, Capability.SCREEN_INFO})

    def _name_of(self, handle):
        """Return the title of the window with this handle."""
        return (self._runner(["kdotool", "getwindowname", handle]) or "").strip()

    def _lines(self, argv):
        """Run `argv` and return its non-empty output lines."""
        output = self._runner(argv) or ""
        return [line.strip() for line in output.splitlines() if line.strip()]

    def windows(self):
        """Every open window."""
        self.require(Capability.WINDOW_LIST)
        return [
            Window(
                handle=handle,
                backend=self,
                title=self._name_of(handle),
                app_id=self._app_id_of(handle),
            )
            for handle in self._lines(["kdotool", "search", "."])
        ]

    def geometry(self, window):
        """Parse xdotool-style geometry output.

        Window {uuid}
          Position: 100,200 (screen: 0)
          Geometry: 800x600

        KWin can report a position mid-animation as a fraction of a pixel
        (confirmed live: "545,274.5403238932292"), so each component is
        parsed as a float and rounded rather than handed straight to int().
        """
        self.require(Capability.WINDOW_GEOMETRY)
        handle = self._handle(window)
        x = y = width = height = None
        for line in self._lines(["kdotool", "getwindowgeometry", handle]):
            if line.startswith("Position:"):
                coords = line.split(":", 1)[1].split("(")[0].strip()
                x, y = (round(float(v)) for v in coords.split(","))
            elif line.startswith("Geometry:"):
                size = line.split(":", 1)[1].strip()
                width, height = (round(float(v)) for v in size.split("x"))
        if None in (x, y, width, height):
            raise WindowNotFound(f"no geometry for window {handle!r}")
        return (x, y, width, height)

    def active_window(self):
        """The focused window, or None."""
        self.require(Capability.WINDOW_STATE)
        handle = (self._runner(["kdotool", "getactivewindow"]) or "").strip()
        if not handle:
            return None
        return Window(
            handle=handle,
            backend=self,
            title=self._name_of(handle),
            app_id=self._app_id_of(handle),
        )

    def is_window_viewable(self, window):
        """Not available: kdotool has no mapped/visibility query.

        WINDOW_STATE is still declared for active_window's sake -- capabilities
        are coarser than this one verb, so the honest per-operation refusal
        has to happen here rather than by withholding the capability, which
        would also hide active_window.
        """
        raise CapabilityUnsupported(
            Capability.WINDOW_STATE,
            self.name,
            "kdotool has no mapped/visibility query",
        )

    def _act(self, window, command, *args):
        """Run a kdotool command against one window."""
        return self._runner(["kdotool", command, self._handle(window), *args])

    def activate_window(self, window):
        """Raise and focus a window."""
        self.require(Capability.WINDOW_ACTIVATE)
        return self._act(window, "windowactivate")

    def move_window(self, window, x, y):
        """Move a window's top-left corner to (x, y)."""
        self.require(Capability.WINDOW_PLACEMENT)
        return self._act(window, "windowmove", str(x), str(y))

    def resize_window(self, window, width, height):
        """Resize a window to `width` by `height`."""
        self.require(Capability.WINDOW_RESIZE)
        return self._act(window, "windowsize", str(width), str(height))

    def minimize_window(self, window, minimized=True):
        """Minimize a window, or restore it when `minimized` is False."""
        self.require(Capability.WINDOW_MINIMIZE)
        return self._act(window, "windowminimize" if minimized else "windowactivate")

    def _app_id_of(self, handle):
        """Return a window's application identity from `getwindowclassname`.

        Mirrors `X11Backend._app_id`: this is the same field sway and
        Hyprland already call `app_id`, kept stable across a title that
        drifts. On KWin it does not distinguish a shell's own multiple
        surfaces (e.g. plasmashell's desktop, panels, and popups all report
        class "plasmashell") -- callers matching on this alone should not
        assume it is unique among a single application's windows.
        """
        try:
            return (
                self._runner(["kdotool", "getwindowclassname", handle]) or ""
            ).strip()
        except Exception:
            return ""


def for_compositor(compositor):
    """Build the window backend for a Compositor, or None.

    Prefers the socket transport and falls back to the CLI, so a working
    session needs no tool installed.
    """
    import os

    from .. import ipc
    from ..session import Compositor

    if compositor is Compositor.WLROOTS:
        # Select on which compositor's environment signature is actually
        # present, not on which transport happens to connect first and
        # which duck-type it satisfies -- a Hyprland session with swaymsg
        # merely installed would otherwise get a SwayBackend that fails on
        # every call, since connect_sway() was tried first unconditionally.
        if os.environ.get("SWAYSOCK") or os.environ.get("I3SOCK"):
            transport = ipc.connect_sway()
            if transport is not None:
                return SwayBackend(transport)
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            transport = ipc.connect_hyprland()
            if transport is not None:
                return HyprlandBackend(transport)
        if os.environ.get("NIRI_SOCKET"):
            transport = ipc.connect_niri()
            if transport is not None:
                return NiriBackend(transport)
        return None
    if compositor is Compositor.KWIN:
        import shutil

        return KdotoolBackend() if shutil.which("kdotool") else None
    if compositor is Compositor.COSMIC:
        from .cosmic import connect_cosmic

        try:
            return connect_cosmic()
        except OSError:
            return None
    return None


def for_tool(name, runner=None):
    """Legacy tool-name entry point, kept for the CLI fallbacks."""
    from .. import ipc

    if name == "swaymsg":
        return SwayBackend(ipc.SwayCLI(runner=runner))
    if name == "hyprctl":
        return HyprlandBackend(ipc.HyprlandCLI(runner=runner))
    if name == "niri":
        return NiriBackend(ipc.NiriCLI(runner=runner))
    if name == "kdotool":
        return KdotoolBackend(runner=runner)
    return None
