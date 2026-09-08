"""Mutter's output geometry, without the shell extension.

`org.gnome.Mutter.DisplayConfig` is core Mutter, and it answers
`GetCurrentState` to any session-bus client unprompted: no extension, no
portal, no consent dialog. That makes it the one source of output geometry a
GNOME session -- Wayland or X11 -- can be asked for before anything else
exists, which is why this is a module rather than a method on
GnomeShellBackend, where it used to live and where its own comment said it
did not belong.

Two callers, and they hold it differently on purpose. `GnomeShellBackend.
screens()` keeps a proxy across calls and turns a failure into a typed
CapabilityUnsupported, because a caller asked it a question. `_screen_size`
in this package's `__init__` has no backend and no caller to tell -- it has
to fix a uinput device's absolute axes before any backend is built -- so it
asks once with a throwaway proxy and reads silence as an answer. What they
share is the arithmetic, which is where the mistakes are.

Sizes are derived rather than read: GetCurrentState reports the panel's pixel
mode and the logical monitor's scale and transform separately, and it is the
logical size -- the units the global coordinate space is actually made of --
that a click coordinate is measured in.
"""

from __future__ import annotations

BUS_NAME = "org.gnome.Mutter.DisplayConfig"
"""Mutter's own output interface, which doubles as the object's interface."""

OBJECT_PATH = "/org/gnome/Mutter/DisplayConfig"

_LAYOUT_MODE_LOGICAL = 1
"""GetCurrentState's `layout-mode`: logical (1) or physical (2).

It decides what a logical monitor's size *means*, so it cannot be ignored.
Under logical layout -- GNOME's default, and what fractional scaling needs
-- the monitor occupies `mode / scale` units of the global coordinate
space; under physical layout it occupies the mode size and the scale
applies only to rendering. Reporting the mode size in both cases would put
these numbers in a different coordinate space from `geometry()` on exactly
the desktops that scale."""


def _gio():
    """Import Gio, or return None.

    The same PyGObject dependency the atspi extra already needs -- see
    docs/developers/adr-001-dependencies.md -- so this adds nothing new to
    install.
    """
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio
    except Exception:
        return None
    return Gio


def new_proxy():
    """A D-Bus proxy bound to Mutter's DisplayConfig.

    Raises: RuntimeError if PyGObject is not installed, and whatever Gio
    raises if the session bus cannot be reached. Both are left to the
    caller to describe, since what they mean depends on who asked.
    """
    Gio = _gio()
    if Gio is None:
        raise RuntimeError("PyGObject is not installed")
    return Gio.DBusProxy.new_for_bus_sync(
        Gio.BusType.SESSION,
        Gio.DBusProxyFlags.NONE,
        None,
        BUS_NAME,
        OBJECT_PATH,
        BUS_NAME,
        None,
    )


def read(proxy=None):
    """Mutter's current display state, unpacked.

    `(serial, monitors, logical_monitors, properties)`, exactly as
    GetCurrentState nests it. Builds a throwaway proxy when the caller has
    none to lend.
    """
    Gio = _gio()
    if Gio is None:
        raise RuntimeError("PyGObject is not installed")
    if proxy is None:
        proxy = new_proxy()
    reply = proxy.call_sync("GetCurrentState", None, Gio.DBusCallFlags.NONE, -1, None)
    return reply.unpack()


def _current_modes(monitors):
    """Map each connector to the (width, height) it is displaying now.

    A monitor advertises every mode it can do and marks the live one
    with `is-current`; a monitor that is switched off marks none, and
    is simply absent from the result.
    """
    sizes = {}
    for spec, modes, _properties in monitors:
        for _id, width, height, _refresh, _preferred, _scales, flags in modes:
            if flags.get("is-current"):
                sizes[spec[0]] = (width, height)
                break
    return sizes


def logical_monitors(state):
    """Every enabled logical monitor as (x, y, width, height, scale, name).

    Logical monitors, not physical ones. A logical monitor is what the
    global coordinate space is made of, so this is the same space
    `geometry()` and `window_at()` answer in -- which is the point, since a
    caller who centres a click from one and a rectangle from the other is
    mixing the two. It also means a mirrored pair is one entry (named after
    the first connector, the one Mutter lists first) and a disabled output
    is none at all, matching how NiriBackend skips an output with no
    logical rect.

    Size is derived rather than read: the logical size is the panel's
    current mode divided by the scale (under logical layout mode -- see
    _LAYOUT_MODE_LOGICAL) with the axes swapped on a 90- or 270-degree
    rotation. A logical monitor whose panel reports no current mode is
    skipped, since inventing a size for it would be worse than leaving it
    out.
    """
    _serial, monitors, logical, properties = state
    scaled = properties.get("layout-mode", _LAYOUT_MODE_LOGICAL) == _LAYOUT_MODE_LOGICAL
    sizes = _current_modes(monitors)

    found = []
    for x, y, scale, transform, _primary, specs, _properties in logical:
        size = next((sizes[s[0]] for s in specs if s[0] in sizes), None)
        if size is None:
            continue
        width, height = size
        # Transforms 1/3 are 90/270 degrees and 5/7 are those flipped;
        # all four are odd, and all four swap the axes.
        if transform % 2:
            width, height = height, width
        if scaled and scale:
            width, height = round(width / scale), round(height / scale)
        found.append((x, y, width, height, scale, specs[0][0] if specs else ""))
    return found


def layout_size():
    """The whole layout's bounding box as (width, height), or None.

    Best-effort by design. The caller this exists for is sizing a uinput
    device's absolute axes and has both a fallback of its own and nobody to
    report to, so every way of not knowing -- no PyGObject, no session bus,
    not a GNOME session at all, a Mutter that answers something this cannot
    unpack -- comes back as None rather than as an exception.

    Measured from the origin, like the wlroots branch beside it: an
    absolute pointer device is mapped onto the layout starting at (0, 0),
    and Mutter normalises the layout to that corner.
    """
    try:
        monitors = logical_monitors(read())
    except Exception:
        return None
    if not monitors:
        return None
    return (
        max(x + width for x, _y, width, _height, _scale, _name in monitors),
        max(y + height for _x, y, _width, height, _scale, _name in monitors),
    )
