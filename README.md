# pyguitest

Cross-platform GUI automation for Python — supporting Wayland, X11, and
XWayland.

pyguitest is the Python successor to
[X11::GUITest](https://metacpan.org/pod/X11::GUITest), providing a single API
for mouse, keyboard, windows, screenshots, and accessible UI elements.

**Status:** every capability implemented across all backends, covering
**every X11::GUITest export**. Much of it has been run against real GNOME
Wayland and X11 sessions; some of it has not, and
[docs/validation.md](docs/validation.md) says exactly which is which, so
nothing here has to be taken on trust.

Because desktops differ in what they permit, what a session can do is
discovered at runtime rather than assumed — `gui.supports(...)` is how you
ask, and [docs/design.md](docs/design.md) is why the API is shaped that way
instead of being a one-to-one port.

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
unlike clicking at `(842, 612)`. `elements()`/`element()` take more than
role and name -- `enabled`/`visible` filter on state, `name`/`description`
take a compiled regex instead of an exact string, and `predicate` is an
escape hatch for anything else (an ancestor/descendant check, say):

```python
from pyguitest import Role

gui.elements(role=Role.PUSH_BUTTON, enabled=True)
gui.element(name=re.compile(r"^Save"))
gui.element(role=Role.CHECK_BOX, within=gui.window_element("Preferences"))
```

Ask before depending on anything that varies by desktop:

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
pyguitest inspect             # the accessible tree of every open window
pyguitest migrate script.pl   # what porting a Perl script involves
```

All five also work as `python -m pyguitest …` without installing.

`pyguitest debug` is what to paste into a bug report: package and Python
versions, every environment probe (not only the ones that came back true),
each detected tool's own `--version`, and whether the process is running
inside a Flatpak, toolbox, or other container -- which changes what every
other probe on this list actually sees. Add `--json` for a machine-readable
form.

`pyguitest inspect` walks the accessible tree of every open window and
prints it, grouped by application -- the tool for seeing what `gui.button(...)`
or `gui.element(role=..., name=...)` actually has to match against, without
writing a script first. `--window TITLE_REGEX` narrows it to one application;
`--json` gives the same tree as machine-readable data, the same split
`debug` uses.

The migration scanner reports the tier of every X11::GUITest call in a source
file and exits non-zero if any call has no Wayland path, so a port can be gated
in CI.

## Documentation

- [docs/api.md](docs/api.md) — the full API reference: every public class,
  method and enum, with the capability each one needs
- [docs/install.md](docs/install.md) — what each backend needs, per
  distribution, and how capture picks a path
- [docs/input.md](docs/input.md) — injecting pointer and keyboard input:
  permissions, daemons, keymap safety, libei and the portal
- [docs/validation.md](docs/validation.md) — what has been run against a real
  desktop, and what has not
- [docs/design.md](docs/design.md) — why the API is not a port, and the
  decisions that follow from that
- [docs/structure.md](docs/structure.md) — the file tree, how a call flows
  through the layers, and the backend registry
- [ADR 001](docs/adr-001-dependencies.md) — why these libraries
- [ADR 002](docs/adr-002-transports.md) — why sockets replaced CLI tools
- [docs/wayland-audit.md](docs/wayland-audit.md) — the audit all of this
  derives from: all 50 X11::GUITest exports, classified
- [docs/upstream.md](docs/upstream.md) — the two Wayland protocol gaps worth
  taking upstream, written as issue text

## Contributing

Tests, lint, types, CI and the D-Bus suite: [CONTRIBUTING.md](CONTRIBUTING.md).

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).
