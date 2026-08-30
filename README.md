# pyguitest

Cross-platform GUI automation for Python. Successor to
[X11::GUITest](https://metacpan.org/pod/X11::GUITest).

**Status:** every capability implemented across all backends, covering
**every X11::GUITest export**. Much of it has been run against real GNOME
Wayland and X11 sessions; some of it has not, and
[docs/validation.md](docs/validation.md) says exactly which is which, so
nothing here has to be taken on trust.

## Why the API is not a port

`docs/wayland-audit.html` classifies all 50 X11::GUITest 0.29 exports by what it
would cost to implement each on Wayland. The distribution is the finding:

| Tier | Count | Meaning |
|------|-------|---------|
| T1 Portable   | 9  | No display server involved; ports unchanged |
| T2 Direct     | 4  | Core Wayland protocol gives a real equivalent |
| T3 Compositor | 19 | Needs a separate backend per desktop |
| T4 Privileged | 8  | Input injection; consent or device access |
| T5 Rework     | 4  | Goal survives via AT-SPI; the model does not |
| T6 No path    | 6  | Deliberately prevented; dropped from the API |

Thirteen of fifty carry over unchanged. The largest block is not "impossible"
but "possible once per desktop" — window management is where a portable Wayland
implementation actually fails, and GNOME is the worst case because Mutter
implements neither foreign-toplevel protocol.

The tier scale is a *Wayland ceiling*, not an absolute one. On Wayland every
achievable capability is served and the tier-6 ones cannot exist; the X11
backend serves those too, so an X11 session gets a strictly larger capability
set through the same API. Discovering which one you are on is what
`supports()` is for.

## Design decisions that follow

- **Capability negotiation is public API.** With 19 functions varying by
  compositor, X11::GUITest's convention of returning zero on failure is
  untestable — it cannot distinguish "the click missed" from "this desktop
  cannot click". Every failure here is a typed exception, and callers can ask
  first.
- **Speak protocols directly; adapt tools only where no protocol exists.** sway,
  Hyprland and niri window control talks to their unix sockets using nothing but
  the stdlib, so no tool need be installed. dogtail covers elements, python-xlib
  covers X11, python-evdev covers in-process uinput; `kdotool` and the
  screenshot tools stay CLI adapters because they wrap genuinely hard work.
  See [ADR 001](docs/adr-001-dependencies.md) and
  [ADR 002](docs/adr-002-transports.md).
- **Input tools are ranked by keymap safety.** `wdotool` and `wtype` let the
  client supply a keymap; `ydotool` injects scancodes *below* the compositor, so
  `type_text("Hello")` produces different characters on an AZERTY session and no
  protocol reports the active layout. That ranking is encoded in `tools.py`, and
  detection warns when only keymap-unsafe tools are present.
- **AT-SPI leads.** It answers what the window-tree walk was really used for,
  needs neither geometry nor injection permission, and behaves identically under
  X11 and Wayland — the one layer needing no backend matrix.
- **The X11 backend is a peer, not a legacy path.** It is the surest route to
  the BSDs and Solaris — python-xlib speaks the wire protocol in pure Python and
  assumes no kernel — and the only backend serving tier-6 capabilities at all.
  Less is Linux-only than it looks: `/dev/uinput` and libei are kernel
  interfaces, but compositor IPC is a unix socket and JSON, and sway, grim and
  wtype are all in FreeBSD ports. Nothing here gates on the platform; nothing
  off Linux is tested either.
- **Compositor IPC fills the geometry hole.** sway, Hyprland and niri report
  window rectangles, which no Wayland protocol exposes — and once every rectangle is
  known, hit-testing a coordinate is arithmetic rather than a compositor query.
- **No hard dependencies.** Every mechanism is probed at runtime and degrades to
  an unsupported capability rather than an import error. Extras are per-backend,
  so a real install is the package plus one extra.

## Install

Requires Python 3.10 or newer.

```sh
pip install pyguitest              # core; no dependencies
pip install 'pyguitest[atspi]'     # + element automation
```

Or from a checkout, which is the same thing with a path instead of a name:

```sh
git clone https://github.com/ctrondlp/pyguitest.git
cd pyguitest
pip install .
pip install '.[atspi]'
```

You do not need `-e`; that flag is for developing *this package*, and is
covered in [CONTRIBUTING.md](CONTRIBUTING.md).

**None are required.** The package imports and runs with nothing else
installed. What you add depends on which backend has to serve your desktop
— extras (`atspi`, `x11`, `uinput`, `eiinput`, `dev`), a few distribution
packages pip cannot supply, and sometimes a tool on `PATH`. Rather than work
that out from a document, ask the machine:

```sh
pyguitest doctor
```

It detects your distribution and prints the exact commands. For the whole
picture — a per-backend requirements matrix, the distribution package table,
and how capture chooses a path — see [docs/install.md](docs/install.md).
Injecting input has its own setup (`/dev/uinput` permissions, the `ydotool`
daemon, libei, portal consent): [docs/input.md](docs/input.md).

## Usage

```python
import pyguitest

gui = pyguitest.connect()

# Widgets by what they are and what they are called -- the recommended way.
gui.button("OK").click()
gui.text_field("Name").set_text("Ada Lovelace")
gui.dropdown("Country").choose("Norway")

# Windows by title regex.
window = gui.find_window("Editor")

# Coordinates and keys, when you need them.
gui.move_mouse(500, 300)
gui.click()
gui.type_text("Hello")
gui.send_keys("^(a)^(c)")  # Ctrl-A, Ctrl-C

# Motion the toolkit can see, for drag-and-drop and hover.
gui.drag((120, 400), (600, 400))
```

Matching on role and name survives the application being moved or resized,
unlike clicking at `(842, 612)`. Ask before depending on anything that varies
by desktop:

```python
from pyguitest import Capability

if gui.supports(Capability.WINDOW_GEOMETRY):
    x, y, w, h = gui.geometry(window)
```

`connect()` never raises on a limited desktop — a session with few capabilities
is the normal case, and `supports()` is how you find out.

A session is usually several backends at once: elements from AT-SPI, injection
from a CLI adapter, capture from another. `CompositeBackend` merges their
capabilities and routes each call to whichever member provides it, so callers
see one object. `backend.providers()` shows the routing.

### Screenshots

```python
gui.screenshot("desktop.png")  # the whole desktop
gui.screenshot("editor.png", window=window)  # one window
gui.screenshot("corner.png", region=(0, 0, 400, 300))
```

`region` is `(x, y, width, height)` in screen coordinates — the same tuple
`gui.geometry(window)` returns, on every backend. You never write a tool's
own rectangle syntax; whichever tool the session picked gets its own built
for it. `window` is served two ways, and the difference shows in the image:
under X11 the window's own pixels are read, so anything stacked on top of it
is absent; everywhere else the rectangle is looked up and cut out of a
full-screen shot, which does include whatever is covering it.
`gui.supports(Capability.WINDOW_CAPTURE)` tells you which you are getting.

**Automatically, when a test fails.** Nothing captures on its own — a
screenshot has to be taken while the failure is still propagating, because
by the time an `except:` block runs the application under test is usually
gone. Wrap the part you want documented:

```python
with gui.capture_on_failure("artifacts"):
    gui.button("Save").click()
    assert gui.element(name="Saved")
```

Nothing is written when the block succeeds. On failure the image lands in
`artifacts/` (or `$PYGUITEST_SCREENSHOT_DIR`, or the temporary directory),
its path is attached to the exception as `.screenshot`, and the original
exception is re-raised untouched, so the test runner still reports the real
failure. A screenshot that itself fails is recorded on the exception as
`.screenshot_error` and swallowed — it never replaces the failure it was
trying to document.

## Examples

Runnable scripts in [examples/](examples/), each degrading with an
explanation when the desktop cannot do what it asks:

```sh
python3 examples/01_what_can_i_do.py     # start here
python3 examples/03_widgets.py           # buttons, text boxes, dropdowns
python3 examples/06_a_real_test.py       # the one to copy: a unittest suite
```

## Tools

```sh
pyguitest                     # what this desktop can actually do
pyguitest doctor              # what to install to unlock more
pyguitest debug               # everything needed to diagnose a bug report
pyguitest migrate script.pl   # what porting a Perl script involves
```

All four also work as `python -m pyguitest …` without installing.

`pyguitest debug` is what to paste into a bug report: package and Python
versions, every environment probe (not only the ones that came back true),
each detected tool's own `--version`, and whether the process is running
inside a Flatpak, toolbox, or other container -- which changes what every
other probe on this list actually sees. Add `--json` for a machine-readable
form.

The migration scanner reports the tier of every X11::GUITest call in a source
file and exits non-zero if any call has no Wayland path, so a port can be gated
in CI.

## Documentation

- [docs/install.md](docs/install.md) — what each backend needs, per
  distribution, and how capture picks a path
- [docs/input.md](docs/input.md) — injecting pointer and keyboard input:
  permissions, daemons, keymap safety, libei and the portal
- [docs/validation.md](docs/validation.md) — what has been run against a real
  desktop, and what has not
- [docs/structure.md](docs/structure.md) — the file tree, how a call flows
  through the layers, and the backend registry
- [ADR 001](docs/adr-001-dependencies.md) — why these libraries
- [ADR 002](docs/adr-002-transports.md) — why sockets replaced CLI tools
- [docs/wayland-audit.html](docs/wayland-audit.html) — the audit all of this
  derives from

## Contributing

Tests, lint, types, CI and the D-Bus suite: [CONTRIBUTING.md](CONTRIBUTING.md).

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).
