# Getting started

Five minutes from nothing to a script that drives a real application. If you
already know what you want and just need the signature, go to
[api.md](api.md) instead.

## 1. Install

Python 3.10 or newer. Pick the line for your platform:

```sh
pip install "pyguitest[atspi]"     # Linux and the BSDs — element automation
pip install "pyguitest[x11]"       # X11 or XWayland — native capture, and the reads only X11 allows
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

Note what `atspi` does *not* add: it is the element tree, not input. Typing
and clicking come from a CLI tool the desktop already has, or from `uinput`
or `eiinput` above — which is why `pyguitest doctor` below reports the two
separately.

One piece of vocabulary used throughout these pages: every capability sits
in a **tier**, which says how portable it is rather than how useful. Tier 1
works everywhere; tier 6 means *deliberately prevented* — reading another
window's keystrokes, say, which only X11 allows and which Wayland refuses
by design. So "a tier-6 read" is not a feature you are missing out on; it
is one the platform has decided nobody gets. The full scale is in
[api.md](api.md#tiers).

Then ask the machine what else it needs:

```sh
pyguitest doctor
```

It reads like this — abridged, from one GNOME session over Xwayland, so
yours will differ in the particulars and that is the point of running it:

```text
session      xwayland
compositor   mutter (GNOME)
input        uinput (in-process)
tools        compare, magick, xclip
mechanisms   libei, portal, uinput, at-spi, xtest
note         Mutter implements no foreign-toplevel protocol; window
             capabilities need a Shell extension

To unlock more capabilities:

  screenshots -- screen capture: no screenshot tool can work on this
      session, but the Screenshot portal can -- connect(
      backend="portalcapture"). It prompts for consent once, then the
      desktop remembers it
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
- **macOS:** grant **Accessibility**, **Screen Recording** and, for scripts
  that inject input, **PostEvent** under System Settings > Privacy &
  Security. A plain `connect()` composes input along with everything else —
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

The shape to expect — the same session as above, heavily abridged, with the
tiers §1 glossed and a `[yes]`/`[ no]` column for *this* session:

```text
backend      gnomeshell+atspi+uinput+clipboard:xclip+imagesearch:compare+x11

T1 PORTABLE -- No display server involved
  [yes] IMAGE_LOCATE
  [yes] PROCESS_LAUNCH
  [yes] TIMING
T2 DIRECT -- Core Wayland protocol
  [yes] SCREEN_INFO
T3 COMPOSITOR -- Per-desktop backend
  ...
```

A `[ no]` is not necessarily a problem to fix: it can be the desktop's
design (tier 6), a tool to install, or a permission to grant — `doctor` is
what says which.

## 3. Drive something

This is the shape of a script rather than one to run: the window and the
three element names belong to an imagined address-book application, because
no stock editor has a `Name` field or a `Save` button that answers with a
`Saved` label, and what matters here is the shape — find the window, act
through named elements, assert on what the application did. For a script that
runs as written, [`examples/04_drive_an_editor.py`](../examples/04_drive_an_editor.py)
launches a real editor with `start_app()`, types into it and closes it; the
Windows and macOS versions further down are self-contained too.

```python
import pyguitest

with pyguitest.connect() as gui:
    contacts = gui.expect_window("Contacts", timeout=10)
    gui.activate_window(contacts)

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

### The same first script on Windows

Everything above is the Linux spelling of a shape that is the same
everywhere: launch, find the window, act through named elements. On Windows
the stock editor makes a self-contained first script — run it from the
logged-in desktop session, since a service or SSH shell has no desktop to
drive:

```python
import pyguitest
from pyguitest import Role

with pyguitest.connect() as gui, gui.start_app(["notepad.exe"]) as app:
    window = gui.expect_window("Notepad", timeout=15)
    if not gui.focus_window(window):
        raise RuntimeError("Notepad did not take keyboard focus")
    gui.type_text("Hello from pyguitest")
    gui.close_window(window)  # asks; Notepad answers, or just goes
    prompt = gui.wait_for_element(Role.PUSH_BUTTON, "Don't Save", timeout=5)
    if prompt is not None:
        prompt.click()
    if not gui.wait_window_close(window, timeout=5):
        raise RuntimeError("Notepad is still open")
```

Three things in there are the shape of every script that types, not Notepad
quirks. `focus_window` rather than `activate_window`: activating asks the
window manager once and returns at once, so text typed straight afterwards
can land in whatever held focus before — `focus_window` asks, confirms the
window actually holds focus, retries, and reports `False` when it never
did, which is the one answer worth stopping on. `wait_for_element` rather
than `button` for the prompt: `close_window` returns as soon as the request
is delivered, and the application puts its prompt up a moment later, so a
lookup that does not wait finds nothing. And the prompt is met rather than
assumed: `close_window` posts the same request the title-bar button does,
so an application with unsaved work answers with its own prompt rather than
exiting — which is the behaviour a test usually wants to meet head-on. But
Windows 11's Notepad, left on its default *Open content from the previous
session* setting, keeps unsaved text for the next launch and closes without
asking, so a script that *requires* the prompt fails on a stock machine.
`wait_for_element` returns `None` when nothing appears in time, where
`expect_element` would raise; the script takes either answer and then
confirms the one thing it actually wanted, that the window is gone.

Run it with no other Notepad window open: `"Notepad"` matches any window of
that title, and the first script has no better handle to go by — on
Windows 11, `notepad.exe` is a short-lived launcher that hands off to the
packaged app, so `app.pid` is *not* the window's process and
`expect_window(pid=app.pid)` would wait for a window that never comes.
Packaged Store applications launch by a different spelling; see
[recipes.md](recipes.md#native-macos-and-windows-applications).

### And on macOS

```python
import pyguitest

with pyguitest.connect() as gui, gui.start_app(["TextEdit"]) as app:
    # TextEdit decides what to show at launch: a blank document, or on a
    # Mac signed into iCloud the iCloud Drive browser. Wait for whichever
    # it is, then ask for a document outright -- File > New works from
    # either -- and find *that* one. A document-based app titles its
    # window after the document, so look by process and title, not by
    # the app's name.
    opened = gui.expect_window(pid=app.pid, timeout=15)
    if not gui.focus_window(opened):
        raise RuntimeError("TextEdit did not come to the front")
    gui.send_keys("#(n)")
    window = gui.expect_window("Untitled", pid=app.pid, timeout=15)
    if not gui.focus_window(window):
        raise RuntimeError("TextEdit did not take keyboard focus")
    gui.type_text("Hello from pyguitest")
```

Before this runs, System Settings must grant the permissions listed in
§1 — `doctor` says which are missing. `start_app` takes the installed-app
name and launches the bundle properly; the plain-executable pattern breaks
on `.app` bundles in measured ways, also in
[recipes.md](recipes.md#native-macos-and-windows-applications). The
`File > New` step is what makes the script land its text in a document of
its own: with iCloud Drive on for TextEdit — the default on a Mac signed
into iCloud — the first window up is the file browser, and typing into it
searches rather than writes. `#` is how `send_keys` spells the Command key
on macOS; [api.md](api.md#modifier-prefixes) says what the prefix holds on
the other platforms.

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
| **macOS** | Elements, input and capture behind a privacy grant in System Settings; window control, the screen list and the pointer position need no grant | A gated capability is withdrawn rather than raised when its grant is missing, so `supports()` is the only honest answer — and an ungated one answers on a Mac with nothing granted at all |

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

`timeout` is in seconds everywhere, and `None` — which every `gui.wait_*`
call defaults to — means wait indefinitely; there is no session-wide default. Whether
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
