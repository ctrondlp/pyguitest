# Dependencies and installation

**Nothing is required.** `pip install pyguitest` pulls nothing, and every
third-party import in the source is guarded, so the package imports and runs
with no other packages present — you get the tier-1 capabilities and an
honest report of what is missing.

Everything beyond that is opt-in, because no single set of packages spans
GNOME, KDE, wlroots, X11 and the BSDs. Which packages you want depends
entirely on which backend has to serve you, so start from the matrix.

**You do not need to read any of this.** `pyguitest doctor` reads
`/etc/os-release`, works out which distribution you are on, and prints the
exact command with the right package names already filled in — which is the
only way a document can give install advice without knowing whose machine it
is on. Everything here is for people who would rather look it up.

## What each backend needs

Backends are composed automatically: a session is usually several at once
(elements from one, injection from another, capture from a third). Install
for the rows you need; missing rows degrade to an unsupported capability
rather than an error.

| Backend | Serves | pip extra | From your distribution | On `PATH` |
|---|---|---|---|---|
| `atspi` | elements — buttons, text fields, dropdowns; windows on GNOME | `[atspi]` (dogtail) | PyGObject, pyatspi, at-spi2-core | — |
| `windows` | window control on sway, Hyprland, niri | — | — | — (unix sockets, stdlib only) |
| `windows` (KWin) | window control on KDE | — | — | `kdotool` |
| `gnomeshell` | window control and prompt-free per-window capture on GNOME | — | PyGObject | — (plus the [Shell extension](../gnome-shell-extension/README.md), installed by hand) |
| `input` | pointer and keyboard through a CLI tool | — | — | `wdotool`, `wtype`, `ydotool` or `xdotool` |
| `uinput` | in-process pointer and keyboard | `[uinput]` (evdev) | — | — (needs `/dev/uinput` access) |
| `eiinput` *(opt-in)* | keymap-safe input over libei | `[eiinput]` (python-libei) | `libei`, PyGObject | — |
| `portal` *(opt-in)* | keyboard and pointer buttons/scroll via the RemoteDesktop portal | — | PyGObject | — |
| `capture` | screenshots | — | — | `grim`, `gnome-screenshot`, `spectacle` or `import` |
| `portalcapture` *(opt-in)* | screenshots via the Screenshot portal, no tool needed | — | PyGObject | — |
| `imagesearch` | finding a control by a picture of it | — | — | `compare` (ImageMagick) |
| `x11` | everything, including tier-6, on X11 and XWayland | `[x11]` (python-xlib) | — | — |

`[dev]` (pytest, ruff, mypy) is the remaining extra; see
[CONTRIBUTING.md](../CONTRIBUTING.md).

Two rows are worth reading twice. `windows` on sway, Hyprland and niri needs
**nothing at all** — window control speaks their unix sockets using only the
standard library. And `x11` with python-xlib installed captures and encodes
PNGs itself, so it needs no screenshot tool either.

## By desktop, in practice

| Desktop | What you install |
|---|---|
| sway / Hyprland / niri | `pip install .` — nothing else |
| GNOME | `pip install '.[atspi]'` + three distribution packages |
| GNOME, pure Wayland (no XWayland) | as GNOME, plus the [pyguitest-window-control extension](../gnome-shell-extension/README.md) for window placement/minimize |
| KDE | as GNOME, plus `kdotool` for windows |
| X11 | `pip install '.[x11]'` |
| Any portal-supporting desktop, deliberately | `pip install '.[atspi]'` for PyGObject, then `connect(backend="portal")` and click Allow (once, if you opt into `persist_mode`) |
| Keymap-safe input over libei, deliberately | `pip install '.[atspi,eiinput]'` + your distribution's `libei`, then `connect(backend="eiinput")` |
| Screenshots with no tool installed at all, deliberately | `pip install '.[atspi]'` for PyGObject, then `connect(backend="portalcapture")` — the only capture path that works inside a Flatpak sandbox |

`pyguitest doctor` reports which of these you have and names what is missing
— except the GNOME Shell extension and portal rows, neither of which it can
detect without making a D-Bus call session detection deliberately avoids
(see `session.detect()`'s own docstring); both are opt-in, not autodetected,
and `portal` doubly so — see
[Backend registry](structure.md#backend-registry) for why `connect()` never
reaches it on its own.

## Distribution packages (pip cannot supply these)

The `atspi` extra is **not self-sufficient**: dogtail declares no
dependencies, and PyGObject does not build from source cleanly. `pyatspi` is
used by the AT-SPI backend but appears in no extra, for exactly the same
reason.

<!-- generated from pyguitest.hints._PACKAGES; tests/test_docs.py pins it -->

| | Fedora | Debian / Ubuntu | Arch | openSUSE |
|---|---|---|---|---|
| *install with* | `sudo dnf install` | `sudo apt install` | `sudo pacman -S` | `sudo zypper install` |
| Elements (AT-SPI) | `python3-gobject python3-pyatspi at-spi2-core` | `python3-gi python3-pyatspi gir1.2-atspi-2.0` | `python-gobject python-atspi at-spi2-core` | `python3-gobject python3-atspi at-spi2-core` |
| Screenshots | `gnome-screenshot` | `gnome-screenshot` | `grim` | `gnome-screenshot` |
| Input injection | `ydotool python3-evdev` | `ydotool python3-evdev` | `ydotool python-evdev` | `ydotool python3-evdev` |
| Image search | `ImageMagick` | `imagemagick` | `imagemagick` | `ImageMagick` |

An unrecognised distribution still gets the component names from `doctor`,
just without a command — the package names are the only part that cannot be
guessed.

`libei` for the `eiinput` backend is not in that table because it is not
part of any automatic path; see [input.md](input.md#eiinput-keymap-safe-input-over-libei).

A virtualenv needs `--system-site-packages` to see any of these.

## External tools (never installed by pip)

Discovered on `PATH` at runtime. Absence degrades a capability; it never
raises.

| Group | Tools |
|---|---|
| Input | `wdotool`, `wtype` *(wlroots only)*, `ydotool` *(keymap-unsafe)*, `xdotool` *(X11 only)* |
| Capture | `grim`, `gnome-screenshot` *(real X11 only)*, `spectacle`, `import` *(X11 only, and real X11 only)* |
| Windows | `swaymsg`, `hyprctl`, `niri msg`, `kdotool` |
| Image search | `compare` (ImageMagick) |

*Real X11 only* is stricter than *X11 only* and is about capture, not about
which clients a tool can see. These tools screenshot by reading the X root
window, and XWayland refuses that outright — a 1×1 read fails exactly as a
full-screen one does. So they are not selected on a Wayland session at all,
XWayland included; being installed there does not make them usable, and
`gnome-screenshot` in particular hangs for the full timeout before failing.

Which input tool you get, and what it does to your typing, is
[input.md](input.md).

## How capture picks a path

**On GNOME under Wayland, per-window capture is prompt-free if you install
the [Shell extension](../gnome-shell-extension/README.md).** It runs inside
gnome-shell, so it sidesteps the sender allowlist that closes every other
route, and it reads the window's own actor — an occluded window still comes
back whole. It has no whole-screen method. Read that extension's own README
before enabling it: while it is on, anything that can reach your session bus
can screenshot any window with no prompt.

**For the whole desktop on GNOME, the Screenshot portal is the capture
path** — verified end to end on GNOME Shell 50.4. The other two do not work
there and cannot be made to: `gnome-screenshot` cannot reach the Shell's own
screenshot interface (restricted to an allowlist of senders since GNOME 42),
falls back to X11, captures nothing, and hangs; and XWayland refuses
`GetImage` on the root window outright — a 1×1 request is rejected as firmly
as a full-screen one, under every pixmap format and plane mask.

```python
gui = pyguitest.connect(backend="portalcapture")
gui.screenshot("shot.png")
```

The first call raises a consent dialog. The grant then persists — the
desktop's permission store records it, and later calls are silent, so an
unattended suite prompts once on a machine and never again. Inspect or
revoke it with `flatpak permissions screenshot`. Note this is the *desktop*
remembering, not something pyguitest can ask for: unlike RemoteDesktop and
ScreenCast, the Screenshot portal has no `persist_mode` or `restore_token`
in its interface at all.

**X11 screen capture is withdrawn under XWayland**, and only there. An X
connection inside a Wayland session works perfectly well for input, window
control and everything else — but native Wayland surfaces are never
composited into the X root window, so a root-window grab cannot return the
desktop. On GNOME 50 it errors outright; the more dangerous outcome would
have been succeeding, since an empty X root is a perfectly valid image of
entirely the wrong thing. This is the same trap the tool registry already
encodes as *X11 only*. Per-window capture is unaffected: an XWayland-backed
X11 client has its own drawable with its own content. On a true X11 session
nothing changes.

**A tool that is installed but does not work no longer takes capture down
with it.** This is a recurring condition, not an accident: `import` is broken
on Fedora 43 (below), and `gnome-screenshot` cannot capture at all on GNOME
42+ Wayland. When the selected backend fails, the next capable one is tried,
the broken one is skipped for the rest of the session rather than costing
another timeout, and a `CaptureFallbackWarning` says which tool failed and
why — silently rescuing the call would hide a real problem that `pyguitest
doctor` cannot see, since the tool *is* installed.

ImageMagick also does the cropping whenever a region is asked of a tool that
has no exact-rectangle mode, so `gnome-screenshot` or `spectacle` alone
gives you whole-screen capture, and `magick`/`convert` alongside either
gives you regions and per-window capture too.

`import` (ImageMagick) is a capture tool on a real X11 session only. Where it
*is* selected, on Fedora 43 it is currently broken outright: `import -window
root` fails with `import: missing an image filename` regardless of
arguments, an
[upstream ImageMagick bug](https://github.com/ImageMagick/ImageMagick/issues/8459),
not a pyguitest one. On such a session, installing `gnome-screenshot`
sidesteps it — it outranks `import` in tool selection — and installing
`python-xlib` sidesteps both, since `X11Backend` then captures with no tool
at all.
