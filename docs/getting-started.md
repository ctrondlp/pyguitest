# Getting started

Five minutes from nothing to a script that drives a real application. If you
already know what you want and just need the signature, go to
[api.md](api.md) instead.

## 1. Install

Python 3.10 or newer. Pick the line for your platform:

```sh
pip install "pyguitest[atspi]"     # Linux and the BSDs — elements, and input
pip install "pyguitest[x11]"       # X11 or XWayland — tier-6 reads, native capture
pip install "pyguitest[windows]"   # Windows
pip install "pyguitest[macos]"     # macOS
```

`atspi`, `windows` and `macos` add the element tree for their platform —
AT-SPI, UI Automation or macOS Accessibility — which is what lets a test say
`gui.button("Save").click()` instead of clicking a coordinate. `x11` adds the
X11 backend; on GNOME or KDE you can also add `"pyguitest[uinput]"` (input
through `/dev/uinput`) or `"pyguitest[eiinput]"` (keymap-safe input over
libei). The full set, and which platforms each one is for, is in
[install.md](install.md#the-extras-all-of-them). Double quotes work in every
shell, including Command Prompt, which does not strip single ones.

Then ask the machine what else it needs:

```sh
pyguitest doctor
```

`doctor` prints the exact commands for whatever is missing: your
distribution's packages on Linux and the BSDs (the AT-SPI libraries come from
there, not from pip), and the settings to change on Windows and macOS.
[install.md](install.md) is the same information as a reference.

A few platform steps `doctor` will point you to, so they are no surprise:

- **Linux and the BSDs:** if you use a virtualenv, create it with
  `--system-site-packages`, or it cannot see the AT-SPI packages your
  distribution installed. Injecting input may need a permission or a daemon;
  see [input.md](input.md).
- **Windows:** nothing further for most tests. Run the suite from the
  logged-in desktop session, not a service or an SSH shell.
- **macOS:** grant **Accessibility** and **Screen Recording** under System
  Settings > Privacy & Security, and pass
  `connect(backend=["macquartz", "macos"])` when the script injects input —
  see [install.md](install.md#on-macos).

## 2. See what this desktop can do

```sh
pyguitest
```

That prints the capabilities of the session you are in. Desktops differ in
what they allow an outside program to do, so two machines can run the same
script and get different results; this is where that difference shows. A
desktop that cannot, say, move windows reports it here, and the call raises
rather than silently doing nothing.

## 3. Drive something

```python
import pyguitest

with pyguitest.connect() as gui:
    editor = gui.expect_window("Text Editor", timeout=10)
    gui.activate_window(editor)

    gui.text_field("Name").set_text("Ada Lovelace")
    gui.button("Save").click()

    gui.expect_element(name="Saved", timeout=5)
```

`expect_window` and `expect_element` raise when nothing matches in time;
their `wait_for_*` siblings return `None` instead, for when absence is an
answer rather than a failure. A plain string title matches as a literal
substring — pass `re.compile(...)` for a regular expression.

`connect()` never raises just because a desktop is limited — a session with
few capabilities is the normal case, not an error. What raises is *asking for
something this session cannot do*, and it raises a typed
`CapabilityUnsupported` naming the capability.

## Which API should I use?

Ways to say "click that thing", in the order you should reach for them:

| You want to… | Use | Needs | Survives a window move? |
|---|---|---|---|
| Act on a named control | `gui.button("Save").click()` | `ELEMENT_ACTION` | Yes |
| Act on a control you can only describe | `gui.element(role=Role.CHECK_BOX, name=re.compile("^Auto")).click()` | `ELEMENT_ACTION` | Yes |
| Work inside a specific window | `gui.element(..., within=gui.window_element("Preferences"))` | `ELEMENT_TREE` | Yes |
| Click something with no accessible name at all | `m = gui.locate_image("icon.png")`, then move and click at `m.x, m.y` | `IMAGE_LOCATE`, `SCREEN_CAPTURE` | Yes, but breaks on theme changes |
| Click a fixed screen position | `gui.move_mouse(842, 612); gui.click()` | `POINTER_MOVE`, `POINTER_BUTTON` | No |

### The locator hierarchy

Prefer whatever is highest on this ladder that the application actually
supports:

```
    element by role + name          gui.button("Save")
        ↓  ambiguous?
    element + constraints           gui.element(role=..., name=..., enabled=True)
        ↓  several windows involved?
    element scoped to a window      gui.element(..., within=gui.window_element("Preferences"))
        ↓  nothing accessible to match?
    image match                     gui.locate_image("save-icon.png")
        ↓  nothing on screen to match either?
    raw coordinates                 gui.move_mouse(x, y); gui.click()
```

Each step down trades robustness for reach. A named element keeps working
when the window moves, the theme changes, or a toolbar item is added; a
coordinate stops working at the first of those. Coordinates are not
forbidden; they are the last resort.

If the application you are testing has no accessible names to match on, that
is fixable at the source: hand its developers
[testable-guis.md][testable-guis], which is written for exactly that
conversation.

## Which desktop am I on, and why does it matter?

The same script can behave differently on each, because the *desktop* decides
what an outside program may do.

| Session | What you get | The usual surprise |
|---|---|---|
| **X11** | Everything. Window control, input injection, screen and window capture, and the readback operations (pointer position, key state) that no Wayland compositor allows | Nothing is refused, so a script written here can quietly depend on things that do not exist elsewhere |
| **Wayland** (GNOME, KDE, sway, Hyprland, niri) | Elements and input, plus whatever window control that specific compositor exposes. Input injection needs consent or device permission | Window operations vary *by compositor*, not by "Wayland". GNOME and sway are as different from each other as either is from X11 |
| **XWayland** | An X11 client inside a Wayland session. X11 operations work for X11 clients only, and native Wayland clients are invisible to them | The one that catches people out: `xdotool` sees half your desktop and reports success |
| **Windows** | Window control, input, capture and elements, through Win32 and UI Automation, with no daemon or permission to set up | Injected input into an elevated window is dropped silently unless the test runs elevated too |
| **macOS** | Elements, windows, capture and input, each behind a privacy grant in System Settings | Injected input is opt-in: a plain `connect()` has no pointer or keyboard until `macquartz` is named |

`pyguitest` reports which one you are on:

```python
print(gui.environment.summary())
```

Write for the capability, not for the display server:

```python
from pyguitest import Capability

if gui.supports(Capability.WINDOW_GEOMETRY):
    x, y, w, h = gui.geometry(window)
```

## Wait for state, never for time

The single most common way to write a flaky GUI test is `time.sleep(2)`.
Almost every wait in pyguitest is a wait for something *observable*:

```python
gui.wait_for_window("Save As", timeout=10)  # not sleep(2)
gui.wait_for_element(name="Export complete", timeout=30)
gui.wait_until(lambda: gui.get_clipboard() == "copied", timeout=5)
```

The exceptions are the two that are not a wait for state: `wait(seconds)`,
which is the sleep this section is telling you not to reach for, and
`sync(timeout)`, which is a confirmation that the compositor consumed the
input just sent — `libei` is the only backend that can answer it.

`timeout` is in seconds everywhere, and `None` — which every `wait_*` call
defaults to — means wait indefinitely; there is no session-wide default. Whether
a window wait is answered by an event or by a poll depends on the platform:
notified through `SetWinEventHook` on Windows and by the compositor on GNOME
Shell, KWin, sway and niri, polling every `interval` on macOS and on every X11
session. [api.md](api.md#waiting) has the table.

The full list, and when each one is the right one, is in
[recipes.md](recipes.md#waiting-instead-of-sleeping).

## Where to go next

| If you want… | Read |
|---|---|
| Task-shaped answers ("how do I fill a form?") | [recipes.md](recipes.md) |
| Something is not working | [troubleshooting.md](troubleshooting.md) |
| Every method and what it needs | [api.md](api.md) |
| Input permissions, daemons, keymap safety | [input.md](input.md) |
| To port an existing X11::GUITest script | [recipes.md](recipes.md#x11guitest-cheat-sheet) |
| To have an AI assistant write pyguitest code | [ai-assistants.md](ai-assistants.md) |
| Runnable examples | [../examples/](../examples/) — start with `01_what_can_i_do.py` |

[testable-guis]: https://github.com/ctrondlp/pyguitest-recorder/blob/main/docs/testable-guis.md
