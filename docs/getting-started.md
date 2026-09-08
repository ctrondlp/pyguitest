# Getting started

Five minutes from nothing to a script that drives a real application. If you
already know what you want and just need the signature, go to
[api.md](api.md) instead.

## 1. Install

```sh
pip install 'pyguitest[atspi]'
```

The `atspi` extra is the one worth having from the start: it is what lets a
test say `gui.button("Save").click()` instead of clicking a coordinate.
Everything else is optional and depends on your desktop.

Then ask the machine rather than reading a table:

```sh
pyguitest doctor
```

`doctor` detects your distribution and prints the exact commands for whatever
is missing — including the pieces `pip` cannot supply, like the AT-SPI
libraries that come from your distribution. Run it before assuming anything
below does not work. [install.md](install.md) is the same information as a
reference table.

## 2. See what this desktop can do

```sh
pyguitest
```

That prints the capabilities of the session you are in right now. It matters
because **pyguitest never pretends**: a desktop that cannot move windows says
so, rather than silently doing nothing. Two machines can run the same script
and get different results, and this is where that difference is visible.

## 3. Drive something

```python
import pyguitest

with pyguitest.connect() as gui:
    editor = gui.wait_for_window("Text Editor", timeout=10)
    gui.activate_window(editor)

    gui.text_field("Name").set_text("Ada Lovelace")
    gui.button("Save").click()

    assert gui.wait_for_element(name="Saved", timeout=5)
```

`connect()` never raises just because a desktop is limited — a session with
few capabilities is the normal case, not an error. What raises is *asking for
something this session cannot do*, and it raises a typed
`CapabilityUnsupported` naming the capability.

## Which API should I use?

Four ways to say "click that thing", in the order you should reach for them:

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
forbidden — they are the floor, and pyguitest tells you when you have hit it.

If the application you are testing has no accessible names to match on, that
is fixable at the source: hand its developers
[testable-guis.md][testable-guis], which is written for exactly that
conversation.

## Which display server am I on, and why does it matter?

The same script does different things on each, because the *desktop* decides
what an outside program may do — not pyguitest.

| Session | What you get | The usual surprise |
|---|---|---|
| **X11** | Everything. Window control, input injection, screen and window capture, and the readback operations (pointer position, key state) that no Wayland compositor allows | Nothing is refused, so a script written here can quietly depend on things that do not exist elsewhere |
| **Wayland** (GNOME, KDE, sway, Hyprland, niri) | Elements and input, plus whatever window control that specific compositor exposes. Input injection needs consent or device permission | Window operations vary *by compositor*, not by "Wayland". GNOME and sway are as different from each other as either is from X11 |
| **XWayland** | An X11 client inside a Wayland session. X11 operations work for X11 clients only, and native Wayland clients are invisible to them | The one that catches people out: `xdotool` sees half your desktop and reports success |

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
Every wait in pyguitest is a wait for something *observable*:

```python
gui.wait_for_window("Save As", timeout=10)   # not sleep(2)
gui.wait_for_element(name="Export complete", timeout=30)
gui.wait_until(lambda: gui.get_clipboard() == "copied", timeout=5)
```

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
