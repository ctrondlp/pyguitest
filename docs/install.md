# Dependencies and installation

**Nothing is required.** `pip install pyguitest` pulls nothing, and every
third-party import in the source is guarded, so the package imports and runs
with no other packages present — you get the tier-1 capabilities and an
honest report of what is missing.

Everything beyond that is opt-in, because no single set of packages spans
GNOME, KDE, wlroots, X11, the BSDs and Windows. Which packages you want
depends entirely on which backend has to serve you, so start from the matrix.

**Most readers can skip this page.** `pyguitest doctor` reads
`/etc/os-release`, works out which distribution you are on, and prints the
exact command with the package names filled in; on Windows and macOS it
answers in that platform's terms instead. This page is the same information
as a reference, for when you would rather look it up.

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
| `kwinevents` | window open/close/title events on KDE | — | PyGObject | — (loads an ad hoc KWin script at runtime; nothing to install) |
| `gnomeshell` | window control, prompt-free per-window capture, and outputs with their scale on GNOME | — | PyGObject | — (plus the [Shell extension](../gnome-shell-extension/README.md), installed by hand) |
| `input` | pointer and keyboard through a CLI tool | — | — | `wdotool`, `wtype`, `ydotool` or `xdotool` |
| `uinput` | in-process pointer and keyboard | `[uinput]` (evdev) | — | — (needs [`/dev/uinput` access](input.md#uinput-devuinput-permissions)) |
| `eiinput` *(opt-in)* | keymap-safe input over libei | `[eiinput]` (`python-libei[portal]`, which brings PyGObject with it) | `libei`, gobject-introspection | — |
| `portal` *(opt-in)* | keyboard and pointer buttons/scroll via the RemoteDesktop portal, and the clipboard with `clipboard=True` — the only clipboard path on GNOME | — | PyGObject | — |
| `capture` | screenshots | — | — | `grim`, `gnome-screenshot`, `spectacle`, `import` or `screencapture` |
| `portalcapture` *(opt-in)* | screenshots via the Screenshot portal, no tool needed | — | PyGObject | — |
| `imagesearch` | finding a control by a picture of it | — | — | `compare` or `magick` (ImageMagick) |
| `clipboard` | clipboard text | — | — | `wl-copy`/`wl-paste`, `xclip` or `xsel` (`pbcopy`/`pbpaste` on macOS) |
| `inputcapture` *(opt-in)* | reading the pointer position when it crosses a screen edge, via the InputCapture portal | `[eiinput]` | `libei` | — |
| `x11` | everything, including tier-6, on X11 and XWayland | `[x11]` (python-xlib) | — | — |

The `windows` *backend* (compositor window control on Linux) is unrelated to
the `[windows]` *extra* (UI Automation on Microsoft Windows); the names
collide for historical reasons. Windows and macOS have backends of their own
— `win32` and `uia`, `macos` and `macquartz` — described under
[On Windows](#on-windows) and [On macOS](#on-macos). `[dev]` (pytest, ruff,
mypy) is covered in [CONTRIBUTING.md](../CONTRIBUTING.md).

Two rows need less than they appear to. `windows` on sway, Hyprland and niri
needs **nothing at all**: window control speaks their unix sockets using only
the standard library. And `x11` with python-xlib installed captures and
encodes PNGs itself, so it needs no screenshot tool either.

## By desktop, in practice

| Desktop | What you install |
|---|---|
| sway / Hyprland / niri | `pip install pyguitest` — nothing else |
| GNOME | `pip install 'pyguitest[atspi]'` + three distribution packages |
| GNOME, pure Wayland (no XWayland) | as GNOME, plus the [pyguitest-window-control extension](../gnome-shell-extension/README.md) for window placement/minimize |
| KDE | as GNOME, plus `kdotool` for windows, and `gsettings set org.gnome.desktop.interface toolkit-accessibility true` or no application publishes any elements — [see below](#on-kde-one-more-step-that-is-not-a-package) |
| X11 | `pip install 'pyguitest[x11]'` for windows, input, capture and tier-6 — add `'pyguitest[atspi]'` too for elements, since AT-SPI works on X11 exactly as it does on GNOME |
| Any portal-supporting desktop, deliberately | `pip install 'pyguitest[atspi]'` for PyGObject, then `connect(backend="portal")` and click Allow (once, if you opt into `persist_mode`) |
| Keymap-safe input over libei, deliberately | `pip install 'pyguitest[eiinput]'` + your distribution's `libei`, then `connect(backend="eiinput")` |
| Screenshots with no tool installed at all, deliberately | `pip install 'pyguitest[atspi]'` for PyGObject, then `connect(backend="portalcapture")` — the only capture path that works inside a Flatpak sandbox |

`pyguitest doctor` reports which of these you have and names what is missing,
including the GNOME Shell extension. The opt-in rows are different:
`connect()` never selects them on its own, and `doctor` names one only where
it is the sole route (`portalcapture`, on a GNOME session with no other
capture path) — see [Backend registry](developers/structure.md#backend-registry)
for why they are opt-in.

## On Windows

No `/etc/os-release`, no package manager, and nothing pip cannot supply
except ImageMagick:

| | |
|---|---|
| `pip install pyguitest` | windows, input, screenshots and the clipboard — everything but the element tree |
| `pip install "pyguitest[windows]"` | + the element tree (UI Automation, through `comtypes`). Use double quotes: Command Prompt does not strip single ones |
| ImageMagick | `winget install ImageMagick.ImageMagick`, only for locating a control by a picture of it. That install provides `magick` without the legacy commands, which is enough: pyguitest calls `magick compare` and `magick identify` directly |

Every Windows mechanism is an API call — `SendInput`, `EnumWindows`, GDI
capture, the Win32 clipboard — so there is no CLI tool to install and no
daemon or group membership to arrange. There is no PRIMARY selection, so
`get_clipboard(primary=True)` raises.

The `windows` extra carries an environment marker, so
`pip install "pyguitest[windows]"` succeeds on every platform and only
installs something on Windows.

Two conditions stop a test without raising anything, and `pyguitest doctor`
names both when it sees them:

- **An elevated target window.** A process that is not elevated cannot inject
  into one that is: Windows (UIPI) drops the events without an error or a
  prompt.
- **No interactive desktop.** A service, a scheduled task running in session 0,
  or an SSH session cannot see the logged-in desktop, so `windows()` returns
  nothing.

`pyguitest debug` reports the rest of the environment the backends depend on:
window station, integrity level, DPI awareness, Windows build, and whether
`comtypes` is importable. [troubleshooting.md](troubleshooting.md#injected-input-vanishes-on-windows)
has the symptoms and fixes.

## On macOS

As on Windows, there is nothing to install from a package manager except,
optionally, ImageMagick:

| | |
|---|---|
| `pip install pyguitest` | screenshots (`screencapture`) and the clipboard (`pbcopy`/`pbpaste`), both of which ship with macOS |
| `pip install "pyguitest[macos]"` | + elements and windows through Accessibility (the `macos` backend), and input through `CGEventPost` (the `macquartz` backend), via PyObjC |
| ImageMagick | `brew install imagemagick`, only for locating a control by a picture of it |

The `macos` extra carries an environment marker on each requirement, so it
installs cleanly, as a no-op, on Linux and Windows. A Mac has a single
selection, so `get_clipboard(primary=True)` raises.

### Permissions

macOS gates each capability behind a privacy grant (TCC). None can be granted
from code; each is switched on by hand under **System Settings > Privacy &
Security**, and `pyguitest doctor` lists whichever are missing.

| Grant | Pane | Needed for | Without it |
|---|---|---|---|
| Accessibility | Accessibility | elements, window control | element queries come back empty rather than raising |
| Screen Recording | Screen Recording | screenshots, image search, window titles | `screencapture` writes a black image, so pyguitest raises `PermissionRequired` before calling it |
| PostEvent | Accessibility (listed in the same pane) | injected pointer and keyboard input | posted events are dropped silently |

A grant is recorded against an application, not against you: usually the app
that launched Python (Terminal, iTerm, an IDE), sometimes the signed Python
interpreter itself. If a grant seems to have no effect, look for both rows. A
virtualenv built on a different Python can count as a new application and
need granting again, and grants do not carry over to another machine or a
container. `tccutil reset Accessibility` withdraws an earlier answer.

`connect(backend="macos", backend_options={"request": True})` asks macOS to
show the Accessibility prompt. The PostEvent request shows nothing in the
cases measured so far (macOS 26.7), so grant that one by hand.

### Connecting with input

`macquartz` is **opt-in**: a plain `connect()` never composes it, so that no
session requests the PostEvent grant unless you asked. A plain `connect()` on
a Mac therefore gives elements, windows, screenshots and the clipboard, but no
pointer or keyboard input. Name the backends to add it:

```python
gui = pyguitest.connect(backend=["macquartz", "macos"])
```

That pair is the combination validated on a real Mac. Naming backends
composes exactly the ones named, so add `"capture"` and `"clipboard"` to that
list if the script also takes screenshots or uses the clipboard, and
`"imagesearch"` if it uses `locate_image` (which needs ImageMagick).

## Distribution packages (pip cannot supply these)

The `atspi` extra is **not self-sufficient**: dogtail declares no
dependencies, and PyGObject and pyatspi do not build cleanly from source, so
both come from your distribution. `pyguitest doctor` prints the command for
yours; the table is the same information.

<!-- generated from pyguitest.hints._PACKAGES; tests/test_docs.py pins it -->

| | Fedora | Debian / Ubuntu | Arch | openSUSE | FreeBSD |
|---|---|---|---|---|---|
| *install with* | `sudo dnf install` | `sudo apt install` | `sudo pacman -S` | `sudo zypper install` | `sudo pkg install` |
| Elements (AT-SPI) | `python3-gobject python3-pyatspi at-spi2-core` | `python3-gi python3-pyatspi gir1.2-atspi-2.0` | `python-gobject python-atspi at-spi2-core` | `python3-gobject python3-atspi at-spi2-core` | `py312-pygobject py312-atspi at-spi2-core` |
| Screenshots | `gnome-screenshot` | `gnome-screenshot` | `grim` | `gnome-screenshot` | `gnome-screenshot` |
| Input injection | `ydotool python3-evdev` | `ydotool python3-evdev` | `ydotool python-evdev` | `ydotool python3-evdev` | `ydotool py312-evdev` |
| Image search | `ImageMagick` | `imagemagick` | `imagemagick` | `ImageMagick` | `ImageMagick7` |

**A virtualenv needs `--system-site-packages`** to see these, since they are
installed into the system Python:

```sh
python3 -m venv --system-site-packages .venv
```

An unrecognised distribution still gets the component names from `doctor`,
just without a command.

FreeBSD's `py312-` prefix follows the ports tree's default Python, not the
interpreter you run: ports build these modules for one Python version, so on
a system whose `python3` is another version (GhostBSD 26.1 ships 3.11 against
a 3.12 ports default) the package installs and the import still fails.
`pyguitest doctor` reports what it can actually load.

`libei`, for the opt-in `eiinput` backend, is not in the table; see
[input.md](input.md#eiinput-keymap-safe-input-over-libei).

### AT-SPI also needs the bus to be running

The packages are one half; a running accessibility bus is the other. Ask
for it directly:

```sh
gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus     --method org.a11y.Bus.GetAddress
```

A desktop session starts `at-spi-bus-launcher` for you and this answers
with an address. A container, a CI runner or a headless session often has
neither the service nor a session bus to start it on; there the `atspi`
backend declines with a message naming the address it looked for.
`pyguitest debug` reports the same answer on its `a11y bus` line, or "could
not ask" when `gdbus` is missing.

On an **X11 session the address comes from the root window, not the session
bus.** When `DISPLAY` is set and `WAYLAND_DISPLAY` is not, libatspi reads the
`AT_SPI_BUS` property on the root window, and nothing clears that property
when the bus it names goes away — a crashed session or test harness can leave
it pointing at a dead socket while `GetAddress` above answers for a live one.
Check the property itself:

```sh
xprop -root AT_SPI_BUS
```

libatspi aborts the whole process when it cannot reach the bus, so pyguitest
checks this address before loading it: a stale property shows up as a
declined `atspi` backend and on `debug`'s `a11y bus` line, not as a crash.
`xprop -root -remove AT_SPI_BUS` deletes the stale property from the running X
server, after which libatspi should fall back to the session bus.

### Chromium and Electron apps need an AT to be announced

VS Code, Chrome, Slack and anything else built on Chromium or Electron
publish **no accessibility elements at all** until something says an
assistive technology is running:

```sh
gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus     --method org.freedesktop.DBus.Properties.Get org.a11y.Status IsEnabled
```

`false` is the normal answer on a desktop with no screen reader. The symptom
is that `windows()` lists the window (that comes from the compositor) while
the application has no node in the accessibility tree at all, so
`window_element()` and every element query behave as though it were not
running.

For a test that launches the application itself, start it with
`--force-renderer-accessibility`. For an application that is already
running, set the property to true. That covers every application in the
session, and they run slower for as long as it stays set:

```sh
gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus     --method org.freedesktop.DBus.Properties.Set org.a11y.Status IsEnabled "<true>"
```

The same call with `"<false>"` turns it back off. pyguitest itself only reads
this property; `pyguitest debug` reports it on its `chromium a11y` line.

### On KDE, one more step that is not a package

GTK applications load their AT-SPI bridge only when the GNOME setting
`toolkit-accessibility` is on. A GNOME session has it on; a KDE session
usually does not, and **the symptom is not an error**: the packages are
installed, `doctor` lists AT-SPI as present, and element queries simply come
back empty. Turn it on:

```sh
gsettings set org.gnome.desktop.interface toolkit-accessibility true
```

`doctor` warns about this on KDE, and `debug` reports the value on every
desktop. The warning is limited to KDE because the setting being off is not a
problem everywhere: a GNOME session has been seen with it off and AT-SPI
working normally.

## Building from source, on an older build environment

Installing from a wheel needs nothing special; this section is only about
building from source. Both this package and pyguitest-recorder declare their
license as an SPDX string (PEP 639) — `license = "GPL-2.0-or-later"` in
`pyproject.toml`, rather than the older `license = {text = "..."}` table.
That form needs **setuptools 77 or newer**. An older one either fails the
build or silently drops the license metadata from the wheel, and its error
names the metadata rather than anything in this package. `pip` in a fresh
virtualenv gets a new enough setuptools on its own; the failure comes from a
system Python or build environment that pins an old one.
`pip install -U setuptools`, or `python -m build` in a fresh virtualenv,
fixes it.

## External tools (never installed by pip)

Discovered on `PATH` at runtime. Absence degrades a capability; it never
raises.

| Group | Tools |
|---|---|
| Input | `wdotool`, `wtype` *(wlroots only)*, `ydotool` *(keymap-unsafe)*, `xdotool` *(X11 only)* |
| Capture | `grim`, `gnome-screenshot` *(real X11 only)*, `spectacle`, `import` *(X11 only, and real X11 only)*, `screencapture` *(macOS only)* |
| Clipboard | `wl-copy`/`wl-paste` (wl-clipboard — not on GNOME, where the `portal` row above is the only clipboard path), `xclip`, `xsel` *(X11 only)*, `pbcopy`/`pbpaste` *(macOS only)* |
| Windows | `swaymsg`, `hyprctl`, `niri msg`, `kdotool` |
| Image search | `compare` or `magick` (ImageMagick) |

*X11 only* means the tool needs an X connection, which XWayland carries, so
it is selected on an XWayland session too. What limits it there is which
clients it can see, never whether it runs.

*Real X11 only* is stricter, and is about capture rather than about which
clients a tool can see. These tools screenshot by reading the X root window,
and XWayland refuses that outright — a 1×1 read fails exactly as a
full-screen one does. So they are not selected on a Wayland session at all,
XWayland included; being installed there does not make them usable, and
`gnome-screenshot` in particular hangs for the full timeout before failing.

Which input tool you get, and what it does to your typing, is
[input.md](input.md).

## Environment variables

Two, both optional, both honoured on every platform. Nothing here changes
which capability is available; they are the only two knobs the package reads
from the environment.

| Variable | What it does |
|---|---|
| `PYGUITEST_SCREENSHOT_DIR` | Where `screenshot()` writes its file when it is not given a `path`, and where `capture_on_failure()` writes its bundle when it is not given a `directory`. Falls back to the system temporary directory, which is why a failure bundle can be hard to find after a long run. |
| `PYGUITEST_DOGTAIL_LOGS` | Set it to anything non-empty and the noise `dogtail` prints while it is imported is let through instead of swallowed. It is swallowed because it includes a multi-line complaint about `gnome-ponytail-daemon` that alarms a first-time reader; that complaint is worth reading when element geometry is what is failing, which is what this is for. |

Neither is in the environment block `pyguitest debug` prints. That block lists
the variables that decide *which session this is* (`XDG_SESSION_TYPE`,
`WAYLAND_DISPLAY`, `DISPLAY`, `SWAYSOCK` and the rest); these two decide where
output lands and how loud an import is.

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
desktop. On GNOME 50 the grab fails outright; had it succeeded, it would
have returned an image of the wrong thing. Per-window capture is unaffected:
an XWayland-backed X11 client has its own drawable with its own content. On a
real X11 session nothing changes.

**A tool that is installed but broken does not take capture down with
it.** This happens in practice: `import` is broken on Fedora 43 (below), and
`gnome-screenshot` cannot capture at all on GNOME 42+ Wayland. When the
selected backend fails, the next capable one is tried, the broken one is
skipped for the rest of the session, and a `CaptureFallbackWarning` says
which tool failed and why — `pyguitest doctor` cannot see that problem,
since the tool *is* installed.

ImageMagick also does the cropping whenever a region is asked of a tool that
has no exact-rectangle mode, so `gnome-screenshot` or `spectacle` alone
gives you whole-screen capture, and `magick`/`convert` alongside either
gives you regions and per-window capture too.

It does the template matching for the same reason. `IMAGE_LOCATE` is served by
`compare` (ImageMagick 6, or a 7 that installed the legacy commands) or by
`magick` (`magick compare` and `magick identify` under IM7's dispatcher, which
is the only entry point a `winget` install lays down) — see
[troubleshooting.md](troubleshooting.md#template-matching-is-slow-or-times-out).
Cost scales with the area searched, not with the template: on a build
without ImageMagick's FFT delegate, one small window takes a few seconds and
a full 1080p desktop about a minute, so `locate_image(within=...)` is worth
passing for speed as well as accuracy.

`import` (ImageMagick) is a capture tool on a real X11 session only. On
Fedora 43 it is currently broken: `import -window root` fails with
`import: missing an image filename` whatever the arguments
([ImageMagick issue 8459](https://github.com/ImageMagick/ImageMagick/issues/8459)). On such a session, installing `gnome-screenshot`
sidesteps it — it outranks `import` in tool selection — and installing
`python-xlib` sidesteps both, since `X11Backend` then captures with no tool
at all.