# ADR 001 — Prefer maintained libraries; keep the dependency count low

Status: accepted · 2026-08-22

## Context

Two constraints that pull against each other: don't reimplement low-level
functionality that a maintained library already provides, but don't accumulate
dependencies either.

## Decision

Resolve the tension along two axes rather than one.

**Every dependency is optional and per-backend.** The package has zero hard
dependencies. You install the extra for the backend you actually use, so the
*installed* count stays near one even though the *supported* set is broad. This
was already required for correctness — no single binding set exists across
GNOME, KDE, wlroots, X11 and the BSDs — so it costs nothing.

**A library must replace substantial work to earn its place. Otherwise adapt a
command-line tool.** Binding a C library from Python is itself low-level work;
shelling out to a maintained tool costs no dependency and inherits its upstream
maintenance.

## What we adopt, and why

| Layer | Choice | Replaces |
|---|---|---|
| Elements (AT-SPI) | **dogtail** | Tree search, predicates, retry-on-stale, action dispatch |
| Input injection | **wdotool / wtype / ydotool** via subprocess | libei, portal, and virtual-device protocol handling |
| Screen capture | **grim / gnome-screenshot / spectacle** via subprocess | Portal + PipeWire negotiation |
| Window control | **swaymsg / hyprctl / kdotool** | Compositor IPC protocols |
| X11 | **python-xlib** | Xlib protocol encoding |
| uinput fallback | **python-evdev** | ioctl-level device setup |

### dogtail

Actively maintained (2025 commits, GitLab home with a GitHub mirror), Red Hat
lineage, used for GNOME QA. It is the established AT-SPI automation framework
and already implements the accessible-tree work we would otherwise write.
`backends/atspi.py` is an adapter over it, not a reimplementation.

It needs PyGObject and pyatspi, but note it does **not** declare them:
dogtail's PyPI metadata has `requires_dist: null`, so pip installs dogtail alone
and expects the GNOME Python stack to come from the distribution. That is normal
for GObject-introspection packages — PyGObject is painful to build from source —
but it means the extra cannot express the full requirement, and the install
instructions must name the system packages explicitly.

The upside stands: where that stack is present it also provides GDBus through
Gio, so portal access needs no separate D-Bus library.

### Input via command-line tools, not bindings

The Python libei bindings that exist are hobby projects, so there is no
maintained library to adopt. `wdotool` is the right shape instead: it speaks
libei through the RemoteDesktop portal with a wlr virtual-device fallback,
which is exactly the mechanism ranking the audit arrived at — and using it
costs no Python dependency.

Tool preference order encodes the audit's keymap trap: `wdotool` and `wtype`
let the client supply its own keymap; `ydotool` injects scancodes below the
compositor, so typed text depends on the session's active layout. `ydotool` is
therefore ranked last among Wayland tools and flagged `keymap_safe=False`.

## Rejected

**pywayland** for a native foreign-toplevel backend. Still self-described as
developmental, and version 0.4.18-6 was marked for autoremoval from Debian
testing in August 2026. Building the window layer on it would put the least
stable dependency under the capability tier that is already hardest. Compositor
IPC tools (`swaymsg`, `hyprctl`) and D-Bus reach the same information.

**dbus-python** for portals. Uses libdbus, which has known problems with
multi-threaded use. Gio via PyGObject is already present; `jeepney` (pure
Python, zero dependencies) is the fallback if a PyGObject-free path is ever
needed.

**pynput / PyAutoGUI / mss** as a shortcut for the input and capture layers.
All are X11-only on Linux and would not reach native Wayland clients — the
exact failure the audit documents for XTest under XWayland.

## Consequences

- A minimal install is the package plus one extra, but the `atspi` extra
  additionally requires distro packages (see README). A virtualenv must be
  created with `--system-site-packages` to see them.
- Backends degrade to unsupported capabilities when a tool is absent, rather
  than failing at import.
- External tool versions are not pinned; `tools.py` probes PATH at runtime and
  reports what it found through `python -m pyguitest`.
- The keymap-safety distinction is visible to callers, so a test suite can
  refuse a keymap-unsafe tool rather than silently type the wrong characters.

## Sources

- <https://gitlab.com/dogtail/dogtail> · <https://github.com/vhumpa/dogtail>
- <https://github.com/cushycush/wdotool>
- <https://www.phoronix.com/news/LIBEI-Emulated-Input-Wayland>
- <https://github.com/flacjacket/pywayland> · <https://tracker.debian.org/pkg/pywayland>
- <https://python-evdev.readthedocs.io/> · <https://pypi.org/project/python-xlib/>
- <https://jeepney.readthedocs.io/> · <https://pypi.org/project/dbus-python/>
