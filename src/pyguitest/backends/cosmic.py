"""Window control for COSMIC (Pop!_OS 24.04) over its own Wayland protocols.

COSMIC -- System76's Smithay compositor -- speaks none of the protocols the
other window backends drive: not sway/i3's IPC, not Hyprland's socket, not
niri's IPC, not KWin's scripting API. What it does speak is a pair of its own
unprivileged Wayland globals: `zcosmic_toplevel_info_v1` (listing, state,
geometry) and `zcosmic_toplevel_manager_v1` (activate, minimize, close). No
CLI wraps them (no kdotool/wmctrl equivalent exists for COSMIC), so this
backend is a small Wayland *client*: it connects to the Wayland socket and
speaks the wire protocol directly, stdlib only, the same way `ipc.py` speaks
the i3-ipc framing over a unix socket.

The two globals are layered on the standard `ext_foreign_toplevel_list_v1`.
At version 2+ of the info protocol the `toplevel` event is gone; titles and
app ids arrive on the foreign toplevel handle, while state and geometry arrive
on the COSMIC extension handle obtained with `get_cosmic_toplevel`. This
backend binds both and tracks the two handles of each window as one
`_Toplevel`.

Geometry comes from the `zcosmic_toplevel_handle_v1.geometry` event, whose
x/y are *output-relative*. The backend also binds one `wl_output` per
advertised output -- the event is only emitted relative to outputs this
client has bound -- and adds each output's own origin, so the coordinates it
reports are absolute screen coordinates even on a multi-output desktop. The
management protocol's `close` request has no corresponding capability here
(`Capability` has no WINDOW_CLOSE), so it is bound but not surfaced.
"""

from __future__ import annotations

import contextlib
import os
import select
import socket
import struct
import time
from typing import cast

from ..capabilities import Capability, CapabilitySet
from ..errors import WindowNotFound
from ..ipc import DEFAULT_TIMEOUT
from .base import Window
from .windows import _WindowBackend

__all__ = ["CosmicBackend", "connect_cosmic"]

_AF_UNIX: int = getattr(socket, "AF_UNIX", -1)
"""The Unix socket address family, looked up rather than named.

Mirrors the same lookup in `ipc.py`: CPython does not define
`socket.AF_UNIX` on Windows, and naming it would break a Windows type check
for a code path no Windows machine runs (COSMIC is a Linux compositor).
Where it is missing this is -1, and the connection attempt fails with an
`OSError` rather than an `AttributeError`."""

# -- core protocol (wayland.xml) -------------------------------------------
# Opcode numbering is the order requests/events appear in the XML. Requests and
# events are numbered independently; both start at zero.

_DISPLAY = 1
"""`wl_display`'s object id, fixed by the protocol."""

_SYNC = 0
_GET_REGISTRY = 1
_REGISTRY_BIND = 0
_REGISTRY_GLOBAL = 0
_CALLBACK_DONE = 0

# -- ext_foreign_toplevel_list_v1 ------------------------------------------
# The standard protocol COSMIC's info protocol extends. Only the request and
# event opcodes this client sends or reads are named.

_EFL_TOPLEVEL = 0
_EFL_FINISHED = 1
_EFH_CLOSED = 0
_EFH_DONE = 1
_EFH_TITLE = 2
_EFH_APP_ID = 3
_EFH_IDENTIFIER = 4

# -- zcosmic_toplevel_info_v1 / zcosmic_toplevel_handle_v1 -----------------

_ZTI_GET_COSMIC_TOPLEVEL = 1
_ZTH_OUTPUT_ENTER = 4
_ZTH_OUTPUT_LEAVE = 5
_ZTH_STATE = 8
_ZTH_GEOMETRY = 9

# -- zcosmic_toplevel_manager_v1 -------------------------------------------

_ZTM_ACTIVATE = 2
_ZTM_SET_MINIMIZED = 5
_ZTM_UNSET_MINIMIZED = 6

# -- state array entries, shared with ext_foreign_toplevel_list's own enum -

_STATE_MAXIMIZED = 0
_STATE_MINIMIZED = 1
_STATE_ACTIVATED = 2
_STATE_FULLSCREEN = 3

# -- wl_output (wayland.xml) -----------------------------------------------
# Bound so the toplevel geometry event -- emitted only relative to outputs the
# client has bound -- arrives at all, and so its output-relative x/y can be
# corrected with the output's own origin. Only the events this client reads
# are named; mode, done, scale, name and description are read and discarded.

_WOUT_GEOMETRY = 0
_WOUT_MODE = 1
_WOUT_DONE = 2
_WOUT_SCALE = 3

_WOUT_VERSION = 4
"""The wl_output version this client binds (the highest, for name/description)."""

_REFRESH_DRAIN = 0.2
"""Seconds to wait for cosmic-comp's rate-limited refresh to deliver the
initial state and geometry. cosmic-comp caps that cycle at 150 ms (src/lib.rs),
so 200 ms covers one cycle without ever feeling like a hang."""


def _pad(length: int) -> int:
    """Bytes of padding needed to reach the next 4-byte word boundary."""
    return (4 - (length & 3)) & 3


def _encode(object_id: int, opcode: int, fields: list[tuple[str, object]]) -> bytes:
    """Marshal one Wayland message.

    `fields` is a list of `(kind, value)` pairs; `kind` is one of "u" (uint),
    "i" (int), "o" (object), "n" (new_id) or "s" (string). Only the kinds the
    COSMIC requests use are needed -- no arrays or fds are ever sent.

    The header's second word packs the total message length in *bytes* (the
    8-byte header included) above the opcode -- libwayland writes
    `size << 16 | opcode` with `size` in bytes, not words. Getting this wrong
    makes every message a protocol error; a live run against COSMIC caught it
    where the socketpair test could not, because its fake used the same words.
    """
    body = bytearray()
    for kind, value in fields:
        if kind in ("u", "o", "n"):
            body += struct.pack("<I", value)
        elif kind == "i":
            body += struct.pack("<i", value)
        elif kind == "s":
            raw = cast(str, value).encode("utf-8") + b"\0"
            body += struct.pack("<I", len(raw))
            body += raw
            body += b"\0" * _pad(len(raw))
        else:
            raise ValueError(f"unknown wire field kind {kind!r}")
    size = 8 + len(body)
    return struct.pack("<II", object_id, (size << 16) | opcode) + bytes(body)


class _Reader:
    """Unmarshal arguments from one received message payload."""

    __slots__ = ("_data", "_off")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._off = 0

    def uint(self) -> int:
        value = struct.unpack_from("<I", self._data, self._off)[0]
        self._off += 4
        return value

    def int_(self) -> int:
        value = struct.unpack_from("<i", self._data, self._off)[0]
        self._off += 4
        return value

    def obj(self) -> int:
        """An object reference, which is a uint32 on the wire."""
        return self.uint()

    def string(self) -> str:
        length = self.uint()
        raw = self._data[self._off : self._off + length]
        self._off += length + _pad(length)
        return raw.rstrip(b"\0").decode("utf-8", "replace")

    def u32_array(self) -> list[int]:
        """An array of uint32 -- the state and capabilities event's shape."""
        length = self.uint()
        raw = self._data[self._off : self._off + length]
        self._off += length + _pad(length)
        return list(struct.unpack("<" + "I" * (length // 4), raw))


class _Toplevel:
    """One window's accumulated properties, across its two protocol handles."""

    __slots__ = (
        "title",
        "app_id",
        "identifier",
        "states",
        "geometry",
        "outputs",
        "cosmic",
        "closed",
    )

    def __init__(self) -> None:
        self.title = ""
        self.app_id = ""
        self.identifier: str | None = None
        self.states: set[int] = set()
        self.geometry: tuple[int, int, int, int] | None = None
        self.outputs: set[int] = set()
        self.cosmic: int | None = None
        """The zcosmic_toplevel_handle_v1 id, once `get_cosmic_toplevel` is sent."""
        self.closed = False


class _CosmicConnection:
    """A minimal synchronous Wayland client for COSMIC's toplevel globals.

    Owns the socket, allocates object ids, performs the registry handshake,
    and turns the compositor's push events into a dict of `_Toplevel` records.
    Each public read drains any pending events with a `wl_display.sync`
    round-trip, so queries see the compositor's latest state.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._next_id = 1
        self._globals: dict[int, tuple[str, int]] = {}
        self._toplevels: dict[int, _Toplevel] = {}
        self._cosmic_handle: dict[int, int] = {}
        self._outputs: dict[int, tuple[int, int]] = {}
        self._registry = 0
        self._info = 0
        self._manager = 0
        self._list = 0
        self._seat = 0
        self._finished = False
        self._handshake()

    # -- wire primitives ---------------------------------------------------

    def _allocate(self, interface: str) -> int:
        """The next client-allocated object id.

        `interface` is accepted and unused so call sites name what they are
        creating; the id itself is just the next sequential value.
        """
        self._next_id += 1
        return self._next_id

    def _send(
        self, object_id: int, opcode: int, fields: list[tuple[str, object]]
    ) -> None:
        self._sock.sendall(_encode(object_id, opcode, fields))

    def _recv_exact(self, count: int) -> bytes:
        chunks = []
        remaining = count
        while remaining:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise OSError("the compositor closed the Wayland connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _recv_message(self) -> tuple[int, int, bytes]:
        header = self._recv_exact(8)
        object_id, size_opcode = struct.unpack("<II", header)
        size = size_opcode >> 16
        opcode = size_opcode & 0xFFFF
        payload = self._recv_exact(size - 8)
        return object_id, opcode, payload

    def _roundtrip(self) -> None:
        """Flush a sync and read until its callback completes.

        Dispatching as it goes: Wayland events are ordered, so every event the
        compositor had queued before seeing the sync arrives before the
        callback's `done`.
        """
        callback = self._allocate("wl_callback")
        self._send(_DISPLAY, _SYNC, [("n", callback)])
        while True:
            object_id, opcode, payload = self._recv_message()
            if object_id == callback and opcode == _CALLBACK_DONE:
                return
            self._handle(object_id, opcode, payload)

    # -- handshake ---------------------------------------------------------

    def _bind(self, interface: str, version: int) -> int:
        """Bind one advertised global, or 0 if this compositor lacks it."""
        for name, (iface, advertised) in self._globals.items():
            if iface == interface:
                object_id = self._allocate(interface)
                self._send(
                    self._registry,
                    _REGISTRY_BIND,
                    [
                        ("u", name),
                        ("s", interface),
                        ("u", min(version, advertised)),
                        ("n", object_id),
                    ],
                )
                return object_id
        return 0

    def _advertised_version(self, interface: str) -> int:
        for iface, advertised in self._globals.values():
            if iface == interface:
                return advertised
        return 0

    def _bind_outputs(self) -> None:
        """Bind one wl_output object per advertised output, origin unknown.

        cosmic-comp only emits a toplevel's geometry (and output_enter)
        relative to outputs this client has bound -- a client that binds none
        receives no geometry at all -- and the geometry event's x/y are
        output-relative. Each bound output's own origin, read from
        wl_output.geometry, is added back later to make the reported
        coordinates absolute.
        """
        for name, (interface, advertised) in list(self._globals.items()):
            if interface != "wl_output":
                continue
            object_id = self._allocate("wl_output")
            self._send(
                self._registry,
                _REGISTRY_BIND,
                [
                    ("u", name),
                    ("s", "wl_output"),
                    ("u", min(_WOUT_VERSION, advertised)),
                    ("n", object_id),
                ],
            )
            self._outputs[object_id] = (0, 0)

    def _handshake(self) -> None:
        self._registry = self._allocate("wl_registry")
        self._send(_DISPLAY, _GET_REGISTRY, [("n", self._registry)])
        self._roundtrip()  # fills self._globals from the registry.global events

        self._info = self._bind("zcosmic_toplevel_info_v1", 3)
        self._manager = self._bind("zcosmic_toplevel_manager_v1", 4)
        self._list = self._bind("ext_foreign_toplevel_list_v1", 1)
        self._seat = self._bind("wl_seat", 1)
        self._bind_outputs()

        if not self._info or not self._manager or not self._list:
            raise OSError(
                "this compositor does not advertise COSMIC's window protocols"
            )
        if self._advertised_version("zcosmic_toplevel_info_v1") < 2:
            raise OSError(
                "this compositor's zcosmic_toplevel_info_v1 predates version 2, "
                "where geometry was added"
            )
        self._sync_toplevels()

    def _sync_toplevels(self) -> None:
        """Drain events and request COSMIC handles for any new toplevels.

        `get_cosmic_toplevel` is sent only after the foreign handle exists, so
        this loops until a pass adds no new handles. Bounded: a compositor that
        kept announcing toplevels forever would otherwise spin. The initial
        state and geometry for those new handles are then emitted on
        cosmic-comp's rate-limited refresh cycle, not in reply to
        `get_cosmic_toplevel`, so a request pass is followed by a bounded wait
        for the properties to land.
        """
        requested = False
        for _ in range(4):
            self._roundtrip()
            pending = [
                handle
                for handle, toplevel in self._toplevels.items()
                if toplevel.cosmic is None and not toplevel.closed
            ]
            if not pending:
                break
            requested = True
            for handle in pending:
                cosmic = self._allocate("zcosmic_toplevel_handle_v1")
                self._toplevels[handle].cosmic = cosmic
                self._cosmic_handle[cosmic] = handle
                self._send(
                    self._info,
                    _ZTI_GET_COSMIC_TOPLEVEL,
                    [("n", cosmic), ("o", handle)],
                )
        if requested:
            self._wait_for_properties()

    def _wait_for_properties(self) -> None:
        """Wait (bounded) for the refresh cycle to deliver state and geometry.

        cosmic-comp sends each toplevel's initial `state` and `geometry` from
        its rate-limited refresh loop (capped at 150 ms), not in reply to
        `get_cosmic_toplevel`, so the sync round-trips above complete before
        the properties land. Block until every non-closed toplevel has a
        geometry -- the last property to arrive -- or `_REFRESH_DRAIN` seconds
        pass, dispatching events as they arrive. Returns immediately when
        nothing is missing, so a window that never reports geometry costs one
        bounded wait, not one per refresh.
        """
        deadline = time.monotonic() + _REFRESH_DRAIN
        while True:
            if all(
                toplevel.closed or toplevel.geometry is not None
                for toplevel in self._toplevels.values()
            ):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            readable, _, _ = select.select([self._sock], [], [], remaining)
            if not readable:
                return
            object_id, opcode, payload = self._recv_message()
            self._handle(object_id, opcode, payload)

    # -- event dispatch ----------------------------------------------------

    def _handle(self, object_id: int, opcode: int, payload: bytes) -> None:
        if object_id == self._registry:
            if opcode == _REGISTRY_GLOBAL:
                reader = _Reader(payload)
                name = reader.uint()
                interface = reader.string()
                version = reader.uint()
                self._globals[name] = (interface, version)
            return
        if object_id == self._list:
            self._handle_list(opcode, payload)
            return
        if object_id in self._toplevels:
            self._handle_foreign(object_id, opcode, payload)
            return
        if object_id in self._outputs:
            self._handle_output(object_id, opcode, payload)
            return
        if object_id in self._cosmic_handle:
            self._handle_cosmic(object_id, opcode, payload)

    def _handle_list(self, opcode: int, payload: bytes) -> None:
        if opcode == _EFL_TOPLEVEL:
            handle = _Reader(payload).uint()
            self._toplevels[handle] = _Toplevel()
        elif opcode == _EFL_FINISHED:
            self._finished = True

    def _handle_foreign(self, object_id: int, opcode: int, payload: bytes) -> None:
        toplevel = self._toplevels[object_id]
        reader = _Reader(payload)
        if opcode == _EFH_CLOSED:
            toplevel.closed = True
        elif opcode == _EFH_TITLE:
            toplevel.title = reader.string()
        elif opcode == _EFH_APP_ID:
            toplevel.app_id = reader.string()
        elif opcode == _EFH_IDENTIFIER:
            toplevel.identifier = reader.string()

    def _handle_cosmic(self, object_id: int, opcode: int, payload: bytes) -> None:
        foreign = self._cosmic_handle[object_id]
        toplevel = self._toplevels.get(foreign)
        if toplevel is None:
            return
        reader = _Reader(payload)
        if opcode == _ZTH_STATE:
            toplevel.states = set(reader.u32_array())
        elif opcode == _ZTH_GEOMETRY:
            output_id = reader.obj()
            x, y, width, height = (
                reader.int_(),
                reader.int_(),
                reader.int_(),
                reader.int_(),
            )
            origin_x, origin_y = self._outputs.get(output_id, (0, 0))
            toplevel.geometry = (x + origin_x, y + origin_y, width, height)
        elif opcode == _ZTH_OUTPUT_ENTER:
            toplevel.outputs.add(reader.obj())
        elif opcode == _ZTH_OUTPUT_LEAVE:
            toplevel.outputs.discard(reader.obj())

    def _handle_output(self, object_id: int, opcode: int, payload: bytes) -> None:
        """Record an output's origin from wl_output.geometry; ignore the rest.

        mode, done, scale, name and description carry no coordinate the
        toplevel geometry correction needs, so they are read and discarded.
        """
        if opcode != _WOUT_GEOMETRY:
            return
        reader = _Reader(payload)
        self._outputs[object_id] = (reader.int_(), reader.int_())

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()


_COSMIC_CAPABILITIES = frozenset(
    {
        Capability.WINDOW_LIST,
        Capability.WINDOW_STATE,
        Capability.WINDOW_GEOMETRY,
        Capability.WINDOW_ACTIVATE,
        Capability.WINDOW_MINIMIZE,
        Capability.WINDOW_AT_POINT,
    }
)
"""What COSMIC's own protocols serve, and nothing more.

Not WINDOW_PLACEMENT/WINDOW_RESIZE (the protocols have no move/resize
request), not WINDOW_PID (no pid is published), not SCREEN_INFO (the
wl_output binding is held now, but screen enumeration is not implemented),
not WINDOW_EVENTS (needs a streaming subscription)."""


class CosmicBackend(_WindowBackend):
    """Window control for COSMIC through its own Wayland protocols."""

    name = "cosmic"

    def __init__(self, connection: _CosmicConnection) -> None:
        """Drive `connection`, an already-handshaken Wayland client."""
        self._conn = connection

    @property
    def capabilities(self) -> CapabilitySet:
        """Listing, state, geometry, activate and minimize -- COSMIC's subset."""
        return CapabilitySet(_COSMIC_CAPABILITIES)

    def close(self) -> None:
        """Close the Wayland connection."""
        self._conn.close()

    def _refresh(self) -> None:
        self._conn._sync_toplevels()

    def _window(self, handle: int, toplevel: _Toplevel) -> Window:
        return Window(
            handle=handle,
            backend=self,
            title=toplevel.title,
            app_id=toplevel.app_id,
        )

    def _toplevel(self, window: Window) -> _Toplevel:
        handle = self._handle(window)
        toplevel = self._conn._toplevels.get(handle)
        if toplevel is None or toplevel.closed:
            raise WindowNotFound(f"no window at handle {handle!r}")
        return toplevel

    def _cosmic_of(self, window: Window) -> int:
        """The zcosmic_toplevel_handle_v1 id a management request targets.

        Reads the cache rather than refreshing: the mapping is established when
        the window was first listed, and never changes for a given handle.
        """
        cosmic = self._toplevel(window).cosmic
        if cosmic is None:
            raise WindowNotFound(
                f"window at handle {self._handle(window)!r} has no COSMIC handle"
            )
        return cosmic

    def windows(self) -> list[Window]:
        """Every open window."""
        self.require(Capability.WINDOW_LIST)
        self._refresh()
        return [
            self._window(handle, toplevel)
            for handle, toplevel in self._conn._toplevels.items()
            if not toplevel.closed
        ]

    def active_window(self) -> Window | None:
        """The activated window, or None."""
        self.require(Capability.WINDOW_STATE)
        self._refresh()
        for handle, toplevel in self._conn._toplevels.items():
            if not toplevel.closed and _STATE_ACTIVATED in toplevel.states:
                return self._window(handle, toplevel)
        return None

    def geometry(self, window: Window) -> tuple[int, int, int, int]:
        """(x, y, width, height) from the last geometry event.

        Absolute screen coordinates.
        """
        self.require(Capability.WINDOW_GEOMETRY)
        self._refresh()
        geometry = self._toplevel(window).geometry
        if geometry is None:
            raise WindowNotFound(
                f"no geometry for window at handle {self._handle(window)!r}"
            )
        return geometry

    def is_window_viewable(self, window: Window) -> bool:
        """Whether `window` is not minimized."""
        self.require(Capability.WINDOW_STATE)
        self._refresh()
        return _STATE_MINIMIZED not in self._toplevel(window).states

    def _windows_with_geometry(self) -> list[tuple[Window, tuple[int, int, int, int]]]:
        """One refresh serves both the window list and every rectangle."""
        self._refresh()
        return [
            (self._window(handle, toplevel), toplevel.geometry)
            for handle, toplevel in self._conn._toplevels.items()
            if not toplevel.closed and toplevel.geometry is not None
        ]

    def activate_window(self, window: Window) -> None:
        """Raise and focus a window."""
        self.require(Capability.WINDOW_ACTIVATE)
        cosmic = self._cosmic_of(window)
        self._conn._send(
            self._conn._manager,
            _ZTM_ACTIVATE,
            [("o", cosmic), ("o", self._conn._seat)],
        )

    def minimize_window(self, window: Window, minimized: bool = True) -> None:
        """Minimize a window, or restore it when `minimized` is False."""
        self.require(Capability.WINDOW_MINIMIZE)
        cosmic = self._cosmic_of(window)
        opcode = _ZTM_SET_MINIMIZED if minimized else _ZTM_UNSET_MINIMIZED
        self._conn._send(self._conn._manager, opcode, [("o", cosmic)])


def connect_cosmic() -> CosmicBackend | None:
    """Connect to the Wayland socket and build a CosmicBackend, or None.

    None for anything a plain `connect()` should treat as "no window backend
    here": no Wayland runtime, no socket, a compositor without COSMIC's
    globals, or a connection that fails. The handshake itself raises only
    `OSError`, so the caller can also let a real failure propagate.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return None
    display = os.environ.get("WAYLAND_DISPLAY") or "wayland-0"
    path = os.path.join(runtime, display)
    if not os.path.exists(path):
        return None

    sock = socket.socket(_AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(DEFAULT_TIMEOUT)
    try:
        sock.connect(path)
    except OSError:
        sock.close()
        return None
    try:
        connection = _CosmicConnection(sock)
    except (OSError, struct.error, ValueError):
        sock.close()
        return None
    return CosmicBackend(connection)
