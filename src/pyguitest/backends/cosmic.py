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

import array
import contextlib
import mmap
import os
import re
import select
import socket
import struct
import tempfile
import time
from collections.abc import Iterator
from typing import cast

from ..capabilities import Capability, CapabilitySet
from ..errors import CapabilityUnsupported, WindowNotFound
from ..ipc import DEFAULT_TIMEOUT
from ..png import write_rgb
from .base import Screen, Window, check_region, fit_region
from .windows import WindowEvent, _WindowBackend

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
# corrected with the output's own origin. geometry, mode, scale and name feed
# screens(); done and description are read and discarded.

_WOUT_GEOMETRY = 0
_WOUT_MODE = 1
_WOUT_DONE = 2
_WOUT_SCALE = 3
_WOUT_NAME = 4

_WOUT_MODE_CURRENT = 0x1
"""The wl_output.mode flag naming the mode currently in use."""

_WOUT_VERSION = 4
"""The wl_output version this client binds (the highest, for name/description)."""

# -- ext-image-copy-capture-v1 / ext-image-capture-source-v1 ---------------
# COSMIC's screen and window capture, verified live on Pop!_OS 24.04. The two
# source managers wrap an opaque ext_image_capture_source_v1; the copy-capture
# manager turns a source into a session that fills client buffers (see
# _capture_frame). Opcodes are the order requests/events appear in
# wayland-protocols (staging), v1.

_CC_CREATE_SESSION = 0
_OUTPUT_CREATE_SOURCE = 0
_TOPLEVEL_CREATE_SOURCE = 0
_SOURCE_DESTROY = 0
"""`ext_image_capture_source_v1.destroy` -- the opaque handle both source
managers wrap has no other request, the same single-purpose shape as the
session and frame objects below, both of which really do put `destroy` at 0."""
_CCS_CREATE_FRAME = 0
_CCS_DESTROY = 1
_CCS_BUFFER_SIZE = 0
_CCS_SHM_FORMAT = 1
_CCS_DONE = 4
_CCF_DESTROY = 0
_CCF_ATTACH_BUFFER = 1
_CCF_DAMAGE_BUFFER = 2
_CCF_CAPTURE = 3
_CCF_READY = 3
_CCF_FAILED = 4

# -- wl_shm / wl_shm_pool / wl_buffer --------------------------------------
_SHM_CREATE_POOL = 0
_SHM_POOL_CREATE_BUFFER = 0
_SHM_POOL_DESTROY = 1
_BUFFER_DESTROY = 0

# The 8-bit shm formats COSMIC advertises, both BGR order: the low byte is R,
# so a pixel is [R, G, B, A/X] in memory. The 10-bit variants ('AB30'/'XB30')
# are skipped in favour of these; either 8-bit one decodes identically.
_SHM_8BIT_FORMATS = frozenset({0x34324241, 0x34324258})  # ABGR8888, XBGR8888

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


def _bgr_rows(raw: bytes, width: int, height: int) -> Iterator[bytes]:
    """Yield RGB scanlines from a BGR-order 32bpp capture buffer.

    COSMIC's 8-bit shm formats store each pixel as [R, G, B, A/X], so a
    stride-3 scanline is the buffer's bytes 0, 1 and 2 of every 4. Slicing
    rather than a per-pixel loop keeps a 4K capture from spending a second in
    Python on the colour shuffle.
    """
    stride = width * 4
    for y in range(height):
        row = raw[y * stride : (y + 1) * stride]
        out = bytearray(width * 3)
        out[0::3] = row[0::4]
        out[1::3] = row[1::4]
        out[2::3] = row[2::4]
        yield bytes(out)


def _crop_bgra(
    raw: bytes, width: int, height: int, x: int, y: int, rw: int, rh: int
) -> bytes:
    """Crop a buffer-relative (x, y, rw, rh) out of a 32bpp BGRA buffer.

    Raises for a rectangle that would fall outside the captured output; a
    caller asking for a region a single-output capture cannot cover is a
    capability gap, not something to silently return a plausible-looking
    image of the wrong thing for.
    """
    if x < 0 or y < 0 or x + rw > width or y + rh > height:
        raise CapabilityUnsupported(
            Capability.SCREEN_CAPTURE,
            "cosmic",
            f"region {(x, y, rw, rh)} extends outside the captured "
            f"{width}x{height} output",
        )
    stride = width * 4
    out = bytearray(rw * rh * 4)
    for row in range(rh):
        src = (y + row) * stride + x * 4
        dst = row * rw * 4
        out[dst : dst + rw * 4] = raw[src : src + rw * 4]
    return bytes(out)


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


class _Output:
    """One bound wl_output's properties: origin, size, scale and name."""

    __slots__ = ("x", "y", "width", "height", "scale", "name")

    def __init__(self) -> None:
        self.x = 0
        self.y = 0
        self.width = 0
        self.height = 0
        self.scale = 1
        self.name = ""


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
        self._outputs: dict[int, _Output] = {}
        self._registry = 0
        self._info = 0
        self._manager = 0
        self._list = 0
        self._seat = 0
        self._shm = 0
        self._capture_mgr = 0
        self._output_capture_mgr = 0
        self._toplevel_capture_mgr = 0
        self._finished = False
        self._events: list[tuple[str, int]] = []
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

    def _send_fd(
        self, object_id: int, opcode: int, fields: list[tuple[str, object]], fd: int
    ) -> None:
        """Send one message whose payload carries a file descriptor.

        `wl_shm.create_pool` takes its fd out-of-band over SCM_RIGHTS, with no
        fd field in the message body itself, which is why `_encode` does not
        know the kind.
        """
        self._sock.sendmsg(
            [_encode(object_id, opcode, fields)],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))],
        )

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
            self._outputs[object_id] = _Output()

    def _handshake(self) -> None:
        self._registry = self._allocate("wl_registry")
        self._send(_DISPLAY, _GET_REGISTRY, [("n", self._registry)])
        self._roundtrip()  # fills self._globals from the registry.global events

        self._info = self._bind("zcosmic_toplevel_info_v1", 3)
        self._manager = self._bind("zcosmic_toplevel_manager_v1", 4)
        self._list = self._bind("ext_foreign_toplevel_list_v1", 1)
        self._seat = self._bind("wl_seat", 1)
        # Capture is optional: an older COSMIC without the copy-capture
        # protocols still gets window control, just no screenshots.
        self._shm = self._bind("wl_shm", 2)
        self._capture_mgr = self._bind("ext_image_copy_capture_manager_v1", 1)
        self._output_capture_mgr = self._bind(
            "ext_output_image_capture_source_manager_v1", 1
        )
        self._toplevel_capture_mgr = self._bind(
            "ext_foreign_toplevel_image_capture_source_manager_v1", 1
        )
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
        # The initial listing's own `toplevel`/`title` events landed above as
        # recorded events, but they describe windows that already existed when
        # this connection opened -- not changes a window_events() subscriber
        # should see. Discard them now that the listing is known complete.
        self._events.clear()

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

    def _wait_for_event(self, timeout: float | None) -> bool:
        """Read until a window event is queued, or `timeout` seconds pass.

        Returns True if an event is now in `_events`; the caller drains it.
        `timeout` of None blocks until an event arrives.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._events:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            readable, _, _ = select.select([self._sock], [], [], remaining)
            if not readable:
                return False
            object_id, opcode, payload = self._recv_message()
            self._handle(object_id, opcode, payload)
        return True

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
            self._events.append(("new", handle))
        elif opcode == _EFL_FINISHED:
            self._finished = True

    def _handle_foreign(self, object_id: int, opcode: int, payload: bytes) -> None:
        toplevel = self._toplevels[object_id]
        reader = _Reader(payload)
        if opcode == _EFH_CLOSED:
            toplevel.closed = True
            self._events.append(("close", object_id))
        elif opcode == _EFH_TITLE:
            toplevel.title = reader.string()
            self._events.append(("title", object_id))
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
            output = self._outputs.get(output_id)
            origin_x, origin_y = (output.x, output.y) if output else (0, 0)
            toplevel.geometry = (x + origin_x, y + origin_y, width, height)
        elif opcode == _ZTH_OUTPUT_ENTER:
            toplevel.outputs.add(reader.obj())
        elif opcode == _ZTH_OUTPUT_LEAVE:
            toplevel.outputs.discard(reader.obj())

    def _handle_output(self, object_id: int, opcode: int, payload: bytes) -> None:
        """Record geometry, mode, scale and name; ignore done/description.

        `mode` reports every mode the output supports, so only the one flagged
        `current` updates the size `screens()` reports.
        """
        output = self._outputs[object_id]
        reader = _Reader(payload)
        if opcode == _WOUT_GEOMETRY:
            output.x, output.y = reader.int_(), reader.int_()
        elif opcode == _WOUT_MODE:
            flags = reader.uint()
            width, height = reader.int_(), reader.int_()
            if flags & _WOUT_MODE_CURRENT:
                output.width, output.height = width, height
        elif opcode == _WOUT_SCALE:
            output.scale = reader.int_()
        elif opcode == _WOUT_NAME:
            output.name = reader.string()

    # -- capture -----------------------------------------------------------

    def _create_output_source(self, output_id: int) -> int:
        """An ext_image_capture_source_v1 for one wl_output."""
        source = self._allocate("ext_image_capture_source_v1")
        self._send(
            self._output_capture_mgr,
            _OUTPUT_CREATE_SOURCE,
            [("n", source), ("o", output_id)],
        )
        return source

    def _create_toplevel_source(self, handle: int) -> int:
        """An ext_image_capture_source_v1 for one foreign toplevel handle."""
        source = self._allocate("ext_image_capture_source_v1")
        self._send(
            self._toplevel_capture_mgr,
            _TOPLEVEL_CREATE_SOURCE,
            [("n", source), ("o", handle)],
        )
        return source

    def _capture_frame(self, source_id: int) -> tuple[int, int, bytes]:
        """Capture one frame from `source_id`, returning (width, height, bgra).

        One pass through ext-image-copy-capture-v1: create a session over the
        source, read the buffer constraints, fill a memfd shm buffer, attach it
        to one frame, capture, and read the pixels back. Events for any other
        object are dispatched to `_handle` as they arrive, so the toplevel and
        output caches stay fresh during the wait.

        Takes ownership of `source_id`: both call sites in `capture()` create
        one just for this call and never touch it again, so this is also
        where it gets destroyed. Every protocol object this method allocates
        -- session, pool, buffer, frame, and `source_id` itself -- is
        destroyed here before returning or raising, child-before-parent
        (frame, then buffer, then pool, then session, then source); without
        this a long run calling `screenshot()` repeatedly leaked all of them
        on the compositor side, a shm pool the size of the screen included.
        """
        session = self._allocate("ext_image_copy_capture_session_v1")
        self._send(
            self._capture_mgr,
            _CC_CREATE_SESSION,
            [("n", session), ("o", source_id), ("u", 0)],
        )
        pending_destroy: list[tuple[int, int]] = [
            (session, _CCS_DESTROY),
            (source_id, _SOURCE_DESTROY),
        ]
        try:
            width = height = 0
            fmt = 0
            while True:
                object_id, opcode, payload = self._recv_message()
                if object_id == session:
                    reader = _Reader(payload)
                    if opcode == _CCS_BUFFER_SIZE:
                        width, height = reader.uint(), reader.uint()
                    elif opcode == _CCS_SHM_FORMAT:
                        fourcc = reader.uint()
                        if fmt == 0 and fourcc in _SHM_8BIT_FORMATS:
                            fmt = fourcc
                    elif opcode == _CCS_DONE:
                        break
                    # dmabuf_device/dmabuf_format are ignored; a stopped
                    # session surfaces as the frame never becoming ready below.
                else:
                    self._handle(object_id, opcode, payload)
            if not width or not height or not fmt:
                raise OSError("COSMIC offered no 8-bit shm format for capture")

            stride = width * 4
            size = stride * height
            fd = os.memfd_create("pyguitest-cosmic-capture", 0)
            try:
                os.ftruncate(fd, size)
                pool = self._allocate("wl_shm_pool")
                self._send_fd(
                    self._shm, _SHM_CREATE_POOL, [("n", pool), ("i", size)], fd
                )
                pending_destroy.insert(0, (pool, _SHM_POOL_DESTROY))
                buffer = self._allocate("wl_buffer")
                self._send(
                    pool,
                    _SHM_POOL_CREATE_BUFFER,
                    [
                        ("n", buffer),
                        ("i", 0),
                        ("i", width),
                        ("i", height),
                        ("i", stride),
                        ("u", fmt),
                    ],
                )
                pending_destroy.insert(0, (buffer, _BUFFER_DESTROY))
                frame = self._allocate("ext_image_copy_capture_frame_v1")
                self._send(session, _CCS_CREATE_FRAME, [("n", frame)])
                pending_destroy.insert(0, (frame, _CCF_DESTROY))
                self._send(frame, _CCF_ATTACH_BUFFER, [("o", buffer)])
                self._send(
                    frame,
                    _CCF_DAMAGE_BUFFER,
                    [("i", 0), ("i", 0), ("i", width), ("i", height)],
                )
                self._send(frame, _CCF_CAPTURE, [])
                while True:
                    object_id, opcode, payload = self._recv_message()
                    if object_id == frame:
                        if opcode == _CCF_READY:
                            break
                        if opcode == _CCF_FAILED:
                            raise OSError(
                                "COSMIC capture failed "
                                f"(reason {_Reader(payload).uint()})"
                            )
                        continue  # transform/damage/presentation_time: ignore
                    self._handle(object_id, opcode, payload)
                data = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ)
                try:
                    return width, height, bytes(data)
                finally:
                    data.close()
            finally:
                os.close(fd)
        finally:
            for object_id, opcode in pending_destroy:
                with contextlib.suppress(OSError):
                    self._send(object_id, opcode, [])

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()


_COSMIC_CAPABILITIES = frozenset(
    {
        Capability.SCREEN_INFO,
        Capability.WINDOW_LIST,
        Capability.WINDOW_STATE,
        Capability.WINDOW_GEOMETRY,
        Capability.WINDOW_ACTIVATE,
        Capability.WINDOW_MINIMIZE,
        Capability.WINDOW_AT_POINT,
        Capability.WINDOW_EVENTS,
    }
)
"""What COSMIC's own protocols always serve.

SCREEN_CAPTURE and WINDOW_CAPTURE are added by the backend's `capabilities`
property when the copy-capture globals are present, since those are optional
on an older COSMIC. Not WINDOW_PLACEMENT/WINDOW_RESIZE (the protocols have no
move/resize request) and not WINDOW_PID (no pid is published)."""


class CosmicBackend(_WindowBackend):
    """Window control for COSMIC through its own Wayland protocols."""

    name = "cosmic"

    def __init__(self, connection: _CosmicConnection) -> None:
        """Drive `connection`, an already-handshaken Wayland client."""
        self._conn = connection

    @property
    def capabilities(self) -> CapabilitySet:
        """Window control, plus capture when the copy-capture globals exist."""
        caps = set(_COSMIC_CAPABILITIES)
        if self._conn._capture_mgr and self._conn._shm:
            if self._conn._output_capture_mgr and self._conn._outputs:
                caps.add(Capability.SCREEN_CAPTURE)
            if self._conn._toplevel_capture_mgr:
                caps.add(Capability.WINDOW_CAPTURE)
        return CapabilitySet(caps)

    def close(self) -> None:
        """Close the Wayland connection."""
        self._conn.close()

    def screens(self) -> list[Screen]:
        """Every output, in advertised order."""
        self.require(Capability.SCREEN_INFO)
        return [
            Screen(
                index=index,
                width=output.width,
                height=output.height,
                scale=output.scale,
                name=output.name,
            )
            for index, output in enumerate(self._conn._outputs.values())
        ]

    def window_events(self, timeout=None):
        """Yield WindowEvents as COSMIC pushes them.

        `timeout` bounds the whole subscription in seconds; the generator ends
        (rather than raising) once it expires. None waits indefinitely.
        """
        self.require(Capability.WINDOW_EVENTS)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._conn._events:
                change, handle = self._conn._events.pop(0)
                toplevel = self._conn._toplevels.get(handle)
                if toplevel is not None:
                    yield WindowEvent(
                        change=change, window=self._window(handle, toplevel)
                    )
                continue
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return
            if not self._conn._wait_for_event(remaining):
                return

    def wait_for_window(self, title, timeout=None):
        """Block until a window whose title matches `title` appears.

        Checks existing windows first, so one already open is not missed.
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

    def _output_for_region(
        self, region: tuple[int, int, int, int]
    ) -> tuple[int, tuple[int, int, int, int]] | None:
        """The output serving `region`, with the region fitted to it, or None.

        A region can land on any output, not just the first one `capture()`
        would otherwise default to -- cropping it out of the wrong output's
        buffer either raises `_crop_bgra`'s own out-of-bounds error on a
        multi-output desktop where the region would actually fit on a
        *different* output, or, worse, silently returns pixels from the
        wrong screen if the coordinates happen to overlap by chance.

        An output containing the region exactly wins, unchanged. Failing
        that, one pixel of overhang -- what `check_region`'s covering can
        add to an edge-flush fractional region -- is fitted to whichever
        output comes within a pixel of containing it, via the shared
        `fit_region` policy. Anything further outside every output is
        None, refused by the caller as before.

        One honest consequence on a multi-output desktop: a region that
        genuinely straddles an output boundary by up to a pixel resolves to
        one of the two outputs rather than being refused, dropping that
        pixel of the other. The fraction is gone by the time this runs, so a
        one-pixel straddle and a one-pixel covering overhang are the same
        integer rectangle -- the indistinguishability `fit_region` documents,
        here against a neighbour that has content rather than a screen edge
        that does not. It is bounded to that one pixel, and is the same
        tradeoff the single-output edge case already makes.
        """
        x, y, width, height = region
        for output_id, output in self._conn._outputs.items():
            if (
                output.x <= x
                and output.y <= y
                and x + width <= output.x + output.width
                and y + height <= output.y + output.height
            ):
                return output_id, region
        for output_id, output in self._conn._outputs.items():
            relative = (x - output.x, y - output.y, width, height)
            try:
                fitted = fit_region(relative, (output.width, output.height), "output")
            except ValueError:
                continue
            return output_id, (
                fitted[0] + output.x,
                fitted[1] + output.y,
                fitted[2],
                fitted[3],
            )
        return None

    def capture(self, window=None, path=None, region=None):
        """Screenshot the desktop, one window, or one rectangle.

        A region is captured from whichever bound output actually contains
        it, cropped out of that output's own buffer in memory. A whole-screen
        capture (no `window`, no `region`) is the first bound output only --
        on a multi-output desktop that is one monitor, not the whole desktop;
        compositing every output into one image is not implemented. A window
        is captured through its own toplevel source, which this connection
        holds for exactly the Window handles it hands out.
        """
        self.require(
            Capability.WINDOW_CAPTURE
            if window is not None
            else Capability.SCREEN_CAPTURE
        )
        region = check_region(region, window)
        if window is not None:
            handle = self._handle(window)
            self._toplevel(window)  # raises WindowNotFound if it is gone
            source = self._conn._create_toplevel_source(handle)
            width, height, raw = self._conn._capture_frame(source)
        else:
            if region is not None:
                match = self._output_for_region(region)
                if match is None:
                    # ValueError, not CapabilityUnsupported: a region outside
                    # every output is the caller's mistake, not a failure of
                    # this backend's mechanism, and it would fail identically
                    # on every capture member. CompositeBackend._capture
                    # caches a PyGUITestError and blacklists the member that
                    # raised it for the rest of the session, so raising one
                    # here would let a single out-of-bounds region
                    # permanently disable COSMIC capture; a ValueError
                    # propagates uncached, exactly as the x11 fit does.
                    raise ValueError(
                        f"region {tuple(region)} is not fully contained in any "
                        "single bound output: capturing it would return pixels "
                        "from the wrong screen or a smaller image instead of "
                        "the rectangle asked for"
                    )
                output_id, region = match
            else:
                output_id = next(iter(self._conn._outputs))
            source = self._conn._create_output_source(output_id)
            width, height, raw = self._conn._capture_frame(source)
            if region is not None:
                x, y, rw, rh = region
                output = self._conn._outputs[output_id]
                raw = _crop_bgra(raw, width, height, x - output.x, y - output.y, rw, rh)
                width, height = rw, rh
        if path is None:
            descriptor, path = tempfile.mkstemp(suffix=".png")
            os.close(descriptor)
        write_rgb(path, width, height, _bgr_rows(raw, width, height))
        return path


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
