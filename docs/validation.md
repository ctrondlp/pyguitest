# What has actually been run

Most of this package has been exercised against real desktops rather than
only against its own tests. This file records which parts, on what, and —
more usefully — which parts have not, so nothing in the README has to be
read as a claim you cannot check.

## Run live on GNOME Shell 50.4 (Wayland)

- **`eiinput`** (opt-in) — pointer and keyboard injection over libei.
  Pointing at GNOME's Activities button and clicking it opens the overview,
  reliably, and the cursor moves.
- **`gnomeshell`** — window control passed 8 of 8 checks, and per-window
  capture through the Shell extension produced a real PNG. That extension
  is the only prompt-free way to screenshot on this desktop.
- **`gnomeshell`'s `WINDOW_EVENTS`** (extension `0.3.0-events`) — closing a
  real `gedit` window during a live run produced a `title` event followed
  by a `close` event, both correctly attributed, over the real D-Bus
  signal. `wait_for_window`/`window_events` were exercised through this,
  not just constructed. The first two live attempts each crashed a
  *different* line of `validate-gnome-extension.sh`'s own read-only
  checks — the script had captured windows before inviting one to be
  closed, then used the stale reference afterwards (`geometry()` on a
  window closed during the listen, then `is_window_viewable()` on one
  closed the same way) — fixed both times by refreshing the reference or
  tolerating `WindowNotFound` after the listen step. A subsequent run
  passed all 9 checks clean.
- **`portalcapture`** (opt-in) — captured the whole screen correctly.
- **`X11Backend` under XWayland** — per-window capture, which produced a
  correct image of a real window through this package's own PNG encoder.
- **AT-SPI, and the CLI-tool input and capture backends.**

## Run live on a real X11 session

- Whole-screen capture — the one capability a real X11 session has that
  XWayland does not.
- Window control: move, resize, minimize, lower, retitle, hit-test,
  geometry and viewability.
- Four of the five tier-6 capabilities: reading the pointer position, and
  the keyboard and button state.

Every python-xlib call the X11 backend makes has also been checked against
the installed library — names, signatures and return shapes.

## Not run live

- **Input injection through XTest** (the X11 backend's input half).
- **`is_window_cursor`** (WINDOW_CURSOR_QUERY), the fifth tier-6
  capability.
- **The compositor IPC backends** — sway, Hyprland, niri, KWin — and
  **UinputBackend**. Their tests replay recorded output and stand-ins, on a
  sandbox where none of those are available to test against. Running them
  against a live sway/Hyprland/niri/KDE session is next.
- **`gnomeshell`'s `"new"` window-created event.** The live run above
  exercised `"title"` and `"close"` (closing a real `gedit` window); no
  window was opened during that run, so `window-created`/`_watchWindow`'s
  "new" path is still fakes-only, in `tests/test_gnomeshell.py`.
- **`portal`, the input half.** Its CreateSession/SelectDevices/Start
  negotiation has been run against a real xdg-desktop-portal (1.22.1) and
  completes; the keyboard, pointer and scroll methods past that point have
  not. `Start()` raises an interactive consent dialog that blocks until a
  human clicks Allow, so every step beyond it needs a person at a real
  desktop.

`tests/test_portal_dbusmock.py` covers the wire plumbing under that gap. It
uses [python-dbusmock](https://github.com/martinpitt/python-dbusmock) the
way `xdg-desktop-portal`'s own test suite does: a private `dbus-daemon` in a
throwaway temp directory (never the ambient session bus —
`$DBUS_SESSION_BUS_ADDRESS` is saved before the test class runs and restored
after, pass or fail), a fake `org.freedesktop.portal.Desktop` on *that* bus
answering `CreateSession`/`SelectDevices`/`Start` the way an
already-approved real portal would, and `PortalBackend` driven against it
over a genuine Gio connection — real D-Bus calls and a real
`Request`/`Response` signal round trip, not the pure-Python stand-ins
`test_portal.py` uses. Both the happy path and a declined-consent path pass
against a real `dbus-daemon` and real PyGObject. That proves the plumbing
between `PortalBackend` and a portal-shaped service. It proves nothing about
the real daemon, GNOME's or KDE's backend implementation, or the dialog.

## Why the live runs mattered

They found bugs no unit test could have:

- backends declaring capabilities they could not deliver;
- a window handle passed to a backend that could not interpret it;
- a screenshot tool selected on a session where it can only hang;
- three backends leaking a bare exception from a dependency instead of a
  typed one (dogtail's ponytail helper, twice, and a tool's stdout being
  dropped from its own error message);
- two bugs in the GNOME Shell extension that the header-derived code had
  carried from the start — which is why nothing here is claimed from
  headers alone any more.

All are fixed and covered by regression tests. Capture in particular had to
meet a real compositor before it was trustworthy.

## Known caveat: `geometry()` on GNOME's XWayland

`X11Backend`'s `geometry()`, and by extension anything that reads a position
back after `move_window`, may report a window's location wildly wrong under
GNOME/Mutter specifically — confirmed live: `move_window` visibly moves the
window to the right place, but a `geometry()` call right after reads back a
position nowhere near it.

This looks like Mutter's XWayland integration not keeping the window's
decoration-frame position, as reported over X11, synced to where it actually
renders the window — the same `XTranslateCoordinates` mechanism
`xdotool`/`wmctrl` use has no other request to fall back on if that is what
is happening. Working hypothesis from one diagnostic session on one machine,
not an independently confirmed root cause or a survey of other window
managers. Be skeptical of `geometry()` results on GNOME's XWayland until
someone reproduces or debunks this.
