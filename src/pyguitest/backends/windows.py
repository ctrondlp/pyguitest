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
        """Move a window's top-left corner to (x, y)."""
        self.require(Capability.WINDOW_PLACEMENT)
        return self._command(window, f"move absolute position {x} {y}")

    def resize_window(self, window, width, height):
        """Resize a window to `width` by `height`."""
        self.require(Capability.WINDOW_RESIZE)
        return self._command(window, f"resize set {width} {height}")

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


class HyprlandBackend(_WindowBackend):
    """Hyprland, over its request socket."""

    name = "hyprland"

    def __init__(self, transport):
        """Drive Hyprland through `transport`, a socket or CLI adapter."""
        self.transport = transport

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

    def is_window_viewable(self, window):
        """Whether `window` is currently visible.

        Hyprland's own client fields: "mapped" false means invisible to the
        user outright; "hidden" true means mapped but not currently shown
        (e.g. on an inactive special workspace). Viewable needs both.
        """
        self.require(Capability.WINDOW_STATE)
        handle = self._handle(window)
        for client in self.transport.clients():
            if client["address"] == handle:
                return bool(client.get("mapped", True)) and not client.get(
                    "hidden", False
                )
        raise WindowNotFound(f"no window at address {handle!r}")

    def _windows_with_geometry(self):
        """One clients() call, reused for the whole window list and rects."""
        return [
            (self._window(c), (*c["at"], *c["size"])) for c in self.transport.clients()
        ]

    def active_window(self):
        """The focused window, or None."""
        self.require(Capability.WINDOW_STATE)
        client = self.transport.active_window()
        if not client or not client.get("address"):
            return None
        return self._window(client)

    def _dispatch(self, window, command, params=""):
        """Send a dispatch request targeting one window.

        Hyprland's syntax is `<command> <params>,address:0x...` with no space
        after the comma, so `params` must already end with one when present.
        """
        target = f"address:{self._handle(window)}"
        return self.transport.dispatch(f"{command} {params}{target}")

    def activate_window(self, window):
        """Raise and focus a window."""
        self.require(Capability.WINDOW_ACTIVATE)
        return self._dispatch(window, "focuswindow")

    def move_window(self, window, x, y):
        """Move a window's top-left corner to (x, y)."""
        self.require(Capability.WINDOW_PLACEMENT)
        return self._dispatch(window, "movewindowpixel", f"exact {x} {y},")

    def resize_window(self, window, width, height):
        """Resize a window to `width` by `height`."""
        self.require(Capability.WINDOW_RESIZE)
        return self._dispatch(window, "resizewindowpixel", f"exact {width} {height},")

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
        """
        self.require(Capability.WINDOW_MINIMIZE)
        if minimized:
            return self._dispatch(window, "movetoworkspacesilent", "special:minimized,")
        try:
            workspace = self.transport.active_workspace()["id"]
        except (AttributeError, KeyError, TypeError):
            return self._dispatch(window, "movetoworkspacesilent", "e+0,")
        return self._dispatch(window, "movetoworkspace", f"{workspace},")

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
                        f"window {handle!r} is scrolled out of the workspace "
                        "view, and niri reports no position for one",
                    )
                return rect
        raise WindowNotFound(f"no window with id {handle!r}")

    def is_window_viewable(self, window):
        """Whether `window` is currently visible.

        Read from the same field geometry depends on: niri gives a tile a
        position within the workspace view exactly when it is on screen.
        """
        self.require(Capability.WINDOW_STATE)
        handle = self._handle(window)
        for record in self.transport.windows():
            if record["id"] == handle:
                layout = record.get("layout") or {}
                return layout.get("tile_pos_in_workspace_view") is not None
        raise WindowNotFound(f"no window with id {handle!r}")

    def _windows_with_geometry(self):
        """One windows() call and one origin lookup for the whole hit-test.

        Off-view windows are dropped rather than given a fake rectangle:
        they cannot be under the pointer.
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
                argv, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT
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
        return Window(handle=handle, backend=self, title=self._name_of(handle))

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
